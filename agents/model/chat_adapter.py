"""Narrow coding Responses/Chat conversion. Codex owns tools and compaction.

Reasoning stays in process memory and is replayed only for items still present
in SDK input. A complete upstream turn is validated before exposing any tools.
"""
from copy import deepcopy
from http.client import IncompleteRead
from hashlib import sha256
import json
import secrets
import time


# Exact fallback instruction lines observed on pinned SDK/CLI 0.156.1. That
# model profile mentions apply_patch even when it does not declare the tool.
# Match whole SDK lines only; never rewrite requirements or conversation input.
_SDK_PATCH_LINES = (
    r'- Use the `apply_patch` tool to edit files (NEVER try `applypatch` or `apply-patch`, only `apply_patch`): {"command":["apply_patch","*** Begin Patch\\n*** Update File: path/to/file.py\\n@@ def example():\\n- pass\\n+ return 123\\n*** End Patch"]}',
    "- Do not waste tokens by re-reading files after calling `apply_patch` on them. The tool call will fail if it didn't work. The same goes for making folders, deleting folders, etc.",
    'The user is working on the same computer as you, and has access to your work. As such there\'s no need to show the full contents of large files you have already written unless the user explicitly asks for them. Similarly, if you\'ve created or modified files using `apply_patch`, there\'s no need to tell users to "save the file" or "copy the code into a file"—just reference the file path.',
)


def _sdk_tool_instructions(instructions, offered):
    if 'apply_patch' in offered:
        return instructions
    if not offered:
        edit_rule = '- No tools are available for this request. Return text only; do not emit tool calls.'
    else:
        edit_rule = '- Use only the currently declared tools (' + ', '.join(sorted(offered)) + ') and their provided argument schemas.'
        if 'exec_command' in offered:
            edit_rule += ' Use exec_command for file edits, then check the command result.'
        edit_rule += ' Do not call an unlisted file-editing tool.'
    replacements = dict(zip(_SDK_PATCH_LINES, (
        edit_rule,
        "- Do not waste tokens by re-reading files after a successful file edit. Check the editing command's result for failures. The same goes for making folders, deleting folders, etc.",
        _SDK_PATCH_LINES[2].replace(' using `apply_patch`', ''),
    )))
    # Keep line terminators and all unmatched SDK content byte-for-byte.
    parts = []
    for line in instructions.splitlines(keepends=True):
        content = line.rstrip('\r\n')
        parts.append(replacements.get(content, content) + line[len(content):])
    return ''.join(parts)


class ChatProtocolError(ValueError):
    """Protocol rejection with a fixed, payload-free diagnostic code."""
    _CODES = {
        'Unsupported message content': 'unsupported_message_content',
        'Only rewritten text content is supported': 'unsupported_message_content',
        'Invalid text content': 'invalid_text_content',
        'Unsupported coding tool': 'unsupported_coding_tool',
        'Expected complete SDK input': 'invalid_sdk_input',
        'Encrypted remote compaction is not supported': 'encrypted_compaction',
        'Missing call ID': 'missing_call_id',
        'Missing previous tool results': 'missing_tool_results',
        'Unmatched tool result': 'unmatched_tool_result',
        'Message interrupts tool results': 'interrupted_tool_results',
        'Unsupported message role': 'unsupported_message_role',
        'Tool results missing from SDK input': 'missing_tool_results',
        'Expected one choice': 'invalid_choice_count',
        'Chat turn did not finish successfully': 'unfinished_turn',
        'Chat response was refused': 'response_refused',
        'Invalid assistant text': 'invalid_assistant_text',
        'Inconsistent Chat terminal result': 'inconsistent_terminal_result',
        'Invalid reasoning text': 'invalid_reasoning_text',
        'Duplicate or unoffered tool call': 'invalid_tool_call',
        'Tool arguments must be a complete object': 'invalid_tool_arguments',
        'Invalid raw tool input': 'invalid_raw_tool_input',
        'Invalid usage': 'invalid_usage',
        'Malformed Chat response': 'malformed_response',
        'Chat stream ended without DONE': 'missing_done',
        'Chat stream exceeds response boundary': 'response_boundary_exceeded',
        'Chat stream lacks terminal choice': 'missing_terminal_choice',
        'Upstream Chat stream error': 'upstream_stream_error',
        'Unexpected extra Chat choice': 'unexpected_choice',
        'Unsupported Chat tool type': 'unsupported_tool_type',
        'Invalid Chat SSE frame': 'invalid_sse_frame',
        'Invalid Chat JSON response': 'invalid_json_response',
    }

    def __init__(self, message, *, detail=None):
        super().__init__(message)
        # Call sites supply fixed labels only, never upstream text or arguments.
        self.detail = detail
        # Unknown SDK item kinds may contain external text. Never derive a log
        # field from that value, or from arbitrary exception messages.
        self.code = ('unsupported_sdk_input' if message.startswith('Unsupported SDK input item: ')
                     else self._CODES.get(message, 'protocol_error'))


def text_content(content):
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ChatProtocolError('Unsupported message content')
    parts = []
    for part in content:
        if not isinstance(part, dict) or part.get('type') not in {'input_text', 'output_text', 'text'}:
            raise ChatProtocolError('Only rewritten text content is supported')
        if not isinstance(part.get('text'), str):
            raise ChatProtocolError('Invalid text content')
        parts.append(part['text'])
    return '\n'.join(parts)


class ChatAdapter:
    def __init__(self):
        self.history = {}
        self.offered = {}
        self.tool_name_correction = False

    def note_tool_mismatch(self):
        # No rejected model text, arguments or invented tool result enter history.
        self.tool_name_correction = True

    def prepare(self, payload, *, stream=True):
        from agents.model.visual_transport import CODING_TOOLS
        tools, self.offered = [], {}
        for tool in payload.get('tools', []):
            name, kind = tool.get('name'), tool.get('type')
            if name not in CODING_TOOLS or kind not in {'function', 'custom'}:
                raise ChatProtocolError('Unsupported coding tool')
            self.offered[name] = kind
            function = {k: deepcopy(tool[k]) for k in ('name', 'description', 'parameters', 'strict') if k in tool}
            if kind == 'custom':
                function['parameters'] = {'type': 'object', 'properties': {'input': {'type': 'string'}},
                    'required': ['input'], 'additionalProperties': False}
                function['description'] = tool.get('description', '') + '\nPass the complete raw tool input unchanged in the input string.'
            tools.append({'type': 'function', 'function': function})
        if payload.get('tool_choice') == 'none':
            self.offered = {}
        source = payload.get('input', [])
        if isinstance(source, str):
            source = [{'role': 'user', 'content': source}]
        if not isinstance(source, list):
            raise ChatProtocolError('Expected complete SDK input')
        # Keep only history identified by the SDK's current input. Compaction is
        # authoritative: a cache entry never adds an absent conversation turn.
        retained = {item.get('call_id', item.get('id')) for item in source if isinstance(item, dict)}
        self.history = {key: value for key, value in self.history.items() if key in retained}
        def as_chat_call(item):
            arguments = item.get('arguments') if item['type'] == 'function_call' else json.dumps({'input': item.get('input')}, ensure_ascii=False)
            return {'id': item['call_id'], 'type': 'function',
                    'function': {'name': item['name'], 'arguments': arguments}}
        current_calls = {item['call_id']: as_chat_call(item) for item in source
                         if isinstance(item, dict) and item.get('call_id')
                         and item.get('type') in {'function_call', 'custom_tool_call'}}
        messages = []
        if payload.get('instructions'):
            messages.append({'role': 'system', 'content': _sdk_tool_instructions(
                text_content(payload['instructions']), self.offered)})
        emitted, pending = set(), set()
        for index, item in enumerate(source):
            kind = item.get('type', 'message')
            if kind == 'reasoning':
                if item.get('encrypted_content'):
                    raise ChatProtocolError('Encrypted remote compaction is not supported')
                continue  # Reasoning is restored from the original Chat turn below.
            if kind in {'function_call', 'custom_tool_call'}:
                identity = item.get('call_id')
                if not identity or identity in emitted:
                    if identity in emitted:
                        continue
                    raise ChatProtocolError('Missing call ID')
                cached = self.history.get(identity)
                if cached:
                    assistant = deepcopy(cached)
                    cached_ids = {call['id'] for call in assistant.get('tool_calls', [])}
                    # SDK compaction can remove one call/result pair from a
                    # parallel turn. Cache supplies reasoning and grouping, but
                    # surviving calls and their arguments come from current input.
                    assistant['tool_calls'] = [call for call_id, call in current_calls.items()
                                               if call_id in cached_ids]
                    ids = {call['id'] for call in assistant['tool_calls']}
                    if messages and messages[-1] == {'role': 'assistant', 'content': assistant.get('content') or ''}:
                        messages.pop()
                else:
                    calls = []
                    for candidate in source[index:]:
                        if candidate.get('type') not in {'function_call', 'custom_tool_call'}:
                            break
                        calls.append(as_chat_call(candidate))
                    assistant = {'role': 'assistant', 'content': None, 'tool_calls': calls}
                    ids = {call['id'] for call in calls}
                if pending:
                    raise ChatProtocolError('Missing previous tool results')
                messages.append(assistant)
                emitted.update(ids)
                pending.update(ids)
            elif kind in {'function_call_output', 'custom_tool_call_output'}:
                identity = item.get('call_id')
                if identity not in pending:
                    raise ChatProtocolError('Unmatched tool result')
                pending.remove(identity)
                messages.append({'role': 'tool', 'tool_call_id': identity, 'content': text_content(item.get('output'))})
            elif kind == 'message':
                if pending:
                    raise ChatProtocolError('Message interrupts tool results')
                role = item.get('role')
                if role not in {'system', 'developer', 'user', 'assistant'}:
                    raise ChatProtocolError('Unsupported message role')
                cached = self.history.get(item.get('id'))
                if cached and cached.get('tool_calls'):
                    continue  # The paired tool items restore this whole assistant.
                messages.append(deepcopy(cached) if cached else
                    {'role': 'system' if role == 'developer' else role, 'content': text_content(item.get('content'))})
            else:
                raise ChatProtocolError('Unsupported SDK input item: ' + str(kind))
        if pending:
            raise ChatProtocolError('Tool results missing from SDK input')
        if self.tool_name_correction:
            allowed = ', '.join(sorted(self.offered)) or '(none; return text only)'
            messages.append({'role': 'user', 'content':
                'Tool-call correction: the previous model response was rejected because a tool name '
                'was not offered. None of the tool calls in that rejected response executed. '
                'Use only these currently declared tool names exactly: ' + allowed + '. '
                'Use their supplied argument schemas; do not invent aliases or wrapper tools. '
                'Preserve previously completed work and do not repeat earlier successful tool effects.'})
            self.tool_name_correction = False
        body = {'model': payload['model'], 'messages': messages, 'stream': stream}
        if stream:
            body['stream_options'] = {'include_usage': True}
        if tools:
            body['tools'] = tools
        if 'tool_choice' in payload:
            choice = payload['tool_choice']
            body['tool_choice'] = ({'type': 'function', 'function': {'name': choice['name']}}
                if isinstance(choice, dict) and choice.get('type') in {'function', 'custom'} else choice)
        for key in ('temperature', 'top_p', 'parallel_tool_calls'):
            if key in payload:
                body[key] = payload[key]
        if 'max_output_tokens' in payload:
            body['max_tokens'] = payload['max_output_tokens']
        # Responses-only storage, text verbosity and encrypted reasoning options
        # have no Chat equivalent; provider-default thinking is preserved.
        return body

    def complete(self, reply, *, stats=None, offered=None):
        # Validate against this request, not a subsequent request's tool inventory.
        offered = dict(self.offered if offered is None else offered)
        if stats is not None:
            stats['offered_tool_names'] = sorted(offered)
        try:
            choices = reply['choices']
            if len(choices) != 1:
                raise ChatProtocolError('Expected one choice')
            choice = choices[0]
            message = choice['message']
            finish = choice.get('finish_reason')
            if message.get('refusal'):
                raise ChatProtocolError('Chat response was refused')
            if finish not in {'stop', 'tool_calls'}:
                raise ChatProtocolError('Chat turn did not finish successfully')
            content = message.get('content')
            calls = deepcopy(message.get('tool_calls') or [])
            if content is not None and not isinstance(content, str):
                raise ChatProtocolError('Invalid assistant text')
            if bool(calls) != (finish == 'tool_calls') or (not calls and not content):
                raise ChatProtocolError('Inconsistent Chat terminal result')
            assistant = {'role': 'assistant', 'content': content}
            if message.get('reasoning_content') is not None:
                if not isinstance(message['reasoning_content'], str):
                    raise ChatProtocolError('Invalid reasoning text')
                assistant['reasoning_content'] = message['reasoning_content']
            # Reject every mismatched name, but permit bounded correction ONLY
            # after all calls, IDs, arguments and usage pass the other checks.
            # No tool or history entry is published from a rejected response.
            seen, checked, missing, aliases, unoffered = set(), [], None, 0, False
            for index, call in enumerate(calls):
                identity, function = call.get('id'), call['function']
                name, arguments = function.get('name'), function['arguments']
                if call.get('type') != 'function':
                    raise ChatProtocolError('Duplicate or unoffered tool call', detail='invalid_tool_call_type')
                if not offered:
                    raise ChatProtocolError('Duplicate or unoffered tool call', detail='tools_disabled')
                # Codex instruction text can qualify names with functions. Only
                # this exact prefix on a tool offered NOW is an unambiguous alias.
                if isinstance(name, str) and name.startswith('functions.') and name[10:] in offered:
                    name = name[10:]
                    function['name'] = name
                    aliases += 1
                if name is None or name == '':
                    missing = missing or 'missing_tool_name'
                elif not isinstance(name, str):
                    if stats is not None:
                        stats['invalid_tool_name_type'] = type(name).__name__
                    raise ChatProtocolError('Duplicate or unoffered tool call', detail='invalid_tool_name_type')
                elif name not in offered:
                    unoffered = True
                    if stats is not None:
                        # Bounded metadata, never unknown names, arguments or reasoning.
                        mismatches = stats.setdefault('tool_name_mismatches', [])
                        if len(mismatches) < 4:
                            mismatches.append({'call_index': index, 'name_length': len(name),
                                'name_sha256': sha256(name.encode('utf-8', errors='surrogatepass')).hexdigest()})
                if identity is None or identity == '':
                    missing = missing or 'missing_tool_call_id'
                elif not isinstance(identity, str):
                    raise ChatProtocolError('Duplicate or unoffered tool call', detail='invalid_tool_call_id')
                elif identity in seen:
                    raise ChatProtocolError('Duplicate or unoffered tool call', detail='duplicate_tool_call_id')
                else:
                    seen.add(identity)
                parsed = json.loads(arguments)
                if not isinstance(parsed, dict):
                    raise ChatProtocolError('Tool arguments must be a complete object')
                if offered.get(name) == 'custom' and (set(parsed) != {'input'} or not isinstance(parsed['input'], str)):
                    raise ChatProtocolError('Invalid raw tool input')
                checked.append((identity, name, arguments, parsed))
            if stats is not None:
                stats['tool_name_aliases'] = aliases
            raw = reply['usage']
            inputs, outputs = raw['prompt_tokens'], raw['completion_tokens']
            usage = {'input_tokens': inputs, 'output_tokens': outputs, 'total_tokens': inputs + outputs,
                'input_tokens_details': {'cached_tokens': (raw.get('prompt_tokens_details') or {}).get('cached_tokens', raw.get('prompt_cache_hit_tokens', 0))},
                'output_tokens_details': {'reasoning_tokens': (raw.get('completion_tokens_details') or {}).get('reasoning_tokens', 0)}}
            if any(type(n) is not int or n < 0 for n in (inputs, outputs,
                   usage['input_tokens_details']['cached_tokens'], usage['output_tokens_details']['reasoning_tokens'])):
                raise ChatProtocolError('Invalid usage')
            if unoffered:
                raise ChatProtocolError('Duplicate or unoffered tool call', detail='unoffered_tool_name')
            if missing:
                raise ChatProtocolError('Duplicate or unoffered tool call', detail=missing)
            if calls:
                assistant['tool_calls'] = calls  # Canonical names in both SDK output and cached history.
            output, identities = [], []
            if content:
                identity = 'msg_' + secrets.token_hex(12)
                output.append({'id': identity, 'type': 'message', 'role': 'assistant', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': content, 'annotations': []}]})
                identities.append(identity)
            for identity, name, arguments, parsed in checked:
                item = {'id': 'fc_' + secrets.token_hex(12), 'call_id': identity, 'name': name, 'status': 'completed'}
                if offered[name] == 'custom':
                    item.update(type='custom_tool_call', input=parsed['input'])
                else:
                    item.update(type='function_call', arguments=arguments)
                output.append(item)
                identities.append(identity)
            response = {'id': 'resp_' + secrets.token_hex(12), 'object': 'response',
                'created_at': reply.get('created', int(time.time())), 'model': reply.get('model'),
                'status': 'completed', 'output': output, 'usage': usage}
            for identity in identities:
                self.history[identity] = assistant
            return response
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            if isinstance(exc, ChatProtocolError):
                raise
            raise ChatProtocolError('Malformed Chat response') from None


def stream_events(response):
    yield {'type': 'response.created', 'response': dict(response, status='in_progress', output=[])}
    for index, item in enumerate(response['output']):
        yield {'type': 'response.output_item.added', 'output_index': index, 'item': item}
        yield {'type': 'response.output_item.done', 'output_index': index, 'item': item}
    yield {'type': 'response.completed', 'response': response}


def read_chat_json(response, progress=lambda: None, *, stats=None,
                   max_bytes=32 * 1024 * 1024, observe=lambda chunk: None):
    """Read one bounded JSON response; never salvage or execute a partial call."""
    stats = {} if stats is None else stats
    stats.update(received_bytes=0, response_complete=False, finish_reason=None,
                 usage_received=False, tool_count=0, refusal_seen=False)
    data = bytearray()
    while True:
        progress()
        chunk = response.read1(min(65536, max_bytes + 1 - len(data)))
        if not chunk:
            break
        observe(chunk)
        data.extend(chunk)
        stats['received_bytes'] = len(data)
        if len(data) > max_bytes:
            raise ChatProtocolError('Chat stream exceeds response boundary')
    remaining = getattr(response, 'length', None)
    if isinstance(remaining, int) and remaining > 0:
        raise IncompleteRead(b'', remaining)
    try:
        reply = json.loads(data)
    except (ValueError, UnicodeError):
        raise ChatProtocolError('Invalid Chat JSON response', detail='invalid_json') from None
    if not isinstance(reply, dict):
        raise ChatProtocolError('Malformed Chat response')
    stats['response_complete'] = True
    stats['usage_received'] = isinstance(reply.get('usage'), dict)
    choices = reply.get('choices')
    if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict):
        choice = choices[0]
        # Diagnostic values must remain bounded, fixed metadata, never text.
        finish = choice.get('finish_reason')
        stats['finish_reason'] = finish if finish in ('stop', 'tool_calls', 'length', 'content_filter') else None
        message = choice.get('message')
        if isinstance(message, dict):
            stats['refusal_seen'] = bool(message.get('refusal'))
            calls = message.get('tool_calls')
            stats['tool_count'] = len(calls) if isinstance(calls, list) else 0
    return reply


def read_chat_stream(response, progress=lambda: None, *, stats=None):
    """Collect bounded SSE, including fragmented tools/reasoning and usage.

    No output item is executable until finish_reason AND [DONE] are present.
    Streaming reads retain existing idle detection without exposing partial tools.
    """
    result = {'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': ''}, 'finish_reason': None}]}
    message, calls = result['choices'][0]['message'], {}
    stats = {} if stats is None else stats
    stats.update(received_bytes=0, done_received=False, finish_reason=None,
                 usage_received=False, tool_count=0, refusal_seen=False)
    total, finished, data = 0, False, []
    while True:
        line = response.readline(1024 * 1024 + 1)
        if not line:
            raise ChatProtocolError('Chat stream ended without DONE')
        total += len(line)
        stats['received_bytes'] = total
        if len(line) > 1024 * 1024 or total > 32 * 1024 * 1024:
            raise ChatProtocolError('Chat stream exceeds response boundary')
        progress()
        line = line.rstrip(b'\r\n')
        if line:
            if line.startswith(b'data:'):
                data.append(line[5:].lstrip())
            continue
        if not data:
            continue
        value, data = b'\n'.join(data), []
        if value == b'[DONE]':
            stats['done_received'] = True
            if not finished:
                raise ChatProtocolError('Chat stream lacks terminal choice')
            message['tool_calls'] = [calls[k] for k in sorted(calls)]
            return result
        detail = 'invalid_json'
        try:
            chunk = json.loads(value)
            if not isinstance(chunk, dict):
                raise ChatProtocolError('Invalid Chat SSE frame', detail='invalid_chunk')
            if chunk.get('error'):
                raise ChatProtocolError('Upstream Chat stream error')
            for key in ('id', 'model', 'created', 'usage'):
                if chunk.get(key) is not None:
                    result[key] = chunk[key]
            if chunk.get('usage') is not None:
                stats['usage_received'] = True
            detail = 'invalid_choices'
            choices = chunk.get('choices')
            if choices is None:
                choices = []  # Some compatible gateways use null in usage-only frames.
            if not isinstance(choices, list):
                raise ChatProtocolError('Invalid Chat SSE frame', detail=detail)
            for choice in choices:
                if not isinstance(choice, dict):
                    raise ChatProtocolError('Invalid Chat SSE frame', detail=detail)
                if choice.get('index', 0) != 0 or finished:
                    raise ChatProtocolError('Unexpected extra Chat choice')
                if choice.get('finish_reason') is not None:
                    finish = choice['finish_reason']
                    result['choices'][0]['finish_reason'] = finish
                    stats['finish_reason'] = (finish if isinstance(finish, str) and finish in {
                        'stop', 'length', 'tool_calls', 'content_filter', 'function_call'} else 'other')
                    if finish == 'content_filter':
                        raise ChatProtocolError('Chat response was refused')
                    finished = True
                detail = 'invalid_delta'
                delta = choice.get('delta')
                if delta is None:
                    delta = {}  # Empty delta is not malformed content.
                if not isinstance(delta, dict):
                    raise ChatProtocolError('Invalid Chat SSE frame', detail=detail)
                if delta.get('refusal'):
                    stats['refusal_seen'] = True
                    raise ChatProtocolError('Chat response was refused')
                detail = 'invalid_text_fragment'
                for key in ('content', 'reasoning_content', 'refusal'):
                    if delta.get(key) is not None:
                        message[key] = message.get(key, '') + delta[key]
                detail = 'invalid_tool_fragment'
                fragments = delta.get('tool_calls')
                if fragments is None:
                    fragments = []
                if not isinstance(fragments, list):
                    raise ChatProtocolError('Invalid Chat SSE frame', detail=detail)
                for fragment in fragments:
                    detail = 'invalid_tool_fragment'
                    if not isinstance(fragment, dict) or type(fragment.get('index')) is not int or fragment['index'] < 0:
                        raise ChatProtocolError('Invalid Chat SSE frame', detail=detail)
                    call = calls.setdefault(fragment['index'], {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                    stats['tool_count'] = len(calls)
                    if fragment.get('type', 'function') != 'function':
                        raise ChatProtocolError('Unsupported Chat tool type')
                    identity, function = fragment.get('id'), fragment.get('function')
                    if identity is not None:
                        if not isinstance(identity, str):
                            raise ChatProtocolError('Invalid Chat SSE frame', detail='invalid_tool_id_fragment')
                        call['id'] += identity
                    if function is None:
                        function = {}
                    if not isinstance(function, dict):
                        raise ChatProtocolError('Invalid Chat SSE frame', detail=detail)
                    for key in ('name', 'arguments'):
                        detail = 'invalid_tool_' + key + '_fragment'
                        part = function.get(key)
                        if part is not None:
                            if not isinstance(part, str):
                                raise ChatProtocolError('Invalid Chat SSE frame', detail=detail)
                            call['function'][key] += part
        except ChatProtocolError:
            raise
        except (KeyError, ValueError, TypeError, AttributeError):
            raise ChatProtocolError('Invalid Chat SSE frame', detail=detail) from None

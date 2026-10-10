"""Shared credential-boundary policy; packaged copies are checked byte-for-byte."""
from contextlib import contextmanager, asynccontextmanager, ExitStack, AsyncExitStack
import ipaddress
import os
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler


def secure_url(value: str, *, allow_query: bool = False) -> str:
    """HTTPS except explicit localhost / literal loopback; never resolve hostnames."""
    try:
        parsed = urlsplit(value)
        _ = parsed.port  # Reject malformed / out-of-range ports before any I/O.
        host = parsed.hostname
        if (not host or parsed.scheme not in ('https', 'http') or
                parsed.username is not None or parsed.password is not None or
                parsed.fragment or (parsed.query and not allow_query) or
                any(c.isspace() or ord(c) < 32 for c in value) or '\\' in value):
            raise ValueError
        if parsed.scheme == 'http' and host != 'localhost':
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError
    except (ValueError, TypeError):
        raise ValueError('Expected HTTPS URL (HTTP only on explicit loopback), without credentials/query/fragment') from None
    return value.rstrip('/')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


# No wildcard inheritance, proxy credentials, language startup hooks or host auth.
RUNTIME_ENV = frozenset({'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'TMPDIR',
                         'SYSTEMROOT', 'WINDIR', 'PATHEXT'})
VALIDATION_ENV = RUNTIME_ENV | {'CI', 'NO_COLOR', 'npm_config_audit', 'npm_config_fund',
    'ARC_DB_FILE', 'DATABASE_FILE', 'PORT', 'ARC_WEB_PORT', 'ARCBENCH_WEB_PORT',
    'SPEC_BASE_PORTS', 'PLAYWRIGHT_BASE_URL', 'ARC_WEB_BASE_URL',
    'PLAYWRIGHT_JSON_OUTPUT_NAME'}


def child_env(source=None, *, validation=False):
    source = os.environ if source is None else source
    allowed = VALIDATION_ENV if validation else RUNTIME_ENV
    result = {key: value for key, value in source.items() if key in allowed}
    result.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
    if validation:
        # Controller-owned memory limit; never inherit arbitrary NODE_OPTIONS code.
        result['NODE_OPTIONS'] = '--max-old-space-size=256'
    return result


@contextmanager
def spawn_environment(env):
    """SDKs merge env overrides; replace the parent only during their spawn call.

    Controllers start one SDK at a time. Request handlers keep upstream secrets
    in their own objects, and no other child spawn may overlap this section.
    """
    previous = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


@contextmanager
def isolated_sdk(factory, env):
    with ExitStack() as stack:
        with spawn_environment(env):
            connected = stack.enter_context(factory())
        yield connected


@asynccontextmanager
async def isolated_async_sdk(factory, env):
    async with AsyncExitStack() as stack:
        with spawn_environment(env):
            connected = await stack.enter_async_context(factory())
        yield connected

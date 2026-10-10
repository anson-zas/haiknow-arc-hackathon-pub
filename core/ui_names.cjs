// Extract literal JSX names and Playwright locators. Never execute application code.
// Babel parser/traverse are already locked by the React/Vite template's plugin-react.
const fs = require('node:fs');
const path = require('node:path');
const {createRequire} = require('node:module');
const [project, output] = process.argv.slice(2);
const appRequire = createRequire(path.join(project, 'frontend/package.json'));
const parser = appRequire('@babel/parser');
const traverse = appRequire('@babel/traverse').default;
const kit = JSON.parse(fs.readFileSync(path.join(__dirname, '../template/frontend/src/ui/contracts.json'), 'utf8'));
const competitionKit = process.env.HAIKNOW_PROFILE === 'none' ? {} :
  JSON.parse(fs.readFileSync(path.join(__dirname, '../competition/scaffold-contracts.json'), 'utf8'));
const result = {controls: [], unresolved: [], interaction_unverified: [], container_locators: [], files: 0};
const roles = new Set(['button', 'link', 'menuitem', 'tab', 'textbox', 'searchbox', 'combobox', 'checkbox', 'radio', 'switch', 'form', 'dialog', 'navigation', 'complementary', 'group', 'alert', 'article', 'field-label']);
const excluded = new Set(['node_modules', 'dist', 'build', '.git', '.arc', '.haiknow', 'coverage']);

function literal(node) {
  if (!node) return null;
  if (node.type === 'StringLiteral' || node.type === 'BooleanLiteral') return node.value;
  if (node.type === 'JSXExpressionContainer') return literal(node.expression);
  if (node.type === 'TemplateLiteral' && node.expressions.length === 0) return node.quasis[0].value.cooked;
  return null;
}
function property(node, key) {
  return node?.properties?.find(p => (p.key?.name || p.key?.value) === key)?.value;
}
function attr(node, key) { return node.openingElement.attributes.find(a => a.name?.name === key); }
function text(node) {
  if (node.type === 'JSXText') return node.value.replace(/\s+/g, ' ');
  if (node.type === 'JSXExpressionContainer') return literal(node);
  if (node.type !== 'JSXElement') return null;
  if (['true', true].includes(literal(attr(node, 'aria-hidden')?.value))) return '';
  if (!/^[a-z]/.test(node.openingElement.name.name || '')) return null;
  const parts = node.children.map(text);
  return parts.includes(null) ? null : parts.join('');
}
function regexName(node) {
  if (node?.type !== 'RegExpLiteral') return null;
  if (/[^iu]/.test(node.flags)) return null;
  const exact = node.pattern.startsWith('^') && node.pattern.endsWith('$');
  const body = exact ? node.pattern.slice(1, -1) : node.pattern;
  if (/(^|[^\\])[.*+?()[\]{}|^$]|\\[a-zA-Z0-9]/.test(body)) return null;
  return {name: body.replace(/\\([^a-zA-Z0-9])/g, '$1'), exact};
}
function scan(file) {
  const relative = path.relative(project, file);
  const isTest = /(^|[/\\])(tests?|test-e2e|e2e|__tests__)([/\\]|$)|\.(spec|test)\./.test(relative);
  const source = fs.readFileSync(file, 'utf8');
  const ast = parser.parse(source, {sourceType: 'unambiguous', plugins: ['jsx', 'typescript']});
  result.files++;
  const links = new Set(), wrappers = new Map(), labels = new Map();
  const rtlScreens = new Set(), rtlWithin = new Set();
  for (const n of ast.program.body) {
    if (n.type === 'ImportDeclaration' && n.source.value.startsWith('.') &&
        /\/ui(?:\/index(?:\.tsx)?)?$/.test(n.source.value)) {
      for (const s of n.specifiers) if (kit[s.imported?.name]) wrappers.set(s.local.name, kit[s.imported.name]);
    }
    if (n.type === 'ImportDeclaration' && n.source.value.startsWith('.') &&
        /\/competition(?:\/index(?:\.tsx)?)?$/.test(n.source.value)) {
      for (const s of n.specifiers) if (competitionKit[s.imported?.name]) wrappers.set(s.local.name, competitionKit[s.imported.name]);
    }
    if (n.type === 'ImportDeclaration' && ['react-router-dom', 'react-router'].includes(n.source.value)) {
      for (const s of n.specifiers) if (['Link', 'NavLink'].includes(s.imported?.name)) links.add(s.local.name);
    }
    if (n.type === 'ImportDeclaration' && ['@testing-library/react', '@testing-library/dom'].includes(n.source.value)) {
      for (const s of n.specifiers) {
        if (s.imported?.name === 'screen') rtlScreens.add(s.local.name);
        if (s.imported?.name === 'within') rtlWithin.add(s.local.name);
      }
    }
  }
  function emit(node, role, name, origin, extra={}) {
    const record = {file: relative, line: node.loc.start.line, role, source: origin, ...extra};
    if (typeof name === 'string' && name.length) result.controls.push({...record, name});
    else result.unresolved.push({...record, reason: 'dynamic_or_unsupported_name'});
  }
  function collectLabels(node) {
    if (!node || typeof node !== 'object') return;
    if (node.type === 'JSXElement' && node.openingElement.name.name === 'label') {
      const id = literal(attr(node, 'htmlFor')?.value);
      if (id) labels.set(id, text(node)?.trim());
    }
    for (const [key, value] of Object.entries(node)) {
      if (['loc','tokens','comments'].includes(key)) continue;
      if (Array.isArray(value)) value.forEach(collectLabels); else if (value && typeof value === 'object') collectLabels(value);
    }
  }
  collectLabels(ast.program);
  // Bounded lexical evidence only: immutable strings, direct awaited inputs and
  // earlier mock statements in the same test/block. Never evaluate test code.
  const paths = new WeakMap(), fixtureEvents = [];
  if (isTest) traverse(ast, {enter(p) { paths.set(p.node, p); }});
  function constant(node, seen = new Set()) {
    const direct = literal(node);
    if (typeof direct === 'string') return direct;
    if (!node || seen.has(node)) return null;
    seen = new Set(seen).add(node);
    if (node.type === 'Identifier') {
      const binding = paths.get(node)?.scope.getBinding(node.name);
      if (binding?.constant && binding.kind === 'const' && binding.path.node.start < node.start)
        return constant(binding.path.node.init, seen);
    }
    if (node.type === 'BinaryExpression' && node.operator === '+') {
      const left = constant(node.left, seen), right = constant(node.right, seen);
      if (left !== null && right !== null) return left + right;
    }
    if (node.type === 'CallExpression' && node.callee.type === 'MemberExpression' && !node.callee.computed &&
        node.callee.property.name === 'join' && node.callee.object.type === 'ArrayExpression' && node.arguments.length === 1) {
      const parts = node.callee.object.elements.map(n => constant(n, seen)), separator = constant(node.arguments[0], seen);
      if (separator !== null && !parts.includes(null)) return parts.join(separator);
    }
    return null;
  }
  function imported(node, sources, name) {
    if (node?.type !== 'Identifier') return false;
    const binding = paths.get(node)?.scope.getBinding(node.name), spec = binding?.path;
    return Boolean(binding?.constant && spec?.parentPath.isImportDeclaration() &&
      sources.includes(spec.parent.source.value) &&
      (name === 'default' ? spec.isImportDefaultSpecifier() : spec.node.imported?.name === name));
  }
  function testOwner(p) {
    return p.findParent(q => q.isFunction() && q.parentPath.isCallExpression() &&
      ['it','test'].includes(q.parent.callee.name));
  }
  function isRTL(node) {
    return imported(node, ['@testing-library/react','@testing-library/dom'], 'screen') ||
      node?.type === 'CallExpression' && imported(node.callee, ['@testing-library/react','@testing-library/dom'], 'within');
  }
  function isPage(node, owner) {
    if (node?.type !== 'Identifier') return false;
    const binding = paths.get(node)?.scope.getBinding(node.name);
    return Boolean(binding?.constant && binding.kind === 'param' && binding.scope.path === owner &&
      owner.node.params.some(p => p.type === 'ObjectPattern' && p.properties.some(v =>
        v.key?.name === 'page' && v.value?.name === node.name)));
  }
  // Scope evidence is narrower than the existing name scan: only a confirmed
  // Playwright callback owns it. Never inherit a case through arbitrary helpers.
  function playwrightTest(node) {
    if (imported(node, ['@playwright/test'], 'test')) return true;
    if (node?.type !== 'Identifier') return false;
    const binding = paths.get(node)?.scope.getBinding(node.name), declaration = binding?.path.node;
    const init = declaration?.init;
    return Boolean(binding?.constant && binding.kind === 'const' && declaration?.id?.type === 'ObjectPattern' &&
      declaration.id.properties.some(p => p.type === 'ObjectProperty' && !p.computed &&
        (p.key.name || p.key.value) === 'test' && p.value.name === node.name) &&
      init?.type === 'CallExpression' && init.callee.type === 'Identifier' && init.callee.name === 'require' &&
      !paths.get(init.callee)?.scope.getBinding('require') && init.arguments.length === 1 &&
      literal(init.arguments[0]) === '@playwright/test');
  }
  function playwrightOwner(p) {
    const owner = p?.getFunctionParent(), call = owner?.parentPath;
    return call?.isCallExpression() && call.node.arguments.at(-1) === owner.node &&
      playwrightTest(call.node.callee) ? owner : null;
  }
  function plainObject(node) {
    if (node?.type !== 'ObjectExpression') return false;
    const keys = node.properties.map(p => p.type === 'ObjectProperty' && !p.computed
      ? p.key.name || p.key.value : null);
    return !keys.includes(null) && new Set(keys).size === keys.length;
  }
  function sourceCase(owner) {
    const options = owner.parent.arguments[1];
    if (!plainObject(options)) return null;
    const value = property(options, 'annotation');
    const annotations = value?.type === 'ArrayExpression' ? value.elements : [value];
    if (!annotations.every(plainObject)) return null;
    const labels = annotations.filter(a => literal(property(a, 'type')) === 'source-case');
    const id = labels.length === 1 && literal(property(labels[0], 'description'));
    return typeof id === 'string' && id ? id : null;
  }
  function immutableInit(node) {
    if (node?.type !== 'Identifier') return null;
    const binding = paths.get(node)?.scope.getBinding(node.name);
    return binding?.constant && binding.kind === 'const' && binding.path.node.start < node.start
      ? binding.path.node.init : null;
  }
  function locatorScope(node, owner, seen = new Set()) {
    if (!node || seen.has(node)) return 'unknown';
    seen = new Set(seen).add(node);
    if (isPage(node, owner)) return 'page';
    if (node.type === 'Identifier') return locatorScope(immutableInit(node), owner, seen);
    if (node.type !== 'CallExpression' || node.callee.type !== 'MemberExpression' || node.callee.computed)
      return 'unknown';
    const base = locatorScope(node.callee.object, owner, seen);
    if (base === 'unknown') return base;
    if (node.callee.property.name === 'getByRole')
      return literal(node.arguments[0]) === 'main' || base === 'main' ? 'main' : 'unknown';
    if (node.callee.property.name === 'locator' &&
        ['main', '[role="main"]', "[role='main']"].includes(constant(node.arguments[0]))) return 'main';
    return 'unknown';
  }
  function locatorOrigin(node, seen = new Set()) {
    if (!node || seen.has(node)) return null;
    seen = new Set(seen).add(node);
    if (node.type === 'Identifier') return locatorOrigin(immutableInit(node), seen);
    return node.type === 'CallExpression' && node.callee.type === 'MemberExpression' &&
      !node.callee.computed && node.callee.property.name === 'getByRole' ? node : null;
  }
  const locatorActions = new WeakMap();
  function recordAction(node, action) {
    if (node && locatorActions.get(node) !== 'click') locatorActions.set(node, action);
  }
  function containsEarlyExit(node) {
    if (!node || typeof node !== 'object' || paths.get(node)?.isFunction()) return false;
    if (['ReturnStatement', 'ThrowStatement'].includes(node.type)) return true;
    return Object.entries(node).some(([key, value]) =>
      !['loc', 'tokens', 'comments'].includes(key) &&
      (Array.isArray(value) ? value.some(containsEarlyExit) : containsEarlyExit(value)));
  }
  if (isTest) traverse(ast, {CallExpression(p) {
    const owner = playwrightOwner(p);
    if (!owner) return;
    const node = p.node, member = node.callee.type === 'MemberExpression' && !node.callee.computed;
    const method = member && node.callee.property.name;
    if (method === 'getByRole' || method === 'locator') return;
    const target = member && locatorOrigin(node.callee.object);
    const awaited = p.parentPath.isAwaitExpression(), statement = p.parentPath.parentPath;
    // A prior branch containing an exit is uncertain; do not solve reachability
    // or follow calls into nested functions to certify this later click.
    const direct = awaited && statement.isExpressionStatement() && statement.parentPath === owner.get('body') &&
      !owner.node.body.body.slice(0, statement.key).some(containsEarlyExit);
    recordAction(target, method === 'click' && direct ? 'click' : 'unknown');
    // A helper or assertion consumes the locator, but does not prove a click.
    for (const argument of node.arguments) recordAction(locatorOrigin(argument), 'unknown');
  }});
  function scopeMetadata(node) {
    const owner = playwrightOwner(paths.get(node));
    if (!owner || node.callee.computed || node.callee.property.name !== 'getByRole') return {};
    const action = locatorActions.get(node);
    return {source_case_id: sourceCase(owner), locator_scope: locatorScope(node.callee.object, owner),
      ...(action ? {locator_action: action} : {})};
  }
  function isUser(node) {
    if (imported(node, ['@testing-library/user-event'], 'default')) return true;
    if (node?.type !== 'Identifier') return false;
    const binding = paths.get(node)?.scope.getBinding(node.name), init = binding?.path.node.init;
    return Boolean(binding?.constant && binding.kind === 'const' && binding.path.node.start < node.start &&
      init?.type === 'CallExpression' && init.callee.type === 'MemberExpression' && !init.callee.computed &&
      init.callee.property.name === 'setup' && imported(init.callee.object, ['@testing-library/user-event'], 'default'));
  }
  function inputLocator(node, owner, rtl) {
    if (node?.type !== 'CallExpression' || node.callee.type !== 'MemberExpression' || node.callee.computed) return false;
    if (!(rtl ? isRTL(node.callee.object) : isPage(node.callee.object, owner))) return false;
    const method = node.callee.property.name;
    return method === (rtl ? 'getByLabelText' : 'getByLabel') ||
      method === 'getByRole' && ['textbox','searchbox','combobox'].includes(literal(node.arguments[0]));
  }
  if (isTest) traverse(ast, {CallExpression(p) {
    const node = p.node, method = node.callee.type === 'MemberExpression' && !node.callee.computed && node.callee.property.name;
    const owner = testOwner(p);
    if (!owner || p.getFunctionParent() !== owner) return;
    const awaited = p.parentPath.isAwaitExpression(), stmt = awaited ? p.parentPath.parentPath : p.parentPath;
    if (!stmt.isExpressionStatement() || !stmt.parentPath.isBlockStatement()) return;
    const add = (value, kind, line = node.loc.start.line) => {
      if (typeof value === 'string' && value) fixtureEvents.push({value, owner, stmt, evidence: {kind, method, line}});
    };
    if (awaited && ['fill','type'].includes(method)) {
      const user = method === 'type' && isUser(node.callee.object);
      const target = user ? node.arguments[0] : node.callee.object;
      const value = constant(node.arguments[user ? 1 : 0]);
      if (inputLocator(target, owner, user) && !(method === 'type' && /[\[{]/.test(value || '')))
        add(value, 'test-input');
    } else if (/^mock(?:Resolved|Return)Value(?:Once)?$/.test(method || '')) {
      function collect(value) {
        if (value?.type === 'StringLiteral') add(value.value, 'test-mock', value.loc.start.line);
        else if (value?.type === 'ArrayExpression') value.elements.forEach(collect);
        else if (value?.type === 'ObjectExpression') value.properties.forEach(v => collect(v.value));
      }
      collect(node.arguments[0]);
    }
  }});
  function recordScope(node, name, owner) {
    if (node?.type !== 'CallExpression') return false;
    if (imported(node.callee, ['@testing-library/react','@testing-library/dom'], 'within'))
      return recordScope(node.arguments[0], name, owner);
    return node.callee.type === 'MemberExpression' && node.callee.property.name === 'getByRole' &&
      ['article','heading'].includes(literal(node.arguments[0])) &&
      constant(property(node.arguments[1], 'name')) === name &&
      (isRTL(node.callee.object) || owner && isPage(node.callee.object, owner));
  }
  function fixtureSources(node, name) {
    const p = paths.get(node), owner = p && testOwner(p);
    const receiver = node.callee.object;
    if (!owner || !(isRTL(receiver) || isPage(receiver, owner) || recordScope(receiver, name, owner))) return [];
    // Only an awaited Testing Library waitFor callback may inherit an earlier
    // input. A deferred helper's textual position says nothing about call order.
    for (let fn = p.getFunctionParent(); fn && fn !== owner; fn = fn.getFunctionParent()) {
      if (!fn.parentPath.isCallExpression() || !fn.parentPath.parentPath.isAwaitExpression() ||
          !imported(fn.parent.callee, ['@testing-library/react','@testing-library/dom'], 'waitFor')) return [];
    }
    return fixtureEvents.filter(e => {
      if (e.owner !== owner || e.value !== name) return false;
      // A conditional input cannot escape its block, nor precede earlier locators.
      let child = p;
      while (child && child.parentPath !== e.stmt.parentPath) child = child.parentPath;
      return child && child.node.start >= e.stmt.node.end;
    }).map(e => e.evidence);
  }
  function navigationScope(node, seen = new Set()) {
    if (!node || seen.has(node)) return false;
    seen = new Set(seen).add(node);
    if (node.type === 'Identifier') {
      const binding = paths.get(node)?.scope.getBinding(node.name);
      return binding?.constant && binding.kind === 'const' && navigationScope(binding.path.node.init, seen);
    }
    if (node.type !== 'CallExpression') return false;
    if (node.callee.type === 'MemberExpression') return (
      /ByRole$/.test(node.callee.property.name) && ['navigation','complementary','menu','menubar','tablist'].includes(literal(node.arguments[0]))
    ) || navigationScope(node.callee.object, seen);
    return imported(node.callee, ['@testing-library/react','@testing-library/dom'], 'within') && navigationScope(node.arguments[0], seen);
  }
  function walk(node, parentLabel = null) {
    if (!node || typeof node !== 'object') return;
    if (!isTest && node.type === 'JSXElement') {
      const tag = node.openingElement.name.name;
      const wrapper = wrappers.get(tag);
      // A click handler can open content, delegate, or only record analytics.
      // Report shapes for review; AST syntax alone cannot establish a UI defect.
      let knownChildControl = false, unknownChildControl = false;
      function delegated(value) {
        if (!value || typeof value !== 'object') return;
        if (value !== node && value.type === 'JSXElement') {
          const childTag = value.openingElement.name.name || '';
          if (!/^[a-z]/.test(childTag)) unknownChildControl = true;
          if (['button','a','input','select','textarea','label','dialog','summary'].includes(childTag) ||
              attr(value,'role') || attr(value,'contentEditable')) knownChildControl = true;
        }
        if (value.type === 'Identifier' && value.name === 'children') unknownChildControl = true;
        for (const [key, child] of Object.entries(value)) {
          if (['loc','comments','tokens'].includes(key)) continue;
          if (Array.isArray(child)) child.forEach(delegated);
          else if (child && typeof child === 'object') delegated(child);
        }
      }
      if (['div','span','p','h1','h2','h3','h4','h5','h6'].includes(tag) && attr(node,'onClick') &&
          node.children.some(c=>c.type!=='JSXText' || c.value.trim()) &&
          !node.openingElement.attributes.some(a=>a.type==='JSXSpreadAttribute') &&
          !attr(node,'onKeyDown') && !attr(node,'onKeyUp') && !attr(node,'contentEditable')) {
        node.children.forEach(delegated);
        if (!knownChildControl) result.interaction_unverified.push({
          file:relative,line:node.loc.start.line,tag,name:text(node)?.trim() || `<${tag}>`,source:'jsx-interaction',
          reason:unknownChildControl ? 'clickable_content_has_unresolved_child_controls' : 'clickable_content_needs_keyboard_review'});
      }
      const inputType = literal(attr(node, 'type')?.value) || 'text';
      const inputRole = ['checkbox','radio'].includes(inputType) ? inputType : inputType === 'search' ? 'searchbox' :
        ['submit','button','reset'].includes(inputType) ? 'button' : inputType === 'hidden' ? null : 'field-label';
      const role = literal(attr(node, 'role')?.value) ||
        wrapper?.role || (tag === 'input' ? (inputType === 'text' ? 'textbox' : inputRole) :
          tag === 'textarea' ? 'textbox' : tag === 'select' ? 'combobox' :
          tag === 'button' ? 'button' : tag === 'a' || links.has(tag) ? 'link' :
          tag === 'nav' ? 'navigation' : tag === 'aside' ? 'complementary' :
          ['form', 'dialog'].includes(tag) ? tag : null);
      if (roles.has(role)) {
        const label = attr(node, 'aria-label');
        const contentName = ['button', 'link', 'menuitem', 'tab'].includes(role);
        const childParts = node.children.map(text);
        const name = attr(node, 'aria-labelledby') ? null : label ? literal(label.value) :
          wrapper ? (wrapper.name === 'children' ? childParts.includes(null) ? null : childParts.join('').trim() : literal(attr(node, wrapper.name)?.value)) :
          ['input','textarea','select'].includes(tag) ? labels.get(literal(attr(node,'id')?.value)) || parentLabel :
          contentName ? text(node)?.trim() : null;
        emit(node, role, name, 'jsx-static', {tag,
          req_id: literal(attr(node, 'data-req-id')?.value),
          on_click: Boolean(attr(node, 'onClick')),
          has_destination: Boolean(attr(node, 'href') || attr(node, 'to'))});
      }
    }
    if (isTest && node.type === 'CallExpression' && node.callee.type === 'MemberExpression') {
      const method = node.callee.property.name;
      const role = literal(node.arguments[0]);
      const receiver = node.callee.object;
      const rtl = receiver.type === 'Identifier' && rtlScreens.has(receiver.name) ||
        receiver.type === 'CallExpression' && rtlWithin.has(receiver.callee.name);
      const byRole = method === 'getByRole' || rtl && /^(?:find|query|get)(?:All)?ByRole$/.test(method);
      const byLabel = method === 'getByLabel' || rtl && /^(?:find|query|get)(?:All)?ByLabelText$/.test(method);
      // Regions were intentionally outside the strict control-name gate. Keep
      // that boundary while exposing literal test roles to source-backed checks.
      if (byRole && role === 'region') {
        const nameNode = property(node.arguments[1], 'name');
        const name = regexName(nameNode)?.name ?? constant(nameNode);
        if (typeof name === 'string' && name.length) result.container_locators.push({
          file: relative, line: node.loc.start.line, role, name, source: 'test-locator'});
      }
      if (byRole && roles.has(role) || byLabel) {
        const options = node.arguments[1];
        const nameNode = byRole ? property(options, 'name') : node.arguments[0];
        const regex = regexName(nameNode);
        // Testing Library ByRole uses exact string name matching (no exact option).
        // Its ByLabelText defaults exact=true; Playwright names default substring.
        const exact = rtl ? byRole || literal(property(options, 'exact')) !== false :
          literal(property(options, 'exact')) === true;
        const name = regex?.name ?? constant(nameNode);
        const evidence = fixtureSources(node, name);
        emit(node, !byRole ? 'field-label' : role,
          name, 'test-locator',
          {exact: regex ? regex.exact : exact, framework: rtl ? 'testing-library' : 'playwright-or-unknown',
            ...scopeMetadata(node),
            ...(!regex && literal(nameNode) === null && name !== null ? {name_from_constant: true} : {}),
            ...(navigationScope(receiver) ? {navigation_scope: true} : {}),
            ...(recordScope(receiver, name, testOwner(paths.get(node))) ? {record_scope: true} : {}),
            ...(evidence.length ? {fixture_sources: evidence} : {})});
      }
    }
    for (const [key, value] of Object.entries(node)) {
      if (key === 'loc' || key === 'tokens' || key === 'comments') continue;
      const inheritedLabel = node.type === 'JSXElement' && node.openingElement.name.name === 'label' ?
        node.children.filter(c => c.type !== 'JSXElement').map(text).join('').trim() : parentLabel;
      if (Array.isArray(value)) value.forEach(v => walk(v, inheritedLabel));
      else if (value && typeof value === 'object') walk(value, inheritedLabel);
    }
  }
  walk(ast.program);
}
function visit(dir) {
  if (!fs.existsSync(dir)) return;
  for (const e of fs.readdirSync(dir, {withFileTypes: true}).sort((a,b) => a.name.localeCompare(b.name))) {
    if (excluded.has(e.name) || e.name.startsWith('.env')) continue;
    const file = path.join(dir, e.name);
    if (e.isSymbolicLink()) throw new Error('Symlink in UI name source: ' + file);
    if (e.isDirectory()) visit(file);
    else if (/\.(js|jsx|ts|tsx|mjs|cjs)$/.test(e.name) && !e.name.endsWith('.d.ts')) scan(file);
  }
}
visit(path.join(project, 'frontend'));
visit(path.join(project, 'backend'));
fs.writeFileSync(output, JSON.stringify(result, null, 2) + '\n');

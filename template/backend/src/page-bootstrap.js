// Data loaders share the application's real read services and request identity.
// Never turn a failed query into an empty successful snapshot.
async function readPageData(request, location, load) {
  if (typeof location !== 'string' || !location.startsWith('/') || location.startsWith('//') || /[\\\r\n#]/.test(location)) {
    throw new Error('Page location must be an absolute local path with optional query');
  }
  const url = new URL(location, 'http://local.invalid');
  const result = await load({request, pathname:url.pathname, search:url.search});
  if (!result || typeof result.scope !== 'string' || !result.scope || !result.resources ||
      typeof result.resources !== 'object' || Array.isArray(result.resources)) {
    throw new Error('Page loader must return scope and a resources object');
  }
  // Enforce JSON data at the boundary; this also rejects cycles/BigInt.
  return JSON.parse(JSON.stringify({version:1, location, scope:result.scope, resources:result.resources}));
}

function renderPageHtml(html, data) {
  const json=JSON.stringify(data).replace(/[<>&\u2028\u2029]/g, character =>
    `\\u${character.charCodeAt(0).toString(16).padStart(4,'0')}`);
  const script=`<script id="page-data" type="application/json">${json}</script>`;
  // Before any app module or inline code reads initial data.
  return /<head\b[^>]*>/i.test(html) ? html.replace(/<head\b[^>]*>/i, head=>head+script) : script+html;
}

module.exports={readPageData,renderPageHtml};

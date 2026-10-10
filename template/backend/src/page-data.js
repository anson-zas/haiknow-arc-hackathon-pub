// Implement using the same authorized read services as the API. Select only the
// current pathname/search and shared navigation data; never static fixture copies.
// Scope must change with the effective user/permissions (not a session secret).
async function loadPageData({request, pathname, search}) {
  return {scope:'public', resources:{}};
}

module.exports={loadPageData};

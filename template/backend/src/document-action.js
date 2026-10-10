// Native form POST -> awaited application commit -> 303 GET. The caller owns
// business validation, authorization, transaction and cookie/session persistence.
// Errors go to its normal middleware, never to a success redirect or raw body echo.
function documentAction(commit, destination) {
  return async (req, res, next) => {
    try {
      const result = await commit(req, res);
      if (res.headersSent) return;
      const target = typeof destination === 'function' ? destination(result, req) : destination;
      if (typeof target !== 'string' || !target.startsWith('/') || target.startsWith('//') ||
          /[\\\x00-\x20\x7f]/.test(target)) {
        throw new Error('Document action destination must be a local absolute path');
      }
      res.set('Cache-Control', 'no-store');
      res.redirect(303, target);
    } catch (error) { next(error); }
  };
}
module.exports = {documentAction};

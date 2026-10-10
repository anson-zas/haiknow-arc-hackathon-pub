# Document form actions

`documentAction(commit, destination)` is Express middleware for native HTML forms.
The existing app parses URL-encoded fields as well as JSON. Use the form's actual
`action`, `method="post"`, input `name`s and a submit button; do not intercept its
submit with an unawaited fetch. Names, destinations and validation are application
contracts derived from the current input, not defaults supplied by this helper.

`commit(req, res)` performs the same authorized transaction as the JSON API. Await
all writes, including session-store persistence. Set/clear cookies on `res` before
resolving. It returns an application result. `destination` is either a local absolute
path or `(result, req) => localPath`; encode dynamic path/query values normally.
The adapter waits for commit, sets no-store and redirects with HTTP 303 so the
browser's next document is a GET with the new cookie. It does not authenticate,
authorize or persist anything on the caller's behalf.

Rejections go to normal Express error middleware, without a success redirect or
body reflection. That middleware must render the required error and usable retry
controls, preserve only safe form fields and never echo credentials. Do not expose
stack traces as product messages. Keep JSON APIs separate (or dispatch explicitly
by content type); API callers must still receive their documented JSON response.

Native submission is for actions that deliberately leave a document. Inline saves,
dialogs and unsaved editors use the existing async hooks and remain in place.
For same-page JSON authentication, the existing `useSession` still owns identity
and cache reset. A document redirect does not substitute for server authorization.

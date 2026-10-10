# Async state and session bootstrap

## First-document data and shared route snapshots

Implement `backend/src/page-data.js:loadPageData({request, pathname, search})` using
the same authorized read services as the API. Return `{scope, resources}`: a
nonempty, nonsecret effective-user/permission scope and a JSON object whose keys
are your resource identifiers. Load the current route and necessary navigation
only. The default returns no business data. Register session middleware before
`/api/page-data` and the HTML fallback; loaders must be read-only and respect that
request's permissions. The helper is not an authorization system.

The backend awaits database readiness and embeds escaped application/json before
app scripts. API/HTML reads are no-store and failures stay failures. Vite uses the
same endpoint/serializer, forwarding each incoming request's cookie/authorization
headers independently. Run the backend alongside Vite; `ARC_WEB_PORT` selects its
port (default 3000). Build does not call the backend; production HTML is populated
per request. Standalone `vite preview` has no backend injection and is not the
application deployment entry.

Import `routeData`, `usePageResource`, `useRouteData` from `../ui`. The shared store reads initial
document data synchronously and uses memory only, never local/sessionStorage:

- `usePageResource<T>(key, read, store?)` consumes the bootstrap synchronously and
  starts `read(signal)` only when that key is missing/idle. Multiple subscribers
  and StrictMode share one initial read. After invalidate it reads again; errors
  stay visible until explicit invalidate/load retries. Use a stable read adapter
  and include every filter/record identity in the key. It does not infer business
  endpoints or fill an empty backend loader. Shared reads survive one subscriber
  unmounting. `useSession` resets the default store before publishing successful
  identity/permission changes; custom authentication or separately owned stores
  must explicitly reset at their own equivalent boundary.
- `useRouteData<T>(key)` subscribes to `{data, hasData, status, error}`. It does not
  fetch on render or invent a default record. Use `createRouteDataStore(initial)`
  and the hook's second argument for an independently owned store.
- `routeData.load(key, signal => read(signal))` retains valid data during refresh;
  returns applied/error/superseded, like useAsyncResource. Changing keys reads the
  other key immediately; a missing key has no data. Use stable keys with all query
  filters and record identity. Explicitly trigger reads for missing/stale data.
- After a server-confirmed write, `publish(key, completeData)` replaces that
  resource before navigation. Use `invalidate(key)` when the complete replacement
  is unknown; it removes stale membership and invalidates in-flight reads. Update
  dependent collections as well as the individual record. Never mark a partial
  collection as complete by appending to an unknown snapshot.
- `invalidateMany(keys)` detaches all specified keys before notifying subscribers
  or cancelling old reads; no observer sees a half-invalid related group.
  `invalidateWhere(predicate)` selects existing keys first, then does the same
  atomic invalidation. Include the owning identity in your key convention so an
  object's write can invalidate its list/detail/alternate views without affecting
  unrelated objects. A throwing predicate changes nothing; publications from
  abort handlers belong to a newer owner and survive this invalidation.
- `reset({scope, resources:{}})` clears all cached data and supersedes pending
  work. `useSession` handles this for its successful changes to the default store.
  For other identity flows, call it before rendering another identity, then load
  authorized data. This includes remote session-expiry paths.
  A superseded adapter cannot republish even if it ignores AbortSignal.
- Use `AsyncContent hasData={snapshot.hasData}` to retain valid content through
  refresh. Do not replace edit drafts from a refresh; use record identity and
  per-field dirty handling if initial data can arrive after typing starts.

The store does not serialize server writes, infer relationships, detect remote
permission changes or replace server authorization. Aborting a request does not
roll back its server side effects. Registration/import alone is not proof that a
generated app has correctly wired the business data.

## Local async operations

`useAsyncResource(initialData)` is a single-resource, latest-started-operation
hook. `run(signal => Promise<data>)` invalidates and aborts the previous operation
**before** calling the adapter. Both success and failure may publish only while
their request generation and mounted lifetime are current. Cleanup invalidates
pending work; React StrictMode's setup/cleanup/setup cannot revive an old request.
`publish(data)` synchronously replaces data and invalidates older work.
Both `run(operation, beforeApply?)` and `publish(data, beforeApply?)` accept an
optional synchronous callback after ownership is checked, before state changes.
Session uses this to invalidate page data atomically with identity publication.
If that callback starts newer work, ownership is checked again; the older state
cannot overwrite it. Failed or superseded operations never invoke the callback.
Each call reserves its own generation and detaches the prior controller before
dispatching abort. An abort listener may synchronously start a newer `run` or
`publish`: that newer call owns the resource. The superseded outer call cannot
replace its controller or publish pending/data/error; an outer `run` whose
adapter has not started returns superseded without invoking it. A superseded
`publish` leaves the newer state intact. During unmount, abort listeners cannot
start new work or publish because the lifetime is already inactive.

State is `{data, status, error}`; status is idle/pending/ready/error. A current
failure preserves the previous data. `run` resolves to `{status: 'applied', data}`,
`{status: 'error', error}`, or `{status: 'superseded'}` (also after unmount). It does
not reject. Callers must inspect the result before navigation or success messages;
await alone does not establish success. Error is unknown: map it to product text.

`useSession(adapter)` is the actual bootstrap consumer, exported from `../ui`.
Supply stable methods (module-level functions or useCallback):

- `read(signal): Promise<User | null>` maps the current server session.
- `signIn(credentials, signal): Promise<User>` maps a successful authentication.
- `signOut(signal): Promise<unknown>` ends the server session; success publishes null.

The hook starts read on mount, exposes refresh/signIn/signOut, and routes all four
through the same resource generation. Initial read preserves the server-authorized
document bootstrap. Successful signIn/signOut/explicit refresh clear the shared
`routeData` before publishing the result, canceling and superseding old reads.
Explicit refresh clears even if the user is unchanged because permissions may
have changed. Exposed run/publish use the same boundary. Failed/stale operations
leave newer authorized pages intact. The new scope is an opaque in-memory
generation, not a user ID or token; server authorization remains necessary.
Adapters map an expired/absent session to null; a network failure is an error,
not evidence that authentication ended. Put one hook in the application's context
provider and share that result with pages. For example, a provider's handler uses
`const result = await session.signIn(credentials)` and navigates only when
`result.status === 'applied'`. Render bootstrap pending/error explicitly before
protected content. Use the current requirement for names, credentials, routes and
API response mapping; no endpoint or user schema is assumed here.

For navigation that removes the initiating button/editor, put the completion in
the stable session/provider handler: `session.signOut(() => navigate(destination))`
or `session.signIn(credentials, user => navigate(destinationFor(user)))`.
The optional synchronous callback runs only for a current successful operation,
after cache invalidation and before publishing the new identity. A child button's
`onApplied` may be suppressed if that child is unmounted by the session change;
do not make required navigation depend on that child's remaining lifetime.
Destination and router remain application-owned; there is no default redirect.

For other resources use `useAsyncResource` directly. Route every read and write
which may replace the same data through the same hook; if persistence happens
outside it, invalidate at write start via `run`, not only after it completes.
The latest-started operation wins, so do not start a competing automatic refresh
while an explicit write is pending unless that is intended. Serialize server
writes where order matters: abort/generation checks protect local publication,
not server side effects or cookie ordering. This is not a cache or a transaction
system and cannot protect arbitrary separate setState callbacks.

The default blank application does not impose authentication. Generated apps
must actually adopt this consumer (or equivalent tested logic). Presence/import
of the hooks does not verify adoption or business semantics.

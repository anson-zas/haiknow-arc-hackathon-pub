import {useCallback, useEffect} from 'react';
import {useAsyncResource} from './async-resource';
import {routeData} from './route-data';

let sessionGeneration = 0;
function clearAuthorizedPages() {
  routeData.reset({scope:`session-generation:${++sessionGeneration}`,resources:{}});
}

export type SessionAdapter<User, Credentials> = {
  read: (signal: AbortSignal) => Promise<User | null>;
  signIn: (credentials: Credentials, signal: AbortSignal) => Promise<User>;
  signOut: (signal: AbortSignal) => Promise<unknown>;
};

// Domain endpoints, credentials and response mapping are supplied by the app.
// Keep adapter methods stable (module-level functions or useCallback).
export function useSession<User, Credentials>(adapter: SessionAdapter<User, Credentials>) {
  const resource = useAsyncResource<User | null>(null);
  const {run: apply, publish: replace} = resource;
  const {read, signIn: authenticate, signOut: endSession} = adapter;
  // Reset before publishing the next identity, only for the current successful
  // operation. Failed/stale requests must not clear a newer authorized cache.
  const run = useCallback((operation:(signal:AbortSignal)=>Promise<User|null>, onApplied?:(user:User|null)=>void) =>
    apply(operation,user=>{clearAuthorizedPages();if(typeof onApplied==='function')onApplied(user)}), [apply]);
  const publish = useCallback((user:User|null) => replace(user,clearAuthorizedPages), [replace]);
  const refresh = useCallback(() => run(read), [run, read]);
  const signIn = useCallback((credentials: Credentials, onApplied?:(user:User|null)=>void) =>
    run(signal => authenticate(credentials, signal),onApplied), [run, authenticate]);
  const signOut = useCallback((onApplied?:(user:User|null)=>void) =>
    run(async signal => {await endSession(signal); return null;},onApplied), [run, endSession]);
  // The first read shares the document's server-authorized bootstrap. Explicit
  // refresh also clears pages, even for the same user (permissions may change).
  useEffect(() => {void apply(read);}, [apply,read]);
  return {...resource, run, publish, refresh, signIn, signOut};
}

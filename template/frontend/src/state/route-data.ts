import {useEffect, useSyncExternalStore} from 'react';
import type {AsyncResult} from './async-resource';

export type PageData = {scope: string; resources: Record<string, unknown>};
export type RouteSnapshot<T> = {data: T | undefined; hasData: boolean;
  status: 'idle' | 'pending' | 'ready' | 'error'; error: unknown};
const empty: RouteSnapshot<unknown> = Object.freeze({data:undefined,hasData:false,status:'idle',error:null});
type Entry = {state: RouteSnapshot<unknown>; controller?: AbortController};

// Memory only: no cross-document/user storage. Writes supersede pending reads.
export function createRouteDataStore(initial: PageData = {scope:'public',resources:{}}) {
  let entries=new Map<string,Entry>(), scope=initial.scope;
  const listeners=new Set<()=>void>();
  const notify=()=>listeners.forEach(listener=>listener());
  const ready=(data:unknown):Entry=>({state:{data,hasData:true,status:'ready',error:null}});
  for (const [key,value] of Object.entries(initial.resources)) entries.set(key,ready(value));
  const replace=(key:string,entry?:Entry)=>{
    const old=entries.get(key);
    if(entry) entries.set(key,entry); else entries.delete(key);
    old?.controller?.abort();notify();
  };
  const invalidateMany=(keys:readonly string[])=>{
    const previous:Entry[]=[];
    for(const key of new Set(keys)) {
      const entry=entries.get(key);
      if(entry){previous.push(entry);entries.delete(key);}
    }
    if(!previous.length)return;
    // Detach the entire batch before abort listeners can publish newer data.
    // A late response must not restore an old sibling or overwrite that owner.
    previous.forEach(entry=>entry.controller?.abort());notify();
  };
  return {
    get scope(){return scope;},
    read<T>(key:string):RouteSnapshot<T>{return (entries.get(key)?.state ?? empty) as RouteSnapshot<T>;},
    subscribe(listener:()=>void){listeners.add(listener);return ()=>{listeners.delete(listener)};},
    publish<T>(key:string,data:T){replace(key,ready(data));},
    invalidate(key:string){replace(key);},
    invalidateMany,
    invalidateWhere(matches:(key:string)=>boolean){
      // Evaluate first: a throwing selector must leave every snapshot intact.
      invalidateMany([...entries.keys()].filter(matches));
    },
    reset(next:PageData){
      const previous=entries;
      entries=new Map(Object.entries(next.resources).map(([key,value])=>[key,ready(value)]));
      scope=next.scope;
      previous.forEach(entry=>entry.controller?.abort());notify();
    },
    async load<T>(key:string,operation:(signal:AbortSignal)=>Promise<T>):Promise<AsyncResult<T>> {
      const previous=entries.get(key)?.state ?? empty, controller=new AbortController();
      const entry:Entry={state:{...previous,status:'pending',error:null},controller};
      replace(key,entry);
      const current=()=>entries.get(key)===entry;
      if(!current())return {status:'superseded'};
      try {
        const data=await operation(controller.signal);
        if(!current())return {status:'superseded'};
        entry.state=ready(data).state;entry.controller=undefined;notify();
        return current() ? {status:'applied',data} : {status:'superseded'};
      } catch(error) {
        if(!current())return {status:'superseded'};
        entry.state={...previous,status:'error',error};entry.controller=undefined;notify();
        return current() ? {status:'error',error} : {status:'superseded'};
      }
    },
  };
}

export function readInitialPageData():PageData | undefined {
  if(typeof document==='undefined')return;
  const text=document.getElementById('page-data')?.textContent;
  if(!text)return;
  const data=JSON.parse(text);
  if(data.version!==1 || data.location!==location.pathname+location.search ||
    typeof data.scope!=='string' || !data.scope || !data.resources ||
    typeof data.resources!=='object' || Array.isArray(data.resources)) {
    throw new Error('Invalid page data for the current document');
  }
  return data;
}

export const routeData=createRouteDataStore(readInitialPageData());
export function useRouteData<T>(key:string,store=routeData):RouteSnapshot<T> {
  return useSyncExternalStore(store.subscribe,()=>store.read<T>(key));
}

// Prefer the document snapshot; fetch a missing resource once across consumers.
// Errors need explicit retry (invalidate/load), never an automatic request loop.
export function usePageResource<T>(key:string,read:(signal:AbortSignal)=>Promise<T>,store=routeData):RouteSnapshot<T> {
  const snapshot=useRouteData<T>(key,store);
  useEffect(()=>{
    if(store.read(key).status==='idle')void store.load(key,read);
    // Shared reads survive one subscriber unmounting. reset/invalidate owns
    // cancellation. useSession resets the default store on successful changes;
    // custom session flows must reset their own user/permission scope.
  },[key,read,store,snapshot.status]);
  return snapshot;
}

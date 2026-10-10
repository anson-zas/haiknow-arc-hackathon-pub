import {useCallback, useEffect, useRef, useState} from 'react';

export type AsyncResult<T> = {status: 'applied'; data: T} | {status: 'error'; error: unknown} | {status: 'superseded'};
export type AsyncState<T> = {data: T; status: 'idle' | 'pending' | 'ready' | 'error'; error: unknown};

// One resource, one latest-started operation. Cancellation is advisory: generation
// checks also reject results from adapters which ignore AbortSignal.
export function useAsyncResource<T>(initialData: T) {
  const [state, setState] = useState<AsyncState<T>>({data: initialData, status: 'idle', error: null});
  const lifetime = useRef({active: true, generation: 0, controller: null as AbortController | null});
  const invalidate = useCallback(() => {
    const current = lifetime.current;
    const generation = ++current.generation, controller = current.controller;
    // Abort dispatches user listeners synchronously. Detach the old controller
    // first, and keep our reserved generation if a listener starts newer work.
    current.controller = null;
    controller?.abort();
    return generation;
  }, []);
  useEffect(() => {
    lifetime.current.active = true;
    return () => {lifetime.current.active = false; invalidate();};
  }, [invalidate]);
  const run = useCallback(async (operation: (signal: AbortSignal) => Promise<T>, beforeApply?: (data:T)=>void): Promise<AsyncResult<T>> => {
    if (!lifetime.current.active) return {status: 'superseded'};
    const generation = invalidate();
    const current = () => lifetime.current.active && lifetime.current.generation === generation;
    if (!current()) return {status: 'superseded'};
    const controller = new AbortController();
    lifetime.current.controller = controller;
    setState(previous => ({...previous, status: 'pending', error: null}));
    try {
      const data = await operation(controller.signal);
      if (!current()) return {status: 'superseded'};
      beforeApply?.(data);
      if (!current()) return {status: 'superseded'};
      setState({data, status: 'ready', error: null});
      return {status: 'applied', data};
    } catch (error) {
      if (!current()) return {status: 'superseded'};
      setState(previous => ({...previous, status: 'error', error}));
      return {status: 'error', error};
    } finally {
      if (current()) lifetime.current.controller = null;
    }
  }, [invalidate]);
  const publish = useCallback((data: T, beforeApply?: (data:T)=>void) => {
    if (!lifetime.current.active) return;
    const generation = invalidate();
    if (!lifetime.current.active || lifetime.current.generation !== generation) return;
    beforeApply?.(data);
    if (!lifetime.current.active || lifetime.current.generation !== generation) return;
    setState({data, status: 'ready', error: null});
  }, [invalidate]);
  return {...state, run, publish};
}

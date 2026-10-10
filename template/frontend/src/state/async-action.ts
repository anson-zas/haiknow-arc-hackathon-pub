import {useCallback, useLayoutEffect, useRef, useState} from 'react';
import type {AsyncResult} from './async-resource';

// Unlike a latest-wins read, a write must not start twice. Explicit outcomes also
// prevent adapters which swallow errors (returning null) from closing an editor.
export function useAsyncAction() {
  const [state, setState] = useState({pending: false, failed: false, error: null as unknown});
  const lifetime = useRef({active: true, inFlight: false});
  useLayoutEffect(() => {
    // Retain the initial owner: a child may already have started work in a layout
    // effect. StrictMode's new lifetime must not reactivate an old result.
    if (!lifetime.current.active) {
      lifetime.current = {active: true, inFlight: false};
      setState({pending: false, failed: false, error: null});
    }
    const owner = lifetime.current;
    return () => {owner.active = false;};
  }, []);
  const run = useCallback(async <T,>(operation: () => Promise<AsyncResult<T>>,
    onApplied?: (data: T) => void): Promise<AsyncResult<T>> => {
    const owner = lifetime.current;
    if (!owner.active || owner.inFlight) return {status: 'superseded'};
    owner.inFlight = true;
    setState({pending: true, failed: false, error: null});
    let result: AsyncResult<T>;
    try {
      result = await operation();
      if (!owner.active) return {status: 'superseded'};
      if (!result || typeof result !== 'object' ||
          !['applied', 'error', 'superseded'].includes(result.status) ||
          (result.status === 'applied' && !('data' in result)) ||
          (result.status === 'error' && !('error' in result))) {
        throw new Error('Action must return an explicit AsyncResult');
      }
      if (result.status === 'applied') onApplied?.(result.data);
    } catch (error) {
      result = {status: 'error', error};
    } finally {
      owner.inFlight = false;
    }
    if (!owner.active) return {status: 'superseded'};
    setState({pending: false, failed: result.status === 'error',
      error: result.status === 'error' ? result.error : null});
    return result;
  }, []);
  return {...state, run};
}

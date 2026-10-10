import React from 'react';
import {useAsyncAction} from '../state/async-action';
import type {AsyncResult} from '../state/async-resource';

// For standalone actions. Forms should call the same hook from onSubmit and use
// a native submit button, so Enter and clicking share one guarded write path.
export function AsyncActionButton<T>({onAction, onApplied, pendingContent, errorContent,
  children, disabled, ...props}:
  Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, 'onClick' | 'type' | 'onError'> & {
    onAction: () => Promise<AsyncResult<T>>; onApplied?: (data: T) => void;
    pendingContent?: React.ReactNode; errorContent: (error: unknown) => React.ReactNode;
  }) {
  const action = useAsyncAction();
  return <><button {...props} type="button" disabled={disabled || action.pending}
    aria-busy={action.pending} onClick={() => {void action.run(onAction, onApplied);}}>{children}</button>
    {action.pending && pendingContent && <span role="status">{pendingContent}</span>}
    {action.failed && <span role="alert">{errorContent(action.error)}</span>}
  </>;
}

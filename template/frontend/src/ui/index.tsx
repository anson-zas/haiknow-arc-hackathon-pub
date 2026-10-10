import React, {useEffect, useId, useRef, useState} from 'react';
import {useAsyncAction} from '../state/async-action';
import type {AsyncResult} from '../state/async-resource';

// Labels and record identities belong to the current requirement, never this kit.
export function ActionButton({type = 'button', ...props}: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return <button {...props} type={type}/>;
}

export function NamedForm({name, ...props}: React.FormHTMLAttributes<HTMLFormElement> & {name: string}) {
  return <form {...props} aria-label={name}/>;
}

export function TextField({label, error, id, ...props}: React.InputHTMLAttributes<HTMLInputElement> & {label: string; error?: string}) {
  const generated = useId(), inputId = id ?? generated, errorId = `${inputId}-error`;
  return <div><label htmlFor={inputId}>{label}</label>
    <input {...props} id={inputId} aria-invalid={error ? true : props['aria-invalid']}
      aria-describedby={[props['aria-describedby'], error && errorId].filter(Boolean).join(' ') || undefined}/>
    {error && <span id={errorId} role="alert">{error}</span>}
  </div>;
}

export function Disclosure({label, defaultOpen = false, children}: {label: string; defaultOpen?: boolean; children: React.ReactNode}) {
  const [open, setOpen] = useState(defaultOpen), id = useId();
  return <section><ActionButton aria-expanded={open} aria-controls={id} onClick={() => setOpen(v => !v)}>{label}</ActionButton>
    <div id={id} hidden={!open}>{open && children}</div></section>;
}

// A popover of native actions, not an ARIA menu (which requires arrow-key navigation).
export function ActionMenu({label, children, openOnHover = false}: {label: string; children: React.ReactNode; openOnHover?: boolean}) {
  const [open, setOpen] = useState(false), id = useId(), root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {if (!root.current?.contains(event.target as Node)) setOpen(false);};
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, [open]);
  return <div ref={root} style={{position:'relative', display:'inline-block'}} onMouseEnter={() => {if (openOnHover) setOpen(true);}}
    onBlur={event => {if (!event.currentTarget.contains(event.relatedTarget as Node)) setOpen(false);}}
    onKeyDown={event => {if (event.key === 'Escape') {setOpen(false); trigger.current?.focus(); event.stopPropagation();}}}>
    <button ref={trigger} type="button" aria-expanded={open} aria-controls={id} onClick={() => setOpen(true)}>{label}</button>
    <div id={id} hidden={!open} style={{position:'absolute', top:'100%', left:0, zIndex:10, background:'Canvas', color:'CanvasText'}}
      onClick={event => {
        const action=(event.target as Element).closest('button:not(:disabled), a[href]');
        if (action && event.currentTarget.contains(action)) {setOpen(false); trigger.current?.focus();}
      }}>{open && children}</div>
  </div>;
}

// The action owner survives closing the popover. Unmounting this entire
// component still invalidates callbacks through useAsyncAction's lifetime.
export function AsyncActionMenu<T>({label, items, openOnHover, pendingContent, errorContent}: {
  label: string; openOnHover?: boolean; pendingContent?: React.ReactNode;
  errorContent: (error: unknown) => React.ReactNode;
  items: {id?: string; name: string; disabled?: boolean;
    onAction: () => Promise<AsyncResult<T>>; onApplied?: (data: T) => void}[];
}) {
  const action = useAsyncAction();
  return <div>
    <ActionMenu label={label} openOnHover={openOnHover}>
      {items.map((item, index)=><ActionButton key={item.id ?? index}
        disabled={item.disabled || action.pending} aria-busy={action.pending}
        onClick={()=>{void action.run(item.onAction, item.onApplied);}}>
        {item.name}</ActionButton>)}
    </ActionMenu>
    {action.pending && pendingContent && <span role="status">{pendingContent}</span>}
    {action.failed && <span role="alert">{errorContent(action.error)}</span>}
  </div>;
}

// Use when the product calls for a real menu. ActionMenu remains a button popover.
export function Menu({label, items}: {label: string; items: {name: string; disabled?: boolean; onSelect: () => void}[]}) {
  const [open,setOpen]=useState(false), root=useRef<HTMLDivElement>(null), trigger=useRef<HTMLButtonElement>(null);
  const panel=useRef<HTMLDivElement>(null), initial=useRef(0), id=useId();
  const available=()=>Array.from(panel.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')??[]);
  const close=(focus=false)=>{setOpen(false);if(focus)trigger.current?.focus();};
  useEffect(()=>{
    if(!open)return;
    const buttons=available();buttons[initial.current<0?buttons.length-1:0]?.focus();
    const outside=(event:PointerEvent)=>{if(!root.current?.contains(event.target as Node))close();};
    document.addEventListener('pointerdown',outside);
    return()=>document.removeEventListener('pointerdown',outside);
  },[open]);
  const show=(last=false)=>{initial.current=last?-1:0;setOpen(true);};
  return <div ref={root} style={{position:'relative',display:'inline-block'}}
    onBlur={event=>{if(!event.currentTarget.contains(event.relatedTarget as Node))close();}}>
    <button ref={trigger} type="button" aria-haspopup="menu" aria-expanded={open} aria-controls={id}
      onClick={()=>open?close():show()} onKeyDown={event=>{
        if(event.key==='ArrowDown'||event.key==='ArrowUp'){event.preventDefault();show(event.key==='ArrowUp');}
      }}>{label}</button>
    {open&&<div id={id} ref={panel} role="menu" aria-label={label}
      style={{position:'absolute',zIndex:10,background:'Canvas',color:'CanvasText'}} onKeyDown={event=>{
        const buttons=available(), index=buttons.indexOf(document.activeElement as HTMLButtonElement);
        if(event.key==='Escape'){event.preventDefault();event.stopPropagation();close(true);return;}
        if(event.key==='Tab'){close();return;}
        if(!buttons.length)return;
        const next=event.key==='Home'?0:event.key==='End'?buttons.length-1:
          event.key==='ArrowDown'?(index+1)%buttons.length:event.key==='ArrowUp'?(index-1+buttons.length)%buttons.length:-1;
        if(next>=0){event.preventDefault();buttons[next].focus();}
        else if(event.key.length===1&&!event.ctrlKey&&!event.metaKey&&!event.altKey){
          const ordered=[...buttons.slice(index+1),...buttons.slice(0,index+1)];
          const match=ordered.find(b=>b.textContent?.toLocaleLowerCase().startsWith(event.key.toLocaleLowerCase()));
          if(match){event.preventDefault();match.focus();}
        }
      }}>{items.map((item,index)=><button key={index} type="button" role="menuitem" tabIndex={-1}
        disabled={item.disabled} onClick={()=>{close(true);item.onSelect();}}>{item.name}</button>)}</div>}
  </div>;
}

export function ToggleGroup({name, value, options, onChange}: {name: string; value: string;
  options: {value: string; name: string; disabled?: boolean}[]; onChange: (value: string) => void}) {
  return <div role="group" aria-label={name}>{options.map(option=><ActionButton key={option.value}
    aria-pressed={option.value===value} disabled={option.disabled} onClick={()=>onChange(option.value)}>
    {option.name}</ActionButton>)}</div>;
}

export function AsyncCheckbox({label, checked, onCommit, onError, disabled, id, ...props}:
  Omit<React.InputHTMLAttributes<HTMLInputElement>, 'type' | 'checked' | 'defaultChecked' | 'onChange' | 'onError'> &
  {label: string; checked: boolean; onCommit: (checked: boolean) => Promise<unknown>; onError?: (error: unknown) => void}) {
  const generated = useId(), inputId = id ?? generated;
  const [value, setValue] = useState(checked), [pending, setPending] = useState(false);
  const mounted = useRef(true), inFlight = useRef(false);
  useEffect(() => {mounted.current = true; return () => {mounted.current = false;};}, []);
  useEffect(() => {setValue(checked);}, [checked]);
  const change = async (next: boolean) => {
    if (inFlight.current) return;
    const previous = value;
    inFlight.current = true; setValue(next); setPending(true);
    try {await onCommit(next);}
    catch (error) {if (mounted.current) {setValue(previous); onError?.(error);}}
    finally {inFlight.current = false; if (mounted.current) setPending(false);}
  };
  return <label htmlFor={inputId}><input {...props} id={inputId} type="checkbox" checked={value}
    disabled={disabled || pending} aria-busy={pending} onChange={event => void change(event.target.checked)}/>{label}</label>;
}

export function Dialog({name, open, onClose, children}: {name: string; open: boolean; onClose: () => void; children: React.ReactNode}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
    return () => {if (dialog.open) dialog.close();};
  }, [open]);
  return <dialog ref={ref} aria-label={name} onCancel={event => {event.preventDefault(); onClose();}}
    onClose={event => {if (open && !event.currentTarget.open) onClose();}}>{children}</dialog>;
}

export {useAsyncResource} from '../state/async-resource';
export {useSession} from '../state/session';
export {PageSection, AsyncContent} from './page';
export {RecordCard, RecordTitle} from './record';
export {SelectField, TextAreaField, RadioGroup, CheckboxGroup} from './fields';
export {EditorSection, RecordStatus} from './editor';
export {createRouteDataStore, routeData, useRouteData, usePageResource, readInitialPageData} from '../state/route-data';
export {AsyncActionButton} from './async-action';
export {useAsyncAction} from '../state/async-action';
export type {AsyncResult} from '../state/async-resource';

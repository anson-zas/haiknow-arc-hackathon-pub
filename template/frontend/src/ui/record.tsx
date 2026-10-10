import React from 'react';
import {RecordStatus} from './editor';

export function RecordTitle({level=3, ...props}:
  React.HTMLAttributes<HTMLHeadingElement> & {level?: 2 | 3 | 4 | 5 | 6}) {
  const Heading = `h${level}` as 'h2' | 'h3' | 'h4' | 'h5' | 'h6';
  return <Heading {...props}/>;
}

// Keep article semantics and separate native controls: wrapping the entire card
// in a button would nest secondary buttons/inputs and break keyboard behavior.
const interactive = 'button,a,input,select,textarea,label,summary,dialog,' +
  '[role="button"],[role="link"],[role="checkbox"],[role="switch"],[role="menuitem"],' +
  '[role="tab"],[role="radio"],[role="combobox"],[role="textbox"],[role="searchbox"],' +
  '[role="slider"],[role="spinbutton"],[role="option"],[role="menuitemcheckbox"],[role="menuitemradio"],' +
  '[contenteditable]:not([contenteditable="false"]),[data-card-action]';

export function RecordCard({name, onOpen, headingLevel=3, children, actions, statuses, onClick, ...props}:
  React.HTMLAttributes<HTMLElement> & {name: string; onOpen?: () => void;
    headingLevel?: 2 | 3 | 4 | 5 | 6; actions?: React.ReactNode;
    statuses?: React.ComponentProps<typeof RecordStatus>[]}) {
  const title = <RecordTitle level={headingLevel}>
    {onOpen ? <button type="button" onClick={onOpen}>{name}</button> : name}
  </RecordTitle>;
  const content=<>{title}{statuses?.map((status,index)=><RecordStatus key={index} {...status}/>)}{children}</>;
  return <article {...props} aria-label={name} onClick={event => {
    onClick?.(event);
    if (!onOpen || event.defaultPrevented) return;
    const target = event.target instanceof Element ? event.target : null;
    if (target?.closest(interactive)) return;
    onOpen();
  }}>
    {actions != null ? <>
      <div style={{minBlockSize: onOpen ? '8rem' : undefined}}>{content}</div>
      <div data-card-action style={{display:'flex', flexWrap:'wrap', alignItems:'center', gap:8}}>
        {actions}
      </div>
    </> : content}
  </article>;
}

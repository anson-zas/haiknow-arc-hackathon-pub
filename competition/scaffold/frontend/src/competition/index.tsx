import React, {useId} from 'react';
import {Link, useHref, useLocation, useNavigate} from 'react-router-dom';
import {ActionButton, RecordTitle} from '../ui';

// Competition default: an operation may change routes and still be a button.
// A fresh document loads authorized data for its own route before first paint.
// Local draft/selection changes stay inside the current document. Explicit SPA
// requirements may opt into client navigation; no global click interception.
export function RouteAction({to, children, navigation='document', ...props}:
  Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, 'onClick' | 'type'> &
  {to: string; navigation?: 'document' | 'client'}) {
  const navigate = useNavigate(), href=useHref(to);
  return <ActionButton {...props} onClick={() => navigation==='client'
    ? navigate(to) : window.location.assign(href)}>{children}</ActionButton>;
}

// Editable competition preference for selectable records. Explicit YAML links
// take precedence; render one control, never duplicate button/link aliases.
export function RecordEntry({name, to, role='button', headingLevel=3, navigation='document'}:
  {name: string; to: string; role?: 'button' | 'link'; headingLevel?: 2 | 3 | 4 | 5 | 6;
   navigation?: 'document' | 'client'}) {
  return <RecordTitle level={headingLevel}>{role==='link'
    ? <Link to={to} reloadDocument={navigation==='document'}>{name}</Link>
    : <RouteAction to={to} navigation={navigation}>{name}</RouteAction>}</RecordTitle>;
}

export type SidebarItem = {name: string; to: string; icon: React.ReactNode; role?: 'button' | 'link'};

// Every item stays mounted and named in icon-only mode, including dynamic groups.
// Caller-supplied roles/names preserve explicit requirements; defaults are policy.
export function Sidebar({name, toggleName, items, collapsed, onCollapsedChange, role='complementary', navigation='document'}:
  {name: string; toggleName: string; items: SidebarItem[]; collapsed: boolean;
   onCollapsedChange: (collapsed: boolean) => void; role?: 'complementary' | 'navigation';
   navigation?: 'document' | 'client'}) {
  const id=useId(), location=useLocation(), Container=role==='navigation'?'nav':'aside';
  return <Container aria-label={name} data-collapsed={collapsed}>
    <ActionButton aria-label={toggleName} aria-expanded={!collapsed} aria-controls={id}
      onClick={()=>onCollapsedChange(!collapsed)}><span aria-hidden="true">☰</span></ActionButton>
    <div id={id}>{items.map(item=>{
      const content=<><span aria-hidden="true">{item.icon}</span>{!collapsed&&<span>{item.name}</span>}</>;
      const props={'aria-label':item.name,'aria-current':location.pathname===item.to?'page' as const:undefined,
                   title:collapsed?item.name:undefined};
      return item.role==='link'
        ? <Link key={item.to} {...props} to={item.to} reloadDocument={navigation==='document'}>{content}</Link>
        : <RouteAction key={item.to} {...props} to={item.to} navigation={navigation}>{content}</RouteAction>;
    })}</div>
  </Container>;
}

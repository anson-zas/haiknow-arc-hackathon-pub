import React, {useId, useState} from 'react';

// Keep subordinate controls in their owner's DOM and retain local draft state.
export function EditorSection({label, children, defaultOpen=false, open, onOpenChange}: {
  label: string; children: React.ReactNode; defaultOpen?: boolean;
  open?: boolean; onOpenChange?: (open: boolean) => void;
}) {
  const id=useId(), [expanded,setExpanded]=useState(defaultOpen), visible=open ?? expanded;
  return <section>
    <button type="button" aria-expanded={visible} aria-controls={id} onClick={()=>{
      if (open === undefined) setExpanded(!visible);
      onOpenChange?.(!visible);
    }}>{label}</button>
    <div id={id} hidden={!visible}>{children}</div>
  </section>;
}

export function RecordStatus({label, icon, active=true}: {
  label: string; icon?: React.ReactNode; active?: boolean;
}) {
  return active ? <span role="img" aria-label={label} title={label}>
    <span aria-hidden="true">{icon ?? label}</span>
  </span> : null;
}

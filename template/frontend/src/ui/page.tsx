import React, {useId} from 'react';

// Keep page identity outside the asynchronous data boundary. Titles and actions
// come from the requirement; never invent a record title before loading it.
export function PageSection({title, actions, children, ...props}:
  Omit<React.HTMLAttributes<HTMLElement>, 'title'> & {title: string; actions?: React.ReactNode}) {
  const headingId = useId();
  return <section {...props} aria-labelledby={headingId}>
    <header><h1 id={headingId}>{title}</h1>{actions}</header>{children}
  </section>;
}

export function AsyncContent({pending, error, hasData, pendingContent, errorContent, children}: {
  pending: boolean; error?: boolean; pendingContent: React.ReactNode;
  hasData?: boolean; errorContent: React.ReactNode; children: React.ReactNode;
}) {
  return <>
    {(hasData === true || (hasData === undefined && !pending && !error)) &&
      <React.Fragment key="content">{children}</React.Fragment>}
    {(pending || (hasData === false && !error)) && <div key="pending" role="status" aria-live="polite">{pendingContent}</div>}
    {!pending && error && <div key="error" role="alert">{errorContent}</div>}
  </>;
}

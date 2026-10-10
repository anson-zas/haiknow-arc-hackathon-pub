# Reusable UI contracts

Import from `./ui` (adjust the relative path). All product labels, field values,
routes and validation messages come from the caller. No component invents domain
data, aliases, success messages or application navigation.

`AsyncContent` accepts optional `hasData`. Set it true only when children represent
valid data for the current record/user. Background pending/error feedback then
appears alongside the same mounted children, retaining input and focus. Without
valid data (explicit false), children stay absent even before the next request
starts, and pending/error feedback is shown. Omitting hasData preserves the old
pending/error replacement behavior. Do not keep a previous
record's form merely to avoid loading; use separate record keys and disable unsafe
submits while initial data is absent.

`EditorSection` renders a named native toggle and an inline region. Its children
remain mounted while collapsed, but are hidden from interaction/accessibility.
It accepts label, children, defaultOpen, or controlled open/onOpenChange. Put it
inside the owning editor when appropriate. It adds no dialog or save/close action.
`CheckboxGroup` accepts label, value (string array), options (value/label/disabled),
onChange and native fieldset props, including disabled. It uses native labeled
checkboxes and preserves the caller's draft. Optional error is associated with the
fieldset. Persistence and when changes commit remain the owner's responsibility.

`RecordStatus` displays a meaningful icon or its label with role=img, title and
accessible label. Pass active=false to remove it. All status text is caller-owned;
preserve separately required attributes on the parent. It does not automatically
copy parent attributes, infer state or add hidden locator aliases.
`RecordCard statuses={[{label, icon, active}]}` renders these indicators as visible
record descendants after the heading. Omit icon to display the label itself;
inactive entries are absent. Labels remain separate from the record title and
secondary actions. Use only actual states, never arbitrary attribute mirrors.

| Component | Contract |
|---|---|
| ActionButton | Native button; default type=button |
| NamedForm | Native form with accessible `name`; use onSubmit for Enter and click |
| TextField / TextAreaField | Visible `label` targets the native input/textarea; optional `error` is associated with it |
| SelectField | Native single select; `label`, native options/values and onChange; no multiple/size override |
| RadioGroup | Native fieldset/legend; `label`, `value`, `onChange`, `options: {value,label,disabled?}[]`; independent name by default |
| RecordCard | Named article and title; optional `onOpen` activates the primary surface; `actions` separates secondary controls below it |
| RecordTitle | Record heading; `level` defaults to 3, children contain the actual title/control |
| Dialog | Native modal; `name`, `open`, `onClose`; caller owns business save/cancel behavior |
| PageSection | Stable named section with h1 `title`, optional `actions`, children |
| AsyncContent | Replaces only its children while `pending` or `error`; caller provides pendingContent/errorContent |
| AsyncActionButton | Standalone guarded write; unchanged button children, separate progress and error feedback |
| AsyncActionMenu | Named button popover; one stable async owner for all items, feedback survives closing the popover |

The existing Disclosure, ActionMenu, Menu, ToggleGroup and AsyncCheckbox are
implemented in `index.tsx`. Use the actual component props. Native elements are
appropriate when these contracts do not match the required behavior.

## Record activation

When activating a record opens an editor, use `RecordCard name={record.name}
onOpen={() => open(record)}`. It supplies the title once as a heading containing
a native button (Enter/Space included). Put summary/body in children and secondary
controls in `actions={<YourControls/>}`; do not repeat the title or wrap the whole
article in a button. With actions, an opening card reserves 8rem for its primary
content and places a wrapping action area below it. Blank action-area space does
not open the record. Without actions the existing children layout is unchanged.
Keep secondary controls in normal flow; custom absolute positioning or an unusually
large toolbar can still displace the primary click area and needs browser checks.
Clicking passive text or article whitespace opens the record. Buttons, links,
labels, inputs, editable regions and `[data-card-action]` descendants keep their
own actions without opening it. A card without onOpen still displays its name as
a plain heading and stays passive; do not repeat that title in children.
Custom interactive descendants must use native/ARIA semantics or data-card-action;
test that their operations do not accidentally open the editor.

For a record that navigates, use one real destination control in RecordTitle.
The optional competition RecordEntry supplies this choice; the generic kit does
not impose a button on a requirement that explicitly asks for a link.

## Stable page identity

Keep PageSection outside AsyncContent. Known page titles and navigation can render
before an API response. Put data-dependent content inside AsyncContent and disable
actions which cannot safely execute yet. Do not invent a record title or render a
fake successful state while data is pending. This boundary unmounts its children:
keep an editor outside it when refreshing data must preserve unsaved input.

## Writes and success ownership

`useAsyncAction()` returns `run`, `pending`, `failed`, `error`. It prevents a second
in-flight operation synchronously and releases the lock after errors. Its adapter
must return the same `AsyncResult<T>` used by useAsyncResource:

- `{status: 'applied', data}` means the operation actually succeeded.
- `{status: 'error', error}` or a thrown error preserves the editor and permits retry.
- `{status: 'superseded'}` does not publish success.

Returning null/undefined is an integration error, not success. `run(operation,
onApplied)` invokes onApplied only for an applied result while still mounted.
Close the editor, clear input or navigate in that callback. This local guard does
not replace server transaction ordering/idempotency or cross-client concurrency.

For forms, route both keyboard and mouse submission through one entry:

```tsx
const action = useAsyncAction();
return <NamedForm name={labels.form} onSubmit={event => {
  event.preventDefault();
  const data = new FormData(event.currentTarget);
  void action.run(async () => {
    const saved = await api.save(data); // adapter throws on failure
    return {status: 'applied', data: saved};
  }, saved => onSaved(saved));
}}>
  <TextField label={labels.field} name={fieldName}/>
  <button type="submit" disabled={action.pending} aria-busy={action.pending}>
    {labels.save}
  </button>
  {action.pending && <p role="status">{labels.pending}</p>}
  {action.failed && <p role="alert">{describeError(action.error)}</p>}
</NamedForm>;
```

For a standalone operation use AsyncActionButton with `onAction`, `onApplied`,
optional `pendingContent` and required `errorContent(error)`. It is type=button,
not a form submit shortcut. If the adapter already returns AsyncResult, pass that
result through; do not wrap an error outcome inside a new applied result.

For a closing action popover use `AsyncActionMenu`, with caller-supplied
`items: {id?, name, disabled?, onAction, onApplied?}[]`, `pendingContent` and
`errorContent(error)`. Its items are native buttons. The action hook and feedback
live outside the popover, so closing it does not discard a successful refresh or
hide a failed write. Reopening while a write is pending keeps every item disabled.
The adapter returns the same explicit AsyncResult as AsyncActionButton.

```tsx
<AsyncActionMenu key={record.id} label={labels.actions}
  pendingContent={labels.pending} errorContent={describeError}
  items={[{id: actionId, name: labels.action, onAction: async () => {
    const saved = await api.update(record.id);
    return {status: 'applied', data: saved};
  }, onApplied: saved => replaceVisibleRecord(saved)}]}/>
```

Keep this component mounted in the record/page owner. Do not nest an
AsyncActionButton inside ActionMenu: closing that menu unmounts its children and
correctly invalidates their callbacks. If the whole record/page is removed,
AsyncActionMenu also suppresses late callbacks; use a stable record identity as
its key when reusing the same UI position for a different record. For a custom
popover, keep useAsyncAction in an equally stable parent and render plain action
buttons in the transient panel.

## Verification

`contracts.json` exposes supported static role/name bindings to the source auditor.
Dynamic values, nested option arrays, component usage and business success still
require runtime tests. A source audit passing is not evidence of a usable form.
Keep controls inside the intended named container and verify actual keyboard,
pending, failure, retry and persistence behavior using requirement-derived tests.

# Competition-derived preferences

This editable overlay summarizes interaction-test habits, never task answers.
The current YAML is authoritative. Preserve its explicit words, roles, states,
order and data even if a public helper once contradicted them. Examples below are
illustrations only; do not insert their records, labels or routes into the product.

## Use executable scaffolding and source checks

- Reuse `frontend/src/ui`: ActionButton, TextField, NamedForm, RecordCard, Dialog,
  Disclosure, ActionMenu, Menu, ToggleGroup, AsyncCheckbox, SelectField, TextAreaField,
  RadioGroup, CheckboxGroup, EditorSection, RecordStatus, PageSection, AsyncContent,
  AsyncActionButton and useAsyncAction.
  Read frontend/src/ui/README.md and the actual props. Supply names
  from the current YAML, not synonyms. Implement business behavior around these
  components; do not replace them with hand-written imitations without a reason.
  Native elements remain appropriate when requirements need different behavior.
- Keep known page headings and actions in PageSection, outside AsyncContent.
  Load/failure should replace only the data region; disable data-dependent actions
  until safe. Do not invent record names or remount an unsaved editor on refresh.
  Use native SelectField/TextAreaField/RadioGroup when those controls satisfy YAML;
  preserve labels and options verbatim. Custom widgets need real keyboard behavior.
- Supply current-route and shared navigation data through backend/src/page-data.js,
  using the same authorized read services as the API. The template injects this real
  snapshot in both built and Vite HTML. Use usePageResource(key, read) for shared records (bootstrap first; missing data loads once);
  pass hasData to AsyncContent so background refresh retains usable content and
  drafts. Wire loaders for destination detail/settings/list routes, not just home.
  After a successful write, publish or invalidate related keys BEFORE navigation:
  invalidateMany(keys) or invalidateWhere(key => belongsToChangedObject(key)) clears
  a family atomically; include its collection and alternate views, not unrelated data.
  useSession clears default snapshots on successful identity/permission
  changes; custom identity flows must reset them. Put required session navigation
  in the stable handler's signOut(callback)/signIn(credentials, callback), not an
  onApplied owned by a child that the session change unmounts.
  See frontend/src/state/README.md for the API. Never preload the whole database,
  copy seeds into client defaults, or report a failed read as a successful empty list.
- Unless YAML specifies an independent surface, expand an object's subordinate
  settings with EditorSection inside its current editor. CheckboxGroup keeps native
  labeled checkboxes in that DOM scope; share draft and save outcome with the owner.
  Do not add a second dialog/footer merely for an attribute chooser. Explicit YAML
  surfaces and names override this editable composition preference.
- When a record state has a meaningful visible indicator, pass statuses={[{label, icon, active}]} to RecordCard, or use RecordStatus
  directly; labels/icons come from the source. Keep any separately required root attributes too.
  Do not mirror arbitrary attributes into hidden descendants or invent status names.
- For saves use useAsyncAction from the form's onSubmit so Enter and clicking share
  one guarded operation. Close/navigate only in its onApplied callback. For standalone
  operations use AsyncActionButton. Both consume explicit applied/error/superseded
  results, release pending after failure and keep the button name unchanged. A plain
  null return is not success. When using useAsyncResource.run, pass through its
  outcome; never wrap its error as applied. Show errors while retaining draft input.
- For login/logout or a completed form that leaves the current document, prefer a
  native POST form and backend/src/document-action.js. Await the real transaction
  and session/cookie persistence before its 303 redirect. Keep JSON API callers on
  their existing response contract. Failed validation must render the source-required
  error/recovery UI, not redirect as success; never echo passwords or put them in URLs.
- For session bootstrap use `useSession` from `frontend/src/ui`, with stable read/signIn/signOut API adapters. Put one instance in the shared provider; route initial reads, explicit authentication, logout and refresh through it. See `frontend/src/state/README.md`. For other replaceable async data use `useAsyncResource`; await the result and navigate/report success only for status `applied`, handle `error`, and ignore `superseded`. Do not mix protected state with direct stale setState callbacks. Serialize order-sensitive server writes; the hook guards local state only. Add delayed old-success/old-error versus new-write tests against the actual provider and navigation handlers. If an adapter's abort listener starts new work, test that newer run/publication retains ownership; a superseded call can skip its adapter entirely. Merely importing a hook does not verify integration. Use equivalent tested logic only when requirements need different ordering.
- For a card that opens an editor, use `RecordCard name={record.name} onOpen={...}`:
  its title is a native button, and passive card space opens the same editor.
  Put summary/body in children and secondary controls in its `actions` prop so
  they occupy a separate area. Do not duplicate its title. Secondary controls do not open
  the editor. Test title Enter/Space, article whitespace, and secondary operations.
- For selectable records, `RecordEntry name={record.name} to={path}` from
  `./competition` supplies a heading and one route button. Use this default when
  YAML permits either link or button; pass `role="link"` for an explicit link.
  RecordEntry and RouteAction load a fresh document by default so destination data
  comes from that route's authorized page loader. For hand-written entity links use
  Link reloadDocument or a native anchor. Local editors, menus and selection stay
  in place; use navigation="client" only for a deliberate, tested SPA interaction.
  This is an editable compatibility preference, not a reason to mislabel arbitrary
  links or turn an account nickname into a heading.
- Actions use real buttons. For an operation that opens another route, the optional
  `frontend/src/competition/RouteAction` export (import from `./competition`) uses
  a native button plus document navigation. Ordinary navigation and explicitly named
  links use real links. For functional record navigation with no specified role, use RouteAction. Do not add hidden aliases or extra controls for a locator.
- Use competition Sidebar for icon-only navigation. Supply name/toggleName/items from
  this input; each item has name/to/icon and optional role="link". All entries stay
  accessible when collapsed, including dynamically loaded labels or groups. Its
  default container is complementary; use role="navigation" when specified.
  Handwritten sidebars must satisfy the same expanded/collapsed entry invariant.
- TextField labels its input; NamedForm and Dialog accept `name`; RecordCard uses
  the current record's identity as its accessible name. Put operation controls
  inside the intended container. Use Dialog only for a real modal, not an inline
  editor. Supply close/save/cancel controls from the current requirement and update
  its `open` state in `onClose`. Native dialog manages modality and focus.
- Disclosure's default is collapsed; use defaultOpen when the YAML specifies an
  initially open section. ActionMenu supports openOnHover explicitly; hover followed
  by click must keep actions accessible. AsyncCheckbox updates immediately, prevents
  duplicate pending requests, and rolls back on failure: provide persisted checked,
  onCommit and visible error handling via onError. Never let slow network refresh
  undo a click before the request completes.
- Use Menu only for an actual menu: it owns menuitem buttons and keyboard focus
  (arrows/Home/End/Escape/Tab). ActionMenu remains a popover of normal action buttons.
  Do not place role=menuitem inside an ordinary list with no menu parent. ToggleGroup
  keeps option names stable and exposes aria-pressed; use current-YAML names and
  values, not a single ambiguous label that silently changes with state.
- Controller checkpoints inspect source after each module without reinstalling or
  rebuilding. Fix their findings during the next existing turn. Final independent
  checks run after dependencies are installed. Dynamic names/unvisited states are
  not certified by static success; cover them with real browser interactions.
- Verify each cross-module entry through its visible controls from the required
  starting state before checking the leaf feature. A direct internal goto or API
  fixture does not prove that journey. Check cold entry, immediate next action,
  write then revisit/refresh, and failed-operation recovery. Use current YAML for
  records, identity, roles and scope; do not invent alternate buttons or aliases.

## Initial state comes from this input

- Read every ancestor description and original GIVEN/WHEN/THEN. Explicit Seed
  declarations apply even inside scenario steps; the module's deduplicated
  seed_contracts preserves their text, owners and case IDs. Provision the stated
  objects/relations for those scenarios; keep different owner/name pairs distinct.
  An ordinary GIVEN remains a scenario precondition, not a global seed override.
  If an ancestor explicitly states that GIVEN records are pre-provisioned or
  available at initial startup, provision those records in the application itself,
  preserving their ownership, permissions and scenario isolation. A test setup
  that creates missing records would hide a broken entry and is not evidence.
  Actions
  and THEN results are not initial values. Follow inherited source rules; record
  conflicts instead of silently choosing one shared state for incompatible cases.
  The compact index supplies fact_ids, constraint_ids, scenario_precondition_ids,
  scenario_seed_ids and unresolved source IDs. Query the frozen module's initial
  context for provenance; do not repeatedly dump all of acceptance.json.
  These are a conservative lexical aid, not a replacement for the original text.
- Initialize an empty SQLite database at ARC_DB_FILE / DATABASE_FILE before health
  becomes ready. Verify original seed requirements with cold-start API/browser tests.
- Preserve owner/child relations, status, counts, permissions and default visibility.
  Scoped absence (an item is absent from ONE account's list) is not global absence.
  Unsupported claims remain partial coverage; add focused cold-start API/browser
  assertions derived from that original source. Do not claim a string scan proves
  relationships or edit frozen facts to silence missing data.
- For creation/cancellation, use a new record identity distinct from the required
  existing one. Verify cancellation preserves the old record. Isolate test databases
  so suites cannot overwrite the running application's initial state.

## Names, containers and journeys

- Preserve explicit names including whitespace/punctuation; comparisons ignore
  case only. Avoid action suffixes, synonyms, translations, icon text or ellipsis
  that change a required accessible name. The code auditor compares current YAML
  and controller-approved visual text; no historic test fixture is an oracle.
- If a name/role is reused with different meanings, associate the control with its
  owning requirement via `data-req-id` and keep correct container scope. A string
  occurring elsewhere does not prove that it names this control.
- `.haiknow/ui-name-review.json` starts as `[]`. Only for an actually unnamed control,
  add one object with file/name/kind="requirement_unnamed"/req_id/source_quote/reason.
  Quote the relevant original requirement verbatim and explain the naming choice.
  This is an unverified assumption, never permission to rename an explicit label.
- In the existing progress.md, briefly map each original journey to GIVEN data,
  scope/role/name, ordered actions, result/persistence, and actual test files. Keep
  explicit requirements distinct from unspecified UI choices; no extra model phase.
- Write expectations from original scenarios before choosing implementation details.
  Playwright role/label locators need exact:true or fully anchored literal regex;
  Testing Library string role names are already exact. Resolve one usable target
  within its intended named form/dialog/record. Do not replace a failing locator
  with global text, .first(), broad substring or test ID as the sole evidence.
- When the source permits either link or button navigation, accept either role
  but require exactly one match across both within the stated scope. Form actions
  remain buttons. Assert named status/alert landmarks as roles with their visible
  text, not just a matching string somewhere on the page. Check listitem text and
  reading order, including unwanted entries; a successful click alone is not the
  stated result. Native and custom selectors must both expose the required named
  combobox, option, and selected-value behavior.
- Start each journey from its stated GIVEN and inherited source rules. Do not
  add sign-in, permissions, setup actions or stronger initial states merely because
  the application supports them. Authentication existing in another module does
  not make every operation authenticated. Preserve all source-required access rules.
  Test helpers must not silently supply extra preconditions that mask a failure;
  reuse login only for journeys whose source requires that session.
- Verify shared entry/navigation from a fresh browser. Follow the original actions (hover/open/select/save/
  close/cancel/undo/refresh) without API shortcuts for the action being tested.
  Avoid extra confirmations or assuming close saves. Check intermediate expanded,
  selected and checked state, blur/submit timing, negative cases and persistence.
- Include delayed-response checks of shared entry and state changes. Await the real
  record/container with retrying assertions; never branch on one immediate isVisible
  while data loads. No fixed sleeps, fabricated rows or disabled pagination.
- Track coverage by original case ID, not the number of generated tests. Passing
  self-written tests does not establish an official score or exhaustive correctness.
- The frozen module change index lists `required_case_ids` for current scenarios
  that are new, changed or cannot be reliably matched to prior source. Cover each
  in the normal `backend/test-e2e` suite using native Playwright metadata:
  `test('journey', {annotation: {type: 'source-case', description: 'CURRENT_CASE_ID'}}, async ({page}) => {...})`.
  Copy the current ID from the index; do not invent IDs or reuse a historical
  position after reordering. Each test may label one case; several tests may cover
  the same case. Keep unchanged regressions and retained-data journeys running.
  Labels are not proof: implement the source actions and assertions before adding
  the annotation. Do not use test.fail(), skip or only to claim coverage. The
  controller checks actual executed results, including failures expected by a test.
- Check every THEN at its stated observation point and container. A redirect
  requires the destination page/URL, not merely a shared navigation link. A
  required positive state is not proved by absence of a different state. Preserve
  negative checks on every specified object after reload as well as before it.
  Do not insert Escape, refresh, closing/reopening, or identity changes just to
  make a blocked WHEN sequence pass; fix the product interaction when the source
  sequence cannot continue. Respect explicit source setup actions where present.
- Changes to shared permissions/state must reach API responses, document/bootstrap
  loaders, cached client data and visible controls. Use the same actor/object to
  compare direct navigation, reload and in-app navigation. Test loss of permission
  with an actor who previously had it, not only one who never had permission.

## Cost control

- After three recognized validation commands with unchanged source, the transport
  requests an end-of-turn summary without tools. This does not certify a module;
  independent full controller checks and bounded repair still follow. Prefer one
  targeted test run per actual change. Do not repeat unchanged suites, create CPU
  pressure, or put retries in a shell loop; diagnose failures and edit first.

## Different-domain examples (never default product data)

Suppose a future YAML says: click the "Authorize dispatch" button in the
"Commission equipment" form. The form and button must keep those exact names;
"Authorize dispatch now" is a mismatch. A route change does not turn the operation
into a link. If it explicitly says the "Inspection history" link, use a link.

Suppose a future source pre-provisions a retired machine "Kiln N8" owned by one
operator for a recovery scenario. Verify its owner and retired state through the
application before the recovery action; do not create it inside the test and call
that a cold-start pass. If GIVEN only describes an ordinary setup and no inherited
rule requires pre-provisioning, follow that setup instead. Conflicting source states
need an explicit interpretation, not an automatic shared-state override.
Verify each state in the actual application. A log line or unrelated record containing the same string is insufficient.
These nouns and values are examples, not an entity whitelist.

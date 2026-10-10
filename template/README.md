# Web Template

This template serves one web application on two HTTP listeners. The frontend is built with Vite and React, and the backend is an Express server that serves both `/api/*` routes and the compiled frontend from `frontend/dist` on each listener.

## Project Layout

- `frontend/`: React, Vite, Tailwind, Vitest, and frontend tests.
- `backend/`: Express, SQLite runtime helpers, backend Vitest tests, and Playwright E2E tests.
- `backend/src/app.js`: Express app, `/api/health`, static frontend hosting, and SPA fallback.
- `backend/src/index.js`: Backend process entrypoint.
- `backend/src/page-data.js`: Current-route data adapter; reuse authorized API reads.
- `backend/src/page-bootstrap.js` and `frontend/page-data-plugin.js`: Real initial
  data in built/development HTML, with request identity and safe JSON serialization.
- `frontend/src/state/route-data.ts`: Shared in-memory snapshots, write publication,
  stale-read invalidation and explicit session reset; usage in state/README.md.

## Prerequisites

- Node.js and npm.
- A shell environment that can install npm dependencies in both `frontend` and `backend`.

## Install Dependencies

Install each package independently:

```bash
cd backend
npm install

cd ../frontend
npm install
```

## Build and Run

The production-style runtime is backend-led. Build the frontend first, then start the backend:

```bash
cd frontend
npm run build

cd ../backend
npm run start
```

The backend listens on `PORT` (default 3000) and 3301, because platform readiness and Web acceptance tests can use different ports. `SPEC_BASE_PORTS` overrides the complete comma-separated list; duplicate ports are used only once. Each listener serves the compiled frontend and API routes from the same origin. Internal validation uses two isolated temporary ports and runs browser journeys against the second one.

Health check:

```bash
curl http://127.0.0.1:<port>/api/health
```

## Development Commands

Backend development server:

```bash
cd backend
npm run dev
```

Frontend development server:

```bash
cd frontend
npm run dev
```

The Vite dev server proxies `/api` to the configured backend port. It also reads
initial page data from that backend before serving HTML; start both servers.
`ARC_WEB_PORT` selects the backend for Vite (default 3000). Production still uses
the built frontend served by Express, not standalone `vite preview`.

## Tests

During agent development, use the managed entry from the project root:

```bash
python3 scripts/check.py frontend backend e2e
python3 scripts/check.py backend --file tests/a-specific-feature.test.js
```

The simple workflow installs package dependencies and a matching Chromium before
coding. The entry builds the frontend for E2E, starts its own non-TTY backend with
two temporary ports and an isolated project-local database, checks readiness and
stops only its own process group, including on failure or cancellation. It reports
real positive test counts; skipped, flaky, empty or missing reports cannot pass.
If package/lock files change, run `npm ci` in that package before checking again.

Successful development evidence is reused only for the same source, dependencies,
environment and test selection, with intact evidence and build artifacts.
The controller-owned `.haiknow/progress.md` journal alone does not invalidate
success; other knowledge files, Markdown and database fixtures still do.
Use
`--fresh` to investigate a concrete new concern; a fresh failure invalidates the
older success. Evidence is in `.arc/local-check`; `cached: true` means reuse, not a
new execution. Final controller acceptance always uses a separate cold copy and
does not trust this cache. Per-command and startup timeouts can be set with
`--timeout` and `--startup-timeout`; they do not change the agent's total budget.

The lower-level commands below remain available for diagnosis. Direct Playwright
commands require an existing server; the managed entry owns that setup normally.

Frontend unit/integration tests:

```bash
cd frontend
npm run test
```

Backend unit/integration tests:

```bash
cd backend
npm run test
```

Backend E2E tests use Playwright and live under `backend/test-e2e`:

```bash
cd backend
npm run test:e2e
```

Run backend Vitest tests followed by E2E tests:

```bash
cd backend
npm run test:all
```

## Database Notes

- Runtime database helpers live under `backend/src/database`.
- The default database file is `database.db`, unless `ARC_DB_FILE` or `DATABASE_FILE` is set.
- Test helpers create isolated SQLite files under `.arc-test-db`.
- `npm run db:seed` runs the template seed entrypoint.
- `npm run db:prepare:e2e` prepares an isolated E2E database.

Use `db_runtime.js` for request-time `run/get/all/exec` and `withTransaction`.
The shared connection queues complete transactions together with ordinary
operations; a separate request cannot read uncommitted data or have its write
rolled back by another request. Await database calls inside the transaction
callback. Its `tx` methods and public runtime helpers join the same transaction;
use the current transaction instead of nesting `withTransaction`. A retained `tx`
handle rejects calls after the callback completes. Failures roll back and release
the queue for subsequent requests.

Raw `getDb()` access is for initialization/lifecycle code, not request handlers.
Raw connections, hand-written BEGIN/COMMIT and database resets bypass the runtime
queue; close/reset/switch the database only after outstanding work has completed.
Keep transactions short and do not wait for unrelated network work inside one.

## Async resource integration

The reusable [UI contracts](frontend/src/ui/README.md) cover native labeled fields,
forms, record/dialog scope, stable page regions and explicit async write outcomes.
The blank application contains no example business records or routes. Compose the
components with names and data from the current requirements.

`frontend/src/state` contains `useAsyncResource` and its `useSession` bootstrap
consumer, also exported by `frontend/src/ui`. See [the state contract](frontend/src/state/README.md)
for adapters, applied/error/superseded results and server-side ordering limits.
The blank App does not mandate authentication; connect the shared session consumer
only when the current application needs it.

## Requirement source queries

The controller creates frozen `.haiknow/modules/index.json` and module views.
Query only the facts needed for the current module instead of dumping the complete
acceptance inventory:

```bash
python3 scripts/contract_context.py query --module CURRENT_REQ --kind requirements
python3 scripts/contract_context.py query --module CURRENT_REQ --kind initial
```

`requirements`/`all` queries retain ancestor and dependency context. Complete input
files remain authoritative. Use `summary` for module identity and input locations,
`initial` for source-owned initial facts, and `all` for the complete module view.
Queries only read frozen source context; implement the original requirements and
verify them with application tests.

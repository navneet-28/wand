# wand: a small app platform for coding agents

wand lets a coding agent **create, run, update and inspect** small data-backed apps through one
documented app format and a JSON-speaking CLI.

An app is one Python file that declares typed collections plus named **read** and **write**
handlers. One app-agnostic runtime validates it, stores its data in SQLite, serves it on localhost,
hot-reloads same-schema updates, rejects bad definitions while the last valid version keeps
serving, and records everything for inspection.

- **For agents building an app:** read [AGENT_GUIDE.md](AGENT_GUIDE.md).
- **For the reasoning behind every choice:** read [DECISIONS.md](DECISIONS.md) (D-001 … D-031).

---

## Setup

Requires Python ≥ 3.10. No runtime dependencies beyond the standard library; pytest for tests.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'      # installs the `wand` command + pytest
source .venv/bin/activate              # optional: puts `wand` on PATH
```
Without installing, `python -m wand ...` works from the repo root.

## Run the demo

```bash
./demo.sh          # interactive: Enter runs the next step, q quits
./demo.sh --auto   # run all steps without pausing
```
It uses a throwaway `WAND_HOME` and copies of `apps/`, checks every step, and stops with `FAIL` if any
claim doesn't hold. Whenever it exits, it stops the server and deletes its temp files. It shows:
1. both example apps deployed on the same runtime
2. Notes over HTTP: callers can't see or change each other's notes
3. **restart persistence**: stop and start the server; data and the active version are still there
4. **same-schema code update**: add a handler, deploy, and the running server serves v2 without a restart
5. **rejected definitions**: a schema change and a syntax error are both rejected; v2 keeps serving with its data
6. `inspect`: identity, active version, handlers, why the latest deploy was rejected, data, logs
7. Feedback: a repeated vote by the same caller doesn't increase the count
8. data separation: separate databases and handlers per app

Env: `PORT` (default 8765), `PYTHON` (default `.venv/bin/python`).

## Run the tests

```bash
.venv/bin/python -m pytest          # runs in under a second
```

| Required behaviour | Where it's tested |
|---|---|
| Validation (definitions, records, queries) | `test_contract.py`, `test_store.py` |
| Caller scoping | `test_runtime.py`, `test_example_apps.py` |
| Read/write restrictions (incl. a caught violation) | `test_store.py`, `test_runtime.py` |
| Data separation | `test_store.py`, `test_runtime.py`, `test_example_apps.py` |
| Persistence (restart, same-schema update) | `test_store.py`, `test_versions.py`, `test_runtime.py` |
| Rejected definitions keep the last valid version + data | `test_versions.py`, `test_runtime.py`, `test_cli.py` |
| CLI JSON output and HTTP API | `test_cli.py`, `test_server.py` |

## Quick tour

```bash
wand new todo                      # apps/todo/app.py: a working skeleton
wand deploy apps/todo              # {"ok": true, "action": "created", "version": 1, ...}
wand call todo add_item --caller alice --args '{"text": "milk"}'
wand inspect todo                  # versions, handlers, data, recent calls/errors
wand run todo --port 8000          # curl -X POST localhost:8000/call/list_items -H "X-Caller: alice"
```

---

## How it works

```
CLI (wand)  new · deploy · run · call · inspect      → JSON on stdout, exit 0/1
  │
  ├─ versions.py   deploy: load + validate → compare schema → ACTIVE, or REJECTED (+ reason)
  ├─ runtime.py    per call: check request → hot reload if the active version changed →
  │                Context(caller, db) → run handler (writes in one transaction) → log
  ├─ server.py     localhost HTTP (POST /call/<handler>, X-Caller header), single-threaded
  │
  ├─ contract.py   App, @app.read / @app.write, load_app (exec stored source), schema_diff
  └─ store.py      one SQLite file per app: records · versions · logs; DataHandle (ctx.db)
```

**The app contract.** `App(name, collections={collection: {field: type}})` with types `string`,
`integer`, `boolean`; every field required; wand adds an integer `id`. Handlers are `fn(ctx, args)`
with `ctx.caller` (asserted identity string) and `ctx.db` (`get`, `find`, `insert`, `update`).

**Deploy lifecycle.** `wand deploy apps/notes` reads `app.py` (the folder name is the identity),
executes it in a fresh module, validates the contract, and diffs the schema against the active
version. A failure is stored as a `rejected` version with its error. On success, one transaction
marks the previous version `superseded` and the new one `active`. A unique index guarantees one
active version. The runtime only ever loads the stored active source, so a broken file on disk can't go live,
and a restart always falls back to the last valid version.

**Enforcement.** Records are validated against the schema on every insert and update. Read handlers get
a read-only handle; a write attempt raises `WRITE_IN_READ_HANDLER` *and* is recorded, so the call fails
even if the handler swallows the exception. Each app has its own database file, so a handle can't reach
another app's data. Caller scoping is the app's job (the platform passes the identity), and the example
apps return the same `NOT_FOUND` for "not yours" and "doesn't exist".

---

## Design decisions (and why)

The full log with every alternative is in [DECISIONS.md](DECISIONS.md). The ones that shape the system:

| Decision | Why |
|---|---|
| **One `app.py` per app**, schema + decorated handlers (D-003) | One file to write and deploy, so a separate manifest and the code can't drift apart. We must execute the code to validate it anyway. |
| **Folder name = app identity**, `name=` must match (D-023) | A deploy knows its app *before* running any code, so even syntax-error rejections are recorded and inspectable. A typo'd name is rejected instead of silently creating a second app. |
| **SQLite, one file per app, one generic JSON `records` table** (D-004, D-005, D-006) | Separation by construction, not by remembering `WHERE app = ?`. No DDL generation. Records, versions and logs live in one place, so activation is atomic. |
| **Full-source snapshots for every deploy, loaded with `exec`** (D-002, D-019) | What was validated is byte-for-byte what runs. Rollback, fallback and "why was it rejected" fall out of one table. An app is ~10–50 KB, so storage isn't a concern. |
| **Any schema difference is rejected** (D-017) | One rule that's easy to state and test. The error carries an exact diff, so an agent can revert precisely. |
| **Errors: one shape, stable codes, `hint`, `source: platform\|app`, all problems at once** (D-013, D-019) | Agents branch on `code`, act on `hint`/`details`, and fix everything in one round trip. `source` says whether to fix the request or the app. |
| **`get()` returns `None`; others' records are `NOT_FOUND`** (D-014, D-021) | If the platform raised `NOT_FOUND` for missing records and the app raised its own for others', the differing `source` would leak which records exist. |
| **Each write call is one transaction** (D-016) | "Error ⇒ nothing was written" is a simple guarantee for agents, and SQLite makes it nearly free. |
| **Hot reload by checking the active version number per call** (D-026) | One indexed query. It can't load unvalidated code (unlike a file watcher), and needs no IPC between CLI and server. |
| **Single-threaded HTTP server; `wand call` runs in-process** (D-028, D-029) | Sequential writes by construction, which the idempotent-vote check relies on. Agents can call handlers without managing a server. |
| **Standard library only** (D-009) | Setup is "Python + pytest". Nothing is hidden behind a framework when tracing the code. |

### Rejected alternatives (selection)
- **Manifest file (`app.json`) + handlers file:** two sources of truth that can disagree.
- **Real SQL columns per field:** schema → DDL generation for no benefit at this scale.
- **One shared database with an `app` column:** one missing filter leaks data across apps.
- **Diff-based version storage:** a restore replays a chain, and one bad link breaks every later version.
- **Allowing additive schema changes / migrations:** each "safe change" rule needs its own reasoning and tests; out of scope.
- **Platform-level unique constraints for votes:** a bigger contract; sequential writes make the handler check safe (see limitation).
- **FastAPI / Typer / Pydantic:** more setup and less transparency for a one-request-at-a-time localhost server.
- **A file watcher for hot reload:** it would run unvalidated code straight from disk.
- **Returning `FORBIDDEN` for other callers' records:** confirms the record exists.

---

## Known limitation

**The schema is frozen after the first successful deploy.** Any change to `collections` (even
adding a new collection or field) is rejected with `SCHEMA_CHANGED`. The only way to evolve the data model is
to create a new app (new folder and name), which starts with empty data. That's acceptable for the
assignment ("schema changes may be rejected clearly"), but it's the first thing a real user would hit.
The next step would be allowing purely additive changes (new collections; new fields with a default),
which never invalidate stored records.

Other gaps, by design or deferred:
- **Uniqueness is the handler's job** (D-012). The vote check is safe only because writes are sequential.
  Concurrent writers would need a platform-enforced unique constraint (a SQLite unique index).
- **No delete, no ranges/sorting/limits** in `find` (D-022). `inspect` returns all data (fine for small apps).
- **Handler `args` aren't schema-validated** (D-010). Bad args surface when the record is validated, or as `HANDLER_ERROR`.
- **Identity is asserted, not authenticated.** Read-only handles and separation are API properties.
  Trusted app code could open SQLite directly (per the assignment's constraints).
- **One app per server process**, and version history grows without bound (D-002).

## Fresh-agent dry run

To test the docs, a fresh Claude Code session in a clean copy of the repo (no prior context) was asked to
build a **polls** app (2–4 options per poll, one vote per person that can be changed, only the creator
can close, no votes on closed polls) using only README.md and AGENT_GUIDE.md, then ship a same-schema
update and inspect it.

**Result:** it set up the environment from the README, built and demonstrated every rule with four callers,
shipped v2, and **never needed to read wand's source or tests**. Its report found real gaps, all since fixed:

| Agent's finding | Fix |
|---|---|
| A missing `--caller` gave `USAGE_ERROR` (unlogged), while HTTP gives `CALLER_REQUIRED` | The CLI now leaves the caller check to the runtime, so it behaves the same everywhere and is logged (D-033) |
| Early errors (`CALLER_REQUIRED`, `ARGS_INVALID`) reported `"version": null` | Responses and logs always carry the active version (D-032) |
| The guide suggested an app code `INVALID_ARGS`, easily confused with wand's `ARGS_INVALID` | The guide now says to use domain-named codes and never imitate platform codes |
| Ids are shared across collections (undocumented) | Documented |
| No pattern for parent → children (poll → options) or change-your-vote | Both added to the guide, plus "owner-only action on shared data" |
| A stale test count in the README | Removed the hard-coded number |

## Time spent

About **5 hours** in total, within the 6-hour limit, over two sessions:

| Session | Time | Work |
|---|---|---|
| 1 | ~3 h | Read the assignment, explored Lakebed, wrote the plan and made the core design decisions ([DECISIONS.md](DECISIONS.md)). Then built the app contract, schema validation, structured errors, the store and versioning/deploy, with tests. |
| 2 | ~2 h | Runtime, HTTP server and CLI, the example apps, README, AGENT_GUIDE.md and demo.sh, the fresh-agent dry run and the fixes it led to, and setting up the git repository. |

## Where I stopped / what I'd do next

Everything in scope is implemented and demonstrated. Next, in order: additive schema changes; a
platform-level `unique` constraint so idempotency doesn't depend on sequential writes; args schemas per
handler; multi-app hosting (a dict of runtimes keyed by app name).

## Repository layout

```
wand/            platform: errors, contract, store, versions, runtime, server, cli
apps/notes/      example: private notes (caller scoping)
apps/feedback/   example: shared posts + idempotent votes
tests/           pytest suite
demo.sh          end-to-end demo with checks
AGENT_GUIDE.md   docs for a coding agent building an app
DECISIONS.md     decision log: every choice, its alternatives, and why
```

# Decision Log

Each entry records what we chose, what else we considered, and why.
This feeds the README's "Design decisions" and "Rejected alternatives" sections.

Format: **Context** → **Options** → **Decision** → **Why** → **Consequences**

---

## D-001: Implementation language: Python

- **Context:** The assignment allows Python or TypeScript.
- **Options:** Python · TypeScript
- **Decision:** Python.
- **Why:** It's the language I'm most fluent in and can defend line by line in the follow-up.
  The stdlib also covers most of what we need (`sqlite3`, `json`, `http.server`, `argparse`,
  `importlib`).
- **Consequences:** App code is plain Python modules loaded with `importlib`. Tests use pytest.

---

## D-002: Version storage: full snapshots, no hashing, no pruning

- **Context:** Each deploy creates a version. We need the active version's code to survive
  restarts and rejected deploys, and `inspect` must show the active version and rejection reasons.
- **Options:**
  1. Full snapshot of `app.py` per version **(chosen)**
  2. Full snapshot + SHA-256 content hash (dedupe identical redeploys, show the hash in inspect)
  3. Retention policy (keep active + previous valid + last N rejected; prune the rest)
  4. Compression (zlib)
  5. Store diffs between versions
- **Decision:** Full snapshot per version. No hash, no pruning, no compression, no diffs.
- **Why:**
  - An app is one small file (~10–50 KB), so 1,000 deploys ≈ 50 MB. App data grows faster than code history.
  - Self-contained snapshots make restore, fallback and inspection trivial. That's the same tradeoff
    Git (full objects), AWS Lambda (a full package per version) and Heroku (a slug per release) make.
  - Hashing and pruning add code and tests for a problem we don't have. Extra features earn no credit.
  - Diffs were rejected outright: restoring means replaying a chain, and one bad link breaks every later version.
- **Consequences / known limitation:** Version history grows without bound. If it mattered, the next
  steps would be (in order) a retention policy, then content-hash dedupe, then compression.

---

## D-003: App format: a single `app.py`

- **Context:** We need a format an agent can write and the platform can validate.
- **Options:**
  1. One `app.py` declaring the schema (`App(...)`) and handlers (`@app.read` / `@app.write`) **(chosen)**
  2. Manifest (`app.json` / `app.yaml`) for schema + handler list, plus a separate `handlers.py`
- **Decision:** One `app.py`.
- **Why:** One file to write, read and deploy, so an agent can't let the manifest and the code drift apart
  (e.g. a handler listed in the manifest but missing in the code). We must import the code to
  validate it anyway (syntax errors, missing functions), so a separate manifest doesn't save an
  import step.
- **Consequences:** Validation = import the module, find the `App` object, check its schema and
  handlers. The schema is still declarative data inside the file, so we can compare schemas between versions.

---

## D-004: Storage model: SQLite with one generic records table

- **Context:** App data must be durable and validated against the declared fields.
- **Options:**
  1. SQLite, one generic table `records(collection, id, data JSON)`; schema enforced in Python **(chosen)**
  2. SQLite with a real table and columns per collection (generated DDL)
  3. Plain JSON files
- **Decision:** Option 1.
- **Why:** The assignment doesn't require real columns. A generic table means no DDL generation and
  no table-creation code, and the same storage code serves every app. SQLite still gives
  durability and atomic transactions, which JSON files don't without extra work.
- **Consequences:** Filtering uses `json_extract`. No DB-level type checks or indexes per field;
  our validator is the only gatekeeper. Fine for small apps.

---

## D-005: Data separation: one SQLite file per app

- **Options:** 1. one file per app **(chosen)** · 2. one shared DB with an `app` column on every row
- **Why:** Separation by construction. A data handle opened on `notes.db` cannot reach feedback rows,
  whatever the query. With a shared DB, every query must remember `WHERE app = ?`, and one forgotten
  filter leaks data.
- **Consequences:** Deleting or resetting an app = deleting one file. Cross-app queries are impossible (not needed).

---

## D-006: Versions and logs live in the app's own SQLite file

- **Options:** 1. tables inside the app's DB **(chosen)** · 2. a folder of version files + `state.json`
- **Why:** One place per app. Storing a version and switching the active pointer happen in one
  SQLite transaction, so a crash can't leave a half-activated version, which a folder +
  `state.json` approach could.
- **Consequences:** Each app's DB has three tables: `records`, `versions`, `logs`.

---

## D-007: Updates are submitted with `wand deploy <dir>`

- **Options:** 1. CLI deploy writes the new version to the store; the running server hot-reloads **(chosen)** ·
  2. HTTP upload to the running server
- **Why:** Works whether or not the server is running. Validation and activation live in one code
  path (the CLI) instead of being split across the server. The server only has to notice that
  the active version changed (one cheap query per request).
- **Consequences:** The server checks the active version id before each request and reloads if it changed.

---

## D-008: Field rules: three types, all fields required

- **Options:** 1. `string`, `integer`, `boolean`; every field required **(chosen)** · 2. optional fields, defaults, more types (float, list, datetime…)
- **Why:** The assignment asks for a small, documented set. Three types cover both example apps.
  A smaller surface means fewer validation rules to explain and test.
- **Consequences:** Every record must have every declared field and no extra fields. The platform
  adds `id` itself. `bool` is checked before `int` (Python treats `True` as an `int`).
  Optional fields are deferred until an app needs them.

---

## D-009: Libraries: stdlib only, plus pytest

- **Options:** 1. stdlib (`sqlite3`, `http.server`, `argparse`, `importlib`, `json`) + pytest **(chosen)** ·
  2. FastAPI + Typer + Pydantic
- **Why:** Setup is just "Python 3.x + pytest", with nothing for reviewers or agents to install.
  Nothing is hidden behind framework magic, which matters when tracing the code live in the interview.
  The traffic is one-request-at-a-time on localhost, so `http.server` is enough.
- **Consequences:** We write a little more glue (JSON parsing, routing for a couple of endpoints).

---

## D-010: Handler arguments: free-form JSON `args`

- **Options:** 1. `args` is a JSON object; the handler reads what it needs **(chosen)** · 2. per-handler parameter schemas validated by the platform
- **Why:** Keeps the contract small. Data written to storage is still validated against the schema,
  so bad input can't corrupt stored data.
- **Consequences:** A missing arg surfaces as a handler error. We'll convert `KeyError` and similar
  exceptions into a structured `HANDLER_ERROR` naming the handler, so agents can still act on it.
  Declared parameter schemas are deferred.

---

## D-011: Name: `wand`

- **Decision:** The platform, Python package and CLI are called `wand` ("wave it, and the app exists").
- **Note:** PyPI already has an unrelated `Wand` package (ImageMagick bindings). We don't publish to PyPI,
  and setup uses a virtualenv, so there's no collision in practice. If we ever packaged it, the
  distribution name would need to change (e.g. `wand-platform`).

---

## D-012: Idempotent votes: enforced by the handler, count derived from vote records

- **Context:** Feedback app: "Repeating a vote by the same declared identity on the same post must
  not increase its count again." Idempotent = doing it twice has the same effect as doing it once.
- **Sub-decision (data model):** Store one record per vote (`votes: {post_id, voter}`) and compute the
  count as the number of vote records for a post. Rejected: a `vote_count` counter field on the post,
  which can drift from reality; a list of voters can't.
- **Options:**
  1. The `vote` handler checks `find("votes", post_id=..., voter=caller)` before inserting **(chosen)**
  2. A schema-level uniqueness rule (e.g. `unique={"votes": [("post_id", "voter")]}`) enforced by the platform
- **Decision:** Option 1. A repeated vote returns **success** with `already_voted: true`, not an error.
- **Why:**
  - Keeps the app contract small. Uniqueness isn't required by the assignment, and adding it
    means more contract surface to validate, document and test.
  - Check-then-insert normally has a race (two concurrent requests both see "not voted"), but the
    assignment allows sequential writes, so the race can't happen here.
  - Returning success on a repeat is true idempotency: an agent's retry "just works" and learns
    the vote already existed.
- **Consequences / limitation:** Each app must remember to do the check itself. Under concurrent
  writes this would break. The fix would be option 2 (a platform-enforced unique constraint, e.g. a
  SQLite unique index).

---

## D-013: Apps raise their own structured errors with `AppError`

- **Context:** Handlers need to report expected failures (e.g. "note not found") in a form agents can act on.
- **Options:**
  1. `raise AppError(code, message, hint=None)`; the runtime returns it as a structured error with `"source": "app"` **(chosen)**
  2. Handlers return `{"error": ...}` as a normal result
  3. No app errors; every exception becomes a generic `HANDLER_ERROR`
- **Why:** (2) makes success and failure look the same to the caller (`ok: true` with an error inside), so
  agents have to inspect every result. (3) turns expected outcomes into tracebacks. With (1), all errors share
  one shape, and `source` (`platform` vs `app`) tells the agent whether to fix its app code or its request.
- **Consequences:** App error codes are free-form strings chosen by the app. Unexpected exceptions still become
  `HANDLER_ERROR` with the exception type, message and traceback in `details`.

---

## D-014: Accessing another caller's record returns `NOT_FOUND`, not `FORBIDDEN`

- **Context:** "Notes handlers called with another declared identity must not expose or update those notes."
- **Options:** 1. `NOT_FOUND`, identical to a missing note **(chosen)** · 2. `FORBIDDEN`
- **Why:** `FORBIDDEN` confirms the note exists, which leaks information about another user's data. Returning the
  same answer for "not yours" and "doesn't exist" exposes nothing. This is the same convention GitHub uses
  for private repos (404, not 403).
- **Consequences:** This is app code, not platform code. AGENT_GUIDE.md documents it as the scoping pattern.

---

## D-015: A write attempt in a read handler fails the call even if the handler catches it

- **Context:** A read handler could `try: ctx.db.insert(...) except: pass` and hide the violation.
- **Options:**
  1. The read-only handle raises *and* records the violation; the runtime fails the call regardless **(chosen)**
  2. Only raise; whatever the handler does with the exception is its business
- **Why:** The rule is "a write from a read handler must fail with a structured error". With (2), the write is
  blocked but the caller sees `ok: true`, so the error never reaches anyone. (1) guarantees the
  structured error surfaces. It's about three lines of code.
- **Consequences:** Not a security boundary. App code is trusted (it could reach SQLite directly);
  this is an API property, as the assignment says.

---

## D-016: Each write-handler call runs in one transaction (all-or-nothing)

- **Context:** A write handler might insert a record and then crash.
- **Options:** 1. one SQLite transaction per write call; roll back on any error **(chosen)** ·
  2. autocommit each operation; partial writes stay
- **Why:** With (2), a crash leaves half-done data (e.g. a vote without its post update), which corrupts app state. SQLite
  transactions make (1) nearly free. The assignment rules out *recovery* from arbitrary failures; this
  is just not leaving half-finished writes behind.
- **Consequences:** An error result from a write handler means nothing was written. Easy to test and to document.

---

## D-017: Any schema difference rejects the deploy

- **Context:** "Schema changes may be rejected clearly."
- **Options:**
  1. Reject any difference (field or collection added, removed, or retyped) **(chosen)**
  2. Allow additive changes (new collection) and reject the rest
  3. Support migrations
- **Why:** One rule that's easy to state, test and explain. (2) is safe in principle, but every
  "is this change safe?" rule needs its own reasoning and tests. (3) is explicitly out of scope.
- **Consequences:** `SCHEMA_CHANGED` includes a precise diff in `details` so an agent can revert exactly
  what it changed. Additive changes are listed as the first thing to relax with more time.

---

## D-018: App identity comes from `name=` in `app.py`; deploy reports created vs updated

> **Superseded by D-023.** Identity from `name=` alone can't record rejections of code that won't load.

- **Context:** A typo like `name="notess"` would silently create a second app instead of updating `notes`.
- **Options:**
  1. Always report `"action": "created"` or `"updated"` in the deploy output **(chosen)**
  2. Require a `--create` flag for the first deploy; deploy without it fails if the app doesn't exist
  3. Identity from the directory name instead of the file
- **Why:** (1) costs nothing and makes the outcome visible in machine-readable output, which an agent can check.
  (2) is safer but adds a step to every first deploy. (3) makes moving or renaming the folder change the app's identity.
- **Consequences:** Documented in AGENT_GUIDE.md: "check `action` after deploy". (2) is noted as a hardening option.

---

## D-019: App code is loaded by `exec`-ing stored source into a fresh module

- **Context:** Deploy must validate code, and the runtime must later run exactly what was validated.
- **Options:**
  1. `exec(compile(source))` into a new `types.ModuleType` each load **(chosen)**
  2. `importlib` import of `apps/<name>/app.py` from disk
- **Why:** Versions live as source text in SQLite (D-002, D-006). Executing that text means the validated
  bytes and the running bytes are identical, and editing the file on disk can't change what's live.
  `importlib` caches modules in `sys.modules`, so an update would need cache eviction to take effect.
- **Consequences:** One loader (`contract.load_app`) for deploy and runtime. App code can't do
  relative imports of sibling files; an app is one file (D-003), so that's fine.
  Validation reports **all** problems at once (`details.problems`) so an agent fixes them in one deploy.

---

## D-020: Record ids are integers assigned by SQLite

- **Options:** 1. `INTEGER PRIMARY KEY AUTOINCREMENT`, unique across the app's collections **(chosen)** ·
  2. random UUID strings · 3. per-collection counters
- **Why:** Short, readable ids (`note 3`) are easier for agents and humans in demos, logs and CLI calls.
  SQLite provides them for free, and AUTOINCREMENT never reuses an id. Per-collection counters would
  need extra bookkeeping. UUIDs hide record counts, but scoping is enforced by handlers anyway, so
  guessable ids aren't a leak.
- **Consequences:** Fields that reference records (e.g. `votes.post_id`) are declared `integer`. The `id`
  type is checked: `get("notes", "1")` is `QUERY_INVALID`.

---

## D-021: `get()` returns `None` for a missing record instead of raising `NOT_FOUND`

- **Context:** D-014 says "not yours" and "doesn't exist" must look identical to the caller.
- **Options:** 1. `get` returns `None`; the handler decides what to raise **(chosen)** · 2. `get` raises platform `NOT_FOUND`
- **Why:** With (2), a missing note gives `NOT_FOUND` with `source: "platform"`, while someone else's note gives the
  app's `NOT_FOUND` with `source: "app"`. The `source` field would reveal which notes exist. With (1)
  the handler handles both cases on one line:
  `if note is None or note["owner"] != ctx.caller: raise AppError("NOT_FOUND", ...)`.
- **Consequences:** Platform `NOT_FOUND` is raised only by `update()` on a missing id; handlers
  should check ownership with `get` first. `find("notes", id=x, owner=ctx.caller)` is an equivalent pattern.

---

## D-022: The data handle API: `get`, `find`, `insert`, `update`, with no delete and equality-only filters

- **Options:** 1. four operations, equality filters, results ordered by id **(chosen)** ·
  2. add `delete`, ranges, sorting, limits · 3. expose raw SQL
- **Why:** Enough for both example apps. Every extra operation is more to validate, document and test.
  Raw SQL would bypass schema validation and read-only enforcement entirely.
- **Consequences:** `find` validates filter fields and value types (`QUERY_INVALID`). `find`'s collection
  parameter is positional-only, so a field may itself be named `collection`. Delete is a known gap.

---

## D-023: The app's folder name is its identity, and `name=` must match it (supersedes D-018)

- **Context:** `inspect` must show why the latest definition was rejected. If `app.py` has a syntax error,
  its `name=` is never executed, so with D-018 we wouldn't know which app to record the rejection under.
- **Options:**
  1. Folder name = identity; `name=` must equal it, otherwise `DEFINITION_INVALID` **(chosen)**
  2. An explicit `wand deploy <dir> --app notes` flag that must match `name=`
  3. Keep D-018: unloadable code is reported to the caller but not recorded
- **Why:** (1) gives every deploy an identity before any code runs, so every rejection is recorded and
  inspectable. It also turns D-018's typo risk (`name="notess"` silently creating a new app) into
  a clear rejection. (2) has the same safety but adds an argument to every deploy, which agents can also get wrong.
  (3) misses the requirement for the most common failure.
- **Consequences:** `wand deploy` takes a folder. Folder names must be valid app names. Renaming the folder means
  a new app. `deploy` still reports `action: created | updated`.

---

## D-024: Every deploy, accepted or rejected, is a numbered version; "latest rejection" means the newest version

- **Options for recording rejections:** 1. a `rejected` row in the same `versions` table **(chosen)** ·
  2. a separate `last_error` field that each deploy overwrites
- **Why:** One table gives a complete, ordered history (`1 active → 2 rejected → 3 active`) with no extra state.
  "Why was the latest definition rejected" = the newest version, if its status is `rejected`. Once a
  later deploy succeeds, the rejection is history, not the current state.
- **Consequences:** Status is `active | superseded | rejected`. A partial unique index makes SQLite itself
  guarantee at most one active version. Activation (old → superseded, new → active) is one transaction.
  Rejection errors carry `rejected_version` and `active_version`, and the hint says what is still live.

---

## D-025: `call()` returns a response envelope that includes the version that served it

- **Options:** 1. always return `{"ok", "version", "result" | "error"}`; never raise for a failed call **(chosen)** ·
  2. return the result and raise exceptions for failures
- **Why:** The server and CLI just serialise the envelope, and every failure is logged in one place.
  `version` tells an agent which code answered, which matters right after a deploy (did my update take effect?).
- **Consequences:** Only setup problems (`APP_NOT_FOUND` when opening an app) raise.

---

## D-026: Hot reload by checking the active version number before every call

- **Options:** 1. one `SELECT number ... WHERE status='active'` per call; reload when it differs **(chosen)** ·
  2. a file watcher on app.py · 3. the deploy command signals the server · 4. no hot reload, restart required
- **Why:** (1) is a single indexed query, works no matter who deployed or how, and can never load an
  unvalidated file, because it reads only the stored active version. (2) would run unvalidated code from disk and
  breaks D-019. (3) needs IPC between CLI and server. (4) makes the update flow clumsy for agents.
- **Consequences:** The loaded `App` is cached per runtime and rebuilt only when the number changes.

---

## D-027: Logs record every call attempt (args included), keeping the most recent 500

- **Options for what to log:** every attempt, including rejected requests (missing caller, unknown handler) **(chosen)** vs. only
  handler executions. **Retention:** keep the last 500 **(chosen)** vs. unbounded.
- **Why:** An agent debugging "why didn't my call work" needs to see the failures that never reached a handler.
  Args are logged because inspection is a trusted developer operation (assignment constraint) and
  args are what you need to reproduce a failure. Results aren't logged (size). Retention makes "recent
  logs" literally recent and bounds growth. It costs one DELETE per call.
- **Consequences:** `HANDLER_ERROR` includes the exception type, the **line in app.py** (frames are named
  `notes/app.py@v3`) and the full traceback, so an agent can go straight to the line.

---

## D-028: The HTTP server is single-threaded

- **Options:** 1. `http.server.HTTPServer`, one request at a time **(chosen)** · 2. `ThreadingHTTPServer` · 3. an async framework
- **Why:** The assignment allows "one app at a time and sequential writes". A single-threaded server makes writes
  sequential *by construction*, which is exactly what the check-then-insert vote logic (D-012) relies on. It also
  means one SQLite connection is never used by two threads at once.
- **Consequences:** Throughput is one request at a time (fine on localhost). The connection is opened with
  `check_same_thread=False` so it can be created on one thread and served from another (as the tests do).
  If this became threaded, uniqueness would move into the store (unique index) and each request would need its own connection.

---

## D-029: `wand call` runs the handler in-process, not through the HTTP server

- **Options:** 1. CLI → `Runtime` directly **(chosen)** · 2. CLI → HTTP → running server
- **Why:** An agent can try a handler right after deploying, without starting and stopping a server, and the
  code path is identical (same `Runtime.call`, same logging). Option 2 makes every CLI call depend on a
  running process and a port.
- **Consequences:** If a server is running at the same time, both processes use the same SQLite file. SQLite
  serialises writers with file locks, and both see new deploys through hot reload.

---

## D-030: HTTP API: `POST /call/<handler>` with an `X-Caller` header and a JSON body of args

- **Options:** caller in a header **(chosen)** vs. in the body or query string. One generic call route **(chosen)** vs. a
  REST route per handler.
- **Why:** The header keeps `args` exactly what the handler receives (no reserved keys mixed in), and makes
  the identity a property of the request, like an auth header would be later. One generic route means the server
  has no app-specific routing (the "reuse one runtime" rule). The body is the same envelope as the CLI. Status codes
  are coarse (200 / 400 / 404 / 500 / 503) because agents should branch on `error.code`, not on status.
- **Consequences:** Plus `GET /health` (app + active version) so scripts can wait for readiness.

---

## D-031: CLI output conventions

- **Decision:** Every command prints exactly one JSON document (pretty-printed) and exits 0 if `ok`, else 1.
  `run` is a stream and prints compact JSON lines (`serving`, `stopped`). argparse errors are JSON (`USAGE_ERROR`),
  and unexpected bugs are JSON (`INTERNAL_ERROR`). `run` fails fast with `NO_ACTIVE_VERSION` rather than serving
  nothing. `wand new` writes a skeleton that deploys and runs as is, and shows the caller-scoping pattern.
  `inspect` returns the full error only for `latest_rejection`; the `versions` list is compact (`error_code` only).
- **Why:** Agents parse stdout and check exit codes, so nothing can be plain text. A deployable skeleton
  gives the agent a known-good starting point. The first deploy succeeds, and every later failure is caused by its own edit.

---

## D-032: Every call response and log entry carries the active version, even for early failures

- **Context:** The fresh-agent dry run noticed `"version": null` on `CALLER_REQUIRED` / `ARGS_INVALID`, because the
  request was checked before the version was looked up.
- **Decision:** Read the active version number at the start of `call()` and `reject()`. `version` means "the version
  that was active when the call arrived", and it's `null` only if there is none.
- **Why:** It's consistent for agents correlating calls with deploys, and costs one indexed query that `current()` would run anyway.

---

## D-033: The CLI doesn't make `--caller` required; the runtime enforces it

- **Context:** The dry run found that a missing `--caller` gave `USAGE_ERROR` (from argparse, unlogged), while HTTP without
  `X-Caller` gives `CALLER_REQUIRED` (logged).
- **Options:** 1. drop `required=True`; the runtime's check applies to both interfaces **(chosen)** · 2. keep both checks
- **Why:** One rule in one place (the same reasoning as the server not checking headers itself). Identical errors on
  both interfaces, and the failed attempt shows up in `inspect`.

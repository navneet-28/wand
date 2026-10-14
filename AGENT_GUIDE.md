# wand: Guide for Coding Agents

wand runs small data-backed apps. You write **one Python file** that declares the app's data
(collections and typed fields) and its **read** and **write** handlers. wand validates it, stores
the data, runs the handlers on localhost, and lets you inspect everything. All CLI output is
JSON.

Run every command from the repository root. Use `wand ...` if the virtualenv is active
(`source .venv/bin/activate`), or `.venv/bin/wand ...` otherwise. App databases live in
`$WAND_HOME` (default `./.wand`).

---

## 1. The workflow

```bash
wand new todo                      # 1. creates apps/todo/app.py (a working example)
# 2. edit apps/todo/app.py
wand deploy apps/todo              # 3. validate + activate. Check "ok"; on failure read error.details
wand call todo add_item --caller alice --args '{"text": "buy milk"}'   # 4. try it
wand inspect todo                  # 5. versions, handlers, data, recent calls and errors
wand run todo --port 8000          # 6. optional: serve over HTTP
```

After every `deploy`, check the output:
- `"ok": true` → `"version"` is now live (`"action": "created"` the first time, `"updated"` after).
- `"ok": false` → nothing changed. The previous version is still serving, and your data is untouched.
  Fix what `error.details` lists, then deploy again.

---

## 2. The app file

`apps/<name>/app.py`. The folder name **is** the app name, and `name=` must match it.

```python
from wand import App, AppError

app = App(
    name="todo",                                   # must equal the folder name
    collections={                                  # collection -> {field: type}
        "items": {"owner": "string", "text": "string", "done": "boolean"},
    },
)


@app.write                                         # may read and write
def add_item(ctx, args):
    return ctx.db.insert("items", {"owner": ctx.caller, "text": args.get("text"), "done": False})


@app.read                                          # may only read
def list_items(ctx, args):
    return ctx.db.find("items", owner=ctx.caller)


@app.write
def complete_item(ctx, args):
    item = ctx.db.get("items", args.get("id"))
    if item is None or item["owner"] != ctx.caller:
        raise AppError("NOT_FOUND", "item not found")
    return ctx.db.update("items", item["id"], {"done": True})
```

### Rules
- Exactly **one** `App(...)` per file. Import only the standard library and `wand`. You can't import sibling files.
- Names (app, collections, fields, handlers) match `^[a-z][a-z0-9_]*$`.
- Field types: **`string`**, **`integer`**, **`boolean`**. Nothing else (no float, list, date, or optional fields).
- **Every field is required on every record.** Use `""`, `0` or `False` for "empty".
- `id` is reserved. wand assigns an integer `id` to every record. To reference another record, use an
  `integer` field (e.g. `"post_id": "integer"`).
- Ids are unique across **all** collections of an app, so they aren't consecutive within one collection
  (posts might be 1, 5, 8, with other records in between). `get("posts", 2)` never returns a record from
  another collection; it returns `None`.
- A handler is a function `name(ctx, args)` decorated with `@app.read` or `@app.write`. Its name is
  the handler name. There must be at least one handler.
- **After the first successful deploy, the schema (`collections`) cannot change.** Any difference
  (field added, removed or retyped; collection added or removed) is rejected with `SCHEMA_CHANGED`.
  Handler code can change freely: add, remove or rewrite handlers.

---

## 3. Inside a handler

| | |
|---|---|
| `ctx.caller` | the caller's identity string (e.g. `"alice"`). It's asserted by the caller, not authenticated. |
| `ctx.db` | the data handle (below). Bound to this app's data only. |
| `args` | a dict, exactly the JSON object the caller sent (`{}` if none). wand doesn't validate it; read it with `args.get(...)`. |
| return value | anything JSON-serialisable (dict, list, str, int, float, bool, None). |

### `ctx.db`: the data handle

| Method | Returns | Notes |
|---|---|---|
| `get(collection, id)` | the record dict, or **`None`** | `id` must be an int |
| `find(collection, **filters)` | list of records, oldest first | equality only: `find("items", owner="alice", done=False)`; no filters = all |
| `insert(collection, record)` | the stored record, with `id` | record must have exactly the declared fields |
| `update(collection, id, changes)` | the updated record | partial dict of changes; the merged record is validated; `NOT_FOUND` if the id doesn't exist |

There is no delete. Records are plain dicts: `{"id": 1, "owner": "alice", ...}`.

### Read vs write
- In an `@app.read` handler, `insert`/`update` raise **`WRITE_IN_READ_HANDLER`** and the call fails,
  even if you catch the exception. Put writes in `@app.write` handlers.
- A write handler runs in **one transaction**. If it raises anything, *all* of its writes are rolled back.

### Errors
- Expected failures: `raise AppError("SOME_CODE", "human message", hint="what to do")`. The caller
  gets `{"code": "SOME_CODE", "source": "app", ...}`. Pick stable UPPER_SNAKE codes **named after your
  domain** (`POLL_CLOSED`, `TOO_MANY_OPTIONS`, `EMPTY_TEXT`). Don't reuse or imitate wand's own codes
  (§7, e.g. `ARGS_INVALID`). `source` tells them apart, but distinct names keep logs unambiguous.
  `NOT_FOUND` is the one shared convention (see §4).
- Bad data is caught for you: inserting a record with a missing or mistyped field returns
  `RECORD_INVALID`, listing every problem. So `args.get("title")` → `None` → a clear error, no extra code needed.
- Any other exception becomes `HANDLER_ERROR` with `details.line` (the line in your app.py) and a traceback.

---

## 4. Patterns

**Data owned by the caller (private).** Store `ctx.caller` in an `owner` field, filter on it, and treat
"not yours" exactly like "doesn't exist". Never return `FORBIDDEN`, because that reveals the record exists.
```python
item = ctx.db.get("items", args.get("id"))
if item is None or item["owner"] != ctx.caller:
    raise AppError("NOT_FOUND", "item not found")
```

**Shared data.** Don't filter by owner (e.g. posts everyone can see). Still record `ctx.caller` as the author.

**Do-once actions (votes, likes, RSVPs).** Store one record per (target, caller) and compute counts
from those records, instead of keeping a counter. Check before inserting, and on a repeat return
success with a flag (don't raise):
```python
already = bool(ctx.db.find("votes", post_id=post["id"], voter=ctx.caller))
if not already:
    ctx.db.insert("votes", {"post_id": post["id"], "voter": ctx.caller})
return {"votes": len(ctx.db.find("votes", post_id=post["id"])), "already_voted": already}
```
This is safe because wand handles one write at a time.

**Shared data with an owner-only action** (anyone can see a poll; only its creator can close it). The record is
visible, so `NOT_FOUND` would be wrong. Use a domain code:
```python
poll = ctx.db.get("polls", poll_id)
if poll is None:
    raise AppError("NOT_FOUND", "poll not found")
if poll["creator"] != ctx.caller:
    raise AppError("NOT_CREATOR", "only the poll's creator can close it")
```

**Change-your-choice actions** (re-voting moves your vote). One record per (target, caller), and `update` it on a repeat:
```python
existing = ctx.db.find("votes", poll_id=poll["id"], voter=ctx.caller)
if existing:
    ctx.db.update("votes", existing[0]["id"], {"option_id": option_id})
else:
    ctx.db.insert("votes", {"poll_id": poll["id"], "voter": ctx.caller, "option_id": option_id})
```

**Lists and children** (a poll's options, an order's items). Fields can't hold lists, so give the
children their own collection with an `integer` reference to the parent. `find` returns records in
insertion order, so children inserted in order come back in order (add a `position` field if order can change):
```python
"options": {"poll_id": "integer", "text": "string"}
...
for text in args.get("options", []):
    ctx.db.insert("options", {"poll_id": poll["id"], "text": text})
options = ctx.db.find("options", poll_id=poll["id"])
```
Do all of a parent's inserts in one write handler: if anything fails, all of them roll back.

**Validating args beyond types.** Check in the handler and raise a domain code
(`AppError("EMPTY_TEXT", ...)`, `AppError("TOO_MANY_OPTIONS", ...)`). For ids taken from args, check
`isinstance(x, int) and not isinstance(x, bool)` first if you want an app-specific error; otherwise wand
answers a non-integer id with `QUERY_INVALID`.

---

## 5. CLI reference

Every command prints one JSON document to stdout and exits `0` if `"ok": true`, `1` otherwise.

| Command | Output on success |
|---|---|
| `wand new <name> [--dir apps]` | `{"ok", "app", "path", "next"}`. The skeleton deploys as is. |
| `wand deploy <app_dir>` | `{"ok", "app", "action": "created"\|"updated", "version", "previous_version", "handlers": {name: "read"\|"write"}}` |
| `wand call <name> <handler> --caller ID [--args JSON]` | `{"ok": true, "version", "result"}`. No server needed. |
| `wand inspect <name> [--logs N]` | `{"ok", "app", "database", "active_version": {number, created_at, handlers, schema}, "latest_rejection", "versions", "data": {collection: [records]}, "recent_logs"}` |
| `wand run <name> [--port 8000] [--host 127.0.0.1]` | prints one JSON line `{"ok": true, "event": "serving", "url", "version"}` and serves until stopped |

- `version` in every call response (and log entry) is the version that was active when the call arrived. It's
  `null` only if the app has no active version.
- `latest_rejection` is `null` unless the most recent deploy was rejected. Then it holds the full error.
- `recent_logs` (newest first) has every call attempt: `at, version, handler, caller, args, ok, duration_ms, error`.
- Quote `--args` for the shell: `--args '{"id": 1}'`.

## 6. HTTP API (`wand run`)

```
POST /call/<handler>     header X-Caller: <identity>    body: JSON object of args (empty = {})
GET  /health             → {"ok": true, "app": "...", "version": N}
```
```bash
curl -s -X POST localhost:8000/call/add_item -H "X-Caller: alice" -d '{"text": "buy milk"}'
```
Responses are the same `{"ok", "version", "result" | "error"}` as `wand call`. Status is 200 on success,
404 for `UNKNOWN_HANDLER`/`UNKNOWN_ROUTE`, 503 for `NO_ACTIVE_VERSION`, 500 for `HANDLER_ERROR`/`RESULT_INVALID`,
400 otherwise. The running server picks up new deploys automatically (no restart). One app per server.

---

## 7. Errors

Every error has the same shape:
```json
{"ok": false, "error": {"code": "...", "message": "...", "hint": "...", "source": "platform" | "app", "details": {}}}
```
`source: "app"` means your handler raised `AppError`. Everything else comes from wand.

| Code | Meaning | What to do |
|---|---|---|
| `DEFINITION_INVALID` | app.py has a syntax/import error or breaks the contract | fix every item in `details.problems` (and `details.line`) and deploy again |
| `SCHEMA_CHANGED` | `collections` differs from the active version | revert each item in `details.changes`; only handler code may change |
| `APP_FILE_NOT_FOUND` | no `app.py` in the folder you deployed | pass the app folder, e.g. `apps/todo` |
| `APP_NOT_FOUND` | the app was never deployed | `wand deploy apps/<name>` |
| `NO_ACTIVE_VERSION` | every deploy so far was rejected | `wand inspect <name>` → `latest_rejection`, fix, deploy |
| `UNKNOWN_HANDLER` | no handler with that name | use a name from `details.handlers` |
| `CALLER_REQUIRED` | missing or empty caller | pass `--caller` / `X-Caller` |
| `ARGS_INVALID` | args aren't a JSON object | send an object, e.g. `{}` |
| `RECORD_INVALID` | a record doesn't match the schema | see `details.problems` (missing, unknown or mistyped fields) |
| `QUERY_INVALID` | `find` on an undeclared field, wrong filter type, or non-integer id | filter on declared fields with the right types |
| `UNKNOWN_COLLECTION` | collection not declared | use one of `details.declared` |
| `NOT_FOUND` (platform) | `update()` on an id that doesn't exist | `get()` it first |
| `WRITE_IN_READ_HANDLER` | a read handler tried to write | make the handler `@app.write`, or move the write |
| `HANDLER_ERROR` | your handler raised an unexpected exception (nothing was written) | fix the code at `details.line`; see `details.traceback` |
| `RESULT_INVALID` | the handler returned something that isn't JSON (e.g. a set) | return dicts/lists/str/int/float/bool/None |
| `APP_EXISTS` | `wand new` on an existing folder | edit it, or choose another name |
| `PORT_IN_USE` | `wand run` can't bind the port | `--port <other>` |
| `UNKNOWN_ROUTE` | HTTP path isn't `/call/<handler>` or `/health` | fix the URL |
| `USAGE_ERROR` | bad CLI arguments | see `details.usage` |
| `INTERNAL_ERROR` | a bug in wand itself | report it; not your app's fault |

---

## 8. Checklist before you deploy
- [ ] Folder name == `name=`; one `App`; every handler is `(ctx, args)` with `@app.read` or `@app.write`
- [ ] Field types only `string` / `integer` / `boolean`; no `id` field; references are `integer`
- [ ] Every `insert` provides **all** fields
- [ ] Private data: filter by `owner=ctx.caller`, and return `NOT_FOUND` for others' records
- [ ] No writes in read handlers
- [ ] Changing an already-deployed app? Keep `collections` exactly the same

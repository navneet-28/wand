"""Runtime: runs any app's handlers. Nothing in here knows about a specific app.

    runtime = Runtime("notes", home)
    runtime.call("create_note", caller="alice", args={"title": "hi"})
      -> {"ok": true, "version": 2, "result": {...}}
      -> {"ok": false, "version": 2, "error": {"code": ..., ...}}

For every call:
  1. check caller and args
  2. hot reload: if the active version changed since the last call, load it
  3. give the handler a Context(caller, db); db is read-only for read handlers
  4. write handlers run in one transaction: any error rolls back every write (D-016)
  5. a read handler that attempted a write fails, even if it caught the error (D-015)
  6. log the call
`call()` never raises for a failed call; failures come back as structured errors.
"""

from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wand.contract import App, Handler, load_app
from wand.errors import WandError
from wand.store import DataHandle, open_app_db
from wand.versions import active_number, active_version

LOG_RETENTION = 500  # keep only the most recent calls


@dataclass(frozen=True)
class Context:
    """What every handler receives as `ctx`."""
    caller: str
    db: DataHandle


class Runtime:
    def __init__(self, app_name: str, home: Path | str) -> None:
        self.name = app_name
        self.conn = open_app_db(home, app_name)  # APP_NOT_FOUND if never deployed
        self._app: App | None = None
        self._version: int | None = None

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Runtime:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def current(self) -> tuple[App, int]:
        """The active app definition, reloaded if a new version was deployed (hot reload)."""
        number = active_number(self.conn)
        if number is None:
            raise WandError(
                "NO_ACTIVE_VERSION",
                f"app {self.name!r} has no successfully deployed version",
                hint=f"Run `wand inspect {self.name}` to see why deploys were rejected, fix app.py, "
                     f"and deploy again.",
                details={"app": self.name},
            )
        if number != self._version:
            source = active_version(self.conn)["source"]
            self._app = load_app(source, origin=f"{self.name}/app.py@v{number}", expected_name=self.name)
            self._version = number
        return self._app, number

    def call(self, handler_name: str, caller: Any, args: Any) -> dict[str, Any]:
        started = time.perf_counter()
        version = active_number(self.conn)  # reported even if the request is rejected early
        try:
            _check_request(caller, args)
            app, version = self.current()
            handler = _find_handler(app, handler_name)
            result = self._invoke(app, handler, caller, args)
            response = {"ok": True, "version": version, "result": result}
        except WandError as error:
            response = {"ok": False, "version": version, "error": error.to_dict()}
        self._log(handler_name, caller, args, version, response, started)
        return response

    def reject(self, handler_name: str, caller: Any, args: Any, error: WandError) -> dict[str, Any]:
        """Log and return a request that failed before it could be made, e.g. unparseable JSON args."""
        version = active_number(self.conn)
        response = {"ok": False, "version": version, "error": error.to_dict()}
        self._log(handler_name, caller, args, version, response, time.perf_counter())
        return response

    def _invoke(self, app: App, handler: Handler, caller: str, args: dict[str, Any]) -> Any:
        db = DataHandle(self.conn, app.collections, read_only=handler.kind == "read")
        ctx = Context(caller=caller, db=db)
        try:
            if handler.kind == "write":
                with self.conn:  # commit on success, roll back on any exception
                    return _json_safe(handler, handler.fn(ctx, args))
            result = _json_safe(handler, handler.fn(ctx, args))
        except WandError:
            if db.violations:
                raise db.violations[0] from None
            raise
        except Exception as e:
            if db.violations:
                raise db.violations[0] from None
            raise _handler_error(handler, e) from e
        if db.violations:  # the handler caught the error and carried on
            raise db.violations[0]
        return result

    def _log(
        self, handler: str, caller: Any, args: Any, version: int | None,
        response: dict[str, Any], started: float,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO logs (at, version, handler, caller, args, ok, duration_ms, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                    version,
                    handler,
                    caller if isinstance(caller, str) else None,
                    json.dumps(args, default=repr),
                    response["ok"],
                    round((time.perf_counter() - started) * 1000, 2),
                    json.dumps(response["error"]) if not response["ok"] else None,
                ),
            )
            self.conn.execute(
                "DELETE FROM logs WHERE id <= (SELECT MAX(id) FROM logs) - ?", (LOG_RETENTION,)
            )


def recent_logs(conn, limit: int = 20) -> list[dict[str, Any]]:
    """The most recent calls, newest first."""
    rows = conn.execute(
        "SELECT at, version, handler, caller, args, ok, duration_ms, error "
        "FROM logs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        {"at": at, "version": version, "handler": handler, "caller": caller,
         "args": json.loads(args), "ok": bool(ok), "duration_ms": ms,
         "error": json.loads(error) if error else None}
        for at, version, handler, caller, args, ok, ms, error in rows
    ]


def _check_request(caller: Any, args: Any) -> None:
    if not isinstance(caller, str) or not caller.strip():
        raise WandError(
            "CALLER_REQUIRED",
            "every call needs a non-empty caller identity",
            hint="Pass the caller, e.g. --caller alice (CLI) or the X-Caller header (HTTP).",
        )
    if not isinstance(args, dict):
        raise WandError(
            "ARGS_INVALID",
            f"args must be a JSON object, got {type(args).__name__}",
            hint='Pass args as a JSON object, e.g. {"key": "value"}; use {} for none.',
        )


def _find_handler(app: App, name: str) -> Handler:
    if name not in app.handlers:
        raise WandError(
            "UNKNOWN_HANDLER",
            f"app {app.name!r} has no handler {name!r}",
            hint="Call one of details.handlers.",
            details={"handlers": {n: h.kind for n, h in app.handlers.items()}},
        )
    return app.handlers[name]


def _json_safe(handler: Handler, result: Any) -> Any:
    try:
        json.dumps(result, allow_nan=False)
    except (TypeError, ValueError) as e:
        raise WandError(
            "RESULT_INVALID",
            f"handler {handler.name!r} returned a value that is not JSON: {e}",
            hint="Return dicts, lists, strings, numbers, booleans or None.",
            details={"handler": handler.name},
        ) from e
    return result


def _handler_error(handler: Handler, e: Exception) -> WandError:
    # Point at the deepest line inside the app's own code, e.g. "notes/app.py@v3".
    frames = traceback.extract_tb(e.__traceback__)
    app_frames = [f for f in frames if "/app.py@v" in f.filename]
    where = app_frames[-1] if app_frames else frames[-1]
    return WandError(
        "HANDLER_ERROR",
        f"handler {handler.name!r} raised {type(e).__name__}: {e}",
        hint="Fix the handler code (see details.line), or raise AppError for expected failures. "
             "Nothing was written.",
        details={
            "handler": handler.name,
            "exception": type(e).__name__,
            "line": where.lineno,
            "traceback": "".join(traceback.format_exception(type(e), e, e.__traceback__)),
        },
    )

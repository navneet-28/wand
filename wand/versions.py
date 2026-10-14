"""Versions: deploying a definition and tracking which one is active.

    wand deploy apps/notes
      1. read apps/notes/app.py (the folder name is the app's identity, D-023)
      2. load + validate it (contract.load_app)
      3. compare its schema with the active version's (D-017)
      4a. any problem -> store it as a REJECTED version with the error, raise;
          the active version and the data are untouched
      4b. all good    -> in one transaction: old active -> superseded, new -> ACTIVE

Every deploy, accepted or rejected, is stored as a numbered full snapshot (D-002),
so `inspect` can show the active version and why the latest one was rejected.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wand.contract import NAME_PATTERN, App, load_app, schema_diff
from wand.errors import WandError
from wand.store import db_path, open_db


def deploy(app_dir: Path | str, home: Path | str) -> dict[str, Any]:
    """Validate the app in `app_dir` and make it active, or record why it was rejected."""
    app_dir = Path(app_dir)
    name = app_dir.resolve().name
    if not NAME_PATTERN.match(name):
        raise WandError(
            "DEFINITION_INVALID",
            f"folder name {name!r} is not a valid app name",
            hint=f"The folder name is the app name; rename it to match {NAME_PATTERN.pattern}.",
            details={"problems": [f"folder name {name!r} must match {NAME_PATTERN.pattern}"]},
        )
    app_file = app_dir / "app.py"
    if not app_file.is_file():
        raise WandError(
            "APP_FILE_NOT_FOUND",
            f"no app.py in {str(app_dir)!r}",
            hint="Pass the app's folder, which must contain app.py. Create one with `wand new <name>`.",
            details={"path": str(app_file)},
        )
    source = app_file.read_text()

    conn = open_db(db_path(home, name))
    try:
        active = active_version(conn)
        app: App | None = None
        try:
            app = load_app(source, origin=str(app_file), expected_name=name)
            if active is not None:
                _check_schema_unchanged(active, app)
        except WandError as error:
            number = _store_version(conn, "rejected", source, app, error.to_dict())
            raise _rejected(error, number, active)

        number = _store_version(conn, "active", source, app, None)
        return {
            "app": name,
            "action": "created" if active is None else "updated",
            "version": number,
            "previous_version": active["number"] if active else None,
            "handlers": _handler_kinds(app),
        }
    finally:
        conn.close()


def active_version(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The active version, including its source, or None if nothing was ever accepted."""
    row = conn.execute(
        "SELECT number, source, schema, handlers, created_at FROM versions WHERE status = 'active'"
    ).fetchone()
    if row is None:
        return None
    return {
        "number": row[0], "source": row[1], "schema": json.loads(row[2]),
        "handlers": json.loads(row[3]), "created_at": row[4],
    }


def active_number(conn: sqlite3.Connection) -> int | None:
    """Just the active version's number: a cheap check the runtime runs before every call."""
    row = conn.execute("SELECT number FROM versions WHERE status = 'active'").fetchone()
    return row[0] if row else None


def version_history(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every version, newest first, without source code."""
    rows = conn.execute(
        "SELECT number, status, error, created_at FROM versions ORDER BY number DESC"
    ).fetchall()
    return [
        {"number": n, "status": s, "created_at": at,
         "error": json.loads(e) if e else None}
        for n, s, e, at in rows
    ]


def latest_rejection(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The newest version if it was rejected, else None.

    Once a later deploy succeeds, an older rejection is history, not "the latest definition".
    """
    history = version_history(conn)
    if history and history[0]["status"] == "rejected":
        return history[0]
    return None


def _check_schema_unchanged(active: dict[str, Any], app: App) -> None:
    changes = schema_diff(active["schema"], app.collections)
    if changes:
        raise WandError(
            "SCHEMA_CHANGED",
            f"the schema differs from active version {active['number']} in {len(changes)} way(s)",
            hint="Schema changes are not supported. Revert every item in details.changes; "
                 "handler code can change freely.",
            details={"changes": changes},
        )


def _store_version(
    conn: sqlite3.Connection,
    status: str,
    source: str,
    app: App | None,
    error: dict[str, Any] | None,
) -> int:
    """Insert a version. Activating one supersedes the previous active one, in one transaction."""
    with conn:
        if status == "active":
            conn.execute("UPDATE versions SET status = 'superseded' WHERE status = 'active'")
        cursor = conn.execute(
            "INSERT INTO versions (status, source, schema, handlers, error, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                status,
                source,
                json.dumps(app.collections) if app else None,
                json.dumps(_handler_kinds(app)) if app else None,
                json.dumps(error) if error else None,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
    return cursor.lastrowid


def _rejected(error: WandError, number: int, active: dict[str, Any] | None) -> WandError:
    """Add the version bookkeeping to a rejection, so the agent knows what is still live."""
    error.details = {
        **error.details,
        "rejected_version": number,
        "active_version": active["number"] if active else None,
    }
    still_live = (
        f" Version {active['number']} is still active; its data is untouched."
        if active else " The app has no active version yet."
    )
    error.hint = (error.hint or "") + still_live
    return error


def _handler_kinds(app: App) -> dict[str, str]:
    return {name: handler.kind for name, handler in app.handlers.items()}

"""Storage: one SQLite file per app, and the data handle that handlers use.

Each app's file (`<home>/<app>.db`) holds its records, version history and call logs.

Records of every collection live in one generic table; the declared schema is
enforced here, in Python, on every write (D-004):

    records(id INTEGER PRIMARY KEY, collection TEXT, data TEXT)  -- data = JSON object

A `DataHandle` is what handlers see as `ctx.db`. It is bound to one app's
connection and schema, so it cannot reach another app's data (D-005). A read-only
handle refuses writes and remembers that it did, so the runtime can fail the call
even if the handler swallows the error (D-015).

Handles never commit. The runtime wraps each write call in a transaction (D-016).
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from wand.contract import Schema
from wand.errors import WandError

_TABLES = """
CREATE TABLE IF NOT EXISTS records (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    collection TEXT NOT NULL,
    data       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS records_by_collection ON records (collection);

CREATE TABLE IF NOT EXISTS versions (
    number     INTEGER PRIMARY KEY AUTOINCREMENT,
    status     TEXT NOT NULL,   -- active | superseded | rejected
    source     TEXT NOT NULL,   -- full app.py snapshot (D-002)
    schema     TEXT,            -- JSON; NULL if the code could not be loaded
    handlers   TEXT,            -- JSON {name: "read" | "write"}; NULL if not loaded
    error      TEXT,            -- JSON error, for rejected versions
    created_at TEXT NOT NULL
);
-- The database itself guarantees at most one active version.
CREATE UNIQUE INDEX IF NOT EXISTS one_active_version ON versions (status) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    version     INTEGER,         -- NULL if the call failed before a version was resolved
    handler     TEXT,
    caller      TEXT,
    args        TEXT,            -- JSON, as received
    ok          INTEGER NOT NULL,
    duration_ms REAL NOT NULL,
    error       TEXT             -- JSON error when ok = 0
);
"""

_JSON_TYPE_NAMES = {
    str: "string", int: "integer", bool: "boolean", float: "number",
    type(None): "null", list: "array", dict: "object",
}


def default_home() -> Path:
    """Where app databases live: $WAND_HOME, or ./.wand"""
    return Path(os.environ.get("WAND_HOME", ".wand"))


def db_path(home: Path | str, app_name: str) -> Path:
    return Path(home) / f"{app_name}.db"


def open_app_db(home: Path | str, app_name: str) -> sqlite3.Connection:
    """Open an app that has been deployed at least once (even if every deploy was rejected)."""
    path = db_path(home, app_name)
    if not path.exists():
        raise WandError(
            "APP_NOT_FOUND",
            f"no app named {app_name!r} in {str(Path(home))!r}",
            hint=f"Deploy it first: wand deploy apps/{app_name}",
            details={"app": app_name, "home": str(Path(home))},
        )
    return open_db(path)


def open_db(path: Path | str) -> sqlite3.Connection:
    """Open (creating if needed) an app's database file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # The server handles one request at a time (D-028), so a connection is never used by
    # two threads at once; allow it to be created and served from different threads.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.executescript(_TABLES)
    return conn


def matches_type(value: Any, field_type: str) -> bool:
    # bool is a subclass of int in Python, so integer must exclude it explicitly.
    if field_type == "string":
        return isinstance(value, str)
    if field_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if field_type == "boolean":
        return isinstance(value, bool)
    return False


def validate_record(collection: str, fields: dict[str, str], record: Any) -> None:
    """Raise RECORD_INVALID unless `record` has exactly the declared fields with the right types."""
    if not isinstance(record, dict):
        raise _record_invalid(collection, [f"record must be an object, got {_type_name(record)}"])

    problems = []
    if "id" in record:
        problems.append("'id' is assigned by wand and cannot be set or changed")
    for field in sorted(fields.keys() - record.keys()):
        problems.append(f"missing field {field!r} ({fields[field]})")
    for field in sorted(record.keys() - fields.keys() - {"id"}):
        problems.append(f"unknown field {field!r}")
    for field, field_type in fields.items():
        if field in record and not matches_type(record[field], field_type):
            problems.append(
                f"field {field!r} must be {field_type}, "
                f"got {_type_name(record[field])} {record[field]!r}"
            )
    if problems:
        raise _record_invalid(collection, problems)


class DataHandle:
    """`ctx.db`: the only way handlers read and write their app's data."""

    def __init__(self, conn: sqlite3.Connection, schema: Schema, read_only: bool = False) -> None:
        self._conn = conn
        self._schema = schema
        self.read_only = read_only
        self.violations: list[WandError] = []

    # --- reads ---------------------------------------------------------------

    def get(self, collection: str, id: int) -> dict[str, Any] | None:
        """Return the record, or None if this collection has no record with that id."""
        self._fields(collection)
        self._check_id(id)
        row = self._conn.execute(
            "SELECT id, data FROM records WHERE collection = ? AND id = ?", (collection, id)
        ).fetchone()
        return _to_record(row) if row else None

    def find(self, collection: str, /, **where: Any) -> list[dict[str, Any]]:
        """Return records whose fields equal every given value, oldest first.

        `find("notes", owner="alice")`. With no filters, returns the whole collection.
        """
        fields = self._fields(collection)
        sql = "SELECT id, data FROM records WHERE collection = ?"
        params: list[Any] = [collection]
        for field, value in where.items():
            self._check_filter(collection, fields, field, value)
            if field == "id":
                sql += " AND id = ?"
                params.append(value)
            else:
                sql += " AND json_extract(data, ?) = ?"
                params += [f"$.{field}", value]
        rows = self._conn.execute(sql + " ORDER BY id", params).fetchall()
        return [_to_record(row) for row in rows]

    # --- writes --------------------------------------------------------------

    def insert(self, collection: str, record: dict[str, Any]) -> dict[str, Any]:
        """Validate and store a new record. Returns it with its assigned `id`."""
        self._check_writable("insert")
        validate_record(collection, self._fields(collection), record)
        cursor = self._conn.execute(
            "INSERT INTO records (collection, data) VALUES (?, ?)",
            (collection, json.dumps(record)),
        )
        return {"id": cursor.lastrowid, **record}

    def update(self, collection: str, id: int, changes: dict[str, Any]) -> dict[str, Any]:
        """Apply `changes` to a record and return the updated record.

        The merged record is validated, so updates obey the same rules as inserts.
        """
        self._check_writable("update")
        fields = self._fields(collection)
        if not isinstance(changes, dict):
            raise _record_invalid(collection, [f"changes must be an object, got {_type_name(changes)}"])
        existing = self.get(collection, id)
        if existing is None:
            raise WandError(
                "NOT_FOUND",
                f"no record in {collection!r} with id {id}",
                hint="Check the id, e.g. with get() or find(), before updating.",
                details={"collection": collection, "id": id},
            )
        del existing["id"]
        merged = {**existing, **changes}
        validate_record(collection, fields, merged)
        self._conn.execute("UPDATE records SET data = ? WHERE id = ?", (json.dumps(merged), id))
        return {"id": id, **merged}

    # --- checks --------------------------------------------------------------

    def _check_writable(self, operation: str) -> None:
        if self.read_only:
            error = WandError(
                "WRITE_IN_READ_HANDLER",
                f"{operation}() is not allowed in a read handler",
                hint="Move this write into a handler decorated with @app.write.",
                details={"operation": operation},
            )
            self.violations.append(error)
            raise error

    def _fields(self, collection: str) -> dict[str, str]:
        if collection not in self._schema:
            raise WandError(
                "UNKNOWN_COLLECTION",
                f"collection {collection!r} is not declared by this app",
                hint="Use one of the declared collections, or declare it in App(collections=...).",
                details={"collection": collection, "declared": sorted(self._schema)},
            )
        return self._schema[collection]

    def _check_id(self, id: Any) -> None:
        if not matches_type(id, "integer"):
            raise WandError(
                "QUERY_INVALID",
                f"id must be an integer, got {_type_name(id)} {id!r}",
                hint="Record ids are integers assigned by insert().",
                details={"id": id},
            )

    def _check_filter(self, collection: str, fields: dict[str, str], field: str, value: Any) -> None:
        if field == "id":
            self._check_id(value)
            return
        if field not in fields:
            raise WandError(
                "QUERY_INVALID",
                f"cannot filter {collection!r} on unknown field {field!r}",
                hint="Filter only on declared fields or 'id'.",
                details={"collection": collection, "field": field, "declared": sorted(fields)},
            )
        if not matches_type(value, fields[field]):
            raise WandError(
                "QUERY_INVALID",
                f"filter {field!r} must be {fields[field]}, got {_type_name(value)} {value!r}",
                hint="Pass a value of the field's declared type.",
                details={"collection": collection, "field": field},
            )


def _to_record(row: tuple[int, str]) -> dict[str, Any]:
    return {"id": row[0], **json.loads(row[1])}


def _type_name(value: Any) -> str:
    return _JSON_TYPE_NAMES.get(type(value), type(value).__name__)


def _record_invalid(collection: str, problems: list[str]) -> WandError:
    return WandError(
        "RECORD_INVALID",
        f"invalid {collection!r} record: {'; '.join(problems)}",
        hint="Make the record match the collection's declared fields and types.",
        details={"collection": collection, "problems": problems},
    )

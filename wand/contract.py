"""The app contract.

An app is one Python file that creates exactly one `App` and registers handlers on it:

    from wand import App

    app = App(name="notes",
              collections={"notes": {"owner": "string", "title": "string"}})

    @app.read
    def list_notes(ctx, args): ...

    @app.write
    def create_note(ctx, args): ...

This module defines `App`, loads a definition from source text (`load_app`), checks it
against the contract, and diffs schemas between versions (`schema_diff`).
Deploy and runtime both load code through `load_app`, so code that passed validation
is loaded exactly the same way when it runs.
"""

from __future__ import annotations

import inspect
import re
import traceback
import types
from dataclasses import dataclass
from typing import Any, Callable

from wand.errors import WandError

FIELD_TYPES = ("string", "integer", "boolean")
RESERVED_FIELDS = ("id",)
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

Schema = dict[str, dict[str, str]]  # {collection: {field: type}}


@dataclass(frozen=True)
class Handler:
    name: str
    kind: str  # "read" or "write"
    fn: Callable[..., Any]


class App:
    def __init__(self, name: str, collections: Schema) -> None:
        self.name = name
        self.collections = collections
        self.handlers: dict[str, Handler] = {}
        # Recorded instead of raised, so validation can report every problem at once.
        self._duplicate_handlers: list[str] = []

    def read(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        return self._register(fn, "read")

    def write(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        return self._register(fn, "write")

    def _register(self, fn: Callable[..., Any], kind: str) -> Callable[..., Any]:
        name = fn.__name__
        if name in self.handlers:
            self._duplicate_handlers.append(name)
        self.handlers[name] = Handler(name=name, kind=kind, fn=fn)
        return fn


def load_app(source: str, origin: str = "app.py", expected_name: str | None = None) -> App:
    """Execute app source, find its single `App`, and validate it.

    `expected_name` is the app's folder name; the App's `name` must equal it (D-023).
    Raises WandError(DEFINITION_INVALID) on any problem. `details["problems"]`
    always lists what is wrong, so an agent can fix everything in one pass.
    """
    module = types.ModuleType("wand_app")
    module.__file__ = origin
    try:
        exec(compile(source, origin, "exec"), module.__dict__)
    except SyntaxError as e:
        raise _invalid(
            origin,
            [f"syntax error on line {e.lineno}: {e.msg}"],
            {"line": e.lineno},
        ) from e
    except Exception as e:
        raise _invalid(
            origin,
            [f"{type(e).__name__} while loading: {e}"],
            {"exception": type(e).__name__, "traceback": traceback.format_exc()},
        ) from e

    # Deduplicate by identity: `app2 = app` is still one App.
    apps = list({id(v): v for v in vars(module).values() if isinstance(v, App)}.values())
    if len(apps) != 1:
        raise _invalid(origin, [f"expected exactly one App object, found {len(apps)}"])

    app = apps[0]
    problems = find_problems(app)
    if expected_name is not None and app.name != expected_name:
        problems.append(f"app name {app.name!r} must match its folder name {expected_name!r}")
    if problems:
        raise _invalid(origin, problems)
    return app


def find_problems(app: App) -> list[str]:
    """Return every way `app` breaks the contract (empty list = valid)."""
    problems: list[str] = []

    if not _is_name(app.name):
        problems.append(f"app name {app.name!r} must match {NAME_PATTERN.pattern}")

    if not isinstance(app.collections, dict) or not app.collections:
        problems.append("collections must be a non-empty dict of {collection: {field: type}}")
    else:
        for collection, fields in app.collections.items():
            problems.extend(_collection_problems(collection, fields))

    if not app.handlers:
        problems.append("no handlers: decorate functions with @app.read or @app.write")
    for name in dict.fromkeys(app._duplicate_handlers):
        problems.append(f"handler {name!r} is defined more than once")
    for handler in app.handlers.values():
        if not _is_name(handler.name):
            problems.append(f"handler name {handler.name!r} must match {NAME_PATTERN.pattern}")
        if not _takes_ctx_and_args(handler.fn):
            problems.append(f"handler {handler.name!r} must take exactly two parameters: (ctx, args)")

    return problems


def schema_diff(old: Schema, new: Schema) -> list[dict[str, Any]]:
    """List every difference between two schemas (empty list = unchanged)."""
    changes: list[dict[str, Any]] = []
    for c in sorted(old.keys() - new.keys()):
        changes.append({"change": "collection_removed", "collection": c})
    for c in sorted(new.keys() - old.keys()):
        changes.append({"change": "collection_added", "collection": c})
    for c in sorted(old.keys() & new.keys()):
        old_fields, new_fields = old[c], new[c]
        for f in sorted(old_fields.keys() - new_fields.keys()):
            changes.append({"change": "field_removed", "collection": c, "field": f})
        for f in sorted(new_fields.keys() - old_fields.keys()):
            changes.append({"change": "field_added", "collection": c, "field": f})
        for f in sorted(old_fields.keys() & new_fields.keys()):
            if old_fields[f] != new_fields[f]:
                changes.append({
                    "change": "field_type_changed", "collection": c, "field": f,
                    "from": old_fields[f], "to": new_fields[f],
                })
    return changes


def _collection_problems(collection: Any, fields: Any) -> list[str]:
    problems: list[str] = []
    if not _is_name(collection):
        problems.append(f"collection name {collection!r} must match {NAME_PATTERN.pattern}")
    if not isinstance(fields, dict) or not fields:
        problems.append(f"collection {collection!r} must be a non-empty dict of {{field: type}}")
        return problems
    for field, field_type in fields.items():
        if field in RESERVED_FIELDS:
            problems.append(f"field {collection}.{field}: {field!r} is reserved (wand adds it to every record)")
        elif not _is_name(field):
            problems.append(f"field name {collection}.{field!r} must match {NAME_PATTERN.pattern}")
        if field_type not in FIELD_TYPES:
            problems.append(
                f"field {collection}.{field} has unsupported type {field_type!r}; "
                f"use one of {', '.join(FIELD_TYPES)}"
            )
    return problems


def _is_name(value: Any) -> bool:
    return isinstance(value, str) and bool(NAME_PATTERN.match(value))


def _takes_ctx_and_args(fn: Callable[..., Any]) -> bool:
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return False
    positional = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    return len(params) == 2 and all(p.kind in positional for p in params)


def _invalid(origin: str, problems: list[str], extra: dict[str, Any] | None = None) -> WandError:
    return WandError(
        "DEFINITION_INVALID",
        f"{origin} is not a valid app definition: {'; '.join(problems)}",
        hint="Fix every item in details.problems and deploy again.",
        details={"problems": problems, **(extra or {})},
    )

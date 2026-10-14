"""The `wand` command line.

    wand new <name> [--dir apps]                    create apps/<name>/app.py (deployable as is)
    wand deploy <app_dir>                           validate and activate, or record the rejection
    wand run <name> [--port 8000] [--host ...]      serve the active version on localhost
    wand call <name> <handler> --caller ID [--args JSON]   call a handler (no server needed)
    wand inspect <name> [--logs 20]                 identity, versions, handlers, data, logs

Every command prints one JSON document to stdout and exits 0 if `ok` is true, 1 otherwise.
`run` is a stream: it prints one JSON line when ready and keeps serving.
Apps' databases live in $WAND_HOME (default ./.wand).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from pathlib import Path
from typing import Any

from wand.contract import NAME_PATTERN
from wand.errors import WandError
from wand.runtime import Runtime, recent_logs
from wand.server import make_server
from wand.store import DataHandle, db_path, default_home, open_app_db
from wand.versions import active_version, deploy, latest_rejection, version_history

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

SKELETON = '''"""{name}: describe what this app does."""

from wand import App, AppError

app = App(
    name="{name}",
    collections={{
        "items": {{"owner": "string", "text": "string"}},
    }},
)


@app.write
def add_item(ctx, args):
    return ctx.db.insert("items", {{"owner": ctx.caller, "text": args.get("text")}})


@app.read
def list_items(ctx, args):
    return ctx.db.find("items", owner=ctx.caller)


@app.read
def get_item(ctx, args):
    item = ctx.db.get("items", args.get("id"))
    if item is None or item["owner"] != ctx.caller:
        raise AppError("NOT_FOUND", "item not found")
    return item
'''


# --- commands -----------------------------------------------------------------

def cmd_new(args: argparse.Namespace) -> dict[str, Any]:
    name = args.name
    if not NAME_PATTERN.match(name):
        raise WandError(
            "USAGE_ERROR",
            f"{name!r} is not a valid app name",
            hint=f"App names must match {NAME_PATTERN.pattern}, e.g. 'notes' or 'book_club'.",
        )
    app_dir = Path(args.dir) / name
    if app_dir.exists():
        raise WandError(
            "APP_EXISTS",
            f"{str(app_dir)!r} already exists",
            hint="Edit the existing app.py, or choose another name.",
            details={"path": str(app_dir)},
        )
    app_dir.mkdir(parents=True)
    app_file = app_dir / "app.py"
    app_file.write_text(SKELETON.format(name=name))
    return {"ok": True, "app": name, "path": str(app_file), "next": f"wand deploy {app_dir}"}


def cmd_deploy(args: argparse.Namespace) -> dict[str, Any]:
    return {"ok": True, **deploy(args.app_dir, default_home())}


def cmd_call(args: argparse.Namespace) -> dict[str, Any]:
    with Runtime(args.name, default_home()) as runtime:
        try:
            call_args = json.loads(args.args)
        except json.JSONDecodeError as e:
            error = WandError(
                "ARGS_INVALID",
                f"--args is not valid JSON: {e}",
                hint="""Quote the JSON for your shell, e.g. --args '{"key": "value"}'.""",
            )
            return runtime.reject(args.handler, args.caller, args.args, error)
        return runtime.call(args.handler, args.caller, call_args)


def cmd_inspect(args: argparse.Namespace) -> dict[str, Any]:
    home = default_home()
    conn = open_app_db(home, args.name)
    try:
        active = active_version(conn)
        data = {}
        if active:
            db = DataHandle(conn, active["schema"], read_only=True)
            data = {collection: db.find(collection) for collection in active["schema"]}
        return {
            "ok": True,
            "app": args.name,
            "database": str(db_path(home, args.name)),
            "active_version": active and {
                "number": active["number"],
                "created_at": active["created_at"],
                "handlers": active["handlers"],
                "schema": active["schema"],
            },
            "latest_rejection": latest_rejection(conn),
            "versions": [  # compact history; the full error is in latest_rejection
                {"number": v["number"], "status": v["status"], "created_at": v["created_at"],
                 "error_code": v["error"]["code"] if v["error"] else None}
                for v in version_history(conn)
            ],
            "data": data,
            "recent_logs": recent_logs(conn, args.logs),
        }
    finally:
        conn.close()


def cmd_run(args: argparse.Namespace) -> dict[str, Any]:
    runtime = Runtime(args.name, default_home())
    try:
        _, version = runtime.current()  # fail fast if nothing was ever deployed
        server = make_server(runtime, args.host, args.port)
        host, port = server.server_address[:2]
        _emit({"ok": True, "event": "serving", "app": args.name, "version": version,
               "url": f"http://{host}:{port}"}, pretty=False)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return {"ok": True, "event": "stopped", "app": args.name}
    finally:
        runtime.close()


# --- plumbing -------------------------------------------------------------------

class _JsonArgumentParser(argparse.ArgumentParser):
    """argparse prints plain-text usage errors; agents need JSON."""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise WandError(
            "USAGE_ERROR",
            _strip_colors(message),
            hint="Run `wand --help` or `wand <command> --help` for usage.",
            details={"usage": _strip_colors(self.format_usage()).strip()},
        )


def _strip_colors(text: str) -> str:
    # Python 3.14+ argparse adds terminal color codes when stdout is a terminal.
    return _ANSI_ESCAPE.sub("", text)


def build_parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(
        prog="wand",
        description="Create, run, update and inspect small data-backed apps. All output is JSON.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    new = commands.add_parser("new", help="create a skeleton app")
    new.add_argument("name")
    new.add_argument("--dir", default="apps", help="parent folder (default: apps)")
    new.set_defaults(func=cmd_new)

    dep = commands.add_parser("deploy", help="validate and activate an app folder")
    dep.add_argument("app_dir", help="folder containing app.py; its name is the app name")
    dep.set_defaults(func=cmd_deploy)

    run = commands.add_parser("run", help="serve an app on localhost")
    run.add_argument("name")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=8000)
    run.set_defaults(func=cmd_run, stream=True)

    call = commands.add_parser("call", help="call a handler directly (no server needed)")
    call.add_argument("name")
    call.add_argument("handler")
    # Not required=True: the runtime checks the caller, so a missing caller is CALLER_REQUIRED
    # and logged, exactly as over HTTP.
    call.add_argument("--caller", help="the caller's identity, e.g. alice (required)")
    call.add_argument("--args", default="{}", help="JSON object of arguments (default: {})")
    call.set_defaults(func=cmd_call)

    ins = commands.add_parser("inspect", help="show an app's versions, handlers, data and logs")
    ins.add_argument("name")
    ins.add_argument("--logs", type=int, default=20, help="number of recent log entries")
    ins.set_defaults(func=cmd_inspect)

    return parser


def main(argv: list[str] | None = None) -> int:
    pretty = True
    try:
        args = build_parser().parse_args(argv)
        pretty = not getattr(args, "stream", False)
        payload = args.func(args)
    except WandError as e:
        payload = {"ok": False, "error": e.to_dict()}
    except Exception as e:  # a bug in wand itself: still answer in JSON
        payload = {"ok": False, "error": WandError(
            "INTERNAL_ERROR",
            f"{type(e).__name__}: {e}",
            hint="This is a bug in wand, not in your app.",
            details={"traceback": traceback.format_exc()},
        ).to_dict()}
    _emit(payload, pretty)
    return 0 if payload["ok"] else 1


def _emit(payload: dict[str, Any], pretty: bool = True) -> None:
    print(json.dumps(payload, indent=2 if pretty else None), flush=True)


if __name__ == "__main__":
    sys.exit(main())

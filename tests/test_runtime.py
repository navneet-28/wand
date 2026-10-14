"""Tests for the runtime: handler context, caller scoping, read/write restrictions,
transactions, hot reload, restart, separation, and logs."""

import pytest

from wand import runtime as runtime_module
from wand.errors import WandError
from wand.runtime import Runtime, recent_logs
from wand.versions import deploy

APP = """
from wand import App, AppError

app = App(name="{name}", collections={{"notes": {{"owner": "string", "title": "string"}}}})

@app.write
def create_note(ctx, args):
    return ctx.db.insert("notes", {{"owner": ctx.caller, "title": args["title"]}})

@app.read
def list_notes(ctx, args):
    return ctx.db.find("notes", owner=ctx.caller)

@app.write
def rename_note(ctx, args):
    note = ctx.db.get("notes", args["id"])
    if note is None or note["owner"] != ctx.caller:
        raise AppError("NOT_FOUND", "note not found")
    return ctx.db.update("notes", args["id"], {{"title": args["title"]}})

@app.read
def sneaky_write(ctx, args):
    return ctx.db.insert("notes", {{"owner": ctx.caller, "title": "sneaky"}})

@app.read
def swallowed_write(ctx, args):
    try:
        ctx.db.insert("notes", {{"owner": ctx.caller, "title": "sneaky"}})
    except Exception:
        pass
    return "looks fine"

@app.write
def crash_after_insert(ctx, args):
    ctx.db.insert("notes", {{"owner": ctx.caller, "title": "half done"}})
    raise RuntimeError("boom")

@app.write
def bad_result_after_insert(ctx, args):
    ctx.db.insert("notes", {{"owner": ctx.caller, "title": "half done"}})
    return {{1, 2}}

@app.write
def bad_record(ctx, args):
    return ctx.db.insert("notes", {{"owner": ctx.caller}})
"""


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


@pytest.fixture
def deploy_app(tmp_path, home):
    def _deploy(source: str = APP, name: str = "notes"):
        app_dir = tmp_path / "apps" / name
        app_dir.mkdir(parents=True, exist_ok=True)
        (app_dir / "app.py").write_text(source.format(name=name) if "{name}" in source else source)
        return deploy(app_dir, home)
    return _deploy


@pytest.fixture
def rt(deploy_app, home):
    deploy_app()
    with Runtime("notes", home) as runtime:
        yield runtime


def ok(response):
    assert response["ok"], response
    return response["result"]


def code(response):
    assert not response["ok"], response
    return response["error"]["code"]


# --- handler context and caller scoping -------------------------------------

def test_handler_receives_caller_and_data(rt):
    note = ok(rt.call("create_note", "alice", {"title": "hi"}))
    assert note == {"id": note["id"], "owner": "alice", "title": "hi"}
    assert ok(rt.call("list_notes", "alice", {})) == [note]


def test_callers_only_see_and_change_their_own_notes(rt):
    note = ok(rt.call("create_note", "alice", {"title": "secret"}))

    assert ok(rt.call("list_notes", "bob", {})) == []
    response = rt.call("rename_note", "bob", {"id": note["id"], "title": "hacked"})
    assert code(response) == "NOT_FOUND"
    assert response["error"]["source"] == "app"
    # Same answer as for a note that doesn't exist at all (D-014, D-021).
    assert rt.call("rename_note", "bob", {"id": 999, "title": "x"})["error"] == response["error"]
    assert ok(rt.call("list_notes", "alice", {}))[0]["title"] == "secret"


# --- request validation -----------------------------------------------------

@pytest.mark.parametrize("caller", [None, "", "   ", 42])
def test_caller_is_required(rt, caller):
    assert code(rt.call("list_notes", caller, {})) == "CALLER_REQUIRED"


@pytest.mark.parametrize("args", [None, [], "title", 3])
def test_args_must_be_an_object(rt, args):
    assert code(rt.call("list_notes", "alice", args)) == "ARGS_INVALID"


def test_unknown_handler_lists_available_handlers(rt):
    response = rt.call("delete_note", "alice", {})
    assert code(response) == "UNKNOWN_HANDLER"
    assert response["error"]["details"]["handlers"]["list_notes"] == "read"


# --- read/write restrictions and transactions --------------------------------

def test_write_from_read_handler_fails_and_writes_nothing(rt):
    response = rt.call("sneaky_write", "alice", {})
    assert code(response) == "WRITE_IN_READ_HANDLER"
    assert response["error"]["source"] == "platform"
    assert ok(rt.call("list_notes", "alice", {})) == []


def test_write_from_read_handler_fails_even_if_caught(rt):
    assert code(rt.call("swallowed_write", "alice", {})) == "WRITE_IN_READ_HANDLER"
    assert ok(rt.call("list_notes", "alice", {})) == []


def test_crashing_write_handler_rolls_back_and_points_at_the_line(rt):
    response = rt.call("crash_after_insert", "alice", {})
    assert code(response) == "HANDLER_ERROR"
    details = response["error"]["details"]
    assert details["exception"] == "RuntimeError"
    raise_line = APP.splitlines().index('    raise RuntimeError("boom")') + 1
    assert details["line"] == raise_line
    assert ok(rt.call("list_notes", "alice", {})) == []


def test_missing_arg_is_a_handler_error(rt):
    response = rt.call("create_note", "alice", {})
    assert code(response) == "HANDLER_ERROR"
    assert response["error"]["details"]["exception"] == "KeyError"


def test_non_json_result_fails_and_rolls_back(rt):
    assert code(rt.call("bad_result_after_insert", "alice", {})) == "RESULT_INVALID"
    assert ok(rt.call("list_notes", "alice", {})) == []


def test_invalid_record_from_handler_is_reported(rt):
    response = rt.call("bad_record", "alice", {})
    assert code(response) == "RECORD_INVALID"
    assert response["error"]["details"]["problems"] == ["missing field 'title' (string)"]


# --- versions: hot reload, rejected deploys, restart --------------------------

V2 = APP + """
@app.read
def count_notes(ctx, args):
    return len(ctx.db.find("notes", owner=ctx.caller))
"""


def test_hot_reload_picks_up_new_version_without_restart(rt, deploy_app):
    ok(rt.call("create_note", "alice", {"title": "hi"}))
    assert code(rt.call("count_notes", "alice", {})) == "UNKNOWN_HANDLER"

    deploy_app(V2)

    response = rt.call("count_notes", "alice", {})
    assert response == {"ok": True, "version": 2, "result": 1}


def test_rejected_deploy_leaves_running_version_serving(rt, deploy_app):
    ok(rt.call("create_note", "alice", {"title": "hi"}))
    with pytest.raises(WandError):
        deploy_app(APP + "\ndef broken(:\n")
    response = rt.call("list_notes", "alice", {})
    assert response["version"] == 1
    assert [n["title"] for n in ok(response)] == ["hi"]


def test_restart_keeps_data_and_active_version(rt, deploy_app, home):
    ok(rt.call("create_note", "alice", {"title": "hi"}))
    deploy_app(V2)
    rt.close()

    with Runtime("notes", home) as restarted:
        assert restarted.call("count_notes", "alice", {}) == {"ok": True, "version": 2, "result": 1}


def test_unknown_app_is_not_found(home):
    with pytest.raises(WandError) as exc:
        Runtime("ghost", home)
    assert exc.value.code == "APP_NOT_FOUND"


def test_app_with_only_rejected_versions_has_no_active_version(deploy_app, home):
    with pytest.raises(WandError):
        deploy_app("def broken(:\n")
    with Runtime("notes", home) as runtime:
        assert code(runtime.call("list_notes", "alice", {})) == "NO_ACTIVE_VERSION"


# --- data separation ----------------------------------------------------------

def test_two_apps_with_same_collection_names_do_not_share_data(deploy_app, home):
    deploy_app(name="first")
    deploy_app(name="second")
    with Runtime("first", home) as first, Runtime("second", home) as second:
        ok(first.call("create_note", "alice", {"title": "only in first"}))
        assert ok(second.call("list_notes", "alice", {})) == []


# --- logs ---------------------------------------------------------------------

def test_calls_are_logged_newest_first(rt):
    rt.call("create_note", "alice", {"title": "hi"})
    rt.call("sneaky_write", "bob", {})

    newest, older = recent_logs(rt.conn, limit=2)
    assert newest["handler"] == "sneaky_write" and newest["caller"] == "bob"
    assert newest["ok"] is False and newest["error"]["code"] == "WRITE_IN_READ_HANDLER"
    assert older == {**older, "handler": "create_note", "caller": "alice", "ok": True,
                     "version": 1, "args": {"title": "hi"}, "error": None}


def test_failed_requests_are_logged_too(rt):
    rt.call("list_notes", "", {})
    assert recent_logs(rt.conn, limit=1)[0]["error"]["code"] == "CALLER_REQUIRED"


def test_early_failures_still_report_the_active_version(rt):
    assert rt.call("list_notes", "", {})["version"] == 1
    assert rt.call("list_notes", "alice", [])["version"] == 1
    assert rt.reject("list_notes", "alice", "{bad", WandError("ARGS_INVALID", "x"))["version"] == 1
    assert recent_logs(rt.conn, limit=1)[0]["version"] == 1


def test_logs_keep_only_recent_calls(rt, monkeypatch):
    monkeypatch.setattr(runtime_module, "LOG_RETENTION", 3)
    for i in range(6):
        rt.call("create_note", "alice", {"title": str(i)})
    logs = recent_logs(rt.conn, limit=100)
    assert [log["args"]["title"] for log in logs] == ["5", "4", "3"]

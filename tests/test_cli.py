"""CLI: every command prints JSON and exits 0 on success, 1 on error."""

import json

import pytest

from wand.cli import main


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WAND_HOME", str(tmp_path / ".wand"))
    return tmp_path


@pytest.fixture
def wand(capsys):
    """Run `wand <argv>` and return (exit code, parsed JSON output)."""
    def run(*argv):
        code = main(list(argv))
        return code, json.loads(capsys.readouterr().out)
    return run


def test_new_creates_a_skeleton_that_deploys_and_runs(wand, workspace):
    code, out = wand("new", "todo")
    assert code == 0 and out["path"] == "apps/todo/app.py"
    assert out["next"] == "wand deploy apps/todo"

    code, out = wand("deploy", "apps/todo")
    assert (code, out["action"], out["version"]) == (0, "created", 1)

    code, out = wand("call", "todo", "add_item", "--caller", "alice", "--args", '{"text": "hi"}')
    assert code == 0 and out["result"]["text"] == "hi"
    code, out = wand("call", "todo", "get_item", "--caller", "bob", "--args", '{"id": 1}')
    assert code == 1 and out["error"]["code"] == "NOT_FOUND"


def test_new_refuses_existing_folder_and_bad_names(wand):
    wand("new", "todo")
    assert wand("new", "todo")[1]["error"]["code"] == "APP_EXISTS"
    assert wand("new", "Bad-Name")[1]["error"]["code"] == "USAGE_ERROR"


def test_usage_errors_are_json(wand):
    code, out = wand("call", "todo")  # missing handler
    assert code == 1
    assert out["error"]["code"] == "USAGE_ERROR"
    assert "handler" in out["error"]["message"]


def test_invalid_args_json_is_reported_and_logged(wand):
    wand("new", "todo")
    wand("deploy", "apps/todo")
    code, out = wand("call", "todo", "add_item", "--caller", "alice", "--args", "{text: hi}")
    assert (code, out["error"]["code"]) == (1, "ARGS_INVALID")
    assert wand("inspect", "todo")[1]["recent_logs"][0]["error"]["code"] == "ARGS_INVALID"


def test_inspect_shows_identity_version_handlers_data_logs_and_rejection(wand, workspace):
    wand("new", "todo")
    wand("deploy", "apps/todo")
    wand("call", "todo", "add_item", "--caller", "alice", "--args", '{"text": "hi"}')
    app_file = workspace / "apps" / "todo" / "app.py"
    app_file.write_text(app_file.read_text() + "\ndef broken(:\n")
    code, out = wand("deploy", "apps/todo")
    assert (code, out["error"]["code"]) == (1, "DEFINITION_INVALID")

    code, out = wand("inspect", "todo")
    assert code == 0
    assert out["app"] == "todo"
    assert out["active_version"]["number"] == 1
    assert out["active_version"]["handlers"] == {
        "add_item": "write", "list_items": "read", "get_item": "read"}
    assert out["latest_rejection"]["number"] == 2
    assert out["latest_rejection"]["error"]["code"] == "DEFINITION_INVALID"
    assert [v["status"] for v in out["versions"]] == ["rejected", "active"]
    assert out["data"]["items"] == [{"id": 1, "owner": "alice", "text": "hi"}]
    assert out["recent_logs"][0]["handler"] == "add_item"


def test_unknown_app(wand):
    for argv in (["inspect", "ghost"], ["call", "ghost", "x", "--caller", "a"], ["run", "ghost"]):
        code, out = wand(*argv)
        assert (code, out["error"]["code"]) == (1, "APP_NOT_FOUND")


def test_run_fails_fast_without_an_active_version(wand, workspace):
    (workspace / "apps" / "todo").mkdir(parents=True)
    (workspace / "apps" / "todo" / "app.py").write_text("def broken(:\n")
    wand("deploy", "apps/todo")
    code, out = wand("run", "todo")
    assert (code, out["error"]["code"]) == (1, "NO_ACTIVE_VERSION")


def test_deploy_missing_app_file(wand, workspace):
    (workspace / "apps" / "empty").mkdir(parents=True)
    assert wand("deploy", "apps/empty")[1]["error"]["code"] == "APP_FILE_NOT_FOUND"


def test_usage_in_errors_has_no_terminal_color_codes(wand, monkeypatch):
    # Python 3.14+ argparse colors usage text on terminals; JSON must stay plain text.
    monkeypatch.setenv("FORCE_COLOR", "1")
    code, out = wand("call", "notes")
    assert out["error"]["code"] == "USAGE_ERROR"
    assert "\x1b" not in out["error"]["details"]["usage"]
    assert "\x1b" not in out["error"]["message"]


def test_missing_caller_is_caller_required_and_logged_like_http(wand):
    wand("new", "todo")
    wand("deploy", "apps/todo")
    code, out = wand("call", "todo", "list_items")  # no --caller
    assert (code, out["error"]["code"], out["version"]) == (1, "CALLER_REQUIRED", 1)
    assert wand("inspect", "todo")[1]["recent_logs"][0]["error"]["code"] == "CALLER_REQUIRED"

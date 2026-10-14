"""HTTP server: routes, caller header, JSON body handling, status codes, hot reload."""

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from wand.errors import WandError
from wand.runtime import Runtime
from wand.server import make_server
from wand.versions import deploy

NOTES = Path(__file__).resolve().parent.parent / "apps" / "notes"


@pytest.fixture
def server(tmp_path):
    home = tmp_path / "home"
    deploy(NOTES, home)
    runtime = Runtime("notes", home)
    httpd = make_server(runtime, port=0)  # any free port
    thread = threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    runtime.close()


def request(server, method, path, body=None, caller=None):
    """Return (HTTP status, parsed JSON body)."""
    host, port = server.server_address[:2]
    req = urllib.request.Request(f"http://{host}:{port}{path}", method=method,
                                 data=body.encode() if body is not None else None)
    if caller:
        req.add_header("X-Caller", caller)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_health(server):
    assert request(server, "GET", "/health") == (200, {"ok": True, "app": "notes", "version": 1})


def test_call_with_caller_header_and_json_body(server):
    status, body = request(server, "POST", "/call/create_note", '{"title": "hi"}', caller="alice")
    assert status == 200 and body["result"]["owner"] == "alice"
    status, body = request(server, "POST", "/call/list_notes", caller="alice")  # empty body = {}
    assert status == 200 and len(body["result"]) == 1
    assert request(server, "POST", "/call/list_notes", caller="bob")[1]["result"] == []


@pytest.mark.parametrize("path, body, caller, status, code", [
    ("/call/list_notes", None, None, 400, "CALLER_REQUIRED"),
    ("/call/list_notes", "{not json", "alice", 400, "ARGS_INVALID"),
    ("/call/list_notes", "[1, 2]", "alice", 400, "ARGS_INVALID"),
    ("/call/nope", None, "alice", 404, "UNKNOWN_HANDLER"),
    ("/elsewhere", None, "alice", 404, "UNKNOWN_ROUTE"),
    ("/call/get_note", '{"id": 999}', "alice", 400, "NOT_FOUND"),
])
def test_errors_have_status_and_structured_body(server, path, body, caller, status, code):
    got_status, got = request(server, "POST", path, body, caller)
    assert (got_status, got["ok"], got["error"]["code"]) == (status, False, code)


def test_server_hot_reloads_a_new_version(server, tmp_path):
    updated = tmp_path / "apps" / "notes"
    updated.mkdir(parents=True)
    (updated / "app.py").write_text((NOTES / "app.py").read_text() + (
        "\n@app.read\ndef count_notes(ctx, args):\n"
        "    return len(ctx.db.find('notes', owner=ctx.caller))\n"))
    deploy(updated, tmp_path / "home")
    assert request(server, "POST", "/call/count_notes", caller="alice")[1] == {
        "ok": True, "version": 2, "result": 0}


def test_port_in_use_is_reported(server, tmp_path):
    with pytest.raises(WandError) as exc:
        make_server(Runtime("notes", tmp_path / "home"), port=server.server_address[1])
    assert exc.value.code == "PORT_IN_USE"

"""Tests for deploy and versions: activation, same-schema updates, rejected
definitions, schema changes, and fallback to the last valid version."""

import pytest

from wand.contract import load_app
from wand.errors import WandError
from wand.store import DataHandle, db_path, open_db
from wand.versions import active_version, deploy, latest_rejection, version_history

NOTES_V1 = """
from wand import App

app = App(name="notes", collections={"notes": {"owner": "string", "title": "string"}})

@app.read
def list_notes(ctx, args):
    return ctx.db.find("notes", owner=ctx.caller)
"""

# Same schema, different handler code: a valid update.
NOTES_V2 = NOTES_V1 + """
@app.write
def create_note(ctx, args):
    return ctx.db.insert("notes", {"owner": ctx.caller, "title": args["title"]})
"""

SYNTAX_ERROR = NOTES_V1 + "\ndef broken(:\n"
SCHEMA_CHANGE = NOTES_V1.replace('"title": "string"', '"title": "integer", "pinned": "boolean"')


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


@pytest.fixture
def write_app(tmp_path):
    """Write source to <tmp>/apps/<folder>/app.py and return the folder."""
    def write(source: str, folder: str = "notes"):
        app_dir = tmp_path / "apps" / folder
        app_dir.mkdir(parents=True, exist_ok=True)
        (app_dir / "app.py").write_text(source)
        return app_dir
    return write


def reject(app_dir, home) -> WandError:
    with pytest.raises(WandError) as exc:
        deploy(app_dir, home)
    return exc.value


def state(home, name="notes"):
    """(active version number, statuses oldest-first, latest rejection) of an app."""
    conn = open_db(db_path(home, name))
    try:
        active = active_version(conn)
        statuses = [v["status"] for v in reversed(version_history(conn))]
        return (active["number"] if active else None), statuses, latest_rejection(conn)
    finally:
        conn.close()


def add_note(home, title="kept"):
    conn = open_db(db_path(home, "notes"))
    with conn:
        DataHandle(conn, {"notes": {"owner": "string", "title": "string"}}).insert(
            "notes", {"owner": "alice", "title": title})
    conn.close()


def notes_in(home):
    conn = open_db(db_path(home, "notes"))
    try:
        return [n["title"] for n in DataHandle(conn, {"notes": {"owner": "string", "title": "string"}}).find("notes")]
    finally:
        conn.close()


# --- accepted deploys -------------------------------------------------------

def test_first_deploy_creates_active_version(write_app, home):
    result = deploy(write_app(NOTES_V1), home)
    assert result == {"app": "notes", "action": "created", "version": 1,
                      "previous_version": None, "handlers": {"list_notes": "read"}}
    assert db_path(home, "notes").exists()
    assert state(home) == (1, ["active"], None)


def test_same_schema_update_activates_new_version_and_keeps_data(write_app, home):
    deploy(write_app(NOTES_V1), home)
    add_note(home)

    result = deploy(write_app(NOTES_V2), home)

    assert result["action"] == "updated"
    assert result["previous_version"] == 1
    assert result["handlers"] == {"list_notes": "read", "create_note": "write"}
    assert state(home) == (2, ["superseded", "active"], None)
    assert notes_in(home) == ["kept"]


# --- rejected deploys -------------------------------------------------------

@pytest.mark.parametrize("bad_source, code", [
    (SYNTAX_ERROR, "DEFINITION_INVALID"),
    (NOTES_V1.replace('"string"', '"float"', 1), "DEFINITION_INVALID"),   # contract violation
    (NOTES_V1 + "\nraise RuntimeError('boom')\n", "DEFINITION_INVALID"), # crash while loading
    (SCHEMA_CHANGE, "SCHEMA_CHANGED"),
])
def test_rejected_definition_keeps_last_valid_version_and_data(write_app, home, bad_source, code):
    deploy(write_app(NOTES_V1), home)
    add_note(home)

    error = reject(write_app(bad_source), home)

    assert error.code == code
    assert error.details["rejected_version"] == 2
    assert error.details["active_version"] == 1
    assert "Version 1 is still active" in error.hint
    active, statuses, rejection = state(home)
    assert (active, statuses) == (1, ["active", "rejected"])
    assert rejection["number"] == 2 and rejection["error"]["code"] == code
    assert notes_in(home) == ["kept"]


def test_schema_change_reports_exact_diff(write_app, home):
    deploy(write_app(NOTES_V1), home)
    error = reject(write_app(SCHEMA_CHANGE), home)
    assert error.details["changes"] == [
        {"change": "field_added", "collection": "notes", "field": "pinned"},
        {"change": "field_type_changed", "collection": "notes", "field": "title",
         "from": "string", "to": "integer"},
    ]


def test_name_must_match_folder(write_app, home):
    error = reject(write_app(NOTES_V1, folder="notess"), home)
    assert error.code == "DEFINITION_INVALID"
    assert "must match its folder name 'notess'" in error.details["problems"][0]
    # Recorded under the folder name, so `inspect notess` can explain it.
    active, statuses, rejection = state(home, "notess")
    assert (active, statuses) == (None, ["rejected"])
    assert rejection["error"]["code"] == "DEFINITION_INVALID"


def test_rejected_first_deploy_is_recorded_and_app_has_no_active_version(write_app, home):
    error = reject(write_app(SYNTAX_ERROR), home)
    assert error.details["active_version"] is None
    assert "no active version yet" in error.hint
    active, statuses, rejection = state(home)
    assert (active, statuses) == (None, ["rejected"])
    assert rejection["error"]["details"]["line"] is not None

    # The next good deploy is the app's first active version: "created".
    assert deploy(write_app(NOTES_V1), home)["action"] == "created"


def test_successful_deploy_clears_latest_rejection(write_app, home):
    deploy(write_app(NOTES_V1), home)
    reject(write_app(SYNTAX_ERROR), home)
    deploy(write_app(NOTES_V2), home)
    assert state(home) == (3, ["superseded", "rejected", "active"], None)


def test_missing_app_file_is_not_recorded(tmp_path, home):
    empty = tmp_path / "apps" / "notes"
    empty.mkdir(parents=True)
    assert reject(empty, home).code == "APP_FILE_NOT_FOUND"
    assert not db_path(home, "notes").exists()


def test_invalid_folder_name_is_rejected(write_app, home):
    error = reject(write_app(NOTES_V1, folder="My-Notes"), home)
    assert error.code == "DEFINITION_INVALID"
    assert not db_path(home, "My-Notes").exists()


# --- persistence and fallback -------------------------------------------------

def test_active_version_survives_restart_and_still_loads(write_app, home):
    deploy(write_app(NOTES_V2), home)
    reject(write_app(SYNTAX_ERROR), home)  # broken code is now on disk

    # "Restart": a fresh connection only knows what the database stored.
    conn = open_db(db_path(home, "notes"))
    active = active_version(conn)
    conn.close()

    app = load_app(active["source"], expected_name="notes")
    assert set(app.handlers) == {"list_notes", "create_note"}


def test_database_allows_only_one_active_version(write_app, home):
    for source in (NOTES_V1, NOTES_V2, NOTES_V2):
        deploy(write_app(source), home)
    conn = open_db(db_path(home, "notes"))
    count = conn.execute("SELECT COUNT(*) FROM versions WHERE status = 'active'").fetchone()[0]
    conn.close()
    assert count == 1


def test_each_app_gets_its_own_database(write_app, home):
    deploy(write_app(NOTES_V1), home)
    deploy(write_app(NOTES_V1.replace('name="notes"', 'name="other"'), folder="other"), home)
    assert db_path(home, "notes").exists() and db_path(home, "other").exists()
    assert state(home, "other") == (1, ["active"], None)

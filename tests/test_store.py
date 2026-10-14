"""Tests for storage: record validation, the data handle, read-only handles,
per-app separation, and persistence."""

import pytest

from wand.errors import WandError
from wand.store import DataHandle, open_db

SCHEMA = {
    "notes": {"owner": "string", "title": "string", "stars": "integer", "pinned": "boolean"},
    "tags": {"label": "string"},
}
NOTE = {"owner": "alice", "title": "hi", "stars": 3, "pinned": False}


@pytest.fixture
def conn(tmp_path):
    connection = open_db(tmp_path / "app.db")
    yield connection
    connection.close()


@pytest.fixture
def db(conn):
    return DataHandle(conn, SCHEMA)


def error_code(fn, *args, **kwargs) -> str:
    with pytest.raises(WandError) as exc:
        fn(*args, **kwargs)
    return exc.value.code


# --- insert / get ----------------------------------------------------------

def test_insert_assigns_id_and_get_returns_record(db):
    stored = db.insert("notes", NOTE)
    assert stored == {"id": stored["id"], **NOTE}
    assert db.get("notes", stored["id"]) == stored


def test_get_missing_record_returns_none(db):
    assert db.get("notes", 999) is None


def test_get_does_not_cross_collections(db):
    tag = db.insert("tags", {"label": "x"})
    assert db.get("notes", tag["id"]) is None


# --- record validation -----------------------------------------------------

@pytest.mark.parametrize("field, bad_value", [
    ("stars", True),        # bool is not an integer
    ("stars", 1.0),         # float is not an integer
    ("stars", "3"),         # string is not an integer
    ("title", 5),
    ("title", None),
    ("pinned", 1),          # integer is not a boolean
    ("pinned", "true"),
])
def test_wrong_field_type_is_rejected(db, field, bad_value):
    assert error_code(db.insert, "notes", {**NOTE, field: bad_value}) == "RECORD_INVALID"


def test_missing_field_is_rejected(db):
    record = {k: v for k, v in NOTE.items() if k != "title"}
    with pytest.raises(WandError) as exc:
        db.insert("notes", record)
    assert exc.value.details["problems"] == ["missing field 'title' (string)"]


def test_unknown_field_is_rejected(db):
    assert error_code(db.insert, "notes", {**NOTE, "color": "red"}) == "RECORD_INVALID"


def test_setting_id_is_rejected(db):
    assert error_code(db.insert, "notes", {**NOTE, "id": 7}) == "RECORD_INVALID"


def test_non_object_record_is_rejected(db):
    assert error_code(db.insert, "notes", ["not", "a", "dict"]) == "RECORD_INVALID"


def test_empty_string_is_a_valid_string(db):
    assert db.insert("notes", {**NOTE, "title": ""})["title"] == ""


def test_rejected_insert_stores_nothing(db):
    error_code(db.insert, "notes", {**NOTE, "stars": "many"})
    assert db.find("notes") == []


@pytest.mark.parametrize("operation", [
    lambda db: db.insert("nope", {}),
    lambda db: db.get("nope", 1),
    lambda db: db.find("nope"),
    lambda db: db.update("nope", 1, {}),
])
def test_unknown_collection_is_rejected(db, operation):
    assert error_code(operation, db) == "UNKNOWN_COLLECTION"


# --- find ------------------------------------------------------------------

def test_find_filters_by_equality_in_insertion_order(db):
    a1 = db.insert("notes", {**NOTE, "title": "a1"})
    db.insert("notes", {**NOTE, "owner": "bob"})
    a2 = db.insert("notes", {**NOTE, "title": "a2", "pinned": True})
    assert db.find("notes", owner="alice") == [a1, a2]
    assert db.find("notes", owner="alice", pinned=True) == [a2]
    assert db.find("notes", id=a1["id"]) == [a1]


def test_find_without_filters_returns_only_that_collection(db):
    note = db.insert("notes", NOTE)
    db.insert("tags", {"label": "x"})
    assert db.find("notes") == [note]


def test_find_on_unknown_field_is_rejected(db):
    assert error_code(db.find, "notes", color="red") == "QUERY_INVALID"


def test_find_with_wrong_value_type_is_rejected(db):
    assert error_code(db.find, "notes", stars="3") == "QUERY_INVALID"


def test_non_integer_id_is_rejected(db):
    assert error_code(db.get, "notes", "1") == "QUERY_INVALID"


# --- update ----------------------------------------------------------------

def test_update_merges_changes(db):
    note = db.insert("notes", NOTE)
    updated = db.update("notes", note["id"], {"title": "new"})
    assert updated == {**note, "title": "new"}
    assert db.get("notes", note["id"]) == updated


@pytest.mark.parametrize("changes", [
    {"stars": "five"},      # wrong type
    {"color": "red"},       # unknown field
    {"id": 42},             # id is immutable
    "not a dict",
])
def test_invalid_update_is_rejected_and_record_unchanged(db, changes):
    note = db.insert("notes", NOTE)
    assert error_code(db.update, "notes", note["id"], changes) == "RECORD_INVALID"
    assert db.get("notes", note["id"]) == note


def test_update_missing_record_is_not_found(db):
    assert error_code(db.update, "notes", 999, {"title": "x"}) == "NOT_FOUND"


def test_update_does_not_cross_collections(db):
    tag = db.insert("tags", {"label": "x"})
    assert error_code(db.update, "notes", tag["id"], {"title": "x"}) == "NOT_FOUND"


# --- read-only handle ------------------------------------------------------

def test_read_only_handle_can_read(conn, db):
    note = db.insert("notes", NOTE)
    ro = DataHandle(conn, SCHEMA, read_only=True)
    assert ro.get("notes", note["id"]) == note
    assert ro.find("notes", owner="alice") == [note]


def test_read_only_handle_refuses_writes_and_records_violations(conn, db):
    note = db.insert("notes", NOTE)
    ro = DataHandle(conn, SCHEMA, read_only=True)

    assert error_code(ro.insert, "notes", NOTE) == "WRITE_IN_READ_HANDLER"
    assert error_code(ro.update, "notes", note["id"], {"title": "x"}) == "WRITE_IN_READ_HANDLER"

    assert [v.details["operation"] for v in ro.violations] == ["insert", "update"]
    assert db.find("notes") == [note]  # nothing was written


def test_read_only_check_comes_before_other_validation(conn):
    ro = DataHandle(conn, SCHEMA, read_only=True)
    # Even an invalid write reports the read/write violation first.
    assert error_code(ro.insert, "nope", {}) == "WRITE_IN_READ_HANDLER"


# --- separation, transactions, persistence ---------------------------------

def test_apps_with_separate_files_do_not_share_data(tmp_path):
    schema = {"items": {"name": "string"}}  # same collection name in both apps
    first = DataHandle(open_db(tmp_path / "first.db"), schema)
    second = DataHandle(open_db(tmp_path / "second.db"), schema)
    first.insert("items", {"name": "only in first"})
    assert second.find("items") == []


def test_failed_transaction_rolls_back_every_write(conn, db):
    with pytest.raises(RuntimeError):
        with conn:  # how the runtime wraps a write handler (D-016)
            db.insert("notes", NOTE)
            raise RuntimeError("handler crashed halfway")
    assert db.find("notes") == []


def test_committed_data_survives_reopening(tmp_path):
    path = tmp_path / "app.db"
    conn = open_db(path)
    with conn:
        stored = DataHandle(conn, SCHEMA).insert("notes", NOTE)
    conn.close()

    reopened = open_db(path)
    assert DataHandle(reopened, SCHEMA).get("notes", stored["id"]) == stored
    reopened.close()

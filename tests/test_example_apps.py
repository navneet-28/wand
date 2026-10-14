"""The two example apps, deployed from apps/ and run on the same runtime."""

from pathlib import Path

import pytest

from wand.runtime import Runtime
from wand.versions import deploy

APPS = Path(__file__).resolve().parent.parent / "apps"


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


@pytest.fixture
def notes(home):
    deploy(APPS / "notes", home)
    with Runtime("notes", home) as runtime:
        yield runtime


@pytest.fixture
def feedback(home):
    deploy(APPS / "feedback", home)
    with Runtime("feedback", home) as runtime:
        yield runtime


def ok(response):
    assert response["ok"], response
    return response["result"]


def error(response):
    assert not response["ok"], response
    return response["error"]


# --- notes --------------------------------------------------------------------

def test_notes_create_list_update(notes):
    note = ok(notes.call("create_note", "alice", {"title": "groceries", "body": "milk"}))
    updated = ok(notes.call("update_note", "alice", {"id": note["id"], "body": "milk, eggs"}))
    assert updated == {**note, "body": "milk, eggs"}
    assert ok(notes.call("list_notes", "alice", {})) == [updated]


def test_notes_are_scoped_to_their_owner(notes):
    note = ok(notes.call("create_note", "alice", {"title": "secret"}))

    assert ok(notes.call("list_notes", "bob", {})) == []
    for handler, args in [("get_note", {"id": note["id"]}),
                          ("update_note", {"id": note["id"], "title": "hacked"})]:
        assert error(notes.call(handler, "bob", args))["code"] == "NOT_FOUND"

    # Indistinguishable from a note that does not exist.
    assert (error(notes.call("get_note", "bob", {"id": note["id"]}))["code"]
            == error(notes.call("get_note", "bob", {"id": 999}))["code"])
    assert ok(notes.call("get_note", "alice", {"id": note["id"]}))["title"] == "secret"


def test_notes_missing_title_is_a_validation_error(notes):
    assert error(notes.call("create_note", "alice", {}))["code"] == "RECORD_INVALID"


def test_notes_update_without_changes_is_an_app_error(notes):
    note = ok(notes.call("create_note", "alice", {"title": "t"}))
    err = error(notes.call("update_note", "alice", {"id": note["id"]}))
    assert (err["code"], err["source"]) == ("NOTHING_TO_UPDATE", "app")


# --- feedback -----------------------------------------------------------------

def test_feedback_posts_are_shared(feedback):
    post = ok(feedback.call("create_post", "alice", {"text": "dark mode please"}))
    assert ok(feedback.call("list_posts", "bob", {})) == [{**post, "votes": 0}]


def test_repeated_vote_does_not_increase_count(feedback):
    post = ok(feedback.call("create_post", "alice", {"text": "dark mode please"}))

    first = ok(feedback.call("vote", "bob", {"post_id": post["id"]}))
    second = ok(feedback.call("vote", "bob", {"post_id": post["id"]}))
    third = ok(feedback.call("vote", "carol", {"post_id": post["id"]}))

    assert (first["votes"], first["already_voted"]) == (1, False)
    assert (second["votes"], second["already_voted"]) == (1, True)
    assert (third["votes"], third["already_voted"]) == (2, False)
    assert ok(feedback.call("list_posts", "alice", {}))[0]["votes"] == 2


def test_votes_are_per_post(feedback):
    a = ok(feedback.call("create_post", "alice", {"text": "a"}))
    b = ok(feedback.call("create_post", "alice", {"text": "b"}))
    ok(feedback.call("vote", "bob", {"post_id": a["id"]}))
    assert [p["votes"] for p in ok(feedback.call("list_posts", "bob", {}))] == [1, 0]
    assert ok(feedback.call("vote", "bob", {"post_id": b["id"]}))["already_voted"] is False


def test_vote_on_missing_post_is_not_found(feedback):
    assert error(feedback.call("vote", "bob", {"post_id": 42}))["code"] == "NOT_FOUND"


# --- same runtime, separate data -----------------------------------------------

def test_both_apps_run_on_one_runtime_with_separate_data(notes, feedback, home):
    ok(notes.call("create_note", "alice", {"title": "n"}))
    ok(feedback.call("create_post", "alice", {"text": "f"}))
    assert (home / "notes.db").exists() and (home / "feedback.db").exists()
    assert error(notes.call("list_posts", "alice", {}))["code"] == "UNKNOWN_HANDLER"
    assert len(ok(notes.call("list_notes", "alice", {}))) == 1
    assert len(ok(feedback.call("list_posts", "alice", {}))) == 1

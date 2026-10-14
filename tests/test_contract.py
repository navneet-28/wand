"""Tests for the app contract: loading, definition validation, and schema diffs."""

import textwrap

import pytest

from wand.contract import App, load_app, schema_diff
from wand.errors import AppError, WandError

VALID_APP = """
from wand import App

app = App(name="notes", collections={"notes": {"owner": "string", "title": "string"}})

@app.read
def list_notes(ctx, args):
    return []

@app.write
def create_note(ctx, args):
    return {}
"""


def load(source: str) -> App:
    return load_app(textwrap.dedent(source))


def problems_of(source: str) -> list[str]:
    with pytest.raises(WandError) as exc:
        load(source)
    assert exc.value.code == "DEFINITION_INVALID"
    return exc.value.details["problems"]


def app_source(name='"notes"', collections='{"notes": {"title": "string"}}', handlers=None) -> str:
    """Build app source with one part swapped out, so each test changes exactly one thing."""
    if handlers is None:
        handlers = "@app.read\ndef list_notes(ctx, args):\n    return []\n"
    return f"from wand import App\napp = App(name={name}, collections={collections})\n{handlers}"


# --- loading ---------------------------------------------------------------

def test_valid_app_loads_with_handler_kinds():
    app = load(VALID_APP)
    assert app.name == "notes"
    assert {n: h.kind for n, h in app.handlers.items()} == {
        "list_notes": "read", "create_note": "write",
    }


def test_decorators_return_the_original_function():
    app = App(name="a", collections={"c": {"f": "string"}})

    def handler(ctx, args):
        return 1

    assert app.read(handler) is handler


def test_syntax_error_is_rejected_with_line_number():
    with pytest.raises(WandError) as exc:
        load("from wand import App\napp = App(name='x'\n")
    assert exc.value.code == "DEFINITION_INVALID"
    assert exc.value.details["line"] is not None


def test_exception_while_loading_is_rejected():
    with pytest.raises(WandError) as exc:
        load("from wand import App\nundefined_name\n")
    assert exc.value.code == "DEFINITION_INVALID"
    assert exc.value.details["exception"] == "NameError"


def test_missing_app_object_is_rejected():
    assert problems_of("x = 1\n") == ["expected exactly one App object, found 0"]


def test_two_app_objects_are_rejected():
    source = app_source() + 'other = App(name="other", collections={"c": {"f": "string"}})\n'
    assert problems_of(source) == ["expected exactly one App object, found 2"]


def test_alias_of_the_same_app_is_fine():
    assert load(app_source() + "alias = app\n").name == "notes"


# --- names -----------------------------------------------------------------

@pytest.mark.parametrize("name", ['"Notes"', '"1notes"', '"../x"', '""', '"my-app"', "42"])
def test_invalid_app_names_are_rejected(name):
    assert any("app name" in p for p in problems_of(app_source(name=name)))


def test_invalid_collection_and_field_names_are_rejected():
    problems = problems_of(app_source(collections='{"Notes": {"Title": "string"}}'))
    assert any("collection name 'Notes'" in p for p in problems)
    assert any("field name" in p and "'Title'" in p for p in problems)


# --- collections and fields ------------------------------------------------

@pytest.mark.parametrize("collections", ["{}", "[]", '{"notes": {}}', '{"notes": ["title"]}'])
def test_empty_or_malformed_collections_are_rejected(collections):
    assert problems_of(app_source(collections=collections))


@pytest.mark.parametrize("bad_type", ["float", "str", "int", "list", "String"])
def test_unsupported_field_types_are_rejected(bad_type):
    problems = problems_of(app_source(collections=f'{{"notes": {{"title": "{bad_type}"}}}}'))
    assert any("unsupported type" in p for p in problems)


def test_all_supported_field_types_are_accepted():
    collections = '{"notes": {"title": "string", "stars": "integer", "pinned": "boolean"}}'
    assert load(app_source(collections=collections)).collections["notes"]["pinned"] == "boolean"


def test_id_field_is_reserved():
    problems = problems_of(app_source(collections='{"notes": {"id": "string"}}'))
    assert any("reserved" in p for p in problems)


# --- handlers --------------------------------------------------------------

def test_app_without_handlers_is_rejected():
    assert any("no handlers" in p for p in problems_of(app_source(handlers="")))


def test_duplicate_handler_is_rejected():
    handlers = (
        "@app.read\ndef list_notes(ctx, args):\n    return []\n"
        "@app.write\ndef list_notes(ctx, args):\n    return []\n"
    )
    assert any("defined more than once" in p for p in problems_of(app_source(handlers=handlers)))


@pytest.mark.parametrize("params", ["ctx", "ctx, args, extra", "*args", "ctx, *, args", "**kw"])
def test_handler_must_take_ctx_and_args(params):
    handlers = f"@app.read\ndef list_notes({params}):\n    return []\n"
    assert any("(ctx, args)" in p for p in problems_of(app_source(handlers=handlers)))


def test_all_problems_are_reported_together():
    source = app_source(name='"Bad"', collections='{"notes": {"id": "float"}}', handlers="")
    assert len(problems_of(source)) >= 3


# --- schema diff -----------------------------------------------------------

def test_identical_schemas_have_no_diff():
    schema = {"notes": {"title": "string"}}
    assert schema_diff(schema, {"notes": {"title": "string"}}) == []


def test_schema_diff_reports_every_kind_of_change():
    old = {"notes": {"title": "string", "body": "string"}, "tags": {"label": "string"}}
    new = {"notes": {"title": "integer", "pinned": "boolean"}, "votes": {"voter": "string"}}
    assert schema_diff(old, new) == [
        {"change": "collection_removed", "collection": "tags"},
        {"change": "collection_added", "collection": "votes"},
        {"change": "field_removed", "collection": "notes", "field": "body"},
        {"change": "field_added", "collection": "notes", "field": "pinned"},
        {"change": "field_type_changed", "collection": "notes", "field": "title",
         "from": "string", "to": "integer"},
    ]


# --- errors ----------------------------------------------------------------

def test_errors_serialize_with_their_source():
    platform = WandError("NOT_FOUND", "missing", hint="check the id").to_dict()
    app = AppError("NOT_FOUND", "note not found").to_dict()
    assert platform == {"code": "NOT_FOUND", "message": "missing", "hint": "check the id",
                        "source": "platform", "details": {}}
    assert app["source"] == "app"


def test_app_name_must_match_expected_folder_name():
    with pytest.raises(WandError) as exc:
        load_app(textwrap.dedent(VALID_APP), expected_name="notess")
    assert exc.value.details["problems"] == [
        "app name 'notes' must match its folder name 'notess'"
    ]

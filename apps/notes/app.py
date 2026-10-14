"""Notes: each caller creates, lists and updates their own notes.

Scoping: every handler filters or checks by `ctx.caller`. Another caller's note
gets the same NOT_FOUND as a note that doesn't exist, so nothing is exposed.
"""

from wand import App, AppError

app = App(
    name="notes",
    collections={
        "notes": {"owner": "string", "title": "string", "body": "string"},
    },
)


def _own_note(ctx, note_id):
    note = ctx.db.get("notes", note_id)
    if note is None or note["owner"] != ctx.caller:
        raise AppError("NOT_FOUND", f"note {note_id} not found",
                       hint="Use list_notes to see your notes and their ids.")
    return note


@app.write
def create_note(ctx, args):
    return ctx.db.insert("notes", {
        "owner": ctx.caller,
        "title": args.get("title"),
        "body": args.get("body", ""),
    })


@app.read
def list_notes(ctx, args):
    return ctx.db.find("notes", owner=ctx.caller)


@app.read
def get_note(ctx, args):
    return _own_note(ctx, args.get("id"))


@app.write
def update_note(ctx, args):
    note = _own_note(ctx, args.get("id"))
    changes = {field: args[field] for field in ("title", "body") if field in args}
    if not changes:
        raise AppError("NOTHING_TO_UPDATE", "pass 'title' and/or 'body' to change")
    return ctx.db.update("notes", note["id"], changes)

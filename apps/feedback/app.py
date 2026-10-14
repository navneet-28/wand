"""Feedback: callers share feedback posts and vote on them.

Posts are visible to everyone. Votes are stored one record per (post, voter) and
counts are computed from them, so a repeated vote by the same caller changes
nothing (D-012). Writes are sequential (one request at a time), so the
check-then-insert in `vote` cannot race.
"""

from wand import App, AppError

app = App(
    name="feedback",
    collections={
        "posts": {"author": "string", "text": "string"},
        "votes": {"post_id": "integer", "voter": "string"},
    },
)


def _with_votes(ctx, post):
    return {**post, "votes": len(ctx.db.find("votes", post_id=post["id"]))}


@app.write
def create_post(ctx, args):
    post = ctx.db.insert("posts", {"author": ctx.caller, "text": args.get("text")})
    return _with_votes(ctx, post)


@app.read
def list_posts(ctx, args):
    return [_with_votes(ctx, post) for post in ctx.db.find("posts")]


@app.write
def vote(ctx, args):
    post = ctx.db.get("posts", args.get("post_id"))
    if post is None:
        raise AppError("NOT_FOUND", f"post {args.get('post_id')} not found",
                       hint="Use list_posts to see posts and their ids.")
    already_voted = bool(ctx.db.find("votes", post_id=post["id"], voter=ctx.caller))
    if not already_voted:
        ctx.db.insert("votes", {"post_id": post["id"], "voter": ctx.caller})
    return {**_with_votes(ctx, post), "already_voted": already_voted}

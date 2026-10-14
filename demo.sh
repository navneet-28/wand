#!/usr/bin/env bash
# End-to-end demo of wand. Run from anywhere:
#
#   ./demo.sh          interactive: before each step press Enter to run it, or q to quit
#   ./demo.sh --auto   run every step without pausing (also used when there's no terminal)
#
# Shows: both example apps on one runtime, caller scoping, idempotent votes,
# restart persistence, a same-schema code update (hot reload), rejected
# definitions (schema change + syntax error) with the last valid version still
# serving, and inspection. Every step checks its output and the script stops
# with FAIL on the first check that doesn't hold.
#
# Uses a throwaway WAND_HOME and copies of apps/, so the repo is never modified.
# Env: PYTHON (default .venv/bin/python, else python3), PORT (default 8765).

set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-.venv/bin/python}"
[ -x "$PY" ] || PY=python3
PORT="${PORT:-8765}"
WORK="$(mktemp -d)"
export WAND_HOME="$WORK/.wand"
SERVER_PID=""
INTERACTIVE=1
if [[ "${1:-}" == "--auto" || ! -t 0 ]]; then INTERACTIVE=0; fi

# Runs on every exit (finished, q, Ctrl-C, or a failed check): nothing is left behind.
cleanup() {
  [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null || true
  rm -rf "$WORK"
  printf '\033[2m(cleaned up: server stopped, %s removed)\033[0m\n' "$WORK"
}
trap cleanup EXIT

# --- helpers -------------------------------------------------------------------

# step TITLE: in interactive mode, wait for Enter (run) or q (quit) before each step.
step() {
  if [ "$INTERACTIVE" = 1 ]; then
    printf '\n\033[1mNext: %s\033[0m\n\033[2m[Enter] run   [q] quit\033[0m ' "$*"
    local key=""
    read -rsn1 key || key=q
    echo
    if [[ "$key" == [qQ] ]]; then
      printf 'Stopping the demo.\n'
      exit 0
    fi
  fi
  printf '\n\033[1;34m== %s\033[0m\n' "$*"
}

# wand ARGS...: run the CLI, print the command and its JSON output, keep it in $OUT.
wand() {
  printf '\033[2m$ wand %s\033[0m\n' "$*"
  OUT="$("$PY" -m wand "$@")" || true
  echo "$OUT"
}

# http METHOD PATH CALLER [JSON]: call the running server, keep the response in $OUT.
http() {
  printf '\033[2m$ curl -X %s localhost:%s%s -H "X-Caller: %s" %s\033[0m\n' "$1" "$PORT" "$2" "$3" "${4:+-d '$4'}"
  OUT="$(curl -s -X "$1" "http://127.0.0.1:$PORT$2" -H "X-Caller: $3" ${4:+-d "$4"} | "$PY" -m json.tool)"
  echo "$OUT"
}

# expect TEXT: the last output must contain TEXT.
expect() {
  if [[ "$OUT" == *"$1"* ]]; then
    printf '\033[32m   ✓ %s\033[0m\n' "$1"
  else
    printf '\033[31m   FAIL: expected output to contain: %s\033[0m\n' "$1"
    exit 1
  fi
}

start_server() {
  printf '\033[2m$ wand run %s --port %s &\033[0m\n' "$1" "$PORT"
  "$PY" -m wand run "$1" --port "$PORT" > "$WORK/server.log" 2>&1 &
  SERVER_PID=$!
  for _ in $(seq 50); do
    curl -s "http://127.0.0.1:$PORT/health" > /dev/null && break
    sleep 0.1
  done
  OUT="$(cat "$WORK/server.log")"
  echo "$OUT"
}

stop_server() {
  printf '\033[2m(stopping the server)\033[0m\n'
  kill "$SERVER_PID"
  wait "$SERVER_PID" 2>/dev/null || true
  SERVER_PID=""
}

# replace FILE OLD NEW: edit an app file (portable; avoids GNU/BSD sed differences).
replace() {
  "$PY" -c 'import sys; p, old, new = sys.argv[1:]; s = open(p).read(); open(p, "w").write(s.replace(old, new))' "$@"
}

# --- demo ----------------------------------------------------------------------

mkdir -p "$WORK/apps"
cp -R apps/notes apps/feedback "$WORK/apps/"
NOTES="$WORK/apps/notes"

step "1. Deploy both example apps (same runtime, no app-specific code)"
wand deploy "$NOTES"
expect '"action": "created"'
wand deploy "$WORK/apps/feedback"
expect '"action": "created"'

step "2. Notes over HTTP: callers only see and change their own notes"
start_server notes
expect '"event": "serving"'
http POST /call/create_note alice '{"title": "groceries", "body": "milk"}'
expect '"owner": "alice"'
http POST /call/create_note alice '{"title": "ideas", "body": "wand"}'
http POST /call/list_notes bob
expect '"result": []'
http POST /call/update_note bob '{"id": 1, "title": "hacked"}'
expect '"code": "NOT_FOUND"'
http POST /call/update_note alice '{"id": 1, "body": "milk, eggs"}'
expect '"body": "milk, eggs"'

step "3. Restart: data and the active version survive"
stop_server
start_server notes
http POST /call/list_notes alice
expect '"title": "groceries"'
expect '"title": "ideas"'

step "4. Same-schema code update: new handler, hot-reloaded by the running server"
cat >> "$NOTES/app.py" <<'EOF'


@app.read
def count_notes(ctx, args):
    return len(ctx.db.find("notes", owner=ctx.caller))
EOF
wand deploy "$NOTES"
expect '"action": "updated"'
expect '"version": 2'
http POST /call/count_notes alice
expect '"version": 2'
expect '"result": 2'

step "5a. Rejected definition: schema change"
cp "$NOTES/app.py" "$WORK/app.py.good"
replace "$NOTES/app.py" '"title": "string"' '"title": "integer"'
wand deploy "$NOTES"
expect '"code": "SCHEMA_CHANGED"'
expect '"change": "field_type_changed"'
expect '"active_version": 2'
cp "$WORK/app.py.good" "$NOTES/app.py"

step "5b. Rejected definition: syntax error"
printf '\ndef broken(:\n' >> "$NOTES/app.py"
wand deploy "$NOTES"
expect '"code": "DEFINITION_INVALID"'
expect 'Version 2 is still active'

step "5c. The last valid version is still serving, with its data"
http POST /call/count_notes alice
expect '"version": 2'
expect '"result": 2'

step "6. Inspect: identity, active version, handlers, why the latest deploy was rejected, data, logs"
wand inspect notes --logs 3
expect '"app": "notes"'
expect '"count_notes": "read"'
expect '"latest_rejection": {'
expect '"number": 4'
expect '"status": "rejected"'
stop_server

step "7. Feedback (one app at a time): a repeated vote doesn't increase the count"
start_server feedback
http POST /call/create_post alice '{"text": "please add dark mode"}'
expect '"votes": 0'
http POST /call/vote bob '{"post_id": 1}'
expect '"votes": 1'
http POST /call/vote bob '{"post_id": 1}'
expect '"votes": 1'
expect '"already_voted": true'
http POST /call/vote carol '{"post_id": 1}'
expect '"votes": 2'
stop_server

step "8. Data separation: each app has its own database and handlers"
wand call notes list_posts --caller alice
expect '"code": "UNKNOWN_HANDLER"'
wand call feedback list_posts --caller dave
expect '"text": "please add dark mode"'
OUT="$(ls "$WAND_HOME")"
echo "$OUT"
expect "feedback.db"
expect "notes.db"

printf '\n\033[1;32mDemo complete: every check passed.\033[0m\n'

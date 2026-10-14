"""HTTP server: serves one app's handlers on localhost.

    POST /call/<handler>    header  X-Caller: alice
                            body    JSON object of args (empty body = {})
    GET  /health            {"ok": true, "app": "notes", "version": 2}

Every response body is the runtime's envelope ({"ok", "version", "result" | "error"}).
The server is single-threaded on purpose: requests are handled one at a time, so
writes are sequential (D-028).
"""

from __future__ import annotations

import errno
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from wand.errors import WandError
from wand.runtime import Runtime

# Everything else that fails is 400 (a problem with the request or app input).
_STATUS = {
    "UNKNOWN_HANDLER": 404,
    "UNKNOWN_ROUTE": 404,
    "NO_ACTIVE_VERSION": 503,
    "HANDLER_ERROR": 500,
    "RESULT_INVALID": 500,
    "INTERNAL_ERROR": 500,
}


def make_server(runtime: Runtime, host: str = "127.0.0.1", port: int = 8000) -> HTTPServer:
    """Bind the server (port 0 = any free port). Call `serve_forever()` on the result."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            prefix = "/call/"
            if not self.path.startswith(prefix):
                return self._send(_unknown_route(self.command, self.path))
            handler = self.path[len(prefix):]
            caller = self.headers.get("X-Caller")
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
            try:
                args = json.loads(body) if body.strip() else {}
            except json.JSONDecodeError as e:
                error = WandError(
                    "ARGS_INVALID",
                    f"request body is not valid JSON: {e}",
                    hint='Send a JSON object as the body, e.g. {"key": "value"}, or an empty body.',
                )
                return self._send(runtime.reject(handler, caller, body, error))
            self._send(runtime.call(handler, caller, args))

        def do_GET(self) -> None:
            if self.path != "/health":
                return self._send(_unknown_route(self.command, self.path))
            try:
                _, version = runtime.current()
                self._send({"ok": True, "app": runtime.name, "version": version})
            except WandError as e:
                self._send({"ok": False, "error": e.to_dict()})

        def _send(self, payload: dict[str, Any]) -> None:
            status = 200 if payload["ok"] else _STATUS.get(payload["error"]["code"], 400)
            body = (json.dumps(payload) + "\n").encode()  # newline: friendlier in terminals
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass  # stdout stays JSON-only; calls are logged in the app database instead

    try:
        return HTTPServer((host, port), Handler)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            raise WandError(
                "PORT_IN_USE",
                f"port {port} on {host} is already in use",
                hint="Stop the other process, or pick another port with --port.",
                details={"host": host, "port": port},
            ) from e
        raise


def _unknown_route(method: str, path: str) -> dict[str, Any]:
    error = WandError(
        "UNKNOWN_ROUTE",
        f"no route for {method} {path}",
        hint="Use POST /call/<handler> or GET /health.",
    )
    return {"ok": False, "error": error.to_dict()}

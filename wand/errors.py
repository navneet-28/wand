"""Structured errors.

Every failure in wand, whether raised by the platform or by app code, becomes one
JSON shape so an agent can branch on `code` instead of parsing prose:

    {"ok": false,
     "error": {"code": "...", "message": "...", "hint": "...",
               "source": "platform" | "app", "details": {...}}}

The full list of platform codes is documented in AGENT_GUIDE.md.
"""

from __future__ import annotations

from typing import Any


class WandError(Exception):
    """An error raised by the platform itself."""

    source = "platform"

    def __init__(
        self,
        code: str,
        message: str,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "source": self.source,
            "details": self.details,
        }


class AppError(WandError):
    """An expected failure raised by app handler code.

    Usage inside a handler:
        raise AppError("NOT_FOUND", "note not found")
    """

    source = "app"

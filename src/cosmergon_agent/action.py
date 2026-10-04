"""Action results from game commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def error_text(body: Any, fallback: str = "") -> str:
    """Human-readable error from a server response body.

    The server answers every error as ``{"error": {"message": ..., "next": ...}}``
    (cosmergon.com/docs/errors/). ``next`` is the call that works instead; it is appended
    so a human reading the CLI or a log sees the way out, not only the failure.
    Older or proxy responses may carry ``{"detail": ...}`` — that still reads.
    """
    if not isinstance(body, dict):
        return fallback
    error = body.get("error")
    if not isinstance(error, dict):
        return str(body.get("detail", fallback))
    text = str(error.get("message", fallback))
    nxt = error.get("next")
    if isinstance(nxt, dict) and nxt.get("path"):
        text += f" → next: {nxt.get('method', '')} {nxt['path']}".rstrip()
    return text


@dataclass(frozen=True)
class ActionResult:
    """Result of an agent action.

    Attributes:
        success: Whether the action completed successfully.
        action: The action type that was attempted.
        data: Response data from the server.
        idempotency_key: The key used for this request (for debugging/tracing).
        error_code: HTTP status code if failed.
        error_message: Human-readable error message if failed.
    """

    success: bool
    action: str
    data: dict
    idempotency_key: str | None = None
    error_code: int | None = None
    error_message: str | None = None

    @property
    def next_call(self) -> dict | None:
        """The call that works instead, if the server named one (``error.next``)."""
        error = self.data.get("error") if isinstance(self.data, dict) else None
        nxt = error.get("next") if isinstance(error, dict) else None
        return nxt if isinstance(nxt, dict) else None

    @classmethod
    def from_response(
        cls,
        action: str,
        status_code: int,
        body: dict,
        idempotency_key: str | None = None,
    ) -> ActionResult:
        """Parse HTTP response into ActionResult."""
        if 200 <= status_code < 300:
            return cls(
                success=True,
                action=action,
                data=body,
                idempotency_key=idempotency_key,
            )

        error = body.get("error", body.get("detail", {}))
        if isinstance(error, dict):
            return cls(
                success=False,
                action=action,
                data=body,
                idempotency_key=idempotency_key,
                error_code=error.get("code", status_code),
                error_message=error.get("message", str(error)),
            )
        return cls(
            success=False,
            action=action,
            data=body,
            idempotency_key=idempotency_key,
            error_code=status_code,
            error_message=str(error),
        )

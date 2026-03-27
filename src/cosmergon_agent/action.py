"""Action results from game commands."""

from __future__ import annotations

from dataclasses import dataclass


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
                success=True, action=action, data=body,
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

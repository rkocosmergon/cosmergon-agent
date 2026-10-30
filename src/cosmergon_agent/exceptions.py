"""Exception hierarchy for the Cosmergon Agent SDK.

All SDK errors inherit from CosmergonError. Server HTTP errors map to
specific subclasses so callers can catch precisely what they need.
"""

from __future__ import annotations


class CosmergonError(Exception):
    """Base for all Cosmergon SDK errors."""

    def __init__(
        self,
        message: str,
        code: int | None = None,
        body: dict | None = None,
    ) -> None:
        self.message = message
        self.code = code
        self.body = body or {}
        super().__init__(message)


class AuthenticationError(CosmergonError):
    """401 — invalid or expired API key."""


class PermissionError(CosmergonError):
    """403 — not authorized for this resource."""


class NotFoundError(CosmergonError):
    """404 — resource does not exist."""


class InsufficientEnergyError(CosmergonError):
    """400 — not enough energy for the requested action."""


class RateLimitError(CosmergonError):
    """429 — too many requests. Check retry_after attribute."""

    def __init__(
        self,
        message: str = "Rate limited",
        retry_after: float = 1.0,
        **kwargs: object,
    ) -> None:
        super().__init__(message, code=429)
        self.retry_after = retry_after


class IdempotencyError(CosmergonError):
    """409 — idempotency key conflict (same key, different endpoint)."""


class ServerError(CosmergonError):
    """5xx — server-side failure."""


class ConnectionError(CosmergonError):
    """Network failure after all retries exhausted, or an answer that cannot be read."""


class NotConnectedError(CosmergonError, RuntimeError):
    """A request before ``run()``/``async with`` opened the connection, or after it closed.

    Also a ``RuntimeError``, which it was before: code that catches either keeps working, and
    the dashboard's handlers, which catch ``CosmergonError``, now catch it too (cos20 #606 F6).
    """


class SendRefusedError(CosmergonError):
    """The caller's ``send_check`` refused a request before it left (cos20 #492).

    Not a network failure: nothing was sent, and nothing is retried.
    """


class WebhookSignatureError(CosmergonError):
    """Webhook signature verification failed (wrong format or invalid HMAC)."""


class WebhookTimestampError(CosmergonError):
    """Webhook timestamp too old — possible replay attack."""

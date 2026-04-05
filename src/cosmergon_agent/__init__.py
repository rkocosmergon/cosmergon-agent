"""Cosmergon Agent SDK — Python client for the Cosmergon Agent Economy."""

__version__ = "0.3.25"

from cosmergon_agent.action import ActionResult
from cosmergon_agent.exceptions import (
    AuthenticationError,
    CosmergonError,
    InsufficientEnergyError,
    NotFoundError,
    RateLimitError,
    ServerError,
    WebhookSignatureError,
    WebhookTimestampError,
)
from cosmergon_agent.state import GameState
from cosmergon_agent.webhook import CosmergonWebhook


# Agent import is deferred to avoid circular import with __version__
def __getattr__(name: str) -> type:
    if name == "CosmergonAgent":
        from cosmergon_agent.agent import CosmergonAgent

        return CosmergonAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ActionResult",
    "AuthenticationError",
    "CosmergonAgent",
    "CosmergonError",
    "CosmergonWebhook",
    "GameState",
    "InsufficientEnergyError",
    "NotFoundError",
    "RateLimitError",
    "ServerError",
    "WebhookSignatureError",
    "WebhookTimestampError",
]

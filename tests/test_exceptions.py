"""Tests for exception hierarchy."""

from cosmergon_agent.exceptions import (
    AuthenticationError,
    CosmergonError,
    InsufficientEnergyError,
    NotFoundError,
    RateLimitError,
    ServerError,
)


def test_all_inherit_from_base() -> None:
    """All exceptions inherit from CosmergonError."""
    for exc_class in (AuthenticationError, NotFoundError, InsufficientEnergyError,
                      RateLimitError, ServerError):
        exc = exc_class("test")
        assert isinstance(exc, CosmergonError)
        assert isinstance(exc, Exception)


def test_base_error_attributes() -> None:
    """CosmergonError stores message, code, and body."""
    exc = CosmergonError("fail", code=500, body={"detail": "crash"})
    assert exc.message == "fail"
    assert exc.code == 500
    assert exc.body == {"detail": "crash"}
    assert str(exc) == "fail"


def test_rate_limit_retry_after() -> None:
    """RateLimitError has retry_after attribute."""
    exc = RateLimitError("slow down", retry_after=2.5)
    assert exc.retry_after == 2.5
    assert exc.code == 429


def test_base_error_defaults() -> None:
    """CosmergonError defaults: code=None, body={}."""
    exc = CosmergonError("oops")
    assert exc.code is None
    assert exc.body == {}

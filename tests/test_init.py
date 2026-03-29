"""Tests for __init__.py — lazy imports and __all__ exports."""

import cosmergon_agent


class TestLazyImport:
    def test_cosmergon_agent_lazy_import(self) -> None:
        """CosmergonAgent is available via lazy __getattr__."""
        agent_cls = cosmergon_agent.CosmergonAgent
        assert agent_cls.__name__ == "CosmergonAgent"

    def test_unknown_attr_raises(self) -> None:
        """Accessing unknown attribute raises AttributeError."""
        import pytest
        with pytest.raises(AttributeError, match="has no attribute"):
            _ = cosmergon_agent.NonExistentThing  # type: ignore[attr-defined]

    def test_all_exports(self) -> None:
        """__all__ contains expected exports."""
        expected = {
            "CosmergonAgent",
            "GameState",
            "ActionResult",
            "CosmergonError",
            "AuthenticationError",
            "InsufficientEnergyError",
            "NotFoundError",
            "RateLimitError",
            "ServerError",
        }
        assert set(cosmergon_agent.__all__) == expected

    def test_version_exists(self) -> None:
        """__version__ is set."""
        assert cosmergon_agent.__version__
        assert isinstance(cosmergon_agent.__version__, str)

    def test_direct_imports_work(self) -> None:
        """All direct imports from __init__ work."""
        from cosmergon_agent import (
            ActionResult,
            GameState,
        )
        assert GameState is not None
        assert ActionResult is not None

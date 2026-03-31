"""Tests for the terminal dashboard."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cosmergon_agent.dashboard import (
    Dashboard,
    _DRAW_INTERVAL,
    _MAX_LOG_ENTRIES,
    _MAX_SELECT_OPTIONS,
    _PRESETS,
    main,
)


class TestDashboardImport:
    """Verify dashboard module loads correctly."""

    def test_import(self) -> None:
        assert Dashboard is not None

    def test_presets(self) -> None:
        assert "blinker" in _PRESETS
        assert "glider" in _PRESETS
        assert len(_PRESETS) == 7

    def test_constants(self) -> None:
        assert _MAX_LOG_ENTRIES == 50
        assert _DRAW_INTERVAL == 0.1
        assert _MAX_SELECT_OPTIONS == 9


class TestDashboardInit:
    """Test Dashboard construction (mocked agent)."""

    def test_creates_with_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        d = Dashboard(api_key="AGENT-TEST:key123")
        assert d.agent is not None
        assert d._paused is False
        assert d._log == []

    def test_creates_without_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "cosmergon_agent.agent.CosmergonAgent._auto_register_anonymous",
            staticmethod(lambda base_url: ("AGENT-X:k", "id-1")),
        )
        d = Dashboard()
        assert d.agent.agent_id == "id-1"


class TestDashboardLog:
    """Test log management."""

    def test_add_log(self, monkeypatch: pytest.MonkeyPatch) -> None:
        d = Dashboard(api_key="AGENT-TEST:key")
        d._add_log("test message", 3)
        assert len(d._log) == 1
        assert d._log[0] == ("test message", 3)

    def test_log_truncation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        d = Dashboard(api_key="AGENT-TEST:key")
        for i in range(100):
            d._add_log(f"msg {i}")
        assert len(d._log) == _MAX_LOG_ENTRIES

    def test_log_default_color(self, monkeypatch: pytest.MonkeyPatch) -> None:
        d = Dashboard(api_key="AGENT-TEST:key")
        d._add_log("no color")
        assert d._log[0][1] == 0


class TestDashboardKeyHandling:
    """Test key dispatch (mocked actions)."""

    @pytest.fixture()
    def dashboard(self, monkeypatch: pytest.MonkeyPatch) -> Dashboard:
        d = Dashboard(api_key="AGENT-TEST:key")
        return d

    @pytest.mark.asyncio()
    async def test_quit_key(self, dashboard: Dashboard) -> None:
        stdscr = MagicMock()
        result = await dashboard._handle_key(ord("Q"), stdscr)
        assert result == "quit"

    @pytest.mark.asyncio()
    async def test_refresh_key(self, dashboard: Dashboard) -> None:
        stdscr = MagicMock()
        result = await dashboard._handle_key(ord("R"), stdscr)
        assert result is None
        assert any("refresh" in msg.lower() for msg, _ in dashboard._log)

    @pytest.mark.asyncio()
    async def test_help_key(self, dashboard: Dashboard) -> None:
        """Help key calls _show_help (requires curses init, skip in CI)."""
        stdscr = MagicMock()
        stdscr.getmaxyx.return_value = (40, 80)
        stdscr.getch.return_value = ord("x")
        # _show_help uses curses.newwin which needs initscr()
        # Mock it to avoid curses dependency in tests
        dashboard._show_help = MagicMock()  # type: ignore[method-assign]
        result = await dashboard._handle_key(ord("?"), stdscr)
        assert result is None
        dashboard._show_help.assert_called_once()

    @pytest.mark.asyncio()
    async def test_unknown_key(self, dashboard: Dashboard) -> None:
        stdscr = MagicMock()
        result = await dashboard._handle_key(ord("Z"), stdscr)
        assert result is None


class TestMainEntryPoint:
    """Test CLI entry point doesn't crash on import."""

    def test_main_function_exists(self) -> None:
        assert callable(main)

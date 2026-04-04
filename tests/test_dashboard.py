"""Dashboard panel tests — verifies content, colors, and layout.

Uses Textual's run_test() headless mode: same rendering pipeline as the real
terminal, no human screenshot needed.

Each test creates a _TestDashboard (no poll loop) with injected fake state,
triggers a manual redraw, and inspects widget.render() for:
  - Correct text content (plain)
  - Correct Rich markup styles (spans)
  - SVG layout (key strings visible, not clipped at 80x24)
"""

from __future__ import annotations

import html
import re

import pytest

from cosmergon_agent import CosmergonAgent
from cosmergon_agent.dashboard import THEMES, CosmergonDashboard
from cosmergon_agent.testing import fake_state
from textual import work
from textual.widgets import Static


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------


class _TestDashboard(CosmergonDashboard):
    """Dashboard with poll loop disabled — state injected directly."""

    @work(exclusive=True)
    async def _run_agent(self) -> None:
        pass  # no network, no infinite loop


def _make_dashboard(
    *,
    energy: float = 1000.0,
    subscription_tier: str = "free",
    score: float = 0.0,
    tier: int = 0,
    tier_name: str = "Novice",
    world_briefing: dict | None = None,
    compass_ever_set: bool = False,
    compass_preset: str = "autonomous",
    paused: bool = False,
    log: list[str] | None = None,
) -> _TestDashboard:
    """Factory: dashboard with fake state, no network calls."""
    state_kwargs: dict = {
        "energy": energy,
        "subscription_tier": subscription_tier,
        "ranking": {"player_tier": tier, "tier_name": tier_name, "player_score": score},
    }
    if world_briefing is not None:
        state_kwargs["world_briefing"] = world_briefing

    state = fake_state(**state_kwargs)
    agent = CosmergonAgent(api_key="AGENT-test:fakekey000000000000000000000")
    agent._state = state
    agent.agent_id = state.agent_id

    app = _TestDashboard(agent=agent, theme=THEMES["cosmergon"])
    app._compass_ever_set = compass_ever_set
    app._compass_preset = compass_preset
    app._paused = paused
    if log is not None:
        app._log = list(log)
    return app


async def _render(app: _TestDashboard, size: tuple[int, int] = (80, 24)):
    """Run app headless, trigger redraw, return (agent, economy, journal) Content."""
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        pilot.app._redraw()
        await pilot.pause()
        agent = pilot.app.query_one("#agent-panel", Static).render()
        economy = pilot.app.query_one("#economy-panel", Static).render()
        journal = pilot.app.query_one("#journal-panel", Static).render()
        return agent, economy, journal


def _has_style(content, text_fragment: str, style_fragment: str) -> bool:
    """Return True if text_fragment appears with style_fragment in any span."""
    for span in content.spans:
        if style_fragment in str(span.style):
            chunk = content.plain[span.start:span.end]
            if text_fragment in chunk:
                return True
    return False


# ---------------------------------------------------------------------------
# Paket 1 regression: Rich markup escaping
# ---------------------------------------------------------------------------


async def test_c_hotkey_visible_in_panel():
    """[C] must render as literal text — not consumed as Rich style tag."""
    app = _make_dashboard()
    agent, _, _ = await _render(app)
    assert "[C]" in agent.plain, (
        "Hotkey [C] invisible — Rich consumed it as a style tag. "
        "Use \\[C] in markup."
    )


async def test_u_hotkey_visible_in_panel():
    """[U] must render as literal text in economy panel."""
    app = _make_dashboard()
    _, economy, _ = await _render(app)
    assert "[U]" in economy.plain, (
        "Hotkey [U] invisible — Rich consumed it as a style tag."
    )


# ---------------------------------------------------------------------------
# One-Thing principle: only [C] may be yellow/orange
# ---------------------------------------------------------------------------


async def test_new_user_only_c_is_yellow():
    """At first start, only [C] should be yellow — not [U] or anything else."""
    app = _make_dashboard()
    agent, economy, _ = await _render(app)

    assert _has_style(agent, "[C]", "yellow"), "[C] must be yellow in agent panel"
    yellow_economy = [
        economy.plain[s.start:s.end]
        for s in economy.spans
        if "yellow" in str(s.style)
    ]
    assert not yellow_economy, (
        f"Nothing in economy panel should be yellow. Found: {yellow_economy}"
    )


async def test_compass_set_removes_yellow():
    """After compass is set, no more yellow in agent panel."""
    app = _make_dashboard(compass_ever_set=True, compass_preset="grow")
    agent, _, _ = await _render(app)

    yellow = [agent.plain[s.start:s.end] for s in agent.spans if "yellow" in str(s.style)]
    assert not yellow, f"After compass set, no yellow expected. Found: {yellow}"
    assert "Compass:" in agent.plain and "Grow" in agent.plain


async def test_upgrade_button_cyan_not_yellow():
    """[U] Upgrade must be cyan, never yellow."""
    app = _make_dashboard()
    _, economy, _ = await _render(app)

    assert _has_style(economy, "Upgrade", "cyan"), "[U] Upgrade must be cyan"
    assert not _has_style(economy, "Upgrade", "yellow"), "[U] Upgrade must not be yellow"


# ---------------------------------------------------------------------------
# Content correctness
# ---------------------------------------------------------------------------


async def test_score_zero_not_shown():
    """Score: 0 must not appear — bad first impression."""
    app = _make_dashboard(score=0.0)
    agent, _, _ = await _render(app)
    assert "Score" not in agent.plain


async def test_score_nonzero_shown():
    """Score > 0 must appear with proper formatting."""
    app = _make_dashboard(score=1500.0)
    agent, _, _ = await _render(app)
    assert "Score" in agent.plain
    assert "1,500" in agent.plain


async def test_agent_id_not_in_panel():
    """Agent ID is in status bar — must not be redundant in agent panel."""
    app = _make_dashboard()
    agent, _, _ = await _render(app)
    assert "Agent:" not in agent.plain


async def test_tip_truncated_at_word_boundary():
    """Long tip must end with … and not cut mid-word."""
    long_tip = "Check the market for underpriced assets. Buy more fields to increase income."
    app = _make_dashboard(world_briefing={
        "total_agents": 79, "your_rank": 44,
        "market_summary": "5 listings", "tip": long_tip,
    })
    _, economy, _ = await _render(app)

    tip_lines = [l for l in economy.plain.splitlines() if "→" in l]
    assert tip_lines, "Tip line must be present"
    tip_line = tip_lines[0]
    assert "…" in tip_line, f"Long tip must end with …, got: {repr(tip_line)}"
    # Must not end on a partial word (last char before … must be a space boundary)
    before_ellipsis = tip_line.split("…")[0].rstrip()
    assert before_ellipsis.endswith((".", "!", "?", "assets", "income", "fields", "market"
                                     )) or before_ellipsis[-1] in " etaoinsrhldcumfpgwybvkxjqz", (
        f"Tip cut mid-word: {repr(tip_line)}"
    )


async def test_last_event_not_red():
    """Past catastrophe events must not show in red — not an active alarm."""
    app = _make_dashboard(world_briefing={
        "total_agents": 79, "your_rank": 44,
        "market_summary": "5 listings",
        "last_event": "Grey Plague at tick 21", "tip": "test",
    })
    _, economy, _ = await _render(app)

    assert "Last:" in economy.plain, "last_event label must be 'Last:'"
    assert not _has_style(economy, "Grey Plague", "red"), (
        "Past event must not be red — use dim instead"
    )
    assert _has_style(economy, "Grey Plague", "dim"), (
        "Past event must be dim (historical, not alarming)"
    )


async def test_paused_status_shown():
    """Paused state must be clearly indicated."""
    app = _make_dashboard(paused=True)
    agent, _, _ = await _render(app)
    assert "PAUSED" in agent.plain


async def test_activity_placeholder_when_empty():
    """Empty log shows 'Connecting...' placeholder, not blank."""
    app = _make_dashboard(log=[])
    _, _, journal = await _render(app)
    assert "Connecting" in journal.plain


async def test_activity_shows_log_entries():
    """When log has entries, placeholder must be gone."""
    app = _make_dashboard(log=["[green]● Connected  1,000 E[/green]"])
    _, _, journal = await _render(app)
    assert "Connected" in journal.plain
    assert "Connecting to cosmergon.com" not in journal.plain


# ---------------------------------------------------------------------------
# Layout: key content visible in SVG at 80x24 (not clipped)
# ---------------------------------------------------------------------------


async def test_layout_headers_visible_80x24():
    """Panel headers must be visible (not clipped) at standard 80x24 terminal."""
    app = _make_dashboard(world_briefing={
        "total_agents": 79, "your_rank": 44,
        "market_summary": "5 listings", "tip": "Place oscillating cells.",
    })
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        pilot.app._redraw()
        await pilot.pause()
        svg = pilot.app.export_screenshot()

    visible = {
        html.unescape(t.strip())
        for t in re.findall(r">([^<\n]+)<", svg)
        if t.strip() and not t.strip().startswith(".")
    }

    assert any("AGENT" in t for t in visible), "AGENT header clipped at 80x24"
    assert any("WIRTSCHAFT" in t for t in visible), "WIRTSCHAFT header clipped at 80x24"
    assert any("JOURNAL" in t for t in visible), "JOURNAL header clipped at 80x24"
    assert any("AKTIV" in t for t in visible), "Status AKTIV clipped at 80x24"

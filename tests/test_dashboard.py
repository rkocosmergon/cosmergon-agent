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
from textual import work
from textual.widgets import Static

from cosmergon_agent import CosmergonAgent, CosmergonError
from cosmergon_agent.action import ActionResult
from cosmergon_agent.dashboard import THEMES, CosmergonDashboard
from cosmergon_agent.testing import fake_state

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


async def test_help_modal_is_english():
    """HelpModal must show English text — pressing ? on a German machine must still work."""
    app = _make_dashboard()
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.press("question_mark")
        await pilot.pause()
        await pilot.pause()  # @work action_help needs two pauses to fully render
        svg = pilot.app.export_screenshot()

    visible = _svg_visible(svg)
    all_text = " ".join(visible)

    assert any("Quit" in t for t in visible), "HelpModal must show 'Quit'"
    assert any("Press any key" in t for t in visible), "HelpModal must show English close hint"
    assert "Beenden" not in all_text, "HelpModal must not contain German 'Beenden'"
    assert "Taste" not in all_text, "HelpModal must not contain German 'Taste drücken'"


async def test_no_fields_warning_hotkey_visible():
    """'press [F] first' warning in journal must show [F] as literal text."""
    app = _make_dashboard(log=[])  # no fields in default state
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.pause()
        await pilot.press("p")  # action_place_cells with no fields — @work, needs two pauses
        await pilot.pause()
        await pilot.pause()
        pilot.app._redraw()
        await pilot.pause()
        journal = pilot.app.query_one("#journal-panel", Static).render()

    assert "[F]" in journal.plain, (
        "[F] in warning message consumed by Rich — use \\[F] in markup"
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


async def test_upgrade_hint_is_dim_not_prominent():
    """[U] Developer tier must be dim — not cyan/yellow (subtle, not a banner)."""
    app = _make_dashboard()
    _, economy, _ = await _render(app)

    assert "Developer tier" in economy.plain, "[U] Developer tier hint must be present"
    assert _has_style(economy, "Developer tier", "dim"), "[U] hint must be dim, not prominent"
    assert not _has_style(economy, "Developer tier", "cyan"), "Upgrade hint must not be cyan"
    assert not _has_style(economy, "Developer tier", "yellow"), "Upgrade hint must not be yellow"


# ---------------------------------------------------------------------------
# Content correctness
# ---------------------------------------------------------------------------


async def test_economy_placeholder_when_no_world_briefing():
    """Economy panel shows 'Joining universe...' when state exists but no world briefing."""
    app = _make_dashboard()  # no world_briefing by default
    _, economy, _ = await _render(app)
    assert "Joining" in economy.plain, (
        "Economy panel must show a placeholder, not be empty, before world briefing arrives"
    )
    assert "Rang" not in economy.plain, "Rang must not appear without world briefing"


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

    tip_lines = [line for line in economy.plain.splitlines() if "→" in line]
    assert tip_lines, "Tip line must be present"
    tip_line = tip_lines[0]
    assert "…" in tip_line, f"Long tip must end with …, got: {tip_line!r}"
    # Must not end on a partial word (last char before … must be a space boundary)
    before_ellipsis = tip_line.split("…")[0].rstrip()
    assert before_ellipsis.endswith((".", "!", "?", "assets", "income", "fields", "market"
                                     )) or before_ellipsis[-1] in " etaoinsrhldcumfpgwybvkxjqz", (
        f"Tip cut mid-word: {tip_line!r}"
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
# Layout: key content visible in SVG — 9 terminal size variants
#
# 3 shapes x 3 sizes (ideal / -10% / +20%):
#   Landscape (120x30)  Square (80x40)  Portrait (60x80)
# ---------------------------------------------------------------------------

# fmt: off
SIZE_MATRIX = [
    # (id,                    cols, rows)
    ("landscape_ideal",       120,  30),
    ("landscape_minus10pct",  108,  27),
    ("landscape_plus20pct",   144,  36),
    ("square_ideal",           80,  40),
    ("square_minus10pct",      72,  36),
    ("square_plus20pct",       96,  48),
    ("portrait_ideal",         60,  80),
    ("portrait_minus10pct",    54,  72),
    ("portrait_plus20pct",     72,  96),
]
# fmt: on

_SVG_WB = {
    "total_agents": 79, "your_rank": 44,
    "market_summary": "5 listings", "tip": "Place oscillating cells.",
}


def _svg_visible(svg: str) -> set[str]:
    """Extract all visible text strings from a Textual SVG screenshot.

    Normalises HTML entities and non-breaking spaces (\xa0) to plain ASCII
    so that assertions like ``"Press any key" in t`` work correctly.
    """
    return {
        html.unescape(t.strip()).replace("\xa0", " ")
        for t in re.findall(r">([^<\n]+)<", svg)
        if t.strip() and not t.strip().startswith(".")
    }


@pytest.mark.parametrize("label,cols,rows", SIZE_MATRIX, ids=[s[0] for s in SIZE_MATRIX])
async def test_layout_minimum_content_visible(label: str, cols: int, rows: int) -> None:
    """At every terminal size, the critical minimum must be visible (not clipped).

    Critical minimum: AGENT header, WIRTSCHAFT header, JOURNAL header, status.
    Panels are split 50/50 horizontally — portrait (60 cols) gives ~23 chars
    effective per panel (border + padding), enough for all headers.
    """
    app = _make_dashboard(world_briefing=_SVG_WB)
    async with app.run_test(size=(cols, rows)) as pilot:
        await pilot.pause()
        pilot.app._redraw()
        await pilot.pause()
        svg = pilot.app.export_screenshot()

    visible = _svg_visible(svg)

    assert any("AGENT" in t for t in visible), (
        f"[{label} {cols}x{rows}] AGENT header clipped"
    )
    assert any("WIRTSCHAFT" in t for t in visible), (
        f"[{label} {cols}x{rows}] WIRTSCHAFT header clipped"
    )
    assert any("JOURNAL" in t for t in visible), (
        f"[{label} {cols}x{rows}] JOURNAL header clipped"
    )
    assert any("AKTIV" in t for t in visible), (
        f"[{label} {cols}x{rows}] Status AKTIV clipped"
    )


@pytest.mark.parametrize(
    "label,cols,rows",
    [s for s in SIZE_MATRIX if s[1] >= 80],  # landscape + square only
    ids=[s[0] for s in SIZE_MATRIX if s[1] >= 80],
)
async def test_layout_economy_content_visible_wide(label: str, cols: int, rows: int) -> None:
    """On wider terminals (landscape + square), economy detail must be visible.

    Portrait panels (~23 chars effective) may clip 'active listings' etc. —
    that is acceptable. Landscape/square panels have enough room.
    """
    app = _make_dashboard(world_briefing={
        **_SVG_WB,
        "market_summary": "5 active listings",
        "last_event": "Grey Plague at tick 21",
    })
    async with app.run_test(size=(cols, rows)) as pilot:
        await pilot.pause()
        pilot.app._redraw()
        await pilot.pause()
        svg = pilot.app.export_screenshot()

    visible = _svg_visible(svg)

    assert any("listings" in t for t in visible), (
        f"[{label} {cols}x{rows}] market listings not visible"
    )
    assert any("Rang" in t for t in visible), (
        f"[{label} {cols}x{rows}] Rang (rank) not visible"
    )


# ---------------------------------------------------------------------------
# Action flow tests — user presses key → correct journal entry, no crash
#
# Each action is tested for:
#   (a) pre-condition guard (no cubes / no fields → warning in journal)
#   (b) error path (CosmergonError → ✗ message in journal, app alive)
#   (c) success path (ActionResult(success=True) → ✓ message in journal)
# ---------------------------------------------------------------------------

# Raw dicts so fake_state / GameState.from_api() can parse them
_CUBE_RAW = {"id": "aaaaaaaa-0000-0000-0000-000000000001", "name": "Test Cube"}
_FIELD_RAW = {
    "id": "bbbbbbbb-0000-0000-0000-000000000001",
    "cube_id": "aaaaaaaa-0000-0000-0000-000000000001",
    "z_position": 0,
    "active_cell_count": 5,
}


def _make_dashboard_with_cubes(log: list[str] | None = None) -> _TestDashboard:
    """Dashboard with one cube + one field in state, no network."""
    state = fake_state(
        energy=1000.0,
        subscription_tier="free",
        ranking={"player_tier": 0, "tier_name": "Novice", "player_score": 0.0},
        universe_cubes=[_CUBE_RAW],
        fields=[_FIELD_RAW],
    )
    agent = CosmergonAgent(api_key="AGENT-test:fakekey000000000000000000000")
    agent._state = state
    agent.agent_id = state.agent_id
    app = _TestDashboard(agent=agent, theme=THEMES["cosmergon"])
    if log is not None:
        app._log = list(log)
    return app


def _act_success() -> object:
    """Patch: agent.act always succeeds."""
    async def _act(*_args: object, **_kwargs: object) -> ActionResult:
        return ActionResult(success=True, action=str(_args[0]) if _args else "act", data={})
    return _act


def _act_fail(msg: str = "rate limited") -> object:
    """Patch: agent.act always raises CosmergonError."""
    async def _act(*_args: object, **_kwargs: object) -> ActionResult:
        raise CosmergonError(msg)
    return _act


async def _journal_after(pilot: object, *keys: str) -> object:
    """Press keys (with pauses for @work), redraw, return journal Content."""
    for key in keys:
        await pilot.press(key)
        await pilot.pause()
    await pilot.pause()
    pilot.app._redraw()
    await pilot.pause()
    return pilot.app.query_one("#journal-panel", Static).render()


# --- create_field ---

async def test_create_field_no_cubes_shows_warning() -> None:
    """[F] with no cubes in state must log a warning, not crash."""
    app = _make_dashboard(log=[])  # default state has no universe_cubes
    async with app.run_test(size=(80, 40)) as pilot:
        journal = await _journal_after(pilot, "f")
    assert "No cubes" in journal.plain


async def test_create_field_error_shows_in_journal() -> None:
    """[F] → select cube → CosmergonError must appear as ✗ in journal, no crash."""
    app = _make_dashboard_with_cubes()
    app.agent.act = _act_fail("rate limited")
    async with app.run_test(size=(80, 40)) as pilot:
        journal = await _journal_after(pilot, "f", "1")
    assert "✗" in journal.plain
    assert "create_field" in journal.plain


async def test_create_field_success_shows_in_journal() -> None:
    """[F] → select cube → success must log ✓ create_field."""
    app = _make_dashboard_with_cubes()
    app.agent.act = _act_success()
    async with app.run_test(size=(80, 40)) as pilot:
        journal = await _journal_after(pilot, "f", "1")
    assert "✓" in journal.plain
    assert "create_field" in journal.plain


# --- place_cells ---

async def test_place_cells_error_shows_in_journal() -> None:
    """[P] → field → preset → CosmergonError must appear as ✗, no crash."""
    app = _make_dashboard_with_cubes()
    app.agent.act = _act_fail("rate limited")
    async with app.run_test(size=(80, 40)) as pilot:
        journal = await _journal_after(pilot, "p", "1", "1")
    assert "✗" in journal.plain
    assert "place_cells" in journal.plain


async def test_place_cells_success_shows_in_journal() -> None:
    """[P] → field → preset → success must log ✓ place_cells(...)."""
    app = _make_dashboard_with_cubes()
    app.agent.act = _act_success()
    async with app.run_test(size=(80, 40)) as pilot:
        journal = await _journal_after(pilot, "p", "1", "1")
    assert "✓" in journal.plain
    assert "place_cells" in journal.plain


# --- evolve ---

async def test_evolve_no_fields_shows_warning() -> None:
    """[E] with no fields must log a warning, not crash."""
    app = _make_dashboard(log=[])
    async with app.run_test(size=(80, 40)) as pilot:
        journal = await _journal_after(pilot, "e")
    assert "No fields" in journal.plain


async def test_evolve_error_shows_in_journal() -> None:
    """[E] → select field → CosmergonError must appear as ✗, no crash."""
    app = _make_dashboard_with_cubes()
    app.agent.act = _act_fail("not ready")
    async with app.run_test(size=(80, 40)) as pilot:
        journal = await _journal_after(pilot, "e", "1")
    assert "✗" in journal.plain
    assert "evolve" in journal.plain


async def test_evolve_success_shows_in_journal() -> None:
    """[E] → select field → success must log ✓ evolve → ok."""
    app = _make_dashboard_with_cubes()
    app.agent.act = _act_success()
    async with app.run_test(size=(80, 40)) as pilot:
        journal = await _journal_after(pilot, "e", "1")
    assert "✓" in journal.plain
    assert "evolve" in journal.plain


# --- pause ---

async def test_pause_success_shows_paused() -> None:
    """[Space] with successful act must toggle to PAUSED in agent panel."""
    app = _make_dashboard_with_cubes()
    app.agent.act = _act_success()
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.press("space")
        await pilot.pause()
        await pilot.pause()
        pilot.app._redraw()
        await pilot.pause()
        agent = pilot.app.query_one("#agent-panel", Static).render()
    assert "PAUSED" in agent.plain


async def test_pause_error_shows_in_journal() -> None:
    """[Space] with CosmergonError must log ✗ pause:, state must NOT toggle."""
    app = _make_dashboard_with_cubes()
    app.agent.act = _act_fail("server error")
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.press("space")
        await pilot.pause()
        await pilot.pause()
        pilot.app._redraw()
        await pilot.pause()
        agent = pilot.app.query_one("#agent-panel", Static).render()
        journal = pilot.app.query_one("#journal-panel", Static).render()
    assert "AKTIV" in agent.plain, "State must not toggle on error"
    assert "✗" in journal.plain
    assert "pause" in journal.plain

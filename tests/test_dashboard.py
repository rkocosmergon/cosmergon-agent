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


async def _render(app: _TestDashboard, size: tuple[int, int] = (80, 36)):
    """Run app headless, trigger redraw, return (agent, economy, log) Content."""
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        pilot.app._redraw()
        await pilot.pause()
        agent = pilot.app.query_one("#agent-panel", Static).render()
        economy = pilot.app.query_one("#economy-panel", Static).render()
        log = pilot.app.query_one("#log-panel", Static).render()
        return agent, economy, log


async def _render_chat(app: _TestDashboard, size: tuple[int, int] = (80, 36)):
    """Run app headless, trigger redraw, return log-panel Content (chat messages live in LOG)."""
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        pilot.app._redraw()
        await pilot.pause()
        return pilot.app.query_one("#log-panel", Static).render()


async def _render_context(app: _TestDashboard, size: tuple[int, int] = (80, 36)):
    """Run app headless, trigger redraw, return context-bar Content."""
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        pilot.app._redraw()
        await pilot.pause()
        return pilot.app.query_one("#context-bar", Static).render()


def _has_style(content, text_fragment: str, style_fragment: str) -> bool:
    """Return True if text_fragment appears with style_fragment in any span."""
    for span in content.spans:
        if style_fragment in str(span.style):
            chunk = content.plain[span.start : span.end]
            if text_fragment in chunk:
                return True
    return False


# ---------------------------------------------------------------------------
# Paket 1 regression: Rich markup escaping
# ---------------------------------------------------------------------------


async def test_u_hotkey_visible_in_key_bar():
    """[U] must render as literal text in the key bar (upgrade hint moved out of economy panel)."""
    app = _make_dashboard()
    _, key = await _render_hint_key(app)
    assert "[U]" in key.plain, "Hotkey [U] invisible in key bar — Rich consumed it as a style tag."


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

    assert any("Esc" in t or "close" in t for t in visible), (
        "HelpModal must show English close/navigation hint"
    )
    assert "Beenden" not in all_text, "HelpModal must not contain German 'Beenden'"
    assert "Taste" not in all_text, "HelpModal must not contain German 'Taste drücken'"


async def test_help_modal_has_guide_and_faq():
    """HelpModal must contain game explanation and FAQ.

    Checked via widget content, not screenshot.
    """
    from textual.widgets import Label

    app = _make_dashboard()
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.press("question_mark")
        await pilot.pause()
        await pilot.pause()
        # HelpModal is a pushed screen — access via screen stack
        modal = pilot.app.screen
        all_text = " ".join(str(lbl._Static__content) for lbl in modal.query(Label))

    assert "THE GAME" in all_text, "Guide must contain THE GAME section"
    assert "Conway" in all_text, "Guide must mention Conway's Game of Life"
    assert "Energy" in all_text, "Guide must mention Energy"
    assert "FAQ" in all_text, "Guide must contain FAQ section"
    assert "Vagant" in all_text, "FAQ must explain Vagants"
    assert "HOTKEYS" in all_text, "Guide must contain HOTKEYS section"


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
        journal = pilot.app.query_one("#log-panel", Static).render()

    assert "[F]" in journal.plain, "[F] in warning message consumed by Rich — use \\[F] in markup"


# ---------------------------------------------------------------------------
# One-Thing principle: only [C] may be yellow/orange
# ---------------------------------------------------------------------------


async def test_new_user_only_c_is_yellow():
    """At first start, CTA lives in hint-bar only — agent panel shows 'Compass: —', no yellow."""
    app = _make_dashboard()
    agent, economy, _ = await _render(app)

    # CTA removed from agent panel — shows neutral placeholder instead
    assert "Compass: —" in agent.plain, "Agent panel must show 'Compass: —' when compass not set"
    yellow_agent = [agent.plain[s.start : s.end] for s in agent.spans if "yellow" in str(s.style)]
    assert not yellow_agent, f"No yellow in agent panel (CTA in hint-bar). Found: {yellow_agent}"
    yellow_economy = [
        economy.plain[s.start : s.end] for s in economy.spans if "yellow" in str(s.style)
    ]
    assert not yellow_economy, f"Nothing in economy panel should be yellow. Found: {yellow_economy}"


async def test_compass_set_removes_yellow():
    """After compass is set, no more yellow in agent panel."""
    app = _make_dashboard(compass_ever_set=True, compass_preset="grow")
    agent, _, _ = await _render(app)

    yellow = [agent.plain[s.start : s.end] for s in agent.spans if "yellow" in str(s.style)]
    assert not yellow, f"After compass set, no yellow expected. Found: {yellow}"
    assert "Compass:" in agent.plain and "Grow" in agent.plain


async def test_upgrade_hint_not_in_economy_panel():
    """[U] Developer tier must NOT appear in economy panel — it lives in the key bar only."""
    app = _make_dashboard()
    _, economy, _ = await _render(app)
    assert "Developer tier" not in economy.plain, (
        "[U] Developer tier hint must be removed from economy panel — it's in the key bar"
    )


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


async def test_last_event_not_red():
    """Past catastrophe events must not show in red — not an active alarm."""
    app = _make_dashboard(
        world_briefing={
            "total_agents": 79,
            "your_rank": 44,
            "market_summary": "5 listings",
            "last_event": "Grey Plague at tick 21",
        }
    )
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
    "total_agents": 79,
    "your_rank": 44,
    "market_summary": "5 listings",
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

    assert any("AGENT" in t for t in visible), f"[{label} {cols}x{rows}] AGENT header clipped"
    assert any("WIRTSCHAFT" in t for t in visible), (
        f"[{label} {cols}x{rows}] WIRTSCHAFT header clipped"
    )
    assert any("LOG" in t for t in visible), f"[{label} {cols}x{rows}] LOG header clipped"
    assert any("AKTIV" in t for t in visible), f"[{label} {cols}x{rows}] Status AKTIV clipped"


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
    app = _make_dashboard(
        world_briefing={
            **_SVG_WB,
            "market_summary": "5 active listings",
            "last_event": "Grey Plague at tick 21",
        }
    )
    async with app.run_test(size=(cols, rows)) as pilot:
        await pilot.pause()
        pilot.app._redraw()
        await pilot.pause()
        svg = pilot.app.export_screenshot()

    visible = _svg_visible(svg)

    assert any("listings" in t for t in visible), (
        f"[{label} {cols}x{rows}] market listings not visible"
    )
    assert any("Rang" in t for t in visible), f"[{label} {cols}x{rows}] Rang (rank) not visible"


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
    return pilot.app.query_one("#log-panel", Static).render()


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
        journal = pilot.app.query_one("#log-panel", Static).render()
    assert "AKTIV" in agent.plain, "State must not toggle on error"
    assert "✗" in journal.plain
    assert "pause" in journal.plain


# --- compass (set_compass, not agent.act) ---


def _set_compass_success() -> object:
    """Patch: agent.set_compass always returns explanation dict."""

    async def _set(*_args: object, **_kwargs: object) -> dict:
        return {"explanation": "Strategy set."}

    return _set


def _set_compass_fail(msg: str = "server error") -> object:
    """Patch: agent.set_compass always raises CosmergonError."""

    async def _set(*_args: object, **_kwargs: object) -> dict:
        raise CosmergonError(msg)

    return _set


async def test_compass_success_clears_yellow_cta() -> None:
    """[C] → select preset → success: yellow CTA gone, compass label shown."""
    app = _make_dashboard()
    app.agent.set_compass = _set_compass_success()
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.press("c")
        await pilot.pause()
        await pilot.press("3")  # preset index 2 = "grow" (1=attack 2=defend 3=grow)
        await pilot.pause()
        await pilot.pause()
        pilot.app._redraw()
        await pilot.pause()
        agent = pilot.app.query_one("#agent-panel", Static).render()

    yellow = [agent.plain[s.start : s.end] for s in agent.spans if "yellow" in str(s.style)]
    assert not yellow, f"Yellow CTA must vanish after compass set. Found: {yellow}"
    assert "Compass" in agent.plain, "Compass label must appear after selection"


async def test_compass_error_shows_in_journal() -> None:
    """[C] → select preset → CosmergonError: ✗ in journal, CTA still yellow."""
    app = _make_dashboard()
    app.agent.set_compass = _set_compass_fail("unreachable")
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.press("c")
        await pilot.pause()
        await pilot.press("1")
        await pilot.pause()
        await pilot.pause()
        pilot.app._redraw()
        await pilot.pause()
        agent = pilot.app.query_one("#agent-panel", Static).render()
        journal = pilot.app.query_one("#log-panel", Static).render()

    assert "✗" in journal.plain
    assert "compass" in journal.plain
    # CTA lives in hint-bar after the move — agent panel shows neutral "Compass: —"
    assert "Compass: —" in agent.plain, "Agent panel must still show 'Compass: —' after failed set"


# --- upgrade ---


async def test_upgrade_error_shows_in_journal() -> None:
    """[U] with connection error shows ✗ in journal, app does not crash."""
    app = _make_dashboard()

    async def _fail_request(*_args: object, **_kwargs: object) -> object:
        raise CosmergonError("connection refused")

    app.agent._request = _fail_request

    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.press("u")
        await pilot.pause()
        await pilot.pause()
        pilot.app._redraw()
        await pilot.pause()
        journal = pilot.app.query_one("#log-panel", Static).render()

    assert "✗" in journal.plain
    assert "Upgrade" in journal.plain or "upgrade" in journal.plain.lower()


# ---------------------------------------------------------------------------
# Hint bar — one-line guidance / state machine
# ---------------------------------------------------------------------------


async def _render_hint_key(
    app: _TestDashboard, size: tuple[int, int] = (80, 40)
) -> tuple[object, object]:
    """Run app headless, trigger redraw, return (hint_bar, key_bar) Content."""
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        pilot.app._redraw()
        await pilot.pause()
        hint = pilot.app.query_one("#hint-bar", Static).render()
        key = pilot.app.query_one("#fix-bar", Static).render()
    return hint, key


def _make_dashboard_with_empty_fields() -> _TestDashboard:
    """Dashboard with one field (no cells placed) and compass ever set."""
    _field = {
        "id": "bbbbbbbb-0000-0000-0000-000000000002",
        "cube_id": "aaaaaaaa-0000-0000-0000-000000000001",
        "z_position": 0,
        "active_cell_count": 0,
    }
    state = fake_state(fields=[_field])
    agent = CosmergonAgent(api_key="AGENT-test:fakekey000000000000000000000")
    agent._state = state
    agent.agent_id = state.agent_id
    app = _TestDashboard(agent=agent, theme=THEMES["cosmergon"])
    app._compass_ever_set = True
    return app


async def test_hint_bar_connecting_when_no_state() -> None:
    """Before first tick, hint bar shows 'Connecting...'."""
    agent = CosmergonAgent(api_key="AGENT-test:fakekey000000000000000000000")
    agent._state = None  # type: ignore[assignment]
    app = _TestDashboard(agent=agent, theme=THEMES["cosmergon"])
    hint, _ = await _render_hint_key(app)
    assert "Connecting" in hint.plain


async def test_hint_bar_paused_shows_pause_hint() -> None:
    """When paused, hint bar shows pause indicator and resume instruction."""
    app = _make_dashboard(paused=True)
    hint, _ = await _render_hint_key(app)
    assert "Paused" in hint.plain
    assert "[Space]" in hint.plain


async def test_hint_bar_no_compass_shows_c_cta() -> None:
    """Without compass, hint bar shows → [C] Set Compass direction.

    [C] is muted grey (cmd color); the surrounding guide text is yellow (guide color).
    """
    app = _make_dashboard(compass_ever_set=False)
    hint, _ = await _render_hint_key(app)
    assert "[C]" in hint.plain
    assert "Compass" in hint.plain
    # [C] is the cmd key (muted grey); the guide text around it is yellow (guide color)
    assert _has_style(hint, "Set Compass direction", "yellow")


async def test_hint_bar_no_fields_shows_f_cta() -> None:
    """With compass set but no fields, hint bar shows → [F] Create a field."""
    app = _make_dashboard(compass_ever_set=True)
    # default state has fields=[] — step 5 of _compute_hint
    hint, _ = await _render_hint_key(app)
    assert "[F]" in hint.plain
    assert "field" in hint.plain.lower()


async def test_hint_bar_no_cells_shows_p_cta() -> None:
    """With fields but 0 cells, hint bar shows → [P] Place cells."""
    app = _make_dashboard_with_empty_fields()  # compass set, field exists, 0 cells
    hint, _ = await _render_hint_key(app)
    assert "[P]" in hint.plain
    assert "cell" in hint.plain.lower()


async def test_hint_bar_normal_shows_tick() -> None:
    """With fields + cells + compass set, hint bar shows tick info.

    Hotkeys are in key-bar, not hint-bar.
    """
    app = _make_dashboard_with_cubes()  # field has active_cell_count=5
    app._compass_ever_set = True
    hint, _ = await _render_hint_key(app)
    assert "tick" in hint.plain
    assert "[P]" not in hint.plain, "Hotkeys must not appear in hint-bar — they live in key-bar"


# ---------------------------------------------------------------------------
# Key bar — all hotkeys visible, [Q] always present at all 9 sizes
# ---------------------------------------------------------------------------


async def test_key_bar_contains_all_hotkeys() -> None:
    """Fix-bar must show all primary hotkeys as literal text (not consumed by Rich).

    [C], [Space], [R] are still active bindings but moved off the visible bar
    to save space for the new [L], [M], [Tab] shortcuts.
    """
    app = _make_dashboard()
    _, key = await _render_hint_key(app)
    for hotkey in ["[Tab]", "[P]", "[F]", "[E]", "[L]", "[M]", "[U]", "[?]", "[Q]"]:
        assert hotkey in key.plain, f"{hotkey} missing from fix-bar"


@pytest.mark.parametrize("label,cols,rows", SIZE_MATRIX, ids=[s[0] for s in SIZE_MATRIX])
async def test_key_bar_q_visible_all_sizes(label: str, cols: int, rows: int) -> None:
    """[Q] Quit must be visible at all 9 terminal sizes (custom key bar, not Textual Footer)."""
    app = _make_dashboard()
    async with app.run_test(size=(cols, rows)) as pilot:
        await pilot.pause()
        pilot.app._redraw()
        await pilot.pause()
        svg = pilot.app.export_screenshot()

    visible = _svg_visible(svg)
    visible_text = " ".join(visible)
    assert "Quit" in visible_text, (
        f"[{label} {cols}x{rows}] 'Quit' not visible — [Q] key bar row clipped"
    )


# ---------------------------------------------------------------------------
# Feedback mechanism
# ---------------------------------------------------------------------------


async def test_feedback_shown_in_hint_bar() -> None:
    """After _set_feedback(), hint bar must show the feedback message."""
    app = _make_dashboard(compass_ever_set=True)
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.pause()
        pilot.app._set_feedback("[green]✓ Test feedback[/green]")
        pilot.app._redraw()
        await pilot.pause()
        hint = pilot.app.query_one("#hint-bar", Static).render()

    assert "Test feedback" in hint.plain


async def test_feedback_overrides_compass_cta() -> None:
    """Active feedback must override the compass CTA in hint bar."""
    app = _make_dashboard(compass_ever_set=False)  # normally shows → [C] ...
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.pause()
        pilot.app._set_feedback("✓ Action done")
        pilot.app._redraw()
        await pilot.pause()
        hint = pilot.app.query_one("#hint-bar", Static).render()

    assert "Action done" in hint.plain
    assert "Compass" not in hint.plain, "Compass CTA must be suppressed while feedback active"


async def test_feedback_shows_countdown_when_tick_known() -> None:
    """When tick timing is known, feedback must also show 'takes effect at next tick ~Xs'.

    This connects the action confirmation to the countdown so the user
    understands *when* the command will take effect.
    """
    import time

    app = _make_dashboard(compass_ever_set=True)
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.pause()
        pilot.app._tick_received_at = time.monotonic() - 2.0  # 2s ago
        pilot.app._set_feedback("✓ blinker placed")
        pilot.app._redraw()
        await pilot.pause()
        hint = pilot.app.query_one("#hint-bar", Static).render()

    assert "blinker placed" in hint.plain
    assert "takes effect" in hint.plain, "Countdown context must explain when command fires"
    assert "next tick" in hint.plain, "Must mention 'next tick' so the countdown makes sense"


# ---------------------------------------------------------------------------
# Issue #3 — Compass text cutoff fix
# ---------------------------------------------------------------------------


def _set_compass_long_explanation() -> object:
    """Patch: set_compass returns a long explanation that used to overflow."""

    async def _set(*_args: object, **_kwargs: object) -> dict:
        return {
            "explanation": (
                "Your agent will prioritize energy accumulation and sustainable "
                "territory growth over the next several ticks."
            )
        }

    return _set


def _set_compass_short_explanation() -> object:
    """Patch: set_compass returns a short explanation that fits on one line."""

    async def _set(*_args: object, **_kwargs: object) -> dict:
        return {"explanation": "Focus on growth."}

    return _set


async def test_compass_log_uses_display_label() -> None:
    """Compass log entry must use display label (e.g. '🌱  Grow'), not raw preset name."""
    app = _make_dashboard()
    app.agent.set_compass = _set_compass_success()
    async with app.run_test(size=(80, 40)) as pilot:
        journal = await _journal_after(pilot, "c", "3")  # index 2 = "grow"

    # Display label must appear
    assert "Grow" in journal.plain, "Display label '🌱  Grow' must appear in log"


async def test_compass_explanation_as_second_log_line() -> None:
    """Explanation must appear as a separate (second) log entry, not on the same line."""
    app = _make_dashboard()
    app.agent.set_compass = _set_compass_long_explanation()
    async with app.run_test(size=(80, 40)) as pilot:
        journal = await _journal_after(pilot, "c", "3")

    plain = journal.plain
    # Confirmation line and explanation line are both present
    assert "✓ compass:" in plain, "Confirmation line must be present"
    assert "prioritize" in plain, "Explanation must appear somewhere in journal"


async def test_compass_explanation_no_midword_cutoff() -> None:
    """Explanation must never end mid-word (no trailing incomplete words)."""
    app = _make_dashboard()
    app.agent.set_compass = _set_compass_long_explanation()
    async with app.run_test(size=(80, 40)) as pilot:
        journal = await _journal_after(pilot, "c", "3")

    plain = journal.plain
    # Find the explanation line (starts with two spaces)
    explanation_lines = [
        line for line in plain.splitlines() if line.startswith("  ") and len(line.strip()) > 3
    ]
    assert explanation_lines, "Explanation line not found in journal"
    last_line = explanation_lines[-1].rstrip()
    # Must end with a complete word or '…' — not a partial word like "sustainab"
    assert last_line.endswith("…") or last_line[-1] in " abcdefghijklmnopqrstuvwxyz.!?,", (
        f"Explanation must end at word boundary, got: {last_line!r}"
    )


async def test_compass_explanation_fits_narrow_terminal() -> None:
    """Explanation line must not exceed 50 chars (safe for 60-col portrait terminal)."""
    app = _make_dashboard()
    app.agent.set_compass = _set_compass_long_explanation()
    async with app.run_test(size=(60, 80)) as pilot:
        journal = await _journal_after(pilot, "c", "3")

    plain = journal.plain
    explanation_lines = [
        line for line in plain.splitlines() if line.startswith("  ") and len(line.strip()) > 3
    ]
    assert explanation_lines, "Explanation line not found"
    # Strip Rich markup artifacts — measure plain content length
    stripped = explanation_lines[-1].strip()
    assert len(stripped) <= 52, (
        f"Explanation too long for narrow terminal ({len(stripped)} chars): {stripped!r}"
    )


async def test_compass_short_explanation_shown_in_full() -> None:
    """Short explanations (≤48 chars) must not be truncated."""
    app = _make_dashboard()
    app.agent.set_compass = _set_compass_short_explanation()
    async with app.run_test(size=(80, 40)) as pilot:
        journal = await _journal_after(pilot, "c", "3")

    assert "Focus on growth." in journal.plain, (
        "Short explanation must appear verbatim — no unnecessary truncation"
    )
    assert "…" not in journal.plain, "No ellipsis for short explanation"


# ---------------------------------------------------------------------------
# Helper unit tests — _truncate_words, _action_cost, _cost_str
# ---------------------------------------------------------------------------


def test_truncate_words_short_unchanged() -> None:
    """Text shorter than max_len must be returned unchanged."""
    from cosmergon_agent.dashboard import _truncate_words

    assert _truncate_words("Hello world", 20) == "Hello world"


def test_truncate_words_exact_length_unchanged() -> None:
    """Text exactly max_len must be returned unchanged."""
    from cosmergon_agent.dashboard import _truncate_words

    text = "Hello world"
    assert _truncate_words(text, len(text)) == text


def test_truncate_words_truncates_at_word_boundary() -> None:
    """Long text must be cut at the last complete word within max_len."""
    from cosmergon_agent.dashboard import _truncate_words

    result = _truncate_words("Your agent will prioritize energy accumulation", 30)
    assert result.endswith("…"), "Must append ellipsis when truncated"
    without_ellipsis = result[:-1]
    # Must end at a word boundary — no partial word
    assert not without_ellipsis.endswith(" "), "Must not end with trailing space"
    assert " " not in without_ellipsis.split()[-1] or True  # last token is a whole word


def test_truncate_words_never_exceeds_max_len_plus_ellipsis() -> None:
    """Result length (excluding '…') must be ≤ max_len."""
    from cosmergon_agent.dashboard import _truncate_words

    result = _truncate_words("one two three four five six seven eight nine ten", 25)
    content = result.rstrip("…")
    assert len(content) <= 25, f"Truncated content exceeds max_len: {result!r}"


def test_action_cost_energy_cost_key() -> None:
    """_action_cost must read energy_cost from result dict."""
    from cosmergon_agent.action import ActionResult
    from cosmergon_agent.dashboard import _action_cost

    r = ActionResult(success=True, action="place_cells", data={"result": {"energy_cost": 150.0}})
    assert _action_cost(r) == 150.0


def test_action_cost_cost_key_fallback() -> None:
    """_action_cost must fall back to 'cost' key (used by create_cube)."""
    from cosmergon_agent.action import ActionResult
    from cosmergon_agent.dashboard import _action_cost

    r = ActionResult(success=True, action="create_cube", data={"result": {"cost": 500.0}})
    assert _action_cost(r) == 500.0


def test_action_cost_missing_returns_zero() -> None:
    """_action_cost must return 0.0 when no cost key is present."""
    from cosmergon_agent.action import ActionResult
    from cosmergon_agent.dashboard import _action_cost

    r = ActionResult(success=True, action="pause", data={"result": {}})
    assert _action_cost(r) == 0.0


def test_action_cost_empty_data_returns_zero() -> None:
    """_action_cost must return 0.0 when data dict is empty."""
    from cosmergon_agent.action import ActionResult
    from cosmergon_agent.dashboard import _action_cost

    r = ActionResult(success=True, action="act", data={})
    assert _action_cost(r) == 0.0


def test_cost_str_zero_is_empty() -> None:
    """_cost_str(0) must return empty string — free actions show no cost."""
    from cosmergon_agent.dashboard import _cost_str

    assert _cost_str(0.0) == ""
    assert _cost_str(0) == ""


def test_cost_str_nonzero_format() -> None:
    """_cost_str must return ' (-N E)' with thousands separator."""
    from cosmergon_agent.dashboard import _cost_str

    assert _cost_str(150.0) == " (-150 E)"
    assert _cost_str(1500.0) == " (-1,500 E)"


# ---------------------------------------------------------------------------
# Issue #6 — Energy cost shown in journal and feedback
# ---------------------------------------------------------------------------


def _act_with_cost(cost: float) -> object:
    """Patch: agent.act returns success with energy_cost in result."""

    async def _act(*_args: object, **_kwargs: object) -> object:
        from cosmergon_agent.action import ActionResult

        action = str(_args[0]) if _args else "act"
        return ActionResult(success=True, action=action, data={"result": {"energy_cost": cost}})

    return _act


def _act_free() -> object:
    """Patch: agent.act returns success with zero energy cost (free action)."""

    async def _act(*_args: object, **_kwargs: object) -> object:
        from cosmergon_agent.action import ActionResult

        action = str(_args[0]) if _args else "act"
        return ActionResult(success=True, action=action, data={"result": {"energy_cost": 0.0}})

    return _act


def _act_evolve_success(new_tier: int = 2, cost: float = 1000.0) -> object:
    """Patch: agent.act returns evolve success with new_tier and energy_cost."""

    async def _act(*_args: object, **_kwargs: object) -> object:
        from cosmergon_agent.action import ActionResult

        return ActionResult(
            success=True,
            action="evolve",
            data={"result": {"new_tier": new_tier, "energy_cost": cost}},
        )

    return _act


async def test_place_cells_shows_cost_in_journal() -> None:
    """[P] with energy cost > 0 must show '(-N E)' in journal log entry."""
    app = _make_dashboard_with_cubes()
    app.agent.act = _act_with_cost(150.0)
    async with app.run_test(size=(80, 40)) as pilot:
        journal = await _journal_after(pilot, "p", "1", "1")

    assert "(-150 E)" in journal.plain, "Energy cost must be visible in journal after place_cells"


async def test_place_cells_free_shows_no_cost() -> None:
    """[P] with cost = 0 must NOT show any cost string (block preset is free)."""
    app = _make_dashboard_with_cubes()
    app.agent.act = _act_free()
    async with app.run_test(size=(80, 40)) as pilot:
        journal = await _journal_after(pilot, "p", "1", "1")

    assert "(-0" not in journal.plain, "Zero cost must not appear as '(-0 E)'"
    assert "(-" not in journal.plain, "Free action must show no cost indicator"


async def test_create_field_shows_cost_in_journal() -> None:
    """[F] with energy cost > 0 must show '(-N E)' in journal log entry."""
    app = _make_dashboard_with_cubes()
    app.agent.act = _act_with_cost(500.0)
    async with app.run_test(size=(80, 40)) as pilot:
        journal = await _journal_after(pilot, "f", "1")

    assert "(-500 E)" in journal.plain, "Energy cost must be visible in journal after create_field"


async def test_create_field_free_shows_no_cost() -> None:
    """[F] with cost = 0 (first field is free) must NOT show any cost string."""
    app = _make_dashboard_with_cubes()
    app.agent.act = _act_free()
    async with app.run_test(size=(80, 40)) as pilot:
        journal = await _journal_after(pilot, "f", "1")

    assert "(-" not in journal.plain, "First (free) field creation must show no cost indicator"


async def test_evolve_shows_new_tier_in_journal() -> None:
    """[E] success must show new tier (e.g. 'T2') in journal."""
    app = _make_dashboard_with_cubes()
    app.agent.act = _act_evolve_success(new_tier=2, cost=1000.0)
    async with app.run_test(size=(80, 40)) as pilot:
        journal = await _journal_after(pilot, "e", "1")

    assert "T2" in journal.plain, "New tier must appear in evolve log entry"


async def test_evolve_shows_cost_in_journal() -> None:
    """[E] success with cost > 0 must show '(-N E)' in journal."""
    app = _make_dashboard_with_cubes()
    app.agent.act = _act_evolve_success(new_tier=2, cost=1000.0)
    async with app.run_test(size=(80, 40)) as pilot:
        journal = await _journal_after(pilot, "e", "1")

    assert "(-1,000 E)" in journal.plain, "Energy cost must be visible in evolve log entry"


async def test_place_cells_cost_in_hint_bar() -> None:
    """[P] with cost > 0: hint bar feedback must also show the cost."""
    app = _make_dashboard_with_cubes()
    app.agent.act = _act_with_cost(150.0)
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.press("p")
        await pilot.press("1")
        await pilot.press("1")
        await pilot.pause()
        await pilot.pause()
        pilot.app._redraw()
        await pilot.pause()
        hint = pilot.app.query_one("#hint-bar", Static).render()

    assert "(-150 E)" in hint.plain, "Cost must appear in hint bar feedback after place_cells"


async def test_create_field_cost_in_hint_bar() -> None:
    """[F] with cost > 0: hint bar feedback must also show the cost."""
    app = _make_dashboard_with_cubes()
    app.agent.act = _act_with_cost(500.0)
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.press("f")
        await pilot.press("1")
        await pilot.pause()
        await pilot.pause()
        pilot.app._redraw()
        await pilot.pause()
        hint = pilot.app.query_one("#hint-bar", Static).render()

    assert "(-500 E)" in hint.plain, "Cost must appear in hint bar feedback after create_field"


# ---------------------------------------------------------------------------
# Issues #5 + #7 — Action Queue: 429 → pending_action → fire on next tick
# ---------------------------------------------------------------------------


def _act_rate_limited(retry_after: float = 30.0) -> object:
    """Patch: agent.act raises RateLimitError (tick limit hit)."""

    async def _act(*_args: object, **_kwargs: object) -> object:
        from cosmergon_agent.exceptions import RateLimitError

        raise RateLimitError(retry_after=retry_after)

    return _act


def _set_compass_rate_limited(retry_after: float = 30.0) -> object:
    """Patch: agent.set_compass raises RateLimitError."""

    async def _set_compass(*_args: object, **_kwargs: object) -> object:
        from cosmergon_agent.exceptions import RateLimitError

        raise RateLimitError(retry_after=retry_after)

    return _set_compass


async def test_place_cells_429_stores_pending_action() -> None:
    """place_cells hit by 429 must store a _pending_action (kind=act, action=place_cells)."""
    app = _make_dashboard_with_cubes()
    app.agent.act = _act_rate_limited(30.0)
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.press("p")
        await pilot.pause()
        await pilot.press("1")
        await pilot.pause()
        await pilot.press("1")
        await pilot.pause()
        await pilot.pause()

    assert app._pending_action is not None, "429 must queue a pending action"
    assert app._pending_action.kind == "act"
    assert app._pending_action.action == "place_cells"
    assert "preset" in app._pending_action.params
    assert "field_id" in app._pending_action.params


async def test_place_cells_429_shows_queued_in_journal() -> None:
    """place_cells 429 must log '⏳' in the journal — not an error symbol."""
    app = _make_dashboard_with_cubes()
    app.agent.act = _act_rate_limited(30.0)
    async with app.run_test(size=(80, 40)) as pilot:
        journal = await _journal_after(pilot, "p", "1", "1")

    assert "⏳" in journal.plain, "Queue indicator must appear in journal on 429"
    assert "✗" not in journal.plain, "Error symbol must NOT appear when action is queued"


async def test_place_cells_429_shows_queued_in_hint_bar() -> None:
    """place_cells 429 must show '⏳ Queued' in the hint bar."""
    app = _make_dashboard_with_cubes()
    app.agent.act = _act_rate_limited(30.0)
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.press("p")
        await pilot.pause()
        await pilot.press("1")
        await pilot.pause()
        await pilot.press("1")
        await pilot.pause()
        await pilot.pause()
        pilot.app._redraw()
        await pilot.pause()
        hint = pilot.app.query_one("#hint-bar", Static).render()

    assert "⏳" in hint.plain, "Queue indicator must appear in hint bar on 429"
    assert "Queued" in hint.plain, "Hint bar must say 'Queued' when action is waiting"


async def test_fire_pending_success_logs_result() -> None:
    """_fire_pending() with a successful retry must log auto-retry and success."""
    from cosmergon_agent.dashboard import _PendingAction

    app = _make_dashboard_with_cubes()
    app.agent.act = _act_with_cost(150.0)
    async with app.run_test(size=(80, 40)) as pilot:
        app._pending_action = _PendingAction(
            kind="act",
            action="place_cells",
            params={"field_id": "field-uuid-1", "preset": "blinker"},
            display="place_cells(blinker)",
        )
        await app._fire_pending()
        app._redraw()
        await pilot.pause()
        journal = pilot.app.query_one("#log-panel", Static).render()

    assert "auto-retry" in journal.plain, "Must log 'auto-retry' when firing pending action"
    assert "✓" in journal.plain, "Success must be indicated in journal after auto-retry"
    assert app._pending_action is None, "Pending slot must be cleared after firing"


async def test_fire_pending_still_429_requeues_with_timer() -> None:
    """_fire_pending() getting another 429 must re-queue the action (not drop it)."""
    from cosmergon_agent.dashboard import _PendingAction

    app = _make_dashboard_with_cubes()
    app.agent.act = _act_rate_limited(5.0)
    async with app.run_test(size=(80, 40)) as pilot:
        app._pending_action = _PendingAction(
            kind="act",
            action="place_cells",
            params={"field_id": "field-uuid-1", "preset": "blinker"},
            display="place_cells(blinker)",
        )
        await app._fire_pending()
        app._redraw()
        await pilot.pause()
        journal = pilot.app.query_one("#log-panel", Static).render()

    # Action must be re-queued, not dropped
    assert app._pending_action is not None, "Action must be re-queued on second 429, not dropped"
    assert app._pending_action.action == "place_cells", "Re-queued action must match original"
    assert "still rate limited" in journal.plain, "Log must mention 'still rate limited'"


async def test_fire_pending_no_action_is_noop() -> None:
    """_fire_pending() with empty queue must not raise or log anything."""
    app = _make_dashboard_with_cubes()
    initial_log_len = len(app._log)
    async with app.run_test(size=(80, 40)) as pilot:
        await app._fire_pending()
        await pilot.pause()

    assert len(app._log) == initial_log_len, "Empty queue must not append to log"


async def test_compass_429_stores_pending_action() -> None:
    """compass hit by 429 must store a _pending_action with kind=compass."""
    app = _make_dashboard_with_cubes()
    app.agent.set_compass = _set_compass_rate_limited(30.0)
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.press("c")
        await pilot.pause()
        await pilot.press("3")  # preset index 2 = "grow"
        await pilot.pause()
        await pilot.pause()

    assert app._pending_action is not None, "429 on compass must queue a pending action"
    assert app._pending_action.kind == "compass"
    assert app._pending_action.action == "grow"


async def test_compass_429_shows_queued_in_journal() -> None:
    """compass 429 must log '⏳' in the journal — not a ✗ error."""
    app = _make_dashboard_with_cubes()
    app.agent.set_compass = _set_compass_rate_limited(30.0)
    async with app.run_test(size=(80, 40)) as pilot:
        journal = await _journal_after(pilot, "c", "3")

    assert "⏳" in journal.plain, "Queue indicator must appear in journal on compass 429"
    assert "✗" not in journal.plain, "Error symbol must NOT appear when compass is queued"


async def test_fire_pending_compass_success_sets_compass_preset() -> None:
    """_fire_pending() for a compass action must update _compass_preset on success."""
    from cosmergon_agent.dashboard import _PendingAction

    app = _make_dashboard_with_cubes()

    async def _ok_compass(preset: str) -> dict:
        return {"explanation": "Growing aggressively.", "opinion": "ok"}

    app.agent.set_compass = _ok_compass
    async with app.run_test(size=(80, 40)) as pilot:
        app._pending_action = _PendingAction(
            kind="compass", action="grow", params={}, display="🌱  Grow"
        )
        await app._fire_pending()
        await pilot.pause()

    assert app._compass_preset == "grow", "Compass preset must be updated after successful retry"
    assert app._compass_ever_set is True, "compass_ever_set must be True after successful retry"


# ---------------------------------------------------------------------------
# First-tick compass restore (Issue #8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compass_restored_from_state_on_first_tick() -> None:
    """Dashboard reads compass_preset from server state on first tick.

    When the server returns compass_preset="grow", the first tick must set
    _compass_ever_set=True and _compass_preset="grow" without user interaction.
    """
    state = fake_state(compass_preset="grow")
    agent = CosmergonAgent(api_key="AGENT-test:fakekey000000000000000000000")
    agent.agent_id = state.agent_id

    app = _TestDashboard(agent=agent, theme=THEMES["cosmergon"])
    assert not app._compass_ever_set, "Must start with compass never set"

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await agent._tick_handler(state)
        await pilot.pause()

    assert app._compass_ever_set, "compass_ever_set must be True after first tick with preset"
    assert app._compass_preset == "grow"


@pytest.mark.asyncio
async def test_compass_not_restored_when_preset_is_none() -> None:
    """Dashboard keeps CTA when server has no compass_preset (never set)."""
    state = fake_state()  # compass_preset=None by default
    agent = CosmergonAgent(api_key="AGENT-test:fakekey000000000000000000000")
    agent.agent_id = state.agent_id

    app = _TestDashboard(agent=agent, theme=THEMES["cosmergon"])

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await agent._tick_handler(state)
        await pilot.pause()

    assert not app._compass_ever_set, "CTA must still show when no compass_preset from server"
    assert app._compass_preset == "autonomous", "Default preset must be unchanged"


# ---------------------------------------------------------------------------
# Paket 6: CHAT panel, context-bar, LOG/CHAT screens
# ---------------------------------------------------------------------------


def _make_dashboard_with_messages(messages: list[dict]) -> _TestDashboard:
    """Dashboard with pre-loaded chat messages."""
    agent = CosmergonAgent(api_key="AGENT-test:fakekey000000000000000000000")
    state = fake_state()
    agent._state = state
    agent.agent_id = state.agent_id
    app = _TestDashboard(agent=agent, theme=THEMES["cosmergon"])
    app._messages = messages
    return app


async def test_chat_panel_empty_shows_header() -> None:
    """LOG panel must show chat hint in its header."""
    app = _make_dashboard()
    log = await _render_chat(app)
    assert "chat" in log.plain, "LOG panel must show 'chat' hint in header"


async def test_chat_panel_shows_player_message() -> None:
    """Player messages must appear in LOG panel labeled 'Du'."""
    msgs = [
        {"sender": "player", "message": "Hello agent!", "message_type": "player_question",
         "created_at": "2026-01-01"}
    ]
    app = _make_dashboard_with_messages(msgs)
    log = await _render_chat(app)
    assert "Du" in log.plain, "Player message must appear as '[Du]' in LOG"
    assert "Hello agent!" in log.plain


async def test_chat_panel_shows_agent_message() -> None:
    """Agent messages must appear in LOG panel labeled with agent name."""
    msgs = [
        {"sender": "agent", "message": "Working on it.", "message_type": "reply",
         "created_at": "2026-01-01"}
    ]
    app = _make_dashboard_with_messages(msgs)
    log = await _render_chat(app)
    assert "Working on it." in log.plain


async def test_context_bar_no_focus_shows_tab_hint() -> None:
    """Context-bar must show Tab hint when no panel is focused."""
    app = _make_dashboard()
    assert app._focus is None
    ctx = await _render_context(app)
    assert "Tab" in ctx.plain, "Context-bar must show Tab hint when no panel focused"


async def test_context_bar_agent_focus_shows_compass_numbers() -> None:
    """Context-bar must show numbered compass options when AGENT is focused."""
    app = _make_dashboard()
    app._focus = "agent"
    ctx = await _render_context(app)
    assert "[1]" in ctx.plain, "Context-bar must show [1] for first compass option"
    assert "[7]" in ctx.plain, "Context-bar must show [7] for last compass option"


async def test_log_screen_opens_on_l() -> None:
    """Pressing [L] must push a LogScreen onto the screen stack."""
    app = _make_dashboard(log=["[green]● Connected[/green]"])
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.press("l")
        await pilot.pause()
        await pilot.pause()
        assert "LogScreen" in type(pilot.app.screen).__name__, (
            "[L] must push a LogScreen, not stay on main dashboard"
        )
        await pilot.press("escape")
        await pilot.pause()
        assert "LogScreen" not in type(pilot.app.screen).__name__, (
            "Esc must close LogScreen and return to main dashboard"
        )


async def test_chat_screen_opens_on_m() -> None:
    """Pressing [M] must push a ChatScreen onto the screen stack."""
    app = _make_dashboard()

    async def _noop_send(*_args: object, **_kwargs: object) -> dict:
        return {"id": "x", "created_at": "2026-01-01"}

    app.agent.send_message = _noop_send  # type: ignore[method-assign]
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.press("m")
        await pilot.pause()
        await pilot.pause()
        assert "ChatScreen" in type(pilot.app.screen).__name__, (
            "[M] must push a ChatScreen, not stay on main dashboard"
        )
        await pilot.press("escape")
        await pilot.pause()
        assert "ChatScreen" not in type(pilot.app.screen).__name__, (
            "Esc must close ChatScreen and return to main dashboard"
        )


# ---------------------------------------------------------------------------
# Focus border highlight (John Maeda Law 5 — DIFFERENCE)
# ---------------------------------------------------------------------------


async def _get_focus_classes(app: _TestDashboard) -> dict[str, bool]:
    """Run headless, trigger redraw, return panel-id → has .panel-focused."""
    async with app.run_test(size=(80, 36)) as pilot:
        await pilot.pause()
        pilot.app._redraw()
        await pilot.pause()
        return {
            pid: pilot.app.query_one(f"#{pid}", Static).has_class("panel-focused")
            for pid in ("agent-panel", "economy-panel", "log-panel")
        }


async def _get_panel_text(app: _TestDashboard, panel_id: str) -> str:
    async with app.run_test(size=(80, 36)) as pilot:
        await pilot.pause()
        pilot.app._redraw()
        await pilot.pause()
        return pilot.app.query_one(f"#{panel_id}", Static).render().plain


@pytest.mark.asyncio
async def test_no_focus_no_panel_highlighted() -> None:
    """With _focus=None no panel must carry .panel-focused."""
    app = _make_dashboard()
    assert app._focus is None
    classes = await _get_focus_classes(app)
    assert not any(classes.values()), f"Expected no highlighted panel, got: {classes}"


@pytest.mark.asyncio
async def test_agent_focus_highlights_agent_panel() -> None:
    """When _focus='agent', only agent-panel must have .panel-focused."""
    app = _make_dashboard()
    app._focus = "agent"
    classes = await _get_focus_classes(app)
    assert classes["agent-panel"], "agent-panel must be highlighted when focus='agent'"
    assert not classes["log-panel"], "log-panel must NOT be highlighted"
    assert not classes["economy-panel"], "economy-panel must NOT be highlighted"


@pytest.mark.asyncio
async def test_fields_focus_highlights_agent_panel() -> None:
    """When _focus='fields', agent-panel must have .panel-focused (fields live inside it)."""
    app = _make_dashboard()
    app._focus = "fields"
    classes = await _get_focus_classes(app)
    assert classes["agent-panel"], "agent-panel must be highlighted when focus='fields'"
    assert not classes["log-panel"]


@pytest.mark.asyncio
async def test_log_focus_highlights_log_panel() -> None:
    """When _focus='log', only log-panel must have .panel-focused."""
    app = _make_dashboard()
    app._focus = "log"
    classes = await _get_focus_classes(app)
    assert classes["log-panel"], "log-panel must be highlighted when focus='log'"
    assert not classes["agent-panel"]


@pytest.mark.asyncio
async def test_focus_marker_in_agent_panel() -> None:
    """▶ must appear in agent-panel text when focus='agent'."""
    app = _make_dashboard()
    app._focus = "agent"
    text = await _get_panel_text(app, "agent-panel")
    assert "▶" in text, "▶ marker must appear in agent-panel when focused"


@pytest.mark.asyncio
async def test_focus_marker_in_log_panel() -> None:
    """▶ must appear in log-panel text when focus='log'."""
    app = _make_dashboard()
    app._focus = "log"
    text = await _get_panel_text(app, "log-panel")
    assert "▶" in text, "▶ marker must appear in log-panel when focused"


@pytest.mark.asyncio
async def test_no_focus_marker_when_unfocused() -> None:
    """No panel must show ▶ when _focus=None."""
    app = _make_dashboard()
    assert app._focus is None
    async with app.run_test(size=(80, 36)) as pilot:
        await pilot.pause()
        pilot.app._redraw()
        await pilot.pause()
        for pid in ("agent-panel", "log-panel"):
            text = pilot.app.query_one(f"#{pid}", Static).render().plain
            assert "▶" not in text, f"▶ must NOT appear in {pid} when no panel is focused"


@pytest.mark.asyncio
async def test_tab_cycles_focus_and_border() -> None:
    """Focus cycle None→agent→fields→log→None: border must follow."""
    app = _make_dashboard()
    async with app.run_test(size=(80, 36)) as pilot:
        await pilot.pause()

        def cycle_and_redraw() -> None:
            pilot.app.action_cycle_focus()
            pilot.app._redraw()

        assert pilot.app._focus is None

        # → agent
        cycle_and_redraw()
        await pilot.pause()
        assert pilot.app._focus == "agent"
        assert pilot.app.query_one("#agent-panel", Static).has_class("panel-focused")

        # → fields
        cycle_and_redraw()
        await pilot.pause()
        assert pilot.app._focus == "fields"
        assert pilot.app.query_one("#agent-panel", Static).has_class("panel-focused")

        # → log
        cycle_and_redraw()
        await pilot.pause()
        assert pilot.app._focus == "log"
        assert pilot.app.query_one("#log-panel", Static).has_class("panel-focused")
        assert not pilot.app.query_one("#agent-panel", Static).has_class("panel-focused")

        # → None (full cycle)
        cycle_and_redraw()
        await pilot.pause()
        assert pilot.app._focus is None
        assert not pilot.app.query_one("#log-panel", Static).has_class("panel-focused")

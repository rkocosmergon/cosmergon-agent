"""Terminal dashboard for Cosmergon agents — btop-inspired Textual UI.

Usage:
    cosmergon-dashboard                    # auto-register and connect
    cosmergon-dashboard --api-key KEY      # use existing key
    cosmergon-dashboard --theme matrix     # use a different theme
    python -m cosmergon_agent.dashboard    # same as above

Hotkeys:
    C  Set Compass direction (highlighted until first use)
    P  Place cells       F  Create field     E  Evolve entity
    Space  Pause/Resume  U  Upgrade → Developer
    R  Refresh now       Q  Quit             ?  Help

Themes: cosmergon (default), matrix, mono, high-contrast
Config: COSMERGON_THEME env var  |  ~/.cosmergon/dashboard.toml
"""

from __future__ import annotations

import argparse
import logging
import os
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Label, Static

from cosmergon_agent import AuthenticationError, CosmergonAgent, CosmergonError, __version__
from cosmergon_agent.state import GameState

logger = logging.getLogger(__name__)

_MAX_LOG = 80
_MAX_FIELDS = 5
_PRESETS = ["block", "blinker", "toad", "glider", "r_pentomino", "pentadecathlon", "pulsar"]
_COMPASS_PRESETS = ["attack", "defend", "grow", "trade", "cooperate", "explore", "autonomous"]
_COMPASS_DISPLAY = {
    "attack": "⚔  Attack",
    "defend": "🛡  Defend",
    "grow": "🌱  Grow",
    "trade": "💹  Trade",
    "cooperate": "🤝  Cooperate",
    "explore": "🔭  Explore",
    "autonomous": "Autonomous",
}


# ---------------------------------------------------------------------------
# Theme system — Rich markup color names
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Theme:
    name: str
    cmd: str  # hotkeys / clickable
    guide: str  # onboarding highlight
    pos: str  # positive / gain
    warn: str  # warning / loss
    struct: str  # headers / separators
    data: str  # neutral data text


THEMES: dict[str, Theme] = {
    "cosmergon": Theme("cosmergon", "#aaaaaa", "yellow", "#6EE21C", "red", "#999999", "white"),
    "matrix": Theme("matrix", "green", "bright_green", "green", "red", "green", "green"),
    "mono": Theme("mono", "white", "white", "white", "white", "white", "white"),
    "high-contrast": Theme("high-contrast", "yellow", "cyan", "green", "red", "white", "white"),
}


def _load_theme(cli_theme: str | None = None) -> Theme:
    """Resolve theme: CLI arg > COSMERGON_THEME env > ~/.cosmergon/dashboard.toml > default."""
    if cli_theme and cli_theme in THEMES:
        return THEMES[cli_theme]
    env = os.environ.get("COSMERGON_THEME")
    if env and env in THEMES:
        return THEMES[env]
    cfg = Path.home() / ".cosmergon" / "dashboard.toml"
    if cfg.exists():
        try:
            try:
                import tomllib
            except ImportError:
                import tomli as tomllib  # type: ignore[no-redef]
            with cfg.open("rb") as fh:
                data = tomllib.load(fh)
            name = data.get("dashboard", {}).get("theme")
            if name and name in THEMES:
                return THEMES[name]
        except Exception:
            pass
    return THEMES["cosmergon"]


def _c(color: str, text: str) -> str:
    return f"[{color}]{text}[/{color}]"


def _hk(key: str) -> str:
    """Return Rich-escaped hotkey notation: _hk('C') → '\\[C]' (renders as literal [C])."""
    return "\\[" + key + "]"


def _energy_bar(energy: float, max_e: float = 5000.0, width: int = 8) -> str:
    ratio = min(1.0, max(0.0, energy / max_e))
    full = int(ratio * width)
    half = int((ratio * width - full) * 2)
    return "▓" * full + ("▒" if half else "") + "░" * max(0, width - full - half)


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------


class SelectModal(ModalScreen):
    """Numbered selection overlay — dismisses with index (int) or None (Esc)."""

    DEFAULT_CSS = """
    SelectModal {
        align: center middle;
    }
    SelectModal > #dialog {
        width: 44;
        height: auto;
        max-height: 20;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, title: str, options: list[str]) -> None:
        super().__init__()
        self._title = title
        self._options = options[:9]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"[bold]{self._title}[/bold]")
            yield Label("")
            for i, opt in enumerate(self._options):
                yield Label(f"[cyan][{i + 1}][/cyan] {opt}")
            yield Label("")
            yield Label("[dim][1-9] select  \\[Esc] cancel[/dim]")

    def on_key(self, event: Any) -> None:
        if event.key == "escape":
            self.dismiss(None)
        elif event.key.isdigit():
            idx = int(event.key) - 1
            if 0 <= idx < len(self._options):
                self.dismiss(idx)


class HelpModal(ModalScreen):
    """Scrollable guide: what is Cosmergon, hotkeys, and FAQ."""

    DEFAULT_CSS = """
    HelpModal {
        align: center middle;
    }
    HelpModal > #guide-wrap {
        width: 64;
        height: 85vh;
        max-height: 40;
        border: solid $accent;
        background: $surface;
    }
    HelpModal > #guide-wrap > #guide-header {
        height: 1;
        padding: 0 2;
    }
    HelpModal > #guide-wrap > VerticalScroll {
        padding: 0 2 1 2;
    }
    """

    def __init__(self, theme_name: str) -> None:
        super().__init__()
        self._theme_name = theme_name

    def compose(self) -> ComposeResult:
        sections: list[str] = [
            # ── THE GAME ──────────────────────────────────────────────────
            "[bold]═ THE GAME[/bold]",
            "",
            "Cosmergon is a living economy where AI agents compete",
            "inside Conway's Game of Life.",
            "",
            "The world is a 3D grid. Cells are born and die each tick",
            "by Conway's rules. Your agent controls cells on game",
            "fields and earns [bold]Energy[/bold] from their activity.",
            "",
            "Energy is the only currency. Spend it to create fields,",
            "place cells, evolve patterns, or trade on the market.",
            "",
            "Your agent evolves through 6 Tiers as its Conway patterns",
            "grow more complex:",
            "  T0  still life     (static cluster)",
            "  T1  oscillator     (repeating pattern)",
            "  T2  spaceship      (moving pattern)",
            "  T3  complex        (large / irregular)",
            "  T4  gun            (shoots gliders)",
            "  T5  breeder        (exponential growth)",
            "",
            "Set a [bold]Compass[/bold] to give your agent strategic direction",
            "(grow, trade, attack, defend…). The agent interprets it",
            "through its own personality and acts autonomously.",
            "",
            # ── FAQ ───────────────────────────────────────────────────────
            "[bold]═ FAQ[/bold]",
            "",
            "[bold]Where is my agent?[/bold]",
            "On cosmergon.com — running 24/7, not on your machine.",
            "Closing this dashboard does not affect it.",
            "",
            "[bold]Dashboard crashed — is my agent dead?[/bold]",
            "No. Your agent lives on the server and keeps acting",
            "autonomously. Restart the dashboard to reconnect.",
            "",
            "[bold]How do I reconnect to my agent?[/bold]",
            "Just run cosmergon-dashboard again. Credentials are",
            "stored in ~/.cosmergon/config.toml and reused.",
            "",
            "[bold]Auth failed / 401 error?[/bold]",
            "Your API key expired (anonymous keys last 24 h).",
            "Run:  rm ~/.cosmergon/config.toml",
            "Then: cosmergon-dashboard   (re-registers automatically)",
            "Your old agent lives on as a Vagant — see below.",
            "",
            "[bold]What is a Vagant?[/bold]",
            "When an anonymous agent's key expires its player account",
            "is gone — but the agent stays alive on the server and",
            "keeps playing autonomously forever. It becomes a Vagant.",
            "You can reclaim it later with 'cosmergon-dashboard",
            "--claim' if you register a permanent account.",
            "",
            "[bold]What is Energy?[/bold]",
            "The game currency. Earned automatically each tick when",
            "your Conway cells are active. Spent on fields, cells,",
            "evolution, and market trades.",
            "",
            "[bold]What is a Field?[/bold]",
            "A 2D Conway grid inside a Cube. Your agent can own",
            "multiple fields. Cells placed on a field evolve each",
            "tick and generate Energy.",
            "",
            "[bold]What is a Compass?[/bold]",
            "A strategic hint you give your agent: grow, trade,",
            "attack, defend, cooperate, explore, or autonomous.",
            "The agent interprets it — it is not a direct command.",
            "",
            # ── HOTKEYS ───────────────────────────────────────────────────
            "[bold]═ HOTKEYS[/bold]",
            "",
            "[cyan]\\[C][/cyan]  Set Compass direction",
            "[cyan]\\[P][/cyan]  Place cells on field",
            "[cyan]\\[F][/cyan]  Create new field",
            "[cyan]\\[E][/cyan]  Evolve entity",
            "[cyan]\\[Space][/cyan]  Pause / Resume",
            "[cyan]\\[U][/cyan]  Upgrade → Developer (opens browser)",
            "[cyan]\\[R][/cyan]  Refresh data",
            "[cyan]\\[Q][/cyan]  Quit",
            "",
            f"[dim]Theme: {self._theme_name}   SDK: {__version__}[/dim]",
            "[dim]Themes: cosmergon  matrix  mono  high-contrast[/dim]",
        ]
        with Vertical(id="guide-wrap"):
            yield Label("[dim]Scroll ↓ for full guide · Press any key to close[/dim]", id="guide-header")
            with VerticalScroll():
                for line in sections:
                    yield Label(line)

    def on_key(self, event: Any) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Dashboard App
# ---------------------------------------------------------------------------


class CosmergonDashboard(App):
    """btop-inspired Textual dashboard for Cosmergon agents."""

    ENABLE_COMMAND_PALETTE = False

    DEFAULT_CSS = """
    Screen {
        background: #1e1e1e;
        layout: vertical;
    }

    #hint-bar {
        height: 7;
        background: #252525;
        padding: 1 2;
        border-bottom: solid #2a2a2a;
    }

    #top-row {
        height: 11;
        min-height: 8;
    }

    #agent-panel {
        width: 1fr;
        background: #161616;
        border: solid #2a2a2a;
        padding: 0 1;
        overflow: hidden hidden;
    }

    #economy-panel {
        width: 1fr;
        background: #161616;
        border: solid #2a2a2a;
        padding: 0 1;
        overflow: hidden hidden;
    }

    #journal-panel {
        background: #161616;
        border: solid #2a2a2a;
        padding: 0 1;
        height: 1fr;
        overflow: hidden hidden;
    }

    #status-bar {
        height: 1;
        background: #1e1e1e;
        padding: 0 1;
    }

    #key-bar {
        height: 4;
        background: #1e1e1e;
        padding: 0 1;
        border-top: solid #2a2a2a;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("c", "compass", "Compass", show=False),
        Binding("p", "place_cells", "Place", show=False),
        Binding("f", "create_field", "Field", show=False),
        Binding("e", "evolve", "Evolve", show=False),
        Binding("u", "upgrade", "Upgrade", show=False),
        Binding("space", "pause", "Pause", show=False),
        Binding("r", "refresh_now", "Refresh", show=False),
        Binding("question_mark", "help", "Help", show=False),
        Binding("q", "quit", "Quit", show=False),
    ]

    def __init__(self, agent: CosmergonAgent, theme: Theme) -> None:
        super().__init__()
        self.agent = agent
        self._theme = theme
        self._log: list[str] = []
        self._paused = False
        self._compass_preset = "autonomous"
        self._compass_ever_set = False
        self._last_energy: float | None = None
        self._feedback: str = ""
        self._feedback_until: float = 0.0
        self._tick_received_at: float = 0.0
        self._tick_interval: float = 60.0  # self-calibrating from observed tick gaps
        self._last_tick: int = -1
        self._hint_history: list[str] = []
        self._panel_cache: dict[str, str] = {}  # widget-id → last rendered content
        self._fatal_error: str = ""  # set on AuthenticationError — shown in hint-bar

    def compose(self) -> ComposeResult:
        yield Static("", id="hint-bar")
        with Horizontal(id="top-row"):
            yield Static("", id="agent-panel")
            yield Static("", id="economy-panel")
        yield Static("", id="journal-panel")
        yield Static("", id="status-bar")
        yield Static("", id="key-bar")

    def on_mount(self) -> None:
        self._register_agent_handlers()
        self._run_agent()
        self.set_interval(0.5, self._redraw)

    def _register_agent_handlers(self) -> None:
        @self.agent.on_tick
        async def _tick(state: GameState) -> None:
            now = time.monotonic()
            # Calibrate tick interval from observed gap (sanity-bound: 10-300s)
            if self._tick_received_at > 0:
                observed = now - self._tick_received_at
                if 10.0 < observed < 300.0:
                    self._tick_interval = observed
            self._tick_received_at = now
            self._last_tick = state.tick
            if self._last_energy is None:
                self._last_energy = state.energy
                self._add_log(_c(self._theme.pos, f"● Connected  {state.energy:,.0f} E"))
                return
            delta = state.energy - self._last_energy
            self._last_energy = state.energy
            sign = "+" if delta >= 0 else ""
            color = self._theme.pos if delta >= 0 else self._theme.warn
            self._add_log(
                _c(color, f"[tick {state.tick}] {sign}{delta:.0f}E  {state.energy:,.0f} total")
            )

        @self.agent.on_error
        async def _error(result: Any) -> None:
            self._add_log(_c(self._theme.warn, f"✗ {result.action}: {result.error_message}"))

    @work(exclusive=True)
    async def _run_agent(self) -> None:
        try:
            await self.agent.start()
        except AuthenticationError as exc:
            self._fatal_error = f"✗ Auth failed: {exc}"
            self._add_log(_c(self._theme.warn, self._fatal_error))
        except Exception as exc:
            self._add_log(_c(self._theme.warn, f"Agent error: {exc}"))

    def _add_log(self, msg: str) -> None:
        self._log.append(msg)
        if len(self._log) > _MAX_LOG:
            self._log = self._log[-_MAX_LOG:]

    # --- Redraw ---

    def _update_panel(self, widget_id: str, content: str) -> None:
        """Call Static.update() only when content changed — prevents unnecessary repaints."""
        if self._panel_cache.get(widget_id) != content:
            self._panel_cache[widget_id] = content
            self.query_one(f"#{widget_id}", Static).update(content)

    def _redraw(self) -> None:
        state = self.agent.state
        self._draw_hint_bar(state)
        self._draw_agent_panel(state)
        self._draw_economy_panel(state)
        self._draw_journal_panel(state)
        self._draw_status_bar(state)
        self._draw_key_bar()

    def _draw_agent_panel(self, state: GameState | None) -> None:
        t = self._theme
        lines = [_c(t.struct, "[bold]═ AGENT[/bold]")]

        if not state:
            lines.append(_c("dim", "Connecting..."))
            self._update_panel("agent-panel", "\n".join(lines))
            return

        # Status + energy
        status = "PAUSED" if self._paused else "AKTIV"
        sc = t.warn if self._paused else t.pos
        bar = _energy_bar(state.energy)
        lines.append(f"{_c(sc, f'● {status}')}  {_c(t.data, f'{state.energy:,.0f} E  {bar}')}")

        if state.ranking:
            score_part = (
                f"  Score: {state.ranking.player_score:,.0f}"
                if state.ranking.player_score > 0
                else ""
            )
            tier_line = f"T{state.ranking.player_tier} {state.ranking.tier_name}{score_part}"
            lines.append(_c(t.data, tier_line))
        lines.append("")

        # Compass — CTA lives in hint-bar, agent panel shows current state only
        compass_label = _COMPASS_DISPLAY.get(self._compass_preset, self._compass_preset)
        compass_val = compass_label if self._compass_ever_set else "—"
        lines.append(_c(t.data, f"Compass: {compass_val}"))

        # Fields
        if state.fields:
            lines.append("")
            lines.append(_c(t.struct, "[bold]═ FIELDS[/bold]"))
            for f in state.fields[:_MAX_FIELDS]:
                tier = f"T{f.entity_tier or 0}"
                etype = (f.entity_type or "novice")[:8]
                bar_f = _energy_bar(f.active_cell_count, 200, 6)
                lines.append(
                    _c(t.data, f"  {f.id[:8]} {tier} {etype:8s} {bar_f} {f.active_cell_count}c")
                )

        self._update_panel("agent-panel", "\n".join(lines))

    def _draw_economy_panel(self, state: GameState | None) -> None:
        t = self._theme
        lines = [_c(t.struct, "[bold]═ WIRTSCHAFT[/bold]")]

        if state and state.world_briefing:
            wb = state.world_briefing
            lines.append(_c(t.data, f"Rang:  #{wb.your_rank} / {wb.total_agents}"))
            if wb.top_agent:
                lines.append(_c(t.data, f"Top:   {wb.top_agent[:32]}"))
            lines.append(_c(t.data, f"Markt: {wb.market_summary[:32]}"))
            if wb.last_event:
                lines.append(_c("dim", f"Last: {wb.last_event[:32]}"))
        elif state:
            lines.append(_c("dim", "Joining universe..."))

        self._update_panel("economy-panel", "\n".join(lines))

    def _draw_journal_panel(self, state: GameState | None) -> None:
        t = self._theme
        lines = [_c(t.struct, "[bold]═ JOURNAL[/bold]")]

        # Learned rules — show last 3
        learned = (state.learned_rules if state else None) or []
        if learned:
            lines.append(_c(t.struct, "Learned Rules:"))
            for rule in learned[-3:]:
                lines.append(_c(t.data, f"  • {rule[:90]}"))
            lines.append("")

        # Activity feed
        lines.append(_c(t.struct, "Activity:"))
        feed = self._log[-10:]
        if feed:
            lines.extend(feed)
        else:
            lines.append("[dim]Connecting to cosmergon.com...[/dim]")

        self._update_panel("journal-panel", "\n".join(lines))

    def _draw_status_bar(self, state: GameState | None) -> None:
        agent_id = (self.agent.agent_id or "?")[:8]
        tier = state.subscription_tier if state else "?"
        tick = state.tick if state else "-"
        sep = " │ "
        tname = self._theme.name
        segments = [
            agent_id,
            f"tick {tick}",
            f"tier {tier}",
            f"sdk {__version__}",
            f"theme {tname}",
        ]
        self._update_panel("status-bar", f"[dim]{sep.join(segments)}[/dim]")

    def _set_feedback(self, msg: str, duration: float = 4.0) -> None:
        """Show a timed message in the hint bar; previous feedback is archived to history."""
        if self._feedback:
            self._hint_history.append(self._feedback)
            if len(self._hint_history) > 4:
                self._hint_history = self._hint_history[-4:]
        self._feedback = msg
        self._feedback_until = time.monotonic() + duration

    def _countdown_suffix(self) -> str:
        """Return countdown suffix using server's next_tick_at (or self-calibrated fallback)."""
        t = self._theme
        state = self.agent.state
        if state and state.next_tick_at:
            remaining = state.next_tick_at - time.time()
            if remaining > 1.0:
                return "  ·  " + _c("dim", f"next ~{int(remaining)}s")
            overdue = max(0, int(-remaining))
            color = t.warn if overdue > 90 else "dim"
            return "  ·  " + _c(color, f"+{overdue}s")
        # Fallback: self-calibrated estimate (old server / first poll)
        if self._tick_received_at > 0:
            elapsed = time.monotonic() - self._tick_received_at
            remaining = self._tick_interval - elapsed
            if remaining > 1.0:
                return "  ·  " + _c("dim", f"next ~{int(remaining)}s")
            return "  ·  " + _c("dim", f"+{max(0, int(-remaining))}s")
        return ""

    def _compute_hint(self, state: GameState | None) -> str:
        """Return Line 1 of the hint bar: active feedback OR current guidance."""
        t = self._theme

        # 0. Fatal error (e.g. auth failure) — shown permanently until restart
        if self._fatal_error and not state:
            return _c(t.warn, self._fatal_error)

        # 1. Active feedback — show confirmation + countdown so user knows *when* it fires.
        if self._feedback and time.monotonic() < self._feedback_until:
            state = self.agent.state
            if state and state.next_tick_at:
                remaining = state.next_tick_at - time.time()
                if remaining > 1.0:
                    suffix = _c("dim", f"takes effect at next tick ~{int(remaining)}s")
                else:
                    suffix = _c("dim", "takes effect at next tick soon")
                return f"{self._feedback}  ·  {suffix}"
            elif self._tick_received_at > 0:
                elapsed = time.monotonic() - self._tick_received_at
                remaining = self._tick_interval - elapsed
                if remaining > 1.0:
                    suffix = _c("dim", f"takes effect at next tick ~{int(remaining)}s")
                else:
                    suffix = _c("dim", "takes effect at next tick soon")
                return f"{self._feedback}  ·  {suffix}"
            return self._feedback

        # Feedback expired — archive it so it shows in history below.
        if self._feedback:
            self._hint_history.append(self._feedback)
            if len(self._hint_history) > 4:
                self._hint_history = self._hint_history[-4:]
            self._feedback = ""

        # 2. No state yet
        if not state:
            return _c("dim", "Connecting to cosmergon.com...")

        # 3. Paused — countdown still shown so user sees when next tick would fire
        spc = _hk("Space")
        if self._paused:
            return f"{_c(t.warn, '⏸ Paused')} · {_c(t.cmd, spc)} resume" + self._countdown_suffix()

        # 4. Compass never set → one thing to do
        if not self._compass_ever_set:
            return (
                f"{_c(t.guide, '→')} {_c(t.cmd, _hk('C'))} "
                f"{_c(t.guide, 'Set Compass direction')} — choose how the agent plays"
                + self._countdown_suffix()
            )

        # 5. No fields yet
        if not state.fields:
            return (
                f"{_c(t.guide, '→')} {_c(t.cmd, _hk('F'))} "
                f"{_c(t.guide, 'Create a field')} — choose a cube for your agent"
                + self._countdown_suffix()
            )

        # 6. Fields exist but no cells placed
        if not any(f.active_cell_count > 0 for f in state.fields):
            return (
                f"{_c(t.guide, '→')} {_c(t.cmd, _hk('P'))} "
                f"{_c(t.guide, 'Place cells')} — start a Conway pattern" + self._countdown_suffix()
            )

        # 7. Normal running state — show tick + countdown + quick-actions
        tick_part = f"tick {state.tick}"
        if state.next_tick_at:
            remaining = state.next_tick_at - time.time()
            if remaining > 1.0:
                tick_part += f" · next ~{int(remaining)}s"
            else:
                tick_part += f" · +{max(0, int(-remaining))}s"
        elif self._tick_received_at > 0:
            elapsed = time.monotonic() - self._tick_received_at
            remaining = self._tick_interval - elapsed
            if remaining > 1.0:
                tick_part += f" · next ~{int(remaining)}s"
            else:
                tick_part += f" · +{max(0, int(-remaining))}s"

        actions = (
            f"{_c(t.cmd, _hk('P'))} place  "
            f"{_c(t.cmd, _hk('F'))} field  "
            f"{_c(t.cmd, _hk('E'))} evolve  "
            f"{_c(t.cmd, _hk('C'))} compass"
        )
        return f"{_c('dim', tick_part)}  ·  {actions}"

    def _draw_hint_bar(self, state: GameState | None) -> None:
        """Render hint bar: Line 1 = guidance/feedback, Lines 2-5 = action history."""
        lines = [self._compute_hint(state)]
        for msg in reversed(self._hint_history[-4:]):
            lines.append(msg)
        self._update_panel("hint-bar", "\n".join(lines))

    def _draw_key_bar(self) -> None:
        t = self._theme

        def k(key: str, label: str) -> str:
            return f"{_c(t.cmd, _hk(key))} {label}"

        row1 = "  ".join(
            [
                k("C", "Compass"),
                k("P", "Place"),
                k("F", "Field"),
                k("E", "Evolve"),
                k("U", "Upgrade"),
            ]
        )
        row2 = "  ".join([k("Space", "Pause"), k("R", "Refresh"), k("?", "Help"), k("Q", "Quit")])
        self._update_panel("key-bar", f"{row1}\n{row2}")

    # --- Actions ---

    @work
    async def action_compass(self) -> None:
        labels = [_COMPASS_DISPLAY.get(p, p) for p in _COMPASS_PRESETS]
        idx = await self.push_screen_wait(SelectModal("Set Compass direction", labels))
        if idx is None:
            return
        preset = _COMPASS_PRESETS[idx]
        self._add_log(_c(self._theme.data, f"⠋ compass → {preset}..."))
        try:
            result = await self.agent.set_compass(preset)
            self._compass_preset = preset
            self._compass_ever_set = True
            explanation = (result.get("explanation") or "")[:60]
            self._add_log(_c(self._theme.pos, f"✓ compass: {preset}  {explanation}"))
            self._set_feedback(_c(self._theme.pos, f"✓ Compass set: {preset}"))
        except Exception as exc:
            self._add_log(_c(self._theme.warn, f"✗ compass failed: {exc}"))
            self._set_feedback(_c(self._theme.warn, f"✗ Compass failed: {exc}"))

    @work
    async def action_place_cells(self) -> None:
        state = self.agent.state
        if not state or not state.fields:
            self._add_log(_c(self._theme.warn, "No fields — press \\[F] first"))
            return
        field_labels = [
            f"{f.id[:8]} T{f.entity_tier or 0} ({f.active_cell_count}c)" for f in state.fields
        ]
        fi = await self.push_screen_wait(SelectModal("Field", field_labels))
        if fi is None:
            return
        pi = await self.push_screen_wait(SelectModal("Preset", _PRESETS))
        if pi is None:
            return
        try:
            r = await self.agent.act(
                "place_cells", field_id=state.fields[fi].id, preset=_PRESETS[pi]
            )
            icon, color = ("✓", self._theme.pos) if r.success else ("✗", self._theme.warn)
            self._add_log(_c(color, f"{icon} place_cells({_PRESETS[pi]})"))
            feedback = f"{icon} Cells placed ({_PRESETS[pi]}) — evolves next tick"
            self._set_feedback(_c(color, feedback))
        except CosmergonError as exc:
            self._add_log(_c(self._theme.warn, f"✗ place_cells: {exc}"))
            self._set_feedback(_c(self._theme.warn, f"✗ Place cells failed: {exc}"))

    @work
    async def action_create_field(self) -> None:
        state = self.agent.state
        if not state:
            return
        cubes = state.cubes or state.universe_cubes
        if not cubes:
            self._add_log(_c(self._theme.warn, "No cubes available"))
            return
        try:
            cube_labels = [f"{c.id[:8]} {c.name}" for c in cubes]
            ci = await self.push_screen_wait(SelectModal("Cube", cube_labels))
            if ci is None:
                return
            r = await self.agent.act("create_field", cube_id=cubes[ci].id)
            icon, color = ("✓", self._theme.pos) if r.success else ("✗", self._theme.warn)
            self._add_log(_c(color, f"{icon} create_field"))
            self._set_feedback(_c(color, f"{icon} Field created — press \\[P] to place cells"))
        except Exception as exc:
            self._add_log(_c(self._theme.warn, f"✗ create_field: {exc}"))
            self._set_feedback(_c(self._theme.warn, f"✗ Field creation failed: {exc}"))

    @work
    async def action_evolve(self) -> None:
        state = self.agent.state
        if not state or not state.fields:
            self._add_log(_c(self._theme.warn, "No fields to evolve"))
            return
        evolve_labels = [
            f"{f.id[:8]} T{f.entity_tier or 0} maturity={f.reife_score}" for f in state.fields
        ]
        fi = await self.push_screen_wait(SelectModal("Evolve", evolve_labels))
        if fi is None:
            return
        try:
            r = await self.agent.act("evolve", field_id=state.fields[fi].id)
            icon, color = ("✓", self._theme.pos) if r.success else ("✗", self._theme.warn)
            msg = r.error_message or "ok"
            self._add_log(_c(color, f"{icon} evolve → {msg}"))
            self._set_feedback(_c(color, f"{icon} Evolve: {msg}"))
        except CosmergonError as exc:
            self._add_log(_c(self._theme.warn, f"✗ evolve: {exc}"))
            self._set_feedback(_c(self._theme.warn, f"✗ Evolve failed: {exc}"))

    async def action_upgrade(self) -> None:
        self._add_log(_c(self._theme.data, "⠋ Opening upgrade page..."))
        try:
            resp = await self.agent._request(
                "GET",
                "/api/v1/billing/upgrade-link",
                params={"tier": "developer"},
                follow_redirects=False,
            )
            url = resp.headers.get("location", "https://cosmergon.com/pricing")
            webbrowser.open(url)
            self._add_log(_c(self._theme.pos, "✓ Browser opened"))
            self._set_feedback(_c(self._theme.pos, "✓ Browser opened — complete upgrade there"))
        except Exception as exc:
            self._add_log(_c(self._theme.warn, f"✗ Upgrade link error: {exc}"))
            self._set_feedback(_c(self._theme.warn, f"✗ Upgrade failed: {exc}"))

    async def action_pause(self) -> None:
        action = "resume" if self._paused else "pause"
        try:
            r = await self.agent.act(action)
            self._paused = not self._paused
            icon, color = ("✓", self._theme.pos) if r.success else ("✗", self._theme.warn)
            self._add_log(_c(color, f"{icon} {action}"))
            label = "⏸ Agent paused" if self._paused else "▶ Agent resumed"
            self._set_feedback(_c(color, f"{icon} {label}"))
        except CosmergonError as exc:
            self._add_log(_c(self._theme.warn, f"✗ {action}: {exc}"))
            self._set_feedback(_c(self._theme.warn, f"✗ {action} failed: {exc}"))

    async def action_refresh_now(self) -> None:
        self._add_log(_c(self._theme.data, "Refreshing..."))

    @work
    async def action_help(self) -> None:
        await self.push_screen_wait(HelpModal(self._theme.name))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cosmergon Agent Dashboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Themes: cosmergon (default), matrix, mono, high-contrast\n"
        "Config: ~/.cosmergon/dashboard.toml  |  COSMERGON_THEME env var",
    )
    parser.add_argument("--api-key", help="API key (auto-registers if omitted)")
    parser.add_argument("--base-url", default="https://cosmergon.com")
    parser.add_argument("--theme", choices=list(THEMES), default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    theme = _load_theme(args.theme)
    try:
        agent = CosmergonAgent(api_key=args.api_key, base_url=args.base_url, poll_interval=10.0)
        CosmergonDashboard(agent=agent, theme=theme).run()
    except CosmergonError as exc:
        msg = str(exc)
        if "429" in msg or "Max" in msg:
            print("\n✗  Too many anonymous registrations from this IP address.")
            print()
            print("   Register for free at cosmergon.com to get your own API key:")
            print()
            print("   cosmergon-dashboard --api-key <your-key>")
            print()
            print("   https://cosmergon.com/getting-started.html")
        else:
            print(f"\n✗  Connection failed: {exc}")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

"""Terminal dashboard for Cosmergon agents — btop-inspired Textual UI.

Usage:
    cosmergon-dashboard                    # auto-register and connect
    cosmergon-dashboard --api-key KEY      # use existing key
    cosmergon-dashboard --theme matrix     # use a different theme
    python -m cosmergon_agent.dashboard    # same as above

Hotkeys:
    C  Set Compass direction (highlighted until first use)
    P  Place cells       F  Create field     E  Evolve entity
    Space  Pause/Resume  U  Upgrade (next tier, opens browser)
    R  Refresh now       Q  Quit             ?  Help

Themes: cosmergon (default), matrix, mono, high-contrast
Config: COSMERGON_THEME env var  |  ~/.cosmergon/dashboard.toml
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from cosmergon_agent.action import ActionResult

try:
    from textual import work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen
    from textual.widgets import Input, Label, Select, Static
except ImportError as _exc:
    raise ImportError(
        "The cosmergon-agent dashboard requires textual.\n"
        "Install it with:\n"
        "  pip install 'cosmergon-agent[dashboard]'\n"
        "or:\n"
        "  pip install textual"
    ) from _exc

from cosmergon_agent import AuthenticationError, CosmergonAgent, CosmergonError, __version__
from cosmergon_agent.exceptions import ConnectionError as CsgConnectionError
from cosmergon_agent.exceptions import RateLimitError
from cosmergon_agent.state import Field, GameState

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

_AUTO_AGENT_NAME = re.compile(r"^agent_[0-9a-f]{8}$")

_PERSONA_OPTIONS: list[tuple[str, str]] = [
    ("Scientist    — patient, evolves high-tier patterns", "scientist"),
    ("Warrior      — aggressive, dominates through invasion", "warrior"),
    ("Expansionist — spreads wide, maximises field presence", "expansionist"),
    ("Trader       — analytical, arbitrages the marketplace", "trader"),
    ("Diplomat     — cooperative, builds alliances", "diplomat"),
    ("Farmer       — patient, optimises stable patterns for yield", "farmer"),
]


def _is_auto_name(name: str) -> bool:
    """Return True if the name matches the auto-generated pattern agent_XXXXXXXX."""
    return bool(_AUTO_AGENT_NAME.match(name))


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


def _energy_ref(energy: float, max_e: float = 5000.0) -> str:
    """Return a compact max-energy reference like '5k' or '50k'."""
    if max_e >= 1000:
        return f"{int(max_e / 1000)}k"
    return str(int(max_e))


def _energy_bar(energy: float, max_e: float = 5000.0, width: int = 8) -> str:
    ratio = min(1.0, max(0.0, energy / max_e))
    full = int(ratio * width)
    half = int((ratio * width - full) * 2)
    return "▓" * full + ("▒" if half else "") + "░" * max(0, width - full - half)


def _truncate_words(text: str, max_len: int) -> str:
    """Truncate at a word boundary, appending '…' if shortened.

    Never cuts mid-word. Safe for narrow terminal panels.
    """
    if len(text) <= max_len:
        return text
    truncated = text[:max_len].rsplit(" ", 1)[0]
    return truncated + "…"


def _action_cost(r: ActionResult) -> float:
    """Extract energy cost from action result. Returns 0.0 if free or unknown."""
    result_data = (r.data or {}).get("result") or {}
    return float(result_data.get("energy_cost", result_data.get("cost", 0)) or 0)


def _cost_str(cost: float) -> str:
    """Format energy cost for display. Returns empty string when free (cost == 0)."""
    return f" (-{cost:,.0f} E)" if cost > 0 else ""


@dataclass
class _PendingAction:
    """Action queued because the tick limit (429) was hit.

    Fired automatically on the next on_tick callback. Only one slot exists —
    pressing a key while a pending action is waiting replaces it (the server
    only allows one action per tick anyway).

    kind:    "act" for agent.act() calls, "compass" for set_compass().
    action:  action name for "act" (e.g. "place_cells"), preset for "compass".
    params:  kwargs forwarded to agent.act() (empty dict for compass).
    display: human-readable label shown in journal and hint bar.
    """

    kind: str
    action: str
    params: dict[str, Any]
    display: str


# ---------------------------------------------------------------------------
# Field-View rendering — pure functions, no Textual dependency
# ---------------------------------------------------------------------------


def _fv_parse_cells(raw: dict[str, int]) -> set[tuple[int, int]]:
    """Parse sparse ``{"x,y": 1}`` dict into a set of ``(x, y)`` int tuples.

    Silently drops malformed keys so a bad API response never crashes the UI.
    """
    cells: set[tuple[int, int]] = set()
    for key in raw:
        try:
            x_s, y_s = key.split(",", 1)
            cells.add((int(x_s), int(y_s)))
        except (ValueError, AttributeError):
            pass
    return cells


def _fv_centroid(
    cells: set[tuple[int, int]], field_w: int, field_h: int
) -> tuple[int, int]:
    """Return integer center-of-mass of alive cells.

    Falls back to field center ``(field_w//2, field_h//2)`` when the field is
    empty so the viewport always has a valid starting position.
    """
    if not cells:
        return field_w // 2, field_h // 2
    xs, ys = zip(*cells, strict=False)
    return sum(xs) // len(xs), sum(ys) // len(ys)


def _fv_render_zoom1(
    cells: set[tuple[int, int]],
    vp_x: int,
    vp_y: int,
    vp_w: int,
    vp_h: int,
    field_w: int,
    field_h: int,
    alive_char: str = "█",
    dead_char: str = "·",
    outside_char: str = " ",
) -> list[str]:
    """Render a viewport slice of a Conway field at 1:1 scale.

    Returns ``vp_h`` strings, each ``vp_w`` characters wide.  Cells outside
    the field boundary use ``outside_char``.  Accepts pre-formatted Rich markup
    strings for ``alive_char`` / ``dead_char`` so callers can inject colour.
    """
    rows: list[str] = []
    for row in range(vp_y, vp_y + vp_h):
        parts: list[str] = []
        for col in range(vp_x, vp_x + vp_w):
            if 0 <= col < field_w and 0 <= row < field_h:
                parts.append(alive_char if (col, row) in cells else dead_char)
            else:
                parts.append(outside_char)
        rows.append("".join(parts))
    return rows


def _fv_render_zoom2(
    cells: set[tuple[int, int]],
    field_w: int,
    field_h: int,
    out_w: int,
    out_h: int,
    alive_char: str = "▓",
    dead_char: str = "░",
) -> list[str]:
    """Render the full field compressed to ``out_w x out_h`` characters.

    Each output character represents a ``(field_w/out_w) x (field_h/out_h)``
    block of cells.  Uses ``alive_char`` if any cell in the block is alive.
    Accepts pre-formatted Rich markup strings for colour injection.
    """
    bw = field_w / out_w
    bh = field_h / out_h
    rows: list[str] = []
    for oy in range(out_h):
        y0, y1 = int(oy * bh), int((oy + 1) * bh)
        parts: list[str] = []
        for ox in range(out_w):
            x0, x1 = int(ox * bw), int((ox + 1) * bw)
            alive = any(
                (x, y) in cells for x in range(x0, x1) for y in range(y0, y1)
            )
            parts.append(alive_char if alive else dead_char)
        rows.append("".join(parts))
    return rows


def _fv_render_minimap(
    cells: set[tuple[int, int]],
    vp_x: int,
    vp_y: int,
    vp_w: int,
    vp_h: int,
    field_w: int,
    field_h: int,
    map_w: int = 16,
    map_h: int = 8,
    alive_char: str = "▓",
    dead_char: str = "·",
    vp_char: str = "▒",
) -> list[str]:
    """Render a ``map_w x map_h`` minimap of the full field.

    Returns ``map_h + 1`` strings: one ``─ MAP ─`` header line followed by
    ``map_h`` data rows.  Cells within the current viewport rect are rendered
    with ``vp_char`` regardless of alive status so the user can see where the
    viewport is positioned.
    """
    bw = field_w / map_w
    bh = field_h / map_h
    lines: list[str] = [f"═ MAP {'═' * max(0, map_w - 6)}"]
    for my in range(map_h):
        y0, y1 = int(my * bh), int((my + 1) * bh)
        parts: list[str] = []
        for mx in range(map_w):
            x0, x1 = int(mx * bw), int((mx + 1) * bw)
            in_vp = vp_x < x1 and vp_x + vp_w > x0 and vp_y < y1 and vp_y + vp_h > y0
            if in_vp:
                parts.append(vp_char)
            elif any((x, y) in cells for x in range(x0, x1) for y in range(y0, y1)):
                parts.append(alive_char)
            else:
                parts.append(dead_char)
        lines.append("".join(parts))
    return lines


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
            "[bold]Found a bug or have a question?[/bold]",
            "Open an issue on GitHub:",
            "  github.com/rkocosmergon/cosmergon-agent/issues",
            "Drag & drop or paste (Ctrl+V) screenshots directly into",
            "the issue text box — GitHub hosts them automatically.",
            "If the dashboard crashed, also run:",
            "  TEXTUAL_LOG=~/cosmergon-crash.log cosmergon-dashboard",
            "Reproduce the crash, then paste the log in the issue.",
            "",
            # ── HOTKEYS ───────────────────────────────────────────────────
            "[bold]═ HOTKEYS[/bold]",
            "",
            "[cyan]\\[C][/cyan]  Set Compass direction",
            "[cyan]\\[P][/cyan]  Place cells on field",
            "[cyan]\\[F][/cyan]  Create new field",
            "[cyan]\\[E][/cyan]  Evolve entity",
            "[cyan]\\[V][/cyan]  View Conway field (zoom, scroll, minimap)",
            "[cyan]\\[Space][/cyan]  Pause / Resume",
            "[cyan]\\[U][/cyan]  Upgrade to next tier (opens browser)",
            "[cyan]\\[R][/cyan]  Refresh data",
            "[cyan]\\[Q][/cyan]  Quit",
            "",
            f"[dim]Theme: {self._theme_name}   SDK: {__version__}[/dim]",
            "[dim]Themes: cosmergon  matrix  mono  high-contrast[/dim]",
        ]
        with Vertical(id="guide-wrap"):
            yield Label("[dim]↑ ↓ PgUp PgDn to scroll · Esc or Q to close[/dim]", id="guide-header")
            with VerticalScroll():
                for line in sections:
                    yield Label(line)

    def on_mount(self) -> None:
        self.query_one(VerticalScroll).focus()

    _SCROLL_KEYS: ClassVar[set[str]] = {"up", "down", "pageup", "pagedown", "home", "end"}

    def on_key(self, event: Any) -> None:
        if event.key not in self._SCROLL_KEYS:
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
        height: 1;
        background: #252525;
        padding: 0 1;
    }

    #top-row {
        height: 14;
        min-height: 14;
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

    #log-panel {
        background: #161616;
        border: solid #2a2a2a;
        padding: 0 1;
        height: 1fr;
        min-height: 6;
        overflow: hidden hidden;
    }

    #context-bar {
        height: 1;
        background: #1e1e1e;
        padding: 0 1;
    }

    #fix-bar {
        height: 4;
        background: #1e1e1e;
        border-top: solid #2a2a2a;
    }

    #status-bar {
        height: 1;
        background: #1e1e1e;
        padding: 0 1;
    }

    .panel-focused {
        border: solid yellow;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        # priority=True: fire before focused widget — needed for Textual 8.x where
        # App-level bindings don't fire reliably without it. Safe for all our keys
        # because the ChatScreen modal blocks App bindings via ModalScreen isolation.
        Binding("c", "compass", "Compass", show=False, priority=True),
        Binding("p", "place_cells", "Place", show=False, priority=True),
        Binding("f", "create_field", "Field", show=False, priority=True),
        Binding("e", "evolve", "Evolve", show=False, priority=True),
        Binding("u", "upgrade", "Upgrade", show=False, priority=True),
        Binding("space", "pause", "Pause", show=False, priority=True),
        Binding("r", "refresh_now", "Refresh", show=False, priority=True),
        Binding("l", "log_screen", "Log", show=False, priority=True),
        Binding("m", "chat_screen", "Chat", show=False, priority=True),
        Binding("v", "field_view", "Fields", show=False, priority=True),
        Binding("tab", "cycle_focus", "Focus", show=False, priority=True),
        Binding("question_mark", "help", "Help", show=False, priority=True),
        Binding("q", "quit", "Quit", show=False, priority=True),
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
        self._panel_cache: dict[str, str] = {}  # widget-id → last rendered content
        self._fatal_error: str = ""  # set on AuthenticationError — shown in hint-bar
        self._pending_action: _PendingAction | None = None  # queued on 429, fires next tick
        self._messages: list[dict] = []  # chat conversation cache (refreshed each tick)
        self._focus: str | None = None   # None | "agent" | "fields" | "log"
        self._focus_panel_id: str | None = None  # last panel id with .panel-focused class
        self._identity_prompted: bool = False  # True after identity setup shown once per session

    def compose(self) -> ComposeResult:
        yield Static("", id="hint-bar")
        with Horizontal(id="top-row"):
            yield Static("", id="agent-panel")
            yield Static("", id="economy-panel")
        yield Static("", id="log-panel")
        yield Static("", id="context-bar")
        yield Static("", id="fix-bar")
        yield Static("", id="status-bar")

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
                # Restore compass from server state (persisted in persona_config)
                if state.compass_preset and state.compass_preset in _COMPASS_PRESETS:
                    self._compass_preset = state.compass_preset
                    self._compass_ever_set = True
                self._add_log(_c(self._theme.pos, f"● Connected  {state.energy:,.0f} E"))
                if not self._identity_prompted and _is_auto_name(state.agent_name):
                    self._identity_prompted = True
                    self._show_identity_setup()
                return
            delta = state.energy - self._last_energy
            self._last_energy = state.energy
            sign = "+" if delta >= 0 else ""
            color = self._theme.pos if delta >= 0 else self._theme.warn
            self._add_log(
                _c(color, f"[tick {state.tick}] {sign}{delta:.0f}E  {state.energy:,.0f} total")
            )
            await self._fire_pending()
            # Refresh chat messages each tick (1 extra HTTP call / ~60s — non-fatal)
            try:
                self._messages = await self.agent.get_messages(limit=20)
            except Exception:
                pass

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

    @work
    async def _show_identity_setup(self) -> None:
        """Open the IdentitySetupScreen modal and log the outcome."""
        state = self.agent.state
        current_name = state.agent_name if state else ""
        current_persona = (state.persona_type if state else "") or "scientist"
        result = await self.push_screen_wait(
            IdentitySetupScreen(self.agent, current_name, current_persona, self._theme)
        )
        if result:
            self._add_log(_c(self._theme.pos, f"✓ Identity set: {result['agent_name']}"))

    def _add_log(self, msg: str) -> None:
        self._log.append(msg)
        if len(self._log) > _MAX_LOG:
            self._log = self._log[-_MAX_LOG:]

    def _schedule_pending(self, pending: _PendingAction, retry_after: float = 65.0) -> None:
        """Queue a pending action and schedule a timer-based retry.

        Uses set_timer as a fallback so the retry fires even when the game tick
        counter is stuck at 0 (on_tick would never fire in that case).
        """
        self._pending_action = pending
        self.set_timer(max(retry_after, 5.0), self._fire_pending)

    async def _fire_pending(self) -> None:
        """Fire a queued action on the next tick. Clears the slot before firing."""
        if not self._pending_action:
            return
        pending = self._pending_action
        self._pending_action = None  # clear before firing — prevents re-queue on error
        self._add_log(_c(self._theme.data, f"⠋ auto-retry: {pending.display}..."))
        try:
            if pending.kind == "compass":
                result = await self.agent.set_compass(pending.action)
                if result.get("error"):
                    self._add_log(_c(self._theme.warn, f"✗ {pending.display}: failed"))
                    self._set_feedback(_c(self._theme.warn, f"✗ {pending.display} failed"))
                else:
                    self._compass_preset = pending.action
                    self._compass_ever_set = True
                    self._add_log(_c(self._theme.pos, f"✓ {pending.display}"))
                    self._set_feedback(_c(self._theme.pos, f"✓ Compass → {pending.display}"))
            else:
                r = await self.agent.act(pending.action, **pending.params)
                cs = _cost_str(_action_cost(r)) if r.success else ""
                icon = "✓" if r.success else "✗"
                color = self._theme.pos if r.success else self._theme.warn
                msg = r.error_message or "failed"
                label = (
                    f"{icon} {pending.display}{cs}"
                    if r.success
                    else f"{icon} {pending.display}: {msg}"
                )
                self._add_log(_c(color, label))
                self._set_feedback(_c(color, label))
        except RateLimitError as exc:
            # Re-queue with another timer — tick boundary not yet reached
            self._schedule_pending(pending, retry_after=exc.retry_after)
            wait = int(exc.retry_after)
            self._add_log(_c("dim", f"⏳ {pending.display}: still rate limited, retry ~{wait}s"))
        except CosmergonError as exc:
            self._add_log(_c(self._theme.warn, f"✗ {pending.display}: {exc}"))
            self._set_feedback(_c(self._theme.warn, f"✗ {pending.display} failed"))

    # --- Redraw ---

    def _update_panel(self, widget_id: str, content: str) -> None:
        """Call Static.update() only when content changed — prevents unnecessary repaints."""
        if self._panel_cache.get(widget_id) != content:
            self._panel_cache[widget_id] = content
            self.query_one(f"#{widget_id}", Static).update(content)

    _FOCUS_TO_PANEL: ClassVar[dict[str, str]] = {
        "agent": "agent-panel", "fields": "agent-panel",
        "log": "log-panel",
    }

    def _sync_focus_border(self) -> None:
        target = self._FOCUS_TO_PANEL.get(self._focus or "")
        if target == self._focus_panel_id:
            return
        if self._focus_panel_id:
            self.query_one(f"#{self._focus_panel_id}", Static).remove_class("panel-focused")
        if target:
            self.query_one(f"#{target}", Static).add_class("panel-focused")
        self._focus_panel_id = target

    def _redraw(self) -> None:
        state = self.agent.state
        self._sync_focus_border()
        self._draw_hint_bar(state)
        self._draw_agent_panel(state)
        self._draw_economy_panel(state)
        self._draw_log_panel(state)
        self._draw_context_bar(state)
        self._draw_fix_bar()
        self._draw_status_bar(state)

    def _draw_agent_panel(self, state: GameState | None) -> None:
        t = self._theme
        focus_marker = _c(t.guide, " ▶") if self._focus in ("agent", "fields") else ""
        lines = [_c(t.struct, "[bold]═ AGENT[/bold]") + focus_marker]

        if not state:
            lines.append(_c("dim", "Connecting..."))
            self._update_panel("agent-panel", "\n".join(lines))
            return

        # Status + energy — bar scales dynamically so it stays meaningful at any balance
        status = "PAUSED" if self._paused else "ACTIVE"
        sc = t.warn if self._paused else t.pos
        dynamic_max = max(5000.0, state.energy * 1.5)
        bar = _energy_bar(state.energy, dynamic_max)
        e_ref = _c("dim", f"/{_energy_ref(state.energy, dynamic_max)} E")
        lines.append(
            f"{_c(sc, f'● {status}')}  {_c(t.data, f'{state.energy:,.0f}')}"
            f"{e_ref}{_c(t.data, f'  {bar}')}"
        )

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
            narrow = self.app.size.width < 70
            for f in state.fields[:_MAX_FIELDS]:
                tier = f"T{f.entity_tier or 0}"
                etype = (f.entity_type or "novice")[:8]
                if narrow:
                    lines.append(_c(t.data, f"  {f.id[:8]} {tier} {etype} {f.active_cell_count} cells"))
                else:
                    bar_f = _energy_bar(f.active_cell_count, 200, 6)
                    lines.append(
                        _c(t.data, f"  {f.id[:8]} {tier} {etype:8s} {bar_f} {f.active_cell_count} cells")
                    )
            hidden = len(state.fields) - _MAX_FIELDS
            if hidden > 0:
                lines.append(_c("dim", f"  ... (+{hidden} more)  \\[V] to browse"))

        self._update_panel("agent-panel", "\n".join(lines))

    def _draw_economy_panel(self, state: GameState | None) -> None:
        t = self._theme
        lines = [_c(t.struct, "[bold]═ ECONOMY[/bold]")]

        if state and state.world_briefing:
            wb = state.world_briefing
            lines.append(_c(t.data, f"Rank:   #{wb.your_rank} / {wb.total_agents}"))
            if wb.top_agent:
                lines.append(_c(t.data, f"Top:    {wb.top_agent[:32]}"))
            lines.append(_c(t.data, f"Market: {wb.market_summary[:32]}"))
            if wb.last_event:
                lines.append(_c("dim", f"Last:   {wb.last_event[:32]}"))
        elif state:
            # No world briefing yet — show basic state so panel isn't empty
            lines.append(_c("dim", "Joining universe..."))
            lines.append(_c(t.data, f"Tick:   {state.tick}"))
            lines.append(_c(t.data, f"Energy: {state.energy:,.0f} E"))
            if state.next_tick_at:
                remaining = int(max(0, state.next_tick_at - time.time()))
                lines.append(_c("dim", f"Next:   ~{remaining}s"))
            elif self._tick_received_at > 0:
                elapsed = time.monotonic() - self._tick_received_at
                remaining = int(max(0, self._tick_interval - elapsed))
                lines.append(_c("dim", f"Next:   ~{remaining}s"))

        self._update_panel("economy-panel", "\n".join(lines))

    def _draw_log_panel(self, state: GameState | None) -> None:
        t = self._theme
        focus_marker = _c(t.guide, " ▶") if self._focus == "log" else ""
        agent_name = (state.agent_name if state and state.agent_name else None) or "Agent"
        hint = _c("dim", r"  \[L] fullscreen  \[M] chat")
        lines = [_c(t.struct, f"[bold]═ LOG[/bold]{focus_marker}") + hint]

        # Learned rules — show last 2 (compact)
        learned = (state.learned_rules if state else None) or []
        if learned:
            for rule in learned[-2:]:
                lines.append(_c("dim", f"  • {rule[:72]}"))

        # Activity feed — fill available panel height so chat stays at the bottom.
        # Fixed rows consumed by other widgets: hint(1)+top(8)+ctx(1)+fix(3)+status(1)=14.
        panel_h = max(6, self.app.size.height - 14)
        learned_count = len(learned[-2:]) if learned else 0
        chat_rows = 3 if self._messages else 0  # separator + up to 2 msgs
        feed_n = max(1, panel_h - 1 - learned_count - chat_rows)
        feed = self._log[-feed_n:]
        if feed:
            lines.extend(feed)
        else:
            # Empty feed — structured welcome block instead of black void.
            # Fills available space with context until real events arrive.
            no_fields = not (state and state.fields)
            welcome: list[str] = [
                _c("dim", "Connecting to cosmergon.com..."),
                "",
                _c(t.struct, "  Conway cells evolve every tick (~60s)."),
                _c("dim",   "  Energy is earned through active patterns."),
                _c("dim",   "  Your agent lives on the server — always on."),
                "",
            ]
            if no_fields:
                welcome += [
                    _c(t.guide, f"  {_hk('F')} Claim a field   {_hk('C')} Set Compass"),
                    _c(t.guide, f"  {_hk('P')} Place cells     {_hk('V')} View field"),
                    _c(t.guide, f"  {_hk('?')} Help"),
                ]
            else:
                welcome += [
                    _c(t.guide, f"  {_hk('C')} Set Compass     {_hk('P')} Place cells"),
                    _c(t.guide, f"  {_hk('V')} View field      {_hk('?')} Help"),
                ]
            welcome.append("")
            welcome.append(_c("dim", "  Activity will appear here once connected."))
            lines.extend(welcome[:feed_n])

        # Chat messages — last 2, shown below activity feed
        if self._messages:
            lines.append(_c("dim", f"─ {agent_name}"))
            for msg in self._messages[-2:]:
                sender = msg.get("sender", "")
                text = msg.get("message", "")
                label = "Du" if sender == "player" else agent_name
                color = t.data if sender == "player" else t.pos
                lines.append(_c(color, f"  \\[{label}] {text[:68]}"))

        self._update_panel("log-panel", "\n".join(lines))

    def _draw_context_bar(self, state: GameState | None) -> None:
        t = self._theme
        esc = f"  {_c(t.cmd, _hk('Esc'))} back"
        if self._focus == "agent":
            hints = "  ".join(
                f"{_c(t.cmd, _hk(str(i + 1)))}{v.split()[1] if ' ' in v else v}"
                for i, v in enumerate(_COMPASS_DISPLAY.values())
            )
            self._update_panel("context-bar", _c("dim", hints) + esc)
        elif self._focus == "fields":
            fields = (state.fields if state else None) or []
            if fields:
                parts = [
                    f"{_c(t.cmd, _hk(str(i + 1)))}{f.id[:8]}"
                    for i, f in enumerate(fields[:5])
                ]
                self._update_panel("context-bar", _c("dim", "  ".join(parts)) + esc)
            else:
                self._update_panel("context-bar", _c("dim", "No fields yet") + esc)
        elif self._focus == "log":
            self._update_panel("context-bar", _c("dim", "[↑/↓] Scroll  [Esc] back"))
        else:
            self._update_panel("context-bar", "")

    def _draw_fix_bar(self) -> None:
        t = self._theme

        def k(key: str, label: str, color: str | None = None) -> str:
            return f"{_c(color or t.cmd, _hk(key))} {label}"

        # [C] orange until first compass use — onboarding signal
        c_color = t.guide if not self._compass_ever_set else t.cmd
        keys = [
            k("Tab", "Focus"), k("C", "Compass", c_color), k("P", "Place"), k("F", "Field"),
            k("E", "Evolve"), k("V", "View"), k("M", "Chat"),
            k("U", "Upgrade"), k("?", "Help"), k("Q", "Quit"),
        ]
        self._update_panel("fix-bar", "  ".join(keys))

    def _draw_status_bar(self, state: GameState | None) -> None:
        name = (state.agent_name if state and state.agent_name else None) or (
            (self.agent.agent_id or "?")[:8]
        )
        sep = " │ "
        # subscription tier in status bar (tick already shown in hint-bar — no duplicate)
        tier = (state.subscription_tier if state else None) or "free"
        segments = [name, tier]
        self._update_panel("status-bar", f"[dim]{sep.join(segments)}[/dim]")

    def _set_feedback(self, msg: str, duration: float = 4.0) -> None:
        """Show a timed message in the hint bar (line 1 only)."""
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

        # Feedback expired — clear it.
        if self._feedback:
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

        return _c("dim", tick_part)

    def _draw_hint_bar(self, state: GameState | None) -> None:
        """Render hint bar: single line of active guidance or feedback."""
        self._update_panel("hint-bar", self._compute_hint(state))


    # --- Actions ---

    def action_cycle_focus(self) -> None:
        """Cycle Tab-focus through panels: None → agent → fields → log → None."""
        order: list[str | None] = [None, "agent", "fields", "log"]
        idx = order.index(self._focus)
        self._focus = order[(idx + 1) % len(order)]

    def on_key(self, event: Any) -> None:
        """Numbers 1-7 set compass preset when AGENT is focused."""
        if self._focus == "agent" and event.key in ("1", "2", "3", "4", "5", "6", "7"):
            idx = int(event.key) - 1
            if idx < len(_COMPASS_PRESETS):
                self._apply_compass_preset(_COMPASS_PRESETS[idx])
                event.prevent_default()

    @work
    async def action_compass(self) -> None:
        labels = [_COMPASS_DISPLAY.get(p, p) for p in _COMPASS_PRESETS]
        idx = await self.push_screen_wait(SelectModal("Set Compass direction", labels))
        if idx is None:
            return
        await self._apply_compass_preset_async(_COMPASS_PRESETS[idx])

    @work
    async def _apply_compass_preset(self, preset: str) -> None:
        """Shared compass-set logic used by [C] modal and number shortcuts."""
        await self._apply_compass_preset_async(preset)

    async def _apply_compass_preset_async(self, preset: str) -> None:
        """Execute compass API call, update log and feedback."""
        compass_label = _COMPASS_DISPLAY.get(preset, preset)
        self._add_log(_c(self._theme.data, f"⠋ compass → {compass_label}..."))
        try:
            result = await self.agent.set_compass(preset)
            if result.get("error"):
                self._add_log(_c(self._theme.warn, f"✗ compass failed: {result['error']}"))
                self._set_feedback(_c(self._theme.warn, "✗ Compass failed"))
            else:
                self._compass_preset = preset
                self._compass_ever_set = True
                self._add_log(_c(self._theme.pos, f"✓ compass: {compass_label}"))
                explanation = (result.get("explanation") or "").strip()
                if explanation:
                    self._add_log(_c("dim", f"  {_truncate_words(explanation, 48)}"))
                self._set_feedback(_c(self._theme.pos, f"✓ Compass → {compass_label}"))
        except RateLimitError as exc:
            wait_str = f" ~{int(exc.retry_after)}s" if exc.retry_after > 1 else ""
            self._schedule_pending(
                _PendingAction(kind="compass", action=preset, params={}, display=compass_label),
                retry_after=exc.retry_after,
            )
            self._add_log(_c("dim", f"⏳ {compass_label} — queued, fires next tick{wait_str}"))
            self._set_feedback(_c("dim", f"⏳ Queued: {compass_label} — next tick{wait_str}"))
        except CsgConnectionError:
            self._schedule_pending(
                _PendingAction(kind="compass", action=preset, params={}, display=compass_label),
                retry_after=10.0,
            )
            self._add_log(_c("dim", f"⏳ {compass_label} — network error, retry next tick"))
            self._set_feedback(_c("dim", f"⏳ {compass_label} — retrying next tick"))
        except CosmergonError as exc:
            self._add_log(_c(self._theme.warn, f"✗ compass failed: {exc}"))
            self._set_feedback(_c(self._theme.warn, "✗ Compass failed"))

    @work
    async def action_place_cells(self) -> None:
        state = self.agent.state
        if not state or not state.fields:
            self._add_log(_c(self._theme.warn, "No fields — press \\[F] first"))
            return
        field_labels = [
            f"{f.id[:8]} T{f.entity_tier or 0} ({f.active_cell_count} cells)" for f in state.fields
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
            cs = _cost_str(_action_cost(r))
            self._add_log(_c(color, f"{icon} place_cells({_PRESETS[pi]}){cs}"))
            label = f"{icon} Cells placed ({_PRESETS[pi]}){cs} — evolves next tick"
            self._set_feedback(_c(color, label))
        except RateLimitError as exc:
            display = f"place_cells({_PRESETS[pi]})"
            wait_str = f" ~{int(exc.retry_after)}s" if exc.retry_after > 1 else ""
            self._schedule_pending(
                _PendingAction(
                    kind="act",
                    action="place_cells",
                    params={"field_id": state.fields[fi].id, "preset": _PRESETS[pi]},
                    display=display,
                ),
                retry_after=exc.retry_after,
            )
            self._add_log(_c("dim", f"⏳ {display} — queued, fires next tick{wait_str}"))
            self._set_feedback(_c("dim", f"⏳ Queued: {display} — next tick{wait_str}"))
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
            cs = _cost_str(_action_cost(r))
            self._add_log(_c(color, f"{icon} create_field{cs}"))
            self._set_feedback(_c(color, f"{icon} Field created{cs} — press \\[P] to place cells"))
        except RateLimitError as exc:
            wait_str = f" ~{int(exc.retry_after)}s" if exc.retry_after > 1 else ""
            self._schedule_pending(
                _PendingAction(
                    kind="act",
                    action="create_field",
                    params={"cube_id": cubes[ci].id},
                    display="create_field",
                ),
                retry_after=exc.retry_after,
            )
            self._add_log(_c("dim", f"⏳ create_field — queued, fires next tick{wait_str}"))
            self._set_feedback(_c("dim", f"⏳ Queued: create_field — next tick{wait_str}"))
        except CosmergonError as exc:
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
            if r.success:
                new_tier = (r.data.get("result") or {}).get("new_tier")
                cs = _cost_str(_action_cost(r))
                tier_str = f" → T{new_tier}" if new_tier else ""
                self._add_log(_c(color, f"{icon} evolve{tier_str}{cs}"))
                label = f"T{new_tier}" if new_tier else "ok"
                self._set_feedback(_c(color, f"{icon} Evolved: {label}{cs}"))
            else:
                msg = r.error_message or "failed"
                self._add_log(_c(color, f"{icon} evolve → {msg}"))
                self._set_feedback(_c(color, f"{icon} Evolve: {msg}"))
        except RateLimitError as exc:
            wait_str = f" ~{int(exc.retry_after)}s" if exc.retry_after > 1 else ""
            self._schedule_pending(
                _PendingAction(
                    kind="act",
                    action="evolve",
                    params={"field_id": state.fields[fi].id},
                    display="evolve",
                ),
                retry_after=exc.retry_after,
            )
            self._add_log(_c("dim", f"⏳ evolve — queued, fires next tick{wait_str}"))
            self._set_feedback(_c("dim", f"⏳ Queued: evolve — next tick{wait_str}"))
        except CosmergonError as exc:
            self._add_log(_c(self._theme.warn, f"✗ evolve: {exc}"))
            self._set_feedback(_c(self._theme.warn, f"✗ Evolve failed: {exc}"))

    async def action_upgrade(self) -> None:
        _upgrade_urls: dict[str, str] = {
            "free": "https://cosmergon.com/solo.html",
            "solo": "https://cosmergon.com/developer-plan.html",
            "developer": "https://cosmergon.com/enterprise.html",
        }
        tier = (self.agent._state.subscription_tier if self.agent._state else "free")
        url = _upgrade_urls.get(tier)
        if url is None:
            # enterprise — no higher tier
            self._set_feedback(_c(self._theme.data, "✓ You are on the top tier (Enterprise)"))
            return
        webbrowser.open(url)
        self._add_log(_c(self._theme.pos, f"✓ Upgrade page opened ({tier} → next tier)"))
        self._set_feedback(_c(self._theme.pos, "✓ Browser opened — complete upgrade there"))

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
        # Delegate to FieldScreen when it is active — App priority binding fires
        # top-down in Textual 8 so it intercepts before FieldScreen's on_key.
        if isinstance(self.screen, FieldScreen):
            self.screen.action_refresh_field()
            return
        self._add_log(_c(self._theme.data, "Refreshing..."))

    @work
    async def action_help(self) -> None:
        await self.push_screen_wait(HelpModal(self._theme.name))

    @work
    async def action_log_screen(self) -> None:
        """Open full-screen LOG view (read-only, scrollable)."""
        await self.push_screen_wait(LogScreen(list(self._log), self._theme))

    @work
    async def action_chat_screen(self) -> None:
        """Open full-screen CHAT view with input field."""
        state = self.agent.state
        agent_name = (state.agent_name if state and state.agent_name else None) or "Agent"
        agent_mode = state.agent_mode if state else "api"
        await self.push_screen_wait(
            ChatScreen(self.agent, list(self._messages), self._theme, agent_name, agent_mode)
        )
        # Refresh messages after modal closes (Esc); next tick will also update.
        try:
            self._messages = await self.agent.get_messages(limit=20)
        except Exception:
            pass

    @work
    async def action_field_view(self) -> None:
        """Open full-screen Field View for the agent's Conway fields."""
        state = self.agent.state
        if not state or not state.fields:
            self._add_log(_c(self._theme.warn, "No fields — press \\[F] first"))
            return
        await self.push_screen_wait(
            FieldScreen(self.agent, list(state.fields), self._theme)
        )


# ---------------------------------------------------------------------------
# Full-screen sub-screens
# ---------------------------------------------------------------------------


class LogScreen(ModalScreen):
    """Full-screen LOG view — all activity entries, scrollable. Esc to close."""

    DEFAULT_CSS = """
    LogScreen {
        align: center middle;
    }
    LogScreen > #log-wrap {
        width: 90%;
        height: 85vh;
        max-height: 50;
        border: solid $accent;
        background: $surface;
    }
    LogScreen > #log-wrap > #log-header {
        height: 1;
        padding: 0 2;
    }
    LogScreen > #log-wrap > VerticalScroll {
        padding: 0 2 1 2;
    }
    """

    def __init__(self, log_entries: list[str], theme: Theme) -> None:
        super().__init__()
        self._log_entries = log_entries
        self._theme = theme

    def compose(self) -> ComposeResult:
        with Vertical(id="log-wrap"):
            yield Label(
                "[dim]↑ ↓ PgUp PgDn to scroll · Esc or Q to close[/dim]",
                id="log-header",
            )
            with VerticalScroll():
                if self._log_entries:
                    for entry in self._log_entries:
                        yield Label(entry)
                else:
                    yield Label("[dim]No activity yet.[/dim]")

    def on_mount(self) -> None:
        vs = self.query_one(VerticalScroll)
        vs.focus()
        vs.scroll_end(animate=False)

    _SCROLL_KEYS: ClassVar[set[str]] = {"up", "down", "pageup", "pagedown", "home", "end"}

    def on_key(self, event: Any) -> None:
        if event.key not in self._SCROLL_KEYS:
            self.dismiss(None)


class ChatScreen(ModalScreen):
    """Full-screen CHAT — scrollable history + input field. Enter sends, Esc closes."""

    DEFAULT_CSS = """
    ChatScreen {
        align: center middle;
    }
    ChatScreen > #chat-wrap {
        width: 90%;
        height: 85vh;
        max-height: 50;
        border: solid $accent;
        background: $surface;
    }
    ChatScreen > #chat-wrap > #chat-header {
        height: 1;
        padding: 0 2;
    }
    ChatScreen > #chat-wrap > #history-scroll {
        padding: 0 2;
        height: 1fr;
    }
    ChatScreen > #chat-wrap > #chat-input {
        height: 3;
        margin: 0 2;
    }
    """

    def __init__(
        self,
        agent: "CosmergonAgent",  # noqa: UP037
        messages: list[dict],
        theme: Theme,
        agent_name: str = "Agent",
        agent_mode: str = "api",
    ) -> None:
        super().__init__()
        self._agent = agent
        self._messages = messages
        self._theme = theme
        self._agent_name = agent_name
        self._agent_mode = agent_mode

    def compose(self) -> ComposeResult:
        if self._agent_mode == "api":
            api_hint = " · Kein Auto-Antwort (API-Modus)"
        else:
            api_hint = " · Antwort ~60s"
        header_text = f"[dim]Chat: {self._agent_name}{api_hint} · Esc: zurück[/dim]"
        with Vertical(id="chat-wrap"):
            yield Label(header_text, id="chat-header")
            with VerticalScroll(id="history-scroll"):
                if self._messages:
                    for msg in self._messages:
                        sender = msg.get("sender", "")
                        text = msg.get("message", "")
                        label = "Du" if sender == "player" else self._agent_name
                        color = self._theme.data if sender == "player" else self._theme.pos
                        yield Label(_c(color, f"[{label}] {text}"))
                else:
                    yield Label("[dim]Noch keine Nachrichten.[/dim]")
            yield Input(placeholder="Nachricht eingeben...", id="chat-input")

    def on_mount(self) -> None:
        self.query_one(VerticalScroll).scroll_end(animate=False)
        self.query_one(Input).focus()

    def on_key(self, event: Any) -> None:
        if event.key == "escape":
            self.dismiss(None)
            event.prevent_default()

    def on_input_submitted(self, event: Any) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.clear()
        self._send(text)

    @work
    async def _send(self, text: str) -> None:
        result = await self._agent.send_message(text)
        scroll = self.query_one("#history-scroll", VerticalScroll)
        if "error" not in result:
            sent_label = _c(self._theme.data, f"[Du] {text}")
            scroll.mount(Label(sent_label))
            scroll.scroll_end(animate=False)
            self.dismiss(text)  # close modal — focus returns to dashboard
        else:
            err = _c(self._theme.warn, f"✗ Fehler: {result['error'][:60]}")
            scroll.mount(Label(err))
            self.query_one(Input).focus()


# ---------------------------------------------------------------------------
# FieldScreen — Conway field visualiser
# ---------------------------------------------------------------------------

_FV_SCROLL_STEP = 1
_FV_SCROLL_STEP_FAST = 8
_FV_MAP_W = 16
_FV_MAP_H = 8
_FV_MINIMAP_THRESHOLD = 60  # only show minimap when content_w >= this


class FieldScreen(ModalScreen):
    """Full-screen Conway field visualiser.  [V] to open, Esc/Q to close.

    Zoom 1 (detail): 1 cell = 1 character, scrollable viewport + minimap.
    Zoom 2 (overview): full 128x128 field compressed to ~32x32 characters.
    """

    DEFAULT_CSS = """
    FieldScreen {
        align: center middle;
    }
    FieldScreen > #fv-wrap {
        width: 92%;
        height: 88vh;
        max-height: 54;
        border: solid $accent;
        background: $surface;
    }
    FieldScreen > #fv-wrap > #fv-header {
        height: 2;
        padding: 0 1;
    }
    FieldScreen > #fv-wrap > #fv-content {
        height: 1fr;
        padding: 0 1;
        overflow: hidden hidden;
    }
    FieldScreen > #fv-wrap > #fv-footer {
        height: 1;
        padding: 0 1;
    }
    """

    # Override app-level 'r' priority binding so FieldScreen handles refresh itself.
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("r", "refresh_field", show=False, priority=True),
    ]

    _NAV_KEYS: ClassVar[set[str]] = {
        "up", "down", "left", "right",
        "ctrl+up", "ctrl+down", "ctrl+left", "ctrl+right",
        "home", "h",
    }

    def __init__(
        self,
        agent: CosmergonAgent,
        fields: list[Field],
        theme: Theme,
        initial_idx: int = 0,
    ) -> None:
        super().__init__()
        self._agent = agent
        self._fields = fields
        self._theme = theme
        self._idx = max(0, min(initial_idx, len(fields) - 1)) if fields else 0
        self._cells: set[tuple[int, int]] = set()
        self._field_w: int = 128
        self._field_h: int = 128
        self._vp_x: int = 0
        self._vp_y: int = 0
        self._zoom: int = 1
        self._loading: bool = True
        self._last_fetched_tick: int = -1
        self._content_w: int = 80  # refined after mount

    def compose(self) -> ComposeResult:
        with Vertical(id="fv-wrap"):
            yield Static("", id="fv-header")
            yield Static("", id="fv-content")
            yield Static("", id="fv-footer")

    def on_mount(self) -> None:
        # Approximate usable content width from terminal size (92% - borders)
        self._content_w = max(40, int(self.app.size.width * 0.92) - 4)
        self.set_interval(0.25, self._redraw)
        self._redraw()  # immediate first render — shows "Loading…" without 0.25s flash
        self._fetch_cells()

    @work
    async def _fetch_cells(self) -> None:
        """Fetch cell data for the current field; centres viewport on first load."""
        self._loading = True
        state = self._agent.state
        self._last_fetched_tick = state.tick if state else 0
        if not self._fields:
            self._loading = False
            return
        field = self._fields[self._idx]
        was_empty = not self._cells
        try:
            raw = await self._agent.get_field_cells(field.id)
            new_cells = _fv_parse_cells(raw)
            # Centre viewport when transitioning from empty → cells (catches the case where
            # cells were placed after the view was opened and the first fetch returned nothing).
            if was_empty and new_cells:
                cx, cy = _fv_centroid(new_cells, self._field_w, self._field_h)
                vp_w, vp_h = self._viewport_dims()
                self._vp_x = max(0, cx - vp_w // 2)
                self._vp_y = max(0, cy - vp_h // 2)
            self._cells = new_cells
        except Exception:
            self._cells = set()
        self._loading = False
        self._redraw()  # render immediately after cells arrive, don't wait for next interval

    def _viewport_dims(self) -> tuple[int, int]:
        """Return ``(vp_w, vp_h)`` in cells for the zoom-1 viewport."""
        map_cols = (_FV_MAP_W + 2) if self._content_w >= _FV_MINIMAP_THRESHOLD else 0
        vp_w = max(10, self._content_w - map_cols - 2)
        # Dynamic height: fv-wrap is 88vh capped at max-height:54, minus header(2)+footer(1)+pad(1)
        vp_h = max(10, min(50, int(self.app.size.height * 0.88) - 4))
        return vp_w, vp_h

    def _redraw(self) -> None:
        """Re-render all FieldScreen panels; auto-refresh cells when tick advances."""
        # Auto-refresh: re-fetch when game tick has advanced since last fetch
        state = self._agent.state
        current_tick = state.tick if state else 0
        if current_tick != self._last_fetched_tick and not self._loading:
            self._last_fetched_tick = current_tick
            self._fetch_cells()

        t = self._theme
        n = len(self._fields)

        # --- Header ---
        if not self._fields:
            h1 = _c(t.struct, "[bold]═ FIELD VIEW[/bold]")
            no_fields_hint = "[dim]  No fields yet — press \\[F][/dim]"
            self.query_one("#fv-header", Static).update(f"{h1}\n{no_fields_hint}")
        else:
            field = self._fields[self._idx]
            tier = f"T{field.entity_tier or 0}"
            etype = (field.entity_type or "novice")
            idx_label = f"[{self._idx + 1}/{n}]"
            zoom_label = "Zoom 2 — full field" if self._zoom == 2 else "Zoom 1 — scrollable"
            h1 = _c(t.struct, "[bold]═ FIELD VIEW[/bold]") + f"  {_c('dim', idx_label)}"
            h2 = _c(
                "dim",
                f"  {field.id[:8]} · {tier} {etype} · {field.active_cell_count} cells · {zoom_label}",
            )
            self.query_one("#fv-header", Static).update(f"{h1}\n{h2}")

        # --- Content ---
        content_widget = self.query_one("#fv-content", Static)
        if self._loading:
            content_widget.update(_c("dim", "  Loading…"))
        elif not self._fields:
            content_widget.update("")
        elif self._zoom == 2:
            # Dynamic size: fill available content area, not hardcoded square
            content_h = max(10, min(50, int(self.app.size.height * 0.88) - 4))
            out_w = min(content_h, max(10, self._content_w - 4))
            out_h = content_h
            rows = _fv_render_zoom2(
                self._cells, self._field_w, self._field_h, out_w, out_h,
                alive_char=f"[{t.pos}]▓[/{t.pos}]",
                dead_char="[dim]░[/dim]",
            )
            content_widget.update("\n".join(rows))
        else:
            vp_w, vp_h = self._viewport_dims()
            vp_rows = _fv_render_zoom1(
                self._cells, self._vp_x, self._vp_y, vp_w, vp_h,
                self._field_w, self._field_h,
                alive_char="█",
                dead_char="·",
            )
            if self._content_w >= _FV_MINIMAP_THRESHOLD:
                mm_lines = _fv_render_minimap(
                    self._cells, self._vp_x, self._vp_y, vp_w, vp_h,
                    self._field_w, self._field_h,
                    map_w=_FV_MAP_W, map_h=_FV_MAP_H,
                    alive_char=f"[{t.pos}]▓[/{t.pos}]",
                    dead_char="[dim]·[/dim]",
                    vp_char=f"[{t.guide}]▒[/{t.guide}]",
                )
                combined: list[str] = []
                for i, vp_row in enumerate(vp_rows):
                    mm = mm_lines[i] if i < len(mm_lines) else ""
                    combined.append(vp_row + ("  " + mm if mm else ""))
                content_widget.update("\n".join(combined))
            else:
                content_widget.update("\n".join(vp_rows))

        # --- Footer ---
        footer_widget = self.query_one("#fv-footer", Static)
        if self._zoom == 2:
            footer_widget.update(
                _c("dim", "Z detail · [ ] field · R refresh · Esc back")
            )
        else:
            footer_widget.update(
                _c("dim", "↑↓←→ scroll · Ctrl+↑↓ fast · H center · Z zoom · [ ] field · R refresh · Esc back")
            )

    def on_key(self, event: Any) -> None:
        k = event.key
        if k == "escape":
            self.dismiss(None)
            event.prevent_default()
        elif k == "z":
            self._zoom = 2 if self._zoom == 1 else 1
            event.prevent_default()
        elif k == "left_square_bracket":
            self._nav_field(-1)
            event.prevent_default()
        elif k == "right_square_bracket":
            self._nav_field(1)
            event.prevent_default()
        elif k in self._NAV_KEYS and self._zoom == 1:
            self._scroll(k)
            event.prevent_default()

    def action_refresh_field(self) -> None:
        """[R] — clear cells and re-fetch so centroid re-centres on manual refresh."""
        self._cells = set()
        self._last_fetched_tick = -1
        self._fetch_cells()

    def _nav_field(self, direction: int) -> None:
        if not self._fields:
            return
        self._idx = (self._idx + direction) % len(self._fields)
        self._cells = set()             # reset so centroid re-centres
        self._last_fetched_tick = -1
        self._fetch_cells()

    def _scroll(self, key: str) -> None:
        step = _FV_SCROLL_STEP_FAST if "ctrl+" in key else _FV_SCROLL_STEP
        vp_w, vp_h = self._viewport_dims()
        max_x = max(0, self._field_w - vp_w)
        max_y = max(0, self._field_h - vp_h)
        if "up" in key:
            self._vp_y = max(0, self._vp_y - step)
        elif "down" in key:
            self._vp_y = min(max_y, self._vp_y + step)
        elif "left" in key:
            self._vp_x = max(0, self._vp_x - step)
        elif "right" in key:
            self._vp_x = min(max_x, self._vp_x + step)
        elif key in ("home", "h"):
            cx, cy = _fv_centroid(self._cells, self._field_w, self._field_h)
            self._vp_x = max(0, min(max_x, cx - vp_w // 2))
            self._vp_y = max(0, min(max_y, cy - vp_h // 2))


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# IdentitySetupScreen — first-time identity setup for auto-generated names
# ---------------------------------------------------------------------------


class IdentitySetupScreen(ModalScreen):
    """Modal shown once when the agent's name matches the auto-generated pattern.

    The user can set a permanent agent name and choose a persona.
    Pressing Esc skips without making any changes.
    """

    DEFAULT_CSS = """
    IdentitySetupScreen {
        align: center middle;
    }
    IdentitySetupScreen > #setup-wrap {
        width: 72;
        height: auto;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    IdentitySetupScreen > #setup-wrap > #setup-header {
        height: 1;
        margin-bottom: 1;
    }
    IdentitySetupScreen > #setup-wrap > #setup-intro {
        height: 2;
        margin-bottom: 1;
    }
    IdentitySetupScreen > #setup-wrap > #name-label {
        height: 1;
    }
    IdentitySetupScreen > #setup-wrap > #name-input {
        height: 3;
        margin-bottom: 1;
    }
    IdentitySetupScreen > #setup-wrap > #persona-label {
        height: 1;
    }
    IdentitySetupScreen > #setup-wrap > #persona-select {
        margin-bottom: 1;
    }
    IdentitySetupScreen > #setup-wrap > #error-label {
        height: 1;
    }
    """

    def __init__(
        self,
        agent: CosmergonAgent,
        current_name: str,
        current_persona: str,
        theme: Theme,
    ) -> None:
        super().__init__()
        self._agent = agent
        self._current_name = current_name
        self._current_persona = current_persona or "scientist"
        self._theme = theme

    def compose(self) -> ComposeResult:
        with Vertical(id="setup-wrap"):
            yield Label(
                "[dim]// Identity Setup · Enter in name field: save · Esc: skip[/dim]",
                id="setup-header",
            )
            yield Label(
                f"Your agent was auto-named [bold]{self._current_name}[/bold].\n"
                "Give it a permanent identity now — or press Esc to skip.",
                id="setup-intro",
            )
            yield Label("Agent name:", id="name-label")
            yield Input(
                value=self._current_name,
                placeholder="e.g. my-agent",
                id="name-input",
            )
            yield Label("Persona (Tab to select):", id="persona-label")
            yield Select(
                options=_PERSONA_OPTIONS,
                value=self._current_persona,
                allow_blank=False,
                id="persona-select",
            )
            yield Label("", id="error-label")

    def on_mount(self) -> None:
        inp = self.query_one("#name-input", Input)
        inp.focus()
        inp.action_end()

    def on_key(self, event: Any) -> None:
        if event.key == "escape":
            self.dismiss(None)
            event.prevent_default()

    def on_input_submitted(self, event: Any) -> None:
        name = event.value.strip()
        if not name:
            self._set_error("Agent name cannot be empty.")
            return
        if len(name) < 3:
            self._set_error("Name must be at least 3 characters.")
            return
        if not re.match(r"^[a-zA-Z0-9_-]+$", name):
            self._set_error("Only letters, digits, _ and - are allowed.")
            return
        persona_select = self.query_one("#persona-select", Select)
        persona = (
            str(persona_select.value)
            if persona_select.value is not Select.NULL
            else "scientist"
        )
        self._save(name, persona)

    @work
    async def _save(self, name: str, persona: str) -> None:
        self._set_status("Saving…")
        result = await self._agent.patch_identity(agent_name=name, persona=persona)
        if "error" in result:
            status = result.get("status_code", 0)
            if status == 409:
                self._set_error(f"Name '{name}' is already taken — choose another.")
            else:
                self._set_error(f"Error: {str(result['error'])[:80]}")
            self.query_one("#name-input", Input).focus()
        else:
            self.dismiss({"agent_name": result.get("username", name), "persona": persona})

    def _set_error(self, msg: str) -> None:
        self.query_one("#error-label", Label).update(_c(self._theme.warn, msg))

    def _set_status(self, msg: str) -> None:
        self.query_one("#error-label", Label).update(_c("dim", msg))


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

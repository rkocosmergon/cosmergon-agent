"""Terminal dashboard for Cosmergon agents — htop for AI agents.

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
import asyncio
import curses
import logging
import os
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cosmergon_agent import CosmergonAgent, __version__
from cosmergon_agent.state import GameState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_CHECK = "✓"
_CROSS = "✗"
_BULLET_ON = "●"
_BULLET_OFF = "○"
_ARROW = "→"

_DRAW_INTERVAL = 0.1  # seconds between redraws (10 fps)
_MAX_LOG = 50
_MAX_FIELDS = 5
_MAX_SELECT = 9
_ENERGY_FLASH_TICKS = 5  # 0.5 s at 10 fps
_HEARTBEAT_TICKS = 20  # 2 s period

_PRESETS = ["block", "blinker", "toad", "glider", "r_pentomino", "pentadecathlon", "pulsar"]

_COMPASS_PRESETS = ["attack", "defend", "grow", "trade", "cooperate", "explore", "autonomous"]
_COMPASS_DISPLAY = {
    "attack": "⚔  Attack",
    "defend": "🛡  Defend",
    "grow": "🌱  Grow",
    "trade": "💹  Trade",
    "cooperate": "🤝  Cooperate",
    "explore": "🔭  Explore",
    "autonomous": "~  Autonomous",
}

# ---------------------------------------------------------------------------
# Theme system
# ---------------------------------------------------------------------------

# curses color-pair indices — semantic names, never use raw numbers in drawing code
_CP_RED = 1
_CP_YELLOW = 2
_CP_GREEN = 3
_CP_CYAN = 4
_CP_WHITE = 5
_CP_MAGENTA = 6
_CP_ORANGE = 7  # 256-color orange; falls back to yellow on 8-color terminals


@dataclass(frozen=True)
class Theme:
    """Named color slots. Every draw call uses these — no magic numbers."""

    name: str
    cmd: int  # Hotkeys / clickable actions
    guide: int  # Onboarding hints, first-action highlight
    pos: int  # Positive / gain
    warn: int  # Warning / loss / error
    struct: int  # Headers, separators
    data: int  # Neutral data text


THEMES: dict[str, Theme] = {
    "cosmergon": Theme(
        name="cosmergon",
        cmd=_CP_CYAN,
        guide=_CP_ORANGE,  # orange if 256-color, else yellow
        pos=_CP_GREEN,
        warn=_CP_RED,
        struct=_CP_WHITE,
        data=_CP_WHITE,
    ),
    "matrix": Theme(
        name="matrix",
        cmd=_CP_GREEN,
        guide=_CP_GREEN,
        pos=_CP_GREEN,
        warn=_CP_RED,
        struct=_CP_GREEN,
        data=_CP_GREEN,
    ),
    "mono": Theme(
        name="mono",
        cmd=_CP_WHITE,
        guide=_CP_WHITE,
        pos=_CP_WHITE,
        warn=_CP_WHITE,
        struct=_CP_WHITE,
        data=_CP_WHITE,
    ),
    "high-contrast": Theme(
        name="high-contrast",
        cmd=_CP_YELLOW,
        guide=_CP_CYAN,
        pos=_CP_GREEN,
        warn=_CP_RED,
        struct=_CP_WHITE,
        data=_CP_WHITE,
    ),
}

_COLOR_NAMES: dict[str, int] = {
    "red": _CP_RED,
    "yellow": _CP_YELLOW,
    "green": _CP_GREEN,
    "cyan": _CP_CYAN,
    "white": _CP_WHITE,
    "magenta": _CP_MAGENTA,
    "orange": _CP_ORANGE,
}


def _load_theme(cli_theme: str | None = None) -> Theme:
    """Resolve theme: CLI arg > COSMERGON_THEME env > ~/.cosmergon/dashboard.toml > default."""
    if cli_theme:
        return THEMES.get(cli_theme, THEMES["cosmergon"])

    env = os.environ.get("COSMERGON_THEME")
    if env:
        return THEMES.get(env, THEMES["cosmergon"])

    cfg = Path.home() / ".cosmergon" / "dashboard.toml"
    if cfg.exists():
        try:
            try:
                import tomllib  # Python 3.11+
            except ImportError:
                try:
                    import tomli as tomllib  # type: ignore[no-redef]
                except ImportError:
                    return THEMES["cosmergon"]
            with cfg.open("rb") as fh:
                data = tomllib.load(fh)
            dash = data.get("dashboard", {})
            name = dash.get("theme")
            if name and name in THEMES:
                return THEMES[name]
            # Custom theme: inherit from cosmergon, override named slots
            custom = dash.get("custom", {})
            if custom:
                base = THEMES["cosmergon"]
                slots = {
                    s: _COLOR_NAMES.get(custom[s], getattr(base, s))
                    for s in ("cmd", "guide", "pos", "warn", "struct", "data")
                    if s in custom
                }
                return Theme(
                    name="custom",
                    **{
                        **{
                            "cmd": base.cmd,
                            "guide": base.guide,
                            "pos": base.pos,
                            "warn": base.warn,
                            "struct": base.struct,
                            "data": base.data,
                        },
                        **slots,
                    },
                )
        except Exception:
            pass

    return THEMES["cosmergon"]


def _init_colors(theme: Theme) -> None:
    """Initialise all curses color pairs. Call once after curses.start_color()."""
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(_CP_RED, curses.COLOR_RED, -1)
    curses.init_pair(_CP_YELLOW, curses.COLOR_YELLOW, -1)
    curses.init_pair(_CP_GREEN, curses.COLOR_GREEN, -1)
    curses.init_pair(_CP_CYAN, curses.COLOR_CYAN, -1)
    curses.init_pair(_CP_WHITE, curses.COLOR_WHITE, -1)
    curses.init_pair(_CP_MAGENTA, curses.COLOR_MAGENTA, -1)
    # Orange: xterm-256 color 208 if available, else yellow
    orange_fg = 208 if curses.COLORS >= 256 else curses.COLOR_YELLOW
    curses.init_pair(_CP_ORANGE, orange_fg, -1)


# ---------------------------------------------------------------------------
# Animation state
# ---------------------------------------------------------------------------


@dataclass
class _AnimState:
    """All animation counters in one place. Driven by the draw loop (no extra tasks)."""

    draw_tick: int = 0
    energy_flash: int = 0
    energy_flash_positive: bool = True
    last_energy: float = 0.0
    pending: dict[str, str] = field(default_factory=dict)  # log_id → label

    def tick(self) -> None:
        self.draw_tick += 1
        if self.energy_flash > 0:
            self.energy_flash -= 1

    @property
    def spinner(self) -> str:
        return _SPINNER[self.draw_tick % len(_SPINNER)]

    @property
    def heartbeat(self) -> str:
        return _BULLET_ON if (self.draw_tick // _HEARTBEAT_TICKS) % 2 == 0 else _BULLET_OFF

    def note_energy(self, energy: float) -> None:
        if self.last_energy and energy != self.last_energy:
            self.energy_flash = _ENERGY_FLASH_TICKS
            self.energy_flash_positive = energy > self.last_energy
        self.last_energy = energy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _energy_bar(energy: float, max_e: float = 5000.0, width: int = 8) -> str:
    ratio = min(1.0, max(0.0, energy / max_e))
    full = int(ratio * width)
    half = int((ratio * width - full) * 2)
    return "▓" * full + ("▒" if half else "") + "░" * max(0, width - full - half)


class _QuitDashboardError(Exception):
    pass


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


class Dashboard:
    """Curses-based agent dashboard wrapping CosmergonAgent."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://cosmergon.com",
        theme: Theme | None = None,
    ) -> None:
        self.agent = CosmergonAgent(api_key=api_key, base_url=base_url, poll_interval=10.0)
        self._theme = theme or THEMES["cosmergon"]
        self._anim = _AnimState()
        self._log: list[tuple[str, int]] = []  # (text, color_pair_index)
        self._paused = False
        self._compass_preset: str = "autonomous"
        self._compass_ever_set: bool = False
        self._last_decision: dict | None = None
        self._register_handlers()

    def _register_handlers(self) -> None:
        @self.agent.on_tick
        async def _tick(state: GameState) -> None:
            self._anim.note_energy(state.energy)
            delta = state.energy - self._anim.last_energy
            sign = "+" if delta >= 0 else ""
            color = self._theme.pos if delta >= 0 else self._theme.warn
            self._add_log(f"[{state.tick}] {sign}{delta:.0f}E  energy {state.energy:.0f}", color)
            # Fetch last decision without blocking the tick handler
            asyncio.create_task(self._refresh_decision())  # noqa: RUF006

        @self.agent.on_error
        async def _error(result: Any) -> None:
            self._add_log(f"{_CROSS} {result.action}: {result.error_message}", self._theme.warn)

    async def _refresh_decision(self) -> None:
        try:
            d = await self.agent.get_last_decision()
            if d:
                self._last_decision = d
        except Exception:
            pass

    def _add_log(self, msg: str, color: int) -> None:
        self._log.append((msg, color))
        if len(self._log) > _MAX_LOG:
            self._log = self._log[-_MAX_LOG:]

    def run(self) -> None:
        try:
            curses.wrapper(self._main)
        except KeyboardInterrupt:
            pass

    def _main(self, stdscr: curses.window) -> None:
        stdscr.nodelay(True)
        curses.curs_set(0)
        _init_colors(self._theme)
        asyncio.run(self._loop(stdscr))

    async def _loop(self, stdscr: curses.window) -> None:
        agent_task = asyncio.create_task(self.agent.start())
        self._add_log("Connecting...", self._theme.data)
        try:
            while True:
                key = stdscr.getch()
                if key != -1 and await self._handle_key(key, stdscr) == "quit":
                    break
                self._anim.tick()
                self._draw(stdscr)
                await asyncio.sleep(_DRAW_INTERVAL)
        except _QuitDashboardError:
            pass
        finally:
            self.agent._running = False
            agent_task.cancel()
            try:
                await agent_task
            except (asyncio.CancelledError, Exception):
                pass
            await self.agent.close()

    # --- Key handling ---

    async def _handle_key(self, key: int, stdscr: curses.window) -> str | None:
        ch = chr(key).upper() if 32 <= key < 127 else ""
        if ch == "Q":
            return "quit"
        if ch == "C":
            await self._action_compass(stdscr)
        elif ch == "P":
            await self._action_place_cells(stdscr)
        elif ch == "F":
            await self._action_create_field(stdscr)
        elif ch == "E":
            await self._action_evolve(stdscr)
        elif ch == "U":
            await self._action_upgrade()
        elif ch == " ":
            await self._toggle_pause()
        elif ch == "R":
            asyncio.create_task(self._refresh_decision())  # noqa: RUF006
            self._add_log("Refreshing...", self._theme.data)
        elif ch == "?":
            self._show_help(stdscr)
        return None

    async def _toggle_pause(self) -> None:
        action = "resume" if self._paused else "pause"
        r = await self.agent.act(action)
        self._paused = not self._paused
        color = self._theme.pos if r.success else self._theme.warn
        self._add_log(f"{_CHECK if r.success else _CROSS} {action}", color)

    async def _action_compass(self, stdscr: curses.window) -> None:
        labels = [_COMPASS_DISPLAY.get(p, p) for p in _COMPASS_PRESETS]
        idx = self._select(stdscr, "Compass — Richtung wählen", labels)
        if idx is None:
            return
        preset = _COMPASS_PRESETS[idx]
        log_id = f"compass_{preset}"
        self._anim.pending[log_id] = f"Compass → {preset}"
        self._add_log(f"{self._anim.spinner} compass → {preset}...", self._theme.guide)
        try:
            result = await self.agent.set_compass(preset)
            self._compass_preset = preset
            self._compass_ever_set = True
            explanation = (result.get("explanation") or "")[:60]
            self._add_log(f"{_CHECK} compass: {preset}  {explanation}", self._theme.pos)
        except Exception as exc:
            self._add_log(f"{_CROSS} compass failed: {exc}", self._theme.warn)
        finally:
            self._anim.pending.pop(log_id, None)

    async def _action_place_cells(self, stdscr: curses.window) -> None:
        state = self.agent.state
        if not state or not state.fields:
            self._add_log("No fields — press [F] first", self._theme.warn)
            return
        fi = self._select(
            stdscr,
            "Field",
            [f"{f.id[:8]} T{f.entity_tier or 0} ({f.active_cell_count}c)" for f in state.fields],
        )
        if fi is None:
            return
        pi = self._select(stdscr, "Preset", _PRESETS)
        if pi is None:
            return
        r = await self.agent.act("place_cells", field_id=state.fields[fi].id, preset=_PRESETS[pi])
        color = self._theme.pos if r.success else self._theme.warn
        self._add_log(f"{_CHECK if r.success else _CROSS} place_cells({_PRESETS[pi]})", color)

    async def _action_create_field(self, stdscr: curses.window) -> None:
        state = self.agent.state
        if not state:
            return
        cubes = state.cubes or state.universe_cubes
        if not cubes:
            self._add_log("No cubes available", self._theme.warn)
            return
        ci = self._select(stdscr, "Cube", [f"{c.id[:8]} {c.name}" for c in cubes])
        if ci is None:
            return
        r = await self.agent.act("create_field", cube_id=cubes[ci].id)
        color = self._theme.pos if r.success else self._theme.warn
        self._add_log(f"{_CHECK if r.success else _CROSS} create_field", color)

    async def _action_evolve(self, stdscr: curses.window) -> None:
        state = self.agent.state
        if not state or not state.fields:
            self._add_log("No fields to evolve", self._theme.warn)
            return
        fi = self._select(
            stdscr,
            "Evolve",
            [f"{f.id[:8]} T{f.entity_tier or 0} reife={f.reife_score}" for f in state.fields],
        )
        if fi is None:
            return
        r = await self.agent.act("evolve", field_id=state.fields[fi].id)
        color = self._theme.pos if r.success else self._theme.warn
        msg = r.error_message or "ok"
        self._add_log(f"{_CHECK if r.success else _CROSS} evolve -> {msg}", color)

    async def _action_upgrade(self) -> None:
        """Open Stripe Checkout in browser via the authenticated upgrade-link endpoint."""
        self._add_log(f"{self._anim.spinner} Upgrade-Seite wird geöffnet...", self._theme.guide)
        try:
            resp = await self.agent._request(
                "GET",
                "/api/v1/billing/upgrade-link",
                params={"tier": "developer"},
                follow_redirects=False,
            )
            url = resp.headers.get("location", "https://cosmergon.com/pricing")
            webbrowser.open(url)
            short = url[:70] + "…" if len(url) > 70 else url
            self._add_log(f"{_CHECK} Browser geöffnet", self._theme.pos)
            self._add_log(f"  {short}", self._theme.data)
        except Exception as exc:
            self._add_log(f"{_CROSS} Upgrade-Link Fehler: {exc}", self._theme.warn)

    # --- Drawing ---

    def _draw(self, stdscr: curses.window) -> None:
        try:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            if h < 15 or w < 50:
                stdscr.addstr(0, 0, "Terminal too small (min 50x15)")
                stdscr.refresh()
                return
            state = self.agent.state
            self._draw_title(stdscr, w, state)
            if not state:
                self._safe_str(stdscr, 3, 2, "Connecting...", self._theme.guide)
                self._draw_log(stdscr, 6, h, w)
                self._draw_footer(stdscr, h, w, state)
                stdscr.refresh()
                return
            mid = w // 2
            row_agent = self._draw_agent_panel(stdscr, state, mid - 2)
            row_right = self._draw_right_panel(stdscr, state, mid, w)
            log_start = max(row_agent, row_right) + 1
            self._draw_log(stdscr, log_start, h - 3, w)
            self._draw_footer(stdscr, h, w, state)
            stdscr.refresh()
        except curses.error:
            logger.debug("Draw interrupted (terminal resize)")

    def _draw_title(self, stdscr: curses.window, w: int, state: GameState | None) -> None:
        title = " COSMERGON "
        tick = f" Tick {state.tick} " if state else " connecting… "
        quit_hint = " [Q]uit "
        self._safe_str(stdscr, 0, 0, "─" * w, self._theme.struct)
        self._safe_str(stdscr, 0, 2, title, self._theme.struct, curses.A_BOLD)
        self._safe_str(stdscr, 0, 2 + len(title), quit_hint, self._theme.cmd)
        if w > len(title) + len(quit_hint) + len(tick) + 6:
            self._safe_str(stdscr, 0, w - len(tick) - 2, tick, self._theme.struct)

    def _draw_agent_panel(self, stdscr: curses.window, state: GameState, width: int) -> int:
        t = self._theme
        y = 2
        self._safe_str(stdscr, y, 1, "═ AGENT ", t.struct, curses.A_BOLD)
        y += 1

        # Heartbeat + status
        hb = self._anim.heartbeat
        status = "PAUSED" if self._paused else "AKTIV"
        status_color = t.warn if self._paused else t.pos
        self._safe_str(stdscr, y, 3, f"{hb} ", t.pos)
        self._safe_str(stdscr, y, 5, status, status_color, curses.A_BOLD)
        y += 1

        # Energy bar with flash
        bar = _energy_bar(state.energy)
        if self._anim.energy_flash > 0:
            e_color = t.pos if self._anim.energy_flash_positive else t.warn
            e_attr = curses.A_BOLD
        else:
            e_color, e_attr = t.data, 0
        self._safe_str(stdscr, y, 3, f"Energie: {bar}  {state.energy:,.0f} E", e_color, e_attr)
        y += 1

        tier_str = f"T{state.ranking.player_tier} {state.ranking.tier_name}"
        self._kv(stdscr, y, 3, "Tier ", tier_str, t.data)
        y += 1
        self._kv(stdscr, y, 3, "Score", f"{state.ranking.player_score:,.0f}", t.data)
        y += 1
        self._kv(stdscr, y, 3, "Agent", state.agent_id[:16], t.data)
        y += 2

        # Compass — highlighted until first use
        compass_label = _COMPASS_DISPLAY.get(self._compass_preset, self._compass_preset)
        self._safe_str(stdscr, y, 3, f"Compass: {compass_label}", t.data)
        y += 1
        if not self._compass_ever_set:
            hint = f"{_ARROW} [C] Richtung setzen"
            self._safe_str(stdscr, y, 3, hint, t.guide, curses.A_BOLD)
        else:
            self._safe_str(stdscr, y, 3, "[C] ändern", t.cmd)
        y += 1

        # Pending spinner entries
        for label in list(self._anim.pending.values()):
            self._safe_str(stdscr, y, 3, f"{self._anim.spinner} {label}", t.guide)
            y += 1

        # Fields
        if state.fields:
            y += 1
            self._safe_str(stdscr, y, 1, "═ FELDER ", t.struct, curses.A_BOLD)
            y += 1
            for f in state.fields[:_MAX_FIELDS]:
                tier = f"T{f.entity_tier or 0}"
                etype = (f.entity_type or "novice")[:8]
                bar_f = _energy_bar(f.active_cell_count, 200, 6)
                line = f"  {f.id[:8]} {tier} {etype:8s} {bar_f} {f.active_cell_count}c"
                self._safe_str(stdscr, y, 1, line[:width], t.data)
                y += 1

        return y

    def _draw_right_panel(self, stdscr: curses.window, state: GameState, x: int, w: int) -> int:
        t = self._theme
        panel_w = w - x - 2
        y = 2

        # Economy / world briefing
        self._safe_str(stdscr, y, x, "═ WIRTSCHAFT ", t.struct, curses.A_BOLD)
        y += 1
        if state.world_briefing:
            wb = state.world_briefing
            self._kv(stdscr, y, x + 1, "Rang  ", f"#{wb.your_rank} / {wb.total_agents}", t.data)
            y += 1
            if wb.top_agent:
                self._kv(stdscr, y, x + 1, "Top   ", wb.top_agent[: panel_w - 10], t.data)
                y += 1
            self._kv(stdscr, y, x + 1, "Markt ", wb.market_summary[: panel_w - 10], t.data)
            y += 1
            if wb.last_event:
                self._kv(stdscr, y, x + 1, "Event ", wb.last_event[: panel_w - 10], t.warn)
                y += 1
            if wb.tip:
                tip = wb.tip[: panel_w - 3]
                self._safe_str(stdscr, y, x + 1, f"→ {tip}", t.data)
                y += 1
        y += 1

        # Last decision
        self._safe_str(stdscr, y, x, "═ LETZTE ENTSCHEIDUNG ", t.struct, curses.A_BOLD)
        y += 1
        if self._last_decision:
            d = self._last_decision
            action = d.get("action", "?")
            tick_d = d.get("tick", "?")
            outcome = d.get("outcome") or ""
            reasoning = d.get("reasoning") or ""

            self._safe_str(stdscr, y, x + 1, f"tick {tick_d}: {action}", t.data, curses.A_BOLD)
            y += 1

            # Reasoning — show first 80 chars; tease paid feature for free tier
            if reasoning:
                snippet = reasoning[: panel_w - 3]
                self._safe_str(stdscr, y, x + 1, f'"{snippet}"', t.data)
                y += 1
                if len(reasoning) > panel_w - 3 and state.subscription_tier == "free":
                    self._safe_str(stdscr, y, x + 1, "[U] voller Prompt → Developer", t.guide)
                    y += 1
            if outcome:
                self._safe_str(stdscr, y, x + 1, outcome[: panel_w - 2], t.pos)
                y += 1
        else:
            self._safe_str(stdscr, y, x + 1, "Warte auf erste Entscheidung…", t.data)
            y += 1

        return y

    def _draw_log(self, stdscr: curses.window, start_y: int, end_y: int, w: int) -> None:
        if start_y >= end_y:
            return
        self._safe_str(stdscr, start_y, 1, "═ LOG ", self._theme.struct, curses.A_BOLD)
        available = end_y - start_y - 1
        entries = self._log[-available:] if available > 0 else []
        for i, (msg, color) in enumerate(entries):
            self._safe_str(stdscr, start_y + 1 + i, 3, msg[: w - 5], color)

    def _draw_footer(self, stdscr: curses.window, h: int, w: int, state: GameState | None) -> None:
        t = self._theme
        sep_y = h - 3
        self._safe_str(stdscr, sep_y, 0, "─" * w, t.struct)

        # Single hotkey line — all cyan
        tier = state.subscription_tier if state else "free"
        upgrade = "  [U] Upgrade" if tier in ("free", "anonymous") else ""
        hotkeys = f"[C]  [P]lace  [F]ield  [E]volve  [Space]Pause  [R]efresh  [?]{upgrade}"
        self._safe_str(stdscr, sep_y + 1, 2, hotkeys[: w - 4], t.cmd)

        # Status bar
        agent_id = (self.agent.agent_id or "?")[:8]
        status = f" {agent_id} │ sdk {__version__} │ theme {self._theme.name} "
        self._safe_str(stdscr, h - 1, 0, "─" * w, t.struct)
        if len(status) < w - 4:
            self._safe_str(stdscr, h - 1, 2, status, t.struct)

    # --- UI helpers ---

    @staticmethod
    def _safe_str(
        stdscr: curses.window,
        y: int,
        x: int,
        text: str,
        color: int,
        attr: int = 0,
    ) -> None:
        try:
            stdscr.addstr(y, x, text, curses.color_pair(color) | attr)
        except curses.error:
            pass

    @staticmethod
    def _kv(stdscr: curses.window, y: int, x: int, key: str, value: str, color: int) -> None:
        try:
            stdscr.addstr(y, x, f"{key}: ", curses.color_pair(_CP_MAGENTA))
            stdscr.addstr(y, x + len(key) + 2, value, curses.color_pair(color))
        except curses.error:
            pass

    def _select(self, stdscr: curses.window, title: str, options: list[str]) -> int | None:
        """Numbered overlay — returns index or None on Esc."""
        h, w = stdscr.getmaxyx()
        display = options[:_MAX_SELECT]
        box_h = min(len(display) + 4, h - 4)
        box_w = min(max((len(o) for o in display), default=10) + 12, w - 4)
        win = curses.newwin(box_h, box_w, (h - box_h) // 2, (w - box_w) // 2)
        win.box()
        win.addstr(0, 2, f" {title} ", curses.color_pair(self._theme.cmd) | curses.A_BOLD)
        for i, opt in enumerate(display):
            win.addstr(
                i + 2,
                3,
                f"[{i + 1}] {opt[: box_w - 10]}",
                curses.color_pair(self._theme.data),
            )
        win.addstr(
            box_h - 1,
            2,
            " [1-9] waehlen  [Esc] zurueck ",
            curses.color_pair(self._theme.cmd),
        )
        win.refresh()
        while True:
            key = stdscr.getch()
            if key == 27:
                return None
            if 49 <= key <= 57:
                idx = key - 49
                if idx < len(options):
                    return idx

    def _show_help(self, stdscr: curses.window) -> None:
        lines = [
            "COSMERGON DASHBOARD",
            "",
            "[C]  Compass-Richtung setzen",
            "[P]  Zellen auf Feld platzieren",
            "[F]  Neues Feld erstellen",
            "[E]  Entity weiterentwickeln",
            "[Space]  Pause / Fortsetzen",
            "[U]  Upgrade → Developer (öffnet Browser)",
            "[R]  Daten aktualisieren",
            "[Q]  Beenden",
            "",
            f"Theme: {self._theme.name}   SDK: {__version__}",
            "Themes: cosmergon  matrix  mono  high-contrast",
            "Config: ~/.cosmergon/dashboard.toml",
            "",
            "Taste drücken zum Schließen.",
        ]
        h, w = stdscr.getmaxyx()
        box_h = len(lines) + 4
        box_w = max((len(ln) for ln in lines), default=20) + 6
        win = curses.newwin(
            min(box_h, h - 2),
            min(box_w, w - 2),
            max(0, (h - box_h) // 2),
            max(0, (w - box_w) // 2),
        )
        win.box()
        for i, ln in enumerate(lines):
            attr = curses.A_BOLD if i == 0 else 0
            color = self._theme.cmd if i == 0 else self._theme.data
            try:
                win.addstr(i + 2, 3, ln, curses.color_pair(color) | attr)
            except curses.error:
                pass
        win.refresh()
        stdscr.nodelay(False)
        stdscr.getch()
        stdscr.nodelay(True)


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
    parser.add_argument(
        "--theme",
        choices=list(THEMES),
        default=None,
        help="Color theme (default: cosmergon)",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    theme = _load_theme(args.theme)
    Dashboard(api_key=args.api_key, base_url=args.base_url, theme=theme).run()


if __name__ == "__main__":
    main()

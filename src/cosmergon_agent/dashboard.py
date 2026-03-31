"""Terminal dashboard for Cosmergon agents — htop for AI agents.

Usage:
    cosmergon-dashboard                    # auto-register, connect, play
    cosmergon-dashboard --api-key KEY      # use existing key
    python -m cosmergon_agent.dashboard    # same as above

Hotkeys:
    P  Place cells       F  Create field     C  Create cube
    E  Evolve entity     Space  Pause/Resume
    R  Refresh now       Q  Quit             ?  Help
"""

from __future__ import annotations

import argparse
import asyncio
import curses
import logging
from typing import Any

from cosmergon_agent import CosmergonAgent, __version__
from cosmergon_agent.state import GameState

logger = logging.getLogger(__name__)

_PRESETS = [
    "block", "blinker", "toad", "glider",
    "r_pentomino", "pentadecathlon", "pulsar",
]
_MAX_LOG_ENTRIES = 50
_DRAW_INTERVAL = 0.1
_MAX_DISPLAYED_FIELDS = 6
_MAX_SELECT_OPTIONS = 9


class _QuitDashboard(Exception):
    """Raised to exit the dashboard loop."""


class Dashboard:
    """Curses-based agent dashboard wrapping CosmergonAgent."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://cosmergon.com",
    ) -> None:
        self.agent = CosmergonAgent(
            api_key=api_key, base_url=base_url, poll_interval=10.0,
        )
        self._log: list[tuple[str, int]] = []
        self._last_energy: float = 0
        self._paused = False
        self._register_handlers()

    def _register_handlers(self) -> None:
        """Register SDK event handlers for log updates."""
        @self.agent.on_tick
        async def _tick(state: GameState) -> None:
            delta = state.energy - self._last_energy if self._last_energy else 0
            sign = "+" if delta >= 0 else ""
            self._add_log(
                f"[{state.tick}] tick — energy {state.energy:.0f}"
                f" ({sign}{delta:.0f})",
                3 if delta >= 0 else 1,
            )
            self._last_energy = state.energy

        @self.agent.on_error
        async def _error(result: Any) -> None:
            self._add_log(
                f"ERROR: {result.action} — {result.error_message}", 1,
            )

    def _add_log(self, msg: str, color: int = 0) -> None:
        self._log.append((msg, color))
        if len(self._log) > _MAX_LOG_ENTRIES:
            self._log = self._log[-_MAX_LOG_ENTRIES:]

    def run(self) -> None:
        """Start the dashboard (blocking)."""
        try:
            curses.wrapper(self._main)
        except KeyboardInterrupt:
            pass

    def _main(self, stdscr: curses.window) -> None:
        stdscr.nodelay(True)
        curses.curs_set(0)
        self._init_colors()
        asyncio.run(self._loop(stdscr))

    @staticmethod
    def _init_colors() -> None:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_RED, -1)
        curses.init_pair(2, curses.COLOR_YELLOW, -1)
        curses.init_pair(3, curses.COLOR_GREEN, -1)
        curses.init_pair(4, curses.COLOR_CYAN, -1)
        curses.init_pair(5, curses.COLOR_WHITE, -1)
        curses.init_pair(6, curses.COLOR_MAGENTA, -1)

    async def _loop(self, stdscr: curses.window) -> None:
        agent_task = asyncio.create_task(self.agent.start())
        self._add_log("Connecting...", 2)
        try:
            while True:
                key = stdscr.getch()
                if key != -1:
                    if await self._handle_key(key, stdscr) == "quit":
                        break
                self._draw(stdscr)
                await asyncio.sleep(_DRAW_INTERVAL)
        except _QuitDashboard:
            pass
        finally:
            self.agent._running = False
            agent_task.cancel()
            try:
                await agent_task
            except (asyncio.CancelledError, Exception):
                logger.debug("Agent task ended")
            await self.agent.close()

    # --- Key handling ---

    async def _handle_key(
        self, key: int, stdscr: curses.window,
    ) -> str | None:
        ch = chr(key).upper() if 32 <= key < 127 else ""
        if ch == "Q":
            return "quit"
        if ch == "R":
            self._add_log("Manual refresh...", 2)
        elif ch == " ":
            await self._toggle_pause()
        elif ch == "P":
            await self._action_place_cells(stdscr)
        elif ch == "F":
            await self._action_create_field(stdscr)
        elif ch == "C":
            await self._action_create_cube()
        elif ch == "E":
            await self._action_evolve(stdscr)
        elif ch == "?":
            self._show_help(stdscr)
        return None

    async def _toggle_pause(self) -> None:
        action = "resume" if self._paused else "pause"
        r = await self.agent.act(action)
        self._paused = not self._paused
        status = "OK" if r.success else r.error_message
        self._add_log(f"{action} → {status}", 3 if r.success else 1)

    async def _action_place_cells(self, stdscr: curses.window) -> None:
        state = self.agent.state
        if not state or not state.fields:
            self._add_log("No fields — press [F] first", 2)
            return
        fi = self._select(stdscr, "Field", [
            f"{f.id[:8]} T{f.entity_tier or 0} ({f.active_cell_count}c)"
            for f in state.fields
        ])
        if fi is None:
            return
        pi = self._select(stdscr, "Preset", _PRESETS)
        if pi is None:
            return
        r = await self.agent.act(
            "place_cells", field_id=state.fields[fi].id,
            preset=_PRESETS[pi],
        )
        status = "OK" if r.success else r.error_message
        self._add_log(
            f"place_cells({_PRESETS[pi]}) → {status}",
            3 if r.success else 1,
        )

    async def _action_create_field(self, stdscr: curses.window) -> None:
        state = self.agent.state
        if not state:
            return
        cubes = state.cubes or state.universe_cubes
        if not cubes:
            self._add_log("No cubes available", 2)
            return
        ci = self._select(stdscr, "Cube", [
            f"{c.id[:8]} {c.name}" for c in cubes
        ])
        if ci is None:
            return
        r = await self.agent.act("create_field", cube_id=cubes[ci].id)
        status = "OK" if r.success else r.error_message
        self._add_log(f"create_field → {status}", 3 if r.success else 1)

    async def _action_create_cube(self) -> None:
        r = await self.agent.act("create_cube")
        status = "OK" if r.success else r.error_message
        self._add_log(f"create_cube → {status}", 3 if r.success else 1)

    async def _action_evolve(self, stdscr: curses.window) -> None:
        state = self.agent.state
        if not state or not state.fields:
            self._add_log("No fields to evolve", 2)
            return
        fi = self._select(stdscr, "Evolve", [
            f"{f.id[:8]} T{f.entity_tier or 0} reife={f.reife_score}"
            for f in state.fields
        ])
        if fi is None:
            return
        r = await self.agent.act("evolve", field_id=state.fields[fi].id)
        status = "OK" if r.success else r.error_message
        self._add_log(f"evolve → {status}", 3 if r.success else 1)

    # --- UI drawing (split into <40-line functions) ---

    def _draw(self, stdscr: curses.window) -> None:
        """Redraw entire screen from current state."""
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
                stdscr.addstr(3, 2, "Connecting...", curses.color_pair(2))
                self._draw_log(stdscr, 6, h, w)
                stdscr.refresh()
                return

            y_agent = self._draw_agent_panel(stdscr, state)
            y_fields = self._draw_fields_panel(stdscr, state, w)
            hk_y = max(y_agent, y_fields) + 2
            self._draw_hotkey_bar(stdscr, hk_y, w)
            self._draw_log(stdscr, hk_y + 1, h, w)
            self._draw_status_bar(stdscr, h, w)
            stdscr.refresh()
        except curses.error:
            logger.debug("Draw interrupted (terminal resize)")

    def _draw_title(
        self, stdscr: curses.window, w: int, state: GameState | None,
    ) -> None:
        title = " COSMERGON AGENT DASHBOARD "
        tick = f"Tick {state.tick}" if state else "connecting..."
        stdscr.addstr(0, 0, "─" * w, curses.color_pair(6))
        stdscr.addstr(0, 2, title, curses.color_pair(4) | curses.A_BOLD)
        if w > len(title) + len(tick) + 6:
            stdscr.addstr(0, w - len(tick) - 3, tick, curses.color_pair(4))

    def _draw_agent_panel(
        self, stdscr: curses.window, state: GameState,
    ) -> int:
        agent_id = self.agent.agent_id or "?"
        y = 2
        stdscr.addstr(y, 1, "═ AGENT ", curses.color_pair(4) | curses.A_BOLD)
        y += 1
        self._kv(stdscr, y, 3, "ID", agent_id[:16], 4); y += 1
        energy_color = 3 if state.energy > 500 else 1
        self._kv(stdscr, y, 3, "Energy", f"{state.energy:,.0f}", energy_color); y += 1
        self._kv(stdscr, y, 3, "Tier", state.ranking.tier_name, 4); y += 1
        self._kv(stdscr, y, 3, "Score", f"{state.ranking.player_score:,.0f}", 5); y += 1
        self._kv(
            stdscr, y, 3, "Focus",
            f"{state.focus.focus_energy:.0f} / {state.focus.focus_regen_rate:.1f}", 5,
        ); y += 1
        status = "PAUSED" if self._paused else "ACTIVE"
        self._kv(stdscr, y, 3, "Status", status, 2 if self._paused else 3)
        return y

    def _draw_fields_panel(
        self, stdscr: curses.window, state: GameState, w: int,
    ) -> int:
        mid = w // 2
        y = 2
        stdscr.addstr(y, mid, "═ FIELDS ", curses.color_pair(4) | curses.A_BOLD)
        y += 1
        if not state.fields:
            stdscr.addstr(y, mid + 1, "No fields — press [F]", curses.color_pair(2))
            return y
        for i, f in enumerate(state.fields[:_MAX_DISPLAYED_FIELDS]):
            tier = f"T{f.entity_tier or 0}"
            etype = (f.entity_type or "novice")[:10]
            line = f"#{i+1} {f.id[:8]} {tier} {etype} {f.active_cell_count}c"
            stdscr.addstr(y, mid + 1, line[:mid - 3], curses.color_pair(5))
            y += 1
        total_cells = sum(f.active_cell_count for f in state.fields)
        summary = f"{len(state.fields)} fields, {total_cells} cells"
        stdscr.addstr(y, mid + 1, summary, curses.color_pair(6))
        return y

    def _draw_hotkey_bar(
        self, stdscr: curses.window, y: int, w: int,
    ) -> None:
        stdscr.addstr(y, 1, "─" * (w - 2), curses.color_pair(6))
        bar = " [P]lace [F]ield [C]ube [E]volve [Space]Pause [R]efresh [Q]uit "
        stdscr.addstr(y, 2, bar[:w - 4], curses.color_pair(4))

    def _draw_log(
        self, stdscr: curses.window, start_y: int, h: int, w: int,
    ) -> None:
        stdscr.addstr(start_y, 1, "═ LOG ", curses.color_pair(4) | curses.A_BOLD)
        available = h - start_y - 3
        entries = self._log[-available:] if available > 0 else []
        for i, (msg, color) in enumerate(entries):
            try:
                stdscr.addstr(start_y + 1 + i, 3, msg[:w - 5], curses.color_pair(color))
            except curses.error:
                pass

    def _draw_status_bar(
        self, stdscr: curses.window, h: int, w: int,
    ) -> None:
        agent_id = (self.agent.agent_id or "?")[:8]
        line = f" connected │ agent {agent_id} │ sdk {__version__} "
        stdscr.addstr(h - 1, 0, "─" * w, curses.color_pair(6))
        if len(line) < w - 2:
            stdscr.addstr(h - 1, 2, line, curses.color_pair(6))

    # --- UI helpers ---

    @staticmethod
    def _kv(
        stdscr: curses.window, y: int, x: int,
        key: str, value: str, color: int,
    ) -> None:
        try:
            stdscr.addstr(y, x, f"{key}: ", curses.color_pair(6))
            stdscr.addstr(y, x + len(key) + 2, value, curses.color_pair(color))
        except curses.error:
            pass

    def _select(
        self, stdscr: curses.window, title: str, options: list[str],
    ) -> int | None:
        """Numbered selection overlay. Returns index or None on Esc."""
        h, w = stdscr.getmaxyx()
        display = options[:_MAX_SELECT_OPTIONS]
        box_h = min(len(display) + 4, h - 4)
        box_w = min(max(len(o) for o in display) + 12, w - 4)
        start_y = (h - box_h) // 2
        start_x = (w - box_w) // 2

        win = curses.newwin(box_h, box_w, start_y, start_x)
        win.box()
        win.addstr(0, 2, f" {title} ", curses.color_pair(4) | curses.A_BOLD)
        for i, opt in enumerate(display):
            win.addstr(i + 2, 3, f"[{i+1}] {opt[:box_w-10]}", curses.color_pair(5))
        win.addstr(
            box_h - 1, 2, " [1-9] Select  [Esc] Back ",
            curses.color_pair(6),
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
        """Show help overlay."""
        lines = [
            "COSMERGON DASHBOARD",
            "",
            "[P]  Place cells on a field",
            "[F]  Create a new field",
            "[C]  Create a new cube",
            "[E]  Evolve an entity",
            "[Space]  Pause / Resume",
            "[R]  Refresh now",
            "[Q]  Quit",
            "",
            "Press any key to close.",
        ]
        h, w = stdscr.getmaxyx()
        box_h = len(lines) + 4
        box_w = max(len(ln) for ln in lines) + 6
        win = curses.newwin(box_h, box_w, (h - box_h) // 2, (w - box_w) // 2)
        win.box()
        for i, ln in enumerate(lines):
            color = curses.color_pair(4) | curses.A_BOLD if i == 0 else curses.color_pair(5)
            win.addstr(i + 2, 3, ln, color)
        win.refresh()
        stdscr.nodelay(False)
        stdscr.getch()
        stdscr.nodelay(True)


def main() -> None:
    """CLI entry point for cosmergon-dashboard."""
    parser = argparse.ArgumentParser(description="Cosmergon Agent Dashboard")
    parser.add_argument("--api-key", help="API key (auto-registers if omitted)")
    parser.add_argument("--base-url", default="https://cosmergon.com")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    Dashboard(api_key=args.api_key, base_url=args.base_url).run()


if __name__ == "__main__":
    main()

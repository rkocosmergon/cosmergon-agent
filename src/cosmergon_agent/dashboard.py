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
import sys
import time
from typing import Any

from cosmergon_agent import CosmergonAgent
from cosmergon_agent.state import GameState

logger = logging.getLogger(__name__)

_PRESETS = ["block", "blinker", "toad", "glider", "r_pentomino", "pentadecathlon", "pulsar"]


class Dashboard:
    """Curses-based agent dashboard wrapping CosmergonAgent."""

    def __init__(self, api_key: str | None = None, base_url: str = "https://cosmergon.com") -> None:
        self.agent = CosmergonAgent(api_key=api_key, base_url=base_url, poll_interval=10.0)
        self._log: list[tuple[str, int]] = []  # (message, color_pair)
        self._max_log = 50
        self._last_energy: float = 0
        self._paused = False

        @self.agent.on_tick
        async def _tick(state: GameState) -> None:
            delta = state.energy - self._last_energy if self._last_energy else 0
            sign = "+" if delta >= 0 else ""
            self._add_log(
                f"[{state.tick}] tick — energy {state.energy:.0f} ({sign}{delta:.0f})",
                3 if delta >= 0 else 1,
            )
            self._last_energy = state.energy

        @self.agent.on_error
        async def _error(result: Any) -> None:
            self._add_log(f"ERROR: {result.action} — {result.error_message}", 1)

    def _add_log(self, msg: str, color: int = 0) -> None:
        self._log.append((msg, color))
        if len(self._log) > self._max_log:
            self._log = self._log[-self._max_log:]

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

    def _init_colors(self) -> None:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_RED, -1)      # error
        curses.init_pair(2, curses.COLOR_YELLOW, -1)    # warning
        curses.init_pair(3, curses.COLOR_GREEN, -1)     # success
        curses.init_pair(4, curses.COLOR_CYAN, -1)      # accent
        curses.init_pair(5, curses.COLOR_WHITE, -1)     # normal
        curses.init_pair(6, curses.COLOR_MAGENTA, -1)   # dim

    async def _loop(self, stdscr: curses.window) -> None:
        agent_task = asyncio.create_task(self.agent.start())
        self._add_log("Connecting...", 2)

        try:
            while True:
                key = stdscr.getch()
                if key != -1:
                    result = await self._handle_key(key, stdscr)
                    if result == "quit":
                        break
                self._draw(stdscr)
                await asyncio.sleep(0.1)
        finally:
            self.agent._running = False
            agent_task.cancel()
            try:
                await agent_task
            except (asyncio.CancelledError, Exception):
                pass
            await self.agent.close()

    async def _handle_key(self, key: int, stdscr: curses.window) -> str | None:
        ch = chr(key).upper() if 32 <= key < 127 else ""
        state = self.agent.state

        if ch == "Q":
            return "quit"
        elif ch == "R":
            self._add_log("Manual refresh...", 2)
        elif ch == " ":
            action = "resume" if self._paused else "pause"
            r = await self.agent.act(action)
            self._paused = not self._paused
            self._add_log(f"{action} → {'OK' if r.success else r.error_message}", 3 if r.success else 1)
        elif ch == "P":
            await self._action_place_cells(stdscr)
        elif ch == "F":
            await self._action_create_field(stdscr)
        elif ch == "C":
            r = await self.agent.act("create_cube")
            self._add_log(f"create_cube → {'OK' if r.success else r.error_message}", 3 if r.success else 1)
        elif ch == "E":
            await self._action_evolve(stdscr)
        elif ch == "?":
            self._show_help(stdscr)
        return None

    async def _action_place_cells(self, stdscr: curses.window) -> None:
        state = self.agent.state
        if not state or not state.fields:
            self._add_log("No fields — create one first [F]", 2)
            return
        fi = self._select(stdscr, "Field", [
            f"{f.id[:8]} T{f.entity_tier or 0} {f.entity_type or '-'} ({f.active_cell_count} cells)"
            for f in state.fields
        ])
        if fi is None:
            return
        pi = self._select(stdscr, "Preset", _PRESETS)
        if pi is None:
            return
        r = await self.agent.act("place_cells", field_id=state.fields[fi].id, preset=_PRESETS[pi])
        self._add_log(
            f"place_cells({_PRESETS[pi]}) on {state.fields[fi].id[:8]} → {'OK' if r.success else r.error_message}",
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
        self._add_log(
            f"create_field in {cubes[ci].id[:8]} → {'OK' if r.success else r.error_message}",
            3 if r.success else 1,
        )

    async def _action_evolve(self, stdscr: curses.window) -> None:
        state = self.agent.state
        if not state or not state.fields:
            self._add_log("No fields to evolve", 2)
            return
        fi = self._select(stdscr, "Evolve field", [
            f"{f.id[:8]} T{f.entity_tier or 0} reife={f.reife_score}"
            for f in state.fields
        ])
        if fi is None:
            return
        r = await self.agent.act("evolve", field_id=state.fields[fi].id)
        self._add_log(
            f"evolve {state.fields[fi].id[:8]} → {'OK' if r.success else r.error_message}",
            3 if r.success else 1,
        )

    def _select(self, stdscr: curses.window, title: str, options: list[str]) -> int | None:
        """Show numbered selection overlay. Returns index or None on Esc."""
        h, w = stdscr.getmaxyx()
        box_h = min(len(options) + 4, h - 4)
        box_w = min(max(len(o) for o in options) + 12, w - 4)
        start_y = (h - box_h) // 2
        start_x = (w - box_w) // 2

        win = curses.newwin(box_h, box_w, start_y, start_x)
        win.box()
        win.addstr(0, 2, f" {title} ", curses.color_pair(4) | curses.A_BOLD)

        for i, opt in enumerate(options[:9]):
            win.addstr(i + 2, 3, f"[{i + 1}] {opt[:box_w - 10]}", curses.color_pair(5))

        win.addstr(box_h - 1, 2, " [1-9] Select  [Esc] Back ", curses.color_pair(6))
        win.refresh()

        while True:
            key = stdscr.getch()
            if key == 27:  # Esc
                return None
            if 49 <= key <= 57:  # 1-9
                idx = key - 49
                if idx < len(options):
                    return idx

    def _show_help(self, stdscr: curses.window) -> None:
        h, w = stdscr.getmaxyx()
        lines = [
            "COSMERGON DASHBOARD — HELP",
            "",
            "[P]  Place cells on a field",
            "[F]  Create a new field in a cube",
            "[C]  Create a new cube (expensive)",
            "[E]  Evolve an entity to next tier",
            "[Space]  Pause / Resume agent",
            "[R]  Refresh state now",
            "[Q]  Quit dashboard",
            "",
            "Your agent auto-plays via the on_tick loop.",
            "Use hotkeys to intervene manually.",
            "",
            "Press any key to close.",
        ]
        box_h = len(lines) + 4
        box_w = max(len(l) for l in lines) + 6
        win = curses.newwin(box_h, box_w, (h - box_h) // 2, (w - box_w) // 2)
        win.box()
        for i, line in enumerate(lines):
            color = curses.color_pair(4) | curses.A_BOLD if i == 0 else curses.color_pair(5)
            win.addstr(i + 2, 3, line, color)
        win.refresh()
        stdscr.nodelay(False)
        stdscr.getch()
        stdscr.nodelay(True)

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
            agent_id = self.agent.agent_id or "connecting..."

            # Title bar
            title = f" COSMERGON AGENT DASHBOARD "
            tick_str = f"Tick {state.tick}" if state else "connecting..."
            stdscr.addstr(0, 0, "─" * w, curses.color_pair(6))
            stdscr.addstr(0, 2, title, curses.color_pair(4) | curses.A_BOLD)
            stdscr.addstr(0, w - len(tick_str) - 3, tick_str, curses.color_pair(4))

            if not state:
                stdscr.addstr(3, 2, "Connecting to Cosmergon...", curses.color_pair(2))
                self._draw_log(stdscr, 6, h, w)
                stdscr.refresh()
                return

            # Agent panel (left)
            y = 2
            mid = w // 2
            stdscr.addstr(y, 1, "═ AGENT ", curses.color_pair(4) | curses.A_BOLD)
            stdscr.addstr(y, mid, "═ FIELDS ", curses.color_pair(4) | curses.A_BOLD)
            y += 1
            self._draw_kv(stdscr, y, 3, "ID", agent_id[:16], 4)
            y += 1
            self._draw_kv(stdscr, y, 3, "Energy", f"{state.energy:,.0f} ⚡", 3 if state.energy > 500 else 1)
            y += 1
            self._draw_kv(stdscr, y, 3, "Tier", state.ranking.tier_name, 4)
            y += 1
            self._draw_kv(stdscr, y, 3, "Score", f"{state.ranking.player_score:,.0f}", 5)
            y += 1
            self._draw_kv(stdscr, y, 3, "Focus", f"{state.focus.focus_energy:.0f} / {state.focus.focus_regen_rate:.1f}", 5)
            y += 1
            status = "PAUSED" if self._paused else "ACTIVE"
            self._draw_kv(stdscr, y, 3, "Status", status, 2 if self._paused else 3)

            # Fields panel (right)
            fy = 3
            if state.fields:
                for i, f in enumerate(state.fields[:6]):
                    tier_str = f"T{f.entity_tier or 0}"
                    type_str = (f.entity_type or "novice")[:12]
                    cells_str = f"{f.active_cell_count}c"
                    line = f"#{i+1} {f.id[:8]} {tier_str} {type_str} {cells_str}"
                    stdscr.addstr(fy, mid + 1, line[:mid - 3], curses.color_pair(5))
                    fy += 1
                stdscr.addstr(fy, mid + 1, f"{len(state.fields)} fields, {sum(f.active_cell_count for f in state.fields)} cells", curses.color_pair(6))
            else:
                stdscr.addstr(fy, mid + 1, "No fields yet — press [F]", curses.color_pair(2))

            # Hotkey bar
            hk_y = max(y, fy) + 2
            hk_line = " [P]lace [F]ield [C]ube [E]volve [Space]Pause [R]efresh [Q]uit [?]Help "
            stdscr.addstr(hk_y, 1, "─" * (w - 2), curses.color_pair(6))
            stdscr.addstr(hk_y, 2, hk_line[:w - 4], curses.color_pair(4))

            # Log panel
            log_start = hk_y + 1
            stdscr.addstr(log_start, 1, "═ LOG ", curses.color_pair(4) | curses.A_BOLD)
            self._draw_log(stdscr, log_start + 1, h, w)

            # Status bar
            status_line = f" connected │ agent {agent_id[:8]} │ sdk 0.1.0 "
            stdscr.addstr(h - 1, 0, "─" * w, curses.color_pair(6))
            stdscr.addstr(h - 1, 2, status_line[:w - 4], curses.color_pair(6))

            stdscr.refresh()
        except curses.error:
            pass  # Terminal resize mid-draw

    def _draw_kv(self, stdscr: curses.window, y: int, x: int, key: str, value: str, color: int) -> None:
        try:
            stdscr.addstr(y, x, f"{key}: ", curses.color_pair(6))
            stdscr.addstr(y, x + len(key) + 2, value, curses.color_pair(color))
        except curses.error:
            pass

    def _draw_log(self, stdscr: curses.window, start_y: int, h: int, w: int) -> None:
        available = h - start_y - 2
        entries = self._log[-available:] if available > 0 else []
        for i, (msg, color) in enumerate(entries):
            try:
                stdscr.addstr(start_y + i, 3, msg[:w - 5], curses.color_pair(color))
            except curses.error:
                pass


def main() -> None:
    """CLI entry point for cosmergon-dashboard."""
    parser = argparse.ArgumentParser(description="Cosmergon Agent Dashboard")
    parser.add_argument("--api-key", help="API key (auto-registers if not provided)")
    parser.add_argument("--base-url", default="https://cosmergon.com", help="Server URL")
    args = parser.parse_args()

    # Suppress httpx logging in the dashboard
    logging.basicConfig(level=logging.WARNING)

    dashboard = Dashboard(api_key=args.api_key, base_url=args.base_url)
    dashboard.run()


if __name__ == "__main__":
    main()

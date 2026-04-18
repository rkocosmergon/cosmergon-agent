"""Cosmergon Pet — Stufe 1: Pet-Modus.

Dein KI-Agent lebt auf einem 128x64-OLED: Gesicht + 8 Info-Screens,
Steuerung über einen KY-040 Dreh-Drück-Knopf.

Bauanleitung: guide/cosmergon-pet-bauanleitung.pdf (cosmergon-pet-Repo)
Konzept:      guide/cosmergon-pet-konzept.pdf

Hardware (40-Pin-RPi: Zero 2 W, 3, 4, 5):
    OLED 1.3" SH1106 I2C   -> VCC=Pin1, GND=Pin6, SDA=Pin3, SCL=Pin5
    KY-040 Rotary Encoder  -> CLK=Pin11, DT=Pin13, SW=Pin15, VCC=Pin17, GND=Pin9

Software:
    sudo raspi-config nonint do_i2c 0  # I2C aktivieren + Reboot
    python3 -m venv ~/cosmergon-env && source ~/cosmergon-env/bin/activate
    pip install cosmergon-agent luma.oled RPi.GPIO pillow
    python3 cosmergon_face.py

Simulation (ohne RPi-Hardware):
    python3 cosmergon_face.py --simulate

Die 8 Info-Screens (Drehen scrollt, Klick auf Screen 1 öffnet Aktionsmenü):
    1 Gesicht + Mood         /health
    2 Energie + Rank         /state
    3 Territorium            /state
    4 Events                 /events
    5 Benchmark              /state
    6 Journal                /decisions
    7 Letzte Aktion          /decisions
    8 Regeln                 /state

Bedienung:
    Drehen            Durch Screens / Menü-Einträge scrollen
    Kurz drücken      Auf Screen 1: Aktionsmenü öffnen
                      Im Menü: ausgewählte Aktion ausführen
                      Sonst: Screen 1 aufrufen
    Lang drücken >1s  Agent pausieren/fortsetzen (oder zurück aus Menü)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from cosmergon_agent import CosmergonAgent
from cosmergon_agent.state import GameState

logger = logging.getLogger("cosmergon-pet")

# --- GPIO-Pins (BCM-Nummerierung) -------------------------------------------
ENC_CLK = 17  # Pin 11
ENC_DT = 27  # Pin 13
ENC_SW = 22  # Pin 15

# --- Timing -----------------------------------------------------------------
DISPLAY_REFRESH_HZ = 10     # Display neu zeichnen (10 FPS reicht für OLED)
STATE_POLL_SECONDS = 30     # /state-Poll; on_tick liefert zusätzlich alle 60 s
DECISION_POLL_SECONDS = 90  # /decisions seltener — spart API-Calls
EVENTS_POLL_SECONDS = 45    # /events
LONGPRESS_SECONDS = 1.0     # Lang-Drück-Schwelle
DORMANT_AFTER_HOURS = 24    # ( z__z ) wenn keine Entscheidung seit N Stunden
ACTION_FLASH_SECONDS = 2.5  # ( >__< ) für N Sekunden nach Aktion
ALERT_AFTER_ROTATION_SECONDS = 0.8  # ( o__o ) wenn Encoder dreht

# --- Gesichter --------------------------------------------------------------
FACES = {
    "thriving":   "( ^__^ )",
    "content":    "( -__- )",
    "struggling": "( ;__; )",
    "dormant":    "( z__z )",
    "alert":      "( o__o )",
    "action":     "( >__< )",
}

COMPASS_PRESETS = ("attack", "defend", "grow", "trade", "explore")


@dataclass
class PetState:
    """Alles, was UI + Input-Thread brauchen. Zentrale Shared State."""

    current_screen: int = 0  # 0..7
    menu_open: bool = False
    menu_index: int = 0
    compass_submenu: bool = False
    compass_index: int = 0
    paused: bool = False

    last_rotation_at: float = 0.0
    last_action_at: float = 0.0
    last_action_label: str = ""

    # Vom Poller gesetzt
    game_state: GameState | None = None
    events: list[dict] = field(default_factory=list)
    last_decision: dict | None = None
    connection_ok: bool = False
    last_error: str = ""


# ----------------------------------------------------------------------------
# Mood-Logik (reine Funktion von Zustand → Gesicht)
# ----------------------------------------------------------------------------

def mood_from_state(ps: PetState, now: float) -> str:
    """Bestimme das Gesicht aus dem aktuellen Zustand.

    Reihenfolge priorisiert visuelle Rückmeldung: laufende Aktion > Encoder-
    Drehung > Schlaf > Notlage > Normalzustand.
    """
    if now - ps.last_action_at < ACTION_FLASH_SECONDS:
        return "action"
    if now - ps.last_rotation_at < ALERT_AFTER_ROTATION_SECONDS:
        return "alert"

    state = ps.game_state
    if state is None:
        return "content"  # Noch kein State — neutrale Anzeige

    # Dormant: keine Entscheidung seit DORMANT_AFTER_HOURS
    decision = ps.last_decision
    if decision and decision.get("created_at"):
        age_hours = _age_hours(decision["created_at"], now)
        if age_hours is not None and age_hours > DORMANT_AFTER_HOURS:
            return "dormant"

    situation = state.world_briefing.situation if state.world_briefing else None
    trend = situation.energy_trend if situation else "stable"

    if situation and situation.active_catastrophe:
        return "struggling"
    if situation and situation.fields_owned == 0:
        return "struggling"
    if trend == "falling":
        return "struggling"
    if trend == "rising":
        return "thriving"
    return "content"


def _age_hours(iso_timestamp: str, now: float) -> float | None:
    """Alter in Stunden; None bei unparsbarem Timestamp."""
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        return (now - dt.replace(tzinfo=timezone.utc).timestamp()) / 3600.0
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Kontextuelles Aktionsmenü
# ----------------------------------------------------------------------------

def build_menu(state: GameState | None, paused: bool) -> list[tuple[str, str]]:
    """Menü-Einträge basierend auf Agentensituation.

    Liefert Liste (label, action_key). action_key wird von execute_menu_action
    interpretiert (API-Aktion oder Pseudo-Aktion wie "compass"/"pause").
    """
    items: list[tuple[str, str]] = []

    if state is None:
        items.append(("Wait for state...", "noop"))
        items.append(("Pause" if not paused else "Resume", "pause"))
        return items

    situation = state.world_briefing.situation if state.world_briefing else None
    energy = state.energy
    tier = state.ranking.player_tier

    if situation:
        if situation.fields_owned == 0:
            items.append(("Create Field (100 E)", "create_field"))
        if situation.fields_without_cells > 0 and situation.affordable_presets:
            # Günstigstes Preset ist bei affordable_presets meistens zuerst
            preset = situation.affordable_presets[0]
            items.append((f"Place Cells ({preset})", f"place_cells:{preset}"))
        if energy >= _tier_up_cost(tier):
            items.append((f"Evolve (~{_tier_up_cost(tier)} E)", "evolve"))
        if situation.active_catastrophe:
            items.append(("Buy Shield", "buy_shield"))

    items.append(("Set Compass \u25b6", "compass"))
    items.append(("Pause" if not paused else "Resume", "pause"))
    items.append(("Close Menu", "close"))
    return items


def _tier_up_cost(current_tier: int) -> int:
    """Grobe Daumenregel für Evolve-Kosten (500 E bei T1, verdoppelt pro Tier)."""
    return 500 * (2 ** max(0, current_tier - 1))


# ----------------------------------------------------------------------------
# Screen-Renderer (liefern Zeilen-Liste; Display-Layer sorgt für Layout)
# ----------------------------------------------------------------------------

def render_screen(ps: PetState, now: float) -> list[str]:
    """Zeichne den aktiven Screen als Text-Zeilen (7 Zeilen à ~21 Zeichen).

    Wenn Menü offen ist, wird statt Screen 1 das Menü dargestellt.
    """
    if ps.menu_open and ps.current_screen == 0:
        return _render_menu(ps)
    screen = ps.current_screen
    renderers = [
        _render_face,
        _render_energy,
        _render_territory,
        _render_events,
        _render_benchmark,
        _render_journal,
        _render_last_action,
        _render_rules,
    ]
    title = [
        "Face",
        "Energy",
        "Territory",
        "Events",
        "Benchmark",
        "Journal",
        "Last Action",
        "Rules",
    ][screen]
    header = f"[{screen + 1}/8] {title}"
    body = renderers[screen](ps, now)
    if ps.paused:
        header = f"PAUSED  {header}"
    if not ps.connection_ok and ps.last_error:
        body = body + [f"! {ps.last_error[:20]}"]
    return [header, "-" * 21, *body]


def _render_face(ps: PetState, now: float) -> list[str]:
    mood = mood_from_state(ps, now)
    face = FACES[mood]
    state = ps.game_state
    energy_str = f"{int(state.energy)} E" if state else "--"
    name = state.agent_name if state and state.agent_name else "agent"
    headline = _headline_for(state) if state else ""
    return [
        "",
        face.center(21),
        mood.center(21),
        "",
        f"{name[:14]:14s} {energy_str:>6}",
        headline[:21],
    ]


def _headline_for(state: GameState) -> str:
    """Eine knackige Zeile aus dem WorldBriefing für den Face-Screen."""
    wb = state.world_briefing
    if not wb:
        return ""
    if wb.last_event:
        return f"Event: {wb.last_event[:14]}"
    s = wb.situation
    if s.active_catastrophe:
        return f"! {s.active_catastrophe[:17]}"
    if s.fields_owned == 0:
        return "No fields yet"
    return f"{s.fields_owned} fields, {wb.total_agents} agents"


def _render_energy(ps: PetState, now: float) -> list[str]:
    state = ps.game_state
    if not state:
        return ["", "No state yet..."]
    rank = state.world_briefing.your_rank if state.world_briefing else 0
    total = state.world_briefing.total_agents if state.world_briefing else 0
    trend = (
        state.world_briefing.situation.energy_trend if state.world_briefing else "stable"
    )
    trend_arrow = {"rising": "up", "falling": "down", "stable": "stable"}.get(trend, trend)
    return [
        f"Energy: {int(state.energy)} E",
        f"Trend:  {trend_arrow}",
        "",
        f"Tier:   {state.ranking.player_tier} {state.ranking.tier_name[:12]}",
        f"Rank:   {rank}/{total}" if total else "Rank:   -",
    ]


def _render_territory(ps: PetState, now: float) -> list[str]:
    state = ps.game_state
    if not state:
        return ["", "No state yet..."]
    total_cells = sum(f.active_cell_count for f in state.fields)
    cubes = len(state.cubes)
    sit = state.world_briefing.situation if state.world_briefing else None
    spores = sit.dormant_spores_on_fields if sit else 0
    return [
        f"Fields:  {len(state.fields)}",
        f"Cubes:   {cubes}",
        f"Cells:   {total_cells}",
        f"Spores:  {spores}",
        "",
        f"Compass: {state.compass_preset or 'unset'}",
    ]


def _render_events(ps: PetState, now: float) -> list[str]:
    if not ps.events:
        return ["", "No recent events."]
    out = []
    for ev in ps.events[:5]:
        typ = ev.get("event_type", "?")[:12]
        tick = ev.get("tick", "?")
        out.append(f"t{tick} {typ}")
    return out


def _render_benchmark(ps: PetState, now: float) -> list[str]:
    state = ps.game_state
    if not state or not state.world_briefing:
        return ["", "No state yet..."]
    sit = state.world_briefing.situation
    if sit.benchmark_ready:
        return [
            "",
            "Benchmark ready!",
            "",
            "See report at",
            "cosmergon.com",
        ]
    return [
        "",
        f"Days to benchmark:",
        "",
        f"  {sit.benchmark_days_remaining}",
        "",
        "(7-day report)",
    ]


def _render_journal(ps: PetState, now: float) -> list[str]:
    decision = ps.last_decision
    if not decision:
        return ["", "No decisions yet."]
    journal = decision.get("journal") or decision.get("reasoning") or ""
    return _wrap(journal, width=21, max_lines=6)


def _render_last_action(ps: PetState, now: float) -> list[str]:
    d = ps.last_decision
    if not d:
        return ["", "No decisions yet."]
    action = d.get("action", "?")
    outcome = d.get("outcome", "?")
    reasoning = d.get("reasoning", "")
    lines = [
        f"Action:  {action[:12]}",
        f"Result:  {outcome[:12]}",
        "",
    ]
    return lines + _wrap(reasoning, width=21, max_lines=3)


def _render_rules(ps: PetState, now: float) -> list[str]:
    state = ps.game_state
    if not state or not state.learned_rules:
        return ["", "No rules yet.", "", "(updated every", " 100 ticks)"]
    out = []
    for rule in state.learned_rules[:5]:
        out.extend(_wrap(rule, width=21, max_lines=1))
    return out


def _render_menu(ps: PetState) -> list[str]:
    """Aktionsmenü über Face-Screen gelegt."""
    if ps.compass_submenu:
        header = "COMPASS"
        items = [(p, p) for p in COMPASS_PRESETS] + [("back", "back")]
        idx = ps.compass_index
    else:
        header = "ACTIONS"
        items = build_menu(ps.game_state, ps.paused)
        idx = ps.menu_index

    lines = [header, "-" * 21]
    # Fenster um aktuellen Index (max 5 Einträge sichtbar)
    start = max(0, min(idx - 2, len(items) - 5))
    end = min(len(items), start + 5)
    for i in range(start, end):
        label = items[i][0]
        marker = ">" if i == idx else " "
        lines.append(f"{marker} {label[:19]}")
    return lines


def _wrap(text: str, width: int = 21, max_lines: int = 6) -> list[str]:
    """Einfacher Greedy-Wrap auf Wortgrenzen."""
    if not text:
        return [""]
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        if len(cur) + 1 + len(w) <= width:
            cur = f"{cur} {w}".strip()
        else:
            if cur:
                lines.append(cur)
            cur = w[:width]
        if len(lines) >= max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    return lines[:max_lines]


# ----------------------------------------------------------------------------
# Display-Backends (OLED via luma.oled + Simulation via stdout)
# ----------------------------------------------------------------------------

class StdoutDisplay:
    """Simulations-Display — Schreibt jeden Frame in die Konsole (für Laptop-Entwicklung)."""

    def __init__(self) -> None:
        self._last_frame: str = ""

    def draw(self, lines: list[str]) -> None:
        frame = "\n".join(lines)
        if frame != self._last_frame:
            os.system("clear" if os.name != "nt" else "cls")
            print("+" + "-" * 23 + "+")
            for line in lines:
                print(f"| {line[:21]:21s} |")
            for _ in range(max(0, 8 - len(lines))):
                print("| " + " " * 21 + " |")
            print("+" + "-" * 23 + "+")
            self._last_frame = frame

    def close(self) -> None:
        pass


class OledDisplay:
    """Hardware-Display — SH1106 128x64 via I2C (luma.oled)."""

    def __init__(self) -> None:
        from luma.core.interface.serial import i2c
        from luma.oled.device import sh1106
        from PIL import ImageFont

        self._serial = i2c(port=1, address=0x3C)
        self._device = sh1106(self._serial, rotate=0)
        # Default-Font (8px) passt 21 Zeichen × 8 Zeilen auf 128×64.
        self._font = ImageFont.load_default()

    def draw(self, lines: list[str]) -> None:
        from luma.core.render import canvas

        with canvas(self._device) as draw:
            for i, line in enumerate(lines[:8]):
                draw.text((0, i * 8), line[:21], font=self._font, fill="white")

    def close(self) -> None:
        self._device.cleanup()


def make_display(simulate: bool) -> Any:
    if simulate:
        return StdoutDisplay()
    try:
        return OledDisplay()
    except Exception as err:
        logger.warning("OLED nicht verfügbar (%s) — wechsle auf Simulation.", err)
        return StdoutDisplay()


# ----------------------------------------------------------------------------
# Input-Backends (KY-040 via RPi.GPIO + Keyboard-Simulation)
# ----------------------------------------------------------------------------

class InputEvent:
    ROT_LEFT = "left"
    ROT_RIGHT = "right"
    CLICK = "click"
    LONGPRESS = "longpress"


class GpioEncoder:
    """KY-040 an CLK/DT/SW — Events landen in asyncio.Queue (thread-safe)."""

    def __init__(self, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop) -> None:
        import RPi.GPIO as GPIO  # type: ignore[import-not-found]

        self._GPIO = GPIO
        self._queue = queue
        self._loop = loop
        self._press_start: float | None = None

        GPIO.setmode(GPIO.BCM)
        GPIO.setup(ENC_CLK, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(ENC_DT, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(ENC_SW, GPIO.IN, pull_up_down=GPIO.PUD_UP)

        self._last_clk = GPIO.input(ENC_CLK)
        GPIO.add_event_detect(ENC_CLK, GPIO.BOTH, callback=self._on_rotate, bouncetime=2)
        GPIO.add_event_detect(ENC_SW, GPIO.BOTH, callback=self._on_switch, bouncetime=20)

    def _push(self, event: str) -> None:
        """Thread-sicher ein Event in die asyncio-Queue schieben."""
        self._loop.call_soon_threadsafe(self._queue.put_nowait, event)

    def _on_rotate(self, _channel: int) -> None:
        clk = self._GPIO.input(ENC_CLK)
        dt = self._GPIO.input(ENC_DT)
        if clk != self._last_clk:
            if dt != clk:
                self._push(InputEvent.ROT_RIGHT)
            else:
                self._push(InputEvent.ROT_LEFT)
        self._last_clk = clk

    def _on_switch(self, _channel: int) -> None:
        pressed = self._GPIO.input(ENC_SW) == 0
        now = time.monotonic()
        if pressed:
            self._press_start = now
        else:
            if self._press_start is None:
                return
            duration = now - self._press_start
            self._press_start = None
            if duration >= LONGPRESS_SECONDS:
                self._push(InputEvent.LONGPRESS)
            elif duration >= 0.03:  # Entprell-Minimum
                self._push(InputEvent.CLICK)

    def close(self) -> None:
        try:
            self._GPIO.cleanup()
        except Exception:
            pass


class KeyboardEncoder:
    """Simulations-Input für Laptop: Pfeiltasten + Enter.

    Nutzt stdin im raw-Modus — nur auf Unix-Terminals.
    """

    def __init__(self, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop) -> None:
        import termios
        import tty

        self._queue = queue
        self._loop = loop
        self._fd = sys.stdin.fileno()
        self._stop = False
        self._old_settings = None
        try:
            self._old_settings = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
            self._task = loop.create_task(self._reader())
        except (termios.error, OSError):
            # Kein TTY (z. B. Unit-Test, Pipe) — Reader überspringen,
            # Display läuft trotzdem, nur ohne Input.
            logger.info("KeyboardEncoder: stdin kein TTY, Input deaktiviert.")
            self._task = None

    async def _reader(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop:
            ch = await loop.run_in_executor(None, sys.stdin.read, 1)
            if ch == "\x1b":
                seq = await loop.run_in_executor(None, sys.stdin.read, 2)
                if seq == "[C":
                    await self._queue.put(InputEvent.ROT_RIGHT)
                elif seq == "[D":
                    await self._queue.put(InputEvent.ROT_LEFT)
            elif ch in ("\r", "\n"):
                await self._queue.put(InputEvent.CLICK)
            elif ch == " ":
                await self._queue.put(InputEvent.LONGPRESS)
            elif ch == "q":
                os.kill(os.getpid(), signal.SIGINT)

    def close(self) -> None:
        import termios
        self._stop = True
        if self._old_settings is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_settings)
            except Exception:
                pass


def make_encoder(
    simulate: bool, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop
) -> Any:
    if simulate:
        return KeyboardEncoder(queue, loop)
    try:
        return GpioEncoder(queue, loop)
    except Exception as err:
        logger.warning("GPIO nicht verfügbar (%s) — wechsle auf Keyboard.", err)
        return KeyboardEncoder(queue, loop)


# ----------------------------------------------------------------------------
# Eingabe-Behandlung (Navigation + Menü-Execution)
# ----------------------------------------------------------------------------

async def handle_event(
    event: str, ps: PetState, agent: CosmergonAgent, now: float
) -> None:
    if event in (InputEvent.ROT_LEFT, InputEvent.ROT_RIGHT):
        ps.last_rotation_at = now
        _handle_rotate(event, ps)
    elif event == InputEvent.CLICK:
        await _handle_click(ps, agent, now)
    elif event == InputEvent.LONGPRESS:
        await _handle_longpress(ps, agent)


def _handle_rotate(event: str, ps: PetState) -> None:
    direction = 1 if event == InputEvent.ROT_RIGHT else -1
    if ps.menu_open:
        if ps.compass_submenu:
            n = len(COMPASS_PRESETS) + 1  # +1 für "back"
            ps.compass_index = (ps.compass_index + direction) % n
        else:
            items = build_menu(ps.game_state, ps.paused)
            ps.menu_index = (ps.menu_index + direction) % len(items)
    else:
        ps.current_screen = (ps.current_screen + direction) % 8


async def _handle_click(ps: PetState, agent: CosmergonAgent, now: float) -> None:
    if not ps.menu_open:
        if ps.current_screen == 0:
            ps.menu_open = True
            ps.menu_index = 0
        else:
            # Kurzer Klick auf anderen Screens springt zurück zu Screen 1
            ps.current_screen = 0
        return

    if ps.compass_submenu:
        items = list(COMPASS_PRESETS) + ["back"]
        choice = items[ps.compass_index]
        if choice == "back":
            ps.compass_submenu = False
            return
        await _execute_action(f"compass:{choice}", ps, agent, now)
        ps.compass_submenu = False
        ps.menu_open = False
        return

    items = build_menu(ps.game_state, ps.paused)
    label, action_key = items[ps.menu_index]

    if action_key == "compass":
        ps.compass_submenu = True
        ps.compass_index = 0
        return
    if action_key == "close":
        ps.menu_open = False
        return
    if action_key == "noop":
        return

    await _execute_action(action_key, ps, agent, now)
    ps.last_action_label = label
    ps.menu_open = False


async def _handle_longpress(ps: PetState, agent: CosmergonAgent) -> None:
    if ps.menu_open:
        # Aus Menü zurück
        if ps.compass_submenu:
            ps.compass_submenu = False
        else:
            ps.menu_open = False
        return
    ps.paused = not ps.paused


async def _execute_action(
    action_key: str, ps: PetState, agent: CosmergonAgent, now: float
) -> None:
    """Führe eine Menü-Aktion aus. Fehler werden ignoriert (ActionResult im Journal)."""
    state = ps.game_state
    try:
        if action_key == "create_field" and state and state.universe_cubes:
            cube_id = state.universe_cubes[0].id
            await agent.act("create_field", cube_id=cube_id)
        elif action_key.startswith("place_cells:") and state and state.fields:
            preset = action_key.split(":", 1)[1]
            empty_field = next(
                (f for f in state.fields if f.active_cell_count == 0), None
            )
            if empty_field:
                await agent.act("place_cells", field_id=empty_field.id, preset=preset)
        elif action_key == "evolve" and state and state.fields:
            await agent.act("evolve", field_id=state.fields[0].id)
        elif action_key == "buy_shield" and state and state.fields:
            await agent.act("buy_shield", field_id=state.fields[0].id)
        elif action_key == "pause":
            ps.paused = not ps.paused
        elif action_key.startswith("compass:"):
            preset = action_key.split(":", 1)[1]
            await agent.set_compass(preset)
        ps.last_action_at = now
    except Exception as err:
        ps.last_error = f"action: {err}"[:30]
        logger.exception("Action %s failed", action_key)


# ----------------------------------------------------------------------------
# Haupt-Loop (state + display + input)
# ----------------------------------------------------------------------------

async def run_pet(agent: CosmergonAgent, simulate: bool) -> None:
    ps = PetState()
    display = make_display(simulate)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    encoder = make_encoder(simulate, queue, loop)

    stop = asyncio.Event()

    def _stop_handler(*_: Any) -> None:
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop_handler)

    # Erste Registrierung / State-Fetch
    await _prime_state(agent, ps)

    # Hintergrund-Tasks: periodische Polls
    poll_state_task = asyncio.create_task(_poll_state(agent, ps, stop))
    poll_events_task = asyncio.create_task(_poll_events(agent, ps, stop))
    poll_decisions_task = asyncio.create_task(_poll_decisions(agent, ps, stop))
    draw_task = asyncio.create_task(_draw_loop(display, ps, stop))

    try:
        while not stop.is_set():
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            await handle_event(event, ps, agent, time.monotonic())
    finally:
        stop.set()
        for task in (poll_state_task, poll_events_task, poll_decisions_task, draw_task):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        encoder.close()
        display.close()


async def _prime_state(agent: CosmergonAgent, ps: PetState) -> None:
    """Initialer State + Registrierung. SDK macht Auto-Register on first call."""
    try:
        await agent._resolve_agent_id()  # type: ignore[attr-defined]
        ps.connection_ok = True
    except Exception as err:
        ps.last_error = str(err)[:30]


async def _poll_state(agent: CosmergonAgent, ps: PetState, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            resp = await agent._request(  # type: ignore[attr-defined]
                "GET", f"/api/v1/agents/{agent.agent_id}/state"
            )
            if resp.status_code == 200:
                ps.game_state = GameState.from_api(resp.json())
                ps.connection_ok = True
                ps.last_error = ""
        except Exception as err:
            ps.connection_ok = False
            ps.last_error = f"state: {err}"[:30]
        await asyncio.sleep(STATE_POLL_SECONDS)


async def _poll_events(agent: CosmergonAgent, ps: PetState, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            ps.events = await agent.get_events(limit=20)
        except Exception as err:
            ps.last_error = f"events: {err}"[:30]
        await asyncio.sleep(EVENTS_POLL_SECONDS)


async def _poll_decisions(
    agent: CosmergonAgent, ps: PetState, stop: asyncio.Event
) -> None:
    while not stop.is_set():
        try:
            ps.last_decision = await agent.get_last_decision()
        except Exception as err:
            ps.last_error = f"decisions: {err}"[:30]
        await asyncio.sleep(DECISION_POLL_SECONDS)


async def _draw_loop(display: Any, ps: PetState, stop: asyncio.Event) -> None:
    interval = 1.0 / DISPLAY_REFRESH_HZ
    while not stop.is_set():
        try:
            lines = render_screen(ps, time.monotonic())
            display.draw(lines)
        except Exception:
            logger.exception("draw failed")
        await asyncio.sleep(interval)


# ----------------------------------------------------------------------------
# Entry Point
# ----------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Cosmergon Pet — Stufe 1")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Ohne RPi-Hardware: Anzeige in Konsole, Steuerung per Pfeiltasten + Enter/Space.",
    )
    parser.add_argument(
        "--log-level", default="WARNING", help="DEBUG/INFO/WARNING/ERROR"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.WARNING),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    api_key = os.environ.get("COSMERGON_API_KEY")
    base_url = os.environ.get("COSMERGON_BASE_URL", "https://cosmergon.com")
    agent = CosmergonAgent(api_key=api_key, base_url=base_url)

    try:
        asyncio.run(run_pet(agent, simulate=args.simulate))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

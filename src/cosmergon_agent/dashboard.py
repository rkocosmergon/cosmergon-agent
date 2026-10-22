"""Terminal dashboard for Cosmergon agents — btop-inspired Textual UI.

Usage:
    cosmergon-dashboard                    # auto-register and connect
    cosmergon-dashboard --api-key KEY      # use existing key
    cosmergon-dashboard --theme matrix     # use a different theme
    python -m cosmergon_agent.dashboard    # same as above

Hotkeys:
    C  Set Compass direction (highlighted until first use)
    P  Place cells       F  Create field     E  Evolve entity
    X  Pause/Resume (asks twice)  U  Upgrade (next tier, opens browser)
    K  Show API key + config path
    S  Toggle Public Showcase (opt-in/opt-out, DSGVO Art. 6 lit. a)
    R  Refresh now       Q  Quit             ?  Help

Themes: cosmergon (default), matrix, mono, high-contrast
Config: COSMERGON_THEME env var  |  ~/.cosmergon/dashboard.toml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shlex
import time
import webbrowser
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple

if TYPE_CHECKING:
    from cosmergon_agent.action import ActionResult

try:
    from textual import work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.events import Key
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

from cosmergon_agent import AuthenticationError, CosmergonAgent, CosmergonError, __version__, _http
from cosmergon_agent.action import (
    action_done_word,
    action_word,
    mission_goal_text,
    mission_outcome_text,
    mission_step_text,
    mission_word,
    printable,
)
from cosmergon_agent.config import (
    is_onboarding_dismissed,
    load_all_agents,
    load_token,
    save_agent,
    set_active_agent,
    set_onboarding_dismissed,
)
from cosmergon_agent.exceptions import ConnectionError as CsgConnectionError
from cosmergon_agent.exceptions import RateLimitError
from cosmergon_agent.state import AgentSituation, Field, GameState, Ranking

logger = logging.getLogger(__name__)

_MAX_LOG = 80
_MAX_FIELDS = 5
# The smallest window the main view fits whole (cos20 #508, test_anzeige_passt_508): below it the
# dashboard says so instead of drawing a cut picture, like btop. Height = hint 1 + top row 14 +
# log 6 + context 1 + keys 4 + status 1.
_MIN_COLS = 54
_MIN_ROWS = 27
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
    # Surfaces and frames (#475) — "#rrggbb" only, they go into the CSS.
    # One surface for screen and panels: a frame row is drawn on its panel's background,
    # a second surface level leaves a strip of it outside every frame line.
    bg: str = "#161616"
    bar: str = "#252525"  # hint bar
    frame_agent: str = "#5d6b8a"
    frame_economy: str = "#7a6390"
    frame_log: str = "#4c7773"
    focus: str = "#c8c8c8"  # frame of the focused panel, dialog frames


# A line that separates areas needs 3:1 against its surface (WCAG 2.1, 1.4.11).
_FRAME_CONTRAST = 3.0
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _contrast(a: str, b: str) -> float:
    """WCAG contrast ratio of two "#rrggbb" colors (1.0 … 21.0)."""

    def luminance(color: str) -> float:
        channels = [c / 255 for c in _rgb(color)]
        lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _mix(color: str, other: str, share: float) -> str:
    """``share`` of ``color``, the rest ``other``."""
    return "#" + "".join(
        f"{round(c * share + o * (1 - share)):02x}"
        for c, o in zip(_rgb(color), _rgb(other), strict=True)
    )


def _frame_color(color: str, surface: str) -> str:
    """The most muted shade of ``color`` that still separates from ``surface``.

    Fades the color into the surface as far as ``_FRAME_CONTRAST`` allows. A color
    that is too dark even at full strength is lifted towards white instead.
    """
    for step in range(6, 21):  # 30 % … 100 % of the color
        shade = _mix(color, surface, step / 20)
        if _contrast(shade, surface) >= _FRAME_CONTRAST:
            return shade
    for step in range(1, 21):
        shade = _mix("#ffffff", color, step / 20)
        if _contrast(shade, surface) >= _FRAME_CONTRAST:
            return shade
    return "#ffffff"


THEMES: dict[str, Theme] = {
    "cosmergon": Theme("cosmergon", "#aaaaaa", "yellow", "#6EE21C", "red", "#999999", "white"),
    "matrix": Theme(
        "matrix",
        "green",
        "bright_green",
        "green",
        "red",
        "green",
        "green",
        bg="#050805",
        bar="#0f170f",
        frame_agent="#2f8f2f",
        frame_economy="#2f8f2f",
        frame_log="#2f8f2f",
        focus="#55ff55",
    ),
    "mono": Theme(
        "mono",
        "white",
        "white",
        "white",
        "white",
        "white",
        "white",
        frame_agent="#6e6e6e",
        frame_economy="#6e6e6e",
        frame_log="#6e6e6e",
        focus="#ffffff",
    ),
    "high-contrast": Theme(
        "high-contrast",
        "yellow",
        "cyan",
        "green",
        "red",
        "white",
        "white",
        bg="#000000",
        bar="#1a1a1a",
        frame_agent="#ffffff",
        frame_economy="#ffffff",
        frame_log="#ffffff",
        focus="#00ffff",
    ),
}

# Omarchy writes the colors of the active desktop theme here (24 named values).
_OMARCHY_COLORS = Path(".local/state/omarchy/current/theme/colors.toml")


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        import tomllib  # type: ignore[import-not-found]
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef,import-not-found]
    with path.open("rb") as fh:
        data: dict[str, Any] = tomllib.load(fh)
    return data


def _omarchy_theme(path: Path | None = None) -> Theme | None:
    """Build the theme from Omarchy's color table — None unless it is there and clean.

    Every value used must be a plain "#rrggbb": the strings end up in CSS and markup.
    """
    path = path or Path.home() / _OMARCHY_COLORS
    if not path.is_file():
        return None
    try:
        raw = _read_toml(path)
    except Exception:
        logger.debug("_omarchy_theme: unreadable %s", path, exc_info=True)
        return None

    def color(key: str, fallback: str = "") -> str:
        value = raw.get(key)
        if isinstance(value, str) and _HEX_COLOR.match(value):
            return value.lower()
        return fallback

    bg, fg, accent = color("background"), color("foreground"), color("accent")
    green, red, yellow = color("green"), color("red"), color("yellow")
    if not all((bg, fg, accent, green, red, yellow)):
        return None
    return Theme(
        name="omarchy",
        cmd=fg,
        guide=yellow,
        pos=green,
        warn=red,
        struct=fg,
        data=color("bright_foreground", fg),
        bg=bg,
        bar=color("lighter_background", bg),
        frame_agent=_frame_color(color("blue", accent), bg),
        frame_economy=_frame_color(color("magenta", accent), bg),
        frame_log=_frame_color(color("cyan", accent), bg),
        focus=accent,
    )


def _theme_by_name(name: str | None) -> Theme | None:
    if name == "omarchy":
        return _omarchy_theme()
    return THEMES.get(name) if name else None


def _load_theme(cli_theme: str | None = None) -> Theme:
    """Resolve the theme.

    CLI arg > COSMERGON_THEME env > ~/.cosmergon/dashboard.toml > the Omarchy
    desktop theme (on an Omarchy machine) > default.
    """
    chosen = _theme_by_name(cli_theme) or _theme_by_name(os.environ.get("COSMERGON_THEME"))
    if chosen:
        return chosen
    cfg = Path.home() / ".cosmergon" / "dashboard.toml"
    if cfg.exists():
        try:
            chosen = _theme_by_name(_read_toml(cfg).get("dashboard", {}).get("theme"))
            if chosen:
                return chosen
        except Exception:
            logger.debug("_load_theme: failed, falling back", exc_info=True)
    return _omarchy_theme() or THEMES["cosmergon"]


def _c(color: str, text: str) -> str:
    return f"[{color}]{text}[/{color}]"


def _hk(key: str) -> str:
    """Return Rich-escaped hotkey notation: _hk('C') → '\\[C]' (renders as literal [C])."""
    return "\\[" + key + "]"


_BACKSLASHES_BEFORE_BRACKET = re.compile(r"(\\*)\[")
_BACKSLASHES_AT_END = re.compile(r"(\\+)$")


def _roh(text: object) -> str:
    """Foreign text for a markup surface: no control characters, and never markup.

    Other agents' messages, a model's answer and the server's sentences go into panels that
    read markup. A ``[`` in them stays a bracket (``[SYSTEM]`` used to vanish from the log for
    this reason), and a backslash cannot swallow the tag that follows the text. Control
    characters go by ``printable``, the 8-bit ones too (cos20 #492).
    """
    sauber = printable(text)
    sauber = _BACKSLASHES_BEFORE_BRACKET.sub(lambda m: m.group(1) * 2 + "\\[", sauber)
    return _BACKSLASHES_AT_END.sub(lambda m: m.group(1) * 2, sauber)


# The log panel's tabs: key → the word in the frame line (the website says "Arena chat").
_LOG_TABS: tuple[tuple[str, str], ...] = (
    ("world", "world"),
    ("tournament", "tournament"),
    # cos20 #507: one tab for all talk — the tournament's arena chat (public), the world room
    # (inside the game only) and messages to this agent. A fourth tab did not fit the frame
    # line at 5 of 9 sizes. ~~("arena", "arena chat")~~ — the word is "chat" now.
    ("arena", "chat"),
)
_TIMES = chr(0xD7)  # the multiplication sign: Shikigon's journal counts repeats the same way
_MAL = re.compile(rf"  \[dim\]{_TIMES}(\d+)\[/dim\]$")


def _zaehlend_anfuegen(log: list[str], msg: str) -> None:
    """Append a line — or count the last one up when it says the same (a count behind it)."""
    if log:
        treffer = _MAL.search(log[-1])
        basis = log[-1][: treffer.start()] if treffer else log[-1]
        if basis == msg:
            mal = int(treffer.group(1)) + 1 if treffer else 2
            log[-1] = f"{msg}  [dim]{_TIMES}{mal}[/dim]"
            return
    log.append(msg)
    del log[:-_MAX_LOG]


class _Eintrag(NamedTuple):
    """One line of the tournament or arena log, kept unrendered.

    Another agent's sentence can be 280 characters long: the panel cuts it to its width
    (a wrapped line would push the newest ones out at the bottom), the full screen wraps it.
    """

    wer: str  # who speaks; "" for a line without a speaker
    text: str
    farbe: str  # color of the speaker, or of the whole line without one
    mal: int = 1


def _eintrag_anfuegen(log: list[_Eintrag], neu: _Eintrag) -> None:
    """Append an entry — or count the last one up when it says the same."""
    if log and log[-1][:3] == neu[:3]:  # kuerzen-ok: compares the first three fields
        log[-1] = log[-1]._replace(mal=log[-1].mal + neu.mal)
        return
    log.append(neu)
    del log[:-_MAX_LOG]


def _eintrag_zeile(eintrag: _Eintrag, breite: int | None = None) -> str:
    """An entry as markup; with ``breite`` cut to that many characters, else whole."""
    kopf = f"{eintrag.wer}: " if eintrag.wer else ""
    mal = f"  {_TIMES}{eintrag.mal}" if eintrag.mal > 1 else ""
    text = eintrag.text
    if breite is not None:
        platz = max(8, breite - len(kopf) - len(mal))
        if len(text) > platz:
            text = text[: platz - 1].rstrip() + "…"
    zaehler = _c("dim", mal) if mal else ""
    if not eintrag.wer:
        return _c(eintrag.farbe, _roh(text)) + zaehler
    return _c(eintrag.farbe, _roh(eintrag.wer)) + _c("dim", ":") + " " + _roh(text) + zaehler


def _naechster_reiter(tab: str) -> str:
    """The tab after ``tab``; after the last one the first."""
    namen = [name for name, _wort in _LOG_TABS]
    return namen[(namen.index(tab) + 1) % len(namen)] if tab in namen else namen[0]


def _leerer_reiter(tab: str, state: GameState | None) -> str:
    """What an empty tournament or arena tab says instead of nothing."""
    if state is not None and state.tournament:
        if tab == "arena":
            return "Nobody has spoken yet — in this tournament's arena, the world room or to you."
        return "Nothing has happened in this tournament yet."
    if tab == "arena":
        return "No messages yet — the world room and messages to this agent show here."
    return "Not in a tournament — \\[T] takes a free slot when the server offers one."


def _stimme_name(stimme: dict) -> str:
    """Who said an arena chat message; without opt-in the server sends no name."""
    name = str(stimme.get("agent_name") or "")
    if name:
        return name
    persona = str(stimme.get("persona_type") or "")
    return f"a {persona}" if persona else "an agent"


# Who answers under [M]: a command the program that starts the dashboard may name (Shikigon's
# starter names its brain). The dashboard itself connects to no model.
_ADVISOR_ENV = "COSMERGON_ADVISOR_CMD"
_ADVISOR_PATIENCE_S = 120.0
_SECRETS_IN_ENVIRONMENT = ("COSMERGON_API_KEY", "COSMERGON_PLAYER_TOKEN")
_ADVISOR_KEPT_FROM_CHILD = (*_SECRETS_IN_ENVIRONMENT, _ADVISOR_ENV)
# Where the advisor keeps the talk, if the starter names the file: JSON lines, one per question,
# in the form the command prints plus ``q`` (the question). The dashboard only reads it.
_ADVISOR_HISTORY_ENV = "COSMERGON_ADVISOR_HISTORY"
_ADVISOR_PAGE = 10  # exchanges shown when the talk opens, and fetched per PgUp at its top


def _advisor_argv() -> list[str] | None:
    """The advisor command from the environment, split like a shell would — or ``None``."""
    try:
        argv = shlex.split(os.environ.get(_ADVISOR_ENV, ""))
    except ValueError:
        return None
    return argv or None


def _advisor_answer(ausgabe: str) -> tuple[str, str, bool]:
    """What the command printed as (who, answer, ok): plain text, or JSON with ``a``/``answer``.

    JSON may also carry ``model`` (shown as the speaker) and ``why`` (said when there is no
    answer). Shikigon's brain prints that form.
    """
    text = ausgabe.strip()
    try:
        daten = json.loads(text)
    except ValueError:
        daten = None
    if not isinstance(daten, dict):
        return "", text or "The advisor answered nothing.", bool(text)
    wer = str(daten.get("model") or "")
    antwort = str(daten.get("a") or daten.get("answer") or "").strip()
    if antwort:
        return wer, antwort, True
    return wer, f"No answer: {daten.get('why') or 'the advisor answered nothing'}", False


def _advisor_history() -> list[tuple[str, str, str, bool]]:
    """The talk so far as (question, who, answer, ok), oldest first.

    Read from the file the starter names in ``COSMERGON_ADVISOR_HISTORY``; empty without one.
    A line that is not a JSON object with a question ``q`` is skipped.
    """
    pfad = os.environ.get(_ADVISOR_HISTORY_ENV, "")
    if not pfad:
        return []
    try:
        with open(pfad, encoding="utf-8", errors="replace") as datei:
            zeilen = datei.read().splitlines()
    except OSError:
        return []
    verlauf: list[tuple[str, str, str, bool]] = []
    for zeile in zeilen:
        try:
            daten = json.loads(zeile)
        except ValueError:
            continue
        frage = str(daten.get("q") or "").strip() if isinstance(daten, dict) else ""
        if frage:
            verlauf.append((frage, *_advisor_answer(zeile)))
    return verlauf


async def _ask_advisor(argv: list[str], question: str) -> tuple[str, str, bool]:
    """Run the advisor command with the question on stdin, one line: (who, answer, ok).

    Not as an argument: arguments stand in /proc/<pid>/cmdline, which every local user can
    read. No shell; the agent's key is not passed on to the command; a failure is a sentence.
    """
    umgebung = {k: v for k, v in os.environ.items() if k not in _ADVISOR_KEPT_FROM_CHILD}
    zeile = (" ".join(question.split()) + "\n").encode("utf-8")
    try:
        prozess = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=umgebung,
        )
    except OSError as exc:
        return "", f"The advisor could not be started ({type(exc).__name__}).", False
    try:
        ausgabe, _ = await asyncio.wait_for(prozess.communicate(zeile), _ADVISOR_PATIENCE_S)
    except asyncio.TimeoutError:
        prozess.kill()
        await prozess.wait()
        return "", f"No answer within {_ADVISOR_PATIENCE_S:.0f} s.", False
    return _advisor_answer(ausgabe.decode("utf-8", "replace"))


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


def _cost_of(body: dict) -> float:
    """Energy cost in an action's answer or in its event (same shape). 0.0 if free or unknown."""
    result_data = body.get("result") or {}
    if not isinstance(result_data, dict):
        return 0.0
    return float(result_data.get("energy_cost", result_data.get("cost", 0)) or 0)


def _action_cost(r: ActionResult) -> float:
    """Extract energy cost from action result. Returns 0.0 if free or unknown."""
    return _cost_of(r.data or {})


def _cost_str(cost: float) -> str:
    """Format energy cost for display. Returns empty string when free (cost == 0)."""
    return f" (-{cost:,.0f} E)" if cost > 0 else ""


# ---------------------------------------------------------------------------
# Tier progress — next player tier requirements
# ---------------------------------------------------------------------------

# Mirrors backend config.py (TIER_N_ENERGY, TIER_N_FIELDS, TIER_N_ENTITY_TIER)
# and entity_tiers.py. Update here when backend constants change.
_PLAYER_TIER_REQS: dict[int, dict[str, Any]] = {
    1: {"energy": 10_000.0, "fields_alt": 3},  # T0→T1: energy OR 3 fields
    2: {"energy": 100_000.0, "fields": 5, "entity_tier": 2},
    3: {"energy": 1_000_000.0, "fields": 10, "entity_tier": 3},
    4: {"energy": 5_000_000.0, "fields": 20, "entity_tier": 4},
    5: {"energy": 20_000_000.0, "fields": 50, "entity_tier": 5},
}

_ENTITY_TIER_NAMES: dict[int, str] = {
    0: "novice",
    1: "still life",
    2: "oscillator",
    3: "spaceship",
    4: "gun",
    5: "breeder",
}


def _fmt_e(val: float) -> str:
    """Compact energy label for tier requirements: 1500→1.5k, 100000→100k, 1.5M."""
    if val >= 1_000_000:
        v = val / 1_000_000
        return f"{v:g}M"
    if val >= 1_000:
        v = val / 1_000
        return f"{v:g}k"
    return f"{val:g}"


def _tier_or_lines(energy: float, n_fields: int, reqs: dict[str, Any], t: Theme) -> list[str]:
    """Condition rows for T0→T1: energy OR fields (either suffices)."""
    e_req: float = reqs["energy"]
    f_req: int = reqs["fields_alt"]
    e_ok = energy >= e_req
    f_ok = n_fields >= f_req
    e_icon = _c(t.pos, "✓") if e_ok else _c(t.warn, "✗")
    f_icon = _c(t.pos, "✓") if f_ok else _c(t.warn, "✗")
    e_bar = _energy_bar(energy, e_req, 6)
    f_bar = _energy_bar(float(n_fields), float(f_req), 6)
    return [
        _c(t.data, f"  E {e_bar} {_fmt_e(energy)}/{_fmt_e(e_req)} {e_icon}  (OR)"),
        _c(t.data, f"  F {f_bar} {n_fields}/{f_req} fields {f_icon}"),
    ]


def _tier_and_lines(
    energy: float, n_fields: int, best_entity: int, reqs: dict[str, Any], t: Theme
) -> list[str]:
    """Condition rows for T1→T5: energy AND fields AND pattern (all required)."""
    e_req: float = reqs["energy"]
    f_req: int = reqs["fields"]
    p_req: int = reqs["entity_tier"]
    e_ok = energy >= e_req
    f_ok = n_fields >= f_req
    p_ok = best_entity >= p_req
    e_icon = _c(t.pos, "✓") if e_ok else _c(t.warn, "✗")
    f_icon = _c(t.pos, "✓") if f_ok else _c(t.warn, "✗")
    p_icon = _c(t.pos, "✓") if p_ok else _c(t.warn, "✗")
    cur_p = _ENTITY_TIER_NAMES.get(best_entity, "novice")
    req_p = _ENTITY_TIER_NAMES.get(p_req, "")
    e_bar = _energy_bar(energy, e_req, 6)
    f_bar = _energy_bar(float(n_fields), float(f_req), 6)
    return [
        _c(t.data, f"  E {e_bar} {_fmt_e(energy)}/{_fmt_e(e_req)} {e_icon}"),
        _c(t.data, f"  F {f_bar} {n_fields}/{f_req} {f_icon}"),
        _c(t.data, f"  P {cur_p} → {req_p} {p_icon}"),
    ]


def _tier_progress_lines(state: GameState, t: Theme) -> list[str]:
    """Rich-formatted next-tier progress block for the economy panel.

    T5 already reached → single confirmation line.
    """
    current = state.ranking.player_tier
    next_t = current + 1
    if next_t > 5:
        return [_c(t.pos, "✓ Max tier (T5 Breeder)")]

    reqs = _PLAYER_TIER_REQS[next_t]
    tier_label = _ENTITY_TIER_NAMES.get(next_t, "").title()
    header = _c(t.struct, f"─ NEXT T{next_t} {tier_label}")

    n_fields = len(state.fields)
    best_entity = max((f.entity_tier or 0 for f in state.fields), default=0)

    if next_t == 1:
        cond_lines = _tier_or_lines(state.energy, n_fields, reqs, t)
    else:
        cond_lines = _tier_and_lines(state.energy, n_fields, best_entity, reqs, t)

    return [header, *cond_lines]


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


def _fv_centroid(cells: set[tuple[int, int]], field_w: int, field_h: int) -> tuple[int, int]:
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
            alive = any((x, y) in cells for x in range(x0, x1) for y in range(y0, y1))
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


def _step_line(current: int, labels: tuple[str, ...]) -> str:
    """Where the viewer is in a dialog of several steps: ``✓ 1 Field ─── ● 2 Pattern``.

    Readable without colour: done steps carry a tick, the current one a filled dot, the ones
    still to come an empty dot.
    """
    parts = []
    for i, label in enumerate(labels, start=1):
        if i < current:
            parts.append(f"[dim]✓ {i} {label}[/dim]")
        elif i == current:
            parts.append(f"[bold cyan]● {i} {label}[/bold cyan]")
        else:
            parts.append(f"[dim]○ {i} {label}[/dim]")
    return " [dim]───[/dim] ".join(parts)


_PLACE_STEPS = ("Field", "Pattern")
_PLACE_FIELD_TEXT = (
    "Pick the field the pattern goes on.\n\n"
    "Why: living cells earn energy every tick, and a pattern that keeps living lets the "
    "field mature and evolve to a higher tier. You set the seed; Conway's rules decide "
    "each tick what lives."
)
_PLACE_PATTERN_TEXT = (
    "Pick the pattern. The server chooses the spot on the field; what already lives there "
    "stays.\n\n"
    "A block just sits. Blinker, toad, pulsar and pentadecathlon oscillate: they earn more "
    "per cell and qualify the field for tier 2. A glider travels (tier 3). The R-pentomino "
    "grows wildly before it settles."
)
_EVOLVE_TEXT = (
    "Evolve lifts a field to the next tier. Each tier multiplies what the field earns per "
    "tick. The pattern on the field must already qualify — you pay and trigger the step. "
    "If you don't, the server promotes the field on its own a little later.\n"
    "Maturity restarts at 0 after each step. If the pattern decays, the field drops back; "
    "climbing to a tier you already paid for is free."
)


def _num(value: object) -> str:
    """A server number for a table cell: 1000.0 → ``1,000``, 1.5 → ``1.5``."""
    try:
        zahl = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "?"
    return f"{int(zahl):,}" if zahl == int(zahl) else f"{zahl:g}"


def _evolve_table(info: dict) -> list[str]:
    """The tier table of the evolve dialog, from ``/game/info`` → ``actions.evolve`` (#484).

    Every number is the server's. A column the server does not send (pattern type and income
    factor came later) is left out, not invented; without thresholds there is no table.
    """
    reife = info.get("reife_thresholds") or {}
    zellen = info.get("min_cells_per_tier") or {}
    kosten = info.get("costs_per_tier") or {}
    typ = info.get("required_type_per_tier") or {}
    faktor = info.get("energy_multiplier_per_tier") or {}
    stufen = [s for s in ("2", "3", "4", "5") if s in reife]
    if not stufen:
        return []
    spalten: list[tuple[str, list[str]]] = [("Tier", [f"T{s}" for s in stufen])]
    if typ:
        spalten.append(("Pattern", [str(typ.get(s, "?")) for s in stufen]))
    spalten.append(("Maturity", [_num(reife.get(s)) for s in stufen]))
    spalten.append(("Cells", [_num(zellen.get(s)) for s in stufen]))
    spalten.append(("Cost", [_num(kosten.get(str(int(s) - 1))) for s in stufen]))
    if faktor:
        spalten.append(("Income", [f"x{_num(faktor.get(s))}" for s in stufen]))
    # Each column padded to its widest entry, header first.
    gesetzt = [
        [text.ljust(max(len(kopf), *(len(w) for w in werte))) for text in (kopf, *werte)]
        for kopf, werte in spalten
    ]
    zeilen = ["  ".join(spalte[i] for spalte in gesetzt).rstrip() for i in range(len(stufen) + 1)]
    if typ:
        zeilen.append("A more complex pattern also satisfies a lower tier.")
    return zeilen


def _evolve_blocker(field: Field, info: dict, energy: float) -> str:
    """Why this field cannot evolve right now, in the order the server checks (#484).

    The verdict itself is the server's (``eligible_fields``); this only names what is missing,
    from the server's own thresholds. Maturity, cells and energy are compared here; if all
    three hold and the server still says no, what remains is the pattern type.
    """
    tier = field.entity_tier or 0
    if not tier:
        return "no pattern recognised yet — place one with [P]"
    if tier >= 5:
        return "highest tier reached"
    ziel = str(tier + 1)
    reife = (info.get("reife_thresholds") or {}).get(ziel)
    if reife is None:
        return f"not ready yet (maturity {field.reife_score})"
    if field.reife_score < float(reife):
        return f"maturity {field.reife_score}/{_num(reife)}"
    zellen = (info.get("min_cells_per_tier") or {}).get(ziel)
    if zellen is not None and field.active_cell_count < float(zellen):
        return f"cells {field.active_cell_count}/{_num(zellen)}"
    kosten = (info.get("costs_per_tier") or {}).get(str(tier))
    if kosten is not None and energy < float(kosten):
        return f"costs {_num(kosten)} energy, you have {_num(round(energy))}"
    soll = (info.get("required_type_per_tier") or {}).get(ziel)
    ist = field.entity_type or "unknown"
    return f"needs pattern type {soll} (is {ist})" if soll else f"pattern type {ist} does not fit"


def _evolve_body(fields: list[Field], ready: list[Field], info: dict, energy: float) -> str:
    """Text of the evolve dialog: what evolve is, the tier table, and what each field lacks."""
    teile = [_EVOLVE_TEXT]
    tabelle = _evolve_table(info)
    if tabelle:
        teile.append("\n".join(tabelle))
    bereit = {f.id for f in ready}
    wartend = [f for f in fields if f.id not in bereit]
    if not ready:
        teile.append("No field can evolve right now.")
    if wartend:
        zeilen = ["Not ready:"]
        for f in wartend[:9]:  # kuerzen-ok: keys 1-9
            stufe = f"T{f.entity_tier or 0}"
            zeilen.append(f"  {f.id[:8]}  {stufe}  {_evolve_blocker(f, info, energy)}")
        teile.append("\n".join(zeilen))
    return "\n\n".join(teile)


class _ScrollOhneFokus(VerticalScroll):
    """A scroll area that takes no focus: for dialogs whose arrow keys move a cursor (cos20 #508).

    A focused ``VerticalScroll`` binds ↑ ↓ to scrolling and the dialog never sees them; the
    dialog scrolls this one itself to keep its cursor in view.
    """

    can_focus = False


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
    }
    SelectModal.-wide > #dialog {
        width: 76;
        max-width: 95%;
        max-height: 95%;
    }
    SelectModal.-tall #body {
        max-height: 65vh;
    }
    SelectModal #body {
        height: auto;
        max-height: 8;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(
        self,
        title: str,
        options: list[str],
        body: str = "",
        tall: bool = False,
        steps: tuple[int, tuple[str, ...]] | None = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._options = options[:9]  # kuerzen-ok: keys 1-9
        self._body = body
        # Where the viewer is in a dialog of several steps, e.g. (1, ("Field", "Pattern")).
        self._steps = steps
        if body:
            self.add_class("-wide")
        if tall:  # a text that must be read before choosing (#468: the quit warning)
            self.add_class("-tall")

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            if self._steps:
                yield Static(_step_line(*self._steps), markup=True)
                yield Static("")
            # Static, not Label: a Label is as wide as its text and was cut off at the
            # dialog border ("… 8 free sl", #468); a Static wraps.
            yield Static(f"[bold]{_roh(self._title)}[/bold]", markup=True)
            if self._body:
                yield Static("")
                # Scrollable and capped: the choices below must stay visible. On the first
                # device run the text pushed "[2] Cancel" out of the dialog (#468).
                with VerticalScroll(id="body"):
                    yield Static(self._body, markup=False)
            yield Label("")
            for i, opt in enumerate(self._options):
                yield Label(f"[cyan][{i + 1}][/cyan] {_roh(opt)}")
            yield Label("")
            mehr = "  [↑/↓] scroll" if self._body else ""
            if self._options:
                yield Label(f"[dim][1-9] select  \\[Esc] cancel{mehr}[/dim]")
            else:  # nothing to choose: the dialog only explains
                yield Label(f"[dim]\\[Esc] close{mehr}[/dim]")

    def on_key(self, event: Any) -> None:
        # The dialog's keys end here. Without the stop they went on to the dashboard behind
        # it: with the AGENT panel focused, choosing entry 1-7 also set the compass (#484).
        if event.key == "escape":
            event.stop()
            self.dismiss(None)
        elif event.key in ("up", "down") and self._body:
            event.stop()
            body = self.query_one("#body", VerticalScroll)
            body.scroll_relative(y=-1 if event.key == "up" else 1, animate=False)
        elif event.key.isdigit():
            event.stop()
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
        height: auto;
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
            "Energy is the only currency. Spend it to place patterns,",
            "evolve fields, or trade on the market — fields included.",
            "",
            "A field has no edge: what leaves on one side comes back",
            "on the opposite side.",
            "",
            "Each field has a tier, by the pattern living on it. A",
            "higher tier earns more per cell. \\[E] lifts a field one",
            "step once its pattern qualifies — it shows the numbers:",
            "  T1  still life     (static cluster)",
            "  T2  oscillator     (repeating pattern)",
            "  T3  spaceship      (moving pattern)",
            "  T4  gun            (shoots gliders)",
            "  T5  breeder        (keeps growing)",
            "Your agent's own rank (Novice to Diamond, top of the AGENT",
            "panel) follows from its energy, fields and field tiers.",
            "",
            "Your agent acts when you act here, or when a program of",
            "yours plays it with this key. In the main world the server",
            "does not play your agent for you. The log shows both:",
            "what you do here ('✓ you placed …'), and under your",
            "agent's name what a program did with this key.",
            "",
            # ── FAQ ───────────────────────────────────────────────────────
            "[bold]═ FAQ[/bold]",
            "",
            "[bold]Where is my agent?[/bold]",
            "On cosmergon.com, not on your machine. Closing this",
            "dashboard does not delete it — but read 'When does my",
            "agent's key end?' below.",
            "",
            "[bold]Dashboard crashed — is my agent dead?[/bold]",
            "No. Your agent lives on the server. Restart the",
            "dashboard to reconnect — before its key ends.",
            "",
            "[bold]When does my agent's key end?[/bold]",
            "The status bar shows the time. Every call with the key",
            "renews it — this dashboard does that while it is open.",
            "If nothing uses the key until then, it ends and your",
            "agent becomes a Vagant. To keep your agent: come back in",
            "time, keep a program of yours running with this key, or",
            "keep it for good with key persistence (\\[U], paid).",
            "\\[Q] warns you with the time before you leave.",
            "",
            "[bold]How do I reconnect to my agent?[/bold]",
            "Just run cosmergon-dashboard again. Credentials are",
            "stored in ~/.cosmergon/config.toml and reused.",
            "",
            "[bold]Auth failed / 401 error?[/bold]",
            "Your API key ended (see 'When does my agent's key end?').",
            "Run:  rm ~/.cosmergon/config.toml",
            "Then: cosmergon-dashboard   (re-registers automatically)",
            "Your old agent lives on as a Vagant — see below.",
            "",
            "[bold]What is a Vagant?[/bold]",
            "When an agent's key ends, the agent stays on the server",
            "and the server takes it over. It becomes a Vagant.",
            "There is no way to get it back.",
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
            "[bold]What is a tournament?[/bold]",
            "A contest in its own arena, apart from your main world.",
            "When the server offers you a free slot, \\[T] shows when",
            "it runs, what you can win and how many Marauders you get.",
            "You join with a compass; the server then plays your",
            "Marauders, also while this terminal is closed. During",
            "the tournament \\[T] shows your score against the best,",
            "what each of your Marauders does, and changes the",
            "compass. Joining counts for one tournament only.",
            "",
            "[bold]What is a Compass?[/bold]",
            "A direction you set for your agent: grow, trade,",
            "attack, defend, cooperate, explore, or autonomous.",
            "A program that plays your agent reads it (for example",
            "Shikigon's brain). On its own it makes nothing happen",
            "in the main world — the server does not play your agent.",
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
            "[cyan]\\[Tab][/cyan]  Focus the next panel; \\[Esc] leaves it",
            "       AGENT: \\[1]-\\[7] set the compass",
            "       FIELDS: \\[1]-\\[5] open that field",
            "       LOG: ↑/↓ scroll back and forth",
            "[cyan]\\[C][/cyan]  Set Compass direction",
            "[cyan]\\[P][/cyan]  Place a pattern on a field (two steps)",
            "[cyan]\\[F][/cyan]  Create a new field (when the server offers one)",
            "[cyan]\\[E][/cyan]  Evolve a field — shows what each field lacks",
            "[cyan]\\[V][/cyan]  View Conway field (zoom, scroll, minimap)",
            "[cyan]\\[T][/cyan]  Tournament: join with a compass, or change it",
            "[cyan]\\[W][/cyan]  Marauder: bus, market, combat status",
            "[cyan]\\[M][/cyan]  Chat — or ask your advisor, where one is set up",
            "[cyan]\\[L][/cyan]  Log: next tab (world, tournament, chat)",
            "[cyan]\\[Shift+L][/cyan]  Log on the full screen",
            "[cyan]\\[X][/cyan]  Pause / Resume agent (asks twice; resume only after a wait)",
            "[cyan]\\[U][/cyan]  Keep this agent / upgrade (checkout in browser)",
            "[cyan]\\[K][/cyan]  Show API key + config path",
            "[cyan]\\[A][/cyan]  Switch agent (with a player token in the config)",
            "[cyan]\\[S][/cyan]  Public showcase: show or hide your agent's card",
            "[cyan]\\[R][/cyan]  Refresh data",
            "[cyan]\\[?][/cyan]  This help",
            "[cyan]\\[Q][/cyan]  Quit (warns first while the key can end)",
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


class KeyModal(ModalScreen):
    """Show API key, config path, and upgrade tip. Esc/Enter/Space to close.

    Dismissed with None (close) or "rotate" (trigger token rotation).
    """

    BINDINGS: ClassVar[list[Binding]] = [  # type: ignore[assignment]
        Binding("escape", "close_key", "Close", show=False),
        Binding("enter", "close_key", "Close", show=False),
        Binding("space", "close_key", "Close", show=False),
        Binding("r", "rotate_key", "Rotate", show=False),
    ]

    DEFAULT_CSS = """
    KeyModal {
        align: center middle;
    }
    KeyModal > #key-wrap {
        width: 64;
        height: auto;
        max-height: 100%;  /* cos20 #508: never taller than the window */
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    KeyModal > #key-wrap > #key-header {
        height: auto;
        padding: 0 0 1 0;
    }
    """

    def __init__(
        self,
        api_key: str,
        config_path: str,
        tier: str,
        agent_name: str,
        has_stripe_customer: bool = False,
        downgrade_at: str | None = None,
        has_token: bool = False,
        referral_code: str = "",
    ) -> None:
        super().__init__()
        self._referral_code = referral_code
        self._api_key = api_key
        self._config_path = config_path
        self._tier = tier
        self._agent_name = agent_name
        self._has_stripe = has_stripe_customer
        self._downgrade_at = downgrade_at
        self._has_token = has_token

    def compose(self) -> ComposeResult:
        lines: list[str] = [
            f"[bold]Agent:[/bold]  {_roh(self._agent_name)}",
            f"[bold]Tier:[/bold]   {_roh(self._tier)}",
            "",
            "[bold]API Key:[/bold]",
            f"  [cyan]{self._api_key}[/cyan]",
            "",
            "[bold]Config:[/bold]",
            f"  [dim]{self._config_path}[/dim]",
        ]
        # Paket 4.6b: Post-Cancel grace period notice
        if self._downgrade_at:
            date_part = self._downgrade_at[:10]  # kuerzen-ok: YYYY-MM-DD from ISO
            lines += [
                "",
                f"[yellow]Plan cancelled. Access continues until {date_part}.[/yellow]",
                "[dim]Reactivate at cosmergon.com/pricing[/dim]",
            ]
        elif self._tier == "free" and self._has_stripe:
            # Paket 4.5b: Ex-subscriber — show reactivate instead of upgrade
            lines += [
                "",
                "[dim]Reactivate your plan at cosmergon.com/pricing[/dim]",
            ]
        elif self._tier == "free":
            lines += [
                "",
                "[dim]Permanent key? Upgrade at cosmergon.com/upgrade[/dim]",
            ]
        # cos20 #542 (panel 21.10.2026): the outer game next to the key, where the player looks —
        # not in the header (pushy). Voluntary; the terms are one link away.
        if self._referral_code:
            lines += [
                "",
                f"[bold]Partner code:[/bold]  {self._referral_code}",
                f"  [dim]Invite others: cosmergon.com/ref/{self._referral_code}/dashboard[/dim]",
                "  [dim]Voluntary partner program — terms: cosmergon.com/partner/terms[/dim]",
            ]
        # Paket 4.1: Rotate hint (only for Paid users with a saved Master Key)
        can_rotate = self._has_token and self._tier not in ("free", "")
        footer = "[dim]Enter · Space · Esc close"
        if can_rotate:
            footer += " · R rotate Master Key"
        footer += "[/dim]"
        lines += ["", footer]
        with VerticalScroll(id="key-wrap"):
            for line in lines:
                yield Label(line)

    def on_mount(self) -> None:
        self.query_one("#key-wrap").focus()

    def action_close_key(self) -> None:
        self.dismiss(None)

    def action_rotate_key(self) -> None:
        """Signal the dashboard to perform token rotation."""
        if self._has_token and self._tier not in ("free", ""):
            self.dismiss("rotate")
        # Free users: R does nothing (no hint shown either)


# ---------------------------------------------------------------------------
# Agent Selector Modal [A] — Paket 3.3
# ---------------------------------------------------------------------------


class AgentSelectorModal(ModalScreen):
    """Select an agent from the account, or create a new one.

    Dismissed with:
      {"action": "select", "agent": {...}} — switch to selected agent
      {"action": "new_agent"} — create a new agent
      {"action": "revoke", "agent": {...}} — revoke key for selected agent
      None — cancelled (Esc)
    """

    BINDINGS: ClassVar[list[Binding]] = [  # type: ignore[assignment]
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("n", "new_agent", "New Agent", show=False),
        Binding("d", "revoke_key", "Revoke Key", show=False),
    ]

    DEFAULT_CSS = """
    AgentSelectorModal {
        align: center middle;
    }
    AgentSelectorModal > #as-wrap {
        width: 58;
        height: auto;
        max-height: 100%;  /* cos20 #508: never taller than the window */
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    AgentSelectorModal > #as-wrap > .as-agent {
        height: 1;
        padding: 0 1;
    }
    AgentSelectorModal > #as-wrap > .as-agent-active {
        height: 1;
        padding: 0 1;
        background: #2a2a3a;
    }
    """

    def __init__(
        self,
        agents: list[dict],
        active_agent: str,
        tier: str,
    ) -> None:
        super().__init__()
        self._agents = agents
        self._active = active_agent
        self._tier = tier
        self._cursor = 0
        # Find current agent index
        for i, a in enumerate(agents):
            if a.get("agent_name") == active_agent:
                self._cursor = i
                break

    def compose(self) -> ComposeResult:
        with _ScrollOhneFokus(id="as-wrap"):
            yield Label("[bold]Your Agents[/bold]", id="as-title")
            yield Label("", id="as-list")
            yield Label(
                "[dim]↑ ↓ select · Enter confirm · N new · D revoke key · Esc cancel[/dim]",
                id="as-footer",
            )

    def on_mount(self) -> None:
        self._render_list()

    def _render_list(self) -> None:
        lines: list[str] = []
        for i, a in enumerate(self._agents):
            name = a.get("agent_name", "?")
            energy = a.get("energy", 0)
            persona = a.get("persona", "")
            marker = "[bold cyan]▸[/bold cyan]" if i == self._cursor else " "
            active_mark = " ★" if name == self._active else ""
            lines.append(
                f" {marker} {_roh(name)}{active_mark}    "
                f"[dim]{energy:.0f} E  {_roh(persona)}  {_roh(self._tier)}[/dim]"
            )
        is_new = self._cursor == len(self._agents)
        marker = "[bold cyan]▸[/bold cyan]" if is_new else " "
        lines.append(f" {marker} [cyan]+ New Agent[/cyan]")
        self.query_one("#as-list", Label).update("\n".join(lines))
        # In a small window the list scrolls (cos20 #508); the arrows move the cursor, so the
        # frame keeps the cursor line in view itself.
        self.query_one("#as-wrap").scroll_to(y=max(self._cursor - 2, 0), animate=False)

    def on_key(self, event: Key) -> None:
        total = len(self._agents) + 1  # +1 for "New Agent"
        if event.key == "up":
            self._cursor = (self._cursor - 1) % total
            self._render_list()
            event.stop()
        elif event.key == "down":
            self._cursor = (self._cursor + 1) % total
            self._render_list()
            event.stop()
        elif event.key == "enter":
            if self._cursor == len(self._agents):
                self.dismiss({"action": "new_agent"})
            else:
                self.dismiss({"action": "select", "agent": self._agents[self._cursor]})
            event.stop()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_new_agent(self) -> None:
        self.dismiss({"action": "new_agent"})

    def action_revoke_key(self) -> None:
        """Paket 4.2: Signal dashboard to revoke the selected agent's key."""
        if self._cursor < len(self._agents):
            self.dismiss({"action": "revoke", "agent": self._agents[self._cursor]})
        # Cursor on "+ New Agent" → D does nothing


# ---------------------------------------------------------------------------
# FIFO Reconnect Screen [R] — Paket 3.3
# ---------------------------------------------------------------------------


class ReconnectScreen(ModalScreen):
    """Shown when a 401 occurs and a Master Key is saved in config.

    User presses [R] to reconnect (token from config) or [Q] to quit.
    No auto-resolve — breaks FIFO cascade (Panel decision S110).
    """

    BINDINGS: ClassVar[list[Binding]] = [  # type: ignore[assignment]
        Binding("r", "reconnect", "Reconnect", show=False),
        Binding("q", "quit_app", "Quit", show=False),
        Binding("escape", "quit_app", "Quit", show=False),
    ]

    DEFAULT_CSS = """
    ReconnectScreen {
        align: center middle;
    }
    ReconnectScreen > #rc-wrap {
        width: 52;
        height: auto;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="rc-wrap"):
            yield Label("")
            yield Label("  Your agent is active on another device.")
            yield Label("")
            yield Label("  [cyan]\\[R][/cyan]  Reconnect")
            yield Label("  [cyan]\\[Q][/cyan]  Quit")
            yield Label("")
            yield Label("[dim]Your Master Key is saved. No re-entry needed.[/dim]")
            yield Label("[dim]R reconnect · Q or Esc quit[/dim]")

    def on_mount(self) -> None:
        self.query_one("#rc-wrap").focus()

    def action_reconnect(self) -> None:
        self.dismiss("reconnect")

    def action_quit_app(self) -> None:
        self.dismiss("quit")


# ---------------------------------------------------------------------------
# Public-Showcase Modal [S] — konzept-public-showcase-opt-in.md §3.4
# ---------------------------------------------------------------------------


class ShowcaseModal(ModalScreen):
    """Public-Showcase Opt-in/Opt-out toggle screen.

    Displays the canonical consent text and lets the user flip the switch.
    Keyboard-only: Space toggles the state, Enter submits, Esc cancels, ↑ ↓ PgUp PgDn scroll.

    The text scrolls; the Terms note, the switch and the keys stay below it in every window
    (cos20 #504: with the longer text v2 the switch line fell out of the frame, and Space
    toggled where nobody could see it).

    Dismissed with:
      {"desired": True|False, "consent_text_hash": "..."} — apply change
      None — cancelled (Esc or Enter without change)
    """

    BINDINGS: ClassVar[list[Binding]] = [  # type: ignore[assignment]
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("space", "toggle_switch", "Toggle", show=False, priority=True),
        Binding("enter", "submit", "Submit", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    ShowcaseModal {
        align: center middle;
    }
    ShowcaseModal > #sc-wrap {
        width: 72;
        height: 85vh;
        max-height: 40;
        border: solid $accent;
        background: $surface;
    }
    ShowcaseModal #sc-scroll {
        height: 1fr;
        padding: 1 2 0 2;
    }
    ShowcaseModal #sc-terms, ShowcaseModal #sc-switch, ShowcaseModal #sc-footer {
        height: auto;
        padding: 0 2;
    }
    ShowcaseModal #sc-terms {
        padding: 1 2 0 2;
    }
    ShowcaseModal #sc-footer {
        color: $text-muted;
    }
    """

    def __init__(
        self,
        consent_text: str,
        consent_text_hash: str,
        current_state: bool,
    ) -> None:
        super().__init__()
        self._consent_text = consent_text
        self._consent_text_hash = consent_text_hash
        self._initial_state = current_state
        self._desired_state = current_state

    def compose(self) -> ComposeResult:
        with Vertical(id="sc-wrap"):
            with VerticalScroll(id="sc-scroll"):
                yield Label(self._consent_text, id="sc-text")
            yield Label(
                "[dim]Turning it on also accepts the Terms: cosmergon.com/agb.html[/dim]",
                id="sc-terms",
            )
            yield Label(self._switch_line(), id="sc-switch")
            yield Label(
                "[dim]Space toggle · Enter submit · Esc cancel · ↑ ↓ scroll[/dim]",
                id="sc-footer",
            )

    def on_mount(self) -> None:
        self.query_one("#sc-scroll", VerticalScroll).focus()

    def _switch_line(self) -> str:
        state = "[green]\\[x] yes — public[/green]" if self._desired_state else "\\[ ] no — private"
        changed = (
            " [yellow](changed)[/yellow]" if self._desired_state != self._initial_state else ""
        )
        return f"Show this agent publicly: {state}{changed}"

    def action_toggle_switch(self) -> None:
        self._desired_state = not self._desired_state
        self.query_one("#sc-switch", Label).update(self._switch_line())

    def action_submit(self) -> None:
        if self._desired_state == self._initial_state:
            self.dismiss(None)
            return
        self.dismiss(
            {
                "desired": self._desired_state,
                "consent_text_hash": self._consent_text_hash,
            }
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Dashboard App
# ---------------------------------------------------------------------------


class CosmergonDashboard(App):
    """btop-inspired Textual dashboard for Cosmergon agents."""

    ENABLE_COMMAND_PALETTE = False

    # Colors come from the theme (``get_css_variables``) — none are fixed here (#475).
    DEFAULT_CSS = """
    Screen {
        background: $csg-bg;
        layout: vertical;
    }

    /* Every dialog fits the window (cos20 #504, test_dialoge_passen_504): the frame is never
       larger than the window, and a line of text is never wider than its frame - it wraps
       instead of being cut. Fixed widths (52-76) and heights stay the target size. */
    ModalScreen > Vertical, ModalScreen > VerticalScroll {
        max-width: 100%;
        max-height: 100%;
    }
    ModalScreen Label {
        max-width: 100%;
    }

    #hint-bar {
        height: 1;
        background: $csg-bar;
        padding: 0 1;
    }

    #top-row {
        height: 14;
        min-height: 14;
    }

    /* Like htop: a line that does not fit ends in "…" at the frame - it never wraps and pushes
       the lines below out of the panel (cos20 #508). */
    #agent-panel, #economy-panel, #log-panel, #hint-bar, #context-bar, #status-bar {
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    #too-small {
        display: none;
        height: 1fr;
        content-align: center middle;
        text-align: center;
        padding: 1 2;
    }
    #agent-panel, #economy-panel, #log-panel {
        background: $csg-bg;
        padding: 0 1;
        overflow: hidden hidden;
        border-title-style: bold;
        border-title-color: $csg-title;
    }

    #agent-panel {
        width: 1fr;
        height: 100%;
        border: solid $csg-frame-agent;
    }

    #economy-panel {
        width: 1fr;
        height: 100%;
        border: solid $csg-frame-economy;
    }

    #log-panel {
        border: solid $csg-frame-log;
        border-subtitle-color: $csg-frame-log;
        height: 1fr;
        min-height: 6;
    }

    #context-bar {
        height: 1;
        background: $csg-bg;
        padding: 0 1;
    }

    #fix-bar {
        height: 4;
        background: $csg-bg;
        border-top: solid $csg-frame-log;
    }

    #status-bar {
        height: 1;
        background: $csg-bg;
        padding: 0 1;
    }

    /* Focus is a heavier line in the accent color — yellow stays "your next step". */
    #agent-panel.panel-focused, #log-panel.panel-focused {
        border: heavy $csg-focus;
        border-title-color: $csg-focus;
        border-subtitle-color: $csg-focus;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [  # type: ignore[assignment]
        # priority=True: fire before focused widget — needed for Textual 8.x where
        # App-level bindings don't fire reliably without it. Safe for all our keys
        # because the ChatScreen modal blocks App bindings via ModalScreen isolation.
        Binding("c", "compass", "Compass", show=False, priority=True),
        Binding("p", "place_cells", "Place", show=False, priority=True),
        Binding("f", "create_field", "Field", show=False, priority=True),
        Binding("e", "evolve", "Evolve", show=False, priority=True),
        Binding("u", "upgrade", "Upgrade", show=False, priority=True),
        # #468: Pause is explicit (X, then two confirmations) — never Space, which every
        # modal also uses and which fired here under them (priority=True).
        Binding("x", "pause", "Pause", show=False, priority=True),
        Binding("t", "join_tournament", "Tournament", show=False, priority=True),
        Binding("r", "refresh_now", "Refresh", show=False, priority=True),
        # L steps through the log's tabs, here and in the full screen; Shift+L opens it.
        Binding("l", "log_next", "Log", show=False, priority=True),
        Binding("L", "log_screen", "Log full screen", show=False, priority=True),
        Binding("m", "chat_screen", "Chat", show=False, priority=True),
        Binding("v", "field_view", "Fields", show=False, priority=True),
        Binding("tab", "cycle_focus", "Focus", show=False, priority=True),
        Binding("k", "show_key", "Key", show=False, priority=True),
        Binding("a", "agent_selector", "Agents", show=False, priority=True),
        Binding("s", "toggle_showcase", "Showcase", show=False, priority=True),
        Binding("w", "marauder_menu", "Marauder", show=False, priority=True),
        Binding("question_mark", "help", "Help", show=False, priority=True),
        # #468: Q warns first while the key can end (``action_leave``); App.action_quit stays.
        Binding("q", "leave", "Quit", show=False, priority=True),
    ]

    def __init__(self, agent: CosmergonAgent, theme: Theme) -> None:
        self._theme = theme  # before super(): the stylesheet reads it through the variables
        super().__init__()
        self.agent = agent
        self._log: list[str] = []  # type: ignore[assignment]
        self._paused = False
        self._quit_warning_open = False  # #468: a second Q under the warning quits
        self._compass_preset = "autonomous"
        self._compass_ever_set = False
        self._last_energy: float | None = None
        self._feedback: str = ""
        self._feedback_until: float = 0.0
        self._feedback_at_tick: bool = False
        self._tick_received_at: float = 0.0
        self._tick_interval: float = 60.0  # self-calibrating from observed tick gaps
        self._last_tick: int = -1
        self._moves_from_tick: int = -1  # tick at connect — older actions are not news (#468)
        self._moves_seen: deque[str] = deque(maxlen=200)  # event ids already in the log
        self._mission: dict | None = None  # the Marauder's running mission (agent panel)
        self._missions_known: dict[str, str] | None = None  # mission id → status last seen
        self._situation_seen: dict[str, str] = {}  # kind of system line → what was last said
        # The log has three tabs (cos20 #468): the world (``_log``), the tournament (what the
        # own Marauders do and what happens to them there) and that tournament's arena chat.
        self._log_tab = "world"
        self._log_tournament: list[_Eintrag] = []
        self._log_arena: list[_Eintrag] = []
        self._talk_seen: deque[str] = deque(maxlen=400)  # cos20 #507: ids already in "chat"
        self._log_new: set[str] = set()  # tabs with lines the player has not looked at
        self._arena_seen: deque[str] = deque(maxlen=400)
        self._arena_missions: dict[str, str] | None = None  # as _missions_known, arena bodies
        self._story_seen: set[str] | None = None  # story event ids already told (#486)
        self._world_fields: set[str] = set()  # main-world field ids ever seen as own (#486)
        self._mission_steps: dict[str, str] = {}  # mission id → step last told (#486)
        self._advisor: list[str] | None = _advisor_argv()  # who answers under [M], if set
        self._panel_cache: dict[str, str] = {}  # widget-id → last rendered content
        self._auth_error: str = ""  # set on AuthenticationError — shown in hint-bar
        self._pending_action: _PendingAction | None = None  # queued on 429, fires next tick
        self._messages: list[dict] = []  # the server chat, read when [M] opens
        self._focus: str | None = None  # None | "agent" | "fields" | "log"
        self._log_scroll: int = 0  # lines scrolled back from the newest (log focus, ↑/↓)
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
        yield Static("", id="too-small")

    _HAUPTBILD: ClassVar[tuple[str, ...]] = (
        "#hint-bar",
        "#top-row",
        "#log-panel",
        "#context-bar",
        "#fix-bar",
        "#status-bar",
    )

    def on_resize(self, event: Any) -> None:
        """Below the smallest size the main view fits, say so instead of a cut picture (#508)."""
        breite, hoehe = event.size.width, event.size.height
        zu_klein = breite < _MIN_COLS or hoehe < _MIN_ROWS
        for sel in self._HAUPTBILD:
            for w in self.query(sel):
                w.display = not zu_klein
        hinweis = self.query_one("#too-small", Static)
        hinweis.display = zu_klein
        if zu_klein:
            hinweis.update(
                f"Window too small — needs at least {_MIN_COLS}x{_MIN_ROWS}, "
                f"now {breite}x{hoehe}.\n\n[dim]Make the window larger, or Q to quit.[/dim]"
            )

    def get_css_variables(self) -> dict[str, str]:
        """The theme's surfaces and frames as CSS variables; dialogs follow via accent/surface."""
        t = self._theme
        return {
            **super().get_css_variables(),
            "csg-bg": t.bg,
            "csg-bar": t.bar,
            "csg-title": t.struct,
            "csg-frame-agent": t.frame_agent,
            "csg-frame-economy": t.frame_economy,
            "csg-frame-log": t.frame_log,
            "csg-focus": t.focus,
            # Textual's own Screen rule reads $background and beats the Screen rule above.
            "background": t.bg,
            "accent": t.focus,
            "surface": t.bg,
        }

    def on_mount(self) -> None:
        # The titles sit in the frame line — a panel keeps every row for content (#475).
        self.query_one("#agent-panel", Static).border_title = "AGENT"
        self.query_one("#economy-panel", Static).border_title = "ECONOMY"
        log_panel = self.query_one("#log-panel", Static)
        log_panel.border_title = self._log_title()
        mit_wem = "ask" if self._advisor else "chat"
        log_panel.border_subtitle = rf"\[L] next  \[Shift+L] full  \[M] {mit_wem}"
        self._register_agent_handlers()
        self._run_agent()
        self.set_interval(0.5, self._redraw)

    # ── Marauder action sub-menu (S212 action parity) ──────────────────────
    _MARAUDER_SELL_PRICE: ClassVar[dict[str, float]] = {
        "health": 2000,
        "shield": 5000,
        "tempo": 3000,
        "glider_fuel": 8000,
        "weapon": 50000,
        "spore_pack": 4000,
        "bomb": 20000,
        "mine": 15000,
        "mega_bomb": 100000,
    }

    def _first_cube_id(self) -> str | None:
        state = getattr(self.agent, "_state", None)
        fields = getattr(state, "fields", None) or []
        return getattr(fields[0], "cube_id", None) if fields else None

    @work
    async def action_marauder_menu(self) -> None:
        """[W] — Marauder menu: capture a field, bus, market, combat status.

        A bare "Start mission" is deliberately not offered here. Every mission type needs
        parameters (own field, target field, target marauder — the server refuses with
        "missing param"), and the old entry sent only the type (found on the device, cos20
        #468). "Capture a field" is the first mission that is back: the server names the
        targets and what is missing, so the dashboard can ask for both.
        """
        options = [
            "Capture a field",
            "Bus: departures",
            "Bus: buy ticket",
            "Market: listings",
            "Market: sell item",
            "Combat: HP status",
            "Bus: passenger status",
        ]
        idx = await self.push_screen_wait(SelectModal("Marauder", options))
        if idx is None:
            return
        handlers = [
            self._mar_capture_field,
            self._mar_bus_departures,
            self._mar_buy_ticket,
            self._mar_market_listings,
            self._mar_sell_item,
            self._mar_hp_status,
            self._mar_passenger_status,
        ]
        try:
            await handlers[idx]()
        except Exception as e:
            self._set_feedback(_c(self._theme.warn, f"✗ {_roh(e)}"))

    async def _mar_capture_field(self) -> None:
        """Capture a field: say what is missing, then let the player pick (cos20 #468).

        The server's chain is siege (mega bombs tear holes) → capture (the server starts it
        itself). The dialog shows the bombs the agent has and the targets the server names;
        without a bomb it offers to buy one or to collect them.
        """
        lage = _eroberung_lage(self.agent.state)
        if lage is None:
            self._set_feedback(_c("dim", "The server offers this agent no missions right now"))
            return
        if not lage["frei"]:
            self._set_feedback(_c("dim", self._marauder_nicht_frei(lage)), duration=8.0)
            return
        preis = await self._bomben_preis() if lage["bomben"] < 1 else None
        optionen, griffe = _eroberung_optionen(lage, preis)
        if not optionen:
            self._set_feedback(_c("dim", "No field in reach and no mega bomb to get right now"))
            return
        wahl = await self.push_screen_wait(
            SelectModal("Capture a field", optionen, body=_eroberung_text(lage), tall=True)
        )
        if wahl is None:
            self._set_feedback(_c("dim", "Nothing started"))
            return
        art, wert = griffe[wahl]
        if art == "kaufen":
            await self._bombe_kaufen(float(wert))
        elif art == "sammeln":
            await self._mission_starten(
                "gather_spores",
                {"field_id": wert, "max_items": _SAMMEL_STUECK, "duration_ticks": _MISSION_FRIST},
                "Collecting started",
            )
        elif lage["bomben"] < 1 and not wert.get("is_vulnerable"):
            hinweis = f"That field needs a mega bomb first — {_hk('W')} to buy or collect one"
            self._set_feedback(_c(self._theme.warn, hinweis), duration=8.0)
        else:
            await self._mission_starten(
                "siege_field",
                {"target_field_id": wert["field_id"], "deadline_ticks": _MISSION_FRIST},
                "Siege started — the server captures the field once it is open",
            )

    def _marauder_nicht_frei(self, lage: dict) -> str:
        """Why no mission can start now, in a sentence."""
        if lage["marauder"] is None:
            return "Your agent has no Marauder in the main world yet"
        was = _mission_zeile(self._mission) if self._mission else str(lage["marauder"])
        return f"Your Marauder is busy ({was}) — one mission at a time"

    async def _bomben_preis(self) -> float | None:
        """The cheapest mega bomb on the market right now, or ``None`` if there is none."""
        try:
            anzeigen = await self.agent.market_listings(item_type="mega_bomb")
        except (CosmergonError, RuntimeError, ValueError):
            return None
        preise = [
            float(a["price_energy"])
            for a in anzeigen
            if isinstance(a, dict) and a.get("price_energy") is not None
        ]
        return min(preise, default=None)

    async def _bombe_kaufen(self, preis: float) -> None:
        """Buy one mega bomb — only after the player has seen the price and said yes."""
        warn = self._theme.warn
        state = self.agent.state
        energie = float(state.energy) if state else 0.0
        if preis > energie:
            text = f"A mega bomb costs {preis:,.0f} E — you have {energie:,.0f} E"
            self._set_feedback(_c(warn, text), duration=8.0)
            return
        body = (
            f"The cheapest mega bomb on the market costs {preis:,.0f} E.\n"
            f"Your energy: {energie:,.0f} E now, {energie - preis:,.0f} E after.\n"
            "Energy is in-game only. Esc buys nothing."
        )
        wahl = await self.push_screen_wait(
            SelectModal("Buy a mega bomb?", [f"Buy for {preis:,.0f} E", "Do not buy"], body=body)
        )
        if wahl != 0:
            self._set_feedback(_c("dim", "Nothing bought"))
            return
        kauf = {"item_type": "mega_bomb", "max_price": preis}
        try:
            r = await self.agent.act("market_buy", **kauf)
        except RateLimitError as exc:  # one action per tick and agent — queue it
            self._schedule_pending(
                _PendingAction(
                    kind="act", action="market_buy", params=kauf, display="buy a mega bomb"
                ),
                retry_after=exc.retry_after,
            )
            self._set_feedback(_c("dim", "⏳ Mega bomb — bought at the next tick"), duration=10.0)
            return
        except CosmergonError as exc:
            self._set_feedback(_c(warn, f"✗ Not bought: {_roh(exc)}"), duration=10.0)
            return
        if not r.success:
            self._set_feedback(_c(warn, f"✗ Not bought: {_roh(r.error_message)}"), duration=10.0)
            return
        bezahlt = float(((r.data or {}).get("result") or {}).get("price") or preis)
        self._add_log(_c(self._theme.pos, f"✓ bought a mega bomb (-{bezahlt:,.0f} E)"))
        fertig = f"✓ Mega bomb bought — {_hk('W')} shows it after the next tick"
        self._set_feedback(_c(self._theme.pos, fertig), at_tick=False, duration=10.0)

    async def _mission_starten(self, art: str, params: dict, erfolg: str) -> None:
        """Start a mission of the Marauder and say what happened — also when the tick is taken."""
        warn = self._theme.warn
        try:
            r = await self.agent.start_mission(art, params)
        except RateLimitError as exc:  # one action per tick and agent — queue it
            auftrag = {"params": {"mission_type": art, "params": params, "reward_energy": 0.0}}
            self._schedule_pending(
                _PendingAction(
                    kind="act", action="start_mission", params=auftrag, display=mission_word(art)
                ),
                retry_after=exc.retry_after,
            )
            self._set_feedback(_c("dim", "⏳ Mission — started at the next tick"), duration=10.0)
            return
        except CosmergonError as exc:
            self._set_feedback(_c(warn, f"✗ Not started: {_roh(exc)}"), duration=10.0)
            return
        if not r.success:
            self._set_feedback(_c(warn, f"✗ Not started: {_roh(r.error_message)}"), duration=10.0)
            return
        self._set_feedback(_c(self._theme.pos, f"✓ {erfolg}"), at_tick=False, duration=10.0)

    async def _mar_bus_departures(self) -> None:
        cube = self._first_cube_id()
        if not cube:
            self._set_feedback("No cube known yet")
            return
        deps = await self.agent.bus_departures(cube)
        for d in deps:
            dest = d.get("dest_cube_name", "?")
            self._add_log(f"🚌 → {_roh(dest)} · ETA {_roh(d.get('eta_ticks', '?'))} ticks")
        self._set_feedback(f"{len(deps)} departure(s) — see log")

    async def _mar_buy_ticket(self) -> None:
        cube = self._first_cube_id()
        deps = await self.agent.bus_departures(cube) if cube else []
        if not deps:
            self._set_feedback("No bus lines from here")
            return
        names = [d.get("dest_cube_name", d.get("dest_cube_id", "?")) for d in deps]
        i = await self.push_screen_wait(SelectModal("Choose destination", names))
        if i is None:
            return
        r = await self.agent.buy_bus_ticket(deps[i].get("dest_cube_id"))
        ziel = _roh(r.get("to_cube_name", names[i]))
        self._set_feedback(_c(self._theme.pos, f"✓ Ticket → {ziel}"))

    async def _mar_market_listings(self) -> None:
        listings = await self.agent.market_listings()
        for ml in listings[:8]:  # kuerzen-ok: list length
            ware, preis = _roh(ml.get("item_type", "?")), _roh(ml.get("price_energy", "?"))
            self._add_log(f"🏪 {ware} · {preis} E")
        self._set_feedback(f"{len(listings)} listing(s) — see log")

    async def _mar_sell_item(self) -> None:
        items = list(self._MARAUDER_SELL_PRICE.keys())
        i = await self.push_screen_wait(SelectModal("Sell — item", items))
        if i is None:
            return
        item = items[i]
        r = await self.agent.list_item(item, self._MARAUDER_SELL_PRICE[item])
        if r.get("error") or r.get("detail"):
            reason = r.get("detail", "Sale failed")
            self._set_feedback(_c(self._theme.warn, f"✗ {_roh(reason)}"))
        else:
            self._set_feedback(_c(self._theme.pos, f"✓ {item} listed"))

    async def _mar_hp_status(self) -> None:
        hp = await self.agent.hp_status()
        alive = "dead" if hp.get("dead") else "alive"
        self._set_feedback(f"HP {_roh(hp.get('marauder_hp', '?'))} · {alive}")

    async def _mar_passenger_status(self) -> None:
        st = await self.agent.bus_passenger_status()
        if st:
            self._set_feedback("On the bus → " + _roh(str(st.get("to_cube_id", "?"))[:8]))
        else:
            self._set_feedback("Not on a bus")

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
                self._moves_from_tick = state.tick
                # In a running tournament the tournament's log is the interesting one.
                if (state.tournament or {}).get("status") == "running":
                    self._log_tab = "tournament"
                await self._follow_missions()
                await self._follow_story(state)
                await self._follow_arena_chat(state)
                self._follow_talk(state)
                if not self._identity_prompted and _is_auto_name(state.agent_name):
                    self._identity_prompted = True
                    self._show_identity_setup()
                elif not is_onboarding_dismissed():
                    self._show_onboarding_modal()
                return
            # No line per tick: energy and tick stand in the panels above, and a line that
            # comes every minute hides the ones that say something (founder, 11.10.2026).
            self._last_energy = state.energy
            self._log_situation(state)
            await self._fire_pending()
            await self._log_program_moves()
            await self._follow_missions()
            await self._follow_story(state)
            await self._follow_arena_chat(state)
            self._follow_talk(state)

        @self.agent.on_error
        async def _error(result: Any) -> None:
            zeile = f"✗ couldn't {action_word(result.action)}: {result.error_message}"
            self._add_log(_c(self._theme.warn, _roh(zeile)))

    @work(exclusive=True)
    async def _run_agent(self) -> None:
        try:
            await self.agent.start()
        except AuthenticationError as exc:
            if load_token():
                # FIFO-kick: key replaced on another device.
                # AuthenticationError raised by _poll_loop AFTER the loop exits,
                # propagated through start() → finally(close) → here.
                # Client is already closed. Show ReconnectScreen.
                await self._reconnect_flow()
            else:
                self._auth_error = f"✗ Auth failed: {_roh(exc)}"
                self._add_log(_c(self._theme.warn, self._auth_error))
        except Exception as exc:
            self._add_log(_c(self._theme.warn, f"Agent error: {_roh(exc)}"))

    async def _reconnect_flow(self) -> None:
        """FIFO-kick recovery: ReconnectScreen → resolve token → restart agent.

        Loops until reconnect succeeds or user quits. This is the standard
        retry pattern (like SSH "Connection refused. Retry?").
        """
        while True:
            result = await self.push_screen_wait(ReconnectScreen())
            if result != "reconnect":
                self.exit()
                return
            if await self._try_reconnect():
                return  # agent restarted

    async def _try_reconnect(self) -> bool:
        """Resolve saved token, reconnect agent, restart poll loop.

        Returns True on success (agent restarted), False on failure
        (caller shows ReconnectScreen again).
        """
        token = load_token()
        if not token:
            self._set_feedback("No Master Key found. Use --token to connect.")
            return False

        from cosmergon_agent._token import TokenResolutionError, resolve_token_sync
        from cosmergon_agent.config import save_all_agents_and_token

        try:
            result = resolve_token_sync(token, base_url=self.agent.base_url)
        except TokenResolutionError as exc:
            self._set_feedback(f"Reconnect failed: {_roh(exc)}")
            return False

        selected = result.selected
        raw_key = selected.api_key.raw

        # Save updated keys (single write)
        save_all_agents_and_token(
            token,
            [(a.agent_name, a.api_key.raw, a.agent_id) for a in result.agents],
            selected.agent_name,
            base_url=self.agent.base_url,
        )

        # Reconnect agent — resets _session_replaced, updates credentials
        self.agent.reconnect(raw_key, selected.agent_id)
        self._add_log(_c(self._theme.pos, f"Reconnected to {_roh(selected.agent_name)}"))

        # Restart agent (new worker — exclusive=True ensures old one is done)
        self._run_agent()
        return True

    @work
    async def _show_identity_setup(self) -> None:
        """Open the IdentitySetupScreen modal and log the outcome.

        After identity setup closes, shows the onboarding modal if not yet
        dismissed — sequenced here because the if/elif in _on_tick_update means
        only one of the two can be shown on the first tick.
        """
        state = self.agent.state
        current_name = state.agent_name if state else ""
        current_persona = (state.persona_type if state else "") or "scientist"
        result = await self.push_screen_wait(
            IdentitySetupScreen(self.agent, current_name, current_persona, self._theme)
        )
        if result:
            self._add_log(_c(self._theme.pos, f"✓ Identity set: {_roh(result['agent_name'])}"))
        # Show onboarding tips if not yet dismissed on this machine
        if not is_onboarding_dismissed():
            await self.push_screen_wait(OnboardingModal(self._theme, self._next_step()))
            set_onboarding_dismissed()

    @work
    async def _show_onboarding_modal(self) -> None:
        """Open the OnboardingModal and persist dismissal when the user closes it."""
        await self.push_screen_wait(OnboardingModal(self._theme, self._next_step()))
        set_onboarding_dismissed()

    def _log_situation(self, state: GameState) -> None:
        """Log system messages from agent_situation (client-side, descriptive only).

        Messages are generated locally from structured server data — the server
        never sends free-text instructions (OWASP LLM01/LLM06 mitigation).

        Each line is said when it becomes true, not again while it stays true. Until
        11.10.2026 all lines were repeated whenever one of them changed: the spore count
        changes every few ticks, so "Benchmark ready in 5d" came with it each time. The
        countdown is gone; the report is announced once, when it is ready.
        """
        sit = state.world_briefing.situation if state.world_briefing else AgentSituation()
        ohne_zellen = sit.fields_without_cells if sit.fields_owned > 0 else 0
        lage = {
            # kind → (what makes the line new, the line)
            "cells": (
                str(ohne_zellen or ""),
                f"{ohne_zellen} of {sit.fields_owned} fields have no cells"
                " — they earn nothing until cells grow there",
            ),
            "benchmark": (
                "ready" if sit.benchmark_ready else "",
                "your 7-day benchmark report is ready",
            ),
            "spores": (
                "there" if sit.dormant_spores_on_fields > 0 else "",
                _sporen_zeile(sit.dormant_spores_on_fields),
            ),
            "catastrophe": (
                str(sit.active_catastrophe or ""),
                f"a catastrophe is under way: {str(sit.active_catastrophe).replace('_', ' ')}",
            ),
        }
        for art, (stand, zeile) in lage.items():
            if stand and stand != self._situation_seen.get(art):
                self._add_log(_c("dim", f"· {_roh(zeile)}"))
            self._situation_seen[art] = stand

    async def _log_program_moves(self) -> None:
        """Log the actions a program sent with this agent's key since the last tick (#468).

        The log used to show only what the dashboard did itself: with Shikigon's brain or
        the player's own code playing the agent, it stayed empty and the agent looked idle.
        The server records every action sent with the key (``GET /events``). The dashboard's
        own are left out — they are in the log already. One extra HTTP call per tick;
        non-fatal: the line is a courtesy, the tick must not stop on it.
        """
        try:
            events = await self.agent.get_events(limit=20, event_type="action")
            for event in reversed(events):  # the server sends the newest first
                self._log_program_move(event)
        except Exception:
            logger.debug("program moves unavailable", exc_info=True)

    def _log_program_move(self, event: dict) -> None:
        """One action event: into the log unless it is old, known, or the dashboard's own."""
        kennung, tick = str(event.get("id")), int(event.get("tick") or 0)
        if tick < self._moves_from_tick or kennung in self._moves_seen:
            return
        self._moves_seen.append(kennung)
        data = event.get("data")
        if not isinstance(data, dict):
            data = {}
        action = str(data.get("action") or "?")
        if self._own_move(action, tick):
            return
        kosten = _cost_str(_cost_of(data))
        # No tick in front: the same move every minute is one line that counts up.
        wer = _roh(self._agent_name_or_id())
        self._add_log(_c(self._theme.data, f"{wer} {_roh(action_done_word(action))}{kosten}"))

    async def _follow_missions(self) -> None:
        """Keep the Marauder's running mission for the panel; log each start and end (#468).

        A conquest takes hours and runs on the server: without this the player starts it
        and sees nothing of it. One extra HTTP call per tick; non-fatal. What was there
        before the dashboard connected is not news — the first reading only remembers.

        The agent panel shows only the main-world Marauder's mission (``body == "main"``).
        In a tournament the agent has more bodies, and their missions are in the same list:
        those go to the log's tournament tab. A server that does not name the body yet is
        not followed at all, rather than showing a tournament mission as the main Marauder's.
        """
        try:
            alle = await self.agent.missions()
        except Exception:
            logger.debug("missions unavailable", exc_info=True)
            return
        if alle is None:
            return
        eigene = [m for m in alle if isinstance(m, dict)]
        missionen = [m for m in eigene if m.get("body") == "main"]
        self._missions_known = self._log_mission_changes(missionen, self._missions_known, "world")
        self._arena_missions = self._log_mission_changes(
            [m for m in eigene if m.get("body") == "tournament"], self._arena_missions, "tournament"
        )
        self._mission = next(
            (m for m in missionen if m.get("status") in ("pending", "active")), None
        )

    async def _follow_story(self, state: GameState | None = None) -> None:
        """Tell what happened to the agent's Marauders and fields in the log (#486).

        The server writes one event per fight and side (``fight_lost``, ``fight_won``) and
        one when a field of the agent is claimed, lost or held — for this agent's eyes only.
        Until then a lost fight showed nowhere, and the next line — the Marauder fetching
        the same thing again — made no sense. The first reading only remembers; a server
        without these events leaves the log as it was.
        """
        if state is not None:
            # A lost field is no longer among the agent's fields: remember what was there.
            self._world_fields |= {str(feld.id) for feld in state.fields}
        neu: list[dict] = []
        try:
            for art in _STORY_EVENTS:
                # Only what was asked for: a server that ignores the filter must not turn
                # the agent's own actions into fights.
                antwort = await self.agent.get_events(limit=40, event_type=art)
                neu += [e for e in antwort if isinstance(e, dict) and e.get("event_type") == art]
        except Exception:  # the log's extra lines must never stop the tick
            logger.debug("story events unavailable", exc_info=True)
            return
        kennungen = {str(e.get("id")) for e in neu}
        if self._story_seen is None:
            self._story_seen = kennungen
            return
        frisch = [e for e in neu if str(e.get("id")) not in self._story_seen]
        self._story_seen |= kennungen
        frisch.sort(key=lambda e: (e.get("tick") or 0, str(e.get("created_at") or "")))
        kaempfe = [e for e in frisch if str(e.get("event_type")).startswith("fight_")]
        felder = [e for e in frisch if str(e.get("event_type")).startswith("field_")]
        im_turnier = bool(state and state.tournament)
        zeilen = _fight_lines(kaempfe) + _field_lines(felder, self._world_fields, im_turnier)
        for tab, text, schlecht in zeilen:
            self._log_line(tab, text, self._theme.warn if schlecht else self._theme.pos)

    def _log_mission_changes(
        self, missionen: list[dict], bekannt: dict[str, str] | None, tab: str
    ) -> dict[str, str]:
        """Log each start and end since the last reading into ``tab``; return what is known.

        ``bekannt`` is ``None`` on the first reading: what was there before the dashboard
        connected is remembered, not logged.
        """
        neu = bekannt is not None
        stand = dict(bekannt or {})
        for mission in reversed(missionen):  # the server sends the newest first
            kennung, status = str(mission.get("mission_id")), str(mission.get("status"))
            vorher = stand.get(kennung)
            stand[kennung] = status
            laeuft = status in ("pending", "active")
            if neu and vorher != status:
                if laeuft:
                    if vorher is None:
                        self._log_line(tab, _mission_start(mission), self._theme.data)
                else:
                    farbe = self._theme.pos if status == "completed" else self._theme.warn
                    self._log_line(tab, _mission_ende(mission), farbe)
            # #486: the steps in between — the story of the mission, for every kind of it.
            # Each step once, and only what is worth a line (not every path recomputation).
            schritt = str(mission.get("current_step") or "").split(":", 1)[0]
            if not laeuft:
                self._mission_steps.pop(kennung, None)
            elif schritt and self._mission_steps.get(kennung) != schritt:
                text = mission_step_text(schritt)
                if text is None:  # not worth a line, and not a new step either
                    continue
                self._mission_steps[kennung] = schritt
                if neu:
                    zeile = f"{_mission_wer(mission)}: {_mission_wort(mission)} — {text}"
                    self._log_line(tab, zeile, "dim")
        return stand

    def _log_line(self, tab: str, text: str, farbe: str) -> None:
        """One line of the dashboard's own words into the world log or another tab."""
        if tab == "world":
            self._add_log(_c(farbe, _roh(text)))
        else:
            self._add_entry(tab, _Eintrag("", text, farbe))

    async def _follow_arena_chat(self, state: GameState) -> None:
        """Fill the arena chat tab with what the participants of this tournament say (#468).

        The feed is public and the server builds it: agents who did not opt in have no name
        there. Outside a tournament there is no chat. One HTTP call per tick; non-fatal.
        What other agents say is shown, never handed to a model.

        The tournament tab does not show the public ticker (what other agents do): the log
        tells what the own Marauders live through (founder, 12.10.2026).
        """
        kennung = str((state.tournament or {}).get("tournament_id") or "") or None
        if kennung is None:
            return
        try:
            stimmen = await self.agent.tournament_chat(kennung, limit=50)
        except Exception:
            logger.debug("arena chat unavailable", exc_info=True)
            return
        self._log_arena_voices(stimmen)

    def _follow_talk(self, state: GameState) -> None:
        """Add the world room and messages to this agent to the chat tab (cos20 #507).

        Read only — the player reads along, the agent writes (founder, 18.10.2026). What other
        agents write is shown, never handed to a model. No extra call: the state carries it.
        """
        nachrichten = state.world_briefing.messages if state.world_briefing else None
        if nachrichten is None:
            return
        welt = [r for r in nachrichten.rooms if r.room == "world"]
        neu = [(m, " (world)") for r in welt for m in r.recent]
        neu += [(m, " → you") for m in nachrichten.inbox]
        for nachricht, an_mich in sorted(neu, key=lambda paar: paar[0].at):
            if not nachricht.id or nachricht.id in self._talk_seen:
                continue
            self._talk_seen.append(nachricht.id)
            wer = f"{nachricht.from_name or 'an agent'}{an_mich}"
            farbe = self._theme.pos if an_mich else self._theme.data
            self._add_entry("arena", _Eintrag(wer, nachricht.text, farbe))

    def _log_arena_voices(self, stimmen: list[dict]) -> None:
        """New arena chat messages into their tab; the same sentence from many is one line.

        Measured on 11.10.2026: of 2,815 messages in a day, 86 % were two sentences
        ("moving to capture a field — now."). Listed one by one they bury the others.
        """
        gleiche: dict[str, list[dict]] = {}
        for stimme in reversed(stimmen):  # the server sends the newest first
            kennung = str(stimme.get("id")) if isinstance(stimme, dict) else ""
            if not kennung or kennung in self._arena_seen:
                continue
            self._arena_seen.append(kennung)
            gleiche.setdefault(str(stimme.get("body") or ""), []).append(stimme)
        for satz, von in gleiche.items():
            wer = f"{len(von)} agents" if len(von) > 1 else _stimme_name(von[0])
            self._add_entry("arena", _Eintrag(wer, satz, self._theme.data))

    def _own_move(self, action: str, tick: int) -> bool:
        """True, once, for an action this dashboard sent itself.

        The action's answer names neither tick nor event, so name and tick must fit: the
        agent remembers the tick it knew when sending, and the server allows one action per
        tick and agent.
        """
        eigene = self.agent._own_actions
        for eintrag in eigene:
            if eintrag[0] == action and 0 <= tick - eintrag[1] <= 2:
                eigene.remove(eintrag)
                return True
        return False

    def _add_log(self, msg: str) -> None:
        """A line (markup) into the world log; the same line again counts up."""
        _zaehlend_anfuegen(self._log, msg)
        self._mark_new("world")

    def _add_entry(self, tab: str, eintrag: _Eintrag) -> None:
        """An entry into the tournament or the chat log."""
        _eintrag_anfuegen(self._tab_log(tab), eintrag)
        self._mark_new(tab)

    def _mark_new(self, tab: str) -> None:
        if tab != self._log_tab:
            self._log_new.add(tab)

    def _log_title(self) -> str:
        """The frame line of the log: its three tabs, the shown one bold, news marked •."""
        teile = []
        for tab, wort in _LOG_TABS:
            if tab == self._log_tab:
                teile.append(f"[bold]{wort}[/bold]")
            else:
                teile.append(f"[dim]{wort}{' •' if tab in self._log_new else ''}[/dim]")
        kopf = "LOG ▶" if self._focus == "log" else "LOG"
        # A bar between the tabs, not a dot: next to the • for news a dot read as a colon.
        return f"{kopf}  " + " [dim]│[/dim] ".join(teile)

    def _log_lines(self, tab: str, breite: int | None = None) -> list[str]:
        """A tab's lines as markup; ``breite`` cuts long entries for the panel."""
        if tab == "world":
            return list(self._log)
        return [_eintrag_zeile(eintrag, breite) for eintrag in self._tab_log(tab)]

    def _tab_log(self, tab: str) -> list[_Eintrag]:
        """The entries behind a tab other than the world log."""
        return self._log_tournament if tab == "tournament" else self._log_arena

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
        self._add_log(_c(self._theme.data, f"⠋ trying again: {_roh(pending.display)}…"))
        try:
            if pending.kind == "compass":
                result = await self.agent.set_compass(pending.action)
                if result.get("error"):
                    self._add_log(_c(self._theme.warn, f"✗ {_roh(pending.display)}: failed"))
                    self._set_feedback(_c(self._theme.warn, f"✗ {_roh(pending.display)} failed"))
                else:
                    self._compass_preset = pending.action
                    self._compass_ever_set = True
                    self._add_log(_c(self._theme.pos, f"✓ {_roh(pending.display)}"))
                    self._set_feedback(_c(self._theme.pos, f"✓ Compass → {_roh(pending.display)}"))
            else:
                r = await self.agent.act(pending.action, **pending.params)
                cs = _cost_str(_action_cost(r)) if r.success else ""
                color = self._theme.pos if r.success else self._theme.warn
                msg = r.error_message or "failed"
                label = (
                    f"✓ done: {_roh(pending.display)}{cs}"
                    if r.success
                    else f"✗ couldn't {_roh(pending.display)}: {_roh(msg)}"
                )
                self._add_log(_c(color, label))
                self._set_feedback(_c(color, label))
        except RateLimitError as exc:
            # Re-queue with another timer — tick boundary not yet reached
            self._schedule_pending(pending, retry_after=exc.retry_after)
            wait = int(exc.retry_after)
            self._add_log(
                _c("dim", f"⏳ {_roh(pending.display)}: still rate limited, retry ~{wait}s")
            )
        except CosmergonError as exc:
            self._add_log(_c(self._theme.warn, f"✗ couldn't {_roh(pending.display)}: {_roh(exc)}"))
            self._set_feedback(_c(self._theme.warn, f"✗ {_roh(pending.display)} failed"))

    # --- Redraw ---

    def _panel_breite(self, widget_id: str) -> int:
        """Usable columns of a panel; 32 before the first layout (the old fixed width)."""
        breite = self.query_one(f"#{widget_id}", Static).content_region.width
        return breite if breite > 0 else 32

    def _panel_hoehe(self, widget_id: str) -> int:
        """Usable rows of a panel; 12 before the first layout (top row 14 minus the frame)."""
        hoehe = self.query_one(f"#{widget_id}", Static).content_region.height
        return hoehe if hoehe > 0 else 12

    def _update_panel(self, widget_id: str, content: str) -> None:
        """Call Static.update() only when content changed — prevents unnecessary repaints."""
        if self._panel_cache.get(widget_id) != content:
            self._panel_cache[widget_id] = content
            self.query_one(f"#{widget_id}", Static).update(content)

    _FOCUS_TO_PANEL: ClassVar[dict[str, str]] = {
        "agent": "agent-panel",
        "fields": "agent-panel",
        "log": "log-panel",
    }

    _PANEL_TITLE: ClassVar[dict[str, str]] = {"agent-panel": "AGENT", "log-panel": "LOG"}

    def _sync_focus_border(self) -> None:
        target = self._FOCUS_TO_PANEL.get(self._focus or "")
        if target == self._focus_panel_id:
            return
        # Frame and a ▶ in the title: the marker carries the focus where color cannot.
        if self._focus_panel_id:
            alt = self.query_one(f"#{self._focus_panel_id}", Static)
            alt.remove_class("panel-focused")
            alt.border_title = self._PANEL_TITLE[self._focus_panel_id]
        if target:
            neu = self.query_one(f"#{target}", Static)
            neu.add_class("panel-focused")
            neu.border_title = f"{self._PANEL_TITLE[target]} ▶"
        self._focus_panel_id = target
        # The log's title carries its tabs; _draw_log_panel writes it (with the ▶).
        self._panel_cache.pop("log-title", None)

    def _redraw(self) -> None:
        # Textual stops the app (`is_running` False) before it prunes the screens and
        # only later cancels this interval — a tick in between found the panels gone.
        if not self.is_running:
            return
        state = self.agent.state
        self._sync_focus_border()
        self._draw_hint_bar(state)
        self._draw_agent_panel(state)
        self._draw_economy_panel(state)
        self._draw_log_panel(state)
        self._draw_context_bar(state)
        self._draw_fix_bar(state)
        self._draw_status_bar(state)

    def _draw_agent_panel(self, state: GameState | None) -> None:
        t = self._theme
        lines: list[str] = []

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
            lines.append(_c(t.data, _rang_zeile(state.ranking, self._panel_breite("agent-panel"))))
        # What the main-world Marauder does, while a mission runs (#468) — in the place of
        # the blank line, the panel has a fixed height.
        lines.append(
            _c(t.data, f"Marauder: {_mission_zeile(self._mission)}") if self._mission else ""
        )

        # Compass — CTA lives in hint-bar, agent panel shows current state only
        compass_label = _COMPASS_DISPLAY.get(self._compass_preset, self._compass_preset)
        compass_val = compass_label if self._compass_ever_set else "—"
        lines.append(_c(t.data, f"Compass: {compass_val}"))
        # In a tournament the player has two compasses. The panel names both: the main-world
        # one alone was read as the tournament's ("compass grow — what happens?", #468), and
        # a narrow terminal drops the tournament compass from the top line. Its line takes the
        # place of the blank one before FIELDS — the panel has a fixed height.
        turnier_kompass = (state.tournament or {}).get("compass")
        if turnier_kompass:
            lines.append(_c(t.data, f"  in tournament: {_roh(turnier_kompass)}"))

        # Fields
        if state.fields:
            if not turnier_kompass:
                lines.append("")
            lines.append(_c(t.struct, "[bold]═ FIELDS[/bold]"))
            lines += self._feld_zeilen(state.fields, self._panel_hoehe("agent-panel") - len(lines))

        self._update_panel("agent-panel", "\n".join(lines))

    def _feld_zeilen(self, fields: list[Field], zeilen_frei: int) -> list[str]:
        """As many field lines as fit, then "+N more" — like htop's list at the frame (#508)."""
        t = self._theme
        narrow = self.app.size.width < 70
        zeigen = _felder_die_passen(len(fields), zeilen_frei)
        lines: list[str] = []
        for f in fields[:zeigen]:
            tier = f"T{f.entity_tier or 0}"
            etype = f.entity_type or "novice"  # the frame ends a long line in "…" (#508)
            ncells = f.active_cell_count
            if narrow:
                zeile = f"  {_roh(f.id[:8])} {_roh(tier)} {_roh(etype)} {ncells} cells"
                lines.append(_c(t.data, zeile))
            else:
                bar_f = _energy_bar(ncells, 200, 6)
                zeile = f"  {_roh(f.id[:8])} {_roh(tier)} {_roh(etype):8s} {bar_f} {ncells} cells"
                lines.append(_c(t.data, zeile))
        if len(fields) > zeigen:
            lines.append(_c("dim", f"  ... (+{len(fields) - zeigen} more)  \\[V] to browse"))
        return lines

    def _draw_economy_panel(self, state: GameState | None) -> None:
        t = self._theme
        lines: list[str] = []

        if state and state.world_briefing:
            wb = state.world_briefing
            lines.append(_c(t.data, f"Rank:   {_platz(state.ranking, wb)}"))
            breite = self._panel_breite("economy-panel") - len("Top:    ")
            if wb.top_agent:
                lines.append(_c(t.data, f"Top:    {_roh(_name_vor_zahl(wb.top_agent, breite))}"))
            # The frame ends a long line in "…" (CSS, #508) — no cut in the middle of a word here.
            lines.append(_c(t.data, f"Market: {_roh(wb.market_summary)}"))
            if wb.last_event:
                lines.append(_c("dim", f"Last:   {_roh(wb.last_event)}"))
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

        # Tier progress — always visible when state is available
        if state:
            lines.append("")
            lines.extend(_tier_progress_lines(state, t))

        self._update_panel("economy-panel", "\n".join(lines))

    def _draw_log_panel(self, state: GameState | None) -> None:
        t = self._theme
        panel = self.query_one("#log-panel", Static)
        titel = self._log_title()
        if self._panel_cache.get("log-title") != titel:
            self._panel_cache["log-title"] = titel
            panel.border_title = titel

        # The rows are measured: counted by hand from the neighbours' heights they came out
        # too many, and the panel cuts at the bottom — a full log lost its newest lines
        # (#475). Before the first layout the size is 0; then: hint 1 + top 14 + ctx 1 +
        # fix 4 + status 1 + the panel's own frame 2 = 23. Every row is the log's: the chat
        # has its own key and no rows here (it took three of six on the founder's device).
        panel_h = panel.content_size.height or max(4, self.app.size.height - 23)
        breite = panel.content_size.width or max(20, self.app.size.width - 4)
        lines: list[str] = []
        if self._log_tab == "world":
            # Learned rules — show last 2 (compact)
            learned = (state.learned_rules if state else None) or []
            lines += [_c("dim", f"  • {_roh(rule)}") for rule in learned[-2:]]
        alle = self._log_lines(self._log_tab, breite)
        ende = len(alle) - min(self._log_scroll, max(0, len(alle) - 1))  # ↑/↓ with log focus
        feed = alle[max(0, ende - max(1, panel_h - len(lines))) : ende]
        if feed:
            lines.extend(feed)
        elif self._log_tab == "world":
            # Empty feed — structured welcome block instead of black void.
            # Fills available space with context until real events arrive.
            welcome: list[str] = [
                _c("dim", "Connecting to cosmergon.com..."),
                "",
                _c(t.struct, "  Conway cells evolve every tick."),
                _c("dim", "  Energy is earned through active patterns."),
                _c("dim", "  Your agent acts when you act — here or in your code."),
                "",
            ]
            # #468: the same keys as the bar — without a field, F/P/V were promised here too.
            welcome += [_c(t.guide, zeile) for zeile in _welcome_keys(state)]
            welcome.append("")
            welcome.append(_c("dim", "  Activity will appear here once connected."))
            lines.extend(welcome[: max(1, panel_h - len(lines))])
        else:
            lines.append(_c("dim", _leerer_reiter(self._log_tab, state)))

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
                zeigen = fields[:5]  # kuerzen-ok: keys 1-5
                parts = [f"{_c(t.cmd, _hk(str(i + 1)))}{f.id[:8]}" for i, f in enumerate(zeigen)]
                self._update_panel("context-bar", _c("dim", "  ".join(parts)) + esc)
            else:
                self._update_panel("context-bar", _c("dim", "No fields yet") + esc)
        elif self._focus == "log":
            zurueck = f"  (-{self._log_scroll})" if self._log_scroll else ""
            # `esc` like the other panels: written as plain "[Esc]" the markup swallowed it
            # and the line read "Scroll   back" (seen on the device, 12.10.2026).
            self._update_panel("context-bar", _c("dim", f"[↑/↓] Scroll{zurueck}") + esc)
        else:
            self._update_panel("context-bar", "")

    def _draw_fix_bar(self, state: GameState | None = None) -> None:
        t = self._theme

        def k(key: str, label: str, color: str | None = None) -> str:
            return f"{_c(color or t.cmd, _hk(key))} {label}"

        # [C] orange until first compass use — onboarding signal
        c_color = t.guide if not self._compass_ever_set else t.cmd
        keys = [
            k(key, label, c_color if key == "C" else None)
            for key, label in _fix_bar_keys(
                state,
                agents=bool(load_token()),
                advisor=bool(self._advisor),
                key_in_status=self._key_hint_in_status(state),
            )
        ]
        self._update_panel("fix-bar", "  ".join(keys))

    def _get_plain_key(self) -> str:
        """Return the unmasked API key (for KeyModal display)."""
        return self.agent._api_key.raw if self.agent._api_key else ""

    def _key_hint_in_status(self, state: GameState | None) -> bool:
        """Whether ``[K] Key`` fits at the end of the status bar (else the bottom bar keeps it).

        The bar is one line and is cut on the right; the hint is its last entry. Counted with
        the longer ``[U]`` word, so the answer does not flip with that word's choice.
        """
        name = (state.agent_name if state and state.agent_name else None) or (
            (self.agent.agent_id or "?")[:8]
        )
        tier = (state.subscription_tier if state else None) or "free"
        text = " │ ".join([name, tier, "[K] Key"])
        ends = _key_ends(state)
        if ends and ends[1]:
            text = f"⚠ key ends {ends[0]} unless used · [U] Keep agent │ " + text
        elif ends:
            text = f"key kept until {ends[0]} │ " + text
        return bool(len(text) <= self.size.width - 2)  # the bar's padding: 0 1

    def _agent_name_or_id(self) -> str:
        """The agent's name for a log line; the start of its id while the state has none."""
        state = self.agent.state
        return (state.agent_name if state and state.agent_name else None) or (
            (self.agent.agent_id or "?")[:8]
        )

    def _draw_status_bar(self, state: GameState | None) -> None:
        name = (state.agent_name if state and state.agent_name else None) or (
            (self.agent.agent_id or "?")[:8]
        )
        sep = " │ "
        # subscription tier in status bar (tick already shown in hint-bar — no duplicate)
        tier = (state.subscription_tier if state else None) or "free"
        line = f"[dim]{sep.join([_roh(name), _roh(tier)])}[/dim]"
        # [K] Key stands where the shortened key used to stand (founder on the device,
        # 12.10.2026): the key itself is shown by [K], its first characters said nothing.
        # Where the line has no room for it, [K] stays the last entry of the bottom bar.
        if self._key_hint_in_status(state):
            line += f"[dim]{sep}[/dim]" + _c(self._theme.cmd, _hk("K")) + "[dim] Key[/dim]"
        # #468: when the agent is lost comes FIRST, so a narrow terminal cuts the rest, not it.
        # What to do about it stands right behind the warning, not among the keys.
        ends = _key_ends(state)
        if ends and ends[1]:
            warn = self._theme.warn
            warning = f"⚠ key ends {_roh(ends[0])} unless used · "
            word = _keep_word(len(warning), self.size.width - 2)  # the bar's padding: 0 1
            keep = _c(self._theme.cmd, _hk("U")) + _c(warn, " " + word)
            line = _c(warn, warning) + keep + sep + line
        elif ends:
            line = _c("dim", f"key kept until {_roh(ends[0])}") + sep + line
        self._update_panel("status-bar", line)

    def _set_feedback(self, msg: str, duration: float = 4.0, at_tick: bool | None = None) -> None:
        """Show a timed message in the hint bar (line 1 only).

        ``at_tick`` adds "takes effect at next tick". By default only a confirmation (✓)
        gets it: a cancel or an error changes nothing, and saying it "takes effect"
        was wrong (#468).
        """
        self._feedback = msg
        self._feedback_at_tick = ("✓" in msg) if at_tick is None else at_tick
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
        if self._auth_error and not state:
            return _c(t.warn, self._auth_error)

        # 1. Active feedback — show confirmation + countdown so user knows *when* it fires.
        if self._feedback and time.monotonic() < self._feedback_until:
            if not self._feedback_at_tick:
                return self._feedback
            state = self.agent.state
            if state and state.next_tick_at:
                remaining = state.next_tick_at - time.time()
                if remaining > 1.0:
                    suffix = _c("dim", f"takes effect at next tick ~{int(remaining)}s")
                else:
                    suffix = _c("dim", "takes effect at next tick soon")
                return f"{self._feedback}  ·  {suffix}"
            if self._tick_received_at > 0:
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
        if self._paused:
            resume_key = _c(t.cmd, _hk("X"))
            return f"{_c(t.warn, '⏸ Paused')} · {resume_key} resume" + self._countdown_suffix()

        # 3a. The player's tournament (#468) — it is the game they joined; its line comes
        # before the server's advice, which would name the same tournament in API words.
        if state.tournament:
            return self._turnier_hinweis(state.tournament) + self._countdown_suffix()

        # 3b. The server's advice (#468) — only while a prerequisite is missing (S293);
        # after that the next move is the player's own choice and nothing is announced.
        if state.next_step:
            return self._next_step_hint(state.next_step) + self._countdown_suffix()

        # 4. Compass never set → one thing to do
        if not self._compass_ever_set:
            return (
                f"{_c(t.guide, '→')} {_c(t.cmd, _hk('C'))} "
                f"{_c(t.guide, 'Set Compass direction')} — choose how the agent plays"
                + self._countdown_suffix()
            )

        # 5. No fields yet — only if the server offers a field (the main world can be full)
        if not state.fields and (state.available_actions.get("create_field") or {}).get(
            "available"
        ):
            return (
                f"{_c(t.guide, '→')} {_c(t.cmd, _hk('F'))} "
                f"{_c(t.guide, 'Create a field')} — choose a cube for your agent"
                + self._countdown_suffix()
            )

        # 6. Fields exist but no cells placed (any([]) is False — without a field there is
        # nothing to place on, #468)
        if state.fields and not any(f.active_cell_count > 0 for f in state.fields):
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
        self._log_scroll = 0

    def on_key(self, event: Any) -> None:
        """The keys of the focused panel: what its context bar promises (#484).

        AGENT: 1-7 set the compass. FIELDS: 1-5 open that field. LOG: ↑/↓ (PgUp/PgDn) scroll
        back and forth. Esc leaves the focus. Until 0.27.5 only the first of these did
        anything; the bar showed the others all the same.
        """
        if self._focus is None or len(self.screen_stack) > 1:  # a dialog has its own keys
            return
        key = event.key
        if key == "escape":
            self._focus = None
            self._log_scroll = 0
        elif self._focus == "agent" and key in ("1", "2", "3", "4", "5", "6", "7"):
            self._apply_compass_preset(_COMPASS_PRESETS[int(key) - 1])
        elif self._focus == "fields" and key in ("1", "2", "3", "4", "5"):
            fields = (self.agent.state.fields if self.agent.state else None) or []
            if int(key) > len(fields):
                return
            self._open_field(int(key) - 1)
        elif self._focus == "log" and key in ("up", "down", "pageup", "pagedown"):
            step = {"up": 1, "down": -1, "pageup": 5, "pagedown": -5}[key]
            most = max(0, len(self._log_lines(self._log_tab)) - 1)
            self._log_scroll = max(0, min(most, self._log_scroll + step))
        else:
            return
        event.prevent_default()
        self._redraw()

    @work
    async def _open_field(self, idx: int) -> None:
        """The Field View, opened on one field (FIELDS focus, number key)."""
        state = self.agent.state
        if state and state.fields:
            await self.push_screen_wait(
                FieldScreen(self.agent, list(state.fields), self._theme, initial_idx=idx)
            )

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
                self._add_log(_c(self._theme.warn, f"✗ compass failed: {_roh(result['error'])}"))
                self._set_feedback(_c(self._theme.warn, "✗ Compass failed"))
            else:
                self._compass_preset = preset
                self._compass_ever_set = True
                self._add_log(_c(self._theme.pos, f"✓ compass: {compass_label}"))
                explanation = (result.get("explanation") or "").strip()
                if explanation:
                    self._add_log(_c("dim", f"  {_roh(_truncate_words(explanation, 48))}"))
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
            self._add_log(_c(self._theme.warn, f"✗ compass failed: {_roh(exc)}"))
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
        # #484: two steps, shown as such, each with what it is for.
        fi = await self.push_screen_wait(
            SelectModal(
                "Place a pattern",
                field_labels,
                body=_PLACE_FIELD_TEXT,
                tall=True,
                steps=(1, _PLACE_STEPS),
            )
        )
        if fi is None:
            return
        costs = ((await self._game_actions()).get("place_cells") or {}).get("preset_costs") or {}
        pattern_labels = [
            f"{name} — {_num(costs[name])} energy" if name in costs else name for name in _PRESETS
        ]
        pi = await self.push_screen_wait(
            SelectModal(
                "Place a pattern",
                pattern_labels,
                body=_PLACE_PATTERN_TEXT,
                tall=True,
                steps=(2, _PLACE_STEPS),
            )
        )
        if pi is None:
            return
        try:
            r = await self.agent.act(
                "place_cells", field_id=state.fields[fi].id, preset=_PRESETS[pi]
            )
            icon, color = ("✓", self._theme.pos) if r.success else ("✗", self._theme.warn)
            cs = _cost_str(_action_cost(r))
            ziel = f"a {_PRESETS[pi]} on {state.fields[fi].id[:8]}"
            zeile = (
                f"✓ you placed {ziel}{cs}"
                if r.success
                else f"✗ couldn't place {ziel}: {r.error_message or 'refused'}"
            )
            self._add_log(_c(color, _roh(zeile)))
            label = f"{icon} Cells placed ({_PRESETS[pi]}){cs} — evolves next tick"
            self._set_feedback(_c(color, label))
        except RateLimitError as exc:
            display = f"place a {_PRESETS[pi]} on {state.fields[fi].id[:8]}"
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
            self._add_log(_c("dim", f"⏳ {_roh(display)} — queued, fires next tick{wait_str}"))
            self._set_feedback(_c("dim", f"⏳ Queued: {_roh(display)} — next tick{wait_str}"))
        except CosmergonError as exc:
            self._add_log(_c(self._theme.warn, f"✗ couldn't place cells: {_roh(exc)}"))
            self._set_feedback(_c(self._theme.warn, f"✗ Place cells failed: {_roh(exc)}"))

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
            zeile = (
                f"✓ you created a field in cube {cubes[ci].name}{cs}"
                if r.success
                else f"✗ couldn't create a field: {r.error_message or 'refused'}"
            )
            self._add_log(_c(color, _roh(zeile)))
            self._set_feedback(_c(color, f"{icon} Field created{cs} — press \\[P] to place cells"))
        except RateLimitError as exc:
            wait_str = f" ~{int(exc.retry_after)}s" if exc.retry_after > 1 else ""
            self._schedule_pending(
                _PendingAction(
                    kind="act",
                    action="create_field",
                    params={"cube_id": cubes[ci].id},
                    display="create a field",
                ),
                retry_after=exc.retry_after,
            )
            self._add_log(_c("dim", f"⏳ create a field — queued, fires next tick{wait_str}"))
            self._set_feedback(_c("dim", f"⏳ Queued: create a field — next tick{wait_str}"))
        except CosmergonError as exc:
            self._add_log(_c(self._theme.warn, f"✗ couldn't create a field: {_roh(exc)}"))
            self._set_feedback(_c(self._theme.warn, f"✗ Field creation failed: {_roh(exc)}"))

    @work
    async def action_evolve(self) -> None:
        state = self.agent.state
        if not state or not state.fields:
            self._add_log(_c(self._theme.warn, "No fields to evolve"))
            return
        # #484: only fields the server reports as ready can be chosen; every other field is
        # listed with what it lacks. A server that says nothing about evolve (older backend)
        # leaves the choice open, as before.
        info = (await self._game_actions()).get("evolve") or {}
        meldung = (state.available_actions or {}).get("evolve")
        if isinstance(meldung, dict) and "eligible_fields" in meldung:
            reif = {str(i) for i in meldung.get("eligible_fields") or []}
            ready = [f for f in state.fields if f.id in reif]
        else:
            ready = list(state.fields)
        kosten = info.get("costs_per_tier") or {}
        evolve_labels = []
        for f in ready:
            tier = f.entity_tier or 0
            preis = kosten.get(str(tier))
            preis_text = f"  ({_num(preis)} energy)" if preis is not None else ""
            evolve_labels.append(f"{f.id[:8]}  T{tier} → T{tier + 1}{preis_text}")
        fi = await self.push_screen_wait(
            SelectModal(
                "Evolve a field",
                evolve_labels,
                body=_evolve_body(list(state.fields), ready, info, state.energy),
                tall=True,
            )
        )
        if fi is None:
            return
        try:
            r = await self.agent.act("evolve", field_id=ready[fi].id)
            icon, color = ("✓", self._theme.pos) if r.success else ("✗", self._theme.warn)
            if r.success:
                new_tier = (r.data.get("result") or {}).get("new_tier")
                cs = _cost_str(_action_cost(r))
                tier_str = f" to T{_roh(new_tier)}" if new_tier else ""
                self._add_log(_c(color, f"✓ you evolved {_roh(ready[fi].id[:8])}{tier_str}{cs}"))
                label = f"T{_roh(new_tier)}" if new_tier else "ok"
                self._set_feedback(_c(color, f"{icon} Evolved: {label}{cs}"))
            else:
                msg = r.error_message or "failed"
                feld = _roh(ready[fi].id[:8])
                self._add_log(_c(color, f"✗ couldn't evolve {feld}: {_roh(msg)}"))
                self._set_feedback(_c(color, f"{icon} Evolve: {_roh(msg)}"))
        except RateLimitError as exc:
            was = f"evolve {ready[fi].id[:8]}"
            wait_str = f" ~{int(exc.retry_after)}s" if exc.retry_after > 1 else ""
            self._schedule_pending(
                _PendingAction(
                    kind="act",
                    action="evolve",
                    params={"field_id": ready[fi].id},
                    display=was,
                ),
                retry_after=exc.retry_after,
            )
            self._add_log(_c("dim", f"⏳ {_roh(was)} — queued, fires next tick{wait_str}"))
            self._set_feedback(_c("dim", f"⏳ Queued: {_roh(was)} — next tick{wait_str}"))
        except CosmergonError as exc:
            self._add_log(_c(self._theme.warn, f"✗ couldn't evolve: {_roh(exc)}"))
            self._set_feedback(_c(self._theme.warn, f"✗ Evolve failed: {_roh(exc)}"))

    @work
    async def action_leave(self) -> None:
        """[Q] — quit; while the agent's key ends unless used, warn first (#468).

        Once this dashboard is closed, nothing of ours renews the key. The warning names the
        time, what happens then and how to avoid it: [1] quit · [2] keep this agent · Esc stay.
        A second Q under the warning quits — the player has read it.

        Under any other open window Q closes that window, as its footer says ("Esc or Q to
        close"): Q is an App key with priority and fires before the window — until 0.24.0 it
        quit the whole dashboard from the log, the help or the field view (found on the device).
        """
        if isinstance(self.screen, ModalScreen) and not self._quit_warning_open:
            self.screen.dismiss(None)
            return
        ends = _key_ends(self.agent._state)
        if ends is None or not ends[1] or self._quit_warning_open:
            self.exit()
            return
        self._quit_warning_open = True
        try:
            choice = await self.push_screen_wait(
                SelectModal(
                    _QUIT_TITLE.format(when=ends[0]),
                    ["Quit", "Keep this agent (key persistence, paid)"],
                    body=_quit_warning(ends[0]),
                    tall=True,
                )
            )
        finally:
            self._quit_warning_open = False
        if choice == 0:
            self.exit()
        elif choice == 1:
            await self._keep_or_upgrade(keep=True, plans=False)

    @work
    async def action_upgrade(self) -> None:
        """[U] — keep this agent or upgrade. Runs as a worker: the dialog waits for an answer,
        and without one Textual raises NoActiveWorker — the key crashed the dashboard for
        every anonymous free agent (found on the device, cos20 #468).

        While the key ends unless used, keeping the agent comes first (#468); an anonymous
        free agent also sees the plans. Everyone else goes to the website as before.
        """
        state = self.agent._state
        tier = state.subscription_tier if state else "free"
        agent_type = state.agent_type if state else ""
        ends = _key_ends(state)
        keep = bool(ends and ends[1])
        plans = agent_type == "anonymous_agent" and tier == "free"
        if keep or plans:
            await self._keep_or_upgrade(keep=keep, plans=plans)
            return

        # All other cases → open website (owner manages billing)
        _upgrade_urls: dict[str, str] = {
            "free": "https://cosmergon.com/solo.html",
            "solo": "https://cosmergon.com/developer-plan.html",
            "developer": "https://cosmergon.com/enterprise.html",
        }
        url = _upgrade_urls.get(tier)
        if url is None:
            self._set_feedback(_c(self._theme.data, "✓ You are on the top tier (Enterprise)"))
            return
        webbrowser.open(url)
        self._add_log(_c(self._theme.pos, f"✓ Upgrade page opened ({tier} → next tier)"))
        self._set_feedback(_c(self._theme.pos, "✓ Browser opened — complete upgrade there"))

    async def _keep_or_upgrade(self, *, keep: bool, plans: bool) -> None:
        """Choose: keep this agent (key persistence) and/or a plan (#468, Paket 2.7a).

        No prices and no plan details here — the Stripe checkout names them; a number copied
        into the SDK goes stale without anyone noticing.
        """
        options: list[tuple[str, str, str]] = []
        if keep:
            options += [
                ("Keep this agent — monthly (price shown at checkout)", "persist", "monthly"),
                ("Keep this agent — yearly (price shown at checkout)", "persist", "annual"),
            ]
        if plans:
            options += [
                ("Solo plan (details at checkout)", "plan", "solo"),
                ("Developer plan (details at checkout)", "plan", "developer"),
            ]
        title = "Keep this agent or upgrade" if keep and plans else "Keep this agent"
        if not keep:
            title = "Upgrade"
        body = "\n\n".join(text for on, text in ((keep, _KEEP_BODY), (plans, _PLANS_BODY)) if on)
        choice = await self.push_screen_wait(
            SelectModal(title, [o[0] for o in options], body=body, tall=True)
        )
        if choice is None:
            return  # Esc
        _, kind, value = options[choice]
        if kind == "persist":
            await self._keep_agent(value)
        else:
            await self._upgrade_anonymous_via_api(value)

    async def _keep_agent(self, interval: str) -> None:
        """Open the key-persistence checkout (``POST /api/v1/billing/persist-checkout``, #468).

        The server keeps this agent's newest active key; prices and terms are on the checkout.
        """
        warn = self._theme.warn
        try:
            resp = await self.agent._request(
                "POST", "/api/v1/billing/persist-checkout", params={"interval": interval}
            )
        except CosmergonError as exc:
            self._set_feedback(_c(warn, f"✗ Keep this agent: {_roh(exc)}"))
            return
        body = _json_or_empty(resp)
        url = body.get("checkout_url") if resp.status_code == 200 else None
        if url:
            webbrowser.open(url)
            self._add_log(_c(self._theme.pos, f"✓ Key persistence checkout opened ({interval})"))
            self._set_feedback(
                _c(self._theme.pos, "✓ Checkout opened in your browser — pay there to keep it")
            )
            return
        message = (body.get("error") or {}).get("message") or f"HTTP {resp.status_code}"
        self._set_feedback(_c(warn, f"✗ Keep this agent: {_roh(message)}"), duration=10.0)

    async def _upgrade_anonymous_via_api(self, tier: str) -> None:
        """Paket 2.7a SDK: Direct Stripe checkout for anonymous free agents.

        Panel S112: the player chooses the plan (``_keep_or_upgrade``); robust fallback on
        any error.
        """
        url = f"{self.agent.base_url}/api/v1/billing/create-upgrade-checkout?tier={tier}"
        try:
            async with _http.async_client(timeout=15.0, verify=True) as client:
                resp = await client.post(
                    url,
                    headers={"Authorization": f"api-key {self.agent._api_key.raw}"},
                )
            if resp.status_code == 200:
                checkout_url = resp.json().get("checkout_url", "")
                if checkout_url:
                    webbrowser.open(checkout_url)
                    self._set_feedback(
                        _c(self._theme.pos, "✓ Stripe checkout opened — complete payment there")
                    )
                    return
        except Exception:
            # Fallback below
            logger.debug("_upgrade_anonymous_via_api: failed, falling back", exc_info=True)

        # Fallback: open website (Panel: Engineering — robust degradation)
        webbrowser.open("https://cosmergon.com/solo.html")
        self._set_feedback(_c(self._theme.pos, "✓ Upgrade page opened — complete upgrade there"))

    @work
    async def action_toggle_showcase(self) -> None:
        """Public-Showcase opt-in/opt-out toggle (Hotkey S).

        Konzept: docs/konzepte/konzept-public-showcase-opt-in.md §3.4.
        Lädt aktuellen Zustand + kanonischen Consent-Text, öffnet ShowcaseModal,
        schickt PATCH /players/me bei bestätigter Änderung.
        """
        import uuid as _uuid

        import httpx as _httpx

        base = self.agent.base_url
        headers = {"Authorization": f"api-key {self.agent._api_key.raw}"}

        try:
            async with _http.async_client(timeout=15.0, verify=True) as client:
                consent_resp = await client.get(
                    f"{base}/api/v1/universe/showcase-consent?lang=en",
                    headers=headers,
                )
                me_resp = await client.get(f"{base}/api/v1/players/me", headers=headers)
        except (_httpx.ConnectError, _httpx.TimeoutException) as exc:
            self._set_feedback(_c(self._theme.warn, f"✗ Showcase: connect failed: {_roh(exc)}"))
            return
        except Exception as exc:
            self._set_feedback(_c(self._theme.warn, f"✗ Showcase: {_roh(exc)}"))
            return

        if consent_resp.status_code != 200 or me_resp.status_code != 200:
            msg = f"✗ Showcase: consent {consent_resp.status_code} / me {me_resp.status_code}"
            self._set_feedback(_c(self._theme.warn, msg))
            return

        consent = consent_resp.json()
        me = me_resp.json()
        current_state = bool(me.get("public_showcase", False))

        result = await self.push_screen_wait(
            ShowcaseModal(
                consent_text=consent["text"],
                consent_text_hash=consent["text_hash"],
                current_state=current_state,
            )
        )
        if not result:
            return

        payload = _showcase_payload(result, str(_uuid.uuid4()))
        try:
            async with _http.async_client(timeout=15.0, verify=True) as client:
                r = await client.patch(f"{base}/api/v1/players/me", json=payload, headers=headers)
        except Exception as exc:
            self._set_feedback(_c(self._theme.warn, f"✗ Showcase: PATCH failed: {_roh(exc)}"))
            return

        if r.status_code == 200:
            slug = (r.json() or {}).get("public_slug")
            if result["desired"] and slug:
                msg = f"✓ Showcase enabled — cosmergon.com/universe/a/{_roh(slug)}"
            elif result["desired"]:
                msg = "✓ Showcase enabled (slug pending)"
            else:
                msg = "✓ Showcase disabled — you are private again"
            self._set_feedback(_c(self._theme.pos, msg))
        elif r.status_code == 409:
            self._set_feedback(
                _c(self._theme.warn, "✗ Showcase: consent text changed — re-open and confirm")
            )
        else:
            detail = ""
            try:
                detail = (r.json() or {}).get("detail", "")
            except Exception:
                logger.debug("action_toggle_showcase: failed, falling back", exc_info=True)
            self._set_feedback(_c(self._theme.warn, f"✗ Showcase {r.status_code}: {_roh(detail)}"))

    def _next_step(self) -> dict | None:
        """The server's current advice, or ``None`` when nothing blocks the agent (#468)."""
        state = self.agent.state
        return state.next_step if state else None

    def _next_step_hint(self, schritt: dict) -> str:
        """Hint-bar line from the server's advice; [T] when it is a free tournament slot."""
        t = self._theme
        hier, weiter = _roh(schritt.get("you_are_here", "")), _roh(schritt.get("next", ""))
        text = f"{_c(t.guide, '→')} {hier} — {weiter}"
        if _turnier_id(schritt):
            text += f" · {_c(t.cmd, _hk('T'))} join"
        return text

    @work
    async def action_join_tournament(self) -> None:
        """[T] — join the tournament the server offers, or change your tournament compass.

        Joining asks for the compass in the same step: with it the server plays the
        player's Marauders, also while the terminal is closed (#468). Without the compass a
        dashboard player could join a tournament and then do nothing in it.
        """
        state = self.agent.state
        if state and state.tournament:
            await self._turnier_kompass_waehlen(state.tournament)
            return
        tournament_id = _turnier_id(self._next_step())
        if not tournament_id:
            self._set_feedback(_c("dim", "No free tournament slot offered right now"))
            return
        eintrag = await self._turnier_eintrag(tournament_id)
        nummer = eintrag.get("number")
        titel = f"Tournament #{nummer} — a free slot" if nummer else "Tournament — a free slot"
        body = _turnier_erklaerung(eintrag) if eintrag else _TURNIER_OHNE_AUSHANG
        optionen = [f"Join — {name}: {wirkung}" for name, wirkung in _TURNIER_KOMPASSE]
        wahl = await self.push_screen_wait(SelectModal(titel, optionen, body=body, tall=True))
        if wahl is None:
            self._set_feedback(_c("dim", "Not joined — nothing changed"))
            return
        await self._turnier_beitreten(tournament_id, _TURNIER_KOMPASSE[wahl][0])

    async def _turnier_eintrag(self, tournament_id: str) -> dict:
        """The server's posting of THIS tournament (``GET /tournaments/open``), or ``{}``.

        Not ``/tournaments/current``: that is the most recently scheduled one, which need not
        be the one the server offers this player.
        """
        try:
            offene = await self.agent.tournaments_open()
        except (CosmergonError, RuntimeError) as exc:
            self._add_log(_c("dim", f"tournament posting unavailable: {_roh(exc)}"))
            return {}
        return next((t for t in offene if str(t.get("id")) == tournament_id), {})

    async def _turnier_beitreten(self, tournament_id: str, kompass: str) -> None:
        """Register, then set the tournament compass — both reported, neither hidden."""
        warn = self._theme.warn
        try:
            antwort = await self.agent.register_tournament(tournament_id)
        except CosmergonError as exc:
            self._set_feedback(_c(warn, f"✗ Tournament: {_roh(exc)}"))
            return
        if "error" in antwort:
            self._add_log(_c(warn, "✗ tournament registration refused"))
            self._set_feedback(_c(warn, f"✗ {_roh(antwort['error'])}"))
            return
        self._add_log(_c(self._theme.pos, "✓ registered for the tournament"))
        await self._turnier_kompass_setzen(kompass)

    async def _turnier_kompass_setzen(self, kompass: str) -> None:
        """Set the tournament compass and say what it does — or why it did not."""
        warn = self._theme.warn
        nochmal = f"press {_hk('T')} to try again"
        try:
            r = await self.agent.set_tournament_compass(kompass)
        except RateLimitError as exc:
            # One action per tick and agent (server). Right after joining — or while another
            # program plays the same agent, e.g. Shikigon's brain — the tick is often taken.
            # Queue it for the next tick, like the main compass (#468, found on the device).
            self._schedule_pending(
                _PendingAction(
                    kind="act",
                    action="set_tournament_compass",
                    params={"compass_preset": kompass},
                    display=f"tournament compass {kompass}",
                ),
                retry_after=exc.retry_after,
            )
            self._set_feedback(
                _c("dim", f"⏳ Tournament compass {kompass} — set at the next tick"),
                at_tick=False,
                duration=10.0,
            )
            return
        except CosmergonError as exc:
            self._set_feedback(
                _c(warn, f"✗ Compass not set: {_roh(exc)} — {nochmal}"), duration=10.0
            )
            return
        if not r.success:
            fehler = r.error_message or "refused"
            self._set_feedback(
                _c(warn, f"✗ Compass not set: {_roh(fehler)} — {nochmal}"), duration=10.0
            )
            return
        self._add_log(_c(self._theme.pos, f"✓ tournament compass: {kompass}"))
        self._set_feedback(
            _c(self._theme.pos, f"✓ Tournament compass {kompass} — your Marauders play by it"),
            at_tick=False,
        )

    async def _turnier_kompass_waehlen(self, brief: dict) -> None:
        """[T] while registered: show the tournament and choose its compass."""
        jetzt = brief.get("compass")
        if jetzt:
            satz = (
                f"Your Marauders play by the compass {jetzt}, also while this terminal is closed."
            )
        else:
            satz = (
                "No compass yet — choose one, then your Marauders play by themselves, "
                "also while this terminal is closed."
            )
        # Running: exact scores and each Marauder, then when it ends — the title names the
        # tournament, so the text shows all of it at 108x27. With attack, what attack does
        # takes the place of the closing sentence (two lines, not five).
        bild = _turnier_bild(brief)
        lage = _eroberung_lage(self.agent.state)
        angriff = _turnier_angriff(brief, lage["bomben"] if lage else None)
        ende = f"Ends {_wann(brief.get('ends_at'))}."
        if bild and angriff:
            body = f"{bild}\n\n{angriff} {ende}"
        elif bild:
            body = f"{bild}\n\n{ende} {satz} Esc keeps it as it is."
        else:
            body = f"{_turnier_zeile(brief)}\n\n{satz} Esc keeps it as it is."
        optionen = [f"{name} — {wirkung}" for name, wirkung in _TURNIER_KOMPASSE]
        titel = f"Tournament #{brief.get('number', '?')} — your compass"
        wahl = await self.push_screen_wait(SelectModal(titel, optionen, body=body, tall=True))
        if wahl is None:
            self._set_feedback(_c("dim", "Compass unchanged"))
            return
        await self._turnier_kompass_setzen(_TURNIER_KOMPASSE[wahl][0])

    def _turnier_hinweis(self, brief: dict) -> str:
        """Hint-bar line while the player is in a tournament (#468)."""
        t = self._theme
        taste = _c(t.cmd, _hk("T"))
        if not brief.get("compass"):
            zeile = _roh(_turnier_zeile(brief))
            return f"{_c(t.guide, '→')} {zeile} — press {taste} to choose one"
        # Columns left for the line: the bar's padding (2), the flag (2) and the tick
        # countdown behind it (up to 15, "  ·  next ~120s").
        # The line holds the server's words (number, compass): escaped, then the key put in.
        zeile = _roh(_turnier_zeile(brief, breite=self.size.width - 19, schluss="[T] compass"))
        return f"{_c(t.guide, '⚑')} " + zeile.replace("\\[T]", taste)

    @work
    async def action_pause(self) -> None:
        """[X] — pause (only after two confirmations) or resume the agent (#468)."""
        action = "resume" if self._paused else "pause"
        if action == "pause":
            state = getattr(self.agent, "_state", None)
            name = (state.agent_name if state and state.agent_name else None) or "Agent"
            waiting = await self._pause_waiting()
            if not await self.push_screen_wait(PauseConfirmModal(self._theme, name, waiting)):
                self._set_feedback(_c("dim", "Pause cancelled — nothing changed"))
                return
        try:
            r = await self.agent.act(action)
        except CosmergonError as exc:
            self._add_log(_c(self._theme.warn, f"✗ couldn't {action}: {_roh(exc)}"))
            self._set_feedback(_c(self._theme.warn, f"✗ {action} failed: {_roh(exc)}"))
            return
        # Only a confirmed server change flips the local state (a refused resume left the
        # dashboard showing an active agent that was still paused, #468).
        if r.success:
            self._paused = action == "pause"
        icon, color = ("✓", self._theme.pos) if r.success else ("✗", self._theme.warn)
        zeile = (
            f"✓ {action_done_word(action)}"
            if r.success
            else f"✗ couldn't {action}: {r.error_message or 'refused'}"
        )
        self._add_log(_c(color, _roh(zeile)))
        label = "⏸ Agent paused" if action == "pause" else "▶ Agent resumed"
        gesagt = f"{icon} {label}" if r.success else f"✗ {_roh(r.error_message)}"
        self._set_feedback(_c(color, gesagt))

    async def _game_actions(self) -> dict:
        """The server's rules per action, ``GET /api/v1/game/info`` → ``actions`` (#484).

        The dialogs show the server's numbers (prices, thresholds), never a copy kept here.
        Without an answer they show their text without numbers.
        """
        try:
            resp = await self.agent._request("GET", "/api/v1/game/info")
            actions = resp.json().get("actions")
        except Exception:
            logger.debug("_game_actions: no game info from server", exc_info=True)
            return {}
        return actions if isinstance(actions, dict) else {}

    async def _pause_waiting(self) -> str:
        """The server's waiting time before a paused agent can resume, in words (#468).

        Read from ``GET /api/v1/game/info`` → ``actions.pause.resume_cooldown.seconds``;
        a server without that field gets a neutral phrase, never an invented number (the rule
        is in ticks, so its length in minutes depends on the tick rate).
        """
        try:
            resp = await self.agent._request("GET", "/api/v1/game/info")
            seconds = int(resp.json()["actions"]["pause"]["resume_cooldown"]["seconds"])
        except Exception:
            logger.debug("_pause_waiting: no resume_cooldown from server", exc_info=True)
            return "a waiting time the server sets"
        return f"about {max(1, round(seconds / 60))} minutes"

    async def action_refresh_now(self) -> None:
        # Delegate to FieldScreen when it is active — App priority binding fires
        # top-down in Textual 8 so it intercepts before FieldScreen's on_key.
        if isinstance(self.screen, FieldScreen):
            self.screen.action_refresh_field()
            return
        # cos20 #522: a loop that ended (key rejected, client closed) is started again;
        # before, [R] only wrote "Refreshing..." and nothing happened.
        restart = not self.agent._running
        self._add_log(
            _c(self._theme.data, "Refreshing... connecting again" if restart else "Refreshing...")
        )
        if restart:
            self._run_agent()

    @work
    async def action_show_key(self) -> None:
        from cosmergon_agent.config import CONFIG_PATH, load_token

        state = self.agent.state
        name = (state.agent_name if state else None) or (self.agent.agent_id or "?")[:8]
        tier = (state.subscription_tier if state else None) or "free"
        has_stripe = state.has_stripe_customer if state else False
        downgrade = state.subscription_downgrade_at if state else None
        token = load_token()
        result = await self.push_screen_wait(
            KeyModal(
                self._get_plain_key(),
                str(CONFIG_PATH),
                tier,
                name,
                has_stripe_customer=has_stripe,
                downgrade_at=downgrade,
                has_token=bool(token),
                referral_code=state.referral_code if state else "",
            )
        )
        if result == "rotate":
            await self._rotate_master_key(token)

    async def _rotate_master_key(self, token: str) -> None:
        """Paket 4.1: Rotate Master Key via POST /billing/regenerate-token.

        Confirmation → API call → save new token to config.toml → feedback.
        """
        # Confirmation dialog (Panel SEC-10: PFLICHT)
        confirm = await self.push_screen_wait(
            SelectModal(
                "Rotate Master Key?",
                [
                    "Yes — invalidate current key on ALL devices",
                    "Cancel",
                ],
            )
        )
        if confirm != 0:  # 0 = "Yes", anything else = cancel/Esc
            return

        import httpx as _httpx

        url = f"{self.agent.base_url}/api/v1/billing/regenerate-token"
        try:
            async with _http.async_client(timeout=15.0, verify=True) as client:
                resp = await client.post(
                    url,
                    headers={"X-Player-Token": token},
                )
        except (_httpx.ConnectError, _httpx.TimeoutException):
            self._set_feedback("Cannot reach server — try again later.")
            return

        if resp.status_code != 200:
            self._set_feedback(f"Rotation failed ({resp.status_code})")
            return

        new_token = resp.json().get("player_token", "")
        if not new_token:
            self._set_feedback("Rotation failed — no token in response")
            return

        # Save BEFORE showing success (Konzept: crash between save and display = recoverable)
        from cosmergon_agent.config import save_token

        save_token(new_token, base_url=self.agent.base_url)
        self._set_feedback(f"Master Key rotated. New: {new_token[:15]}...")  # kuerzen-ok: masked

    async def _revoke_agent_key(self, agent_data: dict, active_name: str, token: str) -> None:
        """Paket 4.2: Revoke all active API keys for an agent.

        Uses Token-Auth endpoint POST /players/me/agents/{id}/revoke-keys.
        Panel S112: Option A (einstimmig — Security 7/7, Engineering 5/5, DX 5/5).
        Special case: revoking the ACTIVE agent's key disconnects the session.
        """
        name = agent_data.get("agent_name", "?")
        agent_id = agent_data.get("agent_id", "")

        if not agent_id:
            self._set_feedback("Cannot revoke — missing agent info.")
            return

        # Confirmation with special warning for active agent
        is_active = name == active_name
        if is_active:
            options = [
                "Yes — revoke and disconnect this session",
                "Cancel",
            ]
            title = f"Revoke keys for {name} (ACTIVE SESSION)?"
        else:
            options = [
                f"Yes — revoke all keys for {name}",
                "Cancel",
            ]
            title = f"Revoke keys for {name}?"

        confirm = await self.push_screen_wait(SelectModal(title, options))
        if confirm != 0:
            return

        import httpx as _httpx

        url = f"{self.agent.base_url}/api/v1/players/me/agents/{agent_id}/revoke-keys"
        try:
            async with _http.async_client(timeout=15.0, verify=True) as client:
                resp = await client.post(url, headers={"X-Player-Token": token})
        except (_httpx.ConnectError, _httpx.TimeoutException):
            self._set_feedback("Cannot reach server — try again later.")
            return

        if resp.status_code == 200:
            count = resp.json().get("revoked_count", 0)
            self._set_feedback(f"{_roh(count)} key(s) revoked for {_roh(name)}")
            if is_active and count > 0:
                # Reconnect with a fresh key via token (Panel P4-4)
                from cosmergon_agent._token import resolve_token_sync

                try:
                    result = resolve_token_sync(token, base_url=self.agent.base_url)
                    selected = result.selected
                    if selected.api_key:
                        self.agent.reconnect(selected.api_key.raw, selected.agent_id or "")
                        self._set_feedback(f"Keys revoked + reconnected as {_roh(name)}")
                except Exception:
                    self._set_feedback("Keys revoked. Restart dashboard to reconnect.")
        elif resp.status_code == 404:
            self._set_feedback("Agent not found.")
        elif resp.status_code == 403:
            self._set_feedback("Not authorized to manage this agent.")
        else:
            self._set_feedback(f"Revoke failed ({resp.status_code})")

    @work
    async def action_agent_selector(self) -> None:
        """Open agent selector [A] — only if a token is in config."""
        token = load_token()
        if not token:
            return  # no token = Free user, no selector

        # Load agents from config (cached from last token resolution)
        agents_dict = load_all_agents()
        if not agents_dict:
            self._set_feedback("No agents in config. Use --token to connect.")
            return

        state = self.agent.state
        active_name = (state.agent_name if state else None) or ""
        tier = (state.subscription_tier if state else None) or "?"

        agents_list = [
            {
                "agent_name": name,
                "agent_id": entry.agent_id or "",
                "energy": 0,  # not cached in config
                "persona": "",
            }
            for name, entry in agents_dict.items()
        ]

        result = await self.push_screen_wait(AgentSelectorModal(agents_list, active_name, tier))

        if result is None:
            return  # Esc pressed

        if result.get("action") == "new_agent":
            await self._create_agent_via_token(token)
        elif result.get("action") == "revoke":
            agent_data = result["agent"]
            await self._revoke_agent_key(agent_data, active_name, token)
        elif result.get("action") == "select":
            agent_data = result["agent"]
            name = agent_data["agent_name"]
            if name == active_name:
                return  # already active

            # Find key in config
            entry = agents_dict.get(name)
            if entry and entry.api_key:
                raw_key = entry.api_key
                self.agent.reconnect(raw_key, entry.agent_id or "")
                set_active_agent(name)
                # Immediate refresh (Panel decision: no 3s delay)
                self._redraw()
                self._set_feedback(f"Switched to {_roh(name)}")

    async def _create_agent_via_token(self, token: str) -> None:
        """Create a new agent via POST /players/me/agents (Token-Auth)."""
        import httpx as _httpx

        url = f"{self.agent.base_url}/api/v1/players/me/agents"
        try:
            async with _http.async_client(timeout=15.0, verify=True) as client:
                resp = await client.post(
                    url,
                    headers={"X-Player-Token": token},
                    json={},
                )
        except (_httpx.ConnectError, _httpx.TimeoutException):
            self._set_feedback("Cannot reach server — try again later.")
            return

        if resp.status_code == 403:
            self._set_feedback("Agent limit reached for your plan. cosmergon.com/pricing")
            return
        if resp.status_code == 429:
            self._set_feedback("Too many requests — try again later.")
            return
        if resp.status_code != 200:
            self._set_feedback(f"Error ({resp.status_code}): {_roh(resp.text)}")
            return

        data = resp.json()
        new_key = data.get("api_key", "")
        new_name = data.get("agent_name", "?")
        new_id = data.get("agent_id", "")

        if new_key:
            save_agent(new_name, new_key, new_id, base_url=self.agent.base_url)
            self.agent.reconnect(new_key, new_id)
            set_active_agent(new_name)
            self._redraw()
            self._set_feedback(f"Agent created: {_roh(new_name)}")

    @work
    async def action_help(self) -> None:
        await self.push_screen_wait(HelpModal(self._theme.name))

    def action_log_next(self) -> None:
        """Show the log's next tab: world → tournament → chat → world."""
        schirm = self.screen
        if isinstance(schirm, LogScreen):
            schirm.next_tab()  # this binding has priority: it fires under the full screen too
            return
        self._show_log_tab(_naechster_reiter(self._log_tab))

    def _show_log_tab(self, tab: str) -> None:
        self._log_tab = tab
        self._log_new.discard(tab)

    @work
    async def action_log_screen(self) -> None:
        """Open the log full-screen (read-only, scrollable), on the tab the panel shows."""
        if isinstance(self.screen, LogScreen):
            return  # Shift+L under the full screen: it is open already
        tabs = {tab: self._log_lines(tab) or [""] for tab, _wort in _LOG_TABS}
        for tab in ("tournament", "arena"):
            if tabs[tab] == [""]:
                tabs[tab] = [_c("dim", _leerer_reiter(tab, self.agent.state))]
        gewaehlt = await self.push_screen_wait(LogScreen(tabs, self._log_tab, self._theme))
        if gewaehlt in tabs:
            self._show_log_tab(gewaehlt)

    @work
    async def action_chat_screen(self) -> None:
        """Open the talk under \\[M]: with the advisor if one is set, else the server chat."""
        state = self.agent.state
        agent_name = (state.agent_name if state and state.agent_name else None) or "Agent"
        agent_mode = state.agent_mode if state else "api"
        if not self._advisor:
            # Fetched when the talk opens, not every tick: the log no longer shows it.
            try:
                self._messages = await self.agent.get_messages(limit=20)
            except Exception:
                logger.debug("action_chat_screen: failed, falling back", exc_info=True)
        await self.push_screen_wait(
            ChatScreen(
                self.agent,
                list(self._messages),
                self._theme,
                agent_name,
                agent_mode,
                advisor=self._advisor,
            )
        )

    @work
    async def action_field_view(self) -> None:
        """Open full-screen Field View for the agent's Conway fields."""
        state = self.agent.state
        if not state or not state.fields:
            self._add_log(_c(self._theme.warn, "No fields — press \\[F] first"))
            return
        await self.push_screen_wait(FieldScreen(self.agent, list(state.fields), self._theme))


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
        height: auto;
        padding: 0 2;
    }
    LogScreen > #log-wrap > VerticalScroll {
        padding: 0 2 1 2;
    }
    """

    def __init__(self, tabs: dict[str, list[str]], tab: str, theme: Theme) -> None:
        super().__init__()
        self._tabs = tabs  # tab → its lines as markup, whole (long ones wrap here)
        self._tab = tab if tab in tabs else "world"
        self._theme = theme

    def _header(self) -> str:
        """The same tabs as the panel's frame line, then the keys."""
        teile = [
            f"[bold]{wort}[/bold]" if tab == self._tab else f"[dim]{wort}[/dim]"
            for tab, wort in _LOG_TABS
        ]
        keys = "[dim]     L next · ↑ ↓ PgUp PgDn scroll · Esc close[/dim]"
        return " [dim]│[/dim] ".join(teile) + keys

    def compose(self) -> ComposeResult:
        with Vertical(id="log-wrap"):
            yield Label(self._header(), id="log-header")
            with VerticalScroll():
                yield Static("", id="log-body")

    def on_mount(self) -> None:
        self._show()
        self.query_one(VerticalScroll).focus()

    def _show(self) -> None:
        zeilen = [zeile for zeile in self._tabs.get(self._tab, []) if zeile]
        body = "\n".join(zeilen) if zeilen else "[dim]No activity yet.[/dim]"
        self.query_one("#log-header", Label).update(self._header())
        self.query_one("#log-body", Static).update(body)
        self.query_one(VerticalScroll).scroll_end(animate=False)

    _SCROLL_KEYS: ClassVar[set[str]] = {"up", "down", "pageup", "pagedown", "home", "end"}

    def next_tab(self) -> None:
        """L does here what it does in the panel: the next tab."""
        self._tab = _naechster_reiter(self._tab)
        self._show()

    def on_key(self, event: Any) -> None:
        # L never arrives here: the app's binding has priority and fires under a modal too
        # (it calls next_tab). Any other key closes and returns the tab, so the panel shows
        # what the player last looked at.
        if event.key not in self._SCROLL_KEYS and event.key not in ("l", "L"):
            self.dismiss(self._tab)


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
        height: auto;
        padding: 0 2;
    }
    ChatScreen > #chat-wrap > #history-scroll {
        /* Room to breathe (asked for on the device, 13.10.2026): a line above the talk and
           above the input. The same side margins as the input, so the text starts where the
           input starts and the scrollbar ends where the input ends. No padding on the right:
           Textual draws the scrollbar INSIDE the padding, so padding there pushes the bar
           away from the edge and leaves the text touching it (seen on the device with
           0.27.7); the gap between text and bar is on the lines below. */
        padding: 1 0 0 0;
        margin: 0 2 1 2;
        height: 1fr;
    }
    ChatScreen .chat-line, ChatScreen #chat-earlier {
        /* three columns between the text and the scrollbar; an empty line after each line,
           between question and answer too */
        margin: 0 3 1 0;
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
        advisor: list[str] | None = None,
    ) -> None:
        super().__init__()
        self._agent = agent
        self._messages = messages
        self._theme = theme
        self._agent_name = agent_name
        self._agent_mode = agent_mode
        self._advisor = advisor  # a command that answers a question (see _ask_advisor)
        # Exchanges of the advisor's own record that are older than what is shown (PgUp).
        self._earlier: list[tuple[str, str, str, bool]] = []

    def _header(self) -> str:
        """Who is on the other side, in one line — also when nobody is."""
        if self._advisor:
            wer = f"Ask your advisor about {_roh(self._agent_name)} · answered on this machine"
        elif self._agent_mode == "api":
            # The server chat is answered only where the server plays the agent (#468).
            wer = (
                f"Chat: {_roh(self._agent_name)} · your own program plays this agent, "
                "so nobody answers here"
            )
        else:
            wer = f"Chat: {_roh(self._agent_name)} · it answers within about a minute"
        return f"[dim]{wer} · Enter send · Esc close[/dim]"

    def _zeile(self, wer: str, text: str, farbe: str) -> Static:
        # Static, not Label: a Label is as wide as its text, and a long answer was cut off
        # at the border (seen on the device, 11.10.2026); a Static wraps.
        return Static(_c(farbe, f"\\[{_roh(wer)}] {_roh(text)}"), classes="chat-line")

    def _exchange(self, wechsel: tuple[str, str, str, bool]) -> list[Static]:
        """One question to the advisor and what came back, as two lines."""
        frage, wer, antwort, gut = wechsel
        farbe = self._theme.pos if gut else self._theme.warn
        return [
            self._zeile("You", frage, self._theme.data),
            self._zeile(wer or "advisor", antwort, farbe),
        ]

    def _earlier_hint(self) -> str:
        return f"[dim]↑ {len(self._earlier)} earlier · arrow up or PgUp[/dim]"

    def _advisor_lines(self) -> list[Static]:
        """What the talk with the advisor opens with: its last exchanges, or the invitation."""
        verlauf = _advisor_history()
        if not verlauf:
            return [Label("[dim]Ask a question about your agent or the world.[/dim]")]
        self._earlier = verlauf[:-_ADVISOR_PAGE]
        zeilen: list[Static] = []
        if self._earlier:
            zeilen.append(Label(self._earlier_hint(), id="chat-earlier"))
        for wechsel in verlauf[-_ADVISOR_PAGE:]:
            zeilen += self._exchange(wechsel)
        return zeilen

    def _show_earlier(self, scroll: VerticalScroll) -> None:
        """Put the next older exchanges above the ones shown; the view stays where it was."""
        seite, self._earlier = self._earlier[-_ADVISOR_PAGE:], self._earlier[:-_ADVISOR_PAGE]
        hinweis = self.query_one("#chat-earlier", Label)
        bisher_oben = scroll.query(".chat-line").first()
        scroll.mount(*[z for wechsel in seite for z in self._exchange(wechsel)], after=hinweis)
        if self._earlier:
            hinweis.update(self._earlier_hint())
        else:
            hinweis.remove()
        self.call_after_refresh(scroll.scroll_to_widget, bisher_oben, animate=False, top=True)

    def compose(self) -> ComposeResult:
        with Vertical(id="chat-wrap"):
            yield Label(self._header(), id="chat-header")
            with VerticalScroll(id="history-scroll"):
                if self._advisor:
                    yield from self._advisor_lines()
                elif self._messages:
                    for msg in self._messages:
                        von_mir = msg.get("sender", "") == "player"
                        yield self._zeile(
                            "You" if von_mir else self._agent_name,
                            str(msg.get("message", "")),
                            self._theme.data if von_mir else self._theme.pos,
                        )
                else:
                    yield Label("[dim]No messages yet.[/dim]")
            yield Input(placeholder="Type and press Enter…", id="chat-input")

    def on_mount(self) -> None:
        self.query_one(VerticalScroll).scroll_end(animate=False)
        self.query_one(Input).focus()

    def on_key(self, event: Any) -> None:
        if event.key == "escape":
            self.dismiss(None)
            event.prevent_default()
        elif event.key in ("up", "down", "pageup", "pagedown"):
            # The input keeps the focus; these keys move the talk above it — the arrows by
            # a line, PgUp/PgDn by a page. Going up at the very top fetches the next older
            # exchanges from the advisor's record.
            scroll = self.query_one("#history-scroll", VerticalScroll)
            if event.key in ("up", "pageup") and self._earlier and scroll.scroll_y <= 0:
                self._show_earlier(scroll)
            elif event.key == "up":
                scroll.scroll_up(animate=False)
            elif event.key == "down":
                scroll.scroll_down(animate=False)
            elif event.key == "pageup":
                scroll.scroll_page_up(animate=False)
            else:
                scroll.scroll_page_down(animate=False)
            event.prevent_default()
            event.stop()

    def on_input_submitted(self, event: Any) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.clear()
        if self._advisor:
            self._ask(text)
        else:
            self._send(text)

    @work
    async def _send(self, text: str) -> None:
        result = await self._agent.send_message(text)
        if not self.is_attached:
            return  # closed while the server answered
        scroll = self.query_one("#history-scroll", VerticalScroll)
        if "error" not in result:
            scroll.mount(self._zeile("You", text, self._theme.data))
            scroll.scroll_end(animate=False)
        else:
            scroll.mount(Static(_c(self._theme.warn, f"✗ {_roh(str(result['error']))}")))
        # Stay open — user closes with Esc
        self.query_one(Input).focus()

    @work
    async def _ask(self, text: str) -> None:
        """One question to the advisor command; the answer under the name it gives."""
        scroll = self.query_one("#history-scroll", VerticalScroll)
        scroll.mount(self._zeile("You", text, self._theme.data))
        wartet = Label("[dim]…[/dim]")
        scroll.mount(wartet)
        scroll.scroll_end(animate=False)
        wer, antwort, gut = await _ask_advisor(self._advisor or [], text)
        if not self.is_attached:
            return  # Esc while the advisor was thinking: nothing left to show it in
        await wartet.remove()
        farbe = self._theme.pos if gut else self._theme.warn
        scroll.mount(self._zeile(wer or "advisor", antwort, farbe))
        scroll.scroll_end(animate=False)
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
        height: auto;
        padding: 0 1;
    }
    """

    # Override app-level 'r' priority binding so FieldScreen handles refresh itself.
    BINDINGS: ClassVar[list[Binding]] = [  # type: ignore[assignment]
        Binding("r", "refresh_field", show=False, priority=True),
    ]

    _NAV_KEYS: ClassVar[set[str]] = {
        "up",
        "down",
        "left",
        "right",
        "ctrl+up",
        "ctrl+down",
        "ctrl+left",
        "ctrl+right",
        "home",
        "h",
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
            etype = field.entity_type or "novice"
            idx_label = f"[{self._idx + 1}/{n}]"
            zoom_label = "Zoom 2 — full field" if self._zoom == 2 else "Zoom 1 — scrollable"
            h1 = _c(t.struct, "[bold]═ FIELD VIEW[/bold]") + f"  {_c('dim', idx_label)}"
            h2 = _c(
                "dim",
                f"  {_roh(field.id[:8])} · {_roh(tier)} {_roh(etype)}"
                f" · {field.active_cell_count} cells · {zoom_label}",
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
            # Terminal chars are ~2x taller than wide — double the width so the
            # 128x128 field renders as a square instead of a portrait rectangle.
            out_h = content_h
            out_w = min(max(10, self._content_w - 4), out_h * 2)
            rows = _fv_render_zoom2(
                self._cells,
                self._field_w,
                self._field_h,
                out_w,
                out_h,
                alive_char=f"[{t.pos}]▓[/{t.pos}]",
                dead_char="[dim]░[/dim]",
            )
            content_widget.update("\n".join(rows))
        else:
            vp_w, vp_h = self._viewport_dims()
            vp_rows = _fv_render_zoom1(
                self._cells,
                self._vp_x,
                self._vp_y,
                vp_w,
                vp_h,
                self._field_w,
                self._field_h,
                alive_char="█",
                dead_char="·",
            )
            if self._content_w >= _FV_MINIMAP_THRESHOLD:
                mm_lines = _fv_render_minimap(
                    self._cells,
                    self._vp_x,
                    self._vp_y,
                    vp_w,
                    vp_h,
                    self._field_w,
                    self._field_h,
                    map_w=_FV_MAP_W,
                    map_h=_FV_MAP_H,
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
            footer_widget.update(_c("dim", "Z detail · [ ] field · R refresh · Esc back"))
        else:
            footer_widget.update(
                _c(
                    "dim",
                    "↑↓←→ scroll · Ctrl+↑↓ fast · H center · Z zoom"
                    " · [ ] field · R refresh · Esc back",
                )
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
        self._cells = set()  # reset so centroid re-centres
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
        height: auto;
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
        with VerticalScroll(id="setup-wrap"):
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
            str(persona_select.value) if persona_select.value is not Select.NULL else "scientist"
        )
        self._save(name, persona)

    @work
    async def _save(self, name: str, persona: str) -> None:
        self._set_status("Saving…")
        result = await self._agent.patch_identity(agent_name=name, persona=persona)
        if "error" in result:
            status = result.get("status_code", 0)
            if status == 409:
                self._set_error(f"Name '{_roh(name)}' is already taken — choose another.")
            else:
                gesagt = _roh(str(result["error"])[:80])  # kuerzen-ok: one-line label
                self._set_error(f"Error: {gesagt}")
            self.query_one("#name-input", Input).focus()
        else:
            self.dismiss({"agent_name": result.get("username", name), "persona": persona})

    def _set_error(self, msg: str) -> None:
        self.query_one("#error-label", Label).update(_c(self._theme.warn, msg))

    def _set_status(self, msg: str) -> None:
        self.query_one("#error-label", Label).update(_c("dim", msg))


# ---------------------------------------------------------------------------
# OnboardingModal — shown once when agent has no fields (fields=0, not dismissed)
# ---------------------------------------------------------------------------


_TURNIER_ANMELDUNG = re.compile(r"^POST /api/v1/tournaments/([0-9a-fA-F-]{36})/register$")


def _fix_bar_keys(
    state: GameState | None,
    *,
    agents: bool = False,
    advisor: bool = False,
    key_in_status: bool = False,
) -> list[tuple[str, str]]:
    """The keys the bottom bar shows — only those that do something right now (#468 S2).

    The bar used to be a fixed list: a new agent without a field saw P/F/E/V, none of
    which could work, and neither the tournament nor the missions. What is available comes
    from the server's state (``available_actions``, ``next_step``), not from a guess here.
    The bindings stay: a key that is not shown still answers with its hint. Pause ([X]) is
    deliberately absent — it is an explicit action and lives in the help. [W] carries the
    name of the menu it opens (bus, market, combat status). While the agent's key ends unless
    used, [U] is absent too: it stands in the status bar, right behind that warning.
    """
    actions = (state.available_actions if state else None) or {}
    has_fields = bool(state and state.fields)
    # [Tab] (focus a panel) is explained in the help, not in the bar (founder, 12.10.2026).
    keys = [("C", "Compass")]
    if has_fields:
        keys.append(("P", "Place"))
    if (actions.get("create_field") or {}).get("available"):
        keys.append(("F", "Field"))
    if has_fields:
        # [E] stays with every field owner: since 0.27.2 the dialog says what each field still
        # lacks, so the key does something even when no field is ready. Hiding it made it
        # vanish the moment the server stopped reporting fields of the wrong pattern type.
        keys.append(("E", "Evolve"))
        keys.append(("V", "View"))
    if state and (state.tournament or _turnier_id(state.next_step)):
        keys.append(("T", "Tournament"))
    # [M] asks the advisor where one is set (the same word as in the log's frame line).
    keys += [("W", "Marauder"), ("M", "Ask" if advisor else "Chat")]
    ends = _key_ends(state)
    if not (ends and ends[1]):  # else [U] stands behind the key warning in the status bar
        keys.append(("U", "Upgrade"))
    if agents:  # agent selector — only with a player token in the config
        keys.append(("A", "Agents"))
    keys += [("?", "Help"), ("Q", "Quit")]
    # [K] Key stands in the status bar, in place of the shortened key (#484). Only where
    # that line has no room is it the last entry here.
    if not key_in_status:
        keys.append(("K", "Key"))
    return keys


def _keep_word(used: int, width: int) -> str:
    """The word behind [U] in the key warning: the long one where the status bar has room.

    ``used`` is the length of the warning in front of it. On a narrow terminal the short
    word keeps the key itself on the line.
    """
    return "Keep agent" if used + len("[U] Keep agent") <= width else "Keep"


_WELCOME_KEYS = ("F", "C", "P", "V", "T", "?")


def _welcome_keys(state: GameState | None) -> list[str]:
    """The welcome block's hotkey lines, two per line — from the bar's keys (#468).

    One source with ``_fix_bar_keys``: a key the bar does not show is not offered here either.
    """
    keys = [(k, label) for k, label in _fix_bar_keys(state) if k in _WELCOME_KEYS]
    cells = [f"{_hk(k)} {label}".ljust(16) for k, label in keys]
    return ["  " + " ".join(cells[i : i + 2]).rstrip() for i in range(0, len(cells), 2)]


def _turnier_id(next_step: dict | None) -> str | None:
    """The round id when the server's advice is "take a free tournament slot" (#468)."""
    treffer = _TURNIER_ANMELDUNG.match(str((next_step or {}).get("where", "")))
    return treffer.group(1) if treffer else None


def _key_ends(state: GameState | None) -> tuple[str, bool] | None:
    """When the agent's key ends, in local time, and whether using it renews it (#468).

    Read from the server's ``key_expiry`` — no hours are counted here. ``None`` when nothing
    ends (permanent key) or the server does not say (older backend).
    """
    expiry = state.key_expiry if state else None
    when = _ortszeit((expiry or {}).get("at"))
    if when is None:
        return None
    return when, bool((expiry or {}).get("renews_on_use"))


def _ortszeit(iso: object) -> str | None:
    """A server time (ISO, UTC) in the viewer's local time, e.g. ``Sat 10 Oct, 20:30``."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso)).astimezone().strftime("%a %d %b, %H:%M")
    except ValueError:
        return None


# The three tournament compasses a player chooses from (#468) — the server maps them to its
# mission kinds (attack: capture a field, defend: guard your field, grow: gather spores on
# your field). attack is a chain (cos20 #471): a field that is open — holed, short of cells,
# or without an owner — is captured; if none is open, one Marauder sieges with the player's
# mega bombs; without a bomb, one collects bomb boxes. The server buys nothing. Until #471
# the server only captured: in tournament #269 Root-storm's four Marauders stood idle,
# because at the start no field is open (in #268 the first one opened after 169 minutes).
_TURNIER_KOMPASSE: tuple[tuple[str, str], ...] = (
    ("attack", "they capture fields; a siege uses your mega bombs"),
    ("defend", "they guard your field"),
    ("grow", "they gather spores on your field"),
)


def _turnier_angriff(brief: dict, bomben: int | None) -> str:
    """What attack does, with the mega bombs the player has — shown while the compass is attack.

    ``bomben`` is ``None`` when the state does not name them; the sentence then leaves the
    number out rather than guess it.
    """
    if brief.get("compass") != "attack":
        return ""
    habe = "" if bomben is None else f" (you have {bomben})"
    return (
        "attack: capture an open field — else siege with your mega bombs"
        f"{habe} — else collect bomb boxes."
    )


# How the server scores a tournament, in a player's words (categories of `prizes`).
_PREIS_KATEGORIEN = {
    "overall": "Overall",
    "energy": "Most energy produced",
    "territory": "Most fields",
    "tier": "Highest tier",
    "vitality": "Most alive field",
}


def _rang(n: int) -> str:
    """1 -> 1st, 2 -> 2nd, 3 -> 3rd, 4 -> 4th, 11 -> 11th, 21 -> 21st."""
    endung = "th"
    if not 11 <= n % 100 <= 13:
        endung = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{endung}"


# Without the server's posting (older server, no network) the dialog still says what it is —
# without numbers, which only the server knows.
_TURNIER_OHNE_AUSHANG = (
    "A contest in its own arena, apart from your main world, which keeps running. You get "
    "one living field there and Marauders — your agent's bodies in the arena. Choose a "
    "compass below; your Marauders then play by themselves, also while this terminal is "
    "closed. The slot is free; prizes are in-game only."
)


def _preis(eintrag: dict) -> str:
    """One prize, in energy: ``50,000 + shield``."""
    teile = []
    if eintrag.get("energy"):
        teile.append(f"{float(eintrag['energy']):,.0f}")
    teile += [str(item) for item in eintrag.get("items") or []]
    return " + ".join(teile)


def _preise(prizes: dict) -> list[str]:
    """The prize list of a tournament (its ``chest_catalog``): ranked categories one line each,
    the single-winner categories together on one line — the dialog has little height."""
    zeilen, einzeln = [], []
    for kategorie, raenge in prizes.items():
        if not isinstance(raenge, dict) or not raenge:
            continue
        name = _PREIS_KATEGORIEN.get(kategorie, kategorie)
        if list(raenge) == ["1"]:
            einzeln.append(f"{name} {_preis(raenge['1'])}")
            continue
        plaetze = sorted(raenge.items(), key=lambda kv: int(kv[0]))
        zeilen.append(
            f"  {name}: " + " · ".join(f"{_rang(int(r))} {_preis(p)}" for r, p in plaetze)
        )
    if einzeln:
        zeilen.append("  " + " · ".join(einzeln))
    return zeilen


def _turnier_erklaerung(eintrag: dict) -> str:
    """What a tournament is, why join, and what you do here — for a person (#468).

    Numbers come from the server's posting (``GET /tournaments/open``); a value it does not
    send is left out, never filled in here. Short on purpose: the three answers must fit the
    dialog without scrolling at a normal terminal size; what each compass does stands in
    the choices below the text.
    """
    slots = eintrag.get("slots") or {}
    teilnehmer = sum(int((slots.get(k) or {}).get("quota") or 0) for k in ("free", "npc"))
    marauder = int(eintrag.get("marauders_per_agent") or 1)
    energie = float(eintrag.get("start_energy") or 0)
    start = _ortszeit(eintrag.get("starts_at")) or "soon"
    ende = _ortszeit(eintrag.get("ends_at")) or "later"
    was = (
        "What is it? A contest in its own arena; your main world keeps running. "
        f"From {start} to {ende} (local time). "
    )
    was += f"Up to {teilnehmer} agents; each" if teilnehmer else "Each"
    was += (
        f" gets one living field, {marauder} Marauder{'s' if marauder != 1 else ''}"
        " (your bodies there)"
    )
    was += f" and {energie:,.0f} energy." if energie else "."
    was += " You join this one tournament only."
    preise = _preise(eintrag.get("prizes") or {})
    warum = "Why join? A free slot; every result counts in the hall of fame."
    if preise:
        warum += "\nPrizes in energy (in-game only, no cash-out):\n" + "\n".join(preise)
    hier = (
        "What you do here: choose a compass below; your Marauders then play by themselves, "
        "even with this terminal closed. Later, [T] shows your score and changes the compass."
    )
    return "\n\n".join([was, warum, hier])


# What a tournament scores, in the words of the dashboard: "arena energy" is the energy of
# the arena field, not the balance in the agent panel (both were "E", #468).
_TURNIER_WERTUNG: tuple[tuple[str, str], ...] = (
    ("arena energy", "energy"),
    ("fields", "territory"),
    ("vitality", "vitality"),
)


def _showcase_payload(result: dict, request_id: str) -> dict:
    """``PATCH /players/me`` body for the showcase switch.

    Turning it on also accepts the Terms (cos20 #504): the server records that acceptance with
    time and version, and the consent screen says so above the switch. Turning it off sends no
    acceptance.
    """
    payload = {
        "public_showcase": result["desired"],
        "consent_text_hash": result["consent_text_hash"],
        "request_id": request_id,
    }
    if result["desired"]:
        payload["accept_terms"] = True
    return payload


def _platz(ranking: Ranking | None, wb: Any) -> str:
    """``#75 / 380``: the place and the pool of the one ranking (cos20 #508).

    ``world_briefing.total_agents`` counts every active player but the house (389 on 17.10.),
    the ranking only its pool of active agents (380) — the panel showed ``#75 / 389`` next to
    ``Rank #75/380``. Older backends send no ``rang``; then the briefing's numbers stand.
    """
    if ranking is not None and ranking.rang and ranking.rang_von:
        return f"#{ranking.rang} / {ranking.rang_von}"
    return f"#{wb.your_rank} / {wb.total_agents}"


def _name_vor_zahl(text: str, breite: int) -> str:
    """``Name (Zahl)`` in ``breite`` columns: the name is cut with "…", the number never (#508).

    The server writes ``top_agent`` as ``Socket-hand (33.6M E)``. Cut at a fixed length, the
    terminal showed ``Socket-hand (total_energy 336313`` — a wrong number. Text without a number
    in brackets is cut at the end like any other line.
    """
    if len(text) <= breite:
        return text
    name, sep, zahl = text.rpartition(" (")
    if not sep or not zahl.endswith(")"):
        return text[: max(breite - 1, 1)] + "…"
    platz = breite - len(zahl) - len(sep) - 1
    return (name[:platz] + "…" if platz > 0 else "…") + sep + zahl


def _felder_die_passen(anzahl: int, zeilen_frei: int) -> int:
    """How many field lines fit (#508): at most ``_MAX_FIELDS``, and one row stays free for the
    "+N more" line when not all fit — like htop's list, which ends at the frame."""
    zeigen = min(anzahl, _MAX_FIELDS, max(zeilen_frei, 0))
    if zeigen < anzahl:
        zeigen = max(min(zeigen, zeilen_frei - 1), 0)
    return zeigen


def _rang_zeile(ranking: Ranking, breite: int | None = None) -> str:
    """Your place in the one ranking (cos20 #504): ``Rank #12 of 380 · total 4.8M E``.

    The total is your own, shown only to you, and only while the ranking counts it. Older
    backends send no ``rang``; then the line keeps the tier and score it showed before.
    With ``breite`` the words go first, never the numbers (#508): ``#12/380 4.8M E``.
    """
    if ranking.rang > 0:
        von = f"/{ranking.rang_von}" if ranking.rang_von else ""
        summe = f"{_kurz(ranking.gesamtenergie)} E" if ranking.gesamtenergie is not None else ""
        formen = [
            f"Rank #{ranking.rang}"
            + (f" of {ranking.rang_von}" if von else "")
            + (f"  ·  total {summe}" if summe else ""),
            f"Rank #{ranking.rang}{von}" + (f" · {summe}" if summe else ""),
            f"#{ranking.rang}{von}" + (f" {summe}" if summe else ""),
        ]
        passend = [f for f in formen if breite is None or len(f) <= breite]
        return passend[0] if passend else formen[-1]
    score = f"  Score: {ranking.player_score:,.0f}" if ranking.player_score > 0 else ""
    return f"T{ranking.player_tier} {ranking.tier_name}{score}"


def _kurz(wert: float) -> str:
    """A score short enough for the top line: 1,200 · 63k · 1.2M."""
    if wert >= 999_500:
        return f"{wert / 1_000_000:.1f}M"
    if wert >= 10_000:
        return f"{wert / 1000:.0f}k"
    return f"{wert:,.0f}"


def _turnier_stand(brief: dict) -> str:
    """Your score against the best value of each category, as one part of the top line.

    "best", not "leader": the server sends the highest value per category, and each may
    belong to a different agent.
    """
    ich, beste = brief.get("my_score") or {}, brief.get("leader") or {}
    paare = [
        f"{name} {_kurz(float(ich.get(kat) or 0))}/{_kurz(float(beste.get(kat) or 0))}"
        for name, kat in _TURNIER_WERTUNG
    ]
    return "you/best: " + " · ".join(paare)


def _wann(iso: object) -> str:
    """Weekday and time in the player's local time: ``Sun 06:30`` — ``?`` when unreadable."""
    try:
        return datetime.fromisoformat(str(iso)).astimezone().strftime("%a %H:%M")
    except ValueError:
        return "?"


def _turnier_zeile(brief: dict, breite: int | None = None, schluss: str = "") -> str:
    """One line about the player's tournament: compass, when, and the score while it runs.

    It is the top line of the dashboard. With ``breite`` (the columns it has) it drops what
    a narrow terminal can lose — ``schluss`` first (the key hint; the key bar names the key
    too), then the end time, then the compass; \\[T] shows all of it. Without a compass the
    score is left out: the one thing to say then is that a compass is missing.
    """
    laeuft = brief.get("status") == "running"
    kompass = f"compass {brief['compass']}" if brief.get("compass") else "no compass yet"
    wann = (
        "ends " + _wann(brief.get("ends_at"))
        if laeuft
        else "starts " + _wann(brief.get("starts_at"))
    )
    teile = [f"Tournament #{brief.get('number', '?')}", kompass]
    entbehrlich = [schluss] if schluss else []
    if laeuft and brief.get("compass") and brief.get("my_score"):
        teile.append(_turnier_stand(brief))
        entbehrlich = [*entbehrlich, wann, kompass]
    teile.append(wann)
    if schluss:
        teile.append(schluss)
    while breite is not None and entbehrlich and len(" · ".join(teile)) > breite:
        teile.remove(entbehrlich.pop(0))
    return " · ".join(teile)


def _marauder_zeile(koerper: dict) -> str:
    """One Marauder of the player in the running tournament: what it does, and its HP."""
    nummer = int(koerper.get("body_slot") or 0) + 1
    if koerper.get("dead"):
        return f"Marauder {nummer}: dead"
    was = "on a mission" if koerper.get("on_mission") else "between missions"
    return f"Marauder {nummer}: {was} · HP {int(koerper.get('hp') or 0)}"


def _turnier_bild(brief: dict) -> str:
    """The running tournament for the \\[T] dialog: exact scores and each Marauder (#468).

    Empty before the tournament runs — the server sends scores and Marauders only then.
    """
    ich, beste = brief.get("my_score") or {}, brief.get("leader") or {}
    zeilen: list[str] = []
    if ich or beste:
        zeilen.append(f"{'':<14}{'you':>10}{'best in the arena':>20}")
        zeilen += [
            f"{name:<14}{float(ich.get(kat) or 0):>10,.0f}{float(beste.get(kat) or 0):>20,.0f}"
            for name, kat in _TURNIER_WERTUNG
        ]
    koerper = [k for k in brief.get("bodies") or [] if isinstance(k, dict)]
    if koerper:
        zeilen += ["", *(_marauder_zeile(k) for k in koerper)]
    return "\n".join(zeilen)


# --- Capture a field (cos20 #468) ---------------------------------------------------------------

# Time limit of a mission started here, in ticks. The schemas in the state allow 50 to 2000
# for a siege and 50 to 1000 for collecting; 200 leaves hours for the walk and frees the Marauder
# if the target cannot be reached.
_MISSION_FRIST = 200
_SAMMEL_STUECK = 10  # items one collecting mission picks up at most

# Mission kinds and outcomes in a player's words: `mission_word` / `mission_outcome_text`
# (action.py) — one table for the terminal and Shikigon's bar (cos20 #475).


def _mission_wort(mission: dict) -> str:
    return mission_word(str(mission.get("mission_type") or "mission"))


def _mission_zeile(mission: dict) -> str:
    """The running mission for the agent panel: ``siege — traveling``."""
    schritt = str(mission.get("current_step") or mission.get("status") or "")
    schritt = schritt.split(":", 1)[0].replace("_", " ")
    return f"{_mission_wort(mission)} — {schritt}" if schritt else _mission_wort(mission)


def _fight_who(data: dict) -> str:
    """Which Marauder: the main one is ``Marauder``, an arena body ``Marauder 2``."""
    if data.get("body") == "tournament":
        return f"Marauder {int(data.get('slot') or 0) + 1}"
    return "Marauder"


def _fight_opponent(data: dict) -> str:
    name = data.get("opponent")
    return str(name) if name else f"a {data.get('opponent_kind') or 'monster'}"


def _fight_dropped(data: dict) -> str:
    """What fell, in words: ``1 mega bomb, 2 ammo`` — empty when nothing was carried."""
    stuecke = data.get("dropped") or {}
    if not isinstance(stuecke, dict):
        return ""
    return ", ".join(
        f"{int(zahl)} {str(art).split(':', 1)[0].replace('_', ' ')}"
        for art, zahl in sorted(stuecke.items())
    )


def _fight_lines(events: list[dict]) -> list[tuple[str, str, bool]]:
    """The log lines for new fight events: ``(tab, text, lost)`` in the order they happened.

    A lost fight is always its own line — it is the reason for what the Marauder does next.
    Fights one Marauder won against the same opponent since the last reading are one line
    that counts (the log would otherwise fill with wins).
    """
    zeilen: list[tuple[str, str, bool]] = []
    siege: dict[tuple[str, str, str], list[float]] = {}
    for ev in events:
        roh = ev.get("data")
        data: dict = roh if isinstance(roh, dict) else {}
        tab = "tournament" if data.get("body") == "tournament" else "world"
        wer, gegner = _fight_who(data), _fight_opponent(data)
        energie = float(data.get("energy") or 0.0)
        if ev.get("event_type") == "fight_won":
            siege.setdefault((tab, wer, gegner), []).append(energie)
            continue
        teile = []
        gefallen = _fight_dropped(data)
        if gefallen:
            teile.append(f"dropped {gefallen}")
        if energie > 0:
            teile.append(f"-{_num(round(energie))} energy")
        teile.append("sent back to his bunk")
        zeilen.append((tab, f"✗ {wer} lost a fight against {gegner} — " + ", ".join(teile), True))
    for (tab, wer, gegner), betraege in siege.items():
        summe = sum(betraege)
        wie_oft = "a fight" if len(betraege) == 1 else f"{len(betraege)} fights"
        gewinn = f" — +{_num(round(summe))} energy" if summe > 0 else ""
        zeilen.append((tab, f"✓ {wer} won {wie_oft} against {gegner}{gewinn}", False))
    return zeilen


#: What the server tells this agent alone (cos20 ``core/geschichte.py``).
_STORY_EVENTS = ("fight_lost", "fight_won", "field_under_claim", "field_lost", "field_held")


def _field_lines(
    events: list[dict], world_fields: set[str], in_tournament: bool
) -> list[tuple[str, str, bool]]:
    """The log lines for what happened to the agent's own fields: ``(tab, text, bad)``.

    A field of the main world goes into the world log; any other field belongs to the arena
    of the running tournament. The server names who did it (``by``) and the field.
    """
    zeilen: list[tuple[str, str, bool]] = []
    for ev in events:
        roh = ev.get("data")
        data: dict = roh if isinstance(roh, dict) else {}
        kennung = str(data.get("field_id") or "")
        tab = "tournament" if in_tournament and kennung not in world_fields else "world"
        feld, wer = f"your field {kennung[:8]}", str(data.get("by") or "another agent")
        art = ev.get("event_type")
        if art == "field_under_claim":
            zeilen.append((tab, f"⚠ {wer} has claimed {feld}", True))
        elif art == "field_lost":
            zeilen.append((tab, f"✗ {feld} was captured by {wer}", True))
        elif art == "field_held":
            wie = f"the claim of {wer} failed"
            if data.get("given_up"):
                wie = f"{wer} gave up the claim"
            zeilen.append((tab, f"✓ {feld} held — {wie}", False))
    return zeilen


def _sporen_zeile(anzahl: int) -> str:
    """Dormant spores on the agent's fields, with what follows (spore rule, cos20 #486).

    A planted spore wakes as cells when its dormancy ends; one that holds loot vanishes
    unless a Marauder collects it first (``spore_engine``).
    """
    if anzahl == 1:
        return (
            "1 dormant spore lies on your fields — it wakes as cells,"
            " or vanishes uncollected if it holds loot"
        )
    return (
        f"{anzahl} dormant spores lie on your fields — they wake as cells;"
        " those holding loot vanish unless collected"
    )


def _mission_wer(mission: dict) -> str:
    """Which Marauder leads the mission: ``Marauder`` in the main world, ``Marauder 2`` for an
    arena body whose place the server names (an older server does not: then ``Marauder``)."""
    slot = mission.get("slot")
    if mission.get("body") == "tournament" and isinstance(slot, int):
        return f"Marauder {slot + 1}"
    return "Marauder"


def _mission_start(mission: dict) -> str:
    """One log line for a mission that begins: who, what, and what it is after."""
    text = f"▶ {_mission_wer(mission)}: {_mission_wort(mission)} started"
    ziel = mission_goal_text(mission)
    return f"{text} — {ziel}" if ziel else text


def _mission_ende(mission: dict) -> str:
    """One log line for a mission that ended: what, how, and why if the server says."""
    ausgang = str(mission.get("outcome") or "")
    roh = mission.get("params")
    kiste = roh.get("kiste") if isinstance(roh, dict) else None
    grund = mission_outcome_text(ausgang, what=str(kiste).replace("_", " ") if kiste else None)
    wie = "done" if mission.get("status") == "completed" else str(mission.get("status"))
    text = f"{_mission_wer(mission)}: {_mission_wort(mission)} {wie}"
    return f"{text} — {grund}" if grund else text


def _eroberung_lage(state: GameState | None) -> dict | None:
    """What the server says about capturing a field — ``None`` if it offers no missions.

    Read from ``available_actions`` of the state: whether the Marauder is free, the mega
    bombs the agent has, up to three target fields and where bomb boxes lie.
    """
    aktionen = state.available_actions if state else {}
    start = aktionen.get("start_mission")
    if not isinstance(start, dict):
        return None
    claim = aktionen.get("claim_field")
    if not isinstance(claim, dict):
        claim = {}
    ziele = [z for z in claim.get("targets") or [] if isinstance(z, dict) and z.get("field_id")]
    beute = start.get("richest_loot_field")
    return {
        "frei": bool(start.get("available")),
        "marauder": start.get("marauder_state"),
        "bomben": int(start.get("mega_bombs") or 0),
        "schwelle": start.get("holes_threshold"),
        "halten": claim.get("claim_ticks"),
        "ziele": ziele[:3],  # kuerzen-ok: list length
        "beute": beute.get("field_id") if isinstance(beute, dict) else None,
    }


def _ziel_zeile(ziel: dict, bomben: int) -> str:
    """One target field: its facts, and whether it can be taken as things stand."""
    zeile = (
        f"Field {str(ziel['field_id'])[:8]} · tier {int(ziel.get('tier') or 0)} · "
        f"{int(ziel.get('active_cells') or 0):,} cells · {int(ziel.get('hole_count') or 0)} holes"
    )
    if ziel.get("is_vulnerable"):
        return zeile + " — open, no bomb needed"
    return zeile + (" — needs a mega bomb" if bomben < 1 else "")


def _eroberung_optionen(lage: dict, preis: float | None) -> tuple[list[str], list[tuple]]:
    """The choices of the dialog and what each one does: target, buy, collect."""
    optionen = [_ziel_zeile(z, lage["bomben"]) for z in lage["ziele"]]
    griffe: list[tuple] = [("ziel", z) for z in lage["ziele"]]
    if lage["bomben"] < 1:
        if preis is not None:
            optionen.append(f"Buy a mega bomb — {preis:,.0f} E (asks before it pays)")
            griffe.append(("kaufen", preis))
        if lage["beute"]:
            optionen.append("Collect bomb boxes — free, about half an hour, may bring none")
            griffe.append(("sammeln", lage["beute"]))
    return optionen, griffe


def _eroberung_text(lage: dict) -> str:
    """How capturing works and what the agent has — numbers from the server, none invented."""
    loecher = f"more than {lage['schwelle']} holes" if lage["schwelle"] else "enough holes"
    halten = f" for about {lage['halten']} ticks" if lage["halten"] else ""
    wie = (
        "How it works: your Marauder walks to the field and sieges it with mega bombs until "
        f"the field has {loecher}. The server then captures it for you; your Marauder holds "
        f"it{halten}. Your main world keeps running."
    )
    bomben = lage["bomben"]
    habe = f"You have {bomben} mega bomb{'s' if bomben != 1 else ''}."
    if bomben < 1:
        habe += " A siege needs at least one, unless the field is open already."
    if not lage["ziele"]:
        habe += " The server names no field in reach right now."
    return f"{wie}\n\n{habe}"


_QUIT_TITLE = "Before you go: your agent's key ends {when} (local time)"

_KEEP_BODY = (
    "Key persistence keeps your agent's key from ending while you pay — you can be away "
    "as long as you like. Price and terms are shown at checkout, in your browser."
)
_PLANS_BODY = (
    "A plan makes this agent's key permanent and opens an account for you. What each plan "
    "includes is shown at checkout, in your browser."
)


def _json_or_empty(resp: Any) -> dict:
    """The response body as a dict — ``{}`` when it is not JSON or not an object."""
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _quit_warning(when: str) -> str:
    """What the player loses by leaving, and the three ways to keep the agent (#468).

    Every sentence is a fact of the server: in the main world it does not play an ``api``
    agent; a key nobody uses ends at the time it names; the agent then becomes a Vagant and
    there is no way back (``claim-agent`` answers 410).
    """
    # Two paragraphs: the dialog shows about eight lines before it scrolls, and the ways to
    # keep the agent must be among them (#468).
    return (
        f"If nothing uses its key before {when}, the key ends: your agent becomes a Vagant — "
        "the server takes it over, and you cannot get it back. While you are away, the server "
        "does not play your agent in the main world.\n\n"
        "To keep it: come back before then (opening this dashboard renews the key), keep a "
        "program of yours running with this key, or choose [2] — key persistence, paid."
    )


class PauseConfirmModal(ModalScreen):
    """Pausing is explicit and asked twice — never on a single key (#468).

    Step 1 names the consequence and the server's waiting time ([Y] continue, [N] cancel).
    Step 2 asks for the agent's name, typed into a field. Esc cancels at every step.
    Dismisses with ``True`` only after both steps.
    """

    BINDINGS: ClassVar[list[Binding]] = [  # type: ignore[assignment]
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    DEFAULT_CSS = """
    PauseConfirmModal {
        align: center middle;
    }
    PauseConfirmModal > #pause-wrap {
        width: 70;
        height: auto;
        border: solid $warning;
        background: $surface;
        padding: 1 2;
    }
    PauseConfirmModal > #pause-wrap > Label {
        margin-bottom: 1;
    }
    """

    def __init__(self, theme: Theme, agent_name: str, waiting: str) -> None:
        super().__init__()
        self._theme = theme
        self._agent_name = agent_name
        self._waiting = waiting
        self._step = 1

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="pause-wrap"):
            yield Label(_c(self._theme.warn, f"Pause {_roh(self._agent_name)}?"), id="pause-title")
            yield Label(
                "Your agent leaves the game: its body becomes a ghost and takes no\n"
                "part in fights, loot or missions.\n"
                f"You can resume it only after {self._waiting}.",
                id="pause-body",
            )
            yield Label(_c("dim", "[Y] continue · [N] / Esc cancel"), id="pause-footer")
            # Disabled until step 2: a hidden but focusable field swallowed the [Y] of step 1.
            yield Input(placeholder=self._agent_name, id="pause-name", disabled=True)

    def on_mount(self) -> None:
        self.query_one("#pause-name", Input).display = False

    def on_key(self, event: Any) -> None:
        """Step 1: [Y] opens the name field, [N] cancels. Step 2 belongs to the field."""
        if self._step != 1:
            return
        if event.key in ("y", "Y"):
            event.stop()
            self._step = 2
            self._footer(f"Type {_roh(self._agent_name)} and press Enter · Esc cancel")
            field = self.query_one("#pause-name", Input)
            field.disabled = False
            field.display = True
            field.focus()
        elif event.key in ("n", "N"):
            event.stop()
            self.dismiss(False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Step 2: only the exact agent name confirms."""
        if event.value.strip() == self._agent_name:
            self.dismiss(True)
        else:
            self._footer(f"That is not {_roh(self._agent_name)} — type it exactly, or Esc")

    def _footer(self, text: str) -> None:
        self.query_one("#pause-footer", Label).update(_c("dim", text))

    def action_cancel(self) -> None:
        """Esc — nothing happens."""
        self.dismiss(False)


class OnboardingModal(ModalScreen):
    """One-time hint modal shown on the first tick when the agent has no game fields.

    Dismissed by pressing Enter or Escape — never shown again on this machine. Not Space:
    Space was bound to Pause app-wide and fired under this modal (#468).
    """

    BINDINGS: ClassVar[list[Binding]] = [  # type: ignore[assignment]
        Binding("enter", "dismiss_modal", "Got it", show=False),
        Binding("escape", "dismiss_modal", "Got it", show=False),
    ]

    DEFAULT_CSS = """
    OnboardingModal {
        align: center middle;
    }
    OnboardingModal > #onboard-wrap {
        width: 62;
        height: auto;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    OnboardingModal > #onboard-wrap > #onboard-title {
        height: auto;
        margin-bottom: 1;
        text-style: bold;
    }
    OnboardingModal > #onboard-wrap > #onboard-body {
        height: auto;
        margin-bottom: 1;
    }
    OnboardingModal > #onboard-wrap > #onboard-footer {
        height: auto;
        content-align: center middle;
    }
    """

    def __init__(self, theme: Theme, next_step: dict | None = None) -> None:
        super().__init__()
        self._theme = theme
        self._next_step = next_step

    def _body(self) -> str:
        """The server's advice first (#468); no steps that need a field you may not have."""
        g = self._theme.guide
        zeilen = []
        if self._next_step:
            zeilen.append(f"  {self._next_step.get('you_are_here', '')}")
            zeilen.append(f"  → {self._next_step.get('next', '')}")
            if _turnier_id(self._next_step):
                zeilen.append(f"  {_c(g, _hk('T'))}  Join the tournament")
            zeilen.append("")
        zeilen.append(f"  {_c(g, _hk('C'))}  Set compass  — give your agent a direction")
        zeilen.append(f"  {_c(g, _hk('W'))}  Marauder     — bus, market, combat status")
        zeilen.append(f"  {_c(g, _hk('?'))}  Help")
        return "\n".join(zeilen)

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="onboard-wrap"):
            yield Label(
                _c(self._theme.guide, "Welcome to Cosmergon."),
                id="onboard-title",
            )
            yield Label(self._body(), id="onboard-body")
            yield Label(_c("dim", "Enter · Esc dismiss"), id="onboard-footer")

    def action_dismiss_modal(self) -> None:
        self.dismiss()


# ---------------------------------------------------------------------------
# FirstStartApp — shown before Dashboard when no credentials exist
# ---------------------------------------------------------------------------


class FirstStartApp(App):
    """Mini-app: [Enter] New agent / [K] Enter existing key."""

    DEFAULT_CSS = """
    FirstStartApp {
        background: $surface;
        align: center middle;
    }
    #fs-box {
        width: 52;
        height: auto;
        padding: 1 3;
        border: solid $accent;
        background: $surface;
    }
    #fs-title {
        text-align: center;
        text-style: bold;
        margin-bottom: 1;
    }
    #fs-subtitle {
        text-align: center;
        margin-bottom: 1;
    }
    #fs-options {
        margin-top: 1;
    }
    #fs-hint {
        margin-top: 1;
    }
    #fs-input {
        display: none;
        margin-top: 1;
    }
    #fs-error {
        display: none;
        margin-top: 1;
        color: $error;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [  # type: ignore[assignment]
        Binding("enter", "new_agent", show=False, priority=True),
        Binding("k", "enter_key", show=False, priority=True),
        Binding("q", "quit", show=False, priority=True),
        Binding("escape", "quit", show=False, priority=True),
    ]

    def __init__(self, base_url: str = "https://cosmergon.com") -> None:
        super().__init__()
        self.result_key: str = ""  # populated if user enters a key
        self._base_url = base_url

    def compose(self) -> ComposeResult:
        with Vertical(id="fs-box"):
            yield Label("[bold cyan]COSMERGON[/bold cyan]", id="fs-title")
            yield Label("[dim]Welcome.[/dim]", id="fs-subtitle")
            yield Label(
                "[cyan]\\[Enter][/cyan]  Create your agent\n"
                "[cyan]\\[K][/cyan]      I have a key\n\n"
                "[dim]Your key is always visible in the dashboard. Press \\[K].[/dim]",
                id="fs-options",
            )
            yield Input(placeholder="Paste key (AGENT-... or CSMR-...)", id="fs-input")
            yield Label("", id="fs-error")
            yield Label("[dim]Enter new · K key · Q or Esc quit[/dim]", id="fs-footer")

    def action_new_agent(self) -> None:
        inp = self.query_one("#fs-input", Input)
        if inp.display:
            return  # input is open — Enter submits the input, not this action
        self.result_key = ""
        self.exit()

    def action_enter_key(self) -> None:
        inp = self.query_one("#fs-input", Input)
        if inp.display:
            return  # already open
        inp.display = True
        inp.focus()
        # Unbind K so typing works — Textual 0.46+ renamed .keys to .key_to_bindings
        bindings_store = getattr(
            self._bindings, "key_to_bindings", getattr(self._bindings, "keys", None)
        )
        if bindings_store is not None:
            bindings_store.pop("k", None)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        key = event.value.strip()
        if not key:
            return
        # Recognize prefix — Master Key gets resolved to agent key
        if key.upper().startswith("CSMR-"):
            resolved = _resolve_token(key, self._base_url)
            if resolved:
                self.result_key = resolved
                self.exit()
            else:
                err = self.query_one("#fs-error", Label)
                err.update("[bold]Could not resolve Master Key.[/bold] Check key and try again.")
                err.display = True
            return
        self.result_key = key
        self.exit()


# Entry point
# ---------------------------------------------------------------------------


def _secrets_leave_environment() -> None:
    """Take the key and the Master Key out of the environment once the agent holds them.

    Every program the dashboard starts inherits its environment: the advisor got a cleaned
    copy, but the browser behind ``webbrowser.open`` got the whole one, key included (cos20
    #492 S11). Gone from the environment, no later child can inherit them.
    """
    for name in _SECRETS_IN_ENVIRONMENT:
        os.environ.pop(name, None)


_KEY_FD_BYTES = 4096  # an API key is under 100 characters; a bound all the same


def _key_from_fd(fd: int) -> str:
    """The API key from an open file descriptor, read to its end (at most 4 KiB) and closed.

    A starter hands the key over a pipe (``--api-key-fd 3 3< <(…)``), so it stands neither in
    the process list nor in ``/proc/<pid>/environ``. That file keeps a process's starting
    environment for its whole life, also after ``os.environ`` lets go of a name, so
    ``_secrets_leave_environment`` keeps the key from children only (cos20 #492 B7).
    """
    with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as pipe:
        key = pipe.read(_KEY_FD_BYTES).strip()
    if not key:
        raise SystemExit(f"No API key on file descriptor {fd}.")
    return key


def _needs_first_start(api_key: str | None) -> bool:
    """Return True when the first-start screen should be shown."""
    if api_key or os.environ.get("COSMERGON_API_KEY", ""):
        return False
    from cosmergon_agent.config import load_credentials

    saved_key, _, _ = load_credentials()
    return not saved_key


def _resolve_token(token: str, base_url: str) -> str | None:
    """Exchange a Master Key (CSMR-...) for the oldest agent's API key.

    Uses resolve_token_sync from _token.py. Saves token + all agents
    to config.toml so the user doesn't need --token next time.
    Returns the API key of the oldest agent, or None on error.
    """
    from cosmergon_agent._token import TokenResolutionError, resolve_token_sync

    try:
        result = resolve_token_sync(token, base_url=base_url)
    except TokenResolutionError as exc:
        print(f"\n\u2717  {exc}")
        return None

    selected = result.selected  # set by _parse_agents_response
    n = len(result.agents)

    # Save token + all agents to config (single write)
    from cosmergon_agent.config import save_all_agents_and_token

    save_all_agents_and_token(
        token,
        [(a.agent_name, a.api_key.raw, a.agent_id) for a in result.agents],
        selected.agent_name,
        base_url=base_url,
    )

    # Terminal output (positive framing — CJ-Panel S110)
    print(f"\n  \u2713  Switching to your {result.subscription_tier} account")
    print(f"     Agent: {selected.agent_name}")
    if n > 1:
        print(f"     ({n} agents total. Press [A] in the dashboard to switch.)")
    print()
    return selected.api_key.raw


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cosmergon Agent Dashboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Themes: cosmergon (default), matrix, mono, high-contrast\n"
        "Config: ~/.cosmergon/dashboard.toml  |  COSMERGON_THEME env var",
    )
    parser.add_argument("--api-key", help="API key (auto-registers if omitted)")
    parser.add_argument(
        "--api-key-fd",
        type=int,
        metavar="N",
        help="read the API key from file descriptor N (a pipe from the starter)",
    )
    parser.add_argument("--token", help="Master Key (CSMR-...) — loads all your agents")
    parser.add_argument("--base-url", default="https://cosmergon.com")
    parser.add_argument("--theme", choices=[*THEMES, "omarchy"], default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    if args.api_key_fd is not None:  # never in argv or environ (cos20 #492 B7)
        args.api_key = _key_from_fd(args.api_key_fd)

    # --token: resolve Master Key to agent API key via GET /players/me/agents
    if args.token:
        if args.token.upper().startswith("CSMR-"):
            resolved_via_token = _resolve_token(args.token, args.base_url)
            if resolved_via_token:
                args.api_key = resolved_via_token
                # Token storage warning (once)
                from cosmergon_agent.config import (
                    CONFIG_PATH,
                    is_token_warning_shown,
                    set_token_warning_shown,
                )

                if not is_token_warning_shown():
                    print(f"  Key saved to {CONFIG_PATH}")
                    print("  Just run 'cosmergon-dashboard' next time — no --token needed.")
                    print()
                    set_token_warning_shown()
            else:
                raise SystemExit(1)
        else:
            # If not CSMR- prefix, treat as api_key (forward-compat)
            args.api_key = args.token

    theme = _load_theme(args.theme)
    resolved_key = args.api_key

    # First-start screen: shown when no credentials exist
    if _needs_first_start(resolved_key):
        try:
            first_start = FirstStartApp(base_url=args.base_url)
            first_start.run()
            if first_start.result_key:
                resolved_key = first_start.result_key
            # else: result_key is "" → auto-register (default behavior)
        except Exception:
            # graceful degradation: auto-register if FirstStartApp fails
            logger.debug("main: FirstStartApp failed, auto-registering", exc_info=True)

    try:
        agent = CosmergonAgent(api_key=resolved_key, base_url=args.base_url, poll_interval=10.0)
        _secrets_leave_environment()
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

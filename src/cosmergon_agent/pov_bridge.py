"""The bridge to the native first-person renderer (cos20 #799, package P6).

When the program ``cosmergon-pov`` is installed, the dashboard starts it, keeps the view's area
empty (``pov_place``) and tells it what the view shows, as text lines on its stdin. The lines are
the control protocol of the program (``cosmergon-pov/COSMERGON.md``, "Control channel"): a fixed
window of 48 x 48 squares, x east, y NORTH, (0, 0) the south-west corner, angles in degrees,
0 = east, counter-clockwise.

Stdlib only, no Textual. Nothing here raises into the dashboard.
"""

from __future__ import annotations

import json
import math
import os
import re
import select
import stat
import subprocess  # nosec B404 - the one program of this module, started without a shell
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from cosmergon_agent import pov_place

if TYPE_CHECKING:
    from cosmergon_agent.scope import Sight

GRID = 48  # squares on a side of the program's window
MARGIN = 22  # squares: ceil(scope._FAR) = 22, how far a look reaches (the test compares both)
MAX_THINGS = 256  # ids 0..255 of the program
_LINE_MOST = 255
_ROW_ABOVE = 4  # more changed squares in a row than this: one ``row`` line is shorter
_PIPE_BUF = getattr(select, "PIPE_BUF", 512)  # a write up to this size is whole or not at all
_NAMES = {".": "empty", "#": "cell", "=": "wall", "T": "tower", "O": "own"}
_CHARS = {0: ".", 1: "#", 2: "#", 3: "=", 4: "T"}  # _posture: EMPTY, CELL, OWN, WALL, TOWER
_CHARS_OWN = {**_CHARS, 2: "O"}  # for a program that knows its own kind of square
# What a program may be able to do beyond the first protocol, named in its ``capabilities`` file:
# ``own`` (``O`` in a row, ``own`` as a kind), ``origin``, ``daytime``, ``clouds``.
CAPABILITIES = frozenset({"own", "origin", "daytime", "clouds"})
_CAPS_FILE = "capabilities"  # next to the program; a program without it gets the first protocol
_CAPS_MOST = 1024  # bytes read of that file
_THEME_NAME = re.compile(r"[a-z0-9]{1,32}")
_THEME_DEFAULT = "wueste"
_TZ_DEFAULT = "Europe/Berlin"
_NOON = 720  # minutes: the time sent when no zone can be had
_ENV_KEYS = (
    "HOME",
    "XDG_RUNTIME_DIR",
    "XDG_DATA_HOME",
    "WAYLAND_DISPLAY",
    "DISPLAY",
    "LANG",
    "LC_ALL",
)
_PATH = "/usr/bin:/bin"
_POS_MOST = 47.9999  # a position is below 48


# --- finding the program ------------------------------------------------------------------------


def _uid() -> int | None:
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid is not None else None


def _candidate() -> Path | None:
    """The path asked for: ``COSMERGON_POV`` (absolute), else under ``XDG_DATA_HOME``."""
    explicit = os.environ.get("COSMERGON_POV", "")
    if explicit:
        return Path(explicit) if os.path.isabs(explicit) else None
    base = os.environ.get("XDG_DATA_HOME", "")
    if not base or not os.path.isabs(base):  # the XDG rule: a relative value is ignored
        base = os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / "cosmergon-pov" / "cosmergon-pov"


def _ours(info: os.stat_result, uid: int) -> bool:
    return info.st_uid in (uid, 0) and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


def _safe_file(wanted: Path, uid: int, *, executable: bool = False) -> Path | None:
    """The resolved file when it is a regular file of this user or root that group and others
    cannot write, in a directory of the same kind (and executable when asked); else None."""
    try:
        real = Path(os.path.realpath(wanted))
        file_info, dir_info = os.stat(real), os.stat(real.parent)
    except (OSError, ValueError):
        return None
    if not stat.S_ISREG(file_info.st_mode) or not stat.S_ISDIR(dir_info.st_mode):
        return None
    if executable and not os.access(real, os.X_OK):
        return None
    if not _ours(file_info, uid) or not _ours(dir_info, uid):
        return None
    return real


def program_path() -> Path | None:
    """The renderer's file, or None unless it is a safe thing to start.

    No search through ``PATH``. The file (after resolving links) is a regular, executable file
    of this user or root that group and others cannot write, in a directory of the same kind.
    """
    uid = _uid()
    wanted = _candidate()
    if uid is None or wanted is None:
        return None
    return _safe_file(wanted, uid, executable=True)


def theme_name() -> str | None:
    """The theme asked for: ``COSMERGON_POV_THEME`` (``[a-z0-9]{1,32}``), else ``wueste``.

    ``none`` is no theme. A value of any other shape (unset, empty, unsafe) counts as not set.
    """
    name = os.environ.get("COSMERGON_POV_THEME", "")
    if not _THEME_NAME.fullmatch(name):
        name = _THEME_DEFAULT
    return None if name == "none" else name


def theme_path(program: Path) -> Path | None:
    """``theme-<name>.wad`` next to the program, if it is safe by the program's own rules."""
    name, uid = theme_name(), _uid()
    if name is None or uid is None:
        return None
    return _safe_file(program.parent / f"theme-{name}.wad", uid)


def capabilities(program: Path) -> frozenset[str]:
    """What the program says it can: the words of ``capabilities`` next to it.

    The file must be safe like the program. No file, an unsafe one or an unreadable one: the
    empty set, i.e. the first protocol only (what an older program understands).
    """
    uid = _uid()
    real = None if uid is None else _safe_file(program.parent / _CAPS_FILE, uid)
    if real is None:
        return frozenset()
    try:
        with open(real, "rb") as handle:
            words = handle.read(_CAPS_MOST).decode("ascii", "replace").split()
    except OSError:
        return frozenset()
    return CAPABILITIES.intersection(words)


# --- the window follower ------------------------------------------------------------------------

_FOLLOW_FILE = "pov-follow.json"  # next to the place file; written by the follower program
_FOLLOW_MOST = 4096  # bytes read of that file


def _follow_pid(uid: int) -> int | None:
    """The pid in the follower's file, or None unless the file is ours and well formed."""
    place = pov_place.place_path()
    if place is None:
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(place.with_name(_FOLLOW_FILE), flags)
    except (OSError, ValueError):
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid:
            return None
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return None
        data = json.loads(os.read(fd, _FOLLOW_MOST))
    except (OSError, ValueError, RecursionError):  # deep nesting fits into 4096 bytes
        return None
    finally:
        os.close(fd)
    pid = data.get("pid") if isinstance(data, dict) else None
    return pid if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 else None


def follower_alive() -> bool:
    """True when the follower program (it lays the window over the placeholder) lives.

    It says so in ``$XDG_RUNTIME_DIR/cosmergon/pov-follow.json`` (a regular file of ours that
    group and others cannot write) with its ``pid``; that process must exist and be ours.
    Anything else, and any error, is False.
    """
    uid = _uid()
    if uid is None:
        return False
    pid = _follow_pid(uid)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, OverflowError):  # gone, not ours, or no such number
        return False
    return True


def window_allowed() -> bool:
    """May the program be started? With a follower, or when the window is asked for beside."""
    return os.environ.get("COSMERGON_POV_WINDOW") == "beside" or follower_alive()


# --- the translation ----------------------------------------------------------------------------
#
# Axes and angles (scope.py, read, not guessed):
#   * a square is grid[y][x] (Field.at, scope.py:249), the camera looks along (cos a, sin a)
#     (Pov.step, scope.py:288-291);
#   * a thing with ``across = ry * dx - rx * dy`` > 0 is drawn on the RIGHT of the picture
#     (Pov._things, scope.py:441-446: left = (across / away / _REACH + 1) / 2 * width).
#   So turning from the looking direction towards +y is a turn to the right: the scope's
#   y axis points to the viewer's right-hand side, i.e. SOUTH when the map is read with
#   x to the east. The program has y north and counter-clockwise = left, hence
#       program y = const - scope y,   program angle = - scope angle.
#   A thing straight ahead stays straight ahead: (cos a, sin a) -> (cos a, -sin a) = angle -a.


def _num(value: float, most: float) -> str:
    return f"{min(max(value, 0.0), most) + 0.0:.4f}"


def _angle(radians: float) -> str:
    return f"{(-math.degrees(radians)) % 360.0:.2f}"


class Window:
    """What the program was told last, and the lines that bring it up to date.

    The 48 x 48 window is a cut-out of the field around the camera, in whole squares. Squares
    outside the field are empty. ``update`` returns the lines for one state; ``reset`` forgets
    what was sent (lines were lost), so that the next ``update`` sends everything anew.
    """

    def __init__(self, caps: frozenset[str] = frozenset()) -> None:
        self._caps = caps  # what the program can beyond the first protocol (``capabilities``)
        self._origin: tuple[int, int] | None = None  # field square at the window's west, south
        self._rows: list[str] = []
        self._ids: dict[object, int] = {}
        self._text: dict[int, str] = {}  # id -> "kind x y angle" as last sent
        self._known: set[int] = set()  # ids the program may hold
        self._cooling: set[int] = set()  # freed in the last update, not yet to be given again
        self._free: set[int] = set(range(MAX_THINGS))
        self._cam = ""

    def reset(self) -> None:
        """The program may have missed lines: the next update is the whole window."""
        self._origin = None
        self._text, self._cam = {}, ""

    def update(self, sight: Sight | None) -> list[str]:
        """The lines that bring the program to ``sight``; empty when nothing changed."""
        if sight is None or not all(math.isfinite(v) for v in (sight.x, sight.y, sight.angle)):
            return []
        old = self._origin
        ox, oy = (None, None) if old is None else old
        origin = (
            _axis(sight.x, sight.side, sight.torus, ox),
            _axis(sight.y, sight.side, sight.torus, oy),
        )
        full = old != origin
        rows = _rows(sight, origin, "own" in self._caps)
        things = self._things(sight, origin)
        lines = [_origin_line(sight, origin)] if full and "origin" in self._caps else []
        lines += _all_rows(rows) if full else _changed_rows(self._rows, rows)
        lines += self._thing_lines(things, full)
        cam = f"cam {_cam(sight, origin)}"
        if full or cam != self._cam:
            lines.append(cam)
        self._origin, self._rows, self._cam = origin, rows, cam
        return lines

    def _things(self, sight: Sight, origin: tuple[int, int]) -> dict[object, tuple[str, str]]:
        """The things in the window, the nearest ``MAX_THINGS``: key -> (kind, "x y angle")."""
        found = []
        for thing in sight.things:
            at = _place(sight, origin, thing.x, thing.y)
            if at is not None and math.isfinite(thing.angle):
                far = (thing.x - sight.x) ** 2 + (thing.y - sight.y) ** 2
                found.append((far, repr(thing.key), thing, at))
        found.sort(key=lambda item: item[:2])
        out = {}
        for _, _, thing, (px, py) in found[:MAX_THINGS]:
            out[thing.key] = (thing.kind, f"{_num(px, _POS_MOST)} {_num(py, _POS_MOST)} "
                              f"{_angle(thing.angle)}")  # fmt: skip
        return out

    def _thing_lines(self, things: dict[object, tuple[str, str]], full: bool) -> list[str]:
        """``gone`` for those that left, ``thing`` for those new or changed (all when ``full``)."""
        self._free |= self._cooling  # ids freed by the last update are free again now
        self._cooling = set()
        for key in [k for k in self._ids if k not in things]:
            self._cooling.add(self._ids.pop(key))
        for key in things:
            if key not in self._ids:
                self._ids[key] = self._take()
        now = set(self._ids.values())
        lines = [f"gone {i}" for i in sorted(self._known - now)]
        self._known = now
        for key, (kind, where) in things.items():
            ident = self._ids[key]
            text = f"{kind} {where}"
            if full or self._text.get(ident) != text:
                lines.append(f"thing {ident} {text}")
                self._text[ident] = text
        for ident in [i for i in self._text if i not in now]:
            del self._text[ident]
        return lines

    def _take(self) -> int:
        """The lowest free id; a cooling one only when no other is left (``gone`` comes first)."""
        pool = self._free or self._cooling
        ident = min(pool)
        pool.discard(ident)
        return ident


def _axis(c: float, side: int, torus: bool, old: int | None) -> int:
    """Where the window starts on one axis: the old place while the camera has ``MARGIN``
    squares of window on both sides (or the field has no more there), else centred on it.

    The reach of a look is ``scope._FAR`` = 22 squares; the window is 48 wide, so the camera may
    stand between 22 and 26 squares from the west edge, and is put at 24 when it leaves that.
    """
    if not torus and side <= GRID:
        return 0
    if old is not None:
        rel = c - old
        low = rel >= MARGIN or (not torus and old <= 0)
        high = GRID - rel >= MARGIN or (not torus and old + GRID >= side)
        if low and high:
            return old
    centred = math.floor(c) - GRID // 2
    return centred if torus else min(max(centred, 0), side - GRID)


def _origin_line(sight: Sight, origin: tuple[int, int]) -> str:
    """``origin``: the world place of the window's square 0, 0 (it sets what a square looks like).

    World x is the field's x; world y points north, so it is the field's y negated (the window's
    row 0 is the field row ``origin y + GRID - 1``). On a torus the place is taken modulo the
    side, so that a square keeps its world place however often the camera went around.
    """
    x, y = origin[0], -(origin[1] + GRID - 1)
    if sight.torus:
        x, y = x % sight.side, y % sight.side - sight.side
    return f"origin {x} {y}"


def _rows(sight: Sight, origin: tuple[int, int], own: bool = False) -> list[str]:
    """The window's rows, south (program y 0) first, each 48 characters west to east."""
    ox, oy = origin
    chars = _CHARS_OWN if own else _CHARS
    return [
        "".join(chars.get(sight.at(ox + px, oy + GRID - 1 - py), ".") for px in range(GRID))
        for py in range(GRID)
    ]


def _all_rows(rows: list[str]) -> list[str]:
    return [f"row {py} {text}" for py, text in enumerate(rows)]


def _changed_rows(old: list[str], new: list[str]) -> list[str]:
    """``cell`` lines for a few changed squares of a row, one ``row`` line for more than four:
    a ``cell`` line is about 14 bytes, a ``row`` line 56."""
    lines = []
    for py, (before, now) in enumerate(zip(old, new, strict=True)):
        if before == now:
            continue
        diff = [px for px in range(GRID) if before[px] != now[px]]
        if len(diff) > _ROW_ABOVE:
            lines.append(f"row {py} {now}")
        else:
            lines += [f"cell {px} {py} {_NAMES[now[px]]}" for px in diff]
    return lines


def _place(sight: Sight, origin: tuple[int, int], x: float, y: float) -> tuple[float, float] | None:
    """A field place as the program's window place, or None when it lies outside the window."""
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    if sight.torus:  # the nearest copy of the thing to the camera
        n = sight.side
        x = sight.x + (x - sight.x + n / 2) % n - n / 2
        y = sight.y + (y - sight.y + n / 2) % n - n / 2
    px, py = x - origin[0], GRID - (y - origin[1])
    return (px, py) if 0.0 <= px < GRID and 0.0 <= py < GRID else None


def _cam(sight: Sight, origin: tuple[int, int]) -> str:
    px, py = sight.x - origin[0], GRID - (sight.y - origin[1])
    return f"{_num(px, _POS_MOST)} {_num(py, _POS_MOST)} {_angle(sight.angle)}"


# --- the process --------------------------------------------------------------------------------


def _chunks(lines: Sequence[str]) -> Iterator[bytes]:
    """The lines as pieces of whole lines, each up to ``PIPE_BUF`` bytes."""
    piece = b""
    for line in lines:
        data = line.encode("ascii", "replace")[:_LINE_MOST] + b"\n"
        if piece and len(piece) + len(data) > _PIPE_BUF:
            yield piece
            piece = b""
        piece += data
    if piece:
        yield piece


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _zone() -> ZoneInfo | None:
    """The zone the user chose in ``COSMERGON_POV_TZ``, else Berlin, else None.

    Never the system's zone and never a guess from the place: only an explicit choice.
    """
    for name in (os.environ.get("COSMERGON_POV_TZ", ""), _TZ_DEFAULT):
        try:
            if name:
                return ZoneInfo(name)
        except (KeyError, ValueError, OSError):  # not found (a KeyError), no data, bad key
            continue
    return None


class Sky:
    """The time of day and the cloud clock for the program: ``daytime`` once a minute,
    ``clouds`` once (at the start), both from one clock that tests may replace."""

    def __init__(self, clock: Callable[[], datetime] = _utc_now) -> None:
        self._clock = clock
        self._zone = _zone()
        self._minute: int | None = None
        self._clouds = False

    def forget(self) -> None:
        """Lines were lost: the next ``lines`` says everything again."""
        self._minute, self._clouds = None, False

    def lines(self, caps: frozenset[str]) -> list[str]:
        """``clouds`` the first time, ``daytime`` when the minute is not the one sent last."""
        out = []
        try:
            now = self._clock().astimezone(timezone.utc)
            if "clouds" in caps and not self._clouds:
                out.append(f"clouds {now.hour * 3600 + now.minute * 60 + now.second}")
                self._clouds = True
            minute = _NOON if self._zone is None else self._local_minute(now)
            if "daytime" in caps and minute != self._minute:
                out.append(f"daytime {minute}")
                self._minute = minute
        except (OSError, ValueError, OverflowError):  # a clock that fails sends nothing
            return out
        return out

    def _local_minute(self, now: datetime) -> int:
        local = now.astimezone(self._zone)
        return local.hour * 60 + local.minute


def _environment() -> dict[str, str]:
    """What a window program needs of the environment; the dashboard's may hold secrets."""
    env = {key: os.environ[key] for key in _ENV_KEYS if key in os.environ}
    env["PATH"] = _PATH
    return env


class Bridge:
    """The program as a child process, written to without ever waiting for it.

    The pipe is non-blocking. A write of up to ``PIPE_BUF`` bytes is whole or fails with EAGAIN,
    so a line is never cut. When the pipe is full the rest of the text is dropped, ``dropped``
    is set, and the next ``push`` sends the whole window anew. A broken pipe or an exited
    program ends the bridge for good: ``alive`` is False and nothing restarts it.
    """

    def __init__(self, path: Path, clock: Callable[[], datetime] = _utc_now) -> None:
        self.path = path
        self.caps = capabilities(path)  # fixed for the life of the bridge
        self.window = Window(self.caps)
        self.sky = Sky(clock)
        self.dropped = False
        self._proc: subprocess.Popen[bytes] | None = None
        self._owes_newline = False
        self._ended = False

    def start(self) -> bool:
        """Start the program; False (and the bridge is over) when that is not possible."""
        try:
            self._proc = subprocess.Popen(  # noqa: S603  # nosec B603 - no shell, path from program_path
                self._argv(),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=False,
                env=_environment(),
                bufsize=0,
            )
            if self._proc.stdin is None:
                raise OSError("no pipe")
            os.set_blocking(self._proc.stdin.fileno(), False)
        except (OSError, ValueError):
            self._ended = True
            self.close()
            return False
        return True

    def _argv(self) -> list[str]:
        theme = theme_path(self.path)
        argv = [str(self.path), "-pov-control"]
        return argv if theme is None else [*argv, "-pov-theme", str(theme)]

    def alive(self) -> bool:
        """Whether the program runs and its pipe stands."""
        proc = self._proc
        if proc is None or self._ended:
            return False
        if proc.poll() is not None:
            self._ended = True
        return not self._ended

    def push(self, sight: Sight | None) -> None:
        """Send what changed since the last push (everything after a drop)."""
        if not self.alive():
            return
        if self.dropped:
            self.window.reset()
            self.sky.forget()
            self.dropped = False
        if sight is None:
            return
        self.send(self.sky.lines(self.caps) + self.window.update(sight))

    def send(self, lines: Sequence[str]) -> bool:
        """Write the lines; False when some were dropped or the program is gone. Never raises."""
        proc = self._proc
        if proc is None or proc.stdin is None or not self.alive():
            return False
        fd = proc.stdin.fileno()
        try:
            if self._owes_newline:  # a short write cut a line: end it before anything else
                os.write(fd, b"\n")
                self._owes_newline = False
            for piece in _chunks(lines):
                done = os.write(fd, piece)
                if done != len(piece):  # not expected for up to PIPE_BUF bytes
                    self._owes_newline = True
                    raise BlockingIOError
        except BlockingIOError:
            self.dropped = True
            return False
        except (OSError, ValueError):  # broken pipe, closed file
            self._ended = True
            return False
        return True

    def close(self) -> None:
        """``quit``, close the pipe, wait a moment, then terminate, then kill. Never raises."""
        proc, self._ended = self._proc, True
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                self._farewell(proc.stdin.fileno())
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        for action, wait in ((None, 0.5), (proc.terminate, 0.3), (proc.kill, 0.2)):
            if _ended_within(proc, action, wait):
                return

    @staticmethod
    def _farewell(fd: int) -> None:
        try:
            os.write(fd, b"quit\n")
        except OSError:
            pass


def _ended_within(proc: subprocess.Popen[bytes], action: object, seconds: float) -> bool:
    """Do ``action`` (if any), then wait up to ``seconds`` for the end of the process."""
    try:
        if callable(action):
            action()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return True
            time.sleep(0.01)
        return proc.poll() is not None
    except Exception:  # never into the dashboard
        return False

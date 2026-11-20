"""The bridge to the native first-person renderer (cos20 #799, package P6).

When the program ``cosmergon-pov`` is installed, the dashboard starts it, keeps the view's area
empty (``pov_place``) and tells it what the view shows, as text lines on its stdin. The lines are
the control protocol of the program (``cosmergon-pov/COSMERGON.md``, "Control channel"): a fixed
window of 48 x 48 squares, x east, y NORTH, (0, 0) the south-west corner, angles in degrees,
0 = east, counter-clockwise.

Stdlib only, no Textual. Nothing here raises into the dashboard.
"""

from __future__ import annotations

import math
import os
import select
import stat
import subprocess  # nosec B404 - the one program of this module, started without a shell
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cosmergon_agent.scope import Sight

GRID = 48  # squares on a side of the program's window
MARGIN = 22  # squares: ceil(scope._FAR) = 22, how far a look reaches (the test compares both)
MAX_THINGS = 256  # ids 0..255 of the program
_LINE_MOST = 255
_ROW_ABOVE = 4  # more changed squares in a row than this: one ``row`` line is shorter
_PIPE_BUF = getattr(select, "PIPE_BUF", 512)  # a write up to this size is whole or not at all
_NAMES = {".": "empty", "#": "cell", "=": "wall", "T": "tower"}
_CHARS = {0: ".", 1: "#", 2: "#", 3: "=", 4: "T"}  # _posture: EMPTY, CELL, OWN, WALL, TOWER
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


def program_path() -> Path | None:
    """The renderer's file, or None unless it is a safe thing to start.

    No search through ``PATH``. The file (after resolving links) is a regular, executable file
    of this user or root that group and others cannot write, in a directory of the same kind.
    """
    uid = _uid()
    wanted = _candidate()
    if uid is None or wanted is None:
        return None
    try:
        real = Path(os.path.realpath(wanted))
        file_info, dir_info = os.stat(real), os.stat(real.parent)
    except (OSError, ValueError):
        return None
    if not stat.S_ISREG(file_info.st_mode) or not stat.S_ISDIR(dir_info.st_mode):
        return None
    if not os.access(real, os.X_OK) or not _ours(file_info, uid) or not _ours(dir_info, uid):
        return None
    return real


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

    def __init__(self) -> None:
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
        rows = _rows(sight, origin)
        things = self._things(sight, origin)
        lines = _all_rows(rows) if full else _changed_rows(self._rows, rows)
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


def _rows(sight: Sight, origin: tuple[int, int]) -> list[str]:
    """The window's rows, south (program y 0) first, each 48 characters west to east."""
    ox, oy = origin
    return [
        "".join(_CHARS.get(sight.at(ox + px, oy + GRID - 1 - py), ".") for px in range(GRID))
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

    def __init__(self, path: Path) -> None:
        self.path = path
        self.window = Window()
        self.dropped = False
        self._proc: subprocess.Popen[bytes] | None = None
        self._owes_newline = False
        self._ended = False

    def start(self) -> bool:
        """Start the program; False (and the bridge is over) when that is not possible."""
        try:
            self._proc = subprocess.Popen(  # noqa: S603  # nosec B603 - no shell, path from program_path
                [str(self.path), "-pov-control"],
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
            self.dropped = False
        self.send(self.window.update(sight))

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

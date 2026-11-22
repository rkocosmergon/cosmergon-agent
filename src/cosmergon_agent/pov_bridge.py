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
    from cosmergon_agent.scope import Sight, SightWorld

GRID = 48  # squares on a side of the program's window
MARGIN = 22  # squares: ceil(scope._FAR) = 22, how far a look reaches (the test compares both)
MAX_THINGS = 256  # ids 0..255 of the program
_LINE_MOST = 255
_ROW_ABOVE = 4  # more changed squares in a row than this: one ``row`` line is shorter
_PIPE_BUF = getattr(select, "PIPE_BUF", 512)  # a write up to this size is whole or not at all
_NAMES = {
    ".": "empty",
    "#": "cell",
    "=": "wall",
    "T": "tower",
    "O": "own",
    "R": "ring",
    "K": "alcove",
    "P": "shaftlo",
    "Q": "shafthi",
}
_CHARS = {0: ".", 1: "#", 2: "#", 3: "=", 4: "T"}  # _posture: EMPTY, CELL, OWN, WALL, TOWER
_CHARS_OWN = {**_CHARS, 2: "O"}  # for a program that knows its own kind of square
# What a program may be able to do beyond the first protocol, named in its ``capabilities`` file:
# ``own`` (``O`` in a row, ``own`` as a kind), ``origin``, ``daytime``, ``clouds``, ``world``
# (``world``, ``cube`` and ``nocube`` lines), ``kinds`` (the kinds of thing ``monster``, ``bird``
# and ``corpse``; a ``thing`` line may give a known id another kind) and ``ring`` (the kinds of
# square of the field's rim: ``R`` ring, ``K`` alcove, ``P`` shaftlo, ``Q`` shafthi in a row).
CAPABILITIES = frozenset({"own", "origin", "daytime", "clouds", "world", "kinds", "ring"})
_BUNKS = 16  # bunks on a wall of the rim
_SHAFT = (1, 2)  # the positions along a wall (from its start) of the two squares of a shaft
_LIKE_MARAUDER = frozenset({"monster", "bird"})  # what a program without ``kinds`` draws so
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
_GRID_MOST = 10_000  # a cube's grid position is within +-10,000 (the program's limit)
_DIM_LEAST, _DIM_MOST = 8, 1024  # a cube's edge in cells (the program's limit)
MAX_CUBES = 64  # ids 0..63 of the program's neighbour cubes


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
        self._world = _WorldTold()

    def reset(self) -> None:
        """The program may have missed lines: the next update is the whole window."""
        self._origin = None
        self._text, self._cam = {}, ""
        self._world.forget()

    def update(self, sight: Sight | None) -> list[str]:
        """The lines that bring the program to ``sight``; empty when nothing changed."""
        if sight is None or not all(math.isfinite(v) for v in (sight.x, sight.y, sight.angle)):
            return []
        old = self._origin
        ox, oy = (None, None) if old is None else old
        ring = "ring" in self._caps
        origin = (
            _axis(sight.x, sight.side, sight.torus, ox, ring),
            _axis(sight.y, sight.side, sight.torus, oy, ring),
        )
        full = old != origin
        rows = _rows(sight, origin, "own" in self._caps, ring)
        things = self._things(sight, origin)
        lines = self._world.lines(sight.world) if "world" in self._caps else []
        lines += [_origin_line(sight, origin)] if full and "origin" in self._caps else []
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
            kind = self._kind(thing.kind)
            at = _place(sight, origin, thing.x, thing.y)
            if kind is not None and at is not None and math.isfinite(thing.angle):
                far = (thing.x - sight.x) ** 2 + (thing.y - sight.y) ** 2
                found.append((far, repr(thing.key), thing, kind, at))
        found.sort(key=lambda item: item[:2])
        out = {}
        for _, _, thing, kind, (px, py) in found[:MAX_THINGS]:
            out[thing.key] = (kind, f"{_num(px, _POS_MOST)} {_num(py, _POS_MOST)} "
                              f"{_angle(thing.angle)}")  # fmt: skip
        return out

    def _kind(self, kind: str) -> str | None:
        """The kind the program is told, or None for a thing it must not be told of.

        Without ``kinds`` a monster or a bird is a marauder (the only figure it has), and a
        body that is down is not sent at all: a lying body must not show up standing.
        """
        if "kinds" in self._caps:
            return kind
        if kind == "corpse":
            return None
        return "marauder" if kind in _LIKE_MARAUDER else kind

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


def _whole(*values: object) -> bool:
    return all(isinstance(v, int) and not isinstance(v, bool) for v in values)


def cube_ok(x: int, y: int, z: int, dim: int) -> bool:
    """Whether the program takes a cube at this grid position with this edge."""
    return (
        _whole(x, y, z, dim)
        and max(abs(x), abs(y), abs(z)) <= _GRID_MOST
        and _DIM_LEAST <= dim <= _DIM_MOST
    )


class _WorldTold:
    """Where the field lies in the world and the neighbour cubes, as the program was told.

    ``lines`` gives ``world`` when cube, level or edge changed, then ``nocube`` for neighbours
    that went and ``cube`` for those new or moved. A neighbour keeps its id while it is among
    the nearest ``MAX_CUBES``. A value the program would reject is not sent at all.
    """

    def __init__(self) -> None:
        self._told: tuple[int, ...] | None = None
        self._ids: dict[object, int] = {}
        self._held: set[int] = set()  # ids the program may hold a cube under
        self._sent: dict[int, tuple[int, int, int, int]] = {}

    def forget(self) -> None:
        """Lines were lost: everything is said again (cubes that went are removed then)."""
        self._told, self._sent = None, {}

    def lines(self, world: SightWorld | None) -> list[str]:
        if world is None:
            return []
        cube = (0, 0, 0) if world.cube is None else world.cube  # the program's own default
        here = (*cube, world.level, world.dim)
        level_ok = _whole(world.level) and 0 <= world.level < world.dim
        if not (cube_ok(*cube, world.dim) and level_ok):
            return []
        out = []
        if here != self._told:
            out.append("world " + " ".join(str(v) for v in here))
            self._told = here
        wanted = self._nearest(world, cube) if world.cube is not None else {}
        return out + self._cube_lines(wanted)

    @staticmethod
    def _nearest(world: SightWorld, cube: tuple[int, ...]) -> dict[object, tuple[int, ...]]:
        """The neighbours the program takes, the nearest ``MAX_CUBES`` by grid distance."""
        found: dict[object, tuple[int, ...]] = {}
        for key, *place in world.neighbours:
            if len(place) == 4 and cube_ok(*place) and tuple(place[:3]) != cube:
                found.setdefault(key, tuple(place))
        cx, cy, cz = cube

        def distance(key: object) -> tuple[int, str]:
            x, y, z = found[key][:3]
            return (x - cx) ** 2 + (y - cy) ** 2 + (z - cz) ** 2, repr(key)

        order = sorted(found, key=distance)
        return {key: found[key] for key in order[:MAX_CUBES]}

    def _cube_lines(self, wanted: dict[object, tuple[int, ...]]) -> list[str]:
        for key in [k for k in self._ids if k not in wanted]:
            del self._ids[key]
        taken = set(self._ids.values())
        free = iter(i for i in range(MAX_CUBES) if i not in taken)
        for key in wanted:
            if key not in self._ids:
                self._ids[key] = next(free)
        now = set(self._ids.values())
        lines = [f"nocube {i}" for i in sorted(self._held - now)]
        self._held = now
        for key, place in wanted.items():
            ident = self._ids[key]
            if self._sent.get(ident) != place:
                lines.append(f"cube {ident} " + " ".join(str(v) for v in place))
                self._sent[ident] = place  # type: ignore[assignment]
        for ident in [i for i in self._sent if i not in now]:
            del self._sent[ident]
        return lines


def _axis(c: float, side: int, torus: bool, old: int | None, ring: bool = False) -> int:
    """Where the window starts on one axis: the old place while the camera has ``MARGIN``
    squares of window on both sides (or the field has no more there), else centred on it.

    The reach of a look is ``scope._FAR`` = 22 squares; the window is 48 wide, so the camera may
    stand between 22 and 26 squares from the west edge, and is put at 24 when it leaves that.

    With ``ring`` the window may reach one square beyond the field on each side (the squares of
    its rim), and a field of at most ``GRID - 2`` squares lies in the middle of it.
    """
    first, last = (-1, side + 1 - GRID) if ring else (0, side - GRID)  # the window's start
    if not torus and (side <= GRID if not ring else last < first):
        return (first + last) // 2 if ring else 0
    if old is not None:
        rel = c - old
        low = rel >= MARGIN or (not torus and old <= first)
        high = GRID - rel >= MARGIN or (not torus and old >= last)
        if low and high:
            return old
    centred = math.floor(c) - GRID // 2
    return centred if torus else min(max(centred, first), last)


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


def _rim(side: int, x: int, y: int) -> str | None:
    """The row character of a square of the field's rim, or None for any other square.

    The rim is the ring of squares at index -1 and ``side`` around the field (corners included).
    A wall has ``_BUNKS`` bunks at ``i * spacing + spacing // 2`` with ``spacing = side // 16``
    (none when that is 0) and a shaft on the positions 1 and 2 from its start; a shaft beats a
    bunk, and the square right beside a shaft is plain wall even where the server has a bunk. A position counts along the field's x on the north (y = -1) and south (y = side) walls
    and along its y on the west (x = -1) and east (x = side) walls. The program's ``shaftlo`` is
    the square with the lower program coordinate along the wall (x east, y north); the field's y
    points south, so on the east and west walls that is position 2, on the others position 1.
    """
    if not (-1 <= x <= side and -1 <= y <= side):
        return None
    if 0 <= x < side and 0 <= y < side:
        return None
    along = x if y in (-1, side) and 0 <= x < side else y if 0 <= y < side else None
    if along is None:
        return "R"  # a corner
    low, high = _SHAFT
    if along in _SHAFT:
        flipped = x in (-1, side)  # the field's y runs against the program's
        return "P" if along == (high if flipped else low) else "Q"
    if along in (low - 1, high + 1):
        return "R"  # no bunk right beside a shaft: the two would open into each other
    spacing = side // _BUNKS
    if spacing and along % spacing == spacing // 2 and along // spacing < _BUNKS:
        return "K"
    return "R"


def _rows(
    sight: Sight, origin: tuple[int, int], own: bool = False, ring: bool = False
) -> list[str]:
    """The window's rows, south (program y 0) first, each 48 characters west to east.

    With ``ring`` the squares of the field's rim that lie in the window are drawn (not on a
    torus, which has no edge); no square of the field is ever replaced by one.
    """
    ox, oy = origin
    chars = _CHARS_OWN if own else _CHARS
    rim = ring and not sight.torus

    def char(x: int, y: int) -> str:
        found = _rim(sight.side, x, y) if rim else None
        return found or chars.get(sight.at(x, y), ".")

    return ["".join(char(ox + px, oy + GRID - 1 - py) for px in range(GRID)) for py in range(GRID)]


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

"""A moving, strongly reduced picture of the world for the terminal (cos20 #789).

The camera looks through the eyes of a marauder on a field: living cells stand
as blocks, walls half as high, towers above them, other marauders and loot boxes between
them. Every surface is covered with characters; which ones and how bright depends on the side
the light falls on and on how far away it is.

The way of drawing follows "Static City (terminal edition)" by sumit kumar
(https://github.com/sumit-official/ascii-city-term, MIT License, Copyright (c) 2026 sumit
kumar): rays through a grid, a texture of characters per surface, brightness in steps by
distance and side. Here one ray per column is enough, because the eye never tilts.

The drawer knows neither Textual nor the network: ``Pov.frame(w, h)`` gives rows of
``(text, keys)`` with one style key per cell. ``Stream`` reads the world beside it, ``Live``
follows a marauder in it, ``Flight`` flies by itself, ``Scope`` holds the cams the dashboard offers.
"""

from __future__ import annotations

import dataclasses
import itertools
import logging
import math
import random
import re
import threading
import time
import uuid
from collections.abc import Callable, Collection
from typing import Any

from cosmergon_agent import _posture, pov_bridge
from cosmergon_agent._posture import CELL, EMPTY, OWN, TOWER, WALL  # what stands on a square

# Sizes in cells, as the browser's first-person view has them (marauder.js: cell 4 m, eye
# 1.28 m, body 1.75 m, loot box 1.4 m); wall = half a storey (founder, 17.11.2026); tower =
# shaft 0.95 storey plus deck and battlements (iso/src/ausbau.js).
EYE = 0.32
HEIGHT = {CELL: 1.0, OWN: 1.0, WALL: 0.5, TOWER: 0.95}
_HIGHEST = max(HEIGHT.values())
_CEILING = 1.0  # a level is one cell high (marauder.js: floor to ceiling 4 m)
_MARAUDER, _LOOT = (0.44, 0.25), (0.35, 0.35)  # height, width
_REACH = 0.8  # half the picture's width at distance 1: an angle of view of about 77 degrees
_FAR = 22.0  # cells a look reaches (the browser's fog ends at 20)
_DEPTH_STEP = 4.5  # cells per step of darkness
_LIGHT = (0.857, 0.514)  # where the light comes from, seen from above (marauder.js: 50, 30)
_FEELERS = (-0.9, -0.45, 0.0, 0.45, 0.9)  # where the walker looks for its way, radians
_TICK_STEPS = 48  # the field lives one generation every this many steps

_NIGHT = "#0d1117"
_LIGHT_LEFT = (1.0, 0.75, 0.52, 0.34, 0.2)  # how much light is left, step by step
# One colour per kind of thing; the step of darkness makes five of each.
_COLORS = {
    "cell": "#8fa8dc",
    "own": "#5ef2c8",
    "wall": "#b48ade",
    "tower": "#e07a5f",
    "ground": "#6b7280",
    "ceiling": "#56607a",
    "marauder": "#ff5a3a",
    "loot": "#e8c547",
}
_KIND_COLOR = {CELL: "cell", OWN: "own", WALL: "wall", TOWER: "tower"}

logger = logging.getLogger(__name__)

_RUN = re.compile(r"(.)\1*", re.DOTALL)


def palette(
    theme: dict[str, str] | None = None, night: str = _NIGHT
) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    """Every style the picture uses: key → (foreground, background); name → its five keys.

    ``theme`` replaces colours by name (the names of ``_COLORS``), ``night`` is the surface
    behind everything. The keys do not depend on the colours.
    """
    colors: dict[str, tuple[str, str]] = {}
    names: dict[str, str] = {}
    for name, color in {**_COLORS, **(theme or {})}.items():
        rgb = [int(color[i : i + 2], 16) for i in (1, 3, 5)]
        keys = ""
        for light in _LIGHT_LEFT:
            key = chr(0x30 + len(colors))
            colors[key] = ("#" + "".join(f"{int(v * light):02x}" for v in rgb), night)
            keys += key
        names[name] = keys
    return colors, names


PALETTE, _KEYS = palette()
_BLANK = _KEYS["ground"][-1]
CAPTION_KEY = _KEYS["loot"][0]
_SPARSE = frozenset(".:+'`,-")  # texture that covers little of its cell: a step darker as a block
_DIM = {k: keys[min(i + 1, len(keys) - 1)] for keys in _KEYS.values() for i, k in enumerate(keys)}


def _halves(text: str, keys: str) -> str:
    """One row of the picture as colours only: the key per cell, a blank where nothing is."""
    return "".join(
        " " if c == " " else _DIM[k] if c in _SPARSE else k for c, k in zip(text, keys, strict=True)
    )


def blocks(rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """A picture of twice the height folded to half blocks: per terminal row the keys of its
    upper and of its lower half. Twice the resolution in height; the texture of signs is gone."""
    return [(_halves(*rows[i]), _halves(*rows[i + 1])) for i in range(0, len(rows) - 1, 2)]


def _hash(a: int, b: int, c: int = 0) -> int:
    """A fixed number below 1000 for three whole numbers: the same spot looks the same."""
    h = (a * 374761393) ^ (b * 668265263) ^ (c * 1274126177)
    h = ((h ^ (h >> 13)) * 1274126177) & 0xFFFFFFFF
    return (h ^ (h >> 16)) % 1000


_GROUND = [["." if _hash(a, b) < 500 else ":" for b in range(64)] for a in range(64)]


def _surface(
    kind: int, spot: int, side: int, u: float, z: float, wide: float, tall: float
) -> tuple[str, int]:
    """The character on a surface at (u along it, z up it), and how much darker it is.

    ``wide`` and ``tall`` say how many columns and rows one cell covers here. Close by, an edge
    stays one column, a storey line one row, and a window shows the dark wall around it; far
    away every character counts and nothing is left out.
    """
    if kind == WALL:
        course = int(z * 8)
        if z * 8 - course < 0.25:
            return "=", 1
        return ("#", 0) if (int(u * 4) + course) % 2 else (":", 1)
    if kind == TOWER:
        if u < 0.08 or u > 0.92:
            return "|", 0
        return ("I", 0) if int(u * 5) % 2 == 0 else (".", 2)
    edge = min(0.06, 1.2 / wide)
    if u < edge or u > 1 - edge:
        return "|", 0
    level = z * 3
    storey = int(level)
    if level - storey < min(0.2, 3.6 / tall):
        return "=", 2
    col = int(u * 3)
    lit = _hash(spot, side * 7 + col, storey) < 520
    if wide >= 9 and not (0.2 < u * 3 - col < 0.8 and 0.4 < level - storey < 0.85):
        return " ", 0  # the wall between the windows
    if lit:
        return "OXZ8"[_hash(col, storey, spot) % 4], 0
    return ".", 2


def _rect(value: Any) -> tuple[int, int, int, int]:
    """A half-open rectangle of the world's answer; anything else is an empty one."""
    try:
        x0, y0, x1, y1 = (int(v) for v in value)
    except (TypeError, ValueError):
        return 0, 0, 0, 0
    return x0, y0, x1, y1


_SIDE_LEAST = 8  # squares: the smallest field the picture is built for
_SIDE_MOST = 1024  # squares: eight times the widest field of the world (128)


class Field:
    """A made-up field: a square torus of living cells, some walls and towers, things on it."""

    _SHAPES = (
        ((0, 0), (1, 0), (0, 1), (1, 1)),  # block
        ((0, 0), (1, 0), (2, 0)),  # blinker
        ((1, 0), (2, 1), (0, 2), (1, 2), (2, 2)),  # glider
        ((1, 0), (2, 0), (0, 1), (3, 1), (1, 2), (2, 2)),  # beehive
    )

    def __init__(self, seed: int = 7, side: int = 48) -> None:
        rng = random.Random(seed)  # nosec B311 — a made-up field for the probe, no secret
        self.side = side
        self.torus = True  # the made-up field has no edge; a field of the world has
        self.grid = [[EMPTY] * side for _ in range(side)]
        for _ in range(side * side // 60):
            x, y, kind = rng.randrange(side), rng.randrange(side), rng.choice((CELL, CELL, OWN))
            for dx, dy in rng.choice(self._SHAPES):
                self.grid[(y + dy) % side][(x + dx) % side] = kind
        for _ in range(side // 8):
            x, y, along = rng.randrange(side), rng.randrange(side), rng.random() < 0.5
            for i in range(rng.randint(4, 9)):
                self.grid[(y + (0 if along else i)) % side][(x + (i if along else 0)) % side] = WALL
            self.grid[y][x] = TOWER
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                self.grid[side // 2 + dy][side // 2 + dx] = EMPTY
        # Things that stand on the ground: (x, y, heading or None for a loot box).
        self.marauders = [
            [rng.uniform(0, side), rng.uniform(0, side), rng.uniform(0, 6.28)] for _ in range(14)
        ]
        self.loot = [(rng.uniform(0, side), rng.uniform(0, side)) for _ in range(18)]

    @classmethod
    def from_level(cls, level: dict[str, Any]) -> Field:
        """A field as the world's public spectator route gives it: cells, walls, towers, loot."""
        # The side is the world's number; a field of side n is n * n squares (0.34.3).
        field = cls(side=min(max(_SIDE_LEAST, int(level.get("field_width") or 128)), _SIDE_MOST))
        n, field.torus = field.side, False
        field.grid = [[EMPTY] * n for _ in range(n)]
        for at in level.get("cells") or {}:
            x, _, y = at.partition(",")
            field._put(int(x), int(y), int(x) + 1, int(y) + 1, CELL)
        built = level.get("ausbau") or {}
        for wall in built.get("walls") or []:
            if wall.get("active"):
                field._put(*_rect(wall.get("rect")), WALL)
                field._put(*_rect(wall.get("door")), EMPTY)
        for tower in built.get("towers") or []:
            if tower.get("active"):
                field._put(*_rect(tower.get("rect")), TOWER)
        field.loot = [(b["x"] + 0.5, b["y"] + 0.5) for b in level.get("loot_boxes") or []]
        field.marauders = []
        return field

    def _put(self, x0: int, y0: int, x1: int, y1: int, kind: int) -> None:
        """Set the squares of the half-open rectangle; what lies outside the field is left out."""
        for y in range(max(0, y0), min(self.side, y1)):
            for x in range(max(0, x0), min(self.side, x1)):
                self.grid[y][x] = kind

    def tick(self) -> None:
        """One generation of Conway's rules; walls stay, a new cell takes its neighbours' kind."""
        n, old = self.side, self.grid
        new = [row[:] for row in old]
        for y in range(n):
            rows = (old[y - 1], old[y], old[(y + 1) % n])
            for x in range(n):
                here = old[y][x]
                if here in (WALL, TOWER):
                    continue
                around = [r[i] for r in rows for i in (x - 1, x, (x + 1) % n)]
                alive = sum(1 for k in around if k in (CELL, OWN)) - (here != EMPTY)
                if here == EMPTY and alive == 3:
                    new[y][x] = OWN if around.count(OWN) >= 2 else CELL
                elif here != EMPTY and alive not in (2, 3):
                    new[y][x] = EMPTY
        self.grid = new

    def free(self, x: float, y: float) -> bool:
        """Whether nothing stands on the square under (x, y)."""
        return self.at(math.floor(x), math.floor(y)) == EMPTY

    def at(self, x: int, y: int) -> int:
        """What stands on a square; beyond the edge of a field of the world there is nothing."""
        n = self.side
        if self.torus:
            return self.grid[y % n][x % n]
        return self.grid[y][x] if 0 <= x < n and 0 <= y < n else EMPTY

    def wander(self) -> None:
        """Every marauder a little further; it turns where something stands in its way."""
        for m in self.marauders:
            nx, ny = m[0] + 0.05 * math.cos(m[2]), m[1] + 0.05 * math.sin(m[2])
            if self.free(nx, ny):
                m[0], m[1] = nx % self.side, ny % self.side
            else:
                m[2] += 1.3


@dataclasses.dataclass(frozen=True)
class SightThing:
    """A marauder or a loot box as an outside reader sees it: a stable key, a place, a facing."""

    key: Any
    # "marauder", "monster", "bird" (a thief bird), "loot", or "corpse" for a body that is down
    # (of any kind: it lies where it fell and does not turn, angle 0). The key stays the same
    # when a body goes down, so a reader sees the same thing change its kind.
    kind: str
    x: float
    y: float
    angle: float  # radians, as the camera's: x = cos, y = sin


@dataclasses.dataclass(frozen=True)
class SightWorld:
    """Where the shown field lies in the world, for a reader outside (cos20 #799).

    ``level`` is the field's level in its cube and ``dim`` the cube's edge in cells. ``cube`` is
    the cube's grid position in its room, ``None`` while it is not known; ``neighbours`` are the
    other cubes of the room as ``(key, x, y, z, dim)``. ``Stream.locate`` reads them from the
    world (the cube's meta and its room); a caller that knows better sets ``Live.places``.
    """

    level: int
    dim: int
    cube: tuple[int, int, int] | None = None
    neighbours: tuple[tuple[Any, int, int, int, int], ...] = ()


@dataclasses.dataclass(frozen=True)
class Sight:
    """What the view shows, read only: the squares, the camera and the things (cos20 #799).

    ``at(x, y)`` is ``Field.at``: what stands on a square (EMPTY, CELL, OWN, WALL, TOWER).
    ``world`` is where the field lies in the world, ``None`` where that is not known.
    """

    side: int
    torus: bool
    at: Callable[[int, int], int]
    x: float
    y: float
    angle: float
    things: tuple[SightThing, ...]
    world: SightWorld | None = None
    lift: float = 0.0  # metres the camera's body stands above the floor of its level (in a shaft)


@dataclasses.dataclass(frozen=True)
class _Being:
    """A body of the stream the picture here does not draw but a reader outside sees."""

    key: Any
    kind: str  # "monster" or "bird"
    x: float
    y: float
    angle: float
    downed: bool


class Pov:
    """The camera "through its eyes": a marauder crosses the field and looks ahead."""

    def __init__(self, field: Field | None = None) -> None:
        self.field = field or Field()
        self.x = self.y = self.field.side / 2 + 0.5
        while not self.field.free(self.x, self.y):
            self.x += 1.0
        self.angle = 0.3
        self.keys: list[Any] = []  # who the field's marauders are, one key each (when known)
        # The reader outside also sees what the drawer here does not: bodies that are no
        # marauder (``foes``) and which of the marauders are down (``down``, by key).
        self.foes: list[_Being] = []
        self.down: frozenset[Any] = frozenset()
        self.traits: dict[Any, tuple[str, bool]] = {}  # id -> (sight kind, down) of the report
        self.step_no = 0
        self.tilt = 0.0  # the look up (below zero) or down, as a share of the picture's height
        self._turn = 0.0
        self._flat_rows: list[tuple[float, str, bool] | None] = []

    def step(self) -> None:
        """A little further: towards the widest way, walking while the way is free."""
        looks = [(self._free(off), off) for off in _FEELERS]
        ahead = looks[len(looks) // 2][0]
        _, want = max(looks, key=lambda look: min(look[0], 6.0) - 1.0 * abs(look[1]))
        self._turn += (0.12 * want - self._turn) * 0.25
        self.angle += self._turn
        if ahead > 1.2:
            self.x += 0.12 * math.cos(self.angle)
            self.y += 0.12 * math.sin(self.angle)
        self.field.wander()
        self.step_no += 1
        if self.step_no % _TICK_STEPS == 0:
            self.field.tick()

    def sight(self) -> Sight:
        """The field, the camera and the things now, for a reader outside (read only)."""
        marauders = self.field.marauders
        own = len(self.keys) == len(marauders)
        things = []
        for i, m in enumerate(marauders):
            key = self.keys[i] if own else i
            lying = own and key in self.down
            kind, angle = ("corpse", 0.0) if lying else ("marauder", m[2])
            things.append(SightThing(("m", key), kind, m[0], m[1], angle))
        for b in self.foes:
            kind, angle = ("corpse", 0.0) if b.downed else (b.kind, b.angle)
            things.append(SightThing(("m", b.key), kind, b.x, b.y, angle))
        seen: dict[tuple[float, float], int] = {}
        for x, y in self.field.loot:  # a loot box never moves: its place is its identity
            seen[(x, y)] = n = seen.get((x, y), 0) + 1
            things.append(SightThing(("l", x, y, n), "loot", x, y, 0.0))
        field = self.field
        return Sight(field.side, field.torus, field.at, self.x, self.y, self.angle, tuple(things))

    def _free(self, off: float) -> float:
        """How far the way is free in a direction, in cells."""
        a = self.angle + off
        for _, entry, _, _, kind in self._walk(math.cos(a), math.sin(a)):
            if kind:
                return float(entry)
        return _FAR

    def _walk(self, vx: float, vy: float) -> Any:
        """The squares a look passes, nearest first: (square, entry, side, facing, kind)."""
        at = self.field.at
        mx, my = math.floor(self.x), math.floor(self.y)
        step_x, step_y = (1 if vx > 0 else -1), (1 if vy > 0 else -1)
        per_x, per_y = abs(1 / vx) if vx else 1e9, abs(1 / vy) if vy else 1e9
        at_x = ((mx + 1 - self.x) if vx > 0 else (self.x - mx)) * per_x
        at_y = ((my + 1 - self.y) if vy > 0 else (self.y - my)) * per_y
        while True:
            if at_x < at_y:
                entry, side, facing = at_x, 0, -step_x * _LIGHT[0]
                at_x += per_x
                mx += step_x
            else:
                entry, side, facing = at_y, 1, -step_y * _LIGHT[1]
                at_y += per_y
                my += step_y
            if entry > _FAR:
                return
            yield mx * 8192 + my, entry, side, facing, at(mx, my)

    def _mid(self, height: int) -> float:
        """The row of the horizon: the middle, moved by the look up or down."""
        return height / 2 - self.tilt * height

    def frame(self, width: int, height: int) -> list[tuple[str, str]]:
        """The picture now, ``height`` rows of ``(text, keys)``, each ``width`` cells long."""
        if width < 1 or height < 1:
            return []
        scale = width * 0.3125  # rows per cell of height at distance 1; a cell is twice as tall
        dx, dy = math.cos(self.angle), math.sin(self.angle)
        self._flat_rows = self._rows(height, scale)
        cols = []
        depth = []
        for x in range(width):
            reach = (2 * (x + 0.5) / width - 1) * _REACH
            text, keys, first = self._column(dx - dy * reach, dy + dx * reach, height, scale, x)
            cols.append((text, keys))
            depth.append(first)
        self._things(cols, depth, width, height, scale)
        return list(
            zip(
                map("".join, zip(*(c[0] for c in cols), strict=True)),
                map("".join, zip(*(c[1] for c in cols), strict=True)),
                strict=True,
            )
        )

    def _column(
        self, vx: float, vy: float, height: int, scale: float, x: int
    ) -> tuple[list[str], list[str], float]:
        """One column of the picture, top to bottom, and how far its first block is away."""
        mid = self._mid(height)
        text, keys = [" "] * height, [_BLANK] * height
        stretch = math.hypot(vx, vy)
        bottom, first = height, _FAR
        for spot, entry, side, facing, kind in self._walk(vx, vy):
            entry = max(entry, 1e-3)  # standing exactly on a line between two squares
            if bottom <= mid - scale * (_HIGHEST - EYE) / entry - 0.5:
                break  # nothing further away can reach above what is drawn
            if not kind:
                continue
            first = min(first, entry)
            foot = min(bottom, max(0, int(mid + scale * EYE / entry - 0.5) + 1))
            self._flat(text, keys, foot, bottom, vx, vy)
            top = max(0, int(mid - scale * (HEIGHT[kind] - EYE) / entry - 0.5) + 1)
            along = (self.y + entry * vy) if side == 0 else (self.x + entry * vx)
            u = along - math.floor(along)
            turned = 0 if facing > 0.7 else 1 if facing > 0 else 2  # away from the light
            dark = int(entry * stretch / _DEPTH_STEP) + turned
            shades = _KEYS[_KIND_COLOR[kind]]
            wide, tall = scale * 3.2 / entry, scale / entry  # columns and rows per cell here
            for row in range(top, foot):
                z = EYE + entry * (mid - row - 0.5) / scale
                ch, more = _surface(kind, spot, side, u, z, wide, tall)
                text[row] = ch if facing > 0 else ch.lower()
                keys[row] = shades[min(4, dark + more)]
            bottom = min(bottom, top)
        self._flat(text, keys, 0, bottom, vx, vy)
        return text, keys, first

    def _rows(self, height: int, scale: float) -> list[tuple[float, str, bool] | None]:
        """Per row what a look at the flat ground or ceiling meets: (reach, key, is ground).

        The same for every column of one picture, so it is worked out once. ``None``: the row
        looks further than a look reaches, or lies on the horizon.
        """
        mid = self._mid(height)
        rows: list[tuple[float, str, bool] | None] = []
        for row in range(height):
            off = row + 0.5 - mid
            high = EYE if off > 0 else _CEILING - EYE
            reach = high * scale / abs(off) if off else _FAR + 1
            shades = _KEYS["ground" if off > 0 else "ceiling"]
            key = shades[min(4, int(reach / _DEPTH_STEP))]
            rows.append((reach, key, off > 0) if reach <= _FAR else None)
        return rows

    def _flat(
        self, text: list[str], keys: list[str], start: int, end: int, vx: float, vy: float
    ) -> None:
        """Ground and ceiling in rows ``start`` to ``end``: they lie on the field, not the screen.

        The ground is dotted; of the ceiling only the joints between the cells above show, a
        grid that runs to the horizon.
        """
        x0, y0, ground, rows = self.x, self.y, _GROUND, self._flat_rows
        for row in range(start, end):
            hit = rows[row]
            if hit is None:
                continue
            reach, key, is_ground = hit
            gx, gy = x0 + vx * reach, y0 + vy * reach
            if is_ground:
                text[row] = ground[int(gx * 3) & 63][int(gy * 3) & 63]
            elif int(gx * 16) & 15 and int(gy * 16) & 15:
                continue
            else:
                text[row] = "+"
            keys[row] = key

    def _things(
        self,
        cols: list[tuple[list[str], list[str]]],
        depth: list[float],
        width: int,
        height: int,
        scale: float,
    ) -> None:
        """Marauders and loot boxes, where no block stands in front of them."""
        dx, dy = math.cos(self.angle), math.sin(self.angle)
        n = self.field.side
        things = [(m[0], m[1], _MARAUDER, "marauder") for m in self.field.marauders]
        things += [(x, y, _LOOT, "loot") for x, y in self.field.loot]
        for tx, ty, (tall, wide), name in things:
            rx, ry = tx - self.x, ty - self.y
            if self.field.torus:
                rx, ry = (rx + n / 2) % n - n / 2, (ry + n / 2) % n - n / 2
            away, across = rx * dx + ry * dy, ry * dx - rx * dy
            if not 0.3 < away < _FAR:
                continue
            per_cell = width / (2 * _REACH * away)
            mid = self._mid(height)
            left = (across / away / _REACH + 1) / 2 * width - wide * per_cell / 2
            top = max(0, int(mid - scale * (tall - EYE) / away - 0.5) + 1)
            foot = min(height, int(mid + scale * EYE / away - 0.5) + 1)
            shade = _KEYS[name][min(4, int(away / _DEPTH_STEP))]
            right = round(left + max(1.0, wide * per_cell))
            for x in range(max(0, round(left)), min(width, right)):
                if away >= depth[x]:
                    continue
                for row in range(top, foot):
                    z = EYE + away * (mid - row - 0.5) / scale
                    cols[x][0][row] = _thing_char(name, z / tall)
                    cols[x][1][row] = shade


# Body radii in metres (cos20 backend/app/core/movement_profiles.py, ``hitbox_radius``): the
# server keeps living bodies apart by the sum of two of them.
_RADIUS_M = {"marauder": 0.6, "monster": 0.9, "bird": 0.8}


def _kept_off(
    spot: tuple[float, float],
    straight: tuple[float, float],
    living: list[tuple[float, float, float]],
) -> tuple[float, float]:
    """The point of the way from ``straight`` to ``spot`` that is the last one not nearer to
    any body of ``living`` (``(x, y, reach)``) than ``straight`` is or its ``reach``.

    A spline through the reports swings out a little between them; where that would put the
    camera into a body the server holds apart, it stops at the edge of that body's circle.
    """
    dx, dy = spot[0] - straight[0], spot[1] - straight[1]
    a = dx * dx + dy * dy
    share = 1.0
    for bx, by, reach in living:
        fx, fy = straight[0] - bx, straight[1] - by
        near = min(reach, math.hypot(fx, fy))
        toward = fx * dx + fy * dy
        disc = toward * toward - a * max(0.0, fx * fx + fy * fy - near * near)
        if a > 0 and disc > 0:
            entry = (-toward - math.sqrt(disc)) / a  # where the way meets the circle
            if -1e-9 <= entry < share:
                share = max(entry, 0.0)
    return straight[0] + dx * share, straight[1] + dy * share


_Report = tuple[float, dict[str, Any]]  # seconds, {id: (x, y, vx, vy)} in cells
_METRES = _posture.CELL_M  # a cell is 4 m (marauder.js CELL_SIZE); the stream speaks in metres


def _in_cells(body: dict[str, Any]) -> tuple[float, float, float, float]:
    """Where a body of the stream stands, in cells, and where it moves."""
    return body["x"] / _METRES, body["y"] / _METRES, body["vx"], body["vy"]


class Follow(Pov):
    """The camera in the head of a real marauder: where it walks, the picture walks.

    ``track`` is what the world's entity stream said, oldest first: ``(seconds, {id: (x, y,
    vx, vy)})`` in cells. Between two reports the camera glides; it looks where the marauder
    moves and keeps its look while it stands.
    """

    def __init__(self, field: Field, track: list[tuple[float, dict[str, Any]]], hero: str) -> None:
        super().__init__(field)
        self.track, self.hero = track, hero
        self.clock = track[0][0] if track else 0.0
        self._at = 0
        field.marauders = []
        self.advance(0.0)

    def step(self) -> None:
        self.advance(0.125)

    def advance(self, seconds: float) -> None:
        """Move the picture on by ``seconds`` of the world's time."""
        self.step_no += 1
        if len(self.track) < 2:
            return
        self.clock += seconds
        if self.clock >= self.track[-1][0]:  # a recording starts over; a live track just waits
            self.clock, self._at = self.track[0][0], 0
        while self.track[self._at + 1][0] < self.clock:
            self._at += 1
        self._glide(self.track[self._at], self.track[self._at + 1])

    def _glide(self, before: _Report, after: _Report) -> None:
        """Put the camera and the others between two reports, by the clock."""
        (t0, old), (t1, new) = before, after
        share = min(1.0, max(0.0, (self.clock - t0) / (t1 - t0))) if t1 > t0 else 1.0
        others: list[list[float]] = []
        keys: list[Any] = []
        foes: list[_Being] = []
        down: set[Any] = set()
        for key, (x1, y1, vx, vy) in new.items():
            x0, y0 = old.get(key, (x1, y1))[:2]
            x, y = x0 + (x1 - x0) * share, y0 + (y1 - y0) * share
            if key != self.hero:
                kind, downed = self.traits.get(key, ("marauder", False))
                angle = math.atan2(vy, vx) if vx or vy else 0.0
                if kind != "marauder":
                    foes.append(_Being(key, kind, x, y, angle, downed))
                    continue
                others.append([x, y, angle])
                keys.append(key)
                if downed:
                    down.add(key)
                continue
            self.x, self.y = x, y
            if vx or vy:
                turn = (math.atan2(vy, vx) - self.angle + math.pi) % (2 * math.pi) - math.pi
                self.angle += turn * 0.2
        self.field.marauders, self.keys = others, keys
        self.foes, self.down = foes, frozenset(down)


_RESTLESS = 12.0  # seconds without anybody walking until the random cam looks elsewhere
_PATIENCE = 4.0  # seconds the random cam stays with one that stopped walking
_LOOK_BACK = 6  # reports back the way of a body is measured over (about three seconds)
_STEP_M = 1.0  # metres made in that time from which a body counts as walking
_DWELL = 60.0  # seconds a flight stays in one cube (konzept-space-night.md: the shortest preset)
_NEEDS_EXTRA = "this view needs: pip install 'cosmergon-agent[pov]'"
_LEVEL_AGE = 10.0  # seconds until a level of the world is read again
_REPORTS_KEPT = 40
# Where the cube lies in its room is asked once per stream (so once per cube shown). When that
# does not work (an older server that names no position, a network error) it is asked again
# only after this many seconds: two small reads every five minutes, never one per picture.
_PLACE_RETRY = 300.0
_ROOM_CUBES = 4096  # cubes of a room's answer that are looked at
_FRESH = 4  # reports (about two seconds) within which a changed ``firing`` stamp is a fight

# The stream's door (0.34.2, cos20 #492): what ``_http`` is for every HTTP answer. The largest
# message of the four busiest cubes was 51,778 bytes with 161 bodies (production, 20.11.2026).
_MESSAGE_BYTES = 256 * 1024  # one message, as it is: the connection is asked for unpacked
_MESSAGES_WAITING = 4  # read but not yet taken; the picture only wants the newest
_OPEN_SECONDS = 20.0  # for the connection to stand, like the reading requests beside it
_BODIES = 1000  # entries of one message that are looked at


_PLACE_KEYS = ("x", "y", "z", "vx", "vy")
_FAR_M = 1e6  # metres: far beyond any cube, and small enough for every sum of the picture


_SIGHT_KIND = {"marauder": "marauder", "monster": "monster", "thief_bird": "bird"}


def _bodies(message: str | bytes) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The bodies of one message of the stream, every text in them printable: the marauders
    and, apart, the monsters and thief birds.

    Only entries that are objects count, only the first ``_BODIES``, and only whole ones
    (``_whole``). What is no JSON, not
    finite or no object at the top raises ``ValueError``; the stream then opens again.
    """
    import json

    from cosmergon_agent._http import _finite, _no_constant
    from cosmergon_agent.state import _texts

    read = _texts(json.loads(message, parse_constant=_no_constant, parse_float=_finite))
    if not isinstance(read, dict):
        raise ValueError("the stream's message is no object")
    found = read.get("entities")
    entries = found[:_BODIES] if isinstance(found, list) else []
    whole = [e for e in entries if isinstance(e, dict) and _whole(e)]
    marauders = [e for e in whole if e.get("kind") == "marauder"]
    return marauders, [e for e in whole if e.get("kind") in ("monster", "thief_bird")]


def _whole(body: dict[str, Any]) -> bool:
    """Whether a body carries what the picture reckons with: who it is, where, and how fast.

    Until 0.34.2 a body without ``id``, or with a text or a number of 400 digits as its ``x``,
    stopped the picture for as long as the stream named it (cos20 #492, K56). It is left out now.
    """
    name = body.get("id")
    if not isinstance(name, str) or not name:
        return False
    for key in _PLACE_KEYS:
        value = body.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        if not -_FAR_M <= value <= _FAR_M:  # also false for nan
            return False
    return True


def _meta_place(meta: Any) -> tuple[tuple[int, int, int, int] | None, str | None]:
    """The cube's grid place and edge, and its room's id, from the answer of
    ``/render/iso/cube/<id>``. ``None`` for what is missing or no valid value: an older server
    names neither, and everything from the net is data (whole numbers, not truth values)."""
    if not isinstance(meta, dict):
        return None, None
    place = (meta.get("cube_x"), meta.get("cube_y"), meta.get("cube_z"), meta.get("size_x"))
    own = tuple(place) if pov_bridge.cube_ok(*place) else None  # type: ignore[arg-type]
    return own, _uuid(meta.get("space_id"))  # type: ignore[return-value]


def _uuid(value: Any) -> str | None:
    """A UUID as its text, or None; the room's id goes into a path, so only this shape does."""
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def _room_places(room: Any, edge: int, own: str) -> dict[str, tuple[int, int, int, int]]:
    """The other cubes of a room as ``id -> (x, y, z, edge)`` from ``/universe/rooms/<id>/detail``.

    The cubes of a room share one edge (``Space.cube_dimension``, models/space.py), so the
    edge of the cube shown is theirs; the answer names none per cube. Cubes without a grid
    place, with a value out of bounds, or without an id are left out.
    """
    cubes = room.get("cubes") if isinstance(room, dict) else None
    found: dict[str, tuple[int, int, int, int]] = {}
    for cube in cubes[:_ROOM_CUBES] if isinstance(cubes, list) else []:
        if not isinstance(cube, dict):
            continue
        ident = _uuid(cube.get("id"))
        place = (cube.get("cube_x"), cube.get("cube_y"), cube.get("cube_z"), edge)
        if ident is not None and ident != _uuid(own) and pov_bridge.cube_ok(*place):  # type: ignore[arg-type]
            found.setdefault(ident, place)  # type: ignore[arg-type]
    return found


class Stream(threading.Thread):
    """Reads the world's public entity stream of one cube, and levels of it, beside the picture.

    Nothing is sent but the two reading requests; no key, no name. An error ends neither the
    thread nor the terminal: it stands in ``error`` and the stream is opened again.

    The stream has a door like every HTTP answer of the SDK (``_http``): asked for unpacked, one
    message at most ``_MESSAGE_BYTES``, at most ``_MESSAGES_WAITING`` waiting, ``_OPEN_SECONDS``
    to stand, and after ``_http.ANSWER_BYTES`` it ends and opens again. Its texts are printable.
    """

    def __init__(self, base_url: str, cube: str) -> None:
        super().__init__(daemon=True)
        self.base_url, self.cube = base_url.rstrip("/"), cube
        self.reports: list[tuple[float, list[dict[str, Any]]]] = []
        # Monsters and thief birds of the same messages, under the same stamps as ``reports``
        # (only a reader outside sees them; no choice of a camera ever looks at them).
        self.beings: list[tuple[float, list[dict[str, Any]]]] = []
        self.error = ""
        self.closed = False
        self._fields: dict[int, tuple[float, Field | None]] = {}
        self._levels: list[tuple[int, int]] | None = None
        self._levels_asked = False
        # cube id -> (grid x, y, z, edge): this cube and the others of its room, once known
        self.places: dict[str, tuple[int, int, int, int]] = {}
        self._place_next = -math.inf  # monotonic time before which it is not asked (again)
        self._place_busy = False
        self._place_thread: threading.Thread | None = None

    def run(self) -> None:
        import asyncio

        while not self.closed:
            try:
                asyncio.run(self._read())
            except ImportError:
                self.error = _NEEDS_EXTRA
                return
            except Exception as exc:  # the picture says what happened, the terminal lives on
                self.error = f"no stream ({type(exc).__name__})"
            time.sleep(5.0)

    async def _read(self) -> None:
        import websockets

        from cosmergon_agent._http import ANSWER_BYTES

        url = f"{self.base_url.replace('http', 'ws', 1)}/ws/entities/{self.cube}"
        seen = 0
        async with websockets.connect(
            url,
            compression=None,
            max_size=_MESSAGE_BYTES,
            max_queue=_MESSAGES_WAITING,
            open_timeout=_OPEN_SECONDS,
        ) as ws:
            async for message in ws:
                if self.closed:
                    return
                seen += len(message)
                if seen > ANSWER_BYTES:  # like a live event stream: it ends and opens again
                    return
                self.take(message)

    def take(self, message: str | bytes) -> None:
        """One message of the stream becomes the newest report."""
        bodies, beings = _bodies(message)
        stamp = time.monotonic()
        self.reports = [*self.reports[-_REPORTS_KEPT:], (stamp, bodies)]
        self.beings = [*self.beings[-_REPORTS_KEPT:], (stamp, beings)]
        self.error = ""

    def field(self, level: int) -> Field | None:
        """The level's field, read again when it is old; ``None`` until the first answer."""
        read_at, field = self._fields.get(level, (-_LEVEL_AGE, None))
        if time.monotonic() - read_at >= _LEVEL_AGE:
            self._fields[level] = (time.monotonic(), field)
            threading.Thread(target=self._load, args=(level,), daemon=True).start()
        return field

    def levels(self) -> list[tuple[int, int]] | None:
        """``(level, living cells)`` for every level of the cube; ``None`` until the answer."""
        if not self._levels_asked:
            self._levels_asked = True
            threading.Thread(target=self._load_levels, daemon=True).start()
        return self._levels

    def locate(self) -> None:
        """Ask where this cube lies in its room, if it is time: at the first call, and again
        only ``_PLACE_RETRY`` seconds after an answer that lacked it. Cheap to call every
        picture; the reading runs in a thread, the answer lands in ``places``."""
        if self._place_busy or time.monotonic() < self._place_next:
            return
        self._place_busy = True
        self._place_thread = threading.Thread(target=self._load_places, daemon=True)
        self._place_thread.start()

    def _load_places(self) -> None:
        """The cube's meta (its place and its room), then the room's cubes: two reads."""
        retry = time.monotonic() + _PLACE_RETRY
        try:
            own, room = _meta_place(self._get(f"/api/v1/render/iso/cube/{self.cube}"))
            if own is not None:
                self.places = {self.cube: own}
            if own is not None and room is not None:
                found = self._get(f"/api/v1/universe/rooms/{room}/detail")
                self.places = {self.cube: own, **_room_places(found, own[3], self.cube)}
                retry = math.inf  # known: not asked again for this cube
        except Exception as exc:  # no position: as before, neither place nor neighbours
            logger.debug("scope: place not read: %s", type(exc).__name__)
        self._place_next, self._place_busy = retry, False

    def _get(self, path: str) -> Any:
        from cosmergon_agent import _http

        answer = _http.get(f"{self.base_url}{path}", timeout=20.0)
        answer.raise_for_status()
        return answer.json()

    def _load_levels(self) -> None:
        from cosmergon_agent import _http

        try:
            answer = _http.get(f"{self.base_url}/api/v1/render/iso/cube/{self.cube}", timeout=20.0)
            answer.raise_for_status()
            found = answer.json().get("levels") or []
            self._levels = [(int(e["z"]), int(e.get("active_cell_count") or 0)) for e in found]
        except Exception as exc:
            logger.debug("scope: levels not read: %s", type(exc).__name__)
            time.sleep(5.0)
            self._levels_asked = False  # asked again with the next picture

    def _load(self, level: int) -> None:
        from cosmergon_agent import _http

        url = f"{self.base_url}/api/v1/render/iso/cube/{self.cube}/level/{level}"
        try:
            answer = _http.get(url, timeout=20.0)
            answer.raise_for_status()
            self._fields[level] = (time.monotonic(), Field.from_level(answer.json()))
        except Exception as exc:  # the old picture of the level stays
            logger.debug("scope: level %s not read: %s", level, type(exc).__name__)


class Live(Follow):
    """The camera in the head of a marauder that walks right now.

    With ``hero`` it stays with that body and says so when it is not out. Without, it takes
    one that walks, a walking one in a fight first; when that one goes down, home, or stands
    for longer than ``_PATIENCE``, the next. Only when nobody walks does it stay with one that
    stands and fights, and the cube counts as quiet (``restless``). A second behind the world,
    so there is always a next report to glide to.

    Measured on 19.11.2026 (cos20 #789): of some 130 bodies in a cube about ten walk at a time,
    while most of the others stand with ``firing`` set. Taking "in a fight" first gave a picture
    that never moved.
    """

    _BEHIND = 1.0

    def __init__(self, stream: Stream, hero: str = "", label: str = "") -> None:
        self.stream, self.fixed, self.label = stream, hero, label
        self.level_seen: int | None = None  # the level of the field shown, once there is one
        # cube id -> (grid x, y, z, dim); what is set here wins over what ``Stream.locate`` read
        self.places: dict[str, tuple[int, int, int, int]] = {}
        self.note = "waiting for the world"
        self.hero_id = hero
        self.idle_since = time.monotonic()  # since when nobody walked in front of the camera
        self.walked_at = time.monotonic()  # when the one it follows last walked
        self.passed: set[str] = set()  # those the viewer left with skip(), until all were seen
        self._timer: Callable[[], float] = time.monotonic  # the one clock of the posture
        self._rng = random.Random()  # nosec B311 - how a picture looks around, nothing secret
        self._posture: _posture.Posture | None = None
        self._posture_for = ""  # whom the posture belongs to; another body, a new posture
        self._heading = 0.3  # where the body goes; the look is staged around it
        self.true_x = self.true_y = 0.0  # the body's place on the path, before the posture
        super().__init__(Field(side=8), [], "")
        self._heading = self.angle

    def _locate(self) -> None:
        """Have the stream ask where its cube lies (it does so once; a test's stand-in may not)."""
        locate = getattr(self.stream, "locate", None)
        if locate is not None:
            locate()

    def advance(self, seconds: float) -> None:
        self.step_no += 1
        reports = self.stream.reports
        if self.stream.error or not reports:
            self.note = self.stream.error or "waiting for the world"
            return
        out = {e["id"]: e for e in reports[-1][1]}
        if self.fixed and self.fixed not in out:
            self.note = f"{self.label} is in its berth"
            return
        if not self.fixed:
            self._keep_or_choose(out)
        if not self.hero_id:
            self.note = "nobody is out here"
            return
        if self.fixed or self.hero_id in self._walkers():
            self.idle_since = time.monotonic()
        level = out[self.hero_id]["z"]
        self._locate()
        field = self.stream.field(int(level // _METRES))
        if field is None:
            self.note = "loading the level"
            return
        self.note, self.field, self.hero = "", field, self.hero_id
        self.level_seen = int(level // _METRES)
        self.lift = float(level) - _METRES * self.level_seen  # above 0 only between two levels
        now = self._timer()
        self.clock = now - self._BEHIND
        later = [i for i, (t, _) in enumerate(reports) if t >= self.clock]
        at = max(1, later[0] if later else len(reports) - 1)
        self.angle = self._heading
        if len(reports) > 1:  # only the two reports the clock stands between are worked out
            before, after = self._around(at, level)
            self._glide(before, after)
            self._heading = self.angle
            self._pose(now, reports)

    lift = 0.0  # the hero's height above the floor of its level, in metres

    def _around(self, at: int, level: float) -> tuple[_Report, _Report]:
        """The two reports before and at ``at``, on one level, with the stream's monsters and
        birds among the marauders; and what the newer one says of each body's kind and state."""
        beings = dict(getattr(self.stream, "beings", []))
        pair = []
        for stamp, found in self.stream.reports[at - 1 : at + 1]:
            bodies = [*found, *beings.get(stamp, [])]
            pair.append((stamp, bodies, {e["id"]: _in_cells(e) for e in bodies if e["z"] == level}))
        self.traits = {
            e["id"]: (_SIGHT_KIND.get(str(e.get("kind")), "marauder"), bool(e.get("downed")))
            for e in pair[1][1]
        }
        return (pair[0][0], pair[0][2]), (pair[1][0], pair[1][2])

    def sight(self) -> Sight:
        """The base's sight, with where the field lies (level, edge, and the places known)."""
        seen = super().sight()
        if self.level_seen is None:
            return seen
        mine = self.stream.cube
        places = {**getattr(self.stream, "places", {}), **self.places}  # what it is told wins
        own = places.get(mine)
        others = tuple((key, *place) for key, place in places.items() if key != mine)
        world = SightWorld(
            self.level_seen, self.field.side, None if own is None else own[:3], others
        )
        return dataclasses.replace(seen, world=world, lift=self.lift)

    def _glide(self, before: _Report, after: _Report) -> None:
        """The others and the heading as before; the body on a spline where its speeds fit,
        but never nearer to a living body than the straight way between the reports is."""
        super()._glide(before, after)
        spot = _posture.spline_at(before, after, self.hero, self.clock)
        if spot is not None:
            self.x, self.y = _kept_off(spot, (self.x, self.y), self._living())
        self.true_x, self.true_y = self.x, self.y

    def _living(self) -> list[tuple[float, float, float]]:
        """Where the bodies are that stand or walk, and how near the camera's body may come to
        each: ``(x, y, reach)`` in cells, the two body radii added."""
        found = [
            (m[0], m[1], "marauder")
            for m, key in zip(self.field.marauders, self.keys, strict=True)
            if key not in self.down
        ]
        found += [(b.x, b.y, b.kind) for b in self.foes if not b.downed]
        return [(x, y, (_RADIUS_M["marauder"] + _RADIUS_M[kind]) / _METRES) for x, y, kind in found]

    def _pose(self, now: float, reports: list[tuple[float, list[dict[str, Any]]]]) -> None:
        """Look and posture at the real place: tension, gaze, gait and the events of a fight."""
        if self._posture is None or self._posture_for != self.hero_id:
            self._posture, self._posture_for = _posture.Posture(self._rng), self.hero_id
        stamp, bodies = reports[-1]
        before = {e["id"]: e for e in reports[-2][1]}
        walking = self.hero_id in self._walkers()
        view = _posture.View(
            self.field,
            self.true_x,
            self.true_y,
            self._heading,
            walking,
            stamp,
            bodies,
            before,
            self.hero_id,
        )
        pose = self._posture.update(now, view)
        spot = (self.true_x + pose.dx, self.true_y + pose.dy)
        self.x, self.y = _kept_off(spot, (self.true_x, self.true_y), self._living())
        self.angle, self.tilt = self._heading + pose.yaw, pose.tilt

    def _keep_or_choose(self, out: dict[Any, dict[str, Any]]) -> None:
        """Stay with the one that walks; after ``_PATIENCE`` of standing take the next."""
        now = time.monotonic()
        if self.hero_id in self._walkers():
            self.walked_at = now
            return
        worth = self._worth(out.get(self.hero_id), self._fighters())
        if worth and now - self.walked_at < _PATIENCE:
            return
        self._take_another(out)

    def _take_another(self, out: dict[Any, dict[str, Any]]) -> None:
        """Another body than the one it follows and those the viewer left, if there is any."""
        walkers, fighters = self._walkers(), self._fighters()
        seen = self.passed | {self.hero_id}
        fresh = self._choose([b for i, b in out.items() if i not in seen], walkers, fighters)
        if not fresh:  # everybody was seen: the round starts again
            self.passed.clear()
            others = [b for i, b in out.items() if i != self.hero_id]
            fresh = self._choose(others, walkers, fighters)
        self.hero_id = fresh or self._choose(list(out.values()), walkers, fighters)
        self.walked_at = time.monotonic()

    def last(self) -> list[dict[str, Any]]:
        """The bodies of the stream's last report."""
        reports = self.stream.reports
        return reports[-1][1] if reports else []

    def restless(self) -> bool:
        """Whether nobody has walked in front of the camera for ``_RESTLESS`` seconds."""
        return time.monotonic() - self.idle_since > _RESTLESS

    def skip(self) -> None:
        """The viewer wants another one: the random cam takes the next body in this cube."""
        reports = self.stream.reports
        if self.fixed or not reports:
            return
        self.passed.add(self.hero_id)
        self._take_another({e["id"]: e for e in reports[-1][1]})

    def _walkers(self) -> set[Any]:
        """Who really went somewhere between an earlier report and the last one.

        By the way made, not by the speed the stream names: a marauder in front of a wall keeps
        its speed and stays where it is (seen on 19.11.2026: one stood so for a minute).
        """
        reports = self.stream.reports
        if len(reports) < 2:
            return set()
        before = {e["id"]: e for e in reports[max(0, len(reports) - 1 - _LOOK_BACK)][1]}
        walkers = set()
        for body in reports[-1][1]:
            old = before.get(body["id"])
            if old is None or body.get("downed") or old["z"] != body["z"]:
                continue
            if math.hypot(body["x"] - old["x"], body["y"] - old["y"]) > _STEP_M:
                walkers.add(body["id"])
        return walkers

    def _fighters(self) -> set[Any]:
        """Who fired lately: whose ``firing`` stamp changed within the last ``_FRESH`` reports.

        ``firing`` holds the subtick of the last shot (a counter for a human's) and stays set
        for good, so only a change since the report before says "now".
        """
        reports = self.stream.reports[-(_FRESH + 1) :]
        found = set()
        for (_, old), (_, new) in itertools.pairwise(reports):
            before = {e["id"]: e.get("firing") for e in old}
            for body in new:
                if body.get("firing") and before.get(body["id"], body["firing"]) != body["firing"]:
                    found.add(body["id"])
        return found

    @staticmethod
    def _worth(body: dict[str, Any] | None, fighters: Collection[Any] = ()) -> bool:
        """Whether a body may be shown at all: out, on its feet, and on its way or fighting."""
        if body is None or body.get("downed"):
            return False
        return body["id"] in fighters or body.get("state") == "moving"

    @classmethod
    def _choose(
        cls,
        bodies: list[dict[str, Any]],
        walkers: Collection[Any] = (),
        fighters: Collection[Any] = (),
    ) -> str:
        """One that walks, in a fight first; else one that fights; ``""`` if there is none."""
        worth = [b for b in bodies if b["id"] in walkers or cls._worth(b, fighters)]
        worth = [b for b in worth if not b.get("downed")]
        worth.sort(key=lambda b: (b["id"] not in walkers, b["id"] not in fighters))
        return str(worth[0]["id"]) if worth else ""

    def frame(self, width: int, height: int) -> list[tuple[str, str]]:
        if not self.note:
            return super().frame(width, height)
        return note_frame(self.note, width, height)


class Flight(Live):
    """The camera that flies by itself: through one of the liveliest levels of a cube, past
    whoever is out there, and after a while on to the next cube.

    The way of choosing follows the Flight concept (cos20 ``konzept-space-night.md``): not
    the liveliest level every time but one of the upper fifth, so that a second visit shows
    something else; a fixed time per cube.
    """

    _PACE = 0.3  # cells a step
    _MARGIN = 4.0  # cells before the field's edge where it turns back

    def __init__(self, stream: Stream) -> None:
        self.level: int | None = None  # set before the first step, which the base takes
        self.since = time.monotonic()
        self.leave = False  # the viewer asked for the next cube (skip)
        super().__init__(stream)
        self.note = "flying to the next cube"

    def restless(self) -> bool:
        return self.leave or time.monotonic() - self.since > _DWELL

    def skip(self) -> None:
        """The viewer wants another one: the flight goes on to the next cube."""
        self.leave = True

    def advance(self, seconds: float) -> None:
        self.step_no += 1
        if self.level is None:
            self.level = self._pick(self.stream.levels())
        self._locate()
        field = None if self.level is None else self.stream.field(self.level)
        if field is None:
            self.note = "flying to the next cube"
            return
        if self.note:  # arrived: start in the middle, on a free square
            self.note, self.since = "", time.monotonic()
            self.x = self.y = field.side / 2 + 0.5
            while not field.free(self.x, self.y) and self.x < field.side - 1:
                self.x += 1.0
        self.field, self.level_seen = field, self.level
        bodies = self.stream.reports[-1][1] if self.stream.reports else []
        here = (b for b in bodies if int(b["z"] // _METRES) == self.level)
        here_now = list(here)
        field.marauders = [[b["x"] / _METRES, b["y"] / _METRES, 0.0] for b in here_now]
        self.keys = [b["id"] for b in here_now]
        self._fly()

    def _fly(self) -> None:
        """Towards the widest way; near the edge, back towards the middle."""
        looks = [(self._free(off), off) for off in _FEELERS]
        ahead = looks[len(looks) // 2][0]
        _, want = max(looks, key=lambda look: min(look[0], 6.0) - abs(look[1]))
        side = self.field.side
        edge = min(self._MARGIN, side / 4)
        if not (edge < self.x < side - edge and edge < self.y < side - edge):
            home = math.atan2(side / 2 - self.y, side / 2 - self.x)
            want = max(-0.9, min(0.9, (home - self.angle + math.pi) % (2 * math.pi) - math.pi))
        self._turn += (0.12 * want - self._turn) * 0.25
        self.angle += self._turn
        x = self.x + self._PACE * math.cos(self.angle)
        y = self.y + self._PACE * math.sin(self.angle)
        inside = 0.5 < x < side - 0.5 and 0.5 < y < side - 0.5
        if ahead > 1.2 and inside and self.field.free(x, y):  # else it only turns
            self.x, self.y = x, y

    @staticmethod
    def _pick(levels: list[tuple[int, int]] | None) -> int | None:
        """One of the upper fifth of the levels by living cells; ``None`` until they are known."""
        if levels is None:
            return None
        alive = sorted((entry for entry in levels if entry[1] > 0), key=lambda entry: -entry[1])
        if not alive:
            return 0
        upper = alive[: max(1, len(alive) // 5)]
        return random.choice(upper)[0]  # nosec B311 — which level to show, no secret


def note_frame(note: str, width: int, height: int) -> list[tuple[str, str]]:
    """A picture that only says something, in the middle."""
    rows = [(" " * width, _BLANK * width)] * height
    if height and width:
        rows[height // 2] = (note[:width].center(width), _KEYS["ground"][0] * width)
    return rows


RANDOM = "random"
FLIGHT = "flight"
_ROAMING = {RANDOM: "random marauder", FLIGHT: "flight between the cubes"}


class Scope:
    """What the view shows: the cams there are, the one chosen, and its picture.

    Cams: one per body of the agent (the user chooses, nothing is chosen for them),
    ``RANDOM``: a marauder somewhere in the world that experiences something right now, and
    ``FLIGHT``: no marauder at all, the camera flies from cube to cube by itself.
    """

    def __init__(self, base_url: str, cam: str = RANDOM) -> None:
        self.base_url, self.cam = base_url.rstrip("/"), cam
        self.fine = False  # half blocks in place of signs: twice the rows, no texture
        self.bodies: list[dict[str, Any]] = []
        self._streams: dict[str, Stream] = {}
        self._camera: Live | None = None
        self._made_for: tuple[str, str | None] | None = None
        self._cubes: list[str] = []
        self._cubes_asked = False

    def set_bodies(self, bodies: list[dict[str, Any]]) -> None:
        """The agent's bodies as its state lists them (``marauders``)."""
        self.bodies = [b for b in bodies if isinstance(b, dict) and b.get("id")]

    def cams(self) -> list[tuple[str, str]]:
        """Every cam there is: (key, name)."""
        return [(str(b["id"]), body_label(b)) for b in self.bodies] + list(_ROAMING.items())

    def next_cam(self) -> str:
        """Choose the cam after the current one; its key."""
        keys = [key for key, _ in self.cams()]
        self.cam = keys[(keys.index(self.cam) + 1) % len(keys)] if self.cam in keys else keys[0]
        return self.cam

    def skip(self) -> None:
        """Another one of the same cam: the next marauder (random), the next cube (flight)."""
        if self._camera is not None and self.cam in _ROAMING:
            self._camera.skip()

    def title(self) -> str:
        """The chosen cam in words, for the frame of the view."""
        return dict(self.cams()).get(self.cam, _ROAMING[RANDOM])

    def caption(self) -> str:
        """What the marauder the picture follows does right now, as a line under the picture:
        ``tournament 2 · on watch``. Empty for the flight and while there is no picture."""
        camera = self._camera
        if camera is None or self.cam == FLIGHT or camera.note or not camera.hero_id:
            return ""
        body = self._body()
        seen = next((e for e in camera.last() if str(e["id"]) == camera.hero_id), None)
        if seen is not None and seen.get("downed"):
            doing = "down"
        elif camera.hero_id in camera._walkers():
            doing = "walking"
        elif camera.hero_id in camera._fighters():
            doing = "fighting"
        elif body is not None and body.get("state") == "recovery":
            doing = "in its berth"
        else:
            doing = "on watch"
        return f"{self.title()} · {doing}"

    def step(self) -> None:
        camera = self._live()
        if camera is not None:
            camera.step()

    def pictured(self) -> bool:
        """Whether the view shows a picture now, not a line of text that says why there is none."""
        camera = self._camera
        return camera is not None and not camera.note

    def sight(self) -> Sight | None:
        """What the view shows now, read only; ``None`` while it shows a line of text."""
        camera = self._camera
        if camera is None or camera.note:
            return None
        return camera.sight()

    def frame(self, width: int, height: int) -> list[tuple[str, str]]:
        """The picture of the chosen cam, or a line that says why there is none."""
        camera = self._live()
        if camera is None:
            return note_frame(self._why_not(), width, height)
        rows = camera.frame(width, height)
        line = f" {self.caption()} "
        if len(line) > 2 and height >= 4 and len(line) <= width and rows:
            at = (width - len(line)) // 2
            text, keys = rows[-1]
            rows[-1] = (
                text[:at] + line + text[at + len(line) :],
                keys[:at] + _KEYS["loot"][0] * len(line) + keys[at + len(line) :],
            )
        return rows

    def blocks(self, width: int, height: int) -> list[tuple[str, str]] | None:
        """The picture in half blocks (``blocks``); ``None`` where a line of text is shown."""
        camera = self._live()
        if camera is None or camera.note:
            return None
        return blocks(camera.frame(width, 2 * height))

    def close(self) -> None:
        """End every stream; the view is not shown any more."""
        for stream in self._streams.values():
            stream.closed = True
        self._streams, self._camera, self._made_for = {}, None, None

    def _body(self) -> dict[str, Any] | None:
        return next((b for b in self.bodies if str(b["id"]) == self.cam), None)

    def _why_not(self) -> str:
        body = self._body()
        if self.cam == FLIGHT:
            return "looking for a cube"
        if self.cam == RANDOM or body is None:
            return "looking for a marauder that is out"
        if body.get("dead"):
            return f"{body_label(body)} is dead"
        return f"{body_label(body)}: no place known"

    def _live(self) -> Live | None:
        """The camera for the chosen cam, made anew when the cam or its cube changes."""
        body = self._body()
        if self.cam not in _ROAMING and body is None:
            self.cam = RANDOM
        cube = self._random_cube() if self.cam in _ROAMING else self._cube_of(body)
        if cube is None:
            return None
        if self._made_for != (self.cam, cube) or self._camera is None:
            for key in [k for k in self._streams if k != cube]:
                self._streams.pop(key).closed = True
            if cube not in self._streams:
                self._streams[cube] = Stream(self.base_url, cube)
                self._streams[cube].start()
            hero, label = ("", "") if body is None else (str(body["id"]), body_label(body))
            stream = self._streams[cube]
            self._camera = Flight(stream) if self.cam == FLIGHT else Live(stream, hero, label)
            self._made_for = (self.cam, cube)
        return self._camera

    @staticmethod
    def _cube_of(body: dict[str, Any] | None) -> str | None:
        if body is None or body.get("dead") or not body.get("cube_id"):
            return None
        return str(body["cube_id"])

    def _random_cube(self) -> str | None:
        """A cube with life in it; another one when the camera has seen enough of this one."""
        if not self._cubes_asked:
            self._cubes_asked = True
            threading.Thread(target=self._ask_cubes, daemon=True).start()
        if not self._cubes:
            return None
        camera = self._camera
        same_cam = self._made_for is not None and self._made_for[0] == self.cam
        if camera is not None and same_cam and camera.restless():
            self._cubes.append(self._cubes.pop(0))
        return self._cubes[0]

    def _ask_cubes(self) -> None:
        from cosmergon_agent import _http

        try:
            answer = _http.get(f"{self.base_url}/api/v1/render/iso/cubes", timeout=20.0)
            answer.raise_for_status()
            cubes = [c for c in answer.json().get("cubes") or [] if not c.get("frozen_at")]
        except Exception as exc:
            logger.debug("scope: cubes not read: %s", type(exc).__name__)
            self._cubes_asked = False  # asked again with the next picture
            time.sleep(5.0)
            return
        cubes.sort(key=lambda c: -int(c.get("live_cells") or 0))
        busiest = [str(c["cube_id"]) for c in cubes[:8]]
        random.shuffle(busiest)
        self._cubes = busiest


def body_label(body: dict[str, Any]) -> str:
    """A body in words: ``main`` or ``tournament 2`` (slots are counted from one)."""
    if body.get("kind") == "main":
        return "main"
    return f"{body.get('kind') or 'body'} {int(body.get('body_slot') or 0) + 1}"


def _thing_char(name: str, up: float) -> str:
    """The character of a marauder or a loot box at a share ``up`` of its height."""
    if name == "loot":
        return "=" if up > 0.8 else "$"
    return "O" if up > 0.78 else "H" if up > 0.35 else "A"

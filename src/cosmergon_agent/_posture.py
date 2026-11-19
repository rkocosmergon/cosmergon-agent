"""How a living being holds its head: the posture of the marauder's camera (cos20 #789).

The camera of the scope stays at the real place of the body it follows. On top of that place it
stages look and posture, so that the picture is not a camera bolted to a coordinate. Nothing here
reads a network or a clock: every function takes what it needs, time included, and the random
numbers come from a ``random.Random`` the caller hands in.

Established practice this follows (the ideas and the shape, not the code):

* Activision, US patent 12,485,347 (procedural first-person camera): coherent noise per axis,
  amplitude and frequency from linear and angular speed, and look and body joined by a
  mass-spring-damper with a dead zone (``Spring``, ``noise``, ``fbm``).
* id Software, Quake ``view.c`` (GPL, only the ideas are used): bob as a sine over a step cycle
  of 0.6 s that follows the speed and is clamped, idle sway from three sines of different
  period, a damage kick that fades within 0.5 s, death tips the view (``bob``, ``_IDLE``,
  ``Posture._events``).
* GDC 2016 "Juicing Your Cameras With Math": one value from 0 to 1 that events raise and that
  decays by itself and drives the strength of everything else (``Tension``).
* Gaze models: a target is chosen by rank (purposeful before random), then a quick turn
  (saccade), a hold, and the head follows through a damped spring (``Gaze``).
* Valve / Gaffer On Games, delayed playback between snapshots with a Hermite spline when the
  velocity comes along (``hermite``, ``spline_at``).

What the camera never does: stop while the body walks, or leave the real place by more than
``_MAX_OFFSET`` cells. Roll is left out: a character grid has no clean way to turn the picture.
"""

from __future__ import annotations

import dataclasses
import math
import random
from typing import Any

# What stands on a square of the field (the drawer's kinds).
EMPTY, CELL, OWN, WALL, TOWER = 0, 1, 2, 3, 4
CELL_M = 4.0  # a cell is 4 m (marauder.js CELL_SIZE); the stream speaks in metres

# --- tension (own choices, tuned by eye; ``_TAU`` is the time the tension takes to ebb) --------
_FLOOR = 0.1  # he is never relaxed
_TAU = 6.0  # seconds in which the excess over the floor falls to a third
_SIGHT = 12.0  # cells within which a foreign marauder or a tower counts as seen
_EARSHOT = 16.0  # cells within which a shot or somebody going down is noticed
_TOWER_R = 10  # cells around it that are searched for a tower
_SAMPLE = 0.25  # cells between the samples of a line of sight

# --- gaze (own choices) ------------------------------------------------------------------------
_HOLD = (2.5, 5.0)  # seconds a look aside is held at no tension, drawn evenly: never in a rhythm
_AHEAD = (5.0, 12.0)  # seconds it looks ahead between two looks aside: the picture stays readable
_HOLD_TENSE = 0.4  # share by which the hold shortens at full tension
_BACK_BASE, _BACK_TENSE = 0.02, 0.08  # chance of a look over the shoulder, at rest and per tension
_GLANCE_AWAY = 0.2  # chance that a foe in sight is not looked at this time: not locked on
_REACT_HOLD = 2.5  # seconds a look after a hit is held
_WALK_LOOK = 0.6  # radians a walker looks aside at most (about 35 degrees)
_STAND_LOOK = 1.2  # radians a standing body looks aside at most; further only over the shoulder
_BACK_LIMIT = 2.5  # radians of a look over the shoulder while walking
_GAZE_FAR = 10.0  # cells a gaze ray reaches
_CORNER_JUMP = 3.0  # cells by which two neighbouring rays differ at an edge
_OPEN_REACH = 7.0  # cells a ray must reach to count as an open line of sight
_TOWER_TILT = 0.1  # share of the picture's height looked up at a tower
_DUCK_TILT = 0.05  # share looked down, per tension, with a foe in sight (a crouch)
_LEAN = 0.12  # cells leaned out sideways to look round an edge

# --- spring: look and body joined (patent), slightly under-damped -------------------------------
_OMEGA, _OMEGA_TENSE = 2.2, 1.5  # 1/s: a turn takes about two seconds and reads as a turn
_OMEGA_HIT = 2.0  # factor for the look after a hit
_ZETA = 0.9
_DEAD = 0.015  # radians (or share of height) of error in which the spring does not pull
_SUBSTEP = 0.02  # seconds an integration step may be at most
_DT_MAX = 0.5  # a longer pause between two pictures is not integrated

# --- noise, step cycle, events -----------------------------------------------------------------
_PLACE_NOISE = 0.05  # cells, at tension 0.5 and standing; the strength is 0.5 + tension
_YAW_NOISE = 0.012  # radians, likewise
_WALK_NOISE = 1.5  # factor while walking
_IDLE = ((0.003, 3.1), (0.002, 4.7), (0.0015, 7.3))  # Quake: three sines; share of height, s
_BOB_CYCLE = 0.6  # seconds of a step cycle at the reference speed (Quake)
_BOB_SPEED = 1.0  # cells a second at which the step cycle has its full size
_BOB_TILT = 0.006  # share of the picture's height up and down: under a fifth of a row at 30 rows
_SWAY = 0.03  # cells to the side, once per two steps
_KICK_TIME = 0.5  # seconds a hit's kick takes to fade (Quake)
_KICK_TILT, _KICK_YAW = 0.07, 0.15  # share of height, radians
_FALL_TIME = 1.2  # seconds it takes to go down
_FALL_TILT = 0.3  # share of height the view sinks and tips
_MAX_OFFSET = 0.3  # cells: the farthest the camera is from the body's real place
_TILT_LIMIT = (-0.4, 0.45)

# --- path --------------------------------------------------------------------------------------
_PLAUSIBLE = 0.5  # how far the speed may differ from the way made before the spline is dropped
_REST_SLACK = 0.05  # cells of slack in that comparison
_AIMED = 1.5  # cells within which a shot's end point counts as aimed at the body (own choice)


def _wrap(angle: float) -> float:
    """An angle in radians as the equal one between minus pi and pi."""
    return (angle + math.pi) % (2 * math.pi) - math.pi


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# --- coherent noise ----------------------------------------------------------------------------


def _lattice(seed: int, i: int) -> float:
    """A fixed value between -1 and 1 for a whole number: the corners of the noise."""
    h = (i * 374761393 + seed * 668265263) & 0xFFFFFFFF
    h = ((h ^ (h >> 13)) * 1274126177) & 0xFFFFFFFF
    h ^= h >> 16
    return h / 2**31 - 1.0


def noise(seed: int, t: float) -> float:
    """Smooth value noise in [-1, 1]: the same seed and time give the same value."""
    i = math.floor(t)
    f = t - i
    s = f * f * (3.0 - 2.0 * f)
    a = _lattice(seed, i)
    return a + (_lattice(seed, i + 1) - a) * s


def fbm(seed: int, t: float, octaves: int = 3) -> float:
    """Noise of several octaves, each twice as fast and half as strong, in [-1, 1]."""
    total = norm = 0.0
    for o in range(octaves):
        amp = 0.5**o
        total += amp * noise(seed + 101 * o, t * 2.0**o)
        norm += amp
    return total / norm


# --- the path between two reports --------------------------------------------------------------


def hermite(
    p0: tuple[float, float], p1: tuple[float, float], v0: tuple[float, float],
    v1: tuple[float, float], span: float, share: float,
) -> tuple[float, float]:  # fmt: skip
    """The place at ``share`` (0..1) of a cubic Hermite piece; velocities per second, ``span`` s."""
    s2, s3 = share * share, share**3
    h00, h10, h01, h11 = 2 * s3 - 3 * s2 + 1, s3 - 2 * s2 + share, 3 * s2 - 2 * s3, s3 - s2
    x = h00 * p0[0] + h10 * span * v0[0] + h01 * p1[0] + h11 * span * v1[0]
    y = h00 * p0[1] + h10 * span * v0[1] + h01 * p1[1] + h11 * span * v1[1]
    return x, y


def plausible(
    p0: tuple[float, float], p1: tuple[float, float], v0: tuple[float, float],
    v1: tuple[float, float], span: float,
) -> bool:  # fmt: skip
    """Whether the velocities fit the way really made between the two places.

    A body in front of a wall keeps its speed and stands: then the way made is nothing and the
    speed is not, and a spline would swing out. Opposed to the way made is as bad.
    """
    chord = (p1[0] - p0[0], p1[1] - p0[1])
    way = math.hypot(*chord)
    expected = span * (math.hypot(*v0) + math.hypot(*v1)) / 2
    if abs(way - expected) > _PLAUSIBLE * max(way, expected) + _REST_SLACK:
        return False
    if way < _REST_SLACK:
        return True
    return all(v[0] * chord[0] + v[1] * chord[1] >= 0 for v in (v0, v1))


def spline_at(
    before: tuple[float, dict[Any, Any]], after: tuple[float, dict[Any, Any]], key: Any,
    clock: float,
) -> tuple[float, float] | None:  # fmt: skip
    """Where the body ``key`` is at ``clock``, on a spline through its two reports.

    The reports hold ``(x, y, vx, vy)`` in cells and metres a second. ``None`` when the body is
    not in both, or its velocities are implausible: the caller then keeps the straight piece.
    """
    (t0, old), (t1, new) = before, after
    if key not in old or key not in new or t1 <= t0:
        return None
    x0, y0, vx0, vy0 = old[key]
    x1, y1, vx1, vy1 = new[key]
    v0, v1 = (vx0 / CELL_M, vy0 / CELL_M), (vx1 / CELL_M, vy1 / CELL_M)
    if not plausible((x0, y0), (x1, y1), v0, v1, t1 - t0):
        return None
    return hermite((x0, y0), (x1, y1), v0, v1, t1 - t0, _clamp((clock - t0) / (t1 - t0), 0, 1))


# --- what can be seen --------------------------------------------------------------------------


def sees(world: Any, ax: float, ay: float, bx: float, by: float, stop: float = 0.0) -> bool:
    """Whether nothing stands between two places; ``stop`` leaves out the last stretch."""
    full = math.hypot(bx - ax, by - ay)
    for i in range(1, int((full - stop) / _SAMPLE) + 1):
        share = i * _SAMPLE / full
        if world.at(math.floor(ax + (bx - ax) * share), math.floor(ay + (by - ay) * share)):
            return False
    return True


def reach(world: Any, x: float, y: float, angle: float, far: float) -> float:
    """How far a look from (x, y) goes in a direction before something stands, up to ``far``."""
    dx, dy, d = math.cos(angle), math.sin(angle), 0.0
    while d < far:
        d += 0.5
        if world.at(math.floor(x + dx * d), math.floor(y + dy * d)):
            return d
    return far


def nearest_tower(world: Any, x: float, y: float) -> tuple[float, float] | None:
    """``(distance, angle)`` of the nearest tower square in sight, if there is one."""
    best: tuple[float, float] | None = None
    cx, cy = math.floor(x), math.floor(y)
    for ty in range(cy - _TOWER_R, cy + _TOWER_R + 1):
        for tx in range(cx - _TOWER_R, cx + _TOWER_R + 1):
            if world.at(tx, ty) != TOWER:
                continue
            dist = math.hypot(tx + 0.5 - x, ty + 0.5 - y)
            nearer = best is None or dist < best[0]
            if dist <= _SIGHT and nearer and sees(world, x, y, tx + 0.5, ty + 0.5, stop=0.7):
                best = (dist, math.atan2(ty + 0.5 - y, tx + 0.5 - x))
    return best


def opening(
    world: Any, x: float, y: float, heading: float, walking: bool, rng: random.Random
) -> tuple[float, bool] | None:
    """A direction worth a look: ``(angle, is_corner)``.

    Walking, only the way ahead is searched. A corner is where two neighbouring rays differ by
    a lot; the open side of it is the angle. Else any long ray is an open line of sight.
    """
    if walking:
        angles = [heading + k * 0.2 for k in range(-7, 8)]
    else:
        angles = [heading + k * math.pi / 8 for k in range(16)]
    far = [reach(world, x, y, a, _GAZE_FAR) for a in angles]
    corners = [
        angles[i if far[i] > far[i - 1] else i - 1]
        for i in range(1, len(angles))
        if abs(far[i] - far[i - 1]) >= _CORNER_JUMP
    ]
    if corners:
        return rng.choice(corners), True
    opens = [a for a, r in zip(angles, far, strict=True) if r >= _OPEN_REACH]
    return (rng.choice(opens), False) if opens else None


@dataclasses.dataclass
class Scan:
    """What one report says about the surroundings of the followed body."""

    foes: list[tuple[float, float]] = dataclasses.field(default_factory=list)  # (dist, angle)
    shooters: list[tuple[float, float]] = dataclasses.field(default_factory=list)
    aimers: list[tuple[float, float]] = dataclasses.field(default_factory=list)  # aim at the body
    tower: tuple[float, float] | None = None
    closing: bool = False  # a foe came nearer since the report before
    hit: bool = False  # the body was hit since the report before
    down: bool = False  # the body is down
    fell_near: bool = False  # somebody nearby went down since the report before


def _changed(body: dict[str, Any], old: dict[str, Any] | None, key: str) -> bool:
    """Whether a counter of the stream moved. ``firing`` and ``hit`` hold the subtick of the last
    shot or hit and stay set for good, so only a change since the report before is an event."""
    return old is not None and bool(body.get(key)) and body.get(key) != old.get(key)


def _aimed_at(body: dict[str, Any], mx: float, my: float) -> bool:
    """Whether a shooter's ``aim`` (radians) and ``fdist`` (cells) end at the place (mx, my)."""
    aim, far = body.get("aim"), body.get("fdist")
    if aim is None or far is None:
        return False
    ex = body["x"] / CELL_M + math.cos(aim) * far
    ey = body["y"] / CELL_M + math.sin(aim) * far
    return math.hypot(ex - mx, ey - my) < _AIMED


def _note(
    scan: Scan, world: Any, me: dict[str, Any], body: dict[str, Any], old: dict[str, Any] | None
) -> None:
    """Enter one other body into the scan."""
    mx, my = me["x"] / CELL_M, me["y"] / CELL_M
    bx, by = body["x"] / CELL_M, body["y"] / CELL_M
    dist, angle = math.hypot(bx - mx, by - my), math.atan2(by - my, bx - mx)
    if dist > _EARSHOT:
        return
    if _changed(body, old, "firing"):
        scan.shooters.append((dist, angle))
        if _aimed_at(body, mx, my):
            scan.aimers.append((dist, angle))
    if body.get("downed") and old is not None and not old.get("downed"):
        scan.fell_near = True
    if body.get("downed") or body.get("pid") == me.get("pid") or dist > _SIGHT:
        return
    if sees(world, mx, my, bx, by):
        scan.foes.append((dist, angle))
        if old is not None:
            before = math.hypot(old["x"] / CELL_M - mx, old["y"] / CELL_M - my)
            scan.closing = scan.closing or before > dist + 0.05


def scan_report(
    world: Any, bodies: list[dict[str, Any]], before: dict[Any, dict[str, Any]], me_id: Any
) -> Scan:
    """The surroundings of body ``me_id`` in the last report, against the report before."""
    scan = Scan()
    me = next((b for b in bodies if b["id"] == me_id), None)
    if me is None:
        return scan
    scan.hit = _changed(me, before.get(me_id), "hit")
    scan.down = bool(me.get("downed"))
    for body in bodies:
        if body["id"] != me_id and body["z"] == me["z"]:
            _note(scan, world, me, body, before.get(body["id"]))
    scan.tower = nearest_tower(world, me["x"] / CELL_M, me["y"] / CELL_M)
    return scan


# --- tension -----------------------------------------------------------------------------------


class Tension:
    """One value from the floor to 1 that events raise and that ebbs by itself (GDC 2016)."""

    def __init__(self) -> None:
        self.value = _FLOOR

    def raise_to(self, level: float) -> None:
        """At least ``level``."""
        self.value = _clamp(max(self.value, level), _FLOOR, 1.0)

    def bump(self, amount: float) -> None:
        """More by ``amount``."""
        self.value = _clamp(self.value + amount, _FLOOR, 1.0)

    def decay(self, dt: float) -> None:
        """Ebb by ``dt`` seconds towards the floor."""
        self.value = _FLOOR + (self.value - _FLOOR) * math.exp(-dt / _TAU)

    def feel(self, scan: Scan) -> None:
        """Raise the tension by what a report shows."""
        if scan.foes:
            self.raise_to(0.3 + 0.4 * (1 - min(d for d, _ in scan.foes) / _SIGHT))
            if scan.closing:
                self.bump(0.15)
        if scan.shooters:
            self.raise_to(0.6)
        if scan.aimers:
            self.raise_to(0.8)
        if scan.tower is not None:
            self.raise_to(0.3)
        if scan.fell_near:
            self.bump(0.3)
        if scan.hit:
            self.raise_to(1.0)


# --- look and body joined ----------------------------------------------------------------------


class Spring:
    """A mass-spring-damper with a dead zone, slightly under-damped; angular or plain."""

    def __init__(self, angular: bool = False) -> None:
        self.pos = self.vel = 0.0
        self.angular = angular

    def step(self, target: float, dt: float, omega: float) -> float:
        """Move on by ``dt`` seconds towards ``target``; the new position."""
        n = max(1, math.ceil(dt / _SUBSTEP))
        h = dt / n
        for _ in range(n):
            err = _wrap(target - self.pos) if self.angular else target - self.pos
            if abs(err) < _DEAD:
                err = 0.0
            self.vel += (omega * omega * err - 2 * _ZETA * omega * self.vel) * h
            self.pos += self.vel * h
        if self.angular:
            self.pos = _wrap(self.pos)
        return self.pos


@dataclasses.dataclass
class View:
    """What the posture needs to know at one picture."""

    world: Any  # the field: ``at`` and ``free``
    x: float  # the true place, in cells
    y: float
    heading: float  # where the body goes (or faces while it stands)
    walking: bool
    stamp: float  # time of the last report; a new one means a new scan
    bodies: list[dict[str, Any]]  # the last report
    before: dict[Any, dict[str, Any]]  # the report before, by id
    me: Any  # id of the followed body


@dataclasses.dataclass
class Pose:
    """What the camera adds to the true place and the heading."""

    dx: float = 0.0
    dy: float = 0.0
    yaw: float = 0.0
    tilt: float = 0.0


class Gaze:
    """Where the head looks: a target by rank, a quick turn, a hold, the head follows a spring."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.yaw, self.tilt, self.lean = Spring(angular=True), Spring(), Spring()
        self.target, self.kind, self.aim_tilt, self.lean_to = 0.0, "idle", 0.0, 0.0
        self.until = -1e18  # the first look is chosen at once
        self.last_hold = 0.0

    def update(
        self, now: float, dt: float, tension: float, scan: Scan, view: View, react: bool
    ) -> tuple[float, float, float]:
        """The look now: (yaw against the heading, tilt, lean to the side in cells)."""
        if react:
            self._react(now, scan, view)
        elif now >= self.until:
            self._choose(now, tension, scan, view)
        off = 0.0 if self.kind == "ahead" else _wrap(self.target - view.heading)
        aside = _WALK_LOOK if view.walking else _STAND_LOOK
        limit = _BACK_LIMIT if self.kind == "back" else math.pi if self.kind == "hit" else aside
        omega = (_OMEGA + _OMEGA_TENSE * tension) * (_OMEGA_HIT if self.kind == "hit" else 1.0)
        return (
            self.yaw.step(_clamp(off, -limit, limit), dt, omega),
            self.tilt.step(self.aim_tilt, dt, omega),
            self.lean.step(self.lean_to, dt, omega / 2),
        )

    def _choose(self, now: float, tension: float, scan: Scan, view: View) -> None:
        """A new target and a hold: the more tense, the shorter."""
        calm = not (scan.foes or scan.shooters)
        if calm and self.kind != "ahead":  # between two looks aside it looks where it goes
            self.target, self.kind, self.aim_tilt = view.heading, "ahead", 0.0
        else:
            self.target, self.kind, self.aim_tilt = self._ranked(tension, scan, view)
        span = _AHEAD if self.kind == "ahead" else _HOLD
        hold = self.rng.uniform(*span) * (1 - _HOLD_TENSE * tension)
        self.until, self.last_hold = now + hold, hold
        side = _wrap(self.target - view.heading)
        self.lean_to = _LEAN * (1 if side > 0 else -1) if self.kind == "corner" else 0.0

    def _ranked(self, tension: float, scan: Scan, view: View) -> tuple[float, str, float]:
        """Enemy in sight, somebody firing, a tower, a look back, an opening, anything else."""
        rng = self.rng
        if scan.foes and rng.random() > _GLANCE_AWAY:
            return min(scan.foes)[1], "foe", _DUCK_TILT * tension
        if scan.shooters:
            return min(scan.shooters)[1], "shot", 0.0
        if scan.tower is not None:
            return scan.tower[1], "tower", -_TOWER_TILT
        if rng.random() < _BACK_BASE + _BACK_TENSE * tension:
            return view.heading + rng.choice((-1, 1)) * rng.uniform(2.0, 3.0), "back", 0.0
        spot = opening(view.world, view.x, view.y, view.heading, view.walking, rng)
        if spot is not None:
            return spot[0], "corner" if spot[1] else "open", 0.0
        spread = _WALK_LOOK if view.walking else 0.7
        return view.heading + rng.uniform(-spread, spread), "idle", 0.0

    def _react(self, now: float, scan: Scan, view: View) -> None:
        """After a hit: at once to where it could have come from, as far as the data shows."""
        sources = scan.aimers or scan.shooters or scan.foes
        source = min(sources) if sources else None
        if source is not None:
            self.target = source[1]
        else:
            self.target = view.heading + self.rng.choice((-1, 1)) * self.rng.uniform(1.2, 2.0)
        self.kind, self.aim_tilt, self.lean_to = "hit", 0.0, 0.0
        self.until, self.last_hold = now + _REACT_HOLD, _REACT_HOLD


def bob(phase: float, speed: float) -> tuple[float, float]:
    """The step cycle at ``phase`` (cycles): ``(tilt, sway)``, both zero when the body stands."""
    size = _clamp(speed / _BOB_SPEED, 0.0, 1.0)
    return (
        _BOB_TILT * size * math.sin(2 * math.pi * phase),
        _SWAY * size * math.sin(math.pi * phase),
    )


def advance_phase(phase: float, speed: float, dt: float) -> float:
    """The step cycle moves on faster with the speed (0.6 s a cycle at the reference speed)."""
    if speed <= 0.05:
        return phase
    return phase + dt / _BOB_CYCLE * _clamp(speed / _BOB_SPEED, 0.6, 1.5)


class Posture:
    """Tension, gaze, gait and events of one followed body, put together to a ``Pose``."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.seed = rng.randrange(1 << 30)
        self.tension, self.gaze, self.scan = Tension(), Gaze(rng), Scan()
        self.phase, self.speed = 0.0, 0.0
        self.kick_at: float | None = None
        self.kick_side = 1.0
        self.fell_at: float | None = None
        self._last: float | None = None
        self._stamp: float | None = None
        self._spot: tuple[float, float] | None = None

    def update(self, now: float, view: View) -> Pose:
        """The pose at time ``now``; once per picture."""
        dt = 0.0 if self._last is None else _clamp(now - self._last, 0.0, _DT_MAX)
        self._last = now
        react = self._sense(now, view)
        self.tension.decay(dt)
        self._measure(view, dt)
        t = self.tension.value
        yaw, tilt, lean = self.gaze.update(now, dt, t, self.scan, view, react)
        tilt_bob, sway = bob(self.phase, self.speed)
        kick_tilt, kick_yaw, fall = self._events(now)
        nx, ny, nyaw = self._noise(now, t, view.walking)
        idle = sum(a * math.sin(2 * math.pi * now / p) for a, p in _IDLE) * (1 + t)
        dx, dy = self._offset(view, (nx, ny), lean + sway)
        total = tilt + idle + tilt_bob + kick_tilt + fall
        return Pose(dx, dy, yaw + nyaw + kick_yaw, _clamp(total, *_TILT_LIMIT))

    @staticmethod
    def _offset(view: View, noisy: tuple[float, float], side: float) -> tuple[float, float]:
        """The camera's step away from the true place: bounded, and never into a block."""
        dx = noisy[0] - math.sin(view.heading) * side
        dy = noisy[1] + math.cos(view.heading) * side
        length = math.hypot(dx, dy)
        if length > _MAX_OFFSET:
            dx, dy = dx * _MAX_OFFSET / length, dy * _MAX_OFFSET / length
        if not view.world.free(view.x + dx, view.y + dy):
            return 0.0, 0.0
        return dx, dy

    def _sense(self, now: float, view: View) -> bool:
        """Read a new report once; ``True`` when it brings a hit."""
        if view.stamp == self._stamp:
            return False
        self._stamp = view.stamp
        self.scan = scan_report(view.world, view.bodies, view.before, view.me)
        self.tension.feel(self.scan)
        if self.scan.hit:
            self.kick_at, self.kick_side = now, self.rng.choice((-1.0, 1.0))
        if self.scan.down and self.fell_at is None:
            self.fell_at = now
        elif not self.scan.down:
            self.fell_at = None
        return self.scan.hit

    def _measure(self, view: View, dt: float) -> None:
        """The speed the body really makes, in cells a second, and the step cycle on it."""
        if self._spot is not None and dt > 0.02:
            made = math.hypot(view.x - self._spot[0], view.y - self._spot[1]) / dt
            self.speed += (made - self.speed) * 0.5
        if not view.walking:
            self.speed = 0.0
        self._spot = (view.x, view.y)
        self.phase = advance_phase(self.phase, self.speed, dt)

    def _noise(self, now: float, tension: float, walking: bool) -> tuple[float, float, float]:
        """Coherent noise on place and look; the stronger the more tense and the more walking."""
        strength = (0.5 + tension) * (_WALK_NOISE if walking else 1.0)
        return (
            _PLACE_NOISE * strength * fbm(self.seed, now * 0.35),
            _PLACE_NOISE * strength * fbm(self.seed + 1, now * 0.35),
            _YAW_NOISE * strength * fbm(self.seed + 2, now * 0.5),
        )

    def _events(self, now: float) -> tuple[float, float, float]:
        """The kick of a hit (fades in half a second) and the sinking of going down."""
        kick = 0.0
        if self.kick_at is not None:
            kick = max(0.0, 1.0 - (now - self.kick_at) / _KICK_TIME)
        fall = 0.0
        if self.fell_at is not None:
            x = _clamp((now - self.fell_at) / _FALL_TIME, 0.0, 1.0)
            fall = _FALL_TILT * x * x * (3 - 2 * x)
        return _KICK_TILT * kick, _KICK_YAW * kick * self.kick_side, fall

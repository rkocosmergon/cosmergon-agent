"""Immutable game state snapshot, recreated each tick."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    """A Conway game field owned by this agent."""

    id: str
    cube_id: str
    z_position: int
    active_cell_count: int
    entity_tier: int | None = None
    entity_type: str | None = None
    reife_score: int = 0
    permeability_state: str = "incubating"


@dataclass(frozen=True)
class Cube:
    """A spatial container for game fields."""

    id: str
    name: str
    space_id: str | None = None
    cube_x: int = 0
    cube_y: int = 0
    cube_z: int = 0


@dataclass(frozen=True)
class Ranking:
    """Agent's current ranking."""

    player_tier: int = 0
    tier_name: str = "Novice"
    player_score: float = 0.0


@dataclass(frozen=True)
class Focus:
    """LLM query budget status."""

    focus_energy: float = 0.0
    focus_regen_rate: float = 1.0
    can_query_llm: bool = False


@dataclass(frozen=True)
class GameState:
    """Complete game state snapshot for one agent at one tick.

    Recreated each tick from the server's /agents/{id}/state endpoint.
    """

    agent_id: str
    agent_type: str
    energy: float
    fields: list[Field]
    cubes: list[Cube]
    universe_cubes: list[Cube]
    ranking: Ranking
    focus: Focus
    tick: int = 0

    @classmethod
    def from_api(cls, data: dict) -> GameState:
        """Parse API response into GameState."""
        fields = [Field(**f) for f in data.get("fields", [])]
        cubes = [Cube(**c) for c in data.get("cubes", [])]
        universe_cubes = [Cube(**c) for c in data.get("universe_cubes", [])]
        ranking = Ranking(**data.get("ranking", {}))
        focus = Focus(**data.get("focus", {}))

        return cls(
            agent_id=data["agent_id"],
            agent_type=data.get("agent_type", "independent_agent"),
            energy=data.get("energy_balance", 0.0),
            fields=fields,
            cubes=cubes,
            universe_cubes=universe_cubes,
            ranking=ranking,
            focus=focus,
            tick=data.get("tick", 0),
        )

"""Immutable game state snapshot, recreated each tick."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from dataclasses import fields as dc_fields
from typing import Any

logger = logging.getLogger(__name__)


def _safe_construct(cls: type, data: dict) -> Any:
    """Construct a dataclass from a dict, ignoring unknown fields (C3).

    Unknown fields are silently dropped for forward-compatibility.
    Missing fields use dataclass defaults.
    """
    known = {f.name for f in dc_fields(cls)}
    filtered = {k: v for k, v in data.items() if k in known}
    return cls(**filtered)


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
    name: str = ""
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
class AgentSituation:
    """Structured facts about the agent's current situation.

    Non-imperative — all values are descriptive facts, never instructions.
    The developer decides what to do with these facts.
    """

    fields_owned: int = 0
    fields_without_cells: int = 0
    energy_trend: str = "stable"
    affordable_presets: tuple[str, ...] = ()
    benchmark_ready: bool = False
    benchmark_days_remaining: int = 0
    active_catastrophe: str | None = None
    catastrophe_warning_ticks: int | None = None
    dormant_spores_on_fields: int = 0

    @classmethod
    def from_api(cls, data: dict) -> AgentSituation:
        return cls(
            fields_owned=data.get("fields_owned", 0),
            fields_without_cells=data.get("fields_without_cells", 0),
            energy_trend=data.get("energy_trend", "stable"),
            affordable_presets=tuple(data.get("affordable_presets", [])),
            benchmark_ready=data.get("benchmark_ready", False),
            benchmark_days_remaining=data.get("benchmark_days_remaining", 0),
            active_catastrophe=data.get("active_catastrophe"),
            catastrophe_warning_ticks=data.get("catastrophe_warning_ticks"),
            dormant_spores_on_fields=data.get("dormant_spores_on_fields", 0),
        )


@dataclass(frozen=True)
class WorldBriefing:
    """Economy-wide context included in every state response."""

    total_agents: int = 0
    your_rank: int = 0
    market_summary: str = ""
    top_agent: str | None = None
    last_event: str | None = None
    tip: str = ""  # Deprecated: static reference only (OWASP LLM01/LLM06)
    infra_fund_pct: float = 0.0
    infra_fund_msg: str = ""
    situation: AgentSituation = AgentSituation()

    @classmethod
    def from_api(cls, data: dict) -> WorldBriefing:
        fund = data.get("infrastructure_fund") or {}
        sit = data.get("agent_situation") or {}
        return cls(
            total_agents=data.get("total_agents", 0),
            your_rank=data.get("your_rank", 0),
            market_summary=data.get("market_summary", ""),
            top_agent=data.get("top_agent"),
            last_event=data.get("last_event"),
            tip=data.get("tip", ""),
            infra_fund_pct=float(fund.get("progress_pct", 0.0)),
            infra_fund_msg=fund.get("message", ""),
            situation=AgentSituation.from_api(sit),
        )


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
    agent_name: str = ""
    persona_type: str = ""  # active persona (scientist, warrior, …) — empty if not yet set
    agent_mode: str = "api"  # "api" | "llm" | "vagant" — api agents don't auto-respond to chat
    subscription_tier: str = "free"
    has_stripe_customer: bool = False  # Owner has Stripe record (ex-paid or active-paid)
    subscription_downgrade_at: str | None = None  # ISO timestamp: cancel grace period end
    world_briefing: WorldBriefing | None = None
    learned_rules: list[str] = field(default_factory=list)
    next_tick_at: float | None = None  # Unix timestamp when next game tick fires (server truth)
    compass_preset: str | None = None  # Last explicitly set compass preset; None if never set

    @classmethod
    def from_api(cls, data: dict) -> GameState:
        """Parse API response into GameState.

        Uses defensive parsing: unknown fields are ignored,
        missing fields use defaults (forward-compatibility, C3).
        """
        fields = [_safe_construct(Field, f) for f in data.get("fields", [])]
        cubes = [_safe_construct(Cube, c) for c in data.get("cubes", [])]
        universe_cubes = [_safe_construct(Cube, c) for c in data.get("universe_cubes", [])]
        ranking = _safe_construct(Ranking, data.get("ranking", {}))
        focus = _safe_construct(Focus, data.get("focus", {}))

        wb_data = data.get("world_briefing")
        world_briefing = WorldBriefing.from_api(wb_data) if wb_data else None

        return cls(
            agent_id=data.get("agent_id", "unknown"),
            agent_name=data.get("agent_name", ""),
            persona_type=data.get("persona_type", ""),
            agent_type=data.get("agent_type", "independent_agent"),
            energy=float(data.get("energy_balance", data.get("energy", 0.0))),
            fields=fields,
            cubes=cubes,
            universe_cubes=universe_cubes,
            ranking=ranking,
            focus=focus,
            tick=data.get("tick", 0),
            agent_mode=data.get("agent_mode", "api"),
            subscription_tier=data.get("subscription_tier", "free"),
            has_stripe_customer=bool(data.get("has_stripe_customer", False)),
            subscription_downgrade_at=data.get("subscription_downgrade_at"),
            world_briefing=world_briefing,
            learned_rules=data.get("learned_rules", []),
            next_tick_at=data.get("next_tick_at"),
            compass_preset=data.get("compass_preset"),
        )

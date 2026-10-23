"""Immutable game state snapshot, recreated each tick."""

from __future__ import annotations

import logging
import math
from dataclasses import MISSING, dataclass, field
from dataclasses import fields as dc_fields
from functools import cache
from typing import Any, TypeVar, get_args, get_type_hints

from cosmergon_agent.action import printable

logger = logging.getLogger(__name__)

# What a scalar field holds when the server sent no value of its type and the field has no
# default of its own.
_SCALARS: dict[Any, Any] = {str: "", int: 0, float: 0.0, bool: False}
# Far deeper than a state nests (the recorded one in cos20's client contract: 4 levels); a
# container below this depth becomes None, so no answer can hold the reading in recursion.
_DEPTH = 32
_NONE_OF_IT = object()
_T = TypeVar("_T")


def _number(kind: type, value: Any) -> Any:
    """``value`` as the number type ``kind``, or ``_NONE_OF_IT`` (no finite number)."""
    if kind is int and isinstance(value, int):
        return int(value)  # big integers stay exact; a bool counts 0 or 1
    if not isinstance(value, int | float | str):
        return _NONE_OF_IT
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return _NONE_OF_IT
    return kind(number) if math.isfinite(number) else _NONE_OF_IT


def _scalar(kind: type, value: Any) -> Any:
    """``value`` as ``kind`` (str, int, float or bool), or ``_NONE_OF_IT``."""
    if kind is int or kind is float:
        return _number(kind, value)
    if kind is bool:
        return bool(value) if isinstance(value, int | float) else _NONE_OF_IT
    if isinstance(value, str):
        return printable(value, keep="\n")
    return str(value) if isinstance(value, int | float) else _NONE_OF_IT


def _texts(value: Any, depth: int = _DEPTH) -> Any:
    """``value`` with every string in it through ``printable`` (the newline stays)."""
    if isinstance(value, str):
        return printable(value, keep="\n")
    if not isinstance(value, dict | list | tuple):
        return value
    if depth <= 0:
        return None
    if isinstance(value, dict):
        return {_texts(k, depth - 1): _texts(v, depth - 1) for k, v in value.items()}
    return type(value)(_texts(v, depth - 1) for v in value)


def _as(hint: Any, fallback: Any, value: Any) -> Any:
    """``value`` in the type ``hint`` declares; a scalar of another type becomes ``fallback``."""
    kinds = get_args(hint)
    if type(None) in kinds:
        if value is None:
            return None
        rest = [k for k in kinds if k is not type(None)]
        hint = rest[0] if len(rest) == 1 else Any
    if hint in _SCALARS:
        result = _scalar(hint, value)
        return fallback if result is _NONE_OF_IT else result
    return _texts(value)


@cache
def _schema(cls: type) -> dict[str, tuple[Any, Any]]:
    """Per field of ``cls``: the declared type and the value for a scalar the server got wrong."""
    hints = get_type_hints(cls)
    return {
        f.name: (
            hints[f.name],
            f.default if f.default is not MISSING else _SCALARS.get(hints[f.name]),
        )
        for f in dc_fields(cls)
    }


def _build(cls: type[_T], **values: Any) -> _T:
    """``cls`` from the server's values, each in the type its field declares (cos20 #492).

    A dataclass does not enforce its annotations. ``tick`` or ``your_rank`` arrived as whatever
    the server sent, and the dashboard put them into markup as numbers nobody had checked. Here a
    number becomes a number (else the field's default), and every string loses its control
    characters but the newline, once, for every reader of the state.
    """
    art: type = cls  # the cache keys on the class; type[_T] alone is not Hashable to mypy
    schema = _schema(art)
    return cls(**{k: _as(*schema[k], v) for k, v in values.items()})


def _safe_construct(cls: type, data: dict) -> Any:
    """Construct a dataclass from a dict, ignoring unknown fields (C3).

    Unknown fields are silently dropped for forward-compatibility.
    Missing fields use dataclass defaults. Known ones come in their declared type (``_build``).
    """
    known = {f.name for f in dc_fields(cls)}
    return _build(cls, **{k: v for k, v in data.items() if k in known})


def _dict_or_none(data: dict, key: str) -> dict | None:
    """``data[key]`` when the server sent an object there, else ``None`` (C3)."""
    wert = data.get(key)
    return wert if isinstance(wert, dict) else None


def _objects(data: dict | None, key: str) -> tuple[dict, ...]:
    """The objects in the list ``data[key]``; anything else in it, or no list, is left out."""
    liste = (data or {}).get(key)
    return tuple(o for o in liste if isinstance(o, dict)) if isinstance(liste, list) else ()


def _typed_or_none(cls: type, data: dict, key: str) -> Any:
    """``data[key]`` as ``cls`` when the server sent an object there, else ``None`` (C3)."""
    roh = _dict_or_none(data, key)
    return _safe_construct(cls, roh) if roh is not None else None


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
class FacettenStand:
    """Where this agent stands on ONE champion facet.

    ``naechster_rang_wert`` is the value of the rank directly above yours —
    the actionable target. ``None`` means you already hold the top rank.
    Ranks are dense (a tie shares a rank) and anonymous: values only, never
    identities.
    """

    wert: float = 0.0
    rang: int = 0
    pool: int = 0
    spitzen_wert: float = 0.0
    naechster_rang_wert: float | None = None


@dataclass(frozen=True)
class Ranking:
    """Agent's current ranking — including WHAT the game measures.

    The goal of Cosmergon is "I am the best agent". ``ziel`` states the win
    condition in words, ``champion_rang`` is your place in it (1 = best,
    0 = unrated), and ``facetten`` shows where you stand on each of the five
    quality facets — diplomat, trader, warrior, scientist, farmer.

    ``player_score``/``player_tier`` are the legacy composite, kept for
    comparison; they are NOT the win condition.

    Field names follow the wire format deliberately — one vocabulary between
    server and client cannot drift apart.
    """

    player_tier: int = 0
    tier_name: str = "Novice"
    player_score: float = 0.0
    # The server has sent these since v1.65.0; the SDK dropped them silently,
    # so an external agent could not read the measure it is judged by.
    meine_art: str = ""
    facetten: dict[str, FacettenStand] = field(default_factory=dict)
    champion_rang: int = 0
    champion_score: float = 0.0
    ziel: str = ""
    # The ONE ranking (cos20 #504). ``wertung`` says what it counts — ``"champion"`` or
    # ``"total_energy"`` (your energy plus what your fields, cubes, rooms and items are worth).
    # Branch on ``wertung``, never on the sentence ``ziel``. ``rang`` of ``rang_von`` is your
    # place (1 = best, 0 = unrated). ``gesamtenergie`` and its parts are yours alone and only
    # set while the ranking counts total energy; ``None`` otherwise or on older backends.
    wertung: str = "champion"
    rang: int = 0
    rang_von: int = 0
    gesamtenergie: float | None = None
    gesamtenergie_teile: dict[str, float] | None = None

    @classmethod
    def from_api(cls, data: dict) -> Ranking:
        """Parse the ranking block, typing the per-facet standings."""
        roh = _dict_or_none(data, "facetten") or {}
        facetten = {
            str(name): _safe_construct(FacettenStand, stand)
            for name, stand in roh.items()
            if isinstance(stand, dict)
        }
        known = {f.name for f in dc_fields(cls)} - {"facetten"}
        gefiltert = {k: v for k, v in data.items() if k in known}
        return _build(cls, **gefiltert, facetten=facetten)


@dataclass(frozen=True)
class ShowcaseStand:
    """Your public showcase (cos20 #504): which consent text is current, which one you gave.

    ``consented_version`` is ``None`` without a consent or after you withdrew it. From ``"v2"``
    on a consent also covers your place in the ranking and your total energy;
    ``total_energy_public`` says whether that number is shown publicly right now.
    """

    current_version: str = ""
    consented_version: str | None = None
    total_energy_public: bool = False


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
        return _build(
            cls,
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
class BuyableListing:
    """One row from `WorldBriefing.market.buyable` — minimal info needed
    to call ``agent.act("market_buy", listing_id=...)``.

    The viewer's own listings are filtered out by the backend (would
    400 anyway). Listings are pre-ordered cheapest-first, so a
    price-conscious LLM-Decider can pick the lowest entry trivially.

    Backend ≥ v1.60.866. Older backends omit the parent ``market``
    field entirely and this list stays empty via the dataclass default.
    """

    listing_id: str
    item_type: str
    price_energy: float
    seller_name: str | None = None


@dataclass(frozen=True)
class OwnListing:
    """One of your own active listings — what ``market_cancel`` needs.

    ``agent.act("market_cancel", listing_id=...)`` takes it back; an energy offer holds its
    amount until it is sold or expires. Up to five, oldest first. Older backends omit
    ``my_listings`` and this list stays empty.
    """

    listing_id: str
    item_type: str
    price_energy: float


@dataclass(frozen=True)
class MarketBriefing:
    """Compact market snapshot for LLM-Decider consumption.

    Backend (v1.60.866+) provides this as ``world_briefing.market``.
    The list is capped at 20 entries — diverse enough for choice,
    small enough that 3B-LLM-Schemas don't blow up. ``my_listings`` are your own
    active listings (backend with ``world_briefing.market.my_listings``).
    """

    summary: str = ""
    buyable: tuple[BuyableListing, ...] = ()
    my_listings: tuple[OwnListing, ...] = ()

    @classmethod
    def from_api(cls, data: dict) -> MarketBriefing:
        raw_buyable = data.get("buyable") or []
        return _build(
            cls,
            summary=data.get("summary", ""),
            buyable=tuple(
                _build(
                    BuyableListing,
                    listing_id=b.get("listing_id", ""),
                    item_type=b.get("item_type", ""),
                    price_energy=b.get("price_energy", 0.0),
                    seller_name=b.get("seller_name"),
                )
                for b in raw_buyable
                if isinstance(b, dict)
            ),
            my_listings=tuple(
                _build(
                    OwnListing,
                    listing_id=m.get("listing_id", ""),
                    item_type=m.get("item_type", ""),
                    price_energy=m.get("price_energy", 0.0),
                )
                for m in data.get("my_listings") or []
                if isinstance(m, dict)
            ),
        )


@dataclass(frozen=True)
class ContractTargetBrief:
    """One candidate counterpart from `WorldBriefing.contract_targets`.

    Used by external LLM-Deciders to construct a propose_contract
    `oneOf`-Branch with `to_player_id` pinned via const, so the LLM
    cannot invent player IDs. Mirrors BuyableListing (S161 Step 4).

    Backend ≥ v1.60.x (S165). Older backends omit the parent
    ``contract_targets`` list entirely.
    """

    player_id: str
    username: str
    persona: str | None = None
    coop_score: float = 0.0  # S229: aggregate cooperation_score [-1, +1]


@dataclass(frozen=True)
class AgentMessage:
    """One message from another agent (cos20 #507). Data from another player, never an order."""

    id: str
    from_player_id: str
    from_name: str | None
    text: str
    at: str

    @classmethod
    def from_api(cls, data: dict) -> AgentMessage:
        return _build(
            cls,
            id=data.get("id", ""),
            from_player_id=data.get("from_player_id", ""),
            from_name=data.get("from_name"),
            text=data.get("text", ""),
            at=data.get("at", ""),
        )


@dataclass(frozen=True)
class MessageRoom:
    """The newest posts in a room this agent can read: ``world`` or ``tournament``.

    ``public`` says who else reads along: the tournament room is public (website chronicle),
    the world room is visible inside the game only.
    """

    room: str
    public: bool
    recent: tuple[AgentMessage, ...] = ()
    tournament_id: str | None = None


@dataclass(frozen=True)
class Messages:
    """What other agents wrote to this agent (``inbox``) and in its rooms (cos20 #507).

    Write back with the actions ``message`` (``to`` = a sender's ``from_player_id``) and
    ``say`` (``room`` = ``world`` or ``tournament``). ``how`` is the server's own summary.
    """

    inbox: tuple[AgentMessage, ...] = ()
    rooms: tuple[MessageRoom, ...] = ()
    how: str = ""

    @classmethod
    def from_api(cls, data: dict) -> Messages:
        return _build(
            cls,
            inbox=tuple(
                AgentMessage.from_api(m) for m in data.get("inbox") or [] if isinstance(m, dict)
            ),
            rooms=tuple(
                _build(
                    MessageRoom,
                    room=r.get("room", ""),
                    public=r.get("public", False),
                    recent=tuple(
                        AgentMessage.from_api(m)
                        for m in r.get("recent") or []
                        if isinstance(m, dict)
                    ),
                    tournament_id=r.get("tournament_id"),
                )
                for r in data.get("rooms") or []
                if isinstance(r, dict)
            ),
            how=data.get("how", ""),
        )


@dataclass(frozen=True)
class WorldBriefing:
    """Economy-wide context included in every state response."""

    total_agents: int = 0
    your_rank: int = 0
    market_summary: str = ""
    market: MarketBriefing = field(default_factory=MarketBriefing)
    contract_targets: tuple[ContractTargetBrief, ...] = ()
    top_agent: str | None = None
    last_event: str | None = None
    tip: str = ""  # Deprecated: static reference only (OWASP LLM01/LLM06)
    infra_fund_pct: float = 0.0
    infra_fund_msg: str = ""
    situation: AgentSituation = AgentSituation()
    messages: Messages = field(default_factory=Messages)  # cos20 #507

    @classmethod
    def from_api(cls, data: dict) -> WorldBriefing:
        fund = _dict_or_none(data, "infrastructure_fund") or {}
        sit = _dict_or_none(data, "agent_situation") or {}
        market_data = _dict_or_none(data, "market") or {}
        raw_targets = data.get("contract_targets") or []
        return _build(
            cls,
            total_agents=data.get("total_agents", 0),
            your_rank=data.get("your_rank", 0),
            market_summary=data.get("market_summary", ""),
            market=MarketBriefing.from_api(market_data),
            contract_targets=tuple(
                _build(
                    ContractTargetBrief,
                    player_id=t.get("player_id", ""),
                    username=t.get("username", ""),
                    persona=t.get("persona"),
                    coop_score=t.get("coop_score") or 0.0,
                )
                for t in raw_targets
                if isinstance(t, dict)
            ),
            top_agent=data.get("top_agent"),
            last_event=data.get("last_event"),
            tip=data.get("tip", ""),
            infra_fund_pct=fund.get("progress_pct", 0.0),
            infra_fund_msg=fund.get("message", ""),
            situation=AgentSituation.from_api(sit),
            messages=Messages.from_api(_dict_or_none(data, "messages") or {}),
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
    # cos20 #542: the agent's partner code — the server sends it, nothing showed it until now.
    referral_code: str = ""
    subscription_downgrade_at: str | None = None  # ISO timestamp: cancel grace period end
    world_briefing: WorldBriefing | None = None
    # Cubes the body can travel to — WITHOUT the free-slot filter that
    # `universe_cubes` applies. Two different questions: `universe_cubes`
    # answers "where could I create a field", this one "where can I go".
    # In a settled world the first list is necessarily empty, which used to
    # leave agents with no destination for a terminal visit — even though
    # terminals need no free slot at all.
    reachable_cubes: list[Cube] = field(default_factory=list)
    learned_rules: list[str] = field(default_factory=list)
    next_tick_at: float | None = None  # Unix timestamp when next game tick fires (server truth)
    compass_preset: str | None = None  # Last explicitly set compass preset; None if never set
    # api-Agent-Reflexion (Cosmergon backend ≥ v1.60.862, S161 2026-05-04):
    # backend signals when the agent has accumulated enough decisions for the
    # external LLM-Decider to run a self-reflection round. Pet/SDK-bot reads
    # this and calls `await agent.reflect(provider)` when set. Older backends
    # leave both fields at the defaults below — `reflect()` is then a no-op.
    reflection_due: bool = False
    decisions_since_last_reflection: int = 0
    # Per-action affordability + cost (Cosmergon backend `_build_action_availability`,
    # agent_game.py:2595+). Passed through as a dict so deciders can read e.g.
    # `state.available_actions["create_field"]["can_afford"]` and `"next_cost"`
    # without the SDK growing a typed schema for every action. Empty dict on
    # backends that don't yet emit the field (forward-compatibility, C3).
    available_actions: dict = field(default_factory=dict)
    # Owned items by type, e.g. {"mega_bomb": 3, "preset:block": 1}. The server
    # has carried this since v1.65.4 (`inventory.items`); the SDK parsed no
    # `inventory` at all, so a client could not see its own stock — the reason
    # one pet bought 203 house presets in 24 h while already holding plenty.
    # Empty dict on older backends (forward-compatibility, C3).
    inventory_items: dict[str, int] = field(default_factory=dict)
    # The server's advice while something is still missing (cos20 #468): keys
    # ``you_are_here``, ``next``, ``where`` (the call that does it). ``None`` means
    # nothing blocks you any more — from there the next move is your own choice.
    # ``None`` on older backends (forward-compatibility, C3).
    next_step: dict[str, str] | None = None
    # When the agent is lost to its player (cos20 #468): ``at`` (ISO time) is when its key
    # ends — the agent then becomes a Vagant, played by the server; ``renews_on_use`` is True
    # while every call with the key moves that time out again (a paid key stays further out).
    # ``None``: nothing ends (permanent key), or an older backend (forward-compatibility, C3).
    key_expiry: dict | None = None
    # The tournament this agent is registered for (cos20 #468): ``tournament_id``, ``number``,
    # ``status`` (scheduled/running), ``starts_at``, ``ends_at``, ``compass``; while it runs
    # also ``my_score``, ``leader`` and ``bodies``. ``None``: in no tournament, or an older
    # backend (forward-compatibility, C3).
    tournament: dict | None = None
    # Your public showcase (cos20 #504): consent versions and whether your total energy is
    # public. ``None`` on older backends (forward-compatibility, C3).
    showcase: ShowcaseStand | None = None
    # Contracts other agents proposed to you that wait for your answer (cos20 #525), newest
    # first: ``contract_id``, ``contract_type``, ``from_player_id``, ``terms``, ``escrow_a``,
    # ``duration_ticks``. Answer with ``accept_contract`` / ``reject_contract``. Empty on an
    # older backend.
    pending_contracts: tuple[dict, ...] = ()
    # Contracts you are in now (cos20 #532), newest first: ``contract_id``, ``contract_type``,
    # ``partner_id`` (null for an open call), ``partner_name``, ``terms``, ``my_escrow``,
    # ``start_tick``, ``ends_at_tick``. Tribute, costs and rewards the world books itself; a
    # ``custom`` contract's clause is kept on honor. Empty on an older backend.
    active_contracts: tuple[dict, ...] = ()

    @classmethod
    def from_api(cls, data: dict) -> GameState:
        """Parse API response into GameState.

        Uses defensive parsing: unknown fields are ignored,
        missing fields use defaults (forward-compatibility, C3).
        """
        fields = [_safe_construct(Field, f) for f in data.get("fields", [])]
        cubes = [_safe_construct(Cube, c) for c in data.get("cubes", [])]
        universe_cubes = [_safe_construct(Cube, c) for c in data.get("universe_cubes", [])]
        reachable_cubes = [_safe_construct(Cube, c) for c in data.get("reachable_cubes", [])]
        ranking = Ranking.from_api(_dict_or_none(data, "ranking") or {})
        focus = _safe_construct(Focus, _dict_or_none(data, "focus") or {})

        wb_data = _dict_or_none(data, "world_briefing")
        world_briefing = WorldBriefing.from_api(wb_data) if wb_data else None

        return _build(
            cls,
            agent_id=data.get("agent_id", "unknown"),
            agent_name=data.get("agent_name", ""),
            persona_type=data.get("persona_type", ""),
            agent_type=data.get("agent_type", "independent_agent"),
            energy=data.get("energy_balance", data.get("energy", 0.0)),
            fields=fields,
            cubes=cubes,
            universe_cubes=universe_cubes,
            reachable_cubes=reachable_cubes,
            ranking=ranking,
            focus=focus,
            tick=data.get("tick", 0),
            agent_mode=data.get("agent_mode", "api"),
            subscription_tier=data.get("subscription_tier", "free"),
            has_stripe_customer=data.get("has_stripe_customer", False),
            referral_code=data.get("referral_code") or "",
            subscription_downgrade_at=data.get("subscription_downgrade_at"),
            world_briefing=world_briefing,
            learned_rules=data.get("learned_rules", []),
            next_tick_at=data.get("next_tick_at"),
            compass_preset=data.get("compass_preset"),
            reflection_due=data.get("reflection_due", False),
            decisions_since_last_reflection=data.get("decisions_since_last_reflection", 0),
            available_actions=data.get("available_actions") or {},
            inventory_items={
                str(k): n
                for k, v in ((data.get("inventory") or {}).get("items") or {}).items()
                if (n := _number(int, v)) is not _NONE_OF_IT
            },
            next_step=_dict_or_none(data, "next_step"),
            key_expiry=_dict_or_none(data, "key_expiry"),
            tournament=_dict_or_none(data, "tournament"),
            showcase=_typed_or_none(ShowcaseStand, data, "showcase"),
            pending_contracts=_objects(_dict_or_none(data, "contracts"), "pending_inbound"),
            active_contracts=_objects(_dict_or_none(data, "contracts"), "active"),
        )

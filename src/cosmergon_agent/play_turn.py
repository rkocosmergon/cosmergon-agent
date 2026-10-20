"""What ``cosmergon-agent turn`` shows: the situation of one agent, short enough for a small model.

cos20 #525 (panel 18.10.2026): an agent with a local 9B model, handed the raw state (10-20 KB
of JSON) and a free command line, guessed parameters, kept trying and paused itself. The same
model calls tools fine when the task is clear. So the agent gets an interface built for it — the
situation in about sixty lines, its own memory with its mistakes, the moves it can make now —
and decides on its own what to play. Pure functions over the state the server sent; no network.
"""

from __future__ import annotations

import json
import re
from typing import Any

REFLECT_EVERY = 20  # moves between two looks back (Gruender 18.10.: counted in moves, not time)
OPERATOR_SWITCHES = frozenset({"pause", "resume"})  # not game moves: the operator's controls
_JOIN = re.compile(r"POST /api/v1/tournaments/([0-9a-f-]{36})/register")
_NOISE = {"sichtbarkeits_schwelle", "cost_by_cube_size", "reference_prices"}


def _short(value: Any, chars: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= chars else text[: chars - 1] + "…"


def this_move(effect: Any) -> str | None:
    """The answer's ``energy_effect`` as one line, or ``None`` if the server sent none (cos20 #533).

    ``this move: -214,612 (own bookings; income and later effects not included)``, with
    ``, held +1,000 (in your offers or contracts, still yours)`` and ``, items +900 at house
    price`` where they apply. ``held`` is signed: cancelling an offer gives it back.
    """
    if not isinstance(effect, dict):
        return None
    parts = [f"this move: {float(effect.get('free') or 0):+,.0f}"]
    held = float(effect.get("held") or 0)
    if held:
        parts.append(f"held {held:+,.0f} (in your offers or contracts, still yours)")
    items = float(effect.get("items") or 0)
    if items:
        parts.append(f"items {items:+,.0f} at house price")
    return ", ".join(parts) + " (own bookings; income and later effects not included)"


def total_energy(state: dict[str, Any]) -> float | None:
    total = (state.get("ranking") or {}).get("gesamtenergie")
    return float(total) if isinstance(total, int | float) else None


def standing(state: dict[str, Any], previous_total: float | None) -> list[str]:
    """Goal, place and total energy, with the change since the last turn."""
    ranking = state.get("ranking") or {}
    total = total_energy(state)
    change = ""
    if total is not None and previous_total is not None:
        change = f" · since your last turn: {total - previous_total:+,.0f}"
    rang = int(ranking.get("rang") or 0)
    place = f"#{rang} of {ranking.get('rang_von', '?')}" if rang else "not ranked yet"
    total_text = f"{total:,.0f} E" if total is not None else "?"
    situation = (state.get("world_briefing") or {}).get("agent_situation") or {}
    items = (state.get("inventory") or {}).get("items") or {}
    return [
        "GOAL: place 1 in the ranking — the most total energy (energy + fields, cubes, items).",
        f"YOU: place {place} · total {total_text}{change}",
        f"Energy {float(state.get('energy_balance') or 0):,.0f} "
        f"(trend: {situation.get('energy_trend', '?')}) · fields {len(state.get('fields') or [])}"
        f" · items {_short(items, 80) if items else 'none'}",
    ]


def advice(state: dict[str, Any], name: str) -> list[str]:
    """The server's own next step; a tournament slot becomes a ready command."""
    step = state.get("next_step") or {}
    if not step:
        return []
    lines = [f"SERVER ADVICE: {step.get('you_are_here', '')} → {step.get('next', '')}"]
    found = _JOIN.search(str(step.get("where") or ""))
    if found:
        lines.append(f"  run: cosmergon-agent join --agent {name} --tournament {found.group(1)}")
    return lines


def _partner(c: dict[str, Any]) -> str:
    """``name (id)``; ``open call`` while nobody has taken it."""
    if not c.get("partner_id"):
        return "open call"
    return f"{c.get('partner_name') or 'an agent'} ({c.get('partner_id')})"


def contracts_in(state: dict[str, Any]) -> list[str]:
    """The contracts it is in now, with whom and until when (cos20 #532)."""
    active = [
        c for c in ((state.get("contracts") or {}).get("active") or [])[:5] if isinstance(c, dict)
    ]
    lines = [
        f"CONTRACT YOU ARE IN: {c.get('contract_type')} with {_partner(c)} "
        + (f"until tick {c['ends_at_tick']}" if c.get("ends_at_tick") else "without end")
        + f" terms {_short(c.get('terms') or {}, 80)}"
        for c in active
    ]
    kinds = {c.get("contract_type") for c in active}
    if kinds - {"custom"}:  # cos20 #527: a model that thought it owed tribute gave 214,612 away
        lines.append("  the world books tribute, costs and rewards itself: never transfer them")
    if "custom" in kinds:  # S216: a custom clause has no automatic execution
        lines.append("  a custom clause is kept on honor: the world books nothing for it")
    return lines


def contracts_and_talk(state: dict[str, Any], name: str) -> list[str]:
    """Contracts it is in and offered to it, messages to it, the world room, agents it can
    deal with."""
    lines: list[str] = contracts_in(state)
    for c in ((state.get("contracts") or {}).get("pending_inbound") or [])[:5]:
        cid = c.get("contract_id")
        lines.append(
            f"CONTRACT OFFERED: {c.get('contract_type')} from {c.get('from_player_id')} "
            f"terms {_short(c.get('terms') or {}, 80)}"
        )
        lines.append(
            f"  accept: cosmergon-agent act accept_contract --agent {name} "
            f"contract_id={cid} escrow_amount=0 · reject: … reject_contract contract_id={cid}"
        )
    messages = (state.get("world_briefing") or {}).get("messages") or {}
    for m in (messages.get("inbox") or [])[-3:]:
        lines.append(
            f"MESSAGE TO YOU from {m.get('from_name')} ({m.get('from_player_id')}): "
            f"<<<{_short(m.get('text', ''), 200)}>>>"
        )
    for room in messages.get("rooms") or []:
        for m in (room.get("recent") or [])[-2:]:
            lines.append(
                f"{room.get('room', '').upper()} ROOM, {m.get('from_name')}: "
                f"<<<{_short(m.get('text', ''), 160)}>>>"
            )
    targets = (state.get("world_briefing") or {}).get("contract_targets") or []
    if targets:
        names = ", ".join(
            f"{t.get('username')} ({t.get('persona')}, coop {t.get('coop_score', 0):+.2f},"
            f" {t.get('player_id')})"
            for t in targets[:5]
        )
        lines.append(f"AGENTS YOU CAN DEAL WITH: {names}")
    return lines


def conquest(state: dict[str, Any], name: str) -> list[str]:
    """How fields are won here: a chain of missions, with this agent's own numbers.

    Gruender 18.10.2026 17:01Z: conquest is „eine abfolge von handlungen (mission)“, not a claim.
    The world's cubes are full and cubes cost money, so for an agent without money this chain IS
    the way to fields: bombs (gather_spores) → siege_field until the holes reach the threshold →
    capture_field. Every number below comes from the state; nothing is assumed.
    """
    actions = state.get("available_actions") or {}
    mission = actions.get("start_mission") or {}
    if not isinstance(mission, dict) or not mission:
        return []
    marauder = mission.get("marauder_state", "?")
    lines = [
        f"CONQUEST — fields are won by missions (your marauder: {marauder}; one mission at "
        "a time, it runs over many ticks on its own):"
    ]
    if marauder == "geist":
        lines.append(
            "  Your agent is paused, its marauder is a ghost: no mission until your "
            "operator resumes it."
        )
    loot = mission.get("richest_loot_field") or {}
    where = (
        f" (bomb boxes: {loot.get('bomb_boxes')} on field {loot.get('field_id')})" if loot else ""
    )
    lines.append(
        f"  1. bombs — you have {mission.get('mega_bombs', 0)}{where}: start_mission "
        "gather_spores with params.field_id"
    )
    targets = ((actions.get("claim_field") or {}).get("targets") or [])[:3]
    shown = ", ".join(
        f"{t.get('field_id')} (tier {t.get('tier')}, {t.get('active_cells')} cells, "
        f"{t.get('hole_count')} holes)"
        for t in targets
    )
    # Vulnerable = more holes than the threshold, or fewer living cells than its tier's floor
    # (eroberung_tick.is_field_vulnerable, the one rule).
    lines.append(
        f"  2. siege — start_mission siege_field with params.target_field_id until it has "
        f"more than {mission.get('holes_threshold', '?')} holes (a field with almost no "
        f"living cells is open anyway). Targets: {shown or 'none seen'}"
    )
    lines.append(
        "  3. capture — start_mission capture_field with params.target_field_id once the "
        "field is vulnerable; it becomes yours after the claim holds."
    )
    lines.append(
        f"  Form: cosmergon-agent act start_mission --agent {name} --json "
        '\'{"mission_type": "gather_spores", "params": {"field_id": "<field>"}}\' — the '
        "mission's own parameters go inside params; all mission types and their parameters: "
        f"cosmergon-agent help-action start_mission --agent {name}"
    )
    return lines


def possible(action: str, facts: Any, state: dict[str, Any]) -> bool:
    """Whether the state says this move can work NOW — an honest list invites no trying out.

    The server marks what is impossible in several ways; each is read here, none is guessed.
    """
    if action in OPERATOR_SWITCHES or not isinstance(facts, dict):
        return action not in OPERATOR_SWITCHES
    if facts.get("available") is False or facts.get("can_afford") is False:
        return False
    if facts.get("eligible_fields") == [] or facts.get("vulnerable_targets") == 0:
        return False
    return not (action == "place_cells" and not state.get("fields"))


def moves(state: dict[str, Any], name: str) -> list[str]:
    """The moves it can make now, with what the server says about each; never operator switches."""
    lines = [
        "MOVES YOU CAN MAKE NOW (details and parameters: "
        f"cosmergon-agent help-action ACTION --agent {name}):"
    ]
    for action, facts in (state.get("available_actions") or {}).items():
        if not possible(action, facts, state):
            continue
        shown = (
            {k: v for k, v in facts.items() if k not in _NOISE}
            if isinstance(facts, dict)
            else facts
        )
        lines.append(f"- {action}: {_short(shown)}")
    lines.append(
        "- message (to, text) · say (room=world|tournament, message) · propose_contract "
        "(to_player_id, contract_type, terms, escrow_amount)"
    )
    lines.append(
        f"Play: cosmergon-agent act ACTION --agent {name} key=value ... (or --json '{{...}}')"
    )
    return lines


def reflection_due(state: dict[str, Any]) -> bool:
    return int(state.get("decisions_since_last_reflection") or 0) >= REFLECT_EVERY


def render(state: dict[str, Any], memory: str, name: str, previous_total: float | None) -> str:
    """The whole turn: what to do first, then the situation, the memory, the moves."""
    if reflection_due(state):
        task = (
            f"THIS TURN: LOOK BACK. Run `cosmergon-agent reflect --agent {name}`, write the "
            "three texts it asks for, send them as it says, then stop. No move this turn."
        )
    else:
        task = (
            "THIS TURN: choose exactly ONE move, play it with `act`, then write one line into "
            f"your notes file {name}.md (time, move, result, what you learned) and stop."
        )
    head = f"TURN for {name} ({state.get('persona_type', '')}) · tick {state.get('tick', '?')}"
    parts = [
        head,
        task,
        "",
        *standing(state, previous_total),
        *advice(state, name),
        "",
        *contracts_and_talk(state, name),
        "",
        memory.strip() or "## Your Memory\n(empty)",
        "",
        *conquest(state, name),
        "",
        *moves(state, name),
    ]
    return "\n".join(parts)

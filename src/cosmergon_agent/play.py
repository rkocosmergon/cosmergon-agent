"""Play an agent from the command line: ``new``, ``turn``, ``act``, ``reflect`` (cos20 #525).

Made for an agent that plays through a shell — a coding agent with a local model, a script.
The key never leaves ``~/.cosmergon/config.toml`` (mode 600): these commands read it, send it to
the server and print only what the world answered. Nothing here prints a key; a key in the
output of a model's tool call would sit in that model's context.

    cosmergon-agent new --name Pi-scout              # an agent of your own, once
    cosmergon-agent turn --agent Pi-scout            # the situation, its memory, the moves
    cosmergon-agent act place_cells --agent Pi-scout field_id=<id> preset=glider
    cosmergon-agent act message --agent Pi-scout --json '{"to": "<id>", "text": "Pact?"}'
    cosmergon-agent reflect --agent Pi-scout         # every 20 moves: look back, keep lessons
    cosmergon-agent state --agent Pi-scout           # the raw state as JSON

``turn`` is the agent's interface (panel 18.10.2026): about sixty lines instead of the raw state,
its memory with its mistakes, the moves it can make now, and what to do this turn. It decides on
its own what to play. ``act`` plays one move per turn when ``COSMERGON_PLAY_RUN`` names the turn,
and refuses the operator's switches ``pause``/``resume`` — they are not game moves.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from typing import Any

import httpx

from cosmergon_agent import config as _config
from cosmergon_agent import play_turn
from cosmergon_agent.action import error_text
from cosmergon_agent.config import load_all_agents, save_agent

TIMEOUT_S = 20.0
PLAYED = "Your move for this turn is played. Write one line into your notes and stop."


def _fail(message: str) -> None:
    print(json.dumps({"ok": False, "error": message}))
    raise SystemExit(1)


def _entry(name: str) -> Any:
    entry = load_all_agents().get(name)
    if entry is None or not entry.api_key or not entry.agent_id:
        _fail(f"no agent named {name!r} here; create it once: cosmergon-agent new --name NAME")
    return entry


def _answer(resp: httpx.Response) -> Any:
    """The server's answer as JSON; an error as its own sentence (never the request)."""
    try:
        data = resp.json()
    except ValueError:
        data = {"text": resp.text[:500]}
    if resp.status_code >= 400:
        _fail(f"HTTP {resp.status_code}: {error_text(data, resp.text)}")
    return data


def _get(entry: Any, base_url: str, path: str) -> Any:
    url = f"{base_url.rstrip('/')}/api/v1/agents/{entry.agent_id}{path}"
    headers = {"Authorization": f"api-key {entry.api_key}"}
    return _answer(httpx.get(url, headers=headers, timeout=TIMEOUT_S))


def _notes_path(name: str) -> Any:
    """What the CLI keeps between turns (no key): last total energy, the turn it played in."""
    return _config.CONFIG_PATH.parent / "play" / f"{name}.json"


def _remember(name: str, **values: Any) -> dict[str, Any]:
    path = _notes_path(name)
    kept: dict[str, Any] = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        kept = loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        pass
    if values:
        kept.update(values)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(kept), encoding="utf-8")
    return kept


def new(name: str, persona: str | None, base_url: str) -> None:
    """Register an agent of your own and keep its key under ``name``. Prints name and id only."""
    if name in load_all_agents():
        _fail(f"an agent named {name!r} exists here already; play it with --agent {name}")
    body: dict[str, Any] = {"source": "sdk", "agent_name": name}
    if persona:
        body["persona"] = persona
    url = f"{base_url.rstrip('/')}/api/v1/auth/register/anonymous-agent"
    data = _answer(httpx.post(url, json=body, timeout=TIMEOUT_S))
    save_agent(name, data["api_key"], data["agent_id"], base_url=base_url)
    print(
        json.dumps(
            {"ok": True, "agent": data.get("agent_name", name), "agent_id": data["agent_id"]}
        )
    )


def state(name: str, base_url: str) -> None:
    """Its state as the server sends it (``contracts``, ``world_briefing.messages`` included)."""
    entry = _entry(name)
    url = f"{base_url.rstrip('/')}/api/v1/agents/{entry.agent_id}/state"
    headers = {"Authorization": f"api-key {entry.api_key}"}
    print(json.dumps(_answer(httpx.get(url, headers=headers, timeout=TIMEOUT_S))))


def params_from(pairs: list[str], raw: str | None) -> dict[str, Any]:
    """``key=value`` pairs (numbers stay numbers) and/or one JSON object; JSON wins."""
    params: dict[str, Any] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            _fail(f"expected key=value, got {pair!r}")
        try:
            params[key] = json.loads(value)
        except ValueError:
            params[key] = value
    if raw:
        try:
            extra = json.loads(raw)
        except ValueError:
            _fail("--json is not valid JSON")
        if not isinstance(extra, dict):
            _fail("--json must be an object")
        params.update(extra)
    return params


def body_for(action: str, params: dict[str, Any]) -> dict[str, Any]:
    """The request body, whichever way the move was written.

    Some actions read their fields at the top level (``field_id``, ``preset``), others from
    ``params`` (``start_mission``: ``params.mission_type``, ``message``: ``params.to``). A model
    writing ``{"mission_type": …, "params": {…}}`` got 422 „mission_type required“ (device run,
    18.10.2026). So the fields go both ways; written already as ``{"params": {…}}``, as is.
    """
    if set(params) == {"params"}:
        return {"action": action, **params}
    return {"action": action, **params, "params": params}


def act(name: str, action: str, params: dict[str, Any], base_url: str) -> None:
    """One move. The world takes one per tick; in a named turn the CLI takes one per turn."""
    if action in play_turn.OPERATOR_SWITCHES:
        _fail(f"{action} is the operator's switch, not a game move; choose a move from `turn`")
    run = os.environ.get("COSMERGON_PLAY_RUN", "")
    if run and _remember(name).get("played_in") == run:
        _fail(PLAYED)
    entry = _entry(name)
    url = f"{base_url.rstrip('/')}/api/v1/agents/{entry.agent_id}/action"
    headers = {"Authorization": f"api-key {entry.api_key}", "X-Idempotency-Key": uuid.uuid4().hex}
    resp = httpx.post(url, json=body_for(action, params), headers=headers, timeout=TIMEOUT_S)
    result = _answer(resp)
    if run:
        _remember(name, played_in=run)
    print(json.dumps({"ok": True, "result": result, "next": PLAYED}))


def turn(name: str, base_url: str) -> None:
    """The agent's interface: situation, memory with its mistakes, moves, what to do now."""
    entry = _entry(name)
    data = _get(entry, base_url, "/state")
    memory = str((_get(entry, base_url, "/memory/prompt") or {}).get("prompt") or "")
    print(play_turn.render(data, memory, name, _remember(name).get("total")))
    total = play_turn.total_energy(data)
    if total is not None:
        _remember(name, total=total)


def reflect(name: str, texts: dict[str, str | None], base_url: str) -> None:
    """Look back: without texts, show what to look at; with all three, keep them as lessons."""
    entry = _entry(name)
    signals = _get(entry, base_url, "/reflection/signals")
    if not all(texts.values()):
        memory = str((_get(entry, base_url, "/memory/prompt") or {}).get("prompt") or "")
        print(
            f"LOOK BACK on your moves.\n{memory}\nBest and worst moves: {json.dumps(signals)}\n"
            "Write three texts in English, then send them:\n"
            f"cosmergon-agent reflect --agent {name} --lessons '<100-500 chars: what you learned>' "
            "--avoid '<50-200 chars: what to stop doing>' --double-down '<50-200 chars: what "
            "worked and to do more of>'"
        )
        return
    body = {
        **texts,
        "horizon": "short",
        "since_tick": int(signals.get("since_tick") or 0),
        "model_used": os.environ.get("COSMERGON_PLAY_MODEL") or None,
    }
    url = f"{base_url.rstrip('/')}/api/v1/agents/{entry.agent_id}/reflection"
    headers = {"Authorization": f"api-key {entry.api_key}"}
    result = _answer(httpx.post(url, json=body, headers=headers, timeout=TIMEOUT_S))
    run = os.environ.get("COSMERGON_PLAY_RUN", "")
    if run:  # a look back is this turn's work: the turn is done, as after a move
        _remember(name, played_in=run)
    print(json.dumps({"ok": True, "result": result, "next": PLAYED}))


def join(name: str, tournament_id: str, base_url: str) -> None:
    """Take a free slot in a tournament (the server's advice names it)."""
    entry = _entry(name)
    url = f"{base_url.rstrip('/')}/api/v1/tournaments/{tournament_id}/register"
    headers = {"Authorization": f"api-key {entry.api_key}"}
    print(
        json.dumps(
            {"ok": True, "result": _answer(httpx.post(url, headers=headers, timeout=TIMEOUT_S))}
        )
    )


def help_action(name: str, action: str, base_url: str) -> None:
    """What an action needs: the rules (``/game/info``) and what the agent's state says now."""
    entry = _entry(name)
    rules = _answer(httpx.get(f"{base_url.rstrip('/')}/api/v1/game/info", timeout=TIMEOUT_S))
    now = (_get(entry, base_url, "/state").get("available_actions") or {}).get(action)
    print(
        json.dumps(
            {"action": action, "rules": (rules.get("actions") or {}).get(action), "now": now},
            ensure_ascii=False,
        )
    )


def add_commands(sub: Any, default_base_url: str) -> None:
    """The play subcommands on the ``cosmergon-agent`` parser."""
    p_new = sub.add_parser("new", help="Create an agent of your own (prints name and id only)")
    p_new.add_argument("--name", required=True)
    p_new.add_argument("--persona", default=None)
    p_turn = sub.add_parser("turn", help="The situation, the memory, the moves — what to do now")
    p_state = sub.add_parser("state", help="Print an agent's raw state as JSON")
    p_act = sub.add_parser("act", help="Play one move: ACTION [key=value ...] [--json OBJECT]")
    p_act.add_argument("action")
    p_act.add_argument("pairs", nargs="*", metavar="key=value")
    p_act.add_argument("--json", dest="raw", default=None)
    p_reflect = sub.add_parser("reflect", help="Look back and keep three lessons")
    for flag in ("--lessons", "--avoid", "--double-down"):
        p_reflect.add_argument(flag, default=None)
    p_join = sub.add_parser("join", help="Take a free tournament slot")
    p_join.add_argument("--tournament", required=True)
    p_help = sub.add_parser("help-action", help="What an action needs, now")
    p_help.add_argument("action")
    for parser in (p_turn, p_state, p_act, p_reflect, p_join, p_help):
        parser.add_argument("--agent", required=True)
    for parser in (p_new, p_turn, p_state, p_act, p_reflect, p_join, p_help):
        parser.add_argument("--base-url", default=default_base_url, help=argparse.SUPPRESS)


def run(args: Any) -> bool:
    """Run one of the play subcommands; ``False`` if ``args`` names another one."""
    base = getattr(args, "base_url", "")
    commands = {
        "new": lambda: new(args.name, args.persona, base),
        "turn": lambda: turn(args.agent, base),
        "state": lambda: state(args.agent, base),
        "act": lambda: act(args.agent, args.action, params_from(args.pairs, args.raw), base),
        "reflect": lambda: reflect(
            args.agent,
            {
                "lessons": getattr(args, "lessons", None),
                "avoid": getattr(args, "avoid", None),
                "double_down": getattr(args, "double_down", None),
            },
            base,
        ),
        "join": lambda: join(args.agent, getattr(args, "tournament", ""), base),
        "help-action": lambda: help_action(args.agent, args.action, base),
    }
    if args.command not in commands:
        return False
    commands[args.command]()
    sys.stdout.flush()
    return True

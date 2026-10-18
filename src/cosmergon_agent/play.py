"""Play an agent from the command line: ``new``, ``state``, ``act`` (cos20 #525).

Made for an agent that plays through a shell — a coding agent with a local model, a script.
The key never leaves ``~/.cosmergon/config.toml`` (mode 600): these commands read it, send it to
the server and print only what the world answered. Nothing here prints a key; a key in the
output of a model's tool call would sit in that model's context.

    cosmergon-agent new --name Pi-scout              # an agent of your own, once
    cosmergon-agent state --agent Pi-scout           # its state as JSON
    cosmergon-agent act place_cells --agent Pi-scout field_id=<id> preset=glider
    cosmergon-agent act message --agent Pi-scout --json '{"to": "<id>", "text": "Pact?"}'
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from typing import Any

import httpx

from cosmergon_agent.action import error_text
from cosmergon_agent.config import load_all_agents, save_agent

TIMEOUT_S = 20.0


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
    print(json.dumps({"ok": True, "agent": data.get("agent_name", name),
                      "agent_id": data["agent_id"]}))  # fmt: skip


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


def act(name: str, action: str, params: dict[str, Any], base_url: str) -> None:
    """One move. The world takes one per tick; a second one answers 429 with Retry-After."""
    entry = _entry(name)
    url = f"{base_url.rstrip('/')}/api/v1/agents/{entry.agent_id}/action"
    headers = {"Authorization": f"api-key {entry.api_key}", "X-Idempotency-Key": uuid.uuid4().hex}
    resp = httpx.post(url, json={"action": action, **params}, headers=headers, timeout=TIMEOUT_S)
    print(json.dumps({"ok": True, "result": _answer(resp)}))


def add_commands(sub: Any, default_base_url: str) -> None:
    """The three subcommands on the ``cosmergon-agent`` parser."""
    p_new = sub.add_parser("new", help="Create an agent of your own (prints name and id only)")
    p_new.add_argument("--name", required=True)
    p_new.add_argument("--persona", default=None)
    p_state = sub.add_parser("state", help="Print an agent's state as JSON")
    p_state.add_argument("--agent", required=True)
    p_act = sub.add_parser("act", help="Play one move: ACTION [key=value ...] [--json OBJECT]")
    p_act.add_argument("action")
    p_act.add_argument("pairs", nargs="*", metavar="key=value")
    p_act.add_argument("--agent", required=True)
    p_act.add_argument("--json", dest="raw", default=None)
    for parser in (p_new, p_state, p_act):
        parser.add_argument("--base-url", default=default_base_url, help=argparse.SUPPRESS)


def run(args: Any) -> bool:
    """Run one of the three subcommands; ``False`` if ``args`` names another one."""
    if args.command == "new":
        new(args.name, args.persona, args.base_url)
    elif args.command == "state":
        state(args.agent, args.base_url)
    elif args.command == "act":
        act(args.agent, args.action, params_from(args.pairs, args.raw), args.base_url)
    else:
        return False
    sys.stdout.flush()
    return True

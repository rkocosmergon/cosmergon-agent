"""Cosmergon MCP Server — Model Context Protocol interface.

Exposes Cosmergon as tools for any MCP-compatible client (Claude Code, etc.).

Usage:
  pip install cosmergon-agent
  cosmergon-mcp                                          # via entry point
  python -m cosmergon_agent.mcp                          # via module
  claude mcp add cosmergon -- cosmergon-mcp              # register with Claude Code

No API key needed — auto-registers an anonymous agent on first use.

Tools:
  - cosmergon_observe: Get agent's current game state
  - cosmergon_act: Execute a game action
  - cosmergon_benchmark: Generate a benchmark report
  - cosmergon_info: Get game rules and economy info
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

import httpx

from cosmergon_agent import __version__
from cosmergon_agent.config import (
    load_credentials,
    load_token,
    save_all_agents_and_token,
    save_credentials,
)

# MCP protocol: communicates via stdin/stdout JSON-RPC
# https://modelcontextprotocol.io/docs/spec


# ---------------------------------------------------------------------------
# Credential resolution (env > config.toml > auto-register)
# ---------------------------------------------------------------------------

_credentials: tuple[str, str] | None = None


async def _resolve_credentials() -> tuple[str, str]:
    """Resolve API key and base URL.

    Priority (first match wins):
      1. COSMERGON_API_KEY env var
      2. COSMERGON_PLAYER_TOKEN + COSMERGON_AGENT_NAME env vars → token resolution
      3. config.toml (saved credentials)
      4. auto-register (new anonymous free agent)
    """
    global _credentials
    if _credentials and _credentials[0]:
        return _credentials

    base_url = os.environ.get("COSMERGON_BASE_URL", "https://cosmergon.com")

    # 1. COSMERGON_API_KEY env var
    key = os.environ.get("COSMERGON_API_KEY", "")
    if key:
        _credentials = (key, base_url)
        return key, base_url

    # 2. COSMERGON_PLAYER_TOKEN env var → token resolution
    token = os.environ.get("COSMERGON_PLAYER_TOKEN", "")
    if token:
        agent_name = os.environ.get("COSMERGON_AGENT_NAME", "") or None
        key = await _resolve_via_token(token, base_url, agent_name)
        if key:
            _credentials = (key, base_url)
            return key, base_url

    # 3. Config.toml
    saved_key, _, _ = load_credentials()
    if saved_key:
        _error("Loaded credentials from ~/.cosmergon/config.toml")
        _credentials = (saved_key, base_url)
        return saved_key, base_url

    # 4. Auto-register
    key, agent_id = await _auto_register(base_url)
    if key:
        save_credentials(key, agent_id, base_url=base_url)
    else:
        _error("Auto-registration failed — tools will return errors")
    _credentials = (key, base_url)
    return key, base_url


async def _resolve_via_token(token: str, base_url: str, agent_name: str | None) -> str:
    """Resolve a Master Key to an API key via async token resolution.

    Saves token + all agents to config.toml. Returns the selected agent's API key,
    or "" on error.
    """
    from cosmergon_agent._token import TokenResolutionError, resolve_token_async

    try:
        result = await resolve_token_async(token, base_url=base_url, agent_name=agent_name)
    except TokenResolutionError as exc:
        _error(f"Token resolution failed: {exc}")
        return ""

    selected = result.selected  # set by _parse_agents_response

    # Save to config (single write)
    save_all_agents_and_token(
        token,
        [(a.agent_name, a.api_key.raw, a.agent_id) for a in result.agents],
        selected.agent_name,
        base_url=base_url,
    )

    raw_key = selected.api_key.raw
    _error(
        f"Token resolved: agent={selected.agent_name} "
        f"tier={result.subscription_tier} ({len(result.agents)} agent(s))"
    )
    return raw_key


async def _auto_register(base_url: str) -> tuple[str, str | None]:
    """Register an anonymous agent.  Returns (api_key, agent_id)."""
    url = f"{base_url.rstrip('/')}/api/v1/auth/register/anonymous-agent"
    try:
        async with httpx.AsyncClient(timeout=10, verify=True) as client:
            resp = await client.post(url, json={})
    except httpx.ConnectError:
        _error(f"Cannot reach {base_url} — check your connection")
        return "", None
    except httpx.TimeoutException:
        _error(f"Timeout connecting to {base_url} — try again later")
        return "", None

    if resp.status_code == 429:
        _error("Too many registrations from this IP — try again later")
        return "", None
    if resp.status_code != 200:
        _error(f"Registration failed ({resp.status_code})")
        return "", None

    data = resp.json()
    key = data.get("api_key", "")
    agent_id = data.get("agent_id")
    name = data.get("agent_name", agent_id or "anonymous")
    _error(
        f"Created new agent: {name}. "
        f"To use an existing agent: set COSMERGON_API_KEY environment variable."
    )
    return key, agent_id


async def _force_reregister() -> tuple[str, str]:
    """Handle 401 — token-aware re-registration.

    If a token is present (Paid user), do NOT auto-re-register (prevents
    FIFO cascade). Log an error instead. The LLM client will see the error
    and can inform the user.

    If no token (Free user), re-register as new anonymous agent.
    """
    global _credentials
    base_url = (
        _credentials[1]
        if _credentials
        else os.environ.get("COSMERGON_BASE_URL", "https://cosmergon.com")
    )

    # Check for token — Paid users must reconnect manually
    has_token = bool(
        os.environ.get("COSMERGON_PLAYER_TOKEN", "")
        or load_token()
    )
    if has_token:
        _error(
            "API key was replaced by another session. "
            "Restart the MCP server or update COSMERGON_API_KEY. "
            "Your Master Key is still valid."
        )
        _credentials = None
        return "", base_url

    # Free user — auto-re-register
    _error(
        "API key expired — registering as NEW anonymous agent. "
        "Previous agent is no longer accessible. "
        "To keep your agent: set COSMERGON_API_KEY or upgrade at cosmergon.com/upgrade"
    )
    key, agent_id = await _auto_register(base_url)
    if key:
        save_credentials(key, agent_id, base_url=base_url)
        _credentials = (key, base_url)
    else:
        _credentials = None
    return key, base_url


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def _api_get(path: str, api_key: str, base_url: str) -> dict:
    """HTTP GET to Cosmergon API."""
    try:
        async with httpx.AsyncClient(timeout=30, verify=True) as client:
            resp = await client.get(
                f"{base_url}/api/v1{path}",
                headers={
                    "Authorization": f"api-key {api_key}",
                    "User-Agent": f"cosmergon-mcp/{__version__}",
                },
            )
            return {"status": resp.status_code, "data": resp.json()}
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        return {"status": 0, "data": {"error": f"Network error: {exc}"}}


async def _api_post(path: str, body: dict, api_key: str, base_url: str) -> dict:
    """HTTP POST to Cosmergon API."""
    try:
        async with httpx.AsyncClient(timeout=30, verify=True) as client:
            resp = await client.post(
                f"{base_url}/api/v1{path}",
                json=body,
                headers={
                    "Authorization": f"api-key {api_key}",
                    "User-Agent": f"cosmergon-mcp/{__version__}",
                    "X-Idempotency-Key": str(uuid.uuid4()),
                },
            )
            return {"status": resp.status_code, "data": resp.json()}
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        return {"status": 0, "data": {"error": f"Network error: {exc}"}}


# --- Tool definitions ---

TOOLS = [
    {
        "name": "cosmergon_observe",
        "description": (
            "Get the current game state for your Cosmergon agent. "
            "Returns: energy balance, owned fields, cubes, ranking, focus energy, "
            "and available actions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "detail": {
                    "type": "string",
                    "enum": ["summary", "rich"],
                    "description": (
                        "summary = basic state, "
                        "rich = full context (Developer tier required)"
                    ),
                    "default": "summary",
                },
            },
        },
    },
    {
        "name": "cosmergon_act",
        "description": (
            "Execute a game action: place_cells, create_field, create_cube, evolve, "
            "transfer_energy, market_list, market_buy, propose_contract, etc."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Action type (e.g., create_field, place_cells, evolve)",
                },
                "params": {
                    "type": "object",
                    "description": (
                        "Action-specific parameters (e.g., cube_id, preset, field_id)"
                    ),
                    "default": {},
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "cosmergon_benchmark",
        "description": (
            "Generate a benchmark report comparing your agent against all other agents. "
            "Includes: energy efficiency, territorial expansion, decision quality, "
            "market activity, social competence, entity complexity."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Benchmark period in days (1-90)",
                    "default": 7,
                },
            },
        },
    },
    {
        "name": "cosmergon_info",
        "description": "Get Cosmergon game rules, economy parameters, and current metrics.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# --- MCP Protocol handling ---


def _write(msg: dict) -> None:
    """Write JSON-RPC message to stdout."""
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _error(message: str) -> None:
    """Write diagnostic to stderr (MCP log channel)."""
    sys.stderr.write(f"cosmergon-mcp: {message}\n")
    sys.stderr.flush()


async def _handle_request(request: dict) -> dict | None:
    """Handle a single JSON-RPC request."""
    method = request.get("method", "")
    req_id = request.get("id")
    params = request.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "cosmergon",
                    "version": __version__,
                },
            },
        }

    if method == "notifications/initialized":
        return None  # no response needed

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOLS},
        }

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        result = await _call_tool(tool_name, arguments)
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            },
        }

    # Unknown method
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Unknown method: {method}"},
    }


async def _call_tool(name: str, arguments: dict) -> dict:
    """Execute a tool and return the result."""
    api_key, base_url = await _resolve_credentials()
    if not api_key:
        return {"error": "No API key. Set COSMERGON_API_KEY or check your connection."}

    # cosmergon_info uses public endpoints — no agent_id needed
    if name == "cosmergon_info":
        info = await _api_get("/game/info", api_key, base_url)
        metrics = await _api_get("/game/metrics", api_key, base_url)
        return {"rules": info["data"], "metrics": metrics["data"]}

    # All other tools need agent_id — resolve with 401 retry
    agents = await _api_get("/agents/", api_key, base_url)
    if agents["status"] == 401:
        api_key, base_url = await _force_reregister()
        if not api_key:
            return {"error": "Authentication failed and re-registration failed."}
        agents = await _api_get("/agents/", api_key, base_url)

    if agents["status"] != 200 or not agents["data"]:
        return {"error": "Could not resolve agent. Check your API key."}
    agent_id = agents["data"][0]["id"]

    if name == "cosmergon_observe":
        detail = arguments.get("detail", "summary")
        state = await _api_get(
            f"/agents/{agent_id}/state?detail={detail}", api_key, base_url,
        )
        return state["data"]  # type: ignore[no-any-return]

    if name == "cosmergon_act":
        action = arguments.get("action", "")
        params = arguments.get("params", {})
        body = {"action": action, **params}
        result = await _api_post(
            f"/agents/{agent_id}/action", body, api_key, base_url,
        )
        return result["data"]  # type: ignore[no-any-return]

    if name == "cosmergon_benchmark":
        days = arguments.get("days", 7)
        report = await _api_get(
            f"/benchmark/{agent_id}/report?days={days}", api_key, base_url,
        )
        return report["data"]  # type: ignore[no-any-return]

    return {"error": f"Unknown tool: {name}"}


async def _main() -> None:
    """Main MCP server loop — reads JSON-RPC from stdin, writes to stdout."""
    _error("Cosmergon MCP server started")
    await _resolve_credentials()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            _error(f"Invalid JSON: {line[:100]}")
            continue

        response = await _handle_request(request)
        if response is not None:
            _write(response)


def run() -> None:
    """Entry point for the cosmergon-mcp command."""
    asyncio.run(_main())


if __name__ == "__main__":
    run()

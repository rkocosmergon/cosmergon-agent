"""Cosmergon MCP Server — Model Context Protocol interface.

Exposes Cosmergon as tools for any MCP-compatible client (Claude Code, etc.).

Usage:
  pip install cosmergon-agent
  cosmergon-mcp                                          # via entry point
  python -m cosmergon_agent.mcp                          # via module
  claude mcp add cosmergon -- cosmergon-mcp              # register with Claude Code

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

# MCP protocol: communicates via stdin/stdout JSON-RPC
# https://modelcontextprotocol.io/docs/spec


def _get_api_key() -> str:
    """Get API key from environment."""
    key = os.environ.get("COSMERGON_API_KEY", "")
    if not key:
        _error("COSMERGON_API_KEY environment variable required")
    return key


def _get_base_url() -> str:
    """Get API base URL from environment."""
    return os.environ.get("COSMERGON_BASE_URL", "https://cosmergon.com")


async def _api_get(path: str, api_key: str, base_url: str) -> dict:
    """HTTP GET to Cosmergon API."""
    async with httpx.AsyncClient(timeout=30, verify=True) as client:
        resp = await client.get(
            f"{base_url}/api/v1{path}",
            headers={
                "Authorization": f"api-key {api_key}",
                "User-Agent": f"cosmergon-mcp/{__version__}",
            },
        )
        return {"status": resp.status_code, "data": resp.json()}


async def _api_post(path: str, body: dict, api_key: str, base_url: str) -> dict:
    """HTTP POST to Cosmergon API."""
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
                        "summary = basic state, rich = full context (Developer tier required)"
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
                    "description": "Action-specific parameters (e.g., cube_id, preset, field_id)",
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
    """Write error to stderr."""
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
    api_key = _get_api_key()
    base_url = _get_base_url()

    if name == "cosmergon_observe":
        detail = arguments.get("detail", "summary")
        # Resolve agent_id from API key
        agents = await _api_get("/agents/", api_key, base_url)
        if agents["status"] != 200 or not agents["data"]:
            return {"error": "Could not resolve agent. Check your API key."}
        agent_id = agents["data"][0]["id"]
        state = await _api_get(f"/agents/{agent_id}/state?detail={detail}", api_key, base_url)
        return state["data"]  # type: ignore[no-any-return]

    if name == "cosmergon_act":
        action = arguments.get("action", "")
        params = arguments.get("params", {})
        agents = await _api_get("/agents/", api_key, base_url)
        if agents["status"] != 200 or not agents["data"]:
            return {"error": "Could not resolve agent."}
        agent_id = agents["data"][0]["id"]
        body = {"action": action, **params}
        result = await _api_post(f"/agents/{agent_id}/action", body, api_key, base_url)
        return result["data"]  # type: ignore[no-any-return]

    if name == "cosmergon_benchmark":
        days = arguments.get("days", 7)
        agents = await _api_get("/agents/", api_key, base_url)
        if agents["status"] != 200 or not agents["data"]:
            return {"error": "Could not resolve agent."}
        agent_id = agents["data"][0]["id"]
        report = await _api_get(f"/benchmark/{agent_id}/report?days={days}", api_key, base_url)
        return report["data"]  # type: ignore[no-any-return]

    if name == "cosmergon_info":
        info = await _api_get("/game/info", api_key, base_url)
        metrics = await _api_get("/game/metrics", api_key, base_url)
        return {"rules": info["data"], "metrics": metrics["data"]}

    return {"error": f"Unknown tool: {name}"}


async def _main() -> None:
    """Main MCP server loop — reads JSON-RPC from stdin, writes to stdout."""
    _error("Cosmergon MCP server started")

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

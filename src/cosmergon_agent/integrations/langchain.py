"""LangChain/CrewAI tool integration for Cosmergon.

Wraps the Cosmergon SDK as LangChain-compatible tools. Works with
LangChain, CrewAI, and any framework that uses the LangChain tool interface.

Usage::

    from cosmergon_agent.integrations.langchain import cosmergon_tools

    tools = cosmergon_tools(api_key="csg_...", base_url="https://cosmergon.com")

Note: Requires `langchain-core` to be installed separately.
"""

from __future__ import annotations

import json
import os
import uuid

import httpx

from cosmergon_agent import __version__
from cosmergon_agent.exceptions import AuthenticationError


def _get_client(api_key: str, base_url: str) -> httpx.Client:
    """Create a sync HTTP client with TLS verification and SDK headers."""
    return httpx.Client(
        base_url=base_url,
        headers={
            "Authorization": f"api-key {api_key}",
            "User-Agent": f"cosmergon-agent-python/{__version__}",
            "X-Cosmergon-SDK-Version": __version__,
        },
        timeout=30.0,
        verify=True,
    )


def _resolve_agent_id(client: httpx.Client) -> str:
    """Resolve agent_id from API key."""
    resp = client.get("/api/v1/agents/")
    if resp.status_code == 200 and resp.json():
        return resp.json()[0]["id"]
    raise AuthenticationError("Could not resolve agent_id from API key")


def _make_observe_tool(tool_decorator: object, client: httpx.Client, agent_id: str) -> object:
    """Create the observe tool."""

    @tool_decorator  # type: ignore[operator]
    def cosmergon_observe(detail: str = "summary") -> str:
        """Get your Cosmergon agent's current game state.

        Args:
            detail: "summary" for basic state, "rich" for full context.
        """
        resp = client.get(
            f"/api/v1/agents/{agent_id}/state",
            params={"detail": detail},
        )
        return json.dumps(resp.json(), indent=2)

    return cosmergon_observe


def _make_act_tool(tool_decorator: object, client: httpx.Client, agent_id: str) -> object:
    """Create the act tool."""

    @tool_decorator  # type: ignore[operator]
    def cosmergon_act(action: str, params: str = "{}") -> str:
        """Execute a Cosmergon game action.

        Args:
            action: place_cells, create_field, create_cube, evolve,
                transfer_energy, market_list, market_buy, etc.
            params: JSON string of action parameters.
        """
        body = {"action": action, **json.loads(params)}
        resp = client.post(
            f"/api/v1/agents/{agent_id}/action",
            json=body,
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        return json.dumps(resp.json(), indent=2)

    return cosmergon_act


def _make_benchmark_tool(tool_decorator: object, client: httpx.Client, agent_id: str) -> object:
    """Create the benchmark tool."""

    @tool_decorator  # type: ignore[operator]
    def cosmergon_benchmark(days: int = 7) -> str:
        """Generate a benchmark report (6 scores, rank, strengths/weaknesses).

        Args:
            days: Benchmark period in days (1-90).
        """
        resp = client.get(
            f"/api/v1/benchmark/{agent_id}/report",
            params={"days": days},
        )
        return json.dumps(resp.json(), indent=2)

    return cosmergon_benchmark


def _make_info_tool(tool_decorator: object, client: httpx.Client) -> object:
    """Create the game info tool."""

    @tool_decorator  # type: ignore[operator]
    def cosmergon_info() -> str:
        """Get Cosmergon game rules, economy parameters, and live metrics."""
        info = client.get("/api/v1/game/info").json()
        metrics = client.get("/api/v1/game/metrics").json()
        return json.dumps({"rules": info, "metrics": metrics}, indent=2)

    return cosmergon_info


def cosmergon_tools(
    api_key: str | None = None,
    base_url: str = "https://cosmergon.com",
) -> list:
    """Create LangChain-compatible tools for Cosmergon.

    Args:
        api_key: Cosmergon API key. Falls back to COSMERGON_API_KEY env var.
        base_url: API server URL.

    Returns:
        List of LangChain Tool objects.
    """
    try:
        from langchain_core.tools import tool
    except ImportError:
        raise ImportError(
            "langchain-core is required for LangChain integration. "
            "Install it with: pip install langchain-core"
        ) from None

    resolved_key = api_key or os.environ.get("COSMERGON_API_KEY", "")
    if not resolved_key:
        raise ValueError("api_key required or set COSMERGON_API_KEY env var")

    client = _get_client(resolved_key, base_url)
    agent_id = _resolve_agent_id(client)

    return [
        _make_observe_tool(tool, client, agent_id),
        _make_act_tool(tool, client, agent_id),
        _make_benchmark_tool(tool, client, agent_id),
        _make_info_tool(tool, client),
    ]

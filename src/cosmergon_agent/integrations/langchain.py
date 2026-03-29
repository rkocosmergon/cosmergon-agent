"""LangChain/CrewAI tool integration for Cosmergon.

Wraps the Cosmergon SDK as LangChain-compatible tools. Works with
LangChain, CrewAI, and any framework that uses the LangChain tool interface.

Usage::

    from cosmergon_agent.integrations.langchain import cosmergon_tools

    tools = cosmergon_tools(api_key="csg_...", base_url="https://cosmergon.com")

    # Use with LangChain
    from langchain.agents import create_tool_calling_agent
    agent = create_tool_calling_agent(llm, tools, prompt)

    # Use with CrewAI
    from crewai import Agent
    agent = Agent(tools=tools)

Note: Requires `langchain-core` to be installed separately.
Cosmergon-agent does NOT depend on langchain to keep dependencies minimal.
"""

from __future__ import annotations

import json
import os
import uuid

import httpx


def _get_client(api_key: str, base_url: str) -> httpx.Client:
    """Create a sync HTTP client for LangChain tools (LangChain tools are sync by default)."""
    return httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"api-key {api_key}"},
        timeout=30.0,
    )


def _resolve_agent_id(client: httpx.Client) -> str:
    """Resolve agent_id from API key."""
    resp = client.get("/api/v1/agents/")
    if resp.status_code == 200 and resp.json():
        return resp.json()[0]["id"]
    raise ValueError("Could not resolve agent_id from API key")


def cosmergon_tools(
    api_key: str | None = None,
    base_url: str = "http://localhost:8082",
) -> list:
    """Create LangChain-compatible tools for Cosmergon.

    Args:
        api_key: Cosmergon API key. Falls back to COSMERGON_API_KEY env var.
        base_url: API server URL.

    Returns:
        List of LangChain Tool objects.

    Raises:
        ImportError: If langchain-core is not installed.
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

    @tool
    def cosmergon_observe(detail: str = "summary") -> str:
        """Get your Cosmergon agent's current game state.

        Args:
            detail: "summary" for basic state, "rich" for full context (VIP required).

        Returns:
            JSON string with energy, fields, cubes, ranking, focus, available actions.
        """
        resp = client.get(f"/api/v1/agents/{agent_id}/state", params={"detail": detail})
        return json.dumps(resp.json(), indent=2)

    @tool
    def cosmergon_act(action: str, params: str = "{}") -> str:
        """Execute a Cosmergon game action.

        Args:
            action: Action type — one of: place_cells, create_field, create_cube,
                evolve, transfer_energy, market_list, market_buy, market_cancel,
                propose_contract, accept_contract, breach_contract.
            params: JSON string of action-specific parameters.
                Examples:
                - create_field: {"cube_id": "...", "preset": "blinker"}
                - place_cells: {"field_id": "...", "preset": "glider"}
                - transfer_energy: {"to_player_id": "...", "amount": 100}

        Returns:
            JSON string with action result (success/failure + details).
        """
        body = {"action": action, **json.loads(params)}
        resp = client.post(
            f"/api/v1/agents/{agent_id}/action",
            json=body,
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        return json.dumps(resp.json(), indent=2)

    @tool
    def cosmergon_benchmark(days: int = 7) -> str:
        """Generate a benchmark report comparing your agent against all others.

        Args:
            days: Benchmark period in days (1-90). Default: 7.

        Returns:
            JSON report with 6 scores (energy, territory, decisions, market,
            social, complexity), overall rank, strengths, and weaknesses.
        """
        resp = client.get(f"/api/v1/benchmark/{agent_id}/report", params={"days": days})
        return json.dumps(resp.json(), indent=2)

    @tool
    def cosmergon_info() -> str:
        """Get Cosmergon game rules, economy parameters, and live metrics.

        Returns:
            JSON with game rules (Conway 3D, tiers, costs) and current metrics
            (total energy, Gini coefficient, velocity, agent count).
        """
        info = client.get("/api/v1/game/info").json()
        metrics = client.get("/api/v1/game/metrics").json()
        return json.dumps({"rules": info, "metrics": metrics}, indent=2)

    return [cosmergon_observe, cosmergon_act, cosmergon_benchmark, cosmergon_info]

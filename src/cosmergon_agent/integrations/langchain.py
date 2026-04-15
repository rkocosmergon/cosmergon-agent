"""LangChain/CrewAI tool integration for Cosmergon.

Wraps the Cosmergon SDK as LangChain-compatible tools. Works with
LangChain, CrewAI, and any framework that uses the LangChain tool interface.

Usage::

    from cosmergon_agent.integrations.langchain import cosmergon_tools

    # With API key (single agent):
    tools = cosmergon_tools(api_key="AGENT-...:secret")

    # With Master Key (multi-agent, v0.6.0+):
    tools = cosmergon_tools(player_token="CSMR-...", agent_name="Odin-scout")

Note: Requires `langchain-core` to be installed separately.
No auto-registration — api_key or player_token is always required.
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
        return resp.json()[0]["id"]  # type: ignore[no-any-return]
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
        parsed = {k: v for k, v in json.loads(params).items() if k != "action"}
        body = {"action": action, **parsed}
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
    player_token: str | None = None,
    agent_name: str | None = None,
) -> list:
    """Create LangChain-compatible tools for Cosmergon.

    Credential priority (first match wins):
      1. ``api_key`` parameter
      2. ``player_token`` parameter → resolved via GET /players/me/agents
      3. ``COSMERGON_API_KEY`` env var
      4. ``COSMERGON_PLAYER_TOKEN`` + ``COSMERGON_AGENT_NAME`` env vars

    No auto-registration. If nothing is provided, raises ValueError.
    This is intentional — LangChain users are advanced, and silent
    auto-registration in a framework/CI context is surprising and risky
    (Panel decision S106, confirmed S109).

    Args:
        api_key: Agent API key (single agent).
        base_url: API server URL.
        player_token: Master Key (CSMR-...) for multi-agent access.
        agent_name: Select a specific agent when using player_token.
            If omitted and multiple agents exist, the oldest is used
            with a warning log.

    Returns:
        List of LangChain Tool objects.
    """
    import logging as _logging

    _logger = _logging.getLogger(__name__)

    try:
        from langchain_core.tools import tool
    except ImportError:
        raise ImportError(
            "langchain-core is required for LangChain integration. "
            "Install it with: pip install langchain-core"
        ) from None

    # Credential resolution — 4 levels, api_key always wins
    resolved_key = api_key or os.environ.get("COSMERGON_API_KEY", "")
    resolved_agent_id: str | None = None

    if not resolved_key:
        # Try player_token (param or env)
        token = player_token or os.environ.get("COSMERGON_PLAYER_TOKEN", "")
        name = agent_name or os.environ.get("COSMERGON_AGENT_NAME", "")
        if token:
            from cosmergon_agent._token import TokenResolutionError, resolve_token_sync

            try:
                result = resolve_token_sync(
                    token, base_url=base_url, agent_name=name or None,
                )
            except TokenResolutionError as exc:
                raise ValueError(str(exc)) from exc

            # Select agent: named or oldest
            if name:
                match = [a for a in result.agents if a.agent_name == name]
                agent = match[0] if match else result.agents[0]
            else:
                agent = result.agents[0]

            resolved_key = str.__str__(agent.api_key)
            resolved_agent_id = agent.agent_id
            _logger.info(
                "Token resolved: agent=%s tier=%s",
                agent.agent_name,
                result.subscription_tier,
            )

    if not resolved_key:
        raise ValueError(
            "api_key or player_token required. Set COSMERGON_API_KEY or "
            "COSMERGON_PLAYER_TOKEN env var, or pass as parameter."
        )

    if player_token and api_key:
        _logger.debug("Both api_key and player_token given — using api_key")

    client = _get_client(resolved_key, base_url)
    agent_id = resolved_agent_id or _resolve_agent_id(client)

    return [
        _make_observe_tool(tool, client, agent_id),
        _make_act_tool(tool, client, agent_id),
        _make_benchmark_tool(tool, client, agent_id),
        _make_info_tool(tool, client),
    ]

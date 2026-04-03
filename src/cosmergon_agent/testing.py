"""Test utilities for cosmergon-agent SDK users.

Provides pre-built mock helpers and factory functions so agent developers
can test their agents without a running Cosmergon backend.

Usage::

    from cosmergon_agent.testing import fake_state, FakeTransport

    state = fake_state(energy=5000.0, fields=[{"id": "f1", "cube_id": "c1",
                                                "z_position": 0, "active_cell_count": 42}])
    assert state.energy == 5000.0

    # Use FakeTransport for full agent testing:
    agent = CosmergonAgent(api_key="test-key", base_url="http://test")
    agent._client = httpx.AsyncClient(transport=FakeTransport())
"""

from __future__ import annotations

from typing import Any

import httpx

from cosmergon_agent.state import GameState


def fake_state(**overrides: Any) -> GameState:
    """Create a GameState for testing with sensible defaults.

    Args:
        **overrides: Any GameState.from_api() key to override.

    Returns:
        A frozen GameState dataclass with the given overrides applied.
    """
    defaults: dict[str, Any] = {
        "agent_id": "test-agent-001",
        "agent_type": "independent_agent",
        "energy_balance": 1000.0,
        "fields": [],
        "cubes": [],
        "universe_cubes": [],
        "ranking": {"player_tier": 0, "tier_name": "Novice", "player_score": 0.0},
        "focus": {"focus_energy": 50.0, "focus_regen_rate": 1.0, "can_query_llm": True},
        "tick": 1,
    }
    defaults.update(overrides)
    return GameState.from_api(defaults)


class FakeTransport(httpx.AsyncBaseTransport):
    """Mock transport for testing agents without a real server.

    Returns configurable responses for known paths.
    Unknown paths return 404.

    Usage::

        transport = FakeTransport()
        transport.add_response("GET", "/api/v1/agents/", json=[{"id": "a1"}])
        transport.add_response("GET", "/api/v1/agents/a1/state", json={...})
        transport.add_response("POST", "/api/v1/agents/a1/action", json={...})

        client = httpx.AsyncClient(transport=transport)
    """

    def __init__(self) -> None:
        self._responses: dict[tuple[str, str], tuple[int, dict]] = {}
        # Default: state endpoint returns minimal valid state
        self.add_response("GET", "/api/v1/agents/", json=[{"id": "test-agent-001"}])
        self.add_response(
            "GET",
            "/api/v1/agents/test-agent-001/state",
            json={
                "agent_id": "test-agent-001",
                "agent_type": "independent_agent",
                "energy_balance": 1000.0,
                "tick": 1,
            },
        )
        self.add_response(
            "POST",
            "/api/v1/agents/test-agent-001/action",
            json={
                "field_id": "f-test",
                "active_cells": 0,
            },
        )

    def add_response(
        self,
        method: str,
        path: str,
        json: dict | list | None = None,
        status_code: int = 200,
    ) -> None:
        """Register a mock response for a method + path combination."""
        self._responses[(method.upper(), path)] = (status_code, json or {})

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Return mock response or 404."""
        key = (request.method, request.url.path)
        if key in self._responses:
            status, body = self._responses[key]
            return httpx.Response(status_code=status, json=body)
        return httpx.Response(status_code=404, json={"error": "not found"})

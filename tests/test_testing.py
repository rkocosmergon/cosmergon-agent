"""Tests for the testing utilities module."""

from cosmergon_agent.testing import fake_state, FakeTransport
from cosmergon_agent.state import GameState


def test_fake_state_defaults() -> None:
    """fake_state() returns a valid GameState with defaults."""
    state = fake_state()
    assert isinstance(state, GameState)
    assert state.agent_id == "test-agent-001"
    assert state.energy == 1000.0
    assert state.tick == 1


def test_fake_state_overrides() -> None:
    """fake_state() accepts arbitrary overrides."""
    state = fake_state(energy_balance=5000.0, tick=42)
    assert state.energy == 5000.0
    assert state.tick == 42


def test_fake_state_with_fields() -> None:
    """fake_state() can include fields."""
    state = fake_state(fields=[
        {"id": "f1", "cube_id": "c1", "z_position": 0, "active_cell_count": 10},
    ])
    assert len(state.fields) == 1
    assert state.fields[0].active_cell_count == 10


async def test_fake_transport_default_state() -> None:
    """FakeTransport returns default state for agent endpoint."""
    import httpx
    transport = FakeTransport()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/agents/test-agent-001/state")
        assert resp.status_code == 200
        assert resp.json()["agent_id"] == "test-agent-001"


async def test_fake_transport_custom_response() -> None:
    """FakeTransport accepts custom responses."""
    import httpx
    transport = FakeTransport()
    transport.add_response("GET", "/custom/path", json={"hello": "world"}, status_code=200)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/custom/path")
        assert resp.json()["hello"] == "world"


async def test_fake_transport_404_on_unknown() -> None:
    """FakeTransport returns 404 for unregistered paths."""
    import httpx
    transport = FakeTransport()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/unknown/path")
        assert resp.status_code == 404

"""Tests for GameState parsing."""

from cosmergon_agent.state import GameState, Field, Cube, Ranking, Focus


def test_from_api_minimal() -> None:
    """Minimal API response produces valid GameState."""
    data = {"agent_id": "abc-123", "agent_type": "independent_agent", "energy_balance": 500.0}
    state = GameState.from_api(data)
    assert state.agent_id == "abc-123"
    assert state.energy == 500.0
    assert state.fields == []
    assert state.cubes == []


def test_from_api_with_fields() -> None:
    """Fields are parsed into Field dataclasses."""
    data = {
        "agent_id": "abc",
        "energy_balance": 100.0,
        "fields": [
            {"id": "f1", "cube_id": "c1", "z_position": 0, "active_cell_count": 42},
        ],
    }
    state = GameState.from_api(data)
    assert len(state.fields) == 1
    assert state.fields[0].active_cell_count == 42


def test_action_result_success() -> None:
    """Successful action returns success=True."""
    from cosmergon_agent.action import ActionResult
    result = ActionResult.from_response("place_cells", 200, {"field_id": "f1"})
    assert result.success is True
    assert result.action == "place_cells"


def test_action_result_error() -> None:
    """Failed action returns error details."""
    from cosmergon_agent.action import ActionResult
    body = {"error": {"code": 400, "message": "insufficient_energy", "type": "http_error"}}
    result = ActionResult.from_response("evolve", 400, body)
    assert result.success is False
    assert result.error_message == "insufficient_energy"

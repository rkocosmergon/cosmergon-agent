"""Tests for GameState parsing and defensive construction."""

from cosmergon_agent.state import Field, GameState, _safe_construct


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


def test_from_api_ignores_unknown_fields() -> None:
    """Unknown fields in API response are silently dropped (C3)."""
    data = {
        "agent_id": "abc",
        "energy_balance": 100.0,
        "fields": [
            {"id": "f1", "cube_id": "c1", "z_position": 0,
             "active_cell_count": 10, "unknown_future_field": "ignored"},
        ],
        "some_new_top_level_key": "also_ignored",
    }
    state = GameState.from_api(data)
    assert len(state.fields) == 1
    assert state.fields[0].id == "f1"


def test_from_api_defaults_for_missing_fields() -> None:
    """Missing optional fields get dataclass defaults."""
    data = {"agent_id": "abc"}
    state = GameState.from_api(data)
    assert state.energy == 0.0
    assert state.agent_type == "independent_agent"
    assert state.ranking.player_tier == 0
    assert state.focus.focus_energy == 0.0


def test_safe_construct_filters_unknown_keys() -> None:
    """_safe_construct drops keys not in the dataclass."""
    field = _safe_construct(Field, {
        "id": "f1", "cube_id": "c1", "z_position": 0,
        "active_cell_count": 5, "nonexistent": True,
    })
    assert field.id == "f1"
    assert not hasattr(field, "nonexistent")


def test_from_api_energy_fallback() -> None:
    """Energy can come from 'energy_balance' or 'energy' key."""
    data1 = {"agent_id": "a", "energy_balance": 42.0}
    data2 = {"agent_id": "a", "energy": 99.0}
    assert GameState.from_api(data1).energy == 42.0
    assert GameState.from_api(data2).energy == 99.0

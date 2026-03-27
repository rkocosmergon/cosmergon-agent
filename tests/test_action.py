"""Tests for ActionResult parsing."""

from cosmergon_agent.action import ActionResult


def test_success_result() -> None:
    """200 response creates success=True result."""
    result = ActionResult.from_response("place_cells", 200, {"field_id": "f1"})
    assert result.success is True
    assert result.action == "place_cells"
    assert result.data["field_id"] == "f1"


def test_error_result_structured() -> None:
    """Error response with structured error body is parsed correctly."""
    body = {"error": {"code": 400, "message": "insufficient_energy", "type": "http_error"}}
    result = ActionResult.from_response("evolve", 400, body)
    assert result.success is False
    assert result.error_code == 400
    assert result.error_message == "insufficient_energy"


def test_error_result_string_detail() -> None:
    """Error response with plain string detail is handled."""
    body = {"detail": "Not found"}
    result = ActionResult.from_response("create_field", 404, body)
    assert result.success is False
    assert result.error_message == "Not found"


def test_idempotency_key_passed_through() -> None:
    """Idempotency key is stored in ActionResult for tracing."""
    result = ActionResult.from_response(
        "transfer_energy", 200, {"amount": 100},
        idempotency_key="idem-123",
    )
    assert result.idempotency_key == "idem-123"


def test_idempotency_key_default_none() -> None:
    """Idempotency key defaults to None."""
    result = ActionResult.from_response("place_cells", 200, {})
    assert result.idempotency_key is None

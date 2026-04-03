"""Tests for CosmergonAgent.events() — SSE sync generator.

Uses mock httpx.Client to simulate SSE streams without a real server.

Spec: docs/konzepte/konzept-sdk-webhook-sse-api-2026-04-03.md §8
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, call, patch

import httpx
import pytest

from cosmergon_agent.agent import CosmergonAgent
from cosmergon_agent.exceptions import AuthenticationError
from cosmergon_agent.exceptions import ConnectionError as CsgConnectionError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent() -> CosmergonAgent:
    """Return an agent with a known api_key and pre-set agent_id (no auto-register)."""
    agent = CosmergonAgent(api_key="csg_testkey", base_url="http://localhost:1")
    agent.agent_id = "test-agent-uuid"
    return agent


def _make_stream_mock(lines: list[str], status_code: int = 200) -> MagicMock:
    """Build a mock that behaves like an httpx streaming response context manager."""
    response = MagicMock()
    response.status_code = status_code
    response.iter_lines.return_value = iter(lines)
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


def _make_client_mock(stream_mock: MagicMock) -> MagicMock:
    """Build a mock httpx.Client that returns stream_mock from stream()."""
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.stream.return_value = stream_mock
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_events_yields_dict() -> None:
    """Mock SSE stream with one data line → event dict is yielded."""
    agent = _make_agent()
    payload = json.dumps({"event_type": "catastrophe.warning", "player_id": "p1"})
    stream_mock = _make_stream_mock([f"data: {payload}"])
    client_mock = _make_client_mock(stream_mock)

    with patch("cosmergon_agent.agent.httpx.Client", return_value=client_mock), \
         patch("cosmergon_agent.agent.time.sleep"):
        gen = agent.events(reconnect=False)
        event = next(gen)

    assert event["event_type"] == "catastrophe.warning"
    assert event["player_id"] == "p1"


def test_events_skips_heartbeat() -> None:
    """SSE heartbeat comment lines are dropped; only data events are yielded."""
    agent = _make_agent()
    payload = json.dumps({"event_type": "agent.tick", "player_id": "p1"})
    stream_mock = _make_stream_mock([": heartbeat", f"data: {payload}"])
    client_mock = _make_client_mock(stream_mock)

    with patch("cosmergon_agent.agent.httpx.Client", return_value=client_mock), \
         patch("cosmergon_agent.agent.time.sleep"):
        gen = agent.events(reconnect=False)
        event = next(gen)

    assert event["event_type"] == "agent.tick"


def test_events_tracks_last_event_id() -> None:
    """id: lines set last_event_id; on reconnect, Last-Event-ID header is sent."""
    agent = _make_agent()
    payload = json.dumps({"event_type": "energy.critical", "player_id": "p1"})

    # First stream: id line followed by data, then ends cleanly
    first_stream = _make_stream_mock(["id: evt-42", f"data: {payload}"])
    first_client = _make_client_mock(first_stream)

    # Second stream: one more event (to confirm reconnect happened)
    second_payload = json.dumps({"event_type": "agent.tick", "player_id": "p1"})
    second_stream = _make_stream_mock([f"data: {second_payload}"])
    second_client = _make_client_mock(second_stream)

    with patch("cosmergon_agent.agent.httpx.Client") as MockClient, \
         patch("cosmergon_agent.agent.time.sleep"):
        MockClient.side_effect = [first_client, second_client]
        gen = agent.events(reconnect=True)

        first_event = next(gen)   # consumes first stream
        second_event = next(gen)  # triggers reconnect + consumes second stream

    assert first_event["event_type"] == "energy.critical"
    assert second_event["event_type"] == "agent.tick"

    # Second stream() call must carry Last-Event-ID
    _, second_call_kwargs = second_client.stream.call_args
    assert second_call_kwargs["headers"].get("Last-Event-ID") == "evt-42"


def test_events_reconnects_on_error() -> None:
    """httpx.TransportError triggers sleep + retry; next stream yields event."""
    agent = _make_agent()

    # First client: stream() raises TransportError immediately
    first_client = MagicMock()
    first_client.__enter__.return_value = first_client
    first_client.__exit__.return_value = False
    first_client.stream.side_effect = httpx.TransportError("connection reset")

    # Second client: succeeds with one event
    payload = json.dumps({"event_type": "agent.attacked", "player_id": "p1"})
    second_stream = _make_stream_mock([f"data: {payload}"])
    second_client = _make_client_mock(second_stream)

    with patch("cosmergon_agent.agent.httpx.Client") as MockClient, \
         patch("cosmergon_agent.agent.time.sleep") as mock_sleep:
        MockClient.side_effect = [first_client, second_client]
        gen = agent.events(reconnect=True, reconnect_delay=5.0)
        event = next(gen)

    assert event["event_type"] == "agent.attacked"
    # sleep called exactly once with initial reconnect_delay
    mock_sleep.assert_called_once_with(5.0)


def test_events_no_reconnect_on_auth_error() -> None:
    """401/403 response raises AuthenticationError without retrying."""
    agent = _make_agent()
    stream_mock = _make_stream_mock([], status_code=401)
    client_mock = _make_client_mock(stream_mock)

    with patch("cosmergon_agent.agent.httpx.Client", return_value=client_mock), \
         patch("cosmergon_agent.agent.time.sleep") as mock_sleep:
        gen = agent.events(reconnect=True)
        with pytest.raises(AuthenticationError):
            next(gen)

    mock_sleep.assert_not_called()


def test_events_raises_without_reconnect() -> None:
    """reconnect=False + TransportError raises CsgConnectionError immediately."""
    agent = _make_agent()
    client_mock = MagicMock()
    client_mock.__enter__.return_value = client_mock
    client_mock.__exit__.return_value = False
    client_mock.stream.side_effect = httpx.TransportError("connect failed")

    with patch("cosmergon_agent.agent.httpx.Client", return_value=client_mock), \
         patch("cosmergon_agent.agent.time.sleep") as mock_sleep:
        gen = agent.events(reconnect=False)
        with pytest.raises(CsgConnectionError):
            next(gen)

    mock_sleep.assert_not_called()

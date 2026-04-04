"""Tests for CosmergonAgent._request() — retry and rate-limit behaviour.

Verifies that:
- 429 responses raise RateLimitError immediately (no sleep, no retry)
- RateLimitError.retry_after is populated from the Retry-After header
- RateLimitError.retry_after falls back to 1.0 when the header is absent
- 5xx responses are retried (existing behaviour, regression guard)
- act() propagates RateLimitError to the caller
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from cosmergon_agent.agent import CosmergonAgent
from cosmergon_agent.exceptions import RateLimitError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent() -> CosmergonAgent:
    """Agent with injected key/id — skips auto-register."""
    agent = CosmergonAgent(api_key="csg_testkey", base_url="http://localhost:1")
    agent.agent_id = "test-agent-uuid"
    return agent


def _mock_response(status: int, headers: dict | None = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.headers = headers or {}
    resp.json.return_value = {}
    resp.text = ""
    return resp


async def _inject_client(agent: CosmergonAgent, response: MagicMock) -> None:
    """Put a mock httpx.AsyncClient onto the agent."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.request = AsyncMock(return_value=response)
    agent._client = mock_client


# ---------------------------------------------------------------------------
# _request(): 429 behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_429_raises_rate_limit_error() -> None:
    """429 response must raise RateLimitError immediately."""
    agent = _make_agent()
    resp = _mock_response(429, {"Retry-After": "45"})
    await _inject_client(agent, resp)

    with pytest.raises(RateLimitError):
        await agent._request("GET", "/api/v1/test")


@pytest.mark.asyncio
async def test_request_429_retry_after_from_header() -> None:
    """RateLimitError.retry_after must equal the Retry-After header value."""
    agent = _make_agent()
    resp = _mock_response(429, {"Retry-After": "15"})
    await _inject_client(agent, resp)

    with pytest.raises(RateLimitError) as exc_info:
        await agent._request("GET", "/api/v1/test")

    assert exc_info.value.retry_after == pytest.approx(15.0)


@pytest.mark.asyncio
async def test_request_429_retry_after_default_when_header_absent() -> None:
    """RateLimitError.retry_after falls back to 1.0 when Retry-After is missing."""
    agent = _make_agent()
    resp = _mock_response(429, {})
    await _inject_client(agent, resp)

    with pytest.raises(RateLimitError) as exc_info:
        await agent._request("GET", "/api/v1/test")

    assert exc_info.value.retry_after == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_request_429_no_retry_attempted() -> None:
    """429 must not trigger any retries — exactly one HTTP call is made."""
    agent = _make_agent()
    resp = _mock_response(429, {"Retry-After": "5"})
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.request = AsyncMock(return_value=resp)
    agent._client = mock_client

    with pytest.raises(RateLimitError):
        await agent._request("GET", "/api/v1/test")

    assert mock_client.request.call_count == 1


@pytest.mark.asyncio
async def test_request_429_capped_at_max_backoff() -> None:
    """retry_after is capped at _MAX_BACKOFF (30s) even if Retry-After is huge."""
    agent = _make_agent()
    resp = _mock_response(429, {"Retry-After": "9999"})
    await _inject_client(agent, resp)

    with pytest.raises(RateLimitError) as exc_info:
        await agent._request("GET", "/api/v1/test")

    assert exc_info.value.retry_after <= 30.0


# ---------------------------------------------------------------------------
# _request(): 5xx still retries (regression guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_500_retries_then_returns() -> None:
    """500 on first attempt, 200 on second — must return the 200 response."""
    agent = _make_agent()
    agent.max_retries = 2
    fail = _mock_response(500)
    ok = _mock_response(200)

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.request = AsyncMock(side_effect=[fail, ok])
    agent._client = mock_client

    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await agent._request("GET", "/api/v1/test")

    assert result.status_code == 200
    assert mock_client.request.call_count == 2


# ---------------------------------------------------------------------------
# act(): RateLimitError propagates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_act_propagates_rate_limit_error() -> None:
    """agent.act() must propagate RateLimitError from _request()."""
    agent = _make_agent()
    resp = _mock_response(429, {"Retry-After": "30"})
    await _inject_client(agent, resp)

    with pytest.raises(RateLimitError) as exc_info:
        await agent.act("place_cells", field_id="f1", preset="blinker")

    assert exc_info.value.retry_after == pytest.approx(30.0)

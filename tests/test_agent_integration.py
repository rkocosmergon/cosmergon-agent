"""Integration tests for CosmergonAgent using FakeTransport.

Tests the agent's core loop, retry logic, and error handling
without a real server.
"""


import httpx
import pytest

from cosmergon_agent.agent import CosmergonAgent
from cosmergon_agent.testing import FakeTransport


class TestAgentWithFakeTransport:
    async def test_resolves_agent_id(self) -> None:
        """Agent resolves its ID from the API on connect."""
        transport = FakeTransport()
        agent = CosmergonAgent(api_key="csg_test123", base_url="http://test")
        agent._client = httpx.AsyncClient(transport=transport, base_url="http://test")
        await agent._resolve_agent_id()
        assert agent.agent_id == "test-agent-001"
        await agent.close()

    async def test_act_returns_success(self) -> None:
        """Agent action via FakeTransport returns success."""
        transport = FakeTransport()
        agent = CosmergonAgent(api_key="csg_test123", base_url="http://test")
        agent._client = httpx.AsyncClient(transport=transport, base_url="http://test")
        agent.agent_id = "test-agent-001"

        result = await agent.act("place_cells", field_id="f1")
        assert result.success is True
        assert result.action == "place_cells"
        assert result.idempotency_key is not None
        await agent.close()

    async def test_act_without_connection_raises(self) -> None:
        """Calling act() before run() raises RuntimeError."""
        agent = CosmergonAgent(api_key="csg_test123", base_url="http://test")
        with pytest.raises(RuntimeError, match="not connected"):
            await agent.act("place_cells")

    async def test_state_is_none_before_tick(self) -> None:
        """State is None before the first tick."""
        agent = CosmergonAgent(api_key="csg_test123", base_url="http://test")
        assert agent.state is None

    async def test_memory_persists_across_calls(self) -> None:
        """Memory dict persists between method calls."""
        agent = CosmergonAgent(api_key="csg_test123", base_url="http://test")
        agent.memory["key1"] = "value1"
        assert agent.memory["key1"] == "value1"

    async def test_context_manager(self) -> None:
        """Agent works as async context manager."""
        transport = FakeTransport()
        agent = CosmergonAgent(api_key="csg_test123", base_url="http://test")
        # Override _create_client to use FakeTransport
        agent._create_client = lambda: httpx.AsyncClient(
            transport=transport, base_url="http://test",
            headers={"Authorization": "api-key test"},
            timeout=30.0,
        )
        async with agent:
            assert agent.agent_id == "test-agent-001"
            assert agent._client is not None
        assert agent._client is None

    async def test_on_error_handler_called(self) -> None:
        """Error handler is called when action fails."""
        transport = FakeTransport()
        transport.add_response("POST", "/api/v1/agents/test-agent-001/action",
                               json={"error": {"code": 400, "message": "bad"}}, status_code=400)

        agent = CosmergonAgent(api_key="csg_test123", base_url="http://test")
        agent._client = httpx.AsyncClient(transport=transport, base_url="http://test")
        agent.agent_id = "test-agent-001"

        errors = []

        @agent.on_error
        async def handle(result):
            errors.append(result)

        await agent.act("bad_action")
        assert len(errors) == 1
        assert errors[0].success is False
        await agent.close()


class TestRetryLogic:
    async def test_retries_on_server_error(self) -> None:
        """Agent retries on 500 with exponential backoff."""
        call_count = 0

        class CountingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                nonlocal call_count
                call_count += 1
                if call_count < 3:
                    return httpx.Response(500, json={"error": "server error"})
                return httpx.Response(200, json={"ok": True})

        agent = CosmergonAgent(api_key="csg_test", base_url="http://test", max_retries=3)
        agent._client = httpx.AsyncClient(transport=CountingTransport(), base_url="http://test")
        agent.agent_id = "test"

        resp = await agent._request("GET", "/test")
        assert resp.status_code == 200
        assert call_count == 3  # 2 failures + 1 success
        await agent.close()

    async def test_returns_last_error_after_max_retries(self) -> None:
        """Agent returns the last 500 response after exhausting retries."""
        class AlwaysFailTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                return httpx.Response(500, json={"error": "down"})

        agent = CosmergonAgent(api_key="csg_test", base_url="http://test", max_retries=1)
        agent._client = httpx.AsyncClient(transport=AlwaysFailTransport(), base_url="http://test")

        resp = await agent._request("GET", "/test")
        assert resp.status_code == 500  # last failed response returned
        await agent.close()


class TestCreateClient:
    def test_creates_client_with_headers(self) -> None:
        """_create_client sets auth and user-agent headers."""
        agent = CosmergonAgent(api_key="csg_testkey123", base_url="http://test")
        client = agent._create_client()
        headers = dict(client.headers)
        assert "cosmergon-agent-python" in headers.get("user-agent", "")
        assert "api-key" in headers.get("authorization", "")

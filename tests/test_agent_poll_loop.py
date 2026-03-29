"""Tests for CosmergonAgent poll_loop, start/connect lifecycle, and retry edge cases."""

import asyncio

import httpx
import pytest

from cosmergon_agent.agent import CosmergonAgent
from cosmergon_agent.exceptions import AuthenticationError
from cosmergon_agent.exceptions import ConnectionError as CsgConnectionError
from cosmergon_agent.testing import FakeTransport


class TestPollLoop:
    async def test_poll_loop_calls_tick_handler(self) -> None:
        """poll_loop fetches state and invokes the on_tick handler."""
        transport = FakeTransport()
        agent = CosmergonAgent(api_key="csg_test123", base_url="http://test")
        agent._client = httpx.AsyncClient(transport=transport, base_url="http://test")
        agent.agent_id = "test-agent-001"
        agent._running = True

        ticks_received: list[int] = []

        @agent.on_tick
        async def handler(state):
            ticks_received.append(state.tick)
            agent._running = False  # stop after first tick

        agent.poll_interval = 0.01
        await agent._poll_loop()

        assert len(ticks_received) == 1
        assert ticks_received[0] == 1

    async def test_poll_loop_skips_duplicate_tick(self) -> None:
        """poll_loop does not call handler if tick hasn't changed."""
        transport = FakeTransport()
        agent = CosmergonAgent(api_key="csg_test123", base_url="http://test")
        agent._client = httpx.AsyncClient(transport=transport, base_url="http://test")
        agent.agent_id = "test-agent-001"
        agent._running = True

        call_count = 0
        loop_count = 0

        @agent.on_tick
        async def handler(state):
            nonlocal call_count
            call_count += 1

        # Override poll_loop to stop after 3 iterations
        original_sleep = asyncio.sleep

        async def limited_sleep(duration):
            nonlocal loop_count
            loop_count += 1
            if loop_count >= 3:
                agent._running = False
            await original_sleep(0.001)

        asyncio.sleep = limited_sleep  # type: ignore[assignment]
        try:
            agent.poll_interval = 0.001
            await agent._poll_loop()
        finally:
            asyncio.sleep = original_sleep  # type: ignore[assignment]

        # Handler called only once because tick stays at 1
        assert call_count == 1

    async def test_poll_loop_handles_non_200_state(self) -> None:
        """poll_loop continues when state endpoint returns non-200."""
        transport = FakeTransport()
        transport.add_response(
            "GET", "/api/v1/agents/test-agent-001/state",
            json={"error": "unavailable"}, status_code=503,
        )
        agent = CosmergonAgent(api_key="csg_test123", base_url="http://test")
        agent._client = httpx.AsyncClient(transport=transport, base_url="http://test")
        agent.agent_id = "test-agent-001"
        agent._running = True

        loop_count = 0

        async def limited_sleep(duration):
            nonlocal loop_count
            loop_count += 1
            if loop_count >= 1:
                agent._running = False

        original_sleep = asyncio.sleep
        asyncio.sleep = limited_sleep  # type: ignore[assignment]
        try:
            agent.poll_interval = 0.001
            await agent._poll_loop()
        finally:
            asyncio.sleep = original_sleep  # type: ignore[assignment]

        # Should have looped at least once without crashing
        assert agent._state is None

    async def test_poll_loop_handles_connection_error(self) -> None:
        """poll_loop catches CsgConnectionError and continues."""

        class FailingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                raise httpx.ConnectError("Connection refused")

        agent = CosmergonAgent(
            api_key="csg_test123", base_url="http://test", max_retries=0,
        )
        agent._client = httpx.AsyncClient(
            transport=FailingTransport(), base_url="http://test",
        )
        agent.agent_id = "test-agent-001"
        agent._running = True

        loop_count = 0

        async def limited_sleep(duration):
            nonlocal loop_count
            loop_count += 1
            if loop_count >= 1:
                agent._running = False

        original_sleep = asyncio.sleep
        asyncio.sleep = limited_sleep  # type: ignore[assignment]
        try:
            agent.poll_interval = 0.001
            await agent._poll_loop()
        finally:
            asyncio.sleep = original_sleep  # type: ignore[assignment]

        # Should have survived the error
        assert loop_count >= 1

    async def test_poll_loop_handles_unexpected_exception(self) -> None:
        """poll_loop catches unexpected exceptions and continues."""

        class ExplodingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                raise RuntimeError("Unexpected boom")

        agent = CosmergonAgent(
            api_key="csg_test123", base_url="http://test", max_retries=0,
        )
        agent._client = httpx.AsyncClient(
            transport=ExplodingTransport(), base_url="http://test",
        )
        agent.agent_id = "test-agent-001"
        agent._running = True

        loop_count = 0

        async def limited_sleep(duration):
            nonlocal loop_count
            loop_count += 1
            if loop_count >= 1:
                agent._running = False

        original_sleep = asyncio.sleep
        asyncio.sleep = limited_sleep  # type: ignore[assignment]
        try:
            agent.poll_interval = 0.001
            await agent._poll_loop()
        finally:
            asyncio.sleep = original_sleep  # type: ignore[assignment]

        assert loop_count >= 1

    async def test_poll_loop_not_connected_raises(self) -> None:
        """poll_loop raises RuntimeError if client is None."""
        agent = CosmergonAgent(api_key="csg_test123", base_url="http://test")
        agent._running = True
        with pytest.raises(RuntimeError, match="not connected"):
            await agent._poll_loop()


class TestStartLifecycle:
    async def test_start_calls_connect_handler(self) -> None:
        """start() invokes on_connect handler after resolving ID."""
        transport = FakeTransport()
        agent = CosmergonAgent(api_key="csg_test123", base_url="http://test")
        agent._create_client = lambda: httpx.AsyncClient(
            transport=transport, base_url="http://test",
            headers={"Authorization": "api-key test"},
            timeout=30.0,
        )

        connected = False

        @agent.on_connect
        async def on_connect():
            nonlocal connected
            connected = True

        # Stop immediately after connect by not registering a tick handler
        # and setting _running to False in connect
        @agent.on_connect
        async def stop_on_connect():
            nonlocal connected
            connected = True
            agent._running = False

        agent.poll_interval = 0.001
        await agent.start()

        assert connected
        assert agent.agent_id == "test-agent-001"
        assert agent._client is None  # closed in finally

    async def test_start_resolves_agent_id(self) -> None:
        """start() resolves agent_id from API if not set."""
        transport = FakeTransport()
        agent = CosmergonAgent(api_key="csg_test123", base_url="http://test")
        agent._create_client = lambda: httpx.AsyncClient(
            transport=transport, base_url="http://test",
            headers={"Authorization": "api-key test"},
            timeout=30.0,
        )

        @agent.on_connect
        async def stop():
            agent._running = False

        agent.poll_interval = 0.001
        await agent.start()

        assert agent.agent_id == "test-agent-001"

    async def test_start_with_preset_agent_id(self) -> None:
        """start() skips _resolve_agent_id when agent_id is already set."""
        transport = FakeTransport()
        # Add state for pre-set agent ID
        transport.add_response("GET", "/api/v1/agents/my-custom-id/state", json={
            "agent_id": "my-custom-id",
            "agent_type": "independent_agent",
            "energy_balance": 500.0,
            "tick": 5,
        })

        agent = CosmergonAgent(
            api_key="csg_test123", base_url="http://test", agent_id="my-custom-id",
        )
        agent._create_client = lambda: httpx.AsyncClient(
            transport=transport, base_url="http://test",
            headers={"Authorization": "api-key test"},
            timeout=30.0,
        )

        @agent.on_connect
        async def stop():
            agent._running = False

        agent.poll_interval = 0.001
        await agent.start()

        assert agent.agent_id == "my-custom-id"

    def test_run_calls_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """run() calls asyncio.run(start())."""
        started = False

        async def fake_start(self_agent):
            nonlocal started
            started = True

        agent = CosmergonAgent(api_key="csg_test123", base_url="http://test")
        monkeypatch.setattr(CosmergonAgent, "start", fake_start)
        agent.run()
        assert started


class TestResolveAgentId:
    async def test_resolve_raises_on_empty_list(self) -> None:
        """_resolve_agent_id raises AuthenticationError on empty agent list."""
        transport = FakeTransport()
        transport.add_response("GET", "/api/v1/agents/", json=[], status_code=200)

        agent = CosmergonAgent(api_key="csg_test123", base_url="http://test")
        agent._client = httpx.AsyncClient(transport=transport, base_url="http://test")

        with pytest.raises(AuthenticationError, match="Could not resolve"):
            await agent._resolve_agent_id()
        await agent.close()

    async def test_resolve_raises_on_non_200(self) -> None:
        """_resolve_agent_id raises AuthenticationError on 401."""
        transport = FakeTransport()
        transport.add_response(
            "GET", "/api/v1/agents/",
            json={"error": "unauthorized"}, status_code=401,
        )

        agent = CosmergonAgent(api_key="csg_bad_key", base_url="http://test")
        agent._client = httpx.AsyncClient(transport=transport, base_url="http://test")

        with pytest.raises(AuthenticationError, match="Could not resolve"):
            await agent._resolve_agent_id()
        await agent.close()

    async def test_resolve_not_connected_raises(self) -> None:
        """_resolve_agent_id raises RuntimeError if client is None."""
        agent = CosmergonAgent(api_key="csg_test123", base_url="http://test")
        with pytest.raises(RuntimeError, match="not connected"):
            await agent._resolve_agent_id()


class TestRetryEdgeCases:
    async def test_retries_on_429_with_retry_after(self) -> None:
        """Agent respects Retry-After header on 429."""
        call_count = 0

        class RateLimitTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return httpx.Response(
                        429,
                        json={"error": "rate limited"},
                        headers={"Retry-After": "0.01"},
                    )
                return httpx.Response(200, json={"ok": True})

        agent = CosmergonAgent(api_key="csg_test", base_url="http://test", max_retries=2)
        agent._client = httpx.AsyncClient(
            transport=RateLimitTransport(), base_url="http://test",
        )

        resp = await agent._request("GET", "/test")
        assert resp.status_code == 200
        assert call_count == 2
        await agent.close()

    async def test_transport_error_raises_connection_error(self) -> None:
        """Transport errors raise CsgConnectionError after retries exhausted."""

        class AlwaysErrorTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                raise httpx.ConnectError("Connection refused")

        agent = CosmergonAgent(api_key="csg_test", base_url="http://test", max_retries=1)
        agent._client = httpx.AsyncClient(
            transport=AlwaysErrorTransport(), base_url="http://test",
        )

        with pytest.raises(CsgConnectionError, match="Failed after 2 attempts"):
            await agent._request("GET", "/test")
        await agent.close()

    async def test_request_not_connected_raises(self) -> None:
        """_request raises RuntimeError when client is None."""
        agent = CosmergonAgent(api_key="csg_test", base_url="http://test")
        with pytest.raises(RuntimeError, match="not connected"):
            await agent._request("GET", "/test")


class TestOnConnectDecorator:
    def test_on_connect_registers_handler(self) -> None:
        """on_connect decorator stores the handler."""
        agent = CosmergonAgent(api_key="csg_test", base_url="http://test")

        @agent.on_connect
        async def my_handler():
            pass

        assert agent._connect_handler is my_handler

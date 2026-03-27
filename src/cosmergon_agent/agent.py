"""Main agent class — connects to Cosmergon, observes state, executes actions."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from cosmergon_agent.action import ActionResult
from cosmergon_agent.state import GameState

logger = logging.getLogger(__name__)


class CosmergonAgent:
    """Client for the Cosmergon Agent Economy.

    Usage::

        agent = CosmergonAgent(api_key="csg_...", base_url="http://...")

        @agent.on_tick
        async def play(state: GameState):
            if state.energy > 1000 and not state.fields:
                await agent.act("create_field", cube_id=state.universe_cubes[0].id)

        agent.run()
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        agent_id: str | None = None,
        poll_interval: float = 10.0,
        auto_reconnect: bool = True,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self.poll_interval = poll_interval
        self.auto_reconnect = auto_reconnect

        self._tick_handler: Callable[[GameState], Awaitable[None]] | None = None
        self._error_handler: Callable[[ActionResult], Awaitable[None]] | None = None
        self._connect_handler: Callable[[], Awaitable[None]] | None = None
        self._event_handlers: dict[str, Callable] = {}
        self._client: httpx.AsyncClient | None = None
        self._running = False
        self._state: GameState | None = None
        self._memory: dict[str, Any] = {}

    # --- Decorators (Discord.py pattern) ---

    def on_tick(self, func: Callable[[GameState], Awaitable[None]]) -> Callable:
        """Register a handler called every game tick with fresh state."""
        self._tick_handler = func
        return func

    def on_error(self, func: Callable[[ActionResult], Awaitable[None]]) -> Callable:
        """Register a handler called when an action fails."""
        self._error_handler = func
        return func

    def on_connect(self, func: Callable[[], Awaitable[None]]) -> Callable:
        """Register a handler called on initial connection."""
        self._connect_handler = func
        return func

    def on_event(self, event_type: str) -> Callable:
        """Register a handler for a specific event type."""
        def decorator(func: Callable) -> Callable:
            self._event_handlers[event_type] = func
            return func
        return decorator

    # --- Properties ---

    @property
    def state(self) -> GameState | None:
        """Current game state (refreshed each tick)."""
        return self._state

    @property
    def memory(self) -> dict[str, Any]:
        """Persistent key-value store across ticks."""
        return self._memory

    # --- Actions (Screeps pattern) ---

    async def act(self, action: str, **params: Any) -> ActionResult:
        """Execute a game action. Returns ActionResult with success/failure."""
        if not self._client:
            raise RuntimeError("Agent not connected. Call run() first.")

        headers = {"X-Idempotency-Key": str(uuid.uuid4())}
        body = {"action": action, **params}

        resp = await self._client.post(
            f"{self.base_url}/api/v1/agents/{self.agent_id}/action",
            json=body,
            headers=headers,
        )

        result = ActionResult.from_response(action, resp.status_code, resp.json())

        if not result.success and self._error_handler:
            await self._error_handler(result)

        return result

    # --- Lifecycle ---

    def run(self) -> None:
        """Start the agent (blocking). Like discord.py client.run()."""
        asyncio.run(self.start())

    async def start(self) -> None:
        """Start the agent (async, non-blocking)."""
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"api-key {self.api_key}"},
            timeout=30.0,
        )
        self._running = True

        try:
            # Resolve agent_id from API key if not provided
            if not self.agent_id:
                await self._resolve_agent_id()

            if self._connect_handler:
                await self._connect_handler()

            logger.info("Connected as agent %s", self.agent_id)
            await self._poll_loop()

        except KeyboardInterrupt:
            logger.info("Agent stopped by user")
        finally:
            self._running = False
            await self.close()

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> CosmergonAgent:
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"api-key {self.api_key}"},
            timeout=30.0,
        )
        if not self.agent_id:
            await self._resolve_agent_id()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    # --- Internal ---

    async def _resolve_agent_id(self) -> None:
        """Get agent_id from the API key's associated agent."""
        assert self._client is not None
        resp = await self._client.get(f"{self.base_url}/api/v1/agents/")
        if resp.status_code == 200:
            agents = resp.json()
            if agents:
                self.agent_id = agents[0]["id"]
                return
        raise RuntimeError("Could not resolve agent_id from API key")

    async def _poll_loop(self) -> None:
        """Main loop: fetch state, call handler, sleep."""
        assert self._client is not None
        last_tick = -1

        while self._running:
            try:
                resp = await self._client.get(
                    f"{self.base_url}/api/v1/agents/{self.agent_id}/state"
                )
                if resp.status_code != 200:
                    logger.warning("State fetch failed: %d", resp.status_code)
                    await asyncio.sleep(self.poll_interval)
                    continue

                self._state = GameState.from_api(resp.json())

                if self._state.tick != last_tick and self._tick_handler:
                    last_tick = self._state.tick
                    await self._tick_handler(self._state)

            except httpx.ConnectError:
                logger.warning("Connection lost, retrying in %ds", self.poll_interval)
            except Exception:
                logger.exception("Error in agent loop")

            await asyncio.sleep(self.poll_interval)

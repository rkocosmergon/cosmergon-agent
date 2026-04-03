"""Main agent class — connects to Cosmergon, observes state, executes actions."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from cosmergon_agent import __version__
from cosmergon_agent.action import ActionResult
from cosmergon_agent.exceptions import (
    AuthenticationError,
    CosmergonError,
)
from cosmergon_agent.exceptions import (
    ConnectionError as CsgConnectionError,
)
from cosmergon_agent.state import GameState

logger = logging.getLogger(__name__)

_DEFAULT_MAX_RETRIES = 3
_INITIAL_BACKOFF = 0.5
_MAX_BACKOFF = 30.0


class _SensitiveStr(str):
    """String that masks its value in repr/str to prevent accidental logging."""

    def __repr__(self) -> str:
        if len(self) <= 8:
            return "'***'"
        return f"'{self[:4]}...{self[-4:]}'"

    def __str__(self) -> str:
        return self.__repr__()


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
        api_key: str | None = None,
        base_url: str = "https://cosmergon.com",
        agent_id: str | None = None,
        poll_interval: float = 10.0,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        auto_reconnect: bool = True,
    ) -> None:
        # C1: Resolve API key from env var fallback (M2)
        resolved_key = api_key or os.environ.get("COSMERGON_API_KEY", "")
        if not resolved_key or not resolved_key.strip():
            # Auto-register anonymous agent if no key provided
            resolved_key, auto_agent_id = self._auto_register_anonymous(base_url)
            if not agent_id:
                agent_id = auto_agent_id
        # M1: Input validation
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        insecure = base_url.startswith("http://")
        local = "localhost" in base_url or "127.0.0.1" in base_url
        if insecure and not local:
            logger.warning("Using unencrypted HTTP — API key will be sent in plaintext")

        # C1: Store key as _SensitiveStr to prevent accidental logging
        self._api_key = _SensitiveStr(resolved_key)
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self.poll_interval = poll_interval
        self.max_retries = max_retries
        self.auto_reconnect = auto_reconnect

        self._tick_handler: Callable[[GameState], Awaitable[None]] | None = None
        self._error_handler: Callable[[ActionResult], Awaitable[None]] | None = None
        self._connect_handler: Callable[[], Awaitable[None]] | None = None
        self._event_handlers: dict[str, Callable] = {}
        self._client: httpx.AsyncClient | None = None
        self._running = False
        self._state: GameState | None = None
        self._memory: dict[str, Any] = {}

    def __repr__(self) -> str:
        """Safe repr that never exposes the full API key."""
        return (
            f"CosmergonAgent(api_key={self._api_key!r}, "
            f"base_url={self.base_url!r}, "
            f"agent_id={self.agent_id!r})"
        )

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
        """Execute a game action. Returns ActionResult with success/failure.

        Raises CosmergonError subclasses on server errors (4xx/5xx).
        """
        idem_key = str(uuid.uuid4())
        body = {"action": action, **params}

        resp = await self._request(
            "POST",
            f"/api/v1/agents/{self.agent_id}/action",
            json=body,
            headers={"X-Idempotency-Key": idem_key},
        )

        result = ActionResult.from_response(
            action,
            resp.status_code,
            resp.json(),
            idempotency_key=idem_key,
        )

        if not result.success and self._error_handler:
            await self._error_handler(result)

        return result

    async def set_compass(self, preset: str) -> dict:
        """Set the agent's strategic compass direction.

        Args:
            preset: One of attack, defend, grow, trade, cooperate, explore, autonomous.

        Returns:
            Server response with explanation and agent opinion.
        """
        resp = await self._request(
            "POST",
            f"/api/v1/agents/{self.agent_id}/compass",
            json={"preset": preset},
        )
        if resp.status_code >= 400:
            return {"error": resp.text}
        return resp.json()

    async def get_last_decision(self) -> dict | None:
        """Fetch the most recent LLM decision for this agent.

        Returns a dict with keys: tick, action, reasoning, outcome, params.
        Returns None if no decisions exist or on error.
        """
        try:
            resp = await self._request(
                "GET",
                f"/api/v1/agents/{self.agent_id}/decisions",
                params={"limit": 1},
            )
            if resp.status_code == 200:
                decisions = resp.json()
                return decisions[0] if decisions else None
        except Exception:
            pass
        return None

    # --- Lifecycle ---

    def run(self) -> None:
        """Start the agent (blocking). Like discord.py client.run()."""
        asyncio.run(self.start())

    async def start(self) -> None:
        """Start the agent (async, non-blocking)."""
        self._client = self._create_client()
        self._running = True

        try:
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
        self._client = self._create_client()
        if not self.agent_id:
            await self._resolve_agent_id()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    # --- Auto-registration ---

    @staticmethod
    def _auto_register_anonymous(base_url: str) -> tuple[str, str | None]:
        """Register an anonymous agent. Returns (api_key, agent_id).

        Called automatically when no api_key is provided.
        The agent gets 1000 energy and a 24h session.
        """
        url = f"{base_url.rstrip('/')}/api/v1/auth/register/anonymous-agent"
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json={})
        if resp.status_code != 200:
            is_json = resp.headers.get(
                "content-type",
                "",
            ).startswith("application/json")
            detail = resp.json().get("detail", resp.text) if is_json else resp.text
            raise CosmergonError(f"Auto-registration failed ({resp.status_code}): {detail}")
        data = resp.json()
        key = data.get("api_key", "")
        if not key:
            raise CosmergonError("Auto-registration returned no API key")
        agent_id = data.get("agent_id")
        logger.info(
            "Auto-registered anonymous agent: %s (expires %s)",
            (agent_id or "?")[:8],
            data.get("expires_at", "?"),
        )
        return key, agent_id

    # --- Internal ---

    def _create_client(self) -> httpx.AsyncClient:
        """Single point of HTTP client creation (H3: DRY + consistent config)."""
        return httpx.AsyncClient(
            headers={
                "Authorization": f"api-key {str.__str__(self._api_key)}",
                "User-Agent": f"cosmergon-agent-python/{__version__}",
                "X-Cosmergon-SDK-Version": __version__,
            },
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
            verify=True,
            max_redirects=3,
        )

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """HTTP request with retry, backoff, and rate-limit handling (C2)."""
        if self._client is None:
            raise RuntimeError("Agent not connected. Call run() or use async with.")

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._client.request(
                    method,
                    f"{self.base_url}{path}",
                    **kwargs,
                )

                if resp.status_code == 429:
                    retry_after = min(
                        float(resp.headers.get("Retry-After", "1")),
                        _MAX_BACKOFF,
                    )
                    logger.warning("Rate limited, waiting %.1fs", retry_after)
                    await asyncio.sleep(retry_after)
                    continue

                if resp.status_code >= 500 and attempt < self.max_retries:
                    delay = min(_INITIAL_BACKOFF * (2**attempt) + random.random(), _MAX_BACKOFF)
                    logger.warning("Server error %d, retry in %.1fs", resp.status_code, delay)
                    await asyncio.sleep(delay)
                    continue

                return resp

            except httpx.TransportError as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    delay = min(_INITIAL_BACKOFF * (2**attempt) + random.random(), _MAX_BACKOFF)
                    logger.warning("Transport error, retry in %.1fs: %s", delay, exc)
                    await asyncio.sleep(delay)

        raise CsgConnectionError(f"Failed after {self.max_retries + 1} attempts") from last_exc

    async def _resolve_agent_id(self) -> None:
        """Get agent_id from the API key's associated agent."""
        if self._client is None:
            raise RuntimeError("Agent not connected")
        resp = await self._request("GET", "/api/v1/agents/")
        if resp.status_code == 200:
            agents = resp.json()
            if agents:
                self.agent_id = agents[0]["id"]
                return
        raise AuthenticationError("Could not resolve agent_id from API key")

    async def _poll_loop(self) -> None:
        """Main loop: fetch state, call handler, sleep."""
        if self._client is None:
            raise RuntimeError("Agent not connected")
        last_tick = -1

        while self._running:
            try:
                resp = await self._request(
                    "GET",
                    f"/api/v1/agents/{self.agent_id}/state",
                )
                if resp.status_code != 200:
                    logger.warning("State fetch failed: %d", resp.status_code)
                    await asyncio.sleep(self.poll_interval)
                    continue

                self._state = GameState.from_api(resp.json())

                if self._state.tick != last_tick and self._tick_handler:
                    last_tick = self._state.tick
                    await self._tick_handler(self._state)

            except CsgConnectionError:
                logger.warning("Connection lost, retrying in %.0fs", self.poll_interval)
            except CosmergonError as exc:
                logger.error("API error in poll loop: %s", exc.message)
            except Exception:
                logger.exception("Unexpected error in agent loop")

            await asyncio.sleep(self.poll_interval)

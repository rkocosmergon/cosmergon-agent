"""Main agent class — connects to Cosmergon, observes state, executes actions."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import random
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import httpx

from cosmergon_agent import __version__
from cosmergon_agent._token import TokenResolutionError, _SensitiveStr, resolve_token_sync
from cosmergon_agent.action import ActionResult
from cosmergon_agent.config import (
    CONFIG_PATH,
    load_credentials,
    load_token,
    save_all_agents_and_token,
    save_credentials,
)
from cosmergon_agent.exceptions import (
    AuthenticationError,
    CosmergonError,
    RateLimitError,
    WebhookSignatureError,
    WebhookTimestampError,
)
from cosmergon_agent.exceptions import (
    ConnectionError as CsgConnectionError,
)
from cosmergon_agent.state import GameState
from cosmergon_agent.webhook import CosmergonWebhook

logger = logging.getLogger(__name__)

_DEFAULT_MAX_RETRIES = 3
_INITIAL_BACKOFF = 0.5
_MAX_BACKOFF = 30.0


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
        player_token: str | None = None,
        agent_name: str | None = None,
    ) -> None:
        """Connect to Cosmergon.

        Credential priority (first match wins):
          1. ``api_key`` parameter
          2. ``player_token`` parameter → resolved via Master Key API
          3. ``COSMERGON_API_KEY`` env var
          4. ``COSMERGON_PLAYER_TOKEN`` + ``COSMERGON_AGENT_NAME`` env vars
          5. config.toml (saved credentials)
          6. auto-register (new anonymous free agent)

        Args:
            api_key: Agent API key (AGENT-...:secret).
            base_url: Server URL.
            agent_id: Agent UUID (resolved automatically if omitted).
            poll_interval: Seconds between state fetches in run().
            max_retries: HTTP retry count.
            auto_reconnect: Retry on transient errors in run().
            player_token: Master Key (CSMR-...) for multi-agent access.
            agent_name: Select agent by name when using player_token.
                If omitted with multiple agents, the oldest is used.
        """
        # C1: 6-level credential resolution
        # Track whether credentials came from the user (explicit) or auto-managed.
        # Used in _poll_loop: user-provided → error on 401, auto → re-register.
        _user_provided = False
        resolved_key = ""

        # Level 1: api_key parameter
        if api_key:
            resolved_key = api_key
            _user_provided = True

        # Level 2: player_token parameter
        if not resolved_key and player_token:
            resolved_key, agent_id = self._resolve_via_token(
                player_token,
                base_url,
                agent_name,
            )
            _user_provided = True

        # Level 3: COSMERGON_API_KEY env var
        if not resolved_key:
            env_key = os.environ.get("COSMERGON_API_KEY", "").strip()
            if env_key:
                resolved_key = env_key
                _user_provided = True

        # Level 4: COSMERGON_PLAYER_TOKEN + COSMERGON_AGENT_NAME env vars
        if not resolved_key:
            env_token = os.environ.get("COSMERGON_PLAYER_TOKEN", "").strip()
            if env_token:
                env_name = os.environ.get("COSMERGON_AGENT_NAME", "").strip() or None
                resolved_key, agent_id = self._resolve_via_token(
                    env_token,
                    base_url,
                    env_name,
                )
                _user_provided = True

        # Level 5: config.toml
        if not resolved_key:
            saved_key, saved_agent_id, saved_activated = load_credentials()
            if saved_key:
                resolved_key = saved_key
                if not agent_id:
                    agent_id = saved_agent_id
                if saved_activated or load_token():
                    # Activated credentials or token in config → user-provided
                    _user_provided = True
                logger.info("Loaded credentials from %s", CONFIG_PATH)

        # Level 6: auto-register anonymous agent
        if not resolved_key:
            resolved_key, auto_agent_id = self._auto_register_anonymous(base_url)
            if not agent_id:
                agent_id = auto_agent_id
            save_credentials(resolved_key, agent_id, base_url=base_url)
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
        self._auto_credentials: bool = not _user_provided
        self._session_replaced: bool = False  # True when 401 + token (FIFO kick)
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

    def on(self, event_type: str) -> Callable:
        """Register a webhook/SSE event handler. Short alias for on_event().

        Supported event types: catastrophe.warning, energy.critical, agent.tick,
            catastrophe.active, catastrophe.resolved, agent.key_expired,
            agent.attacked, contract.proposed, contract.accepted, contract.breached,
            alliance.breach, market.opportunity, agent.mode_changed
        Special: "*" as catch-all for unregistered event types.

        Args:
            event_type: Event type string or "*" for catch-all.

        Returns:
            Decorator that registers the function as handler.

        Example::
            @agent.on("catastrophe.warning")
            def handle(event: dict) -> None:
                print(f"Warning: {event['catastrophe_type']}")
        """
        return self.on_event(event_type)

    # --- Properties ---

    @property
    def state(self) -> GameState | None:
        """Current game state (refreshed each tick).

        Filled automatically by the ``run_until``/``on_tick`` driver loop.
        If you don't use the on_tick driver — for example because you
        run a custom polling loop alongside other tasks — call
        :meth:`refresh_state` manually to populate this. Without one of
        the two, ``state`` stays ``None`` and downstream LLM-Decider /
        prompt-builder code sees an empty world.

        Background: cosmergon-pet diagnosed 2026-05-04 (Pet v0.1.20 fix)
        — its custom poll loop populated only its own display state and
        left ``agent._state`` at None, so the LLM-Decider built a
        single-choice schema ("wait" only) and the agent waited for ~33h
        despite the backend returning live state on every poll.
        """
        return self._state

    @property
    def memory(self) -> dict[str, Any]:
        """Persistent key-value store across ticks."""
        return self._memory

    async def refresh_state(self) -> GameState | None:
        """Fetch ``/state`` once and update ``self.state``.

        Use this when you build a client that does NOT rely on the
        ``run_until``/``on_tick`` driver loop — for example a Pet running
        its own polling, an LLM-Decider that wants a fresh snapshot
        right before a decision, or a one-shot benchmark script.

        Returns the fresh :class:`GameState` (also assigned to
        ``self._state``) or ``None`` if the request failed in a
        non-raising way (rate limit, transient 5xx). 4xx auth errors
        raise the usual :class:`AuthenticationError` /
        :class:`AuthorizationError` so callers can react explicitly.

        Background — cosmergon-pet 2026-05-04: Pet's custom poll loop
        had no public API to mirror its fetched state into
        ``agent._state``. Consumers had to reach into the private slot
        (``agent._state = ...``) to keep ``agent.state`` in sync, which
        is brittle and easy to forget. Pet missed it entirely; the
        LLM-Decider then saw a single-choice schema and the agent waited
        for ~33h. ``refresh_state()`` is the supported way to do it.
        """
        if self._client is None:
            raise RuntimeError("Agent not connected. Call run() or use async with.")
        resp = await self._request("GET", f"/api/v1/agents/{self.agent_id}/state")
        if resp.status_code == 200:
            self._state = GameState.from_api(resp.json())
            return self._state
        return None

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
        return resp.json()  # type: ignore[no-any-return]

    async def patch_identity(
        self,
        agent_name: str | None = None,
        persona: str | None = None,
    ) -> dict:
        """Update the agent's own display name and/or persona.

        Args:
            agent_name: New display name (3-50 chars, alphanumeric/_/-).
            persona: New persona type (scientist, warrior, expansionist,
                     trader, diplomat, farmer).

        Returns:
            Updated player dict on success, or {"error": ...} on failure.
        """
        payload: dict = {}
        if agent_name is not None:
            payload["agent_name"] = agent_name
        if persona is not None:
            payload["persona"] = persona
        resp = await self._request("PATCH", "/api/v1/players/me", json=payload)
        if resp.status_code >= 400:
            return {"error": resp.text, "status_code": resp.status_code}
        return resp.json()  # type: ignore[no-any-return]

    async def get_events(self, limit: int = 20) -> list[dict]:
        """Fetch recent game events for this agent (actions, compass changes, etc.).

        Returns a list of event dicts with keys: tick, event_type, data, created_at.
        """
        try:
            resp = await self._request(
                "GET",
                "/api/v1/events/",
                params={"agent_id": str(self.agent_id), "limit": min(limit, 100)},
            )
            if resp.status_code == 200:
                return resp.json().get("events", [])  # type: ignore[no-any-return]
        except Exception:
            pass
        return []

    async def fetch_memory_prompt(self) -> str:
        """Fetch the agent's server-side memory rendered as a prompt string.

        Cosmergon stores per-agent memory events (decisions, outcomes,
        encounters, lessons from past reflections). This method pulls
        them as a ready-to-use prompt section that you can prepend to
        your own LLM call (OpenAI, Anthropic, local Llama, etc.).

        Cosmergon does NOT run an LLM for you here — it only reads from
        the database and renders the events as text. You stay in
        control of which model handles inference and how much it costs.

        Typical usage::

            memory = await agent.fetch_memory_prompt()
            world = await agent.refresh_state()
            prompt = f"{my_system_prompt}\\n\\n{memory}\\n\\nWorld: {world}\\n\\nAction?"
            decision = my_llm.complete(prompt)
            await agent.act(decision.action, **decision.params)

        Returns:
            Rendered memory section. Empty string if the agent has no
            memory events yet, or on any transient server error.
        """
        try:
            resp = await self._request(
                "GET",
                f"/api/v1/agents/{self.agent_id}/memory/prompt",
            )
            if resp.status_code == 200:
                return resp.json().get("prompt", "")  # type: ignore[no-any-return]
        except Exception:
            pass
        return ""

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

    async def get_messages(self, limit: int = 50) -> list[dict]:
        """Fetch the chat conversation between player and this agent, oldest first.

        Returns a list of message dicts with keys: sender, message, message_type, created_at.
        Returns an empty list on error or if no conversation exists.
        """
        try:
            resp = await self._request(
                "GET",
                f"/api/v1/agents/{self.agent_id}/messages",
                params={"limit": min(limit, 100)},
            )
            if resp.status_code == 200:
                return resp.json()  # type: ignore[no-any-return]
        except Exception:
            pass
        return []

    async def send_message(self, text: str) -> dict:
        """Send a message to this agent. The agent will reply in its next tick whisper.

        Args:
            text: Message to send (max 500 chars). Sanitized server-side.

        Returns:
            Dict with keys: id (UUID str), created_at (str) on success.
            Dict with key: error (str) on failure (4xx/5xx).
        """
        resp = await self._request(
            "POST",
            f"/api/v1/agents/{self.agent_id}/messages",
            json={"message": text},
        )
        if resp.status_code >= 400:
            return {"error": resp.text}
        return resp.json()  # type: ignore[no-any-return]

    async def get_field_cells(self, field_id: str) -> dict[str, int]:
        """Fetch the live cell data for a game field.

        Returns a sparse dict mapping ``"x,y"`` coordinate strings to ``1``
        for each alive cell.  Returns an empty dict on error or if the field
        has no alive cells.

        Args:
            field_id: UUID of the game field to inspect.
        """
        try:
            resp = await self._request(
                "GET",
                f"/api/v1/game-fields/{field_id}/cells",
            )
            if resp.status_code == 200:
                return (resp.json() or {}).get("cells", {})  # type: ignore[no-any-return]
        except Exception:
            pass
        return {}

    async def get_benchmark_report(self, days: int = 7) -> dict | None:
        """Fetch the benchmark report for this agent.

        Args:
            days: Benchmark period in days (1-90, default 7).

        Returns:
            Report dict with scores, rank, strengths, weaknesses.
            Returns None on error or if the agent has insufficient data.
        """
        try:
            resp = await self._request(
                "GET",
                f"/api/v1/benchmark/{self.agent_id}/report",
                params={"days": min(max(days, 1), 90)},
            )
            if resp.status_code == 200:
                return resp.json()  # type: ignore[no-any-return]
        except Exception:
            pass
        return None

    # --- Webhook server ---

    def listen(
        self,
        port: int = 8080,
        host: str = "0.0.0.0",
        webhook_secret: str | None = None,
        path: str = "/webhook",
    ) -> None:
        """Start a blocking HTTP server that receives and dispatches Cosmergon webhooks.

        Verifies HMAC-SHA256 signatures and dispatches to handlers registered via on().
        Blocks the calling thread until KeyboardInterrupt.

        For background usage::

            import threading
            threading.Thread(target=agent.listen, kwargs={"port": 8080}, daemon=True).start()

        Note: Cosmergon requires a publicly reachable HTTPS URL to deliver webhooks.
        For local testing use a tunnel (ngrok, cloudflared).

        Args:
            port:           Port to listen on (default 8080).
            host:           Bind address (default 0.0.0.0 = all interfaces).
            webhook_secret: HMAC signing secret. Falls back to COSMERGON_WEBHOOK_SECRET
                            env var. If neither set: WARNING + signature check skipped.
            path:           URL path for webhook endpoint (default /webhook).
        """
        from http.server import BaseHTTPRequestHandler, HTTPServer

        secret = webhook_secret or os.environ.get("COSMERGON_WEBHOOK_SECRET")
        if not secret:
            logger.warning(
                "No webhook_secret set and COSMERGON_WEBHOOK_SECRET env var missing. "
                "Signature verification disabled — not safe for production."
            )

        agent = self  # closure for the handler class below

        class _WebhookHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                if self.path != path:
                    self.send_response(404)
                    self.end_headers()
                    return

                # Read body — cap at 1 MB to prevent memory exhaustion
                content_length = min(int(self.headers.get("Content-Length", 0)), 1024 * 1024)
                body = self.rfile.read(content_length)

                if secret:
                    sig = self.headers.get("X-Cosmergon-Signature", "")
                    ts = self.headers.get("X-Cosmergon-Timestamp", "")
                    try:
                        valid = CosmergonWebhook.verify_signature(body, sig, secret, ts)
                    except (WebhookSignatureError, WebhookTimestampError):
                        valid = False
                    if not valid:
                        # No detail in error body — avoids leaking timing/format info
                        self.send_response(400)
                        self.end_headers()
                        return

                try:
                    event = json.loads(body)
                except json.JSONDecodeError:
                    self.send_response(400)
                    self.end_headers()
                    return

                event_type = event.get("event_type", "")
                handler = agent._event_handlers.get(event_type) or agent._event_handlers.get("*")

                if handler:
                    if inspect.iscoroutinefunction(handler):
                        asyncio.run(handler(event))
                    else:
                        handler(event)

                self.send_response(200)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                logger.debug(format, *args)

        server = HTTPServer((host, port), _WebhookHandler)
        logger.info("Webhook server listening on %s:%d%s", host, port, path)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Webhook server stopped")
        finally:
            server.server_close()

    # --- SSE Event Stream ---

    def events(
        self,
        reconnect: bool = True,
        reconnect_delay: float = 5.0,
        max_reconnect_delay: float = 60.0,
    ) -> Iterator[dict]:
        """Consume the SSE event stream. Blocking sync generator.

        Opens GET /api/v1/agents/{agent_id}/events/stream and yields event dicts
        as they arrive. Heartbeats are silently dropped. Reconnects on connection
        loss if reconnect=True (exponential backoff: 5s → 10s → 20s → max 60s).

        Yields dicts with at minimum:
            event_type: str    — e.g. "catastrophe.warning"
            player_id:  str    — UUID of the agent's player

        Raises:
            AuthenticationError: 401 or 403 response (no reconnect).
            CosmergonError:      Unrecoverable server error.

        Example::
            agent = CosmergonAgent()   # auto-registers if no key saved
            for event in agent.events():
                if event["event_type"] == "catastrophe.warning":
                    asyncio.run(agent.act("evacuate"))
        """
        # Resolve agent_id synchronously if not yet known
        if not self.agent_id:
            plain_key = self._api_key.raw
            with httpx.Client(timeout=10.0) as resolve_client:
                resp = resolve_client.get(
                    f"{self.base_url}/api/v1/agents/",
                    headers={"Authorization": f"api-key {plain_key}"},
                )
                if resp.status_code in (401, 403):
                    raise AuthenticationError("Invalid API key (resolving agent_id)")
                agents = resp.json() if resp.status_code == 200 else []
                if agents:
                    self.agent_id = agents[0]["id"]
                else:
                    raise AuthenticationError("Could not resolve agent_id from API key")

        url = f"{self.base_url}/api/v1/agents/{self.agent_id}/events/stream"
        last_event_id: str = ""
        delay = reconnect_delay

        while True:
            try:
                plain_key = self._api_key.raw
                headers: dict[str, str] = {
                    "Authorization": f"api-key {plain_key}",
                    "Accept": "text/event-stream",
                    "Cache-Control": "no-cache",
                    "User-Agent": f"cosmergon-agent-python/{__version__}",
                }
                if last_event_id:
                    headers["Last-Event-ID"] = last_event_id

                # read=None keeps the connection open indefinitely (required for SSE).
                # Default 10s covers connect/write/pool; read=None overrides read-only.
                with httpx.Client(timeout=httpx.Timeout(10.0, read=None)) as sse_client:
                    with sse_client.stream("GET", url, headers=headers) as response:
                        if response.status_code in (401, 403):
                            raise AuthenticationError(
                                f"SSE stream rejected: {response.status_code}"
                            )
                        if response.status_code >= 400:
                            raise CosmergonError(f"SSE stream error: HTTP {response.status_code}")

                        for line in response.iter_lines():
                            if line.startswith("data: "):
                                try:
                                    yield json.loads(line[6:])
                                except json.JSONDecodeError:
                                    logger.warning("SSE: invalid JSON dropped: %r", line[6:])
                            elif line.startswith("id: "):
                                last_event_id = line[4:]
                            # ":" prefix = SSE comment/heartbeat → skip silently
                            # empty line = SSE event boundary → no action needed

                # Stream ended cleanly (server closed connection) → reconnect
                logger.info("SSE stream closed by server, reconnecting in %.1fs", delay)

            except AuthenticationError:
                raise  # auth errors are never retried
            except CosmergonError:
                raise  # unrecoverable server errors propagate
            except httpx.TransportError as exc:
                if not reconnect:
                    raise CsgConnectionError("SSE connection failed") from exc
                logger.warning("SSE transport error, reconnecting in %.1fs: %s", delay, exc)

            if not reconnect:
                return

            time.sleep(delay)
            delay = min(delay * 2, max_reconnect_delay)

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

    def stop(self) -> None:
        """Stop the agent's poll loop gracefully. Safe to call from on_tick."""
        self._running = False

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

    # --- Token resolution + reconnect ---

    @staticmethod
    def _resolve_via_token(
        token: str,
        base_url: str,
        agent_name: str | None,
    ) -> tuple[str, str]:
        """Resolve a Master Key to (api_key, agent_id).

        Saves token + all agents to config.toml for future use.
        """
        try:
            result = resolve_token_sync(token, base_url=base_url, agent_name=agent_name)
        except TokenResolutionError as exc:
            raise AuthenticationError(str(exc)) from exc

        selected = result.selected  # set by _parse_agents_response

        # Save token + all agents to config.toml (single write)
        save_all_agents_and_token(
            token,
            [(a.agent_name, a.api_key.raw, a.agent_id) for a in result.agents],
            selected.agent_name,
            base_url=base_url,
        )

        n = len(result.agents)
        logger.info(
            "Token resolved: %s (%s tier, %d agent%s). Active: %s",
            result.player_id[:8],
            result.subscription_tier,
            n,
            "s" if n != 1 else "",
            selected.agent_name,
        )

        return selected.api_key.raw, selected.agent_id

    def reconnect(self, api_key: str, agent_id: str) -> None:
        """Switch to a different agent without restarting.

        Swaps credentials and clears cached state. The httpx client is
        reused (same connection pool, just different auth headers).
        Called by the Dashboard agent-selector [A].
        """
        self._api_key = _SensitiveStr(api_key)
        self.agent_id = agent_id
        self._state = None
        self._auto_credentials = False
        self._session_replaced = False

        # Update auth header on the async client if it exists
        if self._client is not None:
            self._client.headers["Authorization"] = f"api-key {api_key}"

        logger.info("Reconnected to agent %s", agent_id[:8] if agent_id else "?")

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
                "Authorization": f"api-key {self._api_key.raw}",
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
        """HTTP request with retry and backoff (C2).

        429 responses raise RateLimitError immediately — retrying within the
        same game tick is pointless. Callers decide whether to queue or abort.
        5xx responses retry with exponential backoff up to max_retries.
        Transport errors (network) retry with exponential backoff.
        """
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
                        float(resp.headers.get("Retry-After", "1.0")),
                        _MAX_BACKOFF,
                    )
                    raise RateLimitError(retry_after=retry_after)

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

    async def _handle_expired_credentials(self) -> None:
        """React to a 401 response in the poll loop.

        Decision logic (Panel S110):
        - Token in config.toml → user-initiated reconnect required (no auto-resolve,
          prevents FIFO cascade with 4+ devices). Stops the loop with an error.
        - Auto-managed (no token, no explicit key) → re-register as new agent.
        - Explicitly provided key → stop with error.
        """
        has_token = bool(load_token())

        if has_token:
            # Paid user with token — FIFO-kick or revoked key.
            # Do NOT auto-resolve (prevents cascade). User must reconnect.
            # Set flag so Dashboard can show ReconnectScreen after loop ends.
            logger.error(
                "Authentication failed (401) — your key was replaced by a newer session. "
                "Reconnect with your Master Key: "
                "cosmergon-dashboard --token CSMR-..."
            )
            self._running = False
            self._session_replaced = True
        elif self._auto_credentials:
            # Free user, auto-managed — re-register as new agent
            logger.warning(
                "API key expired — registering as NEW anonymous agent. "
                "Your previous agent is no longer accessible from this device."
            )
            try:
                new_key, new_id = self._auto_register_anonymous(self.base_url)
                self._api_key = _SensitiveStr(new_key)
                self.agent_id = new_id
                save_credentials(new_key, new_id, base_url=self.base_url)
                await self.close()
                self._client = self._create_client()
            except Exception as exc:
                logger.error("Re-registration failed: %s", exc)
                self._running = False
        else:
            # Explicitly provided key — stop with error
            logger.error(
                "Authentication failed (401) — your API key has been revoked or expired. "
                "Reconnect with your Master Key: cosmergon-dashboard --token CSMR-... "
                "or contact support at contact@cosmergon.de"
            )
            self._running = False

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
                if resp.status_code == 401:
                    await self._handle_expired_credentials()
                    last_tick = -1
                    await asyncio.sleep(self.poll_interval)
                    continue
                if resp.status_code != 200:
                    logger.warning("State fetch failed: %d", resp.status_code)
                    await asyncio.sleep(self.poll_interval)
                    continue

                self._state = GameState.from_api(resp.json())

                # Migrate config.toml to nested format on first successful state fetch
                # (agent_name becomes known from server response — Panel decision S110)
                if last_tick == -1 and self._state.agent_name:
                    from cosmergon_agent.config import maybe_migrate

                    maybe_migrate(self._state.agent_name)

                if self._state.tick != last_tick and self._tick_handler:
                    last_tick = self._state.tick
                    await self._tick_handler(self._state)

            except RateLimitError as exc:
                logger.warning("State fetch rate limited, waiting %.1fs", exc.retry_after)
                await asyncio.sleep(exc.retry_after)
                continue
            except CsgConnectionError:
                logger.warning("Connection lost, retrying in %.0fs", self.poll_interval)
            except CosmergonError as exc:
                logger.error("API error in poll loop: %s", exc.message)
            except Exception:
                logger.exception("Unexpected error in agent loop")

            await asyncio.sleep(self.poll_interval)

        # After loop exits — signal FIFO-kick to caller via exception.
        # This raise is OUTSIDE the try/except block above, so it
        # propagates cleanly through start() → finally(close) → caller.
        if self._session_replaced:
            raise AuthenticationError(
                "Your key was replaced by a newer session. "
                "Press [R] to reconnect with your Master Key.",
            )

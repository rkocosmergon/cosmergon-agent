"""Token resolution — exchange a Master Key (CSMR-...) for agent credentials.

Provides sync and async variants for use by different entry points:
- resolve_token_sync: agent.py, langchain.py, cli.py, dashboard.py
- resolve_token_async: mcp.py

Network code lives here (not in config.py) to keep config.py pure TOML I/O.
Panel decision ENG-2 (S109).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# _SensitiveStr — masks credentials in repr to prevent accidental logging
# ---------------------------------------------------------------------------


class _SensitiveStr(str):
    """String that masks its value in repr/str to prevent accidental logging.

    ``str()`` and ``repr()`` return masked forms (safe for logs).
    Use ``.raw`` to get the unmasked value for HTTP headers and config writes.
    """

    @property
    def raw(self) -> str:
        """Return the unmasked value (for HTTP headers, config writes)."""
        return str.__str__(self)

    def __repr__(self) -> str:
        if len(self) <= 8:
            return "'***'"
        return f"'{self[:4]}...{self[-4:]}'"

    def __str__(self) -> str:
        return self.__repr__()


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ResolvedAgent:
    """One agent returned by token resolution."""

    agent_name: str
    agent_id: str
    api_key: _SensitiveStr
    persona: str = ""
    energy: float = 0.0
    tier: str = ""


@dataclass
class TokenResolutionResult:
    """Full result of a token resolution call."""

    player_id: str
    subscription_tier: str
    max_agents: int
    agents: list[ResolvedAgent] = field(default_factory=list)
    selected: ResolvedAgent | None = None


# ---------------------------------------------------------------------------
# Shared response parser
# ---------------------------------------------------------------------------


class TokenResolutionError(Exception):
    """Raised when token resolution fails with a clear user-facing message."""

    def __init__(self, message: str, status_code: int = 0) -> None:
        self.status_code = status_code
        super().__init__(message)


def _parse_agents_response(
    resp: httpx.Response,
    base_url: str,
    agent_name: str | None = None,
) -> TokenResolutionResult:
    """Parse GET /players/me/agents response into a TokenResolutionResult.

    Raises TokenResolutionError with user-facing messages for all error cases.
    """
    if resp.status_code == 401:
        raise TokenResolutionError(
            "Invalid master key. Check your key at cosmergon.com "
            "or press [K] in the dashboard.",
            status_code=401,
        )
    if resp.status_code == 403:
        raise TokenResolutionError(
            "Master keys are available with Solo and Developer plans. "
            "cosmergon.com/pricing",
            status_code=403,
        )
    if resp.status_code == 429:
        raise TokenResolutionError(
            "Too many requests. Master key endpoints are limited to 3 per hour.",
            status_code=429,
        )
    if resp.status_code != 200:
        body = resp.text[:200] if resp.text else "(empty)"
        raise TokenResolutionError(
            f"Token resolution failed ({resp.status_code}): {body}",
            status_code=resp.status_code,
        )

    data = resp.json()
    raw_agents = data.get("agents", [])
    if not raw_agents:
        raise TokenResolutionError(
            "No agents found for this account. "
            "Create one at cosmergon.com or via the API.",
            status_code=200,
        )

    agents = [
        ResolvedAgent(
            agent_name=a.get("agent_name", "?"),
            agent_id=a.get("agent_id", ""),
            api_key=_SensitiveStr(a.get("api_key", "")),
            persona=a.get("persona", ""),
            energy=float(a.get("energy", 0)),
            tier=a.get("tier", ""),
        )
        for a in raw_agents
    ]

    # Select agent: named → match, unnamed → oldest (Panel decision S110 #7+#9)
    if agent_name:
        matches = [a for a in agents if a.agent_name == agent_name]
        if not matches:
            available = ", ".join(a.agent_name for a in agents)
            raise TokenResolutionError(
                f"Agent '{agent_name}' not found. "
                f"Available agents: {available}. "
                f"Check spelling or omit agent_name to use the default.",
                status_code=200,
            )
        selected = matches[0]
    else:
        selected = agents[0]
        if len(agents) > 1:
            logger.warning(
                "Multiple agents found (%d), using oldest: %s. "
                "Specify agent_name= to select a different agent.",
                len(agents),
                selected.agent_name,
            )

    return TokenResolutionResult(
        player_id=data.get("player_id", ""),
        subscription_tier=data.get("subscription_tier", ""),
        max_agents=data.get("max_agents", 0),
        agents=agents,
        selected=selected,
    )


# ---------------------------------------------------------------------------
# Sync resolver (agent.py, langchain.py, cli.py, dashboard.py)
# ---------------------------------------------------------------------------


def resolve_token_sync(
    token: str,
    base_url: str = "https://cosmergon.com",
    agent_name: str | None = None,
    timeout: float = 15.0,
) -> TokenResolutionResult:
    """Exchange a Master Key for agent credentials (synchronous).

    Args:
        token: Master Key (CSMR-...).
        base_url: Server URL.
        agent_name: Select a specific agent by name. If None, all agents
            are returned and the caller picks (typically the oldest).
        timeout: HTTP timeout in seconds.

    Returns:
        TokenResolutionResult with all agents and their fresh API keys.

    Raises:
        TokenResolutionError: Server returned an error with a user-facing message.
    """
    raw_token = token.raw if isinstance(token, _SensitiveStr) else token
    url = f"{base_url.rstrip('/')}/api/v1/players/me/agents"
    try:
        resp = httpx.get(
            url,
            headers={"X-Player-Token": raw_token},
            timeout=timeout,
            verify=True,
        )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise TokenResolutionError(
            f"Cannot reach {base_url} — check your connection. "
            f"Your Master Key is valid, try again later.",
        ) from exc

    return _parse_agents_response(resp, base_url, agent_name)


# ---------------------------------------------------------------------------
# Async resolver (mcp.py)
# ---------------------------------------------------------------------------


async def resolve_token_async(
    token: str,
    base_url: str = "https://cosmergon.com",
    agent_name: str | None = None,
    timeout: float = 15.0,
) -> TokenResolutionResult:
    """Exchange a Master Key for agent credentials (asynchronous).

    Same semantics as resolve_token_sync but uses httpx.AsyncClient.
    """
    raw_token = token.raw if isinstance(token, _SensitiveStr) else token
    url = f"{base_url.rstrip('/')}/api/v1/players/me/agents"
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=True) as client:
            resp = await client.get(
                url,
                headers={"X-Player-Token": raw_token},
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise TokenResolutionError(
            f"Cannot reach {base_url} — check your connection. "
            f"Your Master Key is valid, try again later.",
        ) from exc

    return _parse_agents_response(resp, base_url, agent_name)

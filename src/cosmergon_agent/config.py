"""Per-instance configuration for Cosmergon agents.

Reads and writes ~/.cosmergon/config.toml with instance-aware credentials.
Supports two formats — flat (single agent) and nested (multi-agent):

Flat format (v0.5.x, single agent)::

    default_instance = "cosmergon-com"

    [instances.cosmergon-com]
    base_url = "https://cosmergon.com"
    api_key = "AGENT-xxx:secret"
    agent_id = "abc123"
    activated = true

Multi-agent format (v0.6.0+, with Master Key)::

    default_instance = "cosmergon-com"

    [instances.cosmergon-com]
    base_url = "https://cosmergon.com"
    player_token = "CSMR-a1b2c3..."
    active_agent = "Odin-blade"

    [instances.cosmergon-com.agents.Odin-blade]
    api_key = "AGENT-ABC:secret"
    agent_id = "uuid-1"

    [instances.cosmergon-com.agents.Odin-scout]
    api_key = "AGENT-DEF:secret"
    agent_id = "uuid-2"

Both formats are read transparently. Migration from flat to nested happens
when the agent name becomes known (after first get_state from server).
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef,import-not-found]

import tomli_w

logger = logging.getLogger(__name__)

CONFIG_PATH = Path.home() / ".cosmergon" / "config.toml"
_DEFAULT_BASE_URL = "https://cosmergon.com"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class AgentEntry:
    """Credentials for one agent within an instance."""

    api_key: str = ""
    agent_id: str | None = None


@dataclass
class InstanceConfig:
    """Credentials and metadata for one Cosmergon server instance."""

    api_key: str = ""
    agent_id: str | None = None
    base_url: str = _DEFAULT_BASE_URL
    activated: bool = False
    player_token: str = ""
    active_agent: str = ""
    agents: dict[str, AgentEntry] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.agents is None:
            self.agents = {}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _instance_name(url: str) -> str:
    """Derive a config key from a server URL.

    >>> _instance_name("https://cosmergon.com")
    'cosmergon-com'
    >>> _instance_name("http://localhost:8000")
    'localhost-8000'
    """
    parsed = urlparse(url)
    netloc = parsed.netloc or parsed.path
    return netloc.replace(".", "-").replace(":", "-").rstrip("-")


def _read_raw() -> dict:
    """Read config.toml as a dict.  Returns {} if the file is missing or broken."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        CONFIG_PATH.chmod(0o600)
        return tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except Exception:
        return {}


def _write_raw(data: dict) -> None:
    """Atomic write: temp file -> chmod 600 -> rename."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".toml.tmp")
    tmp.write_bytes(tomli_w.dumps(data).encode())
    tmp.chmod(0o600)
    tmp.replace(CONFIG_PATH)


def _migrate_if_needed(data: dict) -> dict:
    """Convert old ``[agent]`` format to ``[instances.*]`` in memory.

    Returns *data* unchanged when no migration is needed.
    """
    agent = data.get("agent")
    if not isinstance(agent, dict) or "api_key" not in agent:
        return data

    name = _instance_name(_DEFAULT_BASE_URL)

    instance: dict = {"base_url": _DEFAULT_BASE_URL, "api_key": agent["api_key"]}
    if agent.get("agent_id"):
        instance["agent_id"] = agent["agent_id"]
    if agent.get("activated"):
        instance["activated"] = True

    new_data: dict = {"default_instance": name}
    if agent.get("onboarding_modal_dismissed"):
        new_data["onboarding_modal_dismissed"] = True
    new_data["instances"] = {name: instance}
    return new_data


def _load_config() -> dict:
    """Read config and migrate in memory (never writes back on read)."""
    return _migrate_if_needed(_read_raw())


# ---------------------------------------------------------------------------
# Public API — credentials
# ---------------------------------------------------------------------------


def load_credentials(instance: str | None = None) -> tuple[str, str | None, bool]:
    """Load ``(api_key, agent_id, activated)`` for an instance.

    Reads the nested multi-agent format first (active_agent → agents subtable),
    then falls back to flat format (api_key directly on instance).

    Uses *default_instance* when *instance* is ``None``.
    Returns ``("", None, False)`` when nothing is saved.
    """
    data = _load_config()
    instances = data.get("instances", {})
    if not instances:
        return "", None, False

    name = instance or data.get("default_instance", "")
    cfg = instances.get(name, {})
    if not cfg:
        # Fall back to the first available instance
        cfg = next(iter(instances.values()), {})

    # Try nested multi-agent format first
    agents_table = cfg.get("agents", {})
    active = cfg.get("active_agent", "")
    if agents_table and active and active in agents_table:
        agent_cfg = agents_table[active]
        key = agent_cfg.get("api_key", "")
        key = key.replace("\r", "").replace("\n", "")
        return key, agent_cfg.get("agent_id") or None, True
    if agents_table:
        # active_agent not set or not found — use first agent
        first_agent = next(iter(agents_table.values()), {})
        if first_agent.get("api_key"):
            key = first_agent["api_key"].replace("\r", "").replace("\n", "")
            return key, first_agent.get("agent_id") or None, True

    # Fall back to flat format (v0.5.x single-agent)
    key = cfg.get("api_key", "")
    key = key.replace("\r", "").replace("\n", "")  # header-injection guard
    return key, cfg.get("agent_id") or None, cfg.get("activated", False)


def save_credentials(
    api_key: str,
    agent_id: str | None,
    *,
    base_url: str = _DEFAULT_BASE_URL,
    activated: bool = False,
    instance: str | None = None,
) -> None:
    """Persist credentials for an instance.  Migrates old format on first write."""
    data = _load_config()

    name = instance or data.get("default_instance") or _instance_name(base_url)

    if "instances" not in data:
        data["instances"] = {}

    inst: dict = data["instances"].setdefault(name, {})
    inst["base_url"] = base_url
    inst["api_key"] = api_key
    if agent_id:
        inst["agent_id"] = agent_id
    elif "agent_id" in inst:
        del inst["agent_id"]
    if activated:
        inst["activated"] = True
    elif "activated" in inst:
        del inst["activated"]

    data.setdefault("default_instance", name)

    try:
        _write_raw(data)
    except Exception as exc:
        logger.warning("Failed to save credentials to %s: %s", CONFIG_PATH, exc)


# ---------------------------------------------------------------------------
# Public API — multi-agent (v0.6.0+)
# ---------------------------------------------------------------------------


def load_token(instance: str | None = None) -> str:
    """Load the player token (Master Key) for an instance.

    Returns ``""`` if no token is saved.
    """
    data = _load_config()
    instances = data.get("instances", {})
    if not instances:
        return ""
    name = instance or data.get("default_instance", "")
    cfg = instances.get(name, {})
    if not cfg:
        cfg = next(iter(instances.values()), {})
    return cfg.get("player_token", "")


def save_token(
    token: str,
    *,
    base_url: str = _DEFAULT_BASE_URL,
    instance: str | None = None,
) -> None:
    """Persist a player token (Master Key) for an instance."""
    data = _load_config()
    name = instance or data.get("default_instance") or _instance_name(base_url)

    if "instances" not in data:
        data["instances"] = {}

    inst: dict = data["instances"].setdefault(name, {})
    inst["base_url"] = base_url
    inst["player_token"] = token
    data.setdefault("default_instance", name)

    try:
        _write_raw(data)
    except Exception as exc:
        logger.warning("Failed to save token to %s: %s", CONFIG_PATH, exc)


def load_all_agents(instance: str | None = None) -> dict[str, AgentEntry]:
    """Load all agents from the nested multi-agent format.

    Returns a dict mapping agent name → AgentEntry.
    Returns empty dict if no agents subtable exists.
    """
    data = _load_config()
    instances = data.get("instances", {})
    if not instances:
        return {}

    name = instance or data.get("default_instance", "")
    cfg = instances.get(name, {})
    if not cfg:
        cfg = next(iter(instances.values()), {})

    agents_table = cfg.get("agents", {})
    result: dict[str, AgentEntry] = {}
    for agent_name, agent_data in agents_table.items():
        result[agent_name] = AgentEntry(
            api_key=agent_data.get("api_key", ""),
            agent_id=agent_data.get("agent_id"),
        )
    return result


def save_agent(
    name: str,
    api_key: str,
    agent_id: str,
    *,
    base_url: str = _DEFAULT_BASE_URL,
    instance: str | None = None,
) -> None:
    """Save an agent's credentials in the nested multi-agent format.

    If an agent with the same name already exists, it is updated (upsert).
    If there's a name collision with a different agent_id, the second agent
    is stored under its agent_id as key with a warning.
    """
    data = _load_config()
    inst_name = instance or data.get("default_instance") or _instance_name(base_url)

    if "instances" not in data:
        data["instances"] = {}

    inst: dict = data["instances"].setdefault(inst_name, {})
    inst.setdefault("base_url", base_url)

    if "agents" not in inst:
        inst["agents"] = {}

    agents: dict = inst["agents"]

    # Check for name collision (different agent_id under same name)
    if name in agents and agents[name].get("agent_id") and agents[name]["agent_id"] != agent_id:
        logger.warning(
            "Duplicate agent name '%s'. Storing second agent under ID '%s'.",
            name,
            agent_id,
        )
        name = agent_id

    agents[name] = {"api_key": api_key, "agent_id": agent_id}

    # Set active_agent to this agent if none set
    if not inst.get("active_agent"):
        inst["active_agent"] = name

    data.setdefault("default_instance", inst_name)

    try:
        _write_raw(data)
    except Exception as exc:
        logger.warning("Failed to save agent to %s: %s", CONFIG_PATH, exc)


def save_all_agents_and_token(
    token: str,
    agents: list[tuple[str, str, str]],
    active_agent: str,
    *,
    base_url: str = _DEFAULT_BASE_URL,
    instance: str | None = None,
) -> None:
    """Save token + all agents in a single write operation.

    Args:
        token: Player token (Master Key).
        agents: List of (name, api_key, agent_id) tuples.
        active_agent: Name of the agent to set as active.
        base_url: Server URL.
        instance: Instance name override.
    """
    data = _load_config()
    inst_name = instance or data.get("default_instance") or _instance_name(base_url)

    if "instances" not in data:
        data["instances"] = {}

    inst: dict = data["instances"].setdefault(inst_name, {})
    inst["base_url"] = base_url
    inst["player_token"] = token
    inst["active_agent"] = active_agent

    if "agents" not in inst:
        inst["agents"] = {}

    agents_table: dict = inst["agents"]
    for name, api_key, agent_id in agents:
        # Duplicate name check (same as save_agent)
        effective_name = name
        existing = agents_table.get(name, {})
        if existing.get("agent_id") and existing["agent_id"] != agent_id:
            logger.warning("Duplicate agent name '%s'. Storing under ID '%s'.", name, agent_id)
            effective_name = agent_id
        agents_table[effective_name] = {"api_key": api_key, "agent_id": agent_id}

    data.setdefault("default_instance", inst_name)

    try:
        _write_raw(data)
    except Exception as exc:
        logger.warning("Failed to save agents to %s: %s", CONFIG_PATH, exc)


def set_active_agent(
    name: str,
    *,
    instance: str | None = None,
) -> None:
    """Set which agent is active (used by Dashboard agent-selector)."""
    data = _load_config()
    instances = data.get("instances", {})
    inst_name = instance or data.get("default_instance", "")
    if inst_name not in instances:
        return

    instances[inst_name]["active_agent"] = name

    try:
        _write_raw(data)
    except Exception as exc:
        logger.warning("Failed to set active agent in %s: %s", CONFIG_PATH, exc)


def maybe_migrate(
    agent_name: str,
    *,
    instance: str | None = None,
) -> None:
    """Migrate flat api_key+agent_id into an agents.{name} subtable.

    Called after the first successful get_state() when the agent name
    becomes known from the server response. Creates config.toml.bak
    before modifying the format.

    No-op if the config already uses the nested format or if there's
    nothing to migrate.
    """
    data = _load_config()
    instances = data.get("instances", {})
    if not instances:
        return

    inst_name = instance or data.get("default_instance", "")
    cfg = instances.get(inst_name, {})
    if not cfg:
        return

    # Already nested — nothing to do
    if cfg.get("agents"):
        return

    # No flat key — nothing to migrate
    flat_key = cfg.get("api_key", "")
    if not flat_key:
        return

    flat_id = cfg.get("agent_id", "")

    # Create backup before first migration
    if CONFIG_PATH.exists():
        bak = CONFIG_PATH.with_suffix(".toml.bak")
        if not bak.exists():
            try:
                import shutil
                shutil.copy2(CONFIG_PATH, bak)
                logger.info("Config backup created: %s", bak)
            except Exception:
                pass  # non-fatal

    # Move flat credentials into nested subtable
    cfg["agents"] = {
        agent_name: {"api_key": flat_key, "agent_id": flat_id} if flat_id else {"api_key": flat_key}
    }
    cfg["active_agent"] = agent_name

    # Remove flat keys (now in subtable)
    cfg.pop("api_key", None)
    cfg.pop("agent_id", None)
    # Keep 'activated' — it's a global flag, not per-agent

    try:
        _write_raw(data)
        logger.info("Config migrated to multi-agent format (agent: %s)", agent_name)
    except Exception as exc:
        logger.warning("Failed to migrate config: %s", exc)


# ---------------------------------------------------------------------------
# Public API — UI state (global, not per-instance)
# ---------------------------------------------------------------------------


def is_token_warning_shown() -> bool:
    """Return True if the token storage warning has been shown."""
    data = _read_raw()
    return bool(data.get("token_warning_shown"))


def set_token_warning_shown() -> None:
    """Mark token storage warning as shown (persists immediately)."""
    data = _load_config()
    data["token_warning_shown"] = True
    try:
        _write_raw(data)
    except Exception:
        pass  # non-fatal


def is_onboarding_dismissed() -> bool:
    """Return True if the onboarding modal has been dismissed."""
    data = _read_raw()
    # New location (top-level)
    if data.get("onboarding_modal_dismissed"):
        return True
    # Old location (inside [agent] table)
    agent = data.get("agent", {})
    return bool(isinstance(agent, dict) and agent.get("onboarding_modal_dismissed"))


def set_onboarding_dismissed() -> None:
    """Mark onboarding modal as dismissed (persists immediately)."""
    data = _load_config()
    data["onboarding_modal_dismissed"] = True
    try:
        _write_raw(data)
    except Exception:
        pass  # non-fatal

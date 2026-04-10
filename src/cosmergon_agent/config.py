"""Per-instance configuration for Cosmergon agents.

Reads and writes ~/.cosmergon/config.toml with instance-aware credentials.
Automatically migrates the old [agent] flat format on first write.

Config format::

    default_instance = "cosmergon-com"
    onboarding_modal_dismissed = true

    [instances.cosmergon-com]
    base_url = "https://cosmergon.com"
    api_key = "AGENT-xxx:secret"
    agent_id = "abc123"
    activated = true
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
class InstanceConfig:
    """Credentials and metadata for one Cosmergon server instance."""

    api_key: str = ""
    agent_id: str | None = None
    base_url: str = _DEFAULT_BASE_URL
    activated: bool = False


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
# Public API — UI state (global, not per-instance)
# ---------------------------------------------------------------------------


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

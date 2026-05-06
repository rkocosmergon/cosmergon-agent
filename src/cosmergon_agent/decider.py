"""Decider protocol — pluggable decision-architecture for api-agents.

Generalisation of the Pet's S157 LLMProvider layer. A Decider takes the
current GameState and returns an (action, params) tuple. Different
concrete implementations live in separate PyPI packages:

  - cosmergon-decider-cloud: Cloud-LLM-Call (Ollama / OpenAI / Anthropic)
  - cosmergon-decider-tree:  rule-based decision tree, no inference
  - cosmergon-decider-btrl:  Behavior-Tree skeleton with Q-Learning
                             tuning of branch selection
  - cosmergon-decider-local: local model inference (llama.cpp / ONNX),
                             with multiple acquisition methods
                             (pre-trained quantized, distilled,
                             custom-SLM, P-KD-Q-pipeline)

Design conventions:

* Stateless across decisions (mutable session state internal to a
  decider is its own concern, but multiple Pet/Cluster runs must be
  reproducible from a fresh decider instance).
* The Decider does not own the agent / SDK loop — the runner driver
  (Pet's `llm_decider_loop`, Cluster's `runner.py`) calls
  `decider.decide(state)` and dispatches `agent.act(action, **params)`.
* Concrete deciders register themselves under entry-point group
  ``cosmergon.deciders`` so consumers (Pet config.toml,
  Cluster docker-compose env) load them by short name.

See `docs/konzepte/konzept-decider-cluster-und-pet-module.md` for the
broader Lab-to-Module-to-Pet pipeline.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from cosmergon_agent.state import GameState


class DeciderError(Exception):
    """Decider failure that the runner should log and skip.

    Concrete subclasses for richer fault classification:
      - :class:`DeciderProviderError` (network / model serving issue)
      - :class:`DeciderValidationError` (decider output does not match
        the action vocabulary or schema)
    """


class DeciderProviderError(DeciderError):
    """Underlying provider (Ollama, OpenAI, llama.cpp) failed."""


class DeciderValidationError(DeciderError):
    """Decider output is structurally invalid (unknown action, malformed
    params)."""


@runtime_checkable
class Decider(Protocol):
    """Protocol every Cosmergon decider must satisfy.

    Attributes:
        name: Short registry name (entry-point key), e.g. ``"cloud"`` or
            ``"tree"``. Used in logs and Cluster-Empirie-Berichten so
            different deciders are distinguishable.
        version: SemVer string of the decider implementation. Empirie
            captures this so a regression after a module-update is
            attributable.
    """

    name: str
    version: str

    async def decide(self, state: GameState) -> tuple[str, dict[str, Any]]:
        """Choose an action for the given state.

        Returns:
            ``(action_name, params)`` where ``action_name`` is one of
            ``VALID_ACTIONS`` and ``params`` is a JSON-serializable
            dict. ``("wait", {})`` is a valid no-op decision.

        Raises:
            DeciderProviderError: underlying provider failed; runner
                should skip the tick.
            DeciderValidationError: decider produced structurally
                invalid output; runner should skip and log.
        """
        ...  # pragma: no cover

    async def healthcheck(self) -> bool:
        """Return True if the decider is ready to serve decisions.

        Cluster runners and Pet should poll this on startup and refuse
        to register the agent if the decider is not ready (rather than
        registering and silently failing every cycle).

        Implementations should be cheap (single ping, file existence
        check, connectivity test) — not run a full decision loop.
        """
        ...  # pragma: no cover


__all__ = [
    "Decider",
    "DeciderError",
    "DeciderProviderError",
    "DeciderValidationError",
]

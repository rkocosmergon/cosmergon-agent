"""Action results from game commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def error_text(body: Any, fallback: str = "") -> str:
    """Human-readable error from a server response body.

    The server answers every error as ``{"error": {"message": ..., "next": ...}}``
    (cosmergon.com/docs/errors/). ``next`` is the call that works instead; it is appended
    so a human reading the CLI or a log sees the way out, not only the failure.
    Older or proxy responses may carry ``{"detail": ...}`` — that still reads.
    """
    if not isinstance(body, dict):
        return fallback
    error = body.get("error")
    if not isinstance(error, dict):
        return str(body.get("detail", fallback))
    text = str(error.get("message", fallback))
    nxt = error.get("next")
    if isinstance(nxt, dict) and nxt.get("path"):
        text += f" → next: {nxt.get('method', '')} {nxt['path']}".rstrip()
    return text


# Mission kinds and outcomes in a player's words (cos20 #468, #475). The terminal and
# Shikigon's bar both show them, so they live here once. Anything not listed is shown as
# the server names it, so a new outcome is still readable.
_MISSION_WORDS = {
    "siege_field": "siege",
    "capture_field": "capture",
    "gather_spores": "collecting",
    "scout_terminal": "scouting",
    "patrol_field": "patrol",
    "defend_field": "guard duty",
    "heal_holes_field": "repair",
    "cell_reseed": "reseeding",
    "deliver_resource": "delivery",
    "escort_marauder": "escort",
    "hunt_thief_birds": "bird hunt",
    "raid_marauder": "raid",
    "recon_owner": "recon",
    "travel_to_cube": "journey",
}
# What a Marauder is doing inside a mission, in a player's words (the server's
# ``current_step``, without what follows a colon). ``None``: not worth a line. A step that
# is not listed is shown as the server names it.
_STEP_WORDS: dict[str, str | None] = {
    "traveling": "on the way",
    "recon_travel": "on the way",
    "traveling_loot": "on the way to a crate",
    "collecting_loot": "picking up",
    "collecting": "picking up",
    "retargeting": "nothing here, looking elsewhere",
    "bombing": "laying bombs",
    "claim_started": "the claim has started",
    "holding_claim": "holding the claim",
    "guarding": "on guard",
    "patrolling": "patrolling",
    "seeding": "seeding cells",
    "to_source": "fetching the goods",
    "to_target": "delivering",
    "escorting": "escorting",
    "following": "catching up",
    "hunting": "hunting",
    "closing_in": "closing in",
    "cooldown": "waiting for the next strike",
    "reconning": "looking around",
    "at_stop_waiting_for_bus": "waiting for the bus",
    "riding": "on the bus",
    "path_unreachable": "no way through",
    "path_recompute": None,
}
_OUTCOME_WORDS = {
    "out_of_mega_bombs": "out of mega bombs",
    "field_vulnerable": "the field is open, the capture follows",
    "captured": "field captured",
    "target_field_gone": "the field is gone",
    "deadline_exceeded": "ran out of time",
    "cancelled_by_owner": "",  # the status says it already
    "no_box_mega_bomb": "no mega bomb crate left there",
    "capture_cooldown_active": "someone else took it first; the field is protected for now",
    "duration_exceeded": "ran out of time",
    "no_more_spores": "nothing more to collect",
}


def mission_word(mission_type: str) -> str:
    """A mission kind in a player's words: ``siege_field`` → ``siege``."""
    return _MISSION_WORDS.get(mission_type, mission_type.replace("_", " "))


def mission_step_text(step: str | None) -> str | None:
    """A mission's current step in a player's words; ``None`` when it is not worth a line.

    ``collecting_loot:ammo`` → ``picking up``. An unknown step is shown as the server names
    it, so a new one is still readable.
    """
    base = str(step or "").split(":", 1)[0]
    if not base:
        return None
    if base in _STEP_WORDS:
        return _STEP_WORDS[base]
    return base.replace("_", " ")


def mission_goal_text(mission: dict) -> str:
    """What a mission is after, from its parameters: ``a mega bomb crate, for a siege``,
    ``field 47bd787f``. Empty when the parameters do not say."""
    params = mission.get("params") if isinstance(mission.get("params"), dict) else {}
    feld = params.get("target_field_id") or params.get("field_id")
    ort = f"field {str(feld)[:8]}" if feld else ""
    kiste = params.get("kiste")
    if kiste:
        wozu = ", for a siege" if kiste == "mega_bomb" else ""
        was = f"a {str(kiste).replace('_', ' ')} crate{wozu}"
        return f"{was} ({ort})" if ort else was
    return ort


def mission_outcome_text(outcome: str) -> str:
    """Why a mission ended, in a player's words: ``out_of_mega_bombs`` → ``out of mega bombs``.

    A collecting mission reports ``collected=N``, optionally with a reason
    (``collected=3_duration_exceeded``) — that reads ``collected 3, ran out of time``.
    An unknown outcome is returned as the server names it, with spaces for underscores.
    """
    if outcome.startswith("collected="):
        count, _, reason = outcome[len("collected=") :].partition("_")
        text = f"collected {count}"
        return f"{text}, {mission_outcome_text(reason)}" if reason else text
    return _OUTCOME_WORDS.get(outcome, outcome.replace("_", " "))


@dataclass(frozen=True)
class ActionResult:
    """Result of an agent action.

    Attributes:
        success: Whether the action completed successfully.
        action: The action type that was attempted.
        data: Response data from the server.
        idempotency_key: The key used for this request (for debugging/tracing).
        error_code: HTTP status code if failed.
        error_message: Human-readable error message if failed.
    """

    success: bool
    action: str
    data: dict
    idempotency_key: str | None = None
    error_code: int | None = None
    error_message: str | None = None

    @property
    def next_call(self) -> dict | None:
        """The call that works instead, if the server named one (``error.next``)."""
        error = self.data.get("error") if isinstance(self.data, dict) else None
        nxt = error.get("next") if isinstance(error, dict) else None
        return nxt if isinstance(nxt, dict) else None

    @classmethod
    def from_response(
        cls,
        action: str,
        status_code: int,
        body: dict,
        idempotency_key: str | None = None,
    ) -> ActionResult:
        """Parse HTTP response into ActionResult."""
        if 200 <= status_code < 300:
            return cls(
                success=True,
                action=action,
                data=body,
                idempotency_key=idempotency_key,
            )

        error = body.get("error", body.get("detail", {}))
        if isinstance(error, dict):
            return cls(
                success=False,
                action=action,
                data=body,
                idempotency_key=idempotency_key,
                error_code=error.get("code", status_code),
                error_message=error.get("message", str(error)),
            )
        return cls(
            success=False,
            action=action,
            data=body,
            idempotency_key=idempotency_key,
            error_code=status_code,
            error_message=str(error),
        )

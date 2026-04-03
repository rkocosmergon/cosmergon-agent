"""Tests for CosmergonWebhook — signature verification and event construction.

Spec: docs/konzepte/konzept-sdk-webhook-sse-api-2026-04-03.md §8
"""

from __future__ import annotations

import hmac
import json
import time
from hashlib import sha256

import pytest

from cosmergon_agent.exceptions import WebhookSignatureError, WebhookTimestampError
from cosmergon_agent.webhook import CosmergonWebhook


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SECRET = "test-webhook-secret-1234"


def _make_signature(payload: bytes, secret: str, timestamp: str) -> str:
    signed = f"{timestamp}.".encode() + payload
    digest = hmac.new(secret.encode(), signed, sha256).hexdigest()
    return f"sha256={digest}"


def _now() -> str:
    return str(int(time.time()))


def _payload(event_type: str = "catastrophe.warning") -> bytes:
    return json.dumps({"event_type": event_type, "player_id": "abc-123"}).encode()


# ---------------------------------------------------------------------------
# verify_signature
# ---------------------------------------------------------------------------


def test_verify_valid_signature() -> None:
    payload = _payload()
    ts = _now()
    sig = _make_signature(payload, SECRET, ts)
    assert CosmergonWebhook.verify_signature(payload, sig, SECRET, ts) is True


def test_verify_wrong_signature() -> None:
    payload = _payload()
    ts = _now()
    sig = _make_signature(payload, SECRET, ts)
    # Flip one char in the hex part
    tampered = sig[:-1] + ("0" if sig[-1] != "0" else "1")
    assert CosmergonWebhook.verify_signature(payload, tampered, SECRET, ts) is False


def test_verify_wrong_secret() -> None:
    payload = _payload()
    ts = _now()
    sig = _make_signature(payload, "other-secret", ts)
    assert CosmergonWebhook.verify_signature(payload, sig, SECRET, ts) is False


def test_verify_stale_timestamp() -> None:
    payload = _payload()
    stale_ts = str(int(time.time()) - CosmergonWebhook.TIMESTAMP_TOLERANCE_SECONDS - 1)
    sig = _make_signature(payload, SECRET, stale_ts)
    with pytest.raises(WebhookTimestampError):
        CosmergonWebhook.verify_signature(payload, sig, SECRET, stale_ts)


def test_verify_malformed_signature() -> None:
    payload = _payload()
    ts = _now()
    with pytest.raises(WebhookSignatureError):
        CosmergonWebhook.verify_signature(payload, "md5=deadbeef", SECRET, ts)


# ---------------------------------------------------------------------------
# construct_event
# ---------------------------------------------------------------------------


def test_construct_event_valid() -> None:
    payload = _payload("agent.attacked")
    ts = _now()
    sig = _make_signature(payload, SECRET, ts)
    event = CosmergonWebhook.construct_event(payload, sig, SECRET, ts)
    assert event["event_type"] == "agent.attacked"
    assert event["player_id"] == "abc-123"


def test_construct_event_invalid_json() -> None:
    payload = b"not-json"
    ts = _now()
    sig = _make_signature(payload, SECRET, ts)
    with pytest.raises(ValueError):
        CosmergonWebhook.construct_event(payload, sig, SECRET, ts)


def test_construct_event_invalid_sig() -> None:
    payload = _payload()
    ts = _now()
    wrong_sig = _make_signature(payload, "wrong-secret", ts)
    with pytest.raises(WebhookSignatureError):
        CosmergonWebhook.construct_event(payload, wrong_sig, SECRET, ts)

"""Tests for CosmergonAgent.listen() — webhook HTTP server.

Each test gets its own agent instance and server port (OS-assigned) to avoid
port conflicts between concurrent test runs.

Spec: docs/konzepte/konzept-sdk-webhook-sse-api-2026-04-03.md §8
"""

from __future__ import annotations

import asyncio
import hmac
import http.client
import json
import socket
import threading
import time
from hashlib import sha256

import pytest

from cosmergon_agent.agent import CosmergonAgent

SECRET = "listener-test-secret"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Return a free TCP port (OS-assigned)."""
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _make_signature(payload: bytes, secret: str, timestamp: str) -> str:
    signed = f"{timestamp}.".encode() + payload
    digest = hmac.new(secret.encode(), signed, sha256).hexdigest()
    return f"sha256={digest}"


def _now() -> str:
    return str(int(time.time()))


def _post(
    port: int,
    body: bytes,
    sig: str = "",
    ts: str = "",
    path: str = "/webhook",
) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("localhost", port, timeout=3)
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
    }
    if sig:
        headers["X-Cosmergon-Signature"] = sig
    if ts:
        headers["X-Cosmergon-Timestamp"] = ts
    conn.request("POST", path, body=body, headers=headers)
    return conn.getresponse()


def _wait_for_server(port: int, timeout: float = 2.0) -> None:
    """Retry until the server accepts connections or timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection("localhost", port, timeout=0.2)
            # POST with empty body — server responds (sig error or 200, both fine)
            conn.request("POST", "/webhook", body=b"{}", headers={"Content-Length": "2"})
            conn.getresponse()
            return
        except Exception:
            time.sleep(0.05)
    raise RuntimeError(f"Webhook server on port {port} did not start within {timeout}s")


@pytest.fixture
def server():
    """Start agent.listen() in a daemon thread. Yields (agent, port)."""
    agent = CosmergonAgent(api_key="csg_testkey", base_url="http://localhost:1")
    port = _free_port()
    t = threading.Thread(
        target=agent.listen,
        kwargs={"port": port, "webhook_secret": SECRET},
        daemon=True,
    )
    t.start()
    _wait_for_server(port)
    yield agent, port
    # Daemon thread is killed automatically when the test process finishes.
    # Between fixtures: each test gets a fresh port, so no conflict.


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_listen_dispatches_valid_event(server: tuple) -> None:
    """POST with valid signature → handler is called with correct event dict."""
    agent, port = server
    received: list[dict] = []

    @agent.on("catastrophe.warning")
    def handle(event: dict) -> None:
        received.append(event)

    payload = json.dumps({"event_type": "catastrophe.warning", "player_id": "p1"}).encode()
    ts = _now()
    sig = _make_signature(payload, SECRET, ts)

    resp = _post(port, payload, sig, ts)
    assert resp.status == 200
    assert len(received) == 1
    assert received[0]["event_type"] == "catastrophe.warning"


def test_listen_rejects_invalid_signature(server: tuple) -> None:
    """POST with wrong signature → 400, handler not called."""
    agent, port = server
    received: list[dict] = []

    @agent.on("energy.critical")
    def handle(event: dict) -> None:
        received.append(event)

    payload = json.dumps({"event_type": "energy.critical", "player_id": "p1"}).encode()
    ts = _now()
    bad_sig = "sha256=" + "0" * 64  # wrong but well-formed

    resp = _post(port, payload, bad_sig, ts)
    assert resp.status == 400
    assert len(received) == 0


def test_listen_handles_unknown_event(server: tuple) -> None:
    """Unknown event type with no handler → 200, no error."""
    _, port = server
    payload = json.dumps({"event_type": "some.unknown.event", "player_id": "p1"}).encode()
    ts = _now()
    sig = _make_signature(payload, SECRET, ts)

    resp = _post(port, payload, sig, ts)
    assert resp.status == 200


def test_listen_wildcard_handler(server: tuple) -> None:
    """'*' handler receives events with no specific handler registered."""
    agent, port = server
    caught: list[dict] = []

    @agent.on("*")
    def catch_all(event: dict) -> None:
        caught.append(event)

    payload = json.dumps({"event_type": "market.opportunity", "player_id": "p1"}).encode()
    ts = _now()
    sig = _make_signature(payload, SECRET, ts)

    resp = _post(port, payload, sig, ts)
    assert resp.status == 200
    assert len(caught) == 1
    assert caught[0]["event_type"] == "market.opportunity"


def test_listen_async_handler(server: tuple) -> None:
    """Async handler is called via asyncio.run() and executes correctly."""
    agent, port = server
    results: list[str] = []

    @agent.on("agent.attacked")
    async def async_handle(event: dict) -> None:
        await asyncio.sleep(0)  # yield to verify async context works
        results.append(event["event_type"])

    payload = json.dumps({"event_type": "agent.attacked", "player_id": "p1"}).encode()
    ts = _now()
    sig = _make_signature(payload, SECRET, ts)

    resp = _post(port, payload, sig, ts)
    assert resp.status == 200
    assert results == ["agent.attacked"]

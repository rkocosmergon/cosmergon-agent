"""Cosmergon webhook signature verification.

Standalone utility — no CosmergonAgent instance required.
Designed for use in any HTTP framework (FastAPI, Flask, AWS Lambda, etc.).

Algorithm matches backend webhook_dispatcher.py:_build_signature exactly.
"""

from __future__ import annotations

import hmac
import json
import time
from hashlib import sha256

from cosmergon_agent.exceptions import WebhookSignatureError, WebhookTimestampError


class CosmergonWebhook:
    """Verify and parse incoming Cosmergon webhook payloads.

    Example (FastAPI)::

        from fastapi import Request, HTTPException
        from cosmergon_agent import CosmergonWebhook, WebhookSignatureError

        @app.post("/webhook")
        async def webhook(request: Request):
            body = await request.body()
            try:
                event = CosmergonWebhook.construct_event(
                    payload=body,
                    signature_header=request.headers["X-Cosmergon-Signature"],
                    secret=webhook_secret,  # your signing secret from webhook registration
                    timestamp_header=request.headers["X-Cosmergon-Timestamp"],
                )
            except (WebhookSignatureError, WebhookTimestampError):
                raise HTTPException(status_code=400)
            handle(event)
    """

    TIMESTAMP_TOLERANCE_SECONDS: int = 300

    @staticmethod
    def verify_signature(
        payload: bytes,
        signature_header: str,
        secret: str,
        timestamp_header: str,
    ) -> bool:
        """Verify HMAC-SHA256 signature of an incoming webhook payload.

        Args:
            payload:          Raw request body bytes.
            signature_header: Value of X-Cosmergon-Signature header ("sha256=...").
            secret:           Webhook signing secret returned at endpoint registration.
            timestamp_header: Value of X-Cosmergon-Timestamp header (Unix timestamp string).

        Returns:
            True if signature is valid and timestamp is fresh.

        Raises:
            WebhookSignatureError: signature_header does not start with "sha256=".
            WebhookTimestampError: Timestamp older than TIMESTAMP_TOLERANCE_SECONDS.
        """
        if not signature_header.startswith("sha256="):
            raise WebhookSignatureError(
                f"Malformed signature header: expected 'sha256=...', got {signature_header!r}"
            )

        try:
            ts = int(timestamp_header)
        except ValueError as exc:
            raise WebhookSignatureError(
                f"Malformed timestamp header: {timestamp_header!r}"
            ) from exc

        age = abs(time.time() - ts)
        if age > CosmergonWebhook.TIMESTAMP_TOLERANCE_SECONDS:
            raise WebhookTimestampError(
                f"Webhook timestamp too old: {age:.0f}s (tolerance "
                f"{CosmergonWebhook.TIMESTAMP_TOLERANCE_SECONDS}s)"
            )

        # Identical to backend webhook_dispatcher.py:_build_signature
        signed_payload = f"{timestamp_header}.".encode() + payload
        expected = "sha256=" + hmac.new(
            secret.encode(), signed_payload, sha256
        ).hexdigest()

        return hmac.compare_digest(expected, signature_header)

    @staticmethod
    def construct_event(
        payload: bytes,
        signature_header: str,
        secret: str,
        timestamp_header: str,
    ) -> dict:
        """Verify signature and return the parsed event dict.

        Convenience wrapper around verify_signature + json.loads.

        Args:
            payload:          Raw request body bytes.
            signature_header: Value of X-Cosmergon-Signature header.
            secret:           Webhook signing secret.
            timestamp_header: Value of X-Cosmergon-Timestamp header.

        Returns:
            Parsed event dict (contains at least "event_type" and "player_id").

        Raises:
            WebhookSignatureError: Invalid or malformed signature.
            WebhookTimestampError: Timestamp too old.
            ValueError:            Payload is not valid JSON.
        """
        valid = CosmergonWebhook.verify_signature(
            payload, signature_header, secret, timestamp_header
        )
        if not valid:
            raise WebhookSignatureError("Webhook signature mismatch")

        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Webhook payload is not valid JSON: {exc}") from exc

"""Tests for auto-registration (anonymous agent flow)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from cosmergon_agent import CosmergonAgent
from cosmergon_agent.exceptions import CosmergonError


class TestAutoRegistration:
    """Tests for _auto_register_anonymous."""

    def test_returns_key_and_agent_id(self) -> None:
        """Successful registration returns (api_key, agent_id)."""
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "api_key": "AGENT-TEST:abc123",
            "agent_id": "uuid-1234",
            "expires_at": "2026-04-01T00:00:00Z",
        }

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = lambda s: s
            mock_client.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.return_value.post.return_value = fake_resp

            key, agent_id = CosmergonAgent._auto_register_anonymous(
                "https://cosmergon.com",
            )

        assert key == "AGENT-TEST:abc123"
        assert agent_id == "uuid-1234"

    def test_raises_on_http_error(self) -> None:
        """Non-200 response raises CosmergonError."""
        fake_resp = MagicMock()
        fake_resp.status_code = 429
        fake_resp.headers = {"content-type": "application/json"}
        fake_resp.json.return_value = {"detail": "Rate limited"}
        fake_resp.text = '{"detail": "Rate limited"}'

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = lambda s: s
            mock_client.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.return_value.post.return_value = fake_resp

            with pytest.raises(CosmergonError, match="429"):
                CosmergonAgent._auto_register_anonymous(
                    "https://cosmergon.com",
                )

    def test_raises_on_missing_key(self) -> None:
        """Response without api_key raises CosmergonError."""
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {"agent_id": "uuid-1234"}

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = lambda s: s
            mock_client.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.return_value.post.return_value = fake_resp

            with pytest.raises(CosmergonError, match="no API key"):
                CosmergonAgent._auto_register_anonymous(
                    "https://cosmergon.com",
                )

    def test_no_key_creates_agent_with_auto_id(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """CosmergonAgent() without key uses auto-registered agent_id."""
        monkeypatch.setattr(
            CosmergonAgent, "_auto_register_anonymous",
            staticmethod(lambda base_url: ("AGENT-X:key", "auto-uuid")),
        )
        agent = CosmergonAgent()
        assert agent.agent_id == "auto-uuid"

    def test_explicit_key_skips_auto_register(self) -> None:
        """Explicit api_key does not trigger auto-registration."""
        agent = CosmergonAgent(api_key="AGENT-MANUAL:key123")
        assert agent.agent_id is None  # resolved later via API

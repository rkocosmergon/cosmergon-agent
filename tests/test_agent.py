"""Tests for CosmergonAgent — constructor, repr, sensitive str."""


import pytest

from cosmergon_agent.agent import CosmergonAgent, _SensitiveStr


class TestSensitiveStr:
    def test_repr_masks_long_key(self) -> None:
        """Long keys show first 4 and last 4 chars."""
        s = _SensitiveStr("csg_abcdefgh12345678")
        assert "csg_" in repr(s)
        assert "5678" in repr(s)
        assert "abcdefgh" not in repr(s)

    def test_repr_masks_short_key(self) -> None:
        """Short keys are fully masked."""
        s = _SensitiveStr("abc")
        assert repr(s) == "'***'"

    def test_str_same_as_repr(self) -> None:
        """str() also masks the value."""
        s = _SensitiveStr("csg_secretkey1234")
        assert str(s) == repr(s)
        assert "secretkey" not in str(s)


class TestAgentConstructor:
    def test_requires_api_key(self) -> None:
        """Empty api_key raises ValueError."""
        with pytest.raises(ValueError, match="api_key"):
            CosmergonAgent(api_key="")

    def test_requires_valid_base_url(self) -> None:
        """Invalid base_url raises ValueError."""
        with pytest.raises(ValueError, match="base_url"):
            CosmergonAgent(api_key="csg_test", base_url="ftp://wrong")

    def test_accepts_valid_params(self) -> None:
        """Valid parameters create agent without error."""
        agent = CosmergonAgent(api_key="csg_test123", base_url="http://localhost:8082")
        assert agent.agent_id is None
        assert agent.poll_interval == 10.0

    def test_env_var_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """API key falls back to COSMERGON_API_KEY env var."""
        monkeypatch.setenv("COSMERGON_API_KEY", "csg_from_env")
        agent = CosmergonAgent(base_url="http://localhost:8082")
        assert "csg_" in repr(agent)

    def test_strips_trailing_slash(self) -> None:
        """base_url trailing slash is removed."""
        agent = CosmergonAgent(api_key="csg_test", base_url="http://localhost:8082/")
        assert agent.base_url == "http://localhost:8082"


class TestAgentRepr:
    def test_repr_masks_api_key(self) -> None:
        """repr() never shows full API key."""
        agent = CosmergonAgent(api_key="csg_supersecretkey12345678", base_url="http://test")
        r = repr(agent)
        assert "supersecret" not in r
        assert "csg_" in r
        assert "http://test" in r

    def test_repr_in_format_string(self) -> None:
        """f-string with agent does not leak key."""
        agent = CosmergonAgent(api_key="csg_supersecretkey12345678", base_url="http://test")
        s = f"Agent: {agent!r}"
        assert "supersecret" not in s


class TestAgentDecorators:
    def test_on_tick_registers_handler(self) -> None:
        """on_tick decorator stores the handler."""
        agent = CosmergonAgent(api_key="csg_test", base_url="http://test")

        @agent.on_tick
        async def my_handler(state):
            pass

        assert agent._tick_handler is my_handler

    def test_on_error_registers_handler(self) -> None:
        """on_error decorator stores the handler."""
        agent = CosmergonAgent(api_key="csg_test", base_url="http://test")

        @agent.on_error
        async def my_error_handler(result):
            pass

        assert agent._error_handler is my_error_handler

    def test_on_event_registers_handler(self) -> None:
        """on_event decorator stores typed handler."""
        agent = CosmergonAgent(api_key="csg_test", base_url="http://test")

        @agent.on_event("catastrophe_warning")
        async def handle_cat(event):
            pass

        assert agent._event_handlers["catastrophe_warning"] is handle_cat

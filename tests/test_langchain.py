"""Tests for LangChain integration module.

Tests use mocking since langchain-core is an optional dependency.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import httpx
import pytest


class TestLangchainImportError:
    def test_cosmergon_tools_raises_without_langchain(self) -> None:
        """cosmergon_tools raises ImportError when langchain-core is not installed."""
        # Temporarily hide langchain_core from imports
        with patch.dict(sys.modules, {"langchain_core": None, "langchain_core.tools": None}):
            # Force re-import to pick up the patched sys.modules
            import importlib

            import cosmergon_agent.integrations.langchain as lc_mod
            importlib.reload(lc_mod)

            with pytest.raises(ImportError, match="langchain-core"):
                lc_mod.cosmergon_tools(api_key="csg_test", base_url="http://test")


class TestGetClient:
    def test_get_client_returns_sync_client(self) -> None:
        """_get_client creates a sync httpx.Client with correct headers."""
        from cosmergon_agent.integrations.langchain import _get_client

        client = _get_client("csg_test123", "http://localhost:8082")
        assert isinstance(client, httpx.Client)
        assert "api-key csg_test123" in client.headers.get("authorization", "")
        client.close()

    def test_get_client_sets_timeout(self) -> None:
        """_get_client sets 30s timeout."""
        from cosmergon_agent.integrations.langchain import _get_client

        client = _get_client("csg_test", "http://test")
        # httpx stores timeout as Timeout object
        assert client.timeout.read == 30.0
        client.close()


class TestResolveAgentId:
    def test_resolve_agent_id_success(self) -> None:
        """_resolve_agent_id returns first agent ID on success."""
        from cosmergon_agent.integrations.langchain import _resolve_agent_id

        mock_client = MagicMock(spec=httpx.Client)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"id": "agent-123"}]
        mock_client.get.return_value = mock_resp

        result = _resolve_agent_id(mock_client)
        assert result == "agent-123"

    def test_resolve_agent_id_empty_list(self) -> None:
        """_resolve_agent_id raises ValueError on empty list."""
        from cosmergon_agent.integrations.langchain import _resolve_agent_id

        mock_client = MagicMock(spec=httpx.Client)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        mock_client.get.return_value = mock_resp

        with pytest.raises(ValueError, match="Could not resolve"):
            _resolve_agent_id(mock_client)

    def test_resolve_agent_id_non_200(self) -> None:
        """_resolve_agent_id raises ValueError on non-200 response."""
        from cosmergon_agent.integrations.langchain import _resolve_agent_id

        mock_client = MagicMock(spec=httpx.Client)
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.json.return_value = {"error": "unauthorized"}
        mock_client.get.return_value = mock_resp

        with pytest.raises(ValueError, match="Could not resolve"):
            _resolve_agent_id(mock_client)


class TestCosmergonToolsWithMockedLangchain:
    """Test cosmergon_tools by mocking langchain_core.tools.tool decorator."""

    def _setup_mock_langchain(self):
        """Create a mock langchain_core module with a @tool decorator."""
        # Create a @tool decorator that just passes through the function
        # but adds the expected attributes
        def mock_tool_decorator(func):
            func.name = func.__name__
            func.description = func.__doc__
            return func

        mock_tools_module = ModuleType("langchain_core.tools")
        mock_tools_module.tool = mock_tool_decorator  # type: ignore[attr-defined]

        mock_langchain_core = ModuleType("langchain_core")

        return {
            "langchain_core": mock_langchain_core,
            "langchain_core.tools": mock_tools_module,
        }

    def test_cosmergon_tools_returns_four_tools(self) -> None:
        """cosmergon_tools returns 4 tools."""
        mock_modules = self._setup_mock_langchain()

        with patch.dict(sys.modules, mock_modules):
            import importlib

            import cosmergon_agent.integrations.langchain as lc_mod
            importlib.reload(lc_mod)

            with patch.object(lc_mod, "_get_client"), \
                 patch.object(lc_mod, "_resolve_agent_id", return_value="agent-1"):
                tools = lc_mod.cosmergon_tools(api_key="csg_test", base_url="http://test")

        assert len(tools) == 4

    def test_tool_names(self) -> None:
        """Tools have expected names."""
        mock_modules = self._setup_mock_langchain()

        with patch.dict(sys.modules, mock_modules):
            import importlib

            import cosmergon_agent.integrations.langchain as lc_mod
            importlib.reload(lc_mod)

            with patch.object(lc_mod, "_get_client"), \
                 patch.object(lc_mod, "_resolve_agent_id", return_value="agent-1"):
                tools = lc_mod.cosmergon_tools(api_key="csg_test", base_url="http://test")

        names = {t.__name__ for t in tools}
        expected = {
            "cosmergon_observe", "cosmergon_act",
            "cosmergon_benchmark", "cosmergon_info",
        }
        assert names == expected

    def test_no_api_key_raises(self) -> None:
        """cosmergon_tools raises ValueError without API key."""
        mock_modules = self._setup_mock_langchain()

        with patch.dict(sys.modules, mock_modules):
            import importlib

            import cosmergon_agent.integrations.langchain as lc_mod
            importlib.reload(lc_mod)

            with patch.dict("os.environ", {}, clear=True):
                with pytest.raises(ValueError, match="api_key"):
                    lc_mod.cosmergon_tools(api_key="", base_url="http://test")

    def test_env_var_fallback(self) -> None:
        """cosmergon_tools falls back to COSMERGON_API_KEY env var."""
        mock_modules = self._setup_mock_langchain()

        with patch.dict(sys.modules, mock_modules):
            import importlib

            import cosmergon_agent.integrations.langchain as lc_mod
            importlib.reload(lc_mod)

            with patch.object(lc_mod, "_get_client") as mock_get_client, \
                 patch.object(lc_mod, "_resolve_agent_id", return_value="agent-1"), \
                 patch.dict("os.environ", {"COSMERGON_API_KEY": "csg_from_env"}):
                tools = lc_mod.cosmergon_tools(base_url="http://test")

        assert len(tools) == 4
        mock_get_client.assert_called_once_with("csg_from_env", "http://test")

    def test_observe_tool_calls_api(self) -> None:
        """cosmergon_observe tool calls the state endpoint."""
        mock_modules = self._setup_mock_langchain()

        with patch.dict(sys.modules, mock_modules):
            import importlib

            import cosmergon_agent.integrations.langchain as lc_mod
            importlib.reload(lc_mod)

            mock_client = MagicMock(spec=httpx.Client)
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"energy": 1000, "tick": 5}
            mock_client.get.return_value = mock_resp

            with patch.object(lc_mod, "_get_client", return_value=mock_client), \
                 patch.object(lc_mod, "_resolve_agent_id", return_value="agent-1"):
                tools = lc_mod.cosmergon_tools(api_key="csg_test", base_url="http://test")

            observe = next(t for t in tools if t.__name__ == "cosmergon_observe")
            result = observe("summary")
            assert '"energy": 1000' in result

    def test_act_tool_calls_api(self) -> None:
        """cosmergon_act tool posts to the action endpoint."""
        mock_modules = self._setup_mock_langchain()

        with patch.dict(sys.modules, mock_modules):
            import importlib

            import cosmergon_agent.integrations.langchain as lc_mod
            importlib.reload(lc_mod)

            mock_client = MagicMock(spec=httpx.Client)
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"success": True}
            mock_client.post.return_value = mock_resp

            with patch.object(lc_mod, "_get_client", return_value=mock_client), \
                 patch.object(lc_mod, "_resolve_agent_id", return_value="agent-1"):
                tools = lc_mod.cosmergon_tools(api_key="csg_test", base_url="http://test")

            act = next(t for t in tools if t.__name__ == "cosmergon_act")
            result = act("place_cells", '{"field_id": "f1"}')
            assert '"success": true' in result

    def test_benchmark_tool_calls_api(self) -> None:
        """cosmergon_benchmark tool calls benchmark endpoint."""
        mock_modules = self._setup_mock_langchain()

        with patch.dict(sys.modules, mock_modules):
            import importlib

            import cosmergon_agent.integrations.langchain as lc_mod
            importlib.reload(lc_mod)

            mock_client = MagicMock(spec=httpx.Client)
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"rank": 1, "score": 95.0}
            mock_client.get.return_value = mock_resp

            with patch.object(lc_mod, "_get_client", return_value=mock_client), \
                 patch.object(lc_mod, "_resolve_agent_id", return_value="agent-1"):
                tools = lc_mod.cosmergon_tools(api_key="csg_test", base_url="http://test")

            bench = next(t for t in tools if t.__name__ == "cosmergon_benchmark")
            result = bench(7)
            assert '"rank": 1' in result

    def test_info_tool_calls_api(self) -> None:
        """cosmergon_info tool calls game info and metrics endpoints."""
        mock_modules = self._setup_mock_langchain()

        with patch.dict(sys.modules, mock_modules):
            import importlib

            import cosmergon_agent.integrations.langchain as lc_mod
            importlib.reload(lc_mod)

            mock_client = MagicMock(spec=httpx.Client)
            mock_info_resp = MagicMock()
            mock_info_resp.json.return_value = {"game": "cosmergon"}
            mock_metrics_resp = MagicMock()
            mock_metrics_resp.json.return_value = {"agents": 48}
            mock_client.get.side_effect = [mock_info_resp, mock_metrics_resp]

            with patch.object(lc_mod, "_get_client", return_value=mock_client), \
                 patch.object(lc_mod, "_resolve_agent_id", return_value="agent-1"):
                tools = lc_mod.cosmergon_tools(api_key="csg_test", base_url="http://test")

            info = next(t for t in tools if t.__name__ == "cosmergon_info")
            result = info()
            assert '"rules"' in result
            assert '"metrics"' in result

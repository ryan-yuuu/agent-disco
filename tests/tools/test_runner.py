"""Unit tests for ``calfkit-tools`` runner helpers.

Covers only the pure helpers (``_resolve_timeout``, ``_resolve_tool_nodes``).
The full ``_amain`` requires Discord auth, a Kafka broker, and an agents
directory — too heavy for a unit test. Operators will see boot failures
of those in stderr; the contracts worth pinning are the local validation
helpers.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from calfkit_organization.tools import private_chat, runner


class TestResolveTimeout:
    def test_unset_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CALFKIT_TOOLS_TIMEOUT_SECONDS", raising=False)
        assert runner._resolve_timeout() == private_chat.DEFAULT_TIMEOUT_SECONDS

    def test_numeric_env_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFKIT_TOOLS_TIMEOUT_SECONDS", "15.5")
        assert runner._resolve_timeout() == 15.5

    def test_non_numeric_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A typo'd env var must fail boot rather than silently use the
        default — a 60s timeout when the operator typed something different
        would be very confusing."""
        monkeypatch.setenv("CALFKIT_TOOLS_TIMEOUT_SECONDS", "abc")
        with pytest.raises(SystemExit, match="must be a number"):
            runner._resolve_timeout()

    def test_zero_or_negative_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-positive timeout is a misconfiguration — without this
        guard, ``execute_node(timeout=0)`` would either always fail or
        block depending on calfkit's interpretation."""
        monkeypatch.setenv("CALFKIT_TOOLS_TIMEOUT_SECONDS", "0")
        with pytest.raises(SystemExit, match="must be positive"):
            runner._resolve_timeout()


class TestResolveToolNodes:
    def test_returns_nodes_from_populated_registry(self) -> None:
        node = MagicMock()
        result = runner._resolve_tool_nodes({"private_chat": node})
        assert result == [node]

    def test_empty_registry_fails_fast(self) -> None:
        """The empty-registry guard exists specifically to prevent the
        worker from starting in an inert state — subscribed to nothing,
        responding to nothing, but otherwise looking healthy in logs."""
        with pytest.raises(SystemExit, match="empty"):
            runner._resolve_tool_nodes({})

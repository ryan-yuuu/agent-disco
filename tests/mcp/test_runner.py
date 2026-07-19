"""Tests for the ``calfkit-mcp`` per-server runner's guards.

One ``disco run mcp <server>`` process hosts exactly one
:class:`MCPToolbox`. Selection/validation behaviors (unknown name, empty
registry, sibling-secret isolation) live on the loader and are pinned in
``test_config.py``'s ``TestLoadOneServer``; here we pin the runner's own
contract — config failures become a clean ``SystemExit`` before any broker
connection, the provisioned connect matches the other tool-hosting runners,
and the CLI shape.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from calfkit.client import Client
from calfkit.worker import Worker

from calfcord._provisioning import PROVISIONING
from calfcord.mcp import runner
from calfcord.mcp.config import McpConfigError
from calfcord.mcp.runner import _amain, _parse_args


class TestAmainGuards:
    async def test_config_load_failure_exits_cleanly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A config-load failure becomes a clean SystemExit with an actionable
        message — never a raw traceback, and never a broker connection (the
        guard precedes Client.connect)."""

        def _raise(*_a: object, **_k: object):
            raise McpConfigError("boom")

        monkeypatch.setattr("calfcord.mcp.runner.load_one_server", _raise)
        with pytest.raises(SystemExit) as excinfo:
            await _amain("demo")
        message = str(excinfo.value)
        assert "boom" in message and "demo" in message


class TestAmainConnects:
    """The toolbox host is a plain calfkit ``Worker`` like the tools runner:
    it connects the process-wide ``Client`` with calfcord's shared provisioning
    policy and hands the one toolbox to a managed ``Worker``. Agents dispatch
    INTO the toolbox, so it claims NO named reply inbox — a regression that
    re-adds the old ``reply_topic=`` kwarg (removed from ``Client.connect`` in
    the 0.12 migration) would forward it to ``KafkaBroker`` and crash the
    process at connect, before the wrapped MCP server ever starts.
    """

    def test_runner_claims_no_named_reply_inbox(self) -> None:
        """The migration deleted the reply-topic literal; pinning its absence
        catches a copy-paste that reintroduces the old inbox."""
        assert not hasattr(runner, "_REPLY_TOPIC")

    async def test_connects_provisioned_and_builds_worker_over_toolbox(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CALF_HOST_URL", raising=False)
        client = MagicMock(spec=Client)
        toolbox = MagicMock(subscribe_topics=["mcp.demo.dispatch"])
        captured: dict[str, object] = {}

        @asynccontextmanager
        async def _fake_connect(*args, **kwargs):
            captured["connect_args"] = args
            captured["connect_kwargs"] = kwargs
            yield client

        fake_client_cls = MagicMock()
        fake_client_cls.connect = _fake_connect
        monkeypatch.setattr(runner, "Client", fake_client_cls)

        def _make_worker(c, node_list):
            worker = MagicMock(spec=Worker)
            captured["worker"] = worker
            captured["worker_client"] = c
            captured["worker_nodes"] = node_list
            return worker

        monkeypatch.setattr(runner, "Worker", _make_worker)
        monkeypatch.setattr(runner, "load_one_server", lambda _path, _name: toolbox)
        run_mock = AsyncMock()
        monkeypatch.setattr(runner, "run_worker_until_signal", run_mock)

        await _amain("demo")

        # Connect targets the default broker (CALF_HOST_URL unset) with
        # calfcord's shared provisioning policy — and claims NO named reply
        # inbox: agents dispatch into the toolbox, so it owns no reply lane.
        assert captured["connect_args"][0] == "localhost"
        assert captured["connect_kwargs"]["provisioning"] is PROVISIONING
        assert "reply_topic" not in captured["connect_kwargs"]
        assert "inbox_topic" not in captured["connect_kwargs"]

        # A plain Worker over the one resolved toolbox, then run via the shared
        # shutdown helper.
        assert captured["worker_client"] is client
        assert captured["worker_nodes"] == [toolbox]
        run_mock.assert_awaited_once()
        assert run_mock.await_args.args[0] is captured["worker"]


class TestParseArgs:
    def test_server_positional_required(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args([])

    def test_server_positional_parsed(self) -> None:
        assert _parse_args(["github"]).server == "github"

"""Unit tests for ``calfkit-tools`` runner helpers.

Covers the tool-registry guard (``_resolve_tool_nodes``), the
supervisor-restart shutdown contract (``_run_worker``), and the runner's
one A2A-specific responsibility: exposing the process-wide calfkit
``Client`` as a worker-scoped resource so the ``private_chat`` tool body
can reach it via ``ctx.resources``. The Discord connection itself is no
longer the runner's concern — it is built by ``private_chat``'s own
node-scoped ``@resource`` bracket (see ``tests/tools/builtin/
test_private_chat.py::TestA2AResource``), so the per-call response
timeout and audit-channel env resolvers now live with that tool and are
tested there.

The full ``_amain`` requires Discord auth, a Kafka broker, and an agents
directory — too heavy for a unit test; the client-exposure test patches
those boundaries.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from calfkit.client import Client
from calfkit.worker import Worker

from calfcord.tools import runner


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

    def test_empty_registry_message_names_include_filter_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty-registry is most often caused by a typo in
        ``CALFCORD_TOOLS_INCLUDE`` (per-tool images). The SystemExit
        message must NAME the env var and surface its value so the
        operator can short-circuit a ``why is my registry empty`` hunt.
        A regression that strips the env-var attribution would pass
        the broader ``match='empty'`` guard above but silently revert
        the cleanup's UX improvement — this test pins it."""
        monkeypatch.setenv("CALFCORD_TOOLS_INCLUDE", "definitely_not_a_real_tool")
        with pytest.raises(SystemExit) as exc_info:
            runner._resolve_tool_nodes({})
        message = str(exc_info.value)
        assert "CALFCORD_TOOLS_INCLUDE=" in message
        assert "definitely_not_a_real_tool" in message

    def test_empty_registry_message_handles_unset_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ``CALFCORD_TOOLS_INCLUDE`` is unset, the message must
        still surface the env var with an explicit ``<unset>`` marker
        rather than an ambiguous empty string — a future log-aggregation
        regex that anchors on ``CALFCORD_TOOLS_INCLUDE=\\S+`` would
        otherwise miss the unset case silently."""
        monkeypatch.delenv("CALFCORD_TOOLS_INCLUDE", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            runner._resolve_tool_nodes({})
        message = str(exc_info.value)
        assert "CALFCORD_TOOLS_INCLUDE=<unset>" in message


class TestAmainExposesClientResource:
    """The runner's sole A2A wiring responsibility after the 0.6.0 migration:
    expose the process-wide calfkit ``Client`` as the worker-scoped
    ``a2a_client`` resource. ``private_chat``'s body reads it from
    ``ctx.resources`` (merged under its own node-scoped Discord bundle), so a
    regression that dropped this line would break A2A at runtime — pin it.
    Discord is built by the tool's own ``@resource`` bracket, so the runner
    constructs none here.
    """

    async def test_connected_client_is_registered_as_worker_resource(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = MagicMock(spec=Client)

        @asynccontextmanager
        async def _fake_connect(*args, **kwargs):
            yield client

        fake_client_cls = MagicMock()
        fake_client_cls.connect = _fake_connect
        monkeypatch.setattr(runner, "Client", fake_client_cls)

        captured: dict[str, Worker] = {}

        def _make_worker(c, nodes):
            worker = MagicMock(spec=Worker)
            worker.resources = {}
            captured["worker"] = worker
            return worker

        monkeypatch.setattr(runner, "Worker", _make_worker)
        monkeypatch.setattr(runner, "_resolve_tool_nodes", lambda registry: [MagicMock()])
        monkeypatch.setattr(runner, "_run_worker", AsyncMock())

        await runner._amain()

        assert captured["worker"].resources["a2a_client"] is client


class TestRunWorkerShutdownContract:
    """The supervisor-restart invariant: any non-signal exit from the
    worker must raise out of ``_run_worker`` so the process exits
    non-zero. Pins both the crash path and the unexpected-clean-return
    path; without one of these tests a future refactor that re-swallowed
    either case would not surface."""

    async def test_worker_crash_propagates(self) -> None:
        """An exception inside ``worker.run()`` must escape ``_run_worker``
        so the surrounding ``asyncio.run`` exits non-zero."""
        crash = ValueError("simulated kafka drop")
        worker = MagicMock(spec=Worker)
        worker.run = AsyncMock(side_effect=crash)
        with pytest.raises(ValueError, match="simulated kafka drop"):
            await runner._run_worker(worker)

    async def test_worker_unexpected_clean_return_raises(self) -> None:
        """A clean ``worker.run()`` return without a shutdown signal is
        unexpected — must synthesize a RuntimeError so supervisors
        configured for ``Restart=on-failure`` restart us."""
        worker = MagicMock(spec=Worker)

        async def returns_immediately() -> None:
            return None

        worker.run = AsyncMock(side_effect=returns_immediately)
        with pytest.raises(RuntimeError, match="returned unexpectedly"):
            await runner._run_worker(worker)

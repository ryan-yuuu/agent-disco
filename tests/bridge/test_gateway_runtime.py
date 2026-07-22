"""Combined lifecycle tests for the Discord gateway and co-located Worker."""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path

import pytest

from calfcord.bridge import gateway as gateway_module


class _FakeLoop:
    def __init__(self) -> None:
        self.handlers: dict[signal.Signals, object] = {}

    def add_signal_handler(self, sig, callback) -> None:
        self.handlers[sig] = callback

    def remove_signal_handler(self, sig) -> bool:
        self.handlers.pop(sig, None)
        return True


class _Gateway:
    connected = True
    bot_identity = "bot (1)"

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self._forever = asyncio.Event()

    async def start(self) -> None:
        self.calls.append("gateway.start")
        await self._forever.wait()

    async def wait_until_ready(self) -> None:
        self.calls.append("gateway.ready")

    def quiesce(self) -> None:
        self.calls.append("gateway.quiesce")

    async def drain_inflight(self) -> None:
        self.calls.append("gateway.drain")

    async def close(self) -> None:
        self.calls.append("gateway.close")


class _Worker:
    def __init__(self, calls: list[str], loop: _FakeLoop, *, error: Exception | None = None) -> None:
        self.calls = calls
        self.loop = loop
        self.error = error

    async def start(self) -> None:
        self.calls.append("worker.start")
        if self.error is not None:
            raise self.error
        self.loop.handlers[signal.SIGTERM]()

    async def stop(self) -> None:
        self.calls.append("worker.stop")


async def _never_refresher(*args, **kwargs) -> None:
    await asyncio.Event().wait()


async def test_combined_runtime_orders_dependency_safe_shutdown(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []
    loop = _FakeLoop()
    gateway = _Gateway(calls)
    worker = _Worker(calls, loop)
    monkeypatch.setattr(gateway_module.asyncio, "get_running_loop", lambda: loop)
    monkeypatch.setattr(gateway_module, "run_refresher", _never_refresher)

    await gateway_module._run_bridge_runtime(  # type: ignore[arg-type]
        gateway, worker, health_home=tmp_path, worker_is_healthy=lambda: True
    )

    assert calls.index("gateway.ready") < calls.index("worker.start")
    assert calls.index("gateway.quiesce") < calls.index("gateway.drain")
    assert calls.index("gateway.drain") < calls.index("worker.stop")
    assert calls.index("worker.stop") < calls.index("gateway.close")


async def test_worker_start_failure_closes_gateway_and_propagates(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []
    loop = _FakeLoop()
    gateway = _Gateway(calls)
    worker = _Worker(calls, loop, error=RuntimeError("provision failed"))
    monkeypatch.setattr(gateway_module.asyncio, "get_running_loop", lambda: loop)
    monkeypatch.setattr(gateway_module, "run_refresher", _never_refresher)

    with pytest.raises(RuntimeError, match="provision failed"):
        await gateway_module._run_bridge_runtime(  # type: ignore[arg-type]
            gateway, worker, health_home=tmp_path, worker_is_healthy=lambda: True
        )

    assert "worker.stop" not in calls
    assert calls[-1] == "gateway.close"

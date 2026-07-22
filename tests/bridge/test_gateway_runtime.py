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
        self.accepting = False

    async def start(self) -> None:
        self.calls.append("gateway.start")
        await self._forever.wait()

    async def wait_until_ready(self) -> None:
        self.calls.append("gateway.ready")

    def quiesce(self) -> None:
        self.calls.append("gateway.quiesce")
        self.accepting = False

    def start_accepting_messages(self) -> None:
        self.calls.append("gateway.accept")
        self.accepting = True

    async def drain_ingress(self) -> None:
        self.calls.append("gateway.drain_ingress")

    async def drain_inflight(self) -> None:
        self.calls.append("gateway.drain")

    async def close(self) -> None:
        self.calls.append("gateway.close")


class _Worker:
    def __init__(
        self,
        calls: list[str],
        loop: _FakeLoop,
        *,
        error: Exception | None = None,
        start_gate: asyncio.Event | None = None,
    ) -> None:
        self.calls = calls
        self.loop = loop
        self.error = error
        self.start_gate = start_gate
        self.start_entered = asyncio.Event()

    async def start(self) -> None:
        self.calls.append("worker.start")
        self.start_entered.set()
        if self.start_gate is not None:
            await self.start_gate.wait()
        if self.error is not None:
            raise self.error

    async def stop(self) -> None:
        self.calls.append("worker.stop")


async def _never_refresher(*args, **kwargs) -> None:
    await asyncio.Event().wait()


async def _wait_for_call(calls: list[str], expected: str) -> None:
    for _ in range(20):
        if expected in calls:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"{expected!r} was not called; calls={calls!r}")


async def test_combined_runtime_orders_dependency_safe_shutdown(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []
    loop = _FakeLoop()
    gateway = _Gateway(calls)
    worker = _Worker(calls, loop)
    monkeypatch.setattr(gateway_module.asyncio, "get_running_loop", lambda: loop)
    monkeypatch.setattr(gateway_module, "run_refresher", _never_refresher)

    runtime = asyncio.create_task(
        gateway_module._run_bridge_runtime(  # type: ignore[arg-type]
            gateway, worker, health_home=tmp_path, worker_is_healthy=lambda: True
        )
    )
    await _wait_for_call(calls, "gateway.accept")
    loop.handlers[signal.SIGTERM]()
    await runtime

    assert calls.index("gateway.ready") < calls.index("worker.start")
    assert calls.index("worker.start") < calls.index("gateway.accept")
    assert calls.index("gateway.quiesce") < calls.index("gateway.drain_ingress")
    assert calls.index("gateway.drain_ingress") < calls.index("gateway.drain")
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


async def test_shutdown_during_worker_start_never_opens_ingress(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []
    loop = _FakeLoop()
    start_gate = asyncio.Event()
    gateway = _Gateway(calls)
    worker = _Worker(calls, loop, start_gate=start_gate)
    monkeypatch.setattr(gateway_module.asyncio, "get_running_loop", lambda: loop)
    monkeypatch.setattr(gateway_module, "run_refresher", _never_refresher)

    runtime = asyncio.create_task(
        gateway_module._run_bridge_runtime(  # type: ignore[arg-type]
            gateway, worker, health_home=tmp_path, worker_is_healthy=lambda: True
        )
    )
    await worker.start_entered.wait()
    loop.handlers[signal.SIGTERM]()
    start_gate.set()
    await runtime

    assert "gateway.accept" not in calls
    assert gateway.accepting is False
    assert calls.count("worker.stop") == 1


async def test_gateway_exit_tied_with_worker_start_never_opens_ingress(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []
    loop = _FakeLoop()
    start_gate = asyncio.Event()
    gateway = _Gateway(calls)
    worker = _Worker(calls, loop, start_gate=start_gate)
    monkeypatch.setattr(gateway_module.asyncio, "get_running_loop", lambda: loop)
    monkeypatch.setattr(gateway_module, "run_refresher", _never_refresher)

    runtime = asyncio.create_task(
        gateway_module._run_bridge_runtime(  # type: ignore[arg-type]
            gateway, worker, health_home=tmp_path, worker_is_healthy=lambda: True
        )
    )
    await worker.start_entered.wait()
    # Complete both tasks before the runtime resumes from FIRST_COMPLETED.
    gateway._forever.set()
    start_gate.set()

    with pytest.raises(RuntimeError, match="Discord gateway returned unexpectedly"):
        await runtime

    assert "gateway.accept" not in calls
    assert gateway.accepting is False
    assert calls.count("worker.stop") == 1


async def test_heartbeat_starts_only_after_worker_and_admission(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []
    loop = _FakeLoop()
    start_gate = asyncio.Event()
    gateway = _Gateway(calls)
    worker = _Worker(calls, loop, start_gate=start_gate)
    heartbeat_started = asyncio.Event()

    async def _record_refresher(*args, **kwargs) -> None:
        calls.append("heartbeat.start")
        heartbeat_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(gateway_module.asyncio, "get_running_loop", lambda: loop)
    monkeypatch.setattr(gateway_module, "run_refresher", _record_refresher)
    runtime = asyncio.create_task(
        gateway_module._run_bridge_runtime(  # type: ignore[arg-type]
            gateway, worker, health_home=tmp_path, worker_is_healthy=lambda: True
        )
    )

    await worker.start_entered.wait()
    await asyncio.sleep(0)
    assert "gateway.accept" not in calls
    assert not heartbeat_started.is_set()

    start_gate.set()
    await heartbeat_started.wait()
    assert calls.index("worker.start") < calls.index("gateway.accept")
    assert calls.index("gateway.accept") < calls.index("heartbeat.start")
    loop.handlers[signal.SIGTERM]()
    await runtime

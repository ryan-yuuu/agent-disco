"""Unit tests for AgentRuntimeState schema and AgentStateStore atomic IO."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from calfkit_organization.agents.state import AgentRuntimeState, AgentStateStore


class TestAgentRuntimeState:
    def test_default_schema_version_is_1(self) -> None:
        s = AgentRuntimeState()
        assert s.schema_version == 1
        assert s.channels == []
        assert s.thinking_effort is None

    def test_extra_fields_ignored(self) -> None:
        """Forward-compat: unknown fields from newer writers must not crash older readers."""
        s = AgentRuntimeState.model_validate({"channels": [42], "future_field": "ignored"})
        assert s.channels == [42]

    def test_thinking_effort_accepts_known_tiers(self) -> None:
        for tier in ("none", "low", "medium", "high", "xhigh", "max"):
            s = AgentRuntimeState.model_validate({"channels": [1], "thinking_effort": tier})
            assert s.thinking_effort == tier

    def test_thinking_effort_rejects_unknown_value(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            AgentRuntimeState.model_validate({"channels": [1], "thinking_effort": "bananas"})

    def test_old_state_files_without_thinking_effort_load_clean(self) -> None:
        """Back-compat: a pre-feature state file must load with effort=None."""
        s = AgentRuntimeState.model_validate({"schema_version": 1, "channels": [42]})
        assert s.thinking_effort is None


class TestAgentStateStore:
    @pytest.fixture
    def store(self, tmp_path: Path) -> AgentStateStore:
        return AgentStateStore(tmp_path / "agents" / "scheduler.json")

    async def test_load_missing_file_raises(self, store: AgentStateStore) -> None:
        with pytest.raises(FileNotFoundError):
            await store.load()

    async def test_save_then_load_roundtrip(self, store: AgentStateStore) -> None:
        await store.save(AgentRuntimeState(channels=[111, 222]))
        loaded = await store.load()
        assert loaded.channels == [111, 222]

    async def test_save_creates_parent_directory(self, tmp_path: Path) -> None:
        nested = tmp_path / "deeply" / "nested" / "agent.json"
        store = AgentStateStore(nested)
        await store.save(AgentRuntimeState(channels=[1]))
        assert nested.exists()
        assert nested.parent.is_dir()

    async def test_save_is_atomic(self, store: AgentStateStore) -> None:
        """After save, only the final file remains — no .tmp leftover."""
        await store.save(AgentRuntimeState(channels=[42]))
        tmp_files = list(store.path.parent.glob(".*.tmp"))
        assert tmp_files == []

    async def test_save_overwrites_existing(self, store: AgentStateStore) -> None:
        await store.save(AgentRuntimeState(channels=[1, 2]))
        await store.save(AgentRuntimeState(channels=[3]))
        loaded = await store.load()
        assert loaded.channels == [3]

    async def test_add_channel_appends(self, store: AgentStateStore) -> None:
        await store.save(AgentRuntimeState(channels=[1]))
        await store.add_channel(2)
        loaded = await store.load()
        assert loaded.channels == [1, 2]

    async def test_add_channel_is_idempotent(self, store: AgentStateStore) -> None:
        await store.save(AgentRuntimeState(channels=[1, 2]))
        await store.add_channel(1)
        loaded = await store.load()
        assert loaded.channels == [1, 2]

    async def test_remove_channel(self, store: AgentStateStore) -> None:
        await store.save(AgentRuntimeState(channels=[1, 2, 3]))
        await store.remove_channel(2)
        loaded = await store.load()
        assert loaded.channels == [1, 3]

    async def test_remove_channel_missing_is_noop(self, store: AgentStateStore) -> None:
        await store.save(AgentRuntimeState(channels=[1]))
        await store.remove_channel(999)
        loaded = await store.load()
        assert loaded.channels == [1]

    async def test_set_thinking_effort_persists(self, store: AgentStateStore) -> None:
        await store.save(AgentRuntimeState(channels=[1]))
        await store.set_thinking_effort("high")
        loaded = await store.load()
        assert loaded.thinking_effort == "high"
        assert loaded.channels == [1]  # other fields preserved

    async def test_set_thinking_effort_can_overwrite(self, store: AgentStateStore) -> None:
        await store.save(AgentRuntimeState(channels=[1], thinking_effort="low"))
        await store.set_thinking_effort("xhigh")
        loaded = await store.load()
        assert loaded.thinking_effort == "xhigh"

    async def test_set_thinking_effort_idempotent_skips_write(
        self, store: AgentStateStore
    ) -> None:
        """No-op write when value is unchanged so file mtime stays stable."""
        await store.save(AgentRuntimeState(channels=[1], thinking_effort="medium"))
        mtime_before = store.path.stat().st_mtime_ns
        await store.set_thinking_effort("medium")
        mtime_after = store.path.stat().st_mtime_ns
        assert mtime_before == mtime_after

    async def test_set_thinking_effort_missing_file_raises(
        self, store: AgentStateStore
    ) -> None:
        """No bootstrap: an agent must have been seeded before effort can be set."""
        with pytest.raises(FileNotFoundError):
            await store.set_thinking_effort("low")

    async def test_concurrent_add_channels_no_lost_writes(self, store: AgentStateStore) -> None:
        """All scheduled add_channel calls land in the persisted state.

        Note: asyncio is single-threaded, so this primarily verifies
        functional correctness of the read-modify-write rather than truly
        stressing the asyncio.Lock — there is no real I/O await mid-RMW
        for the lock to serialize against. Genuine lock stress would
        require patched I/O delays or thread-pool dispatch.
        """
        await store.save(AgentRuntimeState(channels=[]))
        await asyncio.gather(*(store.add_channel(i) for i in range(20)))
        loaded = await store.load()
        assert sorted(loaded.channels) == list(range(20))

    async def test_written_json_is_valid_and_multi_line(self, store: AgentStateStore) -> None:
        """The on-disk file is human-inspectable (multi-line, valid JSON)."""
        await store.save(AgentRuntimeState(channels=[42]))
        raw = store.path.read_text(encoding="utf-8")
        data = json.loads(raw)
        assert data["channels"] == [42]
        # Multi-line (not single-line compact JSON) so operators can read/diff by hand.
        assert "\n" in raw

    def test_construction_sweeps_orphan_tmp_files(self, tmp_path: Path) -> None:
        """Leftover ``.tmp`` from a previously crashed write is removed on construction."""
        target = tmp_path / "agents" / "scheduler.json"
        target.parent.mkdir(parents=True)
        orphan = target.parent / f".{target.name}.deadbeef.tmp"
        orphan.write_text("partial payload from a crash\n")
        assert orphan.exists()
        AgentStateStore(target)
        assert not orphan.exists(), "orphan .tmp should be swept on construction"

    def test_construction_does_not_sweep_other_agents_tmp_files(self, tmp_path: Path) -> None:
        """The sweep is scoped to *this* agent's tmp pattern — other agents survive."""
        target = tmp_path / "agents" / "scheduler.json"
        target.parent.mkdir(parents=True)
        other_orphan = target.parent / ".finance.json.xyz.tmp"
        other_orphan.write_text("not ours")
        AgentStateStore(target)
        assert other_orphan.exists()

    def test_construction_on_missing_parent_dir_is_noop(self, tmp_path: Path) -> None:
        """Construction must not fail when the parent directory does not yet exist."""
        target = tmp_path / "never" / "made" / "scheduler.json"
        AgentStateStore(target)  # must not raise

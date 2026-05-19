"""Per-agent runtime state — channel subscriptions plus future fields.

Each agent runs as its own process and owns one JSON file at
``state/agents/<agent_id>.json`` (or wherever :envvar:`CALFKIT_STATE_DIR`
points). The state file is read at boot, mutated in memory during runtime,
and persisted with a crash-safe rename pattern:

1. write payload to a sibling ``.<name>.<rand>.tmp`` file
2. ``fsync`` the file
3. ``os.replace`` over the destination (atomic rename)
4. on POSIX, ``fsync`` the parent directory so the rename is durable

A crash between steps 2 and 3 leaves the destination intact and an orphan
``.tmp`` file behind, which is swept on next :class:`AgentStateStore`
construction. Step 4 is skipped on platforms where opening a directory
descriptor isn't supported (notably Windows).

The schema is versioned via ``schema_version`` so unknown fields from
newer writers do not crash older readers (``extra="ignore"`` on the model).

Concurrency: writes serialize through an :class:`asyncio.Lock` so
concurrent in-process coroutines cannot interleave saves. Per-agent file
ownership eliminates the cross-process race; one agent = one process =
one file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


ThinkingEffort = Literal["none", "low", "medium", "high", "xhigh", "max"]
"""Operator-facing thinking-effort tiers.

Six abstract levels mapped to provider-specific reasoning/thinking
parameters in :mod:`calfkit_organization.bridge.thinking`. Tier names
parallel Claude Code's effort vocabulary; ``xhigh`` is a calfkit-specific
step between ``high`` and ``max``.
"""


class AgentRuntimeState(BaseModel):
    """Persisted runtime state for one agent.

    Add new fields freely; ``extra="ignore"`` keeps older readers compatible
    with files written by newer code. Bump ``schema_version`` only on
    breaking changes (field removal or rename) and pair with a migrator.
    """

    model_config = ConfigDict(extra="ignore")

    schema_version: int = 1
    channels: list[int] = Field(default_factory=list)
    thinking_effort: ThinkingEffort | None = None


class AgentStateStore:
    """Atomic, in-process serialized read/write for one agent's state file.

    Sweeps any leftover ``.tmp`` files from a previous crashed write on
    construction so they do not accumulate across restarts.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._sweep_orphan_tmp_files()

    @property
    def path(self) -> Path:
        return self._path

    async def load(self) -> AgentRuntimeState:
        """Read the state file.

        Raises :class:`FileNotFoundError` if absent — callers use that as
        the signal to enter the bootstrap path. The store does not auto-
        create with defaults because a fresh agent needs explicit channel
        seeding (it cannot subscribe to a non-existent set).
        """
        async with self._lock:
            return self._read()

    async def save(self, state: AgentRuntimeState) -> None:
        """Atomically write ``state`` to disk.

        See module docstring for the full sequence and the crash-safety
        guarantees it provides.
        """
        async with self._lock:
            self._write(state)

    async def add_channel(self, channel_id: int) -> None:
        """Add ``channel_id`` to the persisted channel set. No-op if present."""
        async with self._lock:
            state = self._read()
            if channel_id in state.channels:
                return
            state = state.model_copy(update={"channels": [*state.channels, channel_id]})
            self._write(state)

    async def remove_channel(self, channel_id: int) -> None:
        """Remove ``channel_id`` from the persisted channel set. No-op if absent."""
        async with self._lock:
            state = self._read()
            if channel_id not in state.channels:
                return
            state = state.model_copy(update={"channels": [c for c in state.channels if c != channel_id]})
            self._write(state)

    async def set_thinking_effort(self, value: ThinkingEffort) -> None:
        """Persist a new ``thinking_effort`` tier. No-op when the value is unchanged.

        The no-op-on-same-value path avoids a spurious write (and its
        fsync) on a re-apply; useful for any future mtime-watching consumer.
        """
        async with self._lock:
            state = self._read()
            if state.thinking_effort == value:
                return
            state = state.model_copy(update={"thinking_effort": value})
            self._write(state)

    def _read(self) -> AgentRuntimeState:
        with self._path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return AgentRuntimeState.model_validate(data)

    def _write(self, state: AgentRuntimeState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = state.model_dump_json(indent=2)
        fd, tmp_name = tempfile.mkstemp(
            dir=self._path.parent,
            prefix=f".{self._path.name}.",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                tmp.write(payload)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, self._path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        # POSIX: fsync the parent directory so the rename is durable across
        # a power loss. On Windows opening a directory for fsync is not
        # supported, so skip — there the rename's durability is whatever
        # the filesystem provides.
        if os.name == "posix":
            dir_fd = os.open(self._path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

        logger.debug("persisted state to %s", self._path)

    def _sweep_orphan_tmp_files(self) -> None:
        """Remove ``.tmp`` files left behind by a previously crashed write.

        Called once at construction. Safe because each agent owns its own
        state file in its own process; no other writer can be mid-write
        when this runs.
        """
        parent = self._path.parent
        if not parent.exists():
            return
        for orphan in parent.glob(f".{self._path.name}.*.tmp"):
            try:
                orphan.unlink()
                logger.warning("removed orphan state-write tempfile: %s", orphan)
            except OSError:
                logger.warning("failed to remove orphan tempfile: %s", orphan, exc_info=True)

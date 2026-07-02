"""``disco logs [component] [-f]`` — tail the workspace's per-process logs.

Every component logs to ``$CALFCORD_HOME/state/logs/<name>.log``, but two
different writers fill that directory: Process Compose captures the SUBSTRATE
processes (``broker``/``bridge`` ``log_location``, plus its own supervisor log,
``process-compose.log``), while :mod:`calfcord.supervisor.procspawn` appends each
detached ROSTER slot's output (agents, ``tools``, ``mcp-<server>``) at spawn.
This command reads those files straight off disk — a path that works even when
the supervisor REST daemon is down (the file is the durable record), so "what
did the broker say before it died?" is always answerable.

Why read files rather than the REST log endpoint: the on-disk path is
supervisor-independent and host-local with no port to derive or daemon to be up;
it is the lowest-coupling way to surface "what happened here." The substrate's
live logs are *also* available over REST (``GET /process/logs/...``), but that
path only covers PC-supervised processes and only while the daemon answers, so
it is left to a future need.

The set of names that *may* have a log is not hardcoded here — it is
reconstructed from the same seams the writers use: the reserved substrate +
``tools`` names (:data:`calfcord.supervisor.compose._RESERVED_PROCESS_NAMES`),
the host's agent ids (:func:`calfcord.cli._agents.detect_agents` over
``agents/*.md``), the ``mcp-<server>`` slots from mcp.json, and the supervisor's
own ``process-compose`` log.

This module imports only ``_agents.detect_agents``, the mcp.json name reader,
and the ``compose`` log-path / name-set seams, all of which are import-light.

The native-install guard (a dev run has no ``$CALFCORD_HOME`` and therefore no
state/logs dir) lives in the ``main.py`` veneer, alongside every other
supervisor-scoped verb's identical guard — this module is handed a concrete
``home`` and concerns itself only with the files under it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from calfcord.cli._agents import detect_agents
from calfcord.mcp.config import McpConfigError, list_server_names, resolve_config_path
from calfcord.supervisor.compose import (
    _RESERVED_PROCESS_NAMES,
    SUPERVISOR_LOG_STEM,
    _log_location,
    mcp_slot_name,
)

# The supervisor's own log (``process-compose up -L ...``) sits beside the
# per-process logs and is a legitimate, useful tail target (it captures
# supervisor-level start/restart events), so it is part of the known set even
# though it is not a process the generator declares. Its name is the shared stem
# ``compose`` owns so it can never drift from the filename ``lifecycle`` writes.
_SUPERVISOR_LOG_NAME = SUPERVISOR_LOG_STEM

# How long the follow loop waits between polls for appended bytes. Small enough
# to feel live, large enough that the loop never busy-spins a CPU. Injectable via
# the ``sleep`` parameter so tests drive (and bound) the loop with no real wait.
_FOLLOW_POLL_INTERVAL_SECONDS = 0.5


def _known_names(agents_dir: Path) -> tuple[list[str], list[str]]:
    """The component names that may have a log, plus any degradation notes.

    The name set is the union of what the two log WRITERS produce: the reserved
    substrate + ``tools`` slots, the host's agent ids (``agents/*.md``), the
    ``mcp-<server>`` slots from mcp.json, and the supervisor's own log. Order is
    deterministic (substrate, tools, agents, mcp, supervisor) so the merged
    "all logs" view and the unknown-name hint read predictably.

    The second element carries operator-facing notes for any part of the set
    that could not be enumerated (today: an unreadable mcp.json) — logs stays
    usable, but the omission is said out loud, never silent.
    """
    notes: list[str] = []
    # ``_RESERVED_PROCESS_NAMES`` is a frozenset; pin a stable, readable order
    # (substrate then the fixed components) rather than its hash order.
    ordered_reserved = [name for name in ("broker", "bridge", "tools") if name in _RESERVED_PROCESS_NAMES]
    # MCP slots come from the same no-secrets mcp.json seam the spawn verbs use.
    # Tolerant on purpose: a broken mcp.json must not take the logs command down
    # with it ("always show what the broker said before it died") — the strict
    # readers (start, mcp start) surface the config error; here it is a note.
    try:
        mcp_slots = [mcp_slot_name(s) for s in list_server_names(resolve_config_path())]
    except McpConfigError:
        mcp_slots = []
        notes.append("note: mcp.json unreadable; MCP logs omitted.")
    names = [
        *ordered_reserved,
        *detect_agents(agents_dir),
        *mcp_slots,
        _SUPERVISOR_LOG_NAME,
    ]
    return names, notes


def _emit_file(path: Path, *, label: str | None, out: Callable[..., None]) -> None:
    """Print a log file's current contents, optionally line-prefixed.

    ``label`` is the component name in the merged view (each line becomes
    ``<name> | <line>`` so a reader can tell who said what); ``None`` for a single
    explicit component streams the raw lines. Trailing-newline-only lines are
    dropped so an empty tail does not print blank rows. This is the one-shot dump
    only; the follow path tracks its own per-file byte offsets, so there is no
    offset to hand back here.

    Decoding is tolerant (``errors="replace"``) to match the follow path: a log
    line with non-UTF-8 bytes (a partial multibyte write, a binary splat, a
    mis-encoded child) must never raise an uncaught ``UnicodeDecodeError`` and
    crash the command — "always show what the broker said before it died" holds
    even when what it said was not clean UTF-8.
    """
    data = path.read_text(encoding="utf-8", errors="replace")
    for line in data.splitlines():
        out(f"{label} | {line}" if label is not None else line)


def _follow(
    targets: list[tuple[str, Path]],
    *,
    labeled: bool,
    out: Callable[..., None],
    sleep: Callable[[float], None],
    poll_interval: float,
) -> int:
    """Stream existing content then poll each target for *appended* bytes.

    ``targets`` are ``(name, path)`` pairs. A path that does not exist yet is
    tolerated (a slot may clock in moments later): it is simply skipped this pass
    and its offset stays at 0, so when the file appears its full content is emitted
    on the next poll. Each pass reads only the bytes past the per-file offset, so a
    long-lived process never re-prints history.

    The loop runs until ``KeyboardInterrupt`` (a real Ctrl-C, or the injected
    sleep in tests), then returns 0 — a clean, expected exit, not a failure. The
    bounded ``sleep`` between passes keeps it from busy-spinning.
    """
    label_for = (lambda name: name) if labeled else (lambda _name: None)
    offsets: dict[str, int] = dict.fromkeys((name for name, _ in targets), 0)

    try:
        while True:
            for name, path in targets:
                if not path.is_file():
                    continue
                try:
                    data = path.read_bytes()
                except OSError:
                    # The is_file()/read window is racy: rotation or a cleanup can
                    # delete the file in between. Skip this pass (offset untouched
                    # — a reappearing file streams from where we left off, or from
                    # 0 via the shrink reset below) rather than crash the stream.
                    continue
                if len(data) < offsets[name]:
                    # The file SHRANK: rotate-at-spawn moved it aside and a
                    # restarted slot is writing a fresh file. Restart from byte 0
                    # so the fresh file's first lines stream (an offset left at
                    # the old size would silently skip them).
                    offsets[name] = 0
                if len(data) == offsets[name]:
                    continue
                fresh = data[offsets[name] :].decode("utf-8", errors="replace")
                offsets[name] = len(data)
                label = label_for(name)
                for line in fresh.splitlines():
                    out(f"{label} | {line}" if label is not None else line)
            sleep(poll_interval)
    except KeyboardInterrupt:
        return 0


def tail(
    home: Path,
    *,
    agents_dir: Path,
    component: str | None = None,
    follow: bool = False,
    out: Callable[..., None] = print,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval: float = _FOLLOW_POLL_INTERVAL_SECONDS,
) -> int:
    """Tail unified or per-component supervisor logs under ``home``.

    ``component`` ``None`` streams every component that has a log (each line
    labeled with its name); a name streams just that file unlabeled. ``follow``
    keeps streaming appended bytes until Ctrl-C. ``out`` and ``sleep`` are
    injected so the follow loop is testable without real stdout or real time.

    Returns a POSIX exit code:

    * **1** — the ``state/logs`` dir does not exist (the workspace was never
      started), or ``component`` is not a known name (a typo). Both print a clean,
      actionable ``error:`` rather than raising — a missing workspace or a typo is
      operator input, not an infrastructure bug.
    * **0** — otherwise, including the benign "this slot has no log yet" case (a
      known component that never clocked in), which is informational, not an
      error.
    """
    log_dir = home / "state" / "logs"
    if not log_dir.is_dir():
        out(f"error: no logs under {log_dir} — the workspace may not be running (start it with: disco start).")
        return 1

    names, notes = _known_names(agents_dir)
    for note in notes:
        out(note)

    if component is not None and component not in names:
        out(f"error: unknown component {component!r}; choose one of: {', '.join(names)} (or omit it to tail all).")
        return 1

    selected = [component] if component is not None else names
    labeled = component is None  # the merged view labels; a single component does not

    if follow:
        targets = [(name, Path(_log_location(str(home), name))) for name in selected]
        return _follow(targets, labeled=labeled, out=out, sleep=sleep, poll_interval=poll_interval)

    # One-shot: dump each selected file's current contents. An explicitly named
    # component with no file yet is informational (the slot never ran); in the
    # merged view an absent file is simply skipped so the output shows only what
    # exists.
    emitted_any = False
    for name in selected:
        path = Path(_log_location(str(home), name))
        if not path.is_file():
            if component is not None:
                out(f"no logs yet for {name} (it may not have started).")
            continue
        _emit_file(path, label=name if labeled else None, out=out)
        emitted_any = True

    if labeled and not emitted_any:
        # The dir exists but nothing has produced a log yet — tell the operator
        # plainly rather than printing nothing and looking broken.
        out("no logs yet (no component has produced output).")

    return 0

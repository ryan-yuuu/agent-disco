"""The step trace's row model — pure values and their rendering (ADR-0024).

A **step trace** is the visible record of one turn's intermediate events; a
**segment** is one Discord message of it; a **row** is one line. This module owns
rows: the value types, and the one function that turns a row into a line.

Everything here is pure — no Discord, no clock, no mutable module state — so the
row state machine is testable without any of them. The stateful fold (segments,
the row index, the writer task) lives in the trace renderer, which stamps
timings and hands them to rows as data.

**The dim register.** A resting row is prefixed ``-# ``, Discord's subtext:
smaller and dim grey. Since Discord offers no per-line colour, *escaping* that
prefix IS the attention mechanism — a row that needs the reader jumps to full
brightness. This mirrors opencode's colour cascade, where a completed row fades
to muted and only failure, permission, or hover pulls it back.

**The glyph register** (settled by probing the live API):

* text glyph + ``-# `` → routine, finished — ``●``, ``⊘``, the seal
* text glyph, bright → in flight, or structural — ``◐``, ``➜``
* **emoji**, bright → needs you — ``❌``, ``⚠️``

An emoji renders at ~1.4x and in full colour; a text glyph renders inline and
inherits the line's dimming. Emoji are therefore *rationed* to the two attention
states: they are the only way to get red onto a Discord line, and a failure is
what that should be spent on.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, assert_never

# --- text hygiene -----------------------------------------------------------

_DETAIL_MAX: Final[int] = 120
"""Cap for any single piece of tool-derived text in a row. Bounds the row's
render, which is what makes the trace renderer's growth reservation sound: a
segment that has already been posted cannot be re-split, so a row's maximum
size must be known when it is appended."""

_MD_ESCAPE: Final[re.Pattern[str]] = re.compile(r"([\\`*_~|])")
"""Discord inline formatting that would break out of a row. The backslash is
FIRST in the class and escaped like the rest, so a literal ``\\`` in tool output
cannot neutralise the escape of the character after it."""

_ROW_GROWTH_RESERVE: Final[int] = 140
"""The most any row can grow when it resolves — the budget the trace renderer
reserves per *pending* row, released as rows resolve.

A posted segment cannot be re-split, so a resolve that overflows it would be
rejected by Discord (400), swallowed to a WARNING, and leave the segment dirty
forever. The worst case is a denied row: it gains ``-# ~~``/``~~`` around the
head plus a full ``_DETAIL_MAX`` note. Pinned by ``TestGrowthReserve`` rather
than left to inspection — this constant and :func:`render_row` must move
together."""


def _plain(text: str, limit: int = _DETAIL_MAX) -> str:
    """Flatten, escape, and bound arbitrary text for use inside one row.

    A row is a single line; tool output is not. A newline that survived into a
    row would break out of the per-line ``-# `` prefix and render the remainder
    at full brightness — so flattening is a correctness requirement, not a
    cosmetic one. ``str.split()`` with no argument collapses every whitespace
    run (newlines and tabs included) in one pass.

    Truncation happens *after* escaping so the returned length is genuinely
    bounded; cutting mid-escape would strand a lone backslash that then escapes
    the ellipsis, so an odd trailing run is trimmed back.
    """
    flat = " ".join(text.split())
    escaped = _MD_ESCAPE.sub(r"\\\1", flat)
    if len(escaped) <= limit:
        return escaped
    cut = escaped[: limit - 1]
    if (len(cut) - len(cut.rstrip("\\"))) % 2:
        cut = cut[:-1]
    return cut.rstrip() + "…"


# --- argument summarisation -------------------------------------------------

_SUBJECT_KEYS: Final[tuple[str, ...]] = (
    "path",
    "file_path",
    "filepath",
    "file",
    "url",
    "query",
    "pattern",
    "command",
    "cmd",
    "name",
)
"""Argument names worth promoting to prose, in precedence order — the thing a
tool is acting *on*. opencode hand-picks this per tool from a closed allowlist;
our tools are arbitrary MCP names, so this is a convention with a graceful
fallback (no match → no subject, everything brackets) rather than a guess that
can be wrong in a damaging way."""

_SCALARS: Final[tuple[type, ...]] = (str, int, float, bool)
"""Values a row may render. Everything else — nested objects, lists — is dropped
so a deep argument can never bloat the line."""


def _summarise_args(args: Mapping[str, Any]) -> tuple[str, str]:
    """Split a tool call's arguments into ``(subject, detail)``.

    opencode's rule, ported: promote ONE argument to prose, bracket the scalar
    remainder, drop every non-scalar. Yields ``read_file invoices/4417.json
    [limit=130]`` — the useful part in prose, the rest legible but out of the
    way.
    """
    subject_key = next(
        (key for key in _SUBJECT_KEYS if isinstance(args.get(key), _SCALARS)),
        None,
    )
    subject = _plain(str(args[subject_key])) if subject_key is not None else ""
    pairs = [f"{key}={value}" for key, value in args.items() if key != subject_key and isinstance(value, _SCALARS)]
    detail = _plain(f"[{', '.join(pairs)}]") if pairs else ""
    return subject, detail


# --- duration ---------------------------------------------------------------


def _duration(ms: int) -> str:
    """Two significant units, never three — opencode's ``Locale.duration``."""
    if ms < 1_000:
        return f"{ms}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    minutes, seconds = divmod(ms // 1000, 60)
    return f"{minutes}m {seconds}s"


# --- the rows ---------------------------------------------------------------

RowState = Literal["pending", "ok", "failed", "denied", "interrupted"]
"""A keyed row's lifecycle. ``pending`` is the live edge; the rest are terminal.
``interrupted`` is what the seal rewrites a still-``pending`` row to when the run
faults — the bridge is alive and knows, so it never leaves a frozen ``◐``."""


@dataclass(frozen=True, slots=True)
class ProseRow:
    """The agent's own narration. Unkeyed — never mutates."""

    text: str


@dataclass(frozen=True, slots=True)
class ToolRow:
    """One tool call and, once it lands, its result — ONE row, not two.

    ``key`` is the ``tool_call_id``: results arrive in *completion* order (calfkit
    fans out parallel calls, each folding on its own hop), so rows are resolved
    by id, never by position.
    """

    key: str
    name: str
    subject: str = ""
    detail: str = ""
    state: RowState = "pending"
    note: str = ""
    """Why it ended that way — the error, or the denial's reason."""
    elapsed_ms: int | None = None


@dataclass(frozen=True, slots=True)
class ConsultRow:
    """One ``message_agent`` consult. Shows THAT it happened and where to read
    it — never what was said, which is ADR-0020's privacy rule.

    ``thread_url`` is the projection's receipt; ``None`` means the best-effort
    audit render failed, so there is no thread to link.
    """

    key: str
    peer: str
    thread_url: str | None = None
    state: RowState = "pending"
    note: str = ""


@dataclass(frozen=True, slots=True)
class HandoffRow:
    """Control transferring, permanently. ``reason`` is the model's own prose for
    the peer — calfkit rejects a blank one, so it is reliably present."""

    target: str
    reason: str = ""


SealOutcome = Literal["ok", "faulted", "interrupted"]
"""How a turn ended, as far as the bridge can actually tell.

``ok`` and ``faulted`` come from the stream's terminal. ``interrupted`` is the
third, honest case: the stream ended WITHOUT a terminal (the drain raised, the
stream broke, calfkit violated its contract), so the outcome is genuinely
unknown — the reply may still arrive. Collapsing that into ``faulted`` would
assert a failure that may not have happened and point at a notice that may not
exist.
"""


@dataclass(frozen=True, slots=True)
class SealRow:
    """The turn's outcome, appended when the stream's terminal arrives (ADR-0025).
    Absence of a seal means the trace is still running."""

    outcome: SealOutcome
    tool_count: int
    elapsed_ms: int


TraceRow = ProseRow | ToolRow | ConsultRow | HandoffRow | SealRow


# --- rendering --------------------------------------------------------------

_DIM: Final[str] = "-# "
"""Discord's subtext prefix: smaller, dim grey. The resting state of a row —
and per-LINE, which is why :func:`_plain` must flatten."""

_PENDING: Final[str] = "◐"
_OK: Final[str] = "●"
_STOPPED: Final[str] = "⊘"
_HANDOFF: Final[str] = "➜"
_FAILED: Final[str] = "❌"
_FAULT: Final[str] = "⚠️"
_AUDIT_GAP: Final[str] = "⚠️ couldn't write the audit log"


def _suffix(note: str) -> str:
    """``" — note"``, or nothing — never a dangling dash."""
    return f" — {note}" if note else ""


def _struck(head: str, note: str) -> str:
    """A dim, struck-through row: routine, stopped, not an error.

    opencode separates denial from failure deliberately — a denial is expected
    (a winning handoff stubs its siblings), so it must not spend the red that a
    real failure needs.
    """
    return f"{_DIM}~~{_STOPPED} {head}~~{_suffix(note)}"


def _tool_head(row: ToolRow) -> str:
    return " ".join(part for part in (row.name, row.subject, row.detail) if part)


def _render_tool(row: ToolRow) -> str:
    head = _tool_head(row)
    match row.state:
        case "pending":
            return f"{_PENDING} {head}"
        case "ok":
            # No duration when none was measured — an orphan result never had a
            # call row to time from, and "· 0ms" would invent a measurement.
            tail = f" · {_duration(row.elapsed_ms)}" if row.elapsed_ms is not None else ""
            return f"{_DIM}{_OK} {head}{tail}"
        case "failed":
            return f"{_FAILED} {head}{_suffix(row.note)}"
        case "denied":
            return _struck(head, row.note)
        case "interrupted":
            return _struck(head, "interrupted")
    assert_never(row.state)


def _render_consult(row: ConsultRow) -> str:
    link = f"[view exchange]({row.thread_url})" if row.thread_url else _AUDIT_GAP
    match row.state:
        case "pending":
            # Present tense. Today's marker says "consulted" the moment the
            # consult STARTS and never updates — it states what has not happened.
            #
            # The link is here, not only on resolve, for two reasons: the audit
            # thread already exists at request time (the projection is awaited
            # before the row is built), so it is clickable immediately — and a
            # link that appeared only on resolve would be unbudgeted growth that
            # breaks the reservation invariant (TestGrowthReserve caught exactly
            # this).
            return f"{_PENDING} consulting {row.peer} · {link}"
        case "ok":
            return f"{_DIM}{_OK} consulted {row.peer} · {link}"
        case "failed":
            return f"{_FAILED} {row.peer} didn't answer · {link}"
        case "denied":
            return f"{_struck(row.peer, row.note)} · {link}"
        case "interrupted":
            return f"{_struck(row.peer, 'never replied')} · {link}"
    assert_never(row.state)


def _render_seal(row: SealRow) -> str:
    tools = f"{row.tool_count} tool{'' if row.tool_count == 1 else 's'} · " if row.tool_count else ""
    body = f"{tools}{_duration(row.elapsed_ms)}"
    match row.outcome:
        case "ok":
            return f"{_DIM}{body}"
        case "faulted":
            # Points at the notice, which lands below on a DIFFERENT message
            # lineage (the native-reply path, which survives a broken webhook).
            return f"{_FAULT} run failed after {body} — details below"
        case "interrupted":
            # Says only what is known. NOT "run failed": the bridge never saw a
            # terminal, so it cannot claim the run failed, and there may be no
            # notice below to point at. Dim, because an incomplete trace is
            # cosmetic — the reply, if there is one, still arrives.
            return f"{_DIM}{_STOPPED} interrupted after {body}"
    assert_never(row.outcome)


def render_row(row: TraceRow) -> str:
    """Render ONE row into ONE line. Pure, total, exhaustive.

    ``assert_never`` is the exhaustiveness guard: a sixth variant added without a
    branch here is a mypy error, not a silently unrendered row.
    """
    if isinstance(row, ProseRow):
        return row.text
    if isinstance(row, ToolRow):
        return _render_tool(row)
    if isinstance(row, ConsultRow):
        return _render_consult(row)
    if isinstance(row, HandoffRow):
        return f"{_HANDOFF} handed off to {row.target}{_suffix(row.reason)}"
    if isinstance(row, SealRow):
        return _render_seal(row)
    assert_never(row)


# --- prose chunking ---------------------------------------------------------

_V2_CHUNK: Final[int] = 3900
"""Chunk target for a full ``agent_message`` body — one :class:`ProseRow` per
message, kept under the segment's 4000-char cap for headroom. Prose is the only
row whose length the model controls, so it is the only one that chunks rather
than truncating: an answer must never be silently cut."""


def _chunk_text(text: str, limit: int) -> list[str]:
    """Split ``text`` into non-empty ≤``limit``-char pieces on line boundaries.

    Greedily packs whole lines; a single line longer than ``limit`` is
    hard-split into ``limit``-sized pieces. ``current is None`` marks "no line
    accumulated yet", distinct from an accumulated *blank* line (``""``), so
    blank lines between paragraphs survive within a chunk. An empty piece (a
    blank line flushed exactly at a cap boundary) is dropped so no empty body is
    ever emitted — every returned chunk is 1..``limit`` chars.
    """
    chunks: list[str] = []
    current: str | None = None
    for line in text.split("\n"):
        while len(line) > limit:
            if current is not None:
                chunks.append(current)
                current = None
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = line if current is None else f"{current}\n{line}"
        if current is not None and len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current is not None:
        chunks.append(current)
    # Drop any empty piece: a blank line flushed at an exact-cap boundary yields
    # ``""``, and Discord rejects an empty TextDisplay (min length 1). A blank
    # line at a message boundary is cosmetically irrelevant (chunks post as
    # separate messages). This upholds "a ProseRow is never empty" — Discord
    # rejects a zero-length TextDisplay. Non-empty input always leaves at least
    # one non-empty chunk.
    return [chunk for chunk in chunks if chunk]

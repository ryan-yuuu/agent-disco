"""Chunk-splitting for Discord's message-length limits.

Agent replies and A2A audit projections can exceed Discord's hard content
limit; both are delivered as consecutive ≤ :data:`CHUNK_SAFE_SIZE` messages.
Chunking is the *only* delivery mechanism for long content — there is no retry
that asks the agent to shorten its reply, so **answer content is never dropped**:
every character of the body survives into the chunks. Only structural framing may
differ — never answer text — namely trailing-boundary whitespace and some fence
markers (see the fence notes below).

Whole lines are packed greedily up to the limit; a single line longer than the
limit is split on a sentence → word → hard boundary. Fenced code (```` ``` ````)
is kept whole where it fits and, where a block is too large for one message,
split with the fence **closed** on one chunk and **reopened** — same backticks
and language — on the next, so every message renders as a self-contained code
block rather than an orphaned half with a dangling fence.

The same core serves both Discord's 2000-char content messages (via
:func:`chunk_split`) and the Components-V2 4000-char text blocks of the live
step trace (via :func:`calfcord.bridge.trace_rows._chunk_text`, which is a thin
wrapper passing its own limit): one implementation, no duplicated split logic.

Fence detection is deliberately conservative — a line toggles fenced state only
when it is a *clean* marker: a run of ≥3 backticks at column 0 followed by a
backtick-free info string (opener), or a run of ≥ the opener's length alone
(closer). Consequences, all content-preserving and mostly cosmetic:

* A line that merely *contains* backticks (prose that mentions ```` ``` ````, a
  ``print('```')`` inside code) is content, kept verbatim — never a dropped
  marker. If such content sits inside a block, Discord renders it exactly as it
  renders the un-chunked original; this module does not neutralise it.
* An indented fence, or a fence whose opener line alone is too long to wrap
  within the limit (a pathologically long backtick run or info string), is
  treated as prose — kept verbatim, never dropped, and never protected from a
  mid-block cut.
* Two adjacent blocks with the same backticks and language merge into one on
  render; an empty fenced block (no inner lines) is dropped. Both are rare and
  carry no answer content.

Process boundary: this module sits beneath its callers and depends only on
stdlib, so the ``bridge`` modules can import it without inducing a cycle.
"""

from __future__ import annotations

import re
from itertools import groupby
from typing import Final, Literal, NamedTuple

CHUNK_SAFE_SIZE: Final[int] = 1990
"""Max chars per chunk. Discord's hard content limit is 2000; the 10-char
safety buffer absorbs the occasional emoji / encoding surprise that tips a
1999-char string over the limit."""

_FENCE_RUN: Final[re.Pattern[str]] = re.compile(r"`{3,}")
"""A run of three-or-more backticks — Discord's fence delimiter."""

_OPENER: Final[re.Pattern[str]] = re.compile(r"(`{3,})([^`]*)")
"""A clean opening fence: a leading backtick-run then a backtick-free info
string. Matched with ``fullmatch`` against the right-stripped line, so it never
matches a line that carries other content (leading text, a second run)."""

# Separators for splitting a single over-long line, largest unit first. Newlines
# are already consumed by the line split, so only intra-line boundaries remain.
_LINE_SEPARATORS: Final[tuple[str, ...]] = (". ", " ")


class _Unit(NamedTuple):
    """One source line, tagged for packing. Fence markers are dropped during
    parsing and regenerated at render time, so a block split across chunks is
    re-fenced in each. ``fence``/``info`` are the block's backtick-run and info
    string for ``"code"`` units, empty for ``"prose"``."""

    kind: Literal["prose", "code"]
    text: str
    fence: str = ""
    info: str = ""


def chunk_split(text: str, *, max_chars: int = CHUNK_SAFE_SIZE) -> list[str]:
    """Split ``text`` into pieces each ≤ ``max_chars`` for posting as
    consecutive Discord messages, never cutting inside a fenced code block.

    Each chunk is right-stripped of trailing whitespace; whitespace-only content
    yields no chunks (Discord rejects empty messages).

    Args:
        text: The full text to split. Empty or whitespace-only returns ``[]``.
        max_chars: Maximum characters per chunk. Defaults to
            :data:`CHUNK_SAFE_SIZE` (1990) — Discord's 2000-char
            limit with a 10-char safety buffer.

    Returns:
        A list of chunks in original order. If ``text`` already fits, returns
        ``[text]`` verbatim.
    """
    if not text.strip():
        return []
    if len(text) <= max_chars:
        # Fits in one message — post the author's content verbatim, untouched.
        return [text]

    chunks = _pack(_to_units(text, max_chars), max_chars)
    return [stripped for chunk in chunks if (stripped := chunk.rstrip())]


def _match_opener(line: str) -> tuple[str, str] | None:
    """``(backtick_run, info)`` if ``line`` is a clean opening fence, else
    ``None``. Requires the backticks at column 0 and an info string free of
    backticks, so a line carrying real content is never mistaken for a fence."""
    match = _OPENER.fullmatch(line.rstrip())
    return (match.group(1), match.group(2).strip()) if match else None


def _is_closer(line: str, fence_len: int) -> bool:
    """Whether ``line`` is a clean closing fence for a block opened with
    ``fence_len`` backticks: a run of at least that many backticks and nothing
    else. A code line that merely contains ```` ``` ```` is not a closer."""
    stripped = line.rstrip()
    return _FENCE_RUN.fullmatch(stripped) is not None and len(stripped) >= fence_len


def _to_units(text: str, max_chars: int) -> list[_Unit]:
    """Tag every line as ``"prose"`` or ``"code"`` (with its fence run and info),
    dropping only *clean* fence-marker lines — they are regenerated at render
    time. An unterminated fence runs as code to the end; the renderer closes it.

    A fence is honored only when its wrapper fits the limit (:func:`_wrappable`);
    a pathologically long fence run or info string — never produced by real code
    blocks — is left as prose so its content survives and no chunk can exceed
    ``max_chars``."""
    units: list[_Unit] = []
    fence: tuple[str, str] | None = None
    for line in text.split("\n"):
        if fence is None:
            opened = _match_opener(line)
            if opened is not None and _wrappable(opened, max_chars):
                fence = opened
            else:
                units.append(_Unit("prose", line))
        elif _is_closer(line, len(fence[0])):
            fence = None
        else:
            units.append(_Unit("code", line, fence[0], fence[1]))
    return units


def _wrappable(opened: tuple[str, str], max_chars: int) -> bool:
    """Whether a fence's wrapper leaves room for at least one content char within
    ``max_chars``. The empty-body wrapper is ``2·len(fence) + len(info) + 2``; if
    that already fills the budget the block can never be split to fit, so its
    opener line is kept as prose instead."""
    fence, info = opened
    return 2 * len(fence) + len(info) + 2 < max_chars


def _render(units: list[_Unit]) -> str:
    """Render packed units to a chunk string, wrapping each maximal run of
    same-fence code units in its fence. A run confined to one chunk is fenced
    once; a run broken across chunks is fenced independently in each — that is
    the close-and-reopen that keeps every message a valid block."""
    lines: list[str] = []
    for (kind, fence, info), group in groupby(units, key=lambda u: (u.kind, u.fence, u.info)):
        texts = [unit.text for unit in group]
        if kind == "code":
            lines.append(fence + info)
            lines.extend(texts)
            lines.append(fence)
        else:
            lines.extend(texts)
    return "\n".join(lines)


def _solo_len(unit: _Unit) -> int:
    """Rendered length of ``unit`` as its own chunk. A code unit pays its fence
    wrapper: opener (``fence + info``) + closer (``fence``) + two newlines."""
    if unit.kind == "code":
        return 2 * len(unit.fence) + len(unit.info) + len(unit.text) + 2
    return len(unit.text)


def _append_cost(tail: tuple[str, str] | None, unit: _Unit) -> int:
    """Chars added by appending ``unit`` to a non-empty buffer whose trailing
    code run is ``tail`` (``(fence, info)``, or ``None`` when it ends in prose).
    Appending to an open same-fence run costs just the line; anything else opens
    a fresh prose line or a freshly-fenced block."""
    if unit.kind == "code" and tail == (unit.fence, unit.info):
        return 1 + len(unit.text)
    return 1 + _solo_len(unit)


def _tail_of(unit: _Unit) -> tuple[str, str] | None:
    """The open code run a buffer ending in ``unit`` carries, or ``None``."""
    return (unit.fence, unit.info) if unit.kind == "code" else None


def _split_unit(unit: _Unit, max_chars: int) -> list[_Unit]:
    """Split a unit whose rendered form exceeds ``max_chars`` into same-kind
    pieces that each fit. Prose splits on sentence/word/hard boundaries; code
    hard-splits (reserving room for its fence wrapper) since code has no prose
    boundaries — each piece stays valid because the renderer re-fences it."""
    if unit.kind == "code":
        # _to_units only tags a line as code when its fence is wrappable, so the
        # wrapper leaves size >= 1 and every piece renders to <= max_chars. An
        # empty code line's rendered form is just the wrapper (< max_chars), so
        # it never reaches here.
        size = max_chars - _solo_len(unit._replace(text=""))
        return [unit._replace(text=unit.text[i : i + size]) for i in range(0, len(unit.text), size)]
    return [_Unit("prose", piece) for piece in _split_long_line(unit.text, max_chars)]


def _split_long_line(text: str, max_chars: int) -> list[str]:
    """Split one over-long line into ≤``max_chars`` pieces, preferring a
    sentence (``". "``) then word (``" "``) boundary no earlier than the
    halfway point, else a hard cut."""
    pieces: list[str] = []
    remaining = text
    min_split = max(1, max_chars // 2)
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        cut = -1
        for sep in _LINE_SEPARATORS:
            idx = window.rfind(sep)
            if idx >= min_split:
                cut = idx + len(sep)
                break
        if cut < 0:
            cut = max_chars
        pieces.append(remaining[:cut])
        remaining = remaining[cut:]
    pieces.append(remaining)
    return pieces


def _pack(units: list[_Unit], max_chars: int) -> list[str]:
    """Greedily pack units into rendered chunks ≤ ``max_chars``, tracking the
    running rendered length so no chunk is re-rendered per unit. A unit that
    will not fit onto the current chunk starts a fresh one; a unit too large to
    fit even alone is split first, its leading pieces emitted as standalone
    chunks and its tail carried forward to keep packing."""
    chunks: list[str] = []
    buf: list[_Unit] = []
    buf_len = 0
    tail: tuple[str, str] | None = None
    for unit in units:
        if buf:
            cost = _append_cost(tail, unit)
            if buf_len + cost <= max_chars:
                buf.append(unit)
                buf_len += cost
                tail = _tail_of(unit)
                continue
            chunks.append(_render(buf))
            buf, buf_len, tail = [], 0, None
        solo = _solo_len(unit)
        if solo <= max_chars:
            buf, buf_len, tail = [unit], solo, _tail_of(unit)
            continue
        pieces = _split_unit(unit, max_chars)
        chunks.extend(_render([piece]) for piece in pieces[:-1])
        last = pieces[-1]
        buf, buf_len, tail = [last], _solo_len(last), _tail_of(last)
    if buf:
        chunks.append(_render(buf))
    return chunks

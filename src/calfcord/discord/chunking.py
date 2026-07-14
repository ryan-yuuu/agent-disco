"""Chunk-splitting for Discord's 2000-character message limit.

Agent replies and A2A audit projections can exceed Discord's hard content
limit; both are delivered as consecutive ≤ :data:`CHUNK_SAFE_SIZE` messages
split at the largest natural boundary available. Chunking is the *only*
delivery mechanism for long content — there is no retry that asks the agent
to shorten its reply.

Consumers: :mod:`calfcord.bridge.reply_poster` (user-facing replies) and
:mod:`calfcord.bridge.a2a_project` (agent-to-agent audit projections).

Process boundary: this module sits beneath its callers and depends only on
stdlib, so the ``bridge`` modules can import it without inducing a cycle.
"""

from __future__ import annotations

from typing import Final

CHUNK_SAFE_SIZE: Final[int] = 1990
"""Max chars per chunk. Discord's hard content limit is 2000; the 10-char
safety buffer absorbs the occasional emoji / encoding surprise that tips a
1999-char string over the limit."""


def chunk_split(text: str, *, max_chars: int = CHUNK_SAFE_SIZE) -> list[str]:
    """Split ``text`` into pieces each ≤ ``max_chars`` for posting as
    consecutive Discord messages.

    Boundary search is greedy from the largest unit down: paragraph
    (``"\\n\\n"``) → line (``"\\n"``) → sentence (``". "``) → word
    (``" "``) → hard cut. The search refuses to split earlier than
    ``max_chars // 2`` so we don't produce a tiny first chunk
    followed by a huge tail.

    Each chunk is right-stripped of trailing whitespace. The split
    preserves all non-boundary characters — joining chunks back with
    the boundary that produced each cut reconstructs (modulo
    boundary whitespace) the original text.

    Args:
        text: The full text to split. May be empty (returns ``[]``).
        max_chars: Maximum characters per chunk. Defaults to
            :data:`CHUNK_SAFE_SIZE` (1990) — Discord's 2000-char
            limit with a 10-char safety buffer.

    Returns:
        A list of chunks in original order. If ``text`` already fits,
        returns ``[text]``. An empty string returns ``[]``.
    """
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    remaining = text
    min_split = max(1, max_chars // 2)

    while remaining:
        if len(remaining) <= max_chars:
            stripped = remaining.rstrip()
            if stripped:
                chunks.append(stripped)
            break

        candidate = remaining[:max_chars]
        cut_at = -1
        # Prefer larger structural boundaries.
        for separator in ("\n\n", "\n", ". ", " "):
            idx = candidate.rfind(separator)
            if idx >= min_split:
                cut_at = idx + len(separator)
                break

        if cut_at < 0:
            # No good boundary found; hard cut at max_chars.
            cut_at = max_chars

        chunk = remaining[:cut_at].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut_at:]

    return chunks

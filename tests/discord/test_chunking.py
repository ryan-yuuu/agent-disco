"""Tests for the chunk-split helper.

Covers :func:`chunk_split` — boundary search, hard-cut fallback, content
preservation. Orchestration-side tests (posting chunks, transcript rows,
failure logging) live with each consumer: bridge reply cases in
``tests/bridge/test_reply_poster.py``, A2A cases in
``tests/bridge/test_a2a_project.py``.
"""

from __future__ import annotations

from calfcord.discord.chunking import CHUNK_SAFE_SIZE, chunk_split


class TestChunkSplit:
    def test_empty_returns_empty_list(self) -> None:
        assert chunk_split("") == []

    def test_short_returns_single_chunk(self) -> None:
        assert chunk_split("hello") == ["hello"]

    def test_long_splits_into_multiple(self) -> None:
        text = "x" * 5000
        chunks = chunk_split(text)
        assert len(chunks) >= 3
        for c in chunks:
            assert len(c) <= CHUNK_SAFE_SIZE

    def test_prefers_paragraph_boundary(self) -> None:
        first = "a" * 1500
        second = "b" * 1500
        text = f"{first}\n\n{second}"
        chunks = chunk_split(text)
        assert len(chunks) == 2
        assert chunks[0] == first
        assert chunks[1] == second

    def test_falls_back_to_line_boundary(self) -> None:
        first = "a" * 1500
        second = "b" * 1500
        text = f"{first}\n{second}"
        chunks = chunk_split(text)
        assert len(chunks) == 2
        assert chunks[0] == first
        assert chunks[1] == second

    def test_falls_back_to_sentence_boundary(self) -> None:
        first = "a" * 1500
        second = "b" * 1500
        text = f"{first}. {second}"
        chunks = chunk_split(text)
        assert len(chunks) == 2
        # Sentence cut keeps the period in the first chunk.
        assert chunks[0].endswith(".")

    def test_hard_cut_when_no_boundary(self) -> None:
        text = "x" * 4000  # no boundaries at all
        chunks = chunk_split(text)
        assert len(chunks) >= 3
        for c in chunks:
            assert len(c) <= CHUNK_SAFE_SIZE

    def test_whitespace_only_tail_is_dropped(self) -> None:
        # A trailing run of whitespace after the last cut is not worth a message.
        assert chunk_split("a" * 1990 + " " * 20) == ["a" * 1990]

    def test_whitespace_only_chunk_is_skipped(self) -> None:
        # A cut that lands inside a whitespace run must not emit an empty message
        # (Discord rejects empty content).
        chunks = chunk_split(" " * 1990 + "b" * 2000)
        assert all(c.strip() for c in chunks)
        assert "".join(chunks).count("b") == 2000

    def test_preserves_total_content_modulo_boundary_whitespace(self) -> None:
        """All non-boundary chars survive across chunks."""
        first = "a" * 1500
        second = "b" * 1500
        text = f"{first}. {second}"
        chunks = chunk_split(text)
        joined = "".join(chunks)
        # The boundary characters (". ") get split off but the
        # alphabetic content is preserved exactly.
        assert "a" * 1500 in joined
        assert "b" * 1500 in joined

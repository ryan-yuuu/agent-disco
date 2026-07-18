"""Tests for the chunk-split helper.

Covers :func:`chunk_split` — boundary search, hard-cut fallback, content
preservation, and fenced-code integrity. Orchestration-side tests (posting
chunks, transcript rows, failure logging) live with each consumer: bridge reply
cases in ``tests/bridge/test_reply_poster.py``, A2A cases in
``tests/bridge/test_a2a_project.py``.
"""

from __future__ import annotations

import re

from calfcord.discord.chunking import CHUNK_SAFE_SIZE, chunk_split

_FENCE_RUN = re.compile(r"`{3,}")


def _fence_runs(text: str) -> int:
    """Count runs of 3+ backticks — a chunk with an odd count has a fence that
    Discord will render unbalanced (an unclosed block, or a dangling opener)."""
    return len(_FENCE_RUN.findall(text))


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


class TestFenceAwareSplit:
    """A cut must never land inside a ``` fenced block. A block that fits stays
    whole; a block too large to fit is split with the fence closed on one chunk
    and reopened (same language) on the next, so every message renders as a
    self-contained, valid code block."""

    def test_block_straddling_a_boundary_is_not_split_mid_fence(self) -> None:
        # The char boundary falls inside the code block; a naive splitter would
        # cut it, leaving an unclosed fence on one chunk and a dangling ``` on
        # the next. Every chunk must keep its fences balanced.
        intro = "x" * 90
        code = "```py\n" + "y" * 20 + "\n```"
        text = f"{intro}\n{code}"
        chunks = chunk_split(text, max_chars=100)
        for c in chunks:
            assert _fence_runs(c) % 2 == 0, c
        assert any(code in c for c in chunks)

    def test_oversized_block_reopens_with_language(self) -> None:
        body = "\n".join(f"line{i}" for i in range(40))
        text = f"```python\n{body}\n```"
        chunks = chunk_split(text, max_chars=60)
        assert len(chunks) >= 2
        for c in chunks:
            assert _fence_runs(c) % 2 == 0, c
            assert c.startswith("```python"), c
            assert c.endswith("```"), c

    def test_oversized_block_preserves_inner_content_in_order(self) -> None:
        body = "\n".join(f"line{i}" for i in range(40))
        text = f"```python\n{body}\n```"
        chunks = chunk_split(text, max_chars=60)
        # Strip each chunk's opening fence line and closing fence line, then
        # rejoin: the inner code must reconstruct the original body exactly.
        inner = "\n".join(line for c in chunks for line in c.split("\n")[1:-1])
        assert inner == body

    def test_oversized_block_without_language_reopens_bare(self) -> None:
        body = "\n".join(f"row{i}" for i in range(40))
        text = f"```\n{body}\n```"
        chunks = chunk_split(text, max_chars=50)
        assert len(chunks) >= 2
        for c in chunks:
            assert _fence_runs(c) % 2 == 0, c
            assert c.split("\n", 1)[0] == "```", c  # bare fence, not a bogus lang

    def test_language_tag_preserved_across_every_reopen(self) -> None:
        body = "\n".join(f"k{i}: v{i}" for i in range(30))
        text = f"```yaml\n{body}\n```"
        chunks = chunk_split(text, max_chars=50)
        assert len(chunks) >= 2
        for c in chunks:
            assert c.split("\n", 1)[0] == "```yaml", c

    def test_all_chunks_within_limit_including_fence_overhead(self) -> None:
        body = "\n".join("z" * 30 for _ in range(30))
        text = f"```python\n{body}\n```"
        chunks = chunk_split(text, max_chars=60)
        for c in chunks:
            assert len(c) <= 60, (len(c), c)

    def test_prose_before_oversized_block_is_not_fenced(self) -> None:
        prose = "here is the code:"
        body = "\n".join(f"c{i}" for i in range(30))
        text = f"{prose}\n```python\n{body}\n```"
        chunks = chunk_split(text, max_chars=50)
        assert prose in chunks[0]
        assert not chunks[0].startswith("```")  # prose is not wrapped as code
        for c in chunks:
            assert _fence_runs(c) % 2 == 0, c

    def test_single_code_line_longer_than_a_message_is_split_within_fences(self) -> None:
        # A lone code line (e.g. minified JS) too long for one message must
        # still split, each piece a self-contained fenced block; room for the
        # fence wrapper is reserved so every chunk stays within the limit.
        line = "x" * 200
        text = f"```js\n{line}\n```"
        chunks = chunk_split(text, max_chars=60)
        assert len(chunks) >= 2
        for c in chunks:
            assert c.startswith("```js"), c
            assert c.endswith("```"), c
            assert _fence_runs(c) % 2 == 0, c
            assert len(c) <= 60, (len(c), c)
        # The characters survive; only newlines are introduced at the hard cuts.
        inner = "".join(piece for c in chunks for piece in c.split("\n")[1:-1])
        assert inner == line

    def test_text_of_only_fence_markers_yields_no_chunks(self) -> None:
        # Pathological: content that is nothing but fence lines carries no body,
        # so there is nothing to deliver.
        text = "\n".join("```" for _ in range(600))
        assert len(text) > CHUNK_SAFE_SIZE  # past the single-message fast path
        assert chunk_split(text) == []


class TestFenceContentPreservation:
    """A cut must never DROP content. A line that merely *contains* backticks is
    not a fence marker — only a clean opener (backticks + backtick-free info) or
    closer (backticks alone) toggles fenced state, so real answer content that
    happens to include ``` survives verbatim."""

    def test_stray_fence_run_in_prose_is_kept(self) -> None:
        # A long prose reply that mentions ``` in a sentence must keep the
        # sentence — the stray run is not a fence marker.
        stray = "ALPHA to make a block wrap text in ``` markers like so OMEGA"
        pad = "\n".join(f"pad{i}" for i in range(80))
        text = f"{pad}\n{stray}\n{pad}"
        chunks = chunk_split(text, max_chars=150)  # force the multi-chunk path
        joined = "\n".join(chunks)
        assert "ALPHA" in joined and "OMEGA" in joined
        for i in range(80):
            assert f"pad{i}" in joined, i

    def test_triple_backtick_literal_inside_code_block_survives(self) -> None:
        lines = [f"code{i}" for i in range(60)]
        lines.insert(30, "    print('```')  # MARKER: a literal fence in code")
        body = "\n".join(lines)
        text = f"```python\n{body}\n```"
        chunks = chunk_split(text, max_chars=200)
        joined = "\n".join(chunks)
        assert "MARKER" in joined
        for i in range(60):
            assert f"code{i}" in joined, i

    def test_four_backtick_block_embedding_a_triple_backtick_survives(self) -> None:
        # A ````-fenced block deliberately shows a ```python example; the inner
        # triple-backtick must NOT close the four-backtick block.
        lines = [f"row{i}" for i in range(50)]
        lines.insert(25, "```python  # INNER: an example opener shown verbatim")
        body = "\n".join(lines)
        text = f"````markdown\n{body}\n````"
        chunks = chunk_split(text, max_chars=200)
        joined = "\n".join(chunks)
        assert "INNER" in joined
        for i in range(50):
            assert f"row{i}" in joined, i
        # Reopened faithfully with four backticks, not normalized to three.
        for c in chunks:
            assert c.split("\n", 1)[0].startswith("````"), c

    def test_whitespace_only_input_returns_no_chunks(self) -> None:
        # Contract: whitespace-only content carries nothing to post (and callers
        # like a2a substitute a placeholder for []).
        assert chunk_split("   ") == []
        assert chunk_split("\n\n  \n\t") == []

    def test_unterminated_fence_is_closed_on_every_chunk(self) -> None:
        body = "\n".join(f"line{i}" for i in range(40))
        text = f"intro\n```python\n{body}"  # no closing fence at all
        chunks = chunk_split(text, max_chars=60)
        assert len(chunks) >= 2
        for c in chunks:
            assert _fence_runs(c) % 2 == 0, c
        assert any("```python" in c for c in chunks)

    def test_mixed_prose_and_code_keeps_every_line(self) -> None:
        py = "\n".join(f"p{i}" for i in range(30))
        js = "\n".join(f"j{i}" for i in range(30))
        text = f"before the code\n```py\n{py}\n```\nbetween the blocks\n```js\n{js}\n```\nafter the code"
        chunks = chunk_split(text, max_chars=60)
        joined = "\n".join(chunks)
        for token in ("before the code", "between the blocks", "after the code"):
            assert token in joined, token
        for i in range(30):
            assert f"p{i}" in joined and f"j{i}" in joined, i
        for c in chunks:
            assert _fence_runs(c) % 2 == 0, c

    def test_each_block_reopens_with_its_own_language(self) -> None:
        py = "\n".join(f"p{i}" for i in range(30))
        js = "\n".join(f"j{i}" for i in range(30))
        text = f"```py\n{py}\n```\n```js\n{js}\n```"
        chunks = chunk_split(text, max_chars=50)
        py_chunks = [c for c in chunks if "p0" in c or "p29" in c]
        js_chunks = [c for c in chunks if "j0" in c or "j29" in c]
        assert all(c.split("\n", 1)[0] == "```py" for c in py_chunks)
        assert all(c.split("\n", 1)[0] == "```js" for c in js_chunks)

    def test_blank_lines_inside_a_code_block_survive(self) -> None:
        body = "a\n\nb\n\nc\n" + "\n".join(f"d{i}" for i in range(20))
        text = f"```python\n{body}\n```"
        chunks = chunk_split(text, max_chars=45)
        assert len(chunks) >= 2
        inner = "\n".join(line for c in chunks for line in c.split("\n")[1:-1])
        assert inner == body  # interior blank lines preserved across chunks

    def test_pathological_fence_never_exceeds_limit_and_keeps_content(self) -> None:
        # A fence whose wrapper (a huge backtick run, or a huge info string) can't
        # fit within the limit is treated as prose, so no chunk ever exceeds
        # max_chars (Discord would reject it) and its characters still survive.
        huge_run = "`" * 994
        t1 = f"{huge_run}\nBODYTOKEN\n{huge_run}"
        t2 = "```" + "I" * 1986 + "\nCODETOKEN\n```"
        for text, token in ((t1, "BODYTOKEN"), (t2, "CODETOKEN")):
            chunks = chunk_split(text)  # default limit 1990
            assert chunks
            for c in chunks:
                assert len(c) <= CHUNK_SAFE_SIZE, (len(c), c[:40])
            assert token in "\n".join(chunks)

    def test_limit_below_fence_overhead_treats_fences_as_prose(self) -> None:
        # A max_chars smaller than a bare fence's wrapper: the fence can't be
        # honored, so its markers become prose and every chunk still fits.
        text = "prose padding to force a split\n```\n\n```"
        chunks = chunk_split(text, max_chars=5)
        assert chunks
        assert "prose" in "\n".join(chunks)
        for c in chunks:
            assert len(c) <= 5, (len(c), c)

    def test_all_chunks_stay_within_limit_for_structured_inputs(self) -> None:
        # Locks the packer's length accounting: a wrong cost would emit an
        # over-limit chunk here.
        py = "\n".join(f"p{i}" for i in range(40))
        cases = [
            "\n".join(f"line{i}" for i in range(200)),
            f"prose intro\n```python\n{py}\n```\nprose outro",
            f"```js\n{'x' * 500}\n```",
            "```py\n" + "\n".join("z" * 30 for _ in range(40)) + "\n```",
            "word " * 400,
        ]
        for text in cases:
            for max_chars in (30, 45, 60, 200):
                for c in chunk_split(text, max_chars=max_chars):
                    assert len(c) <= max_chars, (max_chars, len(c), c[:80])

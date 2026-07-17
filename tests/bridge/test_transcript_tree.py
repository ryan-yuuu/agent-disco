"""Unit tests for the persisted transcript's tree renderer
(:mod:`calfcord.bridge.transcript_tree`).

This is the PERSISTED transcript's surface, not the live one — the step trace
is rendered from row values in :mod:`calfcord.bridge.trace_rows` and posted by
:class:`~calfcord.bridge.trace.StepTraceRenderer` (see ``test_trace.py``). The
two were once one module; rows are markdown rather than code blocks, so nothing
is shared any more.

What lives here takes an input and returns a value, with no Discord, Kafka, or
state:

* the full transcript tree renderer over ``Sequence[ModelMessage]``
  (``TestTreeRender``) — byte-for-byte stable so persisted transcripts keep
  rendering identically. Its block COUNT is what gates whether the reply poster
  writes a transcript row for tool-call replay;
* the backtick-fence neutralizer (``TestFenceSafe``).
"""

from __future__ import annotations

from calfkit._vendor.pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

import calfcord.bridge.transcript_tree as transcript_tree
from calfcord.bridge.step_events import StepEvent


def _step(
    kind: str,
    *,
    text: str = "",
    name: str | None = None,
    args: dict[str, object] | None = None,
    outcome: str = "success",
    target: str | None = None,
) -> StepEvent:
    """Build a StepEvent for the renderer. correlation_id/depth/emitter are
    fixed — the pure renderer reads only ``kind``/``text``/``name``/``args``/
    ``outcome``/``target``."""
    return StepEvent(
        kind=kind,  # type: ignore[arg-type]
        correlation_id="corr-1",
        depth=0,
        emitter="aksel",
        text=text,
        name=name,
        args=args,
        outcome=outcome,  # type: ignore[arg-type]
        target=target,
    )


class TestTreeRender:
    """The full ``⤵ steps`` transcript renderer (``_render_tree_blocks``):
    Claude-Code-style ``● tool(args)`` / ``⎿ result`` blocks, one per visual
    block (a tool call and its result are ONE block), no per-part truncation,
    paired by ``tool_call_id`` (handles parallel calls); a return whose call is
    absent from the slice renders standalone."""

    def test_text_then_call_pair_counts_as_two_blocks(self) -> None:
        delta = [
            ModelResponse(
                parts=[
                    TextPart(content="Let me check."),
                    ToolCallPart(tool_name="weather", args={"c": "Tokyo"}, tool_call_id="t1"),
                ]
            ),
            ModelRequest(parts=[ToolReturnPart(tool_name="weather", content="18C", tool_call_id="t1")]),
        ]
        blocks = transcript_tree._render_tree_blocks(delta)
        # Prose block + ONE call/return block — the result is folded into its
        # call, so a tool use credits a single step.
        assert blocks == ["Let me check.", '```\n● weather(c="Tokyo")\n  ⎿  18C\n```']

    def test_multiline_result_nests_with_aligned_continuation(self) -> None:
        delta = [
            ModelResponse(parts=[ToolCallPart(tool_name="shell", args={"cmd": "ls"}, tool_call_id="t1")]),
            ModelRequest(parts=[ToolReturnPart(tool_name="shell", content="a\nb\nc", tool_call_id="t1")]),
        ]
        assert transcript_tree._render_tree_blocks(delta) == ['```\n● shell(cmd="ls")\n  ⎿  a\n     b\n     c\n```']

    def test_parallel_calls_pair_to_their_own_returns(self) -> None:
        delta = [
            ModelResponse(
                parts=[
                    ToolCallPart(tool_name="weather", args={"c": "Tokyo"}, tool_call_id="a"),
                    ToolCallPart(tool_name="news", args={"t": "tech"}, tool_call_id="b"),
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(tool_name="weather", content="18C", tool_call_id="a"),
                    ToolReturnPart(tool_name="news", content="headline", tool_call_id="b"),
                ]
            ),
        ]
        # Two call/return blocks, each return matched to its call BY ID (not by
        # position) and rendered in call order.
        assert transcript_tree._render_tree_blocks(delta) == [
            '```\n● weather(c="Tokyo")\n  ⎿  18C\n```',
            '```\n● news(t="tech")\n  ⎿  headline\n```',
        ]

    def test_call_without_return_renders_call_line_alone(self) -> None:
        delta = [ModelResponse(parts=[ToolCallPart(tool_name="slow", args={"x": 1}, tool_call_id="p")])]
        assert transcript_tree._render_tree_blocks(delta) == ["```\n● slow(x=1)\n```"]

    def test_orphan_return_renders_standalone_not_dropped(self) -> None:
        # A return whose call predates the slice must NOT be silently dropped —
        # that would also skew the step count gating the ⤵ button.
        delta = [ModelRequest(parts=[ToolReturnPart(tool_name="weather", content="18C", tool_call_id="z")])]
        assert transcript_tree._render_tree_blocks(delta) == ["```\n⎿  18C\n```"]

    def test_no_per_part_truncation_in_full_view(self) -> None:
        big = "y" * 9000
        delta = [
            ModelResponse(parts=[ToolCallPart(tool_name="dump", args={}, tool_call_id="t1")]),
            ModelRequest(parts=[ToolReturnPart(tool_name="dump", content=big, tool_call_id="t1")]),
        ]
        rendered = transcript_tree._render_tree_blocks(delta)[0]
        # The full payload survives — no per-part cap. The tree render now feeds
        # the persisted transcript (tool-call replay) and the step count, not a
        # size-bounded display, so it is deliberately unbounded here.
        assert rendered.count("y") == 9000

    def test_triple_backticks_in_result_cannot_break_the_fence(self) -> None:
        delta = [
            ModelResponse(parts=[ToolCallPart(tool_name="echo", args={}, tool_call_id="t1")]),
            ModelRequest(parts=[ToolReturnPart(tool_name="echo", content="```py\ncode\n```", tool_call_id="t1")]),
        ]
        rendered = transcript_tree._render_tree_blocks(delta)[0]
        # Only the wrapping fence survives as a raw triple-backtick run; the
        # embedded fences are woven with zero-width spaces.
        assert rendered.count("```") == 2
        assert "\u200b" in rendered

    def test_whitespace_only_text_part_is_skipped(self) -> None:
        # An empty/whitespace-only preamble TextPart produces no prose block.
        delta = [ModelResponse(parts=[TextPart(content="   \n  ")])]
        assert transcript_tree._render_tree_blocks(delta) == []

    def test_skips_prompt_parts(self) -> None:
        delta = [
            ModelRequest(
                parts=[
                    SystemPromptPart(content="system."),
                    UserPromptPart(content="hello"),
                ]
            ),
        ]
        assert transcript_tree._render_tree_blocks(delta) == []

    def test_parallel_call_with_one_missing_return(self) -> None:
        # Two parallel calls, only the first has returned this slice: the
        # paired call folds its result, the in-flight one renders alone.
        delta = [
            ModelResponse(
                parts=[
                    ToolCallPart(tool_name="a", args={}, tool_call_id="a"),
                    ToolCallPart(tool_name="b", args={}, tool_call_id="b"),
                ]
            ),
            ModelRequest(parts=[ToolReturnPart(tool_name="a", content="ra", tool_call_id="a")]),
        ]
        assert transcript_tree._render_tree_blocks(delta) == [
            "```\n● a()\n  ⎿  ra\n```",
            "```\n● b()\n```",
        ]

    def test_return_before_its_call_renders_once_not_twice(self) -> None:
        # Order-independence: a return that appears BEFORE its call in the
        # slice must fold into the call exactly once — never render both
        # standalone AND nested (which would also inflate the step count).
        delta = [
            ModelRequest(parts=[ToolReturnPart(tool_name="a", content="EARLY", tool_call_id="x")]),
            ModelResponse(parts=[ToolCallPart(tool_name="a", args={}, tool_call_id="x")]),
        ]
        assert transcript_tree._render_tree_blocks(delta) == ["```\n● a()\n  ⎿  EARLY\n```"]

    def test_full_view_preserves_arg_whitespace_fidelity(self) -> None:
        # collapse=False on the full view keeps inner whitespace in arg values
        # byte-for-byte (the live preview would collapse "a  b" -> "a b").
        delta = [ModelResponse(parts=[ToolCallPart(tool_name="run", args={"cmd": "a  b"}, tool_call_id="t1")])]
        assert transcript_tree._render_tree_blocks(delta) == ['```\n● run(cmd="a  b")\n```']




class TestFenceSafe:
    """``_fence_safe`` neutralizes runs of 3+ backticks (which would close a
    Discord code fence early regardless of the opening fence length) while
    leaving 1-2 backtick runs — which render literally inside a block —
    untouched."""

    def test_single_and_double_backtick_runs_untouched(self) -> None:
        assert transcript_tree._fence_safe("a `b` c") == "a `b` c"
        assert transcript_tree._fence_safe("``x``") == "``x``"

    def test_runs_of_three_or_more_are_woven_with_zwsp(self) -> None:
        for n in (3, 4, 6):
            out = transcript_tree._fence_safe("`" * n)
            assert "```" not in out  # no raw 3-run survives to close a fence
            assert out.count("`") == n  # every backtick preserved, just separated
            assert out.count("\u200b") == n - 1

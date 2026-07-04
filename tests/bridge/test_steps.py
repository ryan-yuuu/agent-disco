"""Unit tests for the PURE step renderers in :mod:`calfcord.bridge.steps_render`.

The live step surface is driven by
:class:`~calfcord.bridge.progress.ProgressRenderer` off the normalized
``StepEvent`` stream (its posting lifecycle is covered in ``test_progress.py``).
What remains here are the renderer's pure functions, which take an input and
return a value with no Discord, Kafka, or state:

* the Components-V2 step renderer — :func:`render_step_message` over one
  ``StepEvent`` (a monospace ``tool_call`` / ``tool_result`` line, a handoff
  note, or the full ``agent_message`` prose chunked to the v2 cap)
  (``TestRenderStepMessage``);
* the full transcript tree renderer over ``Sequence[ModelMessage]``
  (``TestTreeRender``) — byte-for-byte stable so persisted transcripts keep
  rendering identically;
* the backtick-fence neutralizer (``TestFenceSafe``).
"""

from __future__ import annotations

import logging

from calfkit._vendor.pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

import calfcord.bridge.steps_render as steps_render
from calfcord.bridge.step_events import StepEvent


def _step(
    kind: str,
    *,
    text: str = "",
    name: str | None = None,
    args: dict[str, object] | None = None,
    is_error: bool = False,
    target: str | None = None,
) -> StepEvent:
    """Build a StepEvent for the renderer. correlation_id/depth/emitter are
    fixed — the pure renderer reads only ``kind``/``text``/``name``/``args``/
    ``is_error``/``target``."""
    return StepEvent(
        kind=kind,  # type: ignore[arg-type]
        correlation_id="corr-1",
        depth=0,
        emitter="aksel",
        text=text,
        name=name,
        args=args,
        is_error=is_error,
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
        blocks = steps_render._render_tree_blocks(delta)
        # Prose block + ONE call/return block — the result is folded into its
        # call, so a tool use credits a single step.
        assert blocks == ["Let me check.", '```\n● weather(c="Tokyo")\n  ⎿  18C\n```']

    def test_multiline_result_nests_with_aligned_continuation(self) -> None:
        delta = [
            ModelResponse(parts=[ToolCallPart(tool_name="shell", args={"cmd": "ls"}, tool_call_id="t1")]),
            ModelRequest(parts=[ToolReturnPart(tool_name="shell", content="a\nb\nc", tool_call_id="t1")]),
        ]
        assert steps_render._render_tree_blocks(delta) == ['```\n● shell(cmd="ls")\n  ⎿  a\n     b\n     c\n```']

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
        assert steps_render._render_tree_blocks(delta) == [
            '```\n● weather(c="Tokyo")\n  ⎿  18C\n```',
            '```\n● news(t="tech")\n  ⎿  headline\n```',
        ]

    def test_call_without_return_renders_call_line_alone(self) -> None:
        delta = [ModelResponse(parts=[ToolCallPart(tool_name="slow", args={"x": 1}, tool_call_id="p")])]
        assert steps_render._render_tree_blocks(delta) == ["```\n● slow(x=1)\n```"]

    def test_orphan_return_renders_standalone_not_dropped(self) -> None:
        # A return whose call predates the slice must NOT be silently dropped —
        # that would also skew the step count gating the ⤵ button.
        delta = [ModelRequest(parts=[ToolReturnPart(tool_name="weather", content="18C", tool_call_id="z")])]
        assert steps_render._render_tree_blocks(delta) == ["```\n⎿  18C\n```"]

    def test_no_per_part_truncation_in_full_view(self) -> None:
        big = "y" * 9000
        delta = [
            ModelResponse(parts=[ToolCallPart(tool_name="dump", args={}, tool_call_id="t1")]),
            ModelRequest(parts=[ToolReturnPart(tool_name="dump", content=big, tool_call_id="t1")]),
        ]
        rendered = steps_render._render_tree_blocks(delta)[0]
        # The full payload survives — the only bound is the overall message cap
        # (enforced by steps_toggle's file-attachment path), not a per-part cap.
        assert rendered.count("y") == 9000

    def test_triple_backticks_in_result_cannot_break_the_fence(self) -> None:
        delta = [
            ModelResponse(parts=[ToolCallPart(tool_name="echo", args={}, tool_call_id="t1")]),
            ModelRequest(parts=[ToolReturnPart(tool_name="echo", content="```py\ncode\n```", tool_call_id="t1")]),
        ]
        rendered = steps_render._render_tree_blocks(delta)[0]
        # Only the wrapping fence survives as a raw triple-backtick run; the
        # embedded fences are woven with zero-width spaces.
        assert rendered.count("```") == 2
        assert "\u200b" in rendered

    def test_whitespace_only_text_part_is_skipped(self) -> None:
        # An empty/whitespace-only preamble TextPart produces no prose block.
        delta = [ModelResponse(parts=[TextPart(content="   \n  ")])]
        assert steps_render._render_tree_blocks(delta) == []

    def test_skips_prompt_parts(self) -> None:
        delta = [
            ModelRequest(
                parts=[
                    SystemPromptPart(content="system."),
                    UserPromptPart(content="hello"),
                ]
            ),
        ]
        assert steps_render._render_tree_blocks(delta) == []

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
        assert steps_render._render_tree_blocks(delta) == [
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
        assert steps_render._render_tree_blocks(delta) == ["```\n● a()\n  ⎿  EARLY\n```"]

    def test_full_view_preserves_arg_whitespace_fidelity(self) -> None:
        # collapse=False on the full view keeps inner whitespace in arg values
        # byte-for-byte (the live preview would collapse "a  b" -> "a b").
        delta = [ModelResponse(parts=[ToolCallPart(tool_name="run", args={"cmd": "a  b"}, tool_call_id="t1")])]
        assert steps_render._render_tree_blocks(delta) == ['```\n● run(cmd="a  b")\n```']


class TestRenderStepMessage:
    """The Components-V2 step renderer (:func:`render_step_message`): a list of
    message bodies for one step. Tool calls/results are short-form with the name
    in monospace; an ``agent_message`` carries the FULL prose (chunked to the v2
    cap); a ``handoff`` shows the bare target; unknown kinds are a safe no-op."""

    def test_tool_call_renders_monospace_name(self) -> None:
        assert steps_render.render_step_message(_step("tool_call", name="read_file")) == ["🔧 `read_file` called"]

    def test_tool_result_renders_returned(self) -> None:
        assert steps_render.render_step_message(_step("tool_result", name="read_file")) == ["✅ `read_file` returned"]

    def test_tool_result_error_renders_errored(self) -> None:
        assert steps_render.render_step_message(_step("tool_result", name="read_file", is_error=True)) == [
            "❌ `read_file` errored"
        ]

    def test_handoff_renders_bare_target(self) -> None:
        assert steps_render.render_step_message(_step("handoff", target="billing")) == ["➡️ handed off to `billing`"]

    def test_handoff_strips_leading_slash_from_target(self) -> None:
        # An agent name may arrive slash-prefixed; render just the bare name.
        assert steps_render.render_step_message(_step("handoff", target="/billing")) == ["➡️ handed off to `billing`"]

    def test_agent_message_short_is_one_full_body(self) -> None:
        assert steps_render.render_step_message(_step("agent_message", text="Let me check the config.")) == [
            "Let me check the config."
        ]

    def test_agent_message_is_outer_stripped(self) -> None:
        assert steps_render.render_step_message(_step("agent_message", text="  hello  ")) == ["hello"]

    def test_whitespace_only_agent_message_renders_nothing(self) -> None:
        assert steps_render.render_step_message(_step("agent_message", text="   \n  ")) == []

    def test_agent_message_chunks_on_line_boundaries_nothing_lost(self) -> None:
        limit = steps_render._V2_CHUNK
        lines = [f"line{i} " + "y" * 50 for i in range(200)]  # well over the cap
        text = "\n".join(lines)
        bodies = steps_render.render_step_message(_step("agent_message", text=text))
        assert len(bodies) >= 2
        assert all(0 < len(b) <= limit for b in bodies)  # every body fits and is non-empty
        assert "\n".join(bodies) == text  # split only at newlines — nothing dropped

    def test_agent_message_hard_splits_an_over_long_line(self) -> None:
        limit = steps_render._V2_CHUNK
        text = "z" * (limit * 2 + 100)  # a single line with no split points, way over the cap
        bodies = steps_render.render_step_message(_step("agent_message", text=text))
        assert len(bodies) == 3  # two full pieces + the remainder
        assert all(0 < len(b) <= limit for b in bodies)
        assert "".join(bodies) == text  # a hard-split line rejoins with no separator — nothing lost

    def test_agent_message_flushes_accumulated_line_before_hard_split(self) -> None:
        # A short line followed by an over-long line: the short line is flushed
        # as its own chunk BEFORE the long line is hard-split (nothing merged).
        limit = steps_render._V2_CHUNK
        short = "short line"
        long = "z" * (limit * 2)
        bodies = steps_render.render_step_message(_step("agent_message", text=f"{short}\n{long}"))
        assert all(0 < len(b) <= limit for b in bodies)
        assert bodies[0] == short
        assert "".join(bodies[1:]) == long  # the hard-split pieces rejoin to the long line

    def test_agent_message_preserves_blank_lines_across_a_split(self) -> None:
        limit = steps_render._V2_CHUNK
        para = "w" * (limit - 100)
        text = f"{para}\n\n{para}"  # two paragraphs; the blank line must survive the split
        bodies = steps_render.render_step_message(_step("agent_message", text=text))
        assert len(bodies) >= 2
        assert all(0 < len(b) <= limit for b in bodies)
        assert "\n".join(bodies) == text  # blank line preserved, nothing lost

    def test_unknown_kind_is_a_safe_noop_that_logs(self, caplog) -> None:
        # A future/unknown calfkit kind must never crash the drain: render nothing,
        # but log so the gap is visible (e.g. agent_thinking if it ever surfaces).
        with caplog.at_level(logging.WARNING, logger="calfcord.bridge.steps_render"):
            result = steps_render.render_step_message(_step("agent_thinking"))
        assert result == []
        assert any("agent_thinking" in r.getMessage() for r in caplog.records)


class TestFenceSafe:
    """``_fence_safe`` neutralizes runs of 3+ backticks (which would close a
    Discord code fence early regardless of the opening fence length) while
    leaving 1-2 backtick runs — which render literally inside a block —
    untouched."""

    def test_single_and_double_backtick_runs_untouched(self) -> None:
        assert steps_render._fence_safe("a `b` c") == "a `b` c"
        assert steps_render._fence_safe("``x``") == "``x``"

    def test_runs_of_three_or_more_are_woven_with_zwsp(self) -> None:
        for n in (3, 4, 6):
            out = steps_render._fence_safe("`" * n)
            assert "```" not in out  # no raw 3-run survives to close a fence
            assert out.count("`") == n  # every backtick preserved, just separated
            assert out.count("\u200b") == n - 1

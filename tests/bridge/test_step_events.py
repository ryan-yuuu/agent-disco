"""Unit tests for the RunEvent → StepEvent seam (``normalize_run_event``)."""

from __future__ import annotations

from calfkit.client import (
    AgentMessageEvent,
    HandoffEvent,
    RunCompleted,
    RunFailed,
    ToolCallEvent,
    ToolResultEvent,
)
from calfkit.models.error_report import ErrorReport
from calfkit.models.payload import TextPart

from calfcord.bridge.step_events import StepEvent, normalize_run_event, normalize_terminal


def test_tool_call_normalizes_with_dict_args() -> None:
    e = ToolCallEvent(
        correlation_id="c1",
        depth=1,
        frame_id="f",
        emitter="alice",
        tool_call_id="t1",
        name="message_agent",
        args={"name": "scribe", "message": "hi"},
    )
    assert normalize_run_event(e) == StepEvent(
        kind="tool_call",
        correlation_id="c1",
        depth=1,
        emitter="alice",
        tool_call_id="t1",
        name="message_agent",
        args={"name": "scribe", "message": "hi"},
    )


def test_tool_call_normalizes_json_string_args() -> None:
    e = ToolCallEvent(
        correlation_id="c1",
        depth=1,
        frame_id="f",
        emitter="alice",
        tool_call_id="t1",
        name="message_agent",
        args='{"name": "scribe", "message": "hi"}',
    )
    s = normalize_run_event(e)
    assert s is not None and s.args == {"name": "scribe", "message": "hi"}


def _tool_result(outcome: str) -> ToolResultEvent:
    return ToolResultEvent(
        correlation_id="c1",
        depth=1,
        frame_id="f",
        emitter="scribe",
        tool_call_id="t1",
        name="scribe",
        parts=[TextPart(text="the summary")],
        outcome=outcome,  # type: ignore[arg-type]
    )


def test_tool_result_renders_text_and_success_outcome() -> None:
    s = normalize_run_event(_tool_result("success"))
    assert s is not None
    assert (s.kind, s.text, s.tool_call_id, s.outcome) == ("tool_result", "the summary", "t1", "success")


def test_tool_result_preserves_failed_outcome() -> None:
    """The three-valued calfkit outcome must survive the seam 1:1 — a lossy
    ``is_error`` bool would collapse ``failed`` and ``denied`` together."""
    s = normalize_run_event(_tool_result("failed"))
    assert s is not None and s.outcome == "failed"


def test_tool_result_preserves_denied_outcome() -> None:
    s = normalize_run_event(_tool_result("denied"))
    assert s is not None and s.outcome == "denied"


def test_agent_message_concatenates_text_parts() -> None:
    e = AgentMessageEvent(
        correlation_id="c1",
        depth=0,
        frame_id="f",
        emitter="alice",
        parts=[TextPart(text="think "), TextPart(text="hard")],
    )
    s = normalize_run_event(e)
    assert s is not None and s.kind == "agent_message" and s.text == "think hard"


def test_handoff_normalizes() -> None:
    e = HandoffEvent(correlation_id="c1", depth=0, frame_id="f", emitter="alice", target="scribe", reason="yours")
    s = normalize_run_event(e)
    assert s is not None and s.kind == "handoff" and s.target == "scribe" and s.reason == "yours"


def test_run_completed_returns_none() -> None:
    """The terminal RunCompleted carries no ``kind`` — the seam must return None so
    the handler drain skips it (the answer arrives via ``result()``). A refactor to
    direct ``event.kind`` access would AttributeError on every terminal and crash
    the whole drain, posting no reply."""
    e = RunCompleted(output="done", correlation_id="c1", agent="scribe", _envelope=None)
    assert normalize_run_event(e) is None


def test_run_failed_returns_none() -> None:
    e = RunFailed(report=ErrorReport(error_type="calf.test.boom"), correlation_id="c1")
    assert normalize_run_event(e) is None


# --- the terminal seam (ADR-0025) ------------------------------------------
# The trace's seal reads the terminal the drain ALREADY receives and discards.
# It stays behind this seam because step_events is the only module that knows
# calfkit's event types (spec §5.1). ``result()`` remains the authority for the
# reply and the fault notice — this only answers "did it fail?".


def test_normalize_terminal_reports_a_completed_run() -> None:
    e = RunCompleted(output="done", correlation_id="c1", agent="scribe", _envelope=None)
    assert normalize_terminal(e) is False


def test_normalize_terminal_reports_a_failed_run() -> None:
    e = RunFailed(report=ErrorReport(error_type="calf.test.fault"), correlation_id="c1")
    assert normalize_terminal(e) is True


def test_normalize_terminal_ignores_step_events() -> None:
    # None means "not a terminal" — distinct from False ("terminal, succeeded"),
    # which is why this returns bool | None rather than a bare bool.
    e = AgentMessageEvent(correlation_id="c1", depth=0, frame_id="f", emitter="a", parts=[TextPart(text="hi")])
    assert normalize_terminal(e) is None


def test_the_two_seams_agree_on_what_a_terminal_is() -> None:
    # normalize_run_event returns None for terminals precisely because they are
    # normalize_terminal's business. If that ever drifts, one of them silently
    # drops an event.
    for terminal in (
        RunCompleted(output="", correlation_id="c1", agent=None, _envelope=None),
        RunFailed(report=ErrorReport(error_type="calf.test.fault"), correlation_id="c1"),
    ):
        assert normalize_run_event(terminal) is None
        assert normalize_terminal(terminal) is not None

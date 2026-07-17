"""The persisted transcript's tree renderer — pure, no Discord, no Kafka, no
state (spec §5.1).

**Full transcript** — :func:`_render_tree_blocks` projects a turn's
``message_history`` slice into the Claude-Code-style ``● tool(args)`` /
``⎿ result`` blocks; its block COUNT gates whether the reply poster persists a
transcript row (for tool-call replay). It operates on ``Sequence[ModelMessage]``
because it renders persisted deltas, and its output is byte-for-byte stable so
stored transcripts keep rendering the same.

This module once also held the live step renderer. That surface is now row
values (:mod:`calfcord.bridge.trace_rows`) posted by
:class:`~calfcord.bridge.trace.StepTraceRenderer`: rows are markdown rather than
fenced code, so the two shared nothing once the split landed.

Everything here is pure: no I/O, no time, no mutable module state.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from typing import Any, Final

from calfkit._vendor.pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)

logger = logging.getLogger(__name__)

# --- Full transcript tree renderer (the persisted step transcript) ----------
# Renders the turn's steps as a Claude-Code-style trace: model text as prose,
# each tool call as ``● tool(args)`` with its result nested under ``⎿``. This is
# the FULL trace — NO per-part truncation. One string per visual block (a prose
# block, or a call+return pair); the block COUNT gates whether the reply poster
# persists the turn's transcript row (for tool-call replay), so a tool call and
# its result count as ONE step.

_TREE_CALL_MARKER: Final[str] = "●"
_TREE_RETURN_MARKER: Final[str] = "⎿"

_TRIPLE_BACKTICK_RUN: Final[re.Pattern[str]] = re.compile(r"`{3,}")


# --- Shared low-level text helpers ------------------------------------------


def _fence_safe(content: str) -> str:
    """Neutralize runs of 3+ backticks so embedded fences can't break out.

    Discord closes a ``` code block at the next run of three-or-more
    backticks regardless of the opening fence's length, so a triple-backtick
    inside tool output would otherwise terminate the block early and spill the
    remainder as raw markdown. Weaving a zero-width space between the backticks
    of any 3+ run leaves the text visually identical while ensuring no raw
    ``` survives to close the fence. Single/double backticks are left untouched
    — they render literally inside a block. (The only cost: a steps.md copied
    from output that itself contained ``` carries invisible zero-width spaces.)
    """
    return _TRIPLE_BACKTICK_RUN.sub(lambda m: "\u200b".join("`" * len(m.group())), content)


def _fenced(content: str) -> str:
    """Wrap ``content`` in a code fence, neutralizing any inner ``` first."""
    return f"```\n{_fence_safe(content)}\n```"


def _format_call_args(args: dict[str, Any]) -> str:
    """Render tool-call args as keyword form: ``k=<json-value>, …``.

    ``args`` is the already-coerced argument dict from :func:`_tool_call_args`,
    which guarantees a dict (an unparseable arg is coerced to ``{}`` and renders
    as ``name()``). A flat object renders as ``city="Tokyo", n=5`` — each value
    JSON-encoded, so strings keep their quotes and booleans read as
    ``true``/``false``; nested values use compact separators to keep the line
    tight. Byte fidelity is preserved: JSON already escapes real newlines, so
    the signature stays one line and inner spacing in string values survives.
    """
    if not args:
        return ""
    pairs: list[str] = []
    for key, value in args.items():
        try:
            rendered_value = json.dumps(value, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            rendered_value = str(value)
        pairs.append(f"{key}={rendered_value}")
    return ", ".join(pairs)


def _tool_call_args(call: ToolCallPart) -> dict[str, Any]:
    """Best-effort argument dict for a transcript ``ToolCallPart`` (never raises).

    The tree renderer projects persisted ``message_history``, so it coerces a
    tool call's raw args here: a non-object or unparseable arg becomes ``{}``
    (rendered as ``name()``) rather than raising and losing the WHOLE stored
    transcript over one malformed call. ``args_as_dict`` is pydantic-ai's
    canonical accessor — it returns ``{}`` for empty args and asserts on a
    bare list/scalar — so well-formed object args render byte-for-byte as before.
    """
    try:
        args = call.args_as_dict()
    except Exception:
        # A bare list/scalar makes args_as_dict assert; malformed JSON makes it
        # raise. Either way the args aren't an object → render as ``name()``.
        return {}
    return args if isinstance(args, dict) else {}


# --- Full transcript tree renderer ------------------------------------------


def _render_text_part(part: TextPart) -> str | None:
    """Render a ``TextPart`` into a transcript block, or ``None`` to skip.

    Whitespace-only content is skipped — empty preambles are common
    when the model emits a tool call with no narrative.
    """
    text = part.content.strip()
    if not text:
        return None
    return text


def _tool_tree_block(call: ToolCallPart, ret: ToolReturnPart | None) -> str:
    """Render a tool call and its (optional) result as one fenced tree block.

    ``● tool(args)`` on the first line; when a matching return is present, its
    result is nested under ``⎿`` with continuation lines aligned beneath the
    first result character. Args use the keyword form WITHOUT whitespace
    collapsing — this is the full view, so byte fidelity is preserved (real
    newlines are already JSON-escaped, so the signature stays one line). No
    truncation: the only bound is the overall message cap enforced upstream.
    """
    sig = f"{call.tool_name}({_format_call_args(_tool_call_args(call))})"
    lines = [f"{_TREE_CALL_MARKER} {sig}"]
    if ret is not None:
        first, *rest = ret.model_response_str().split("\n")
        lines.append(f"  {_TREE_RETURN_MARKER}  {first}")
        lines.extend(f"     {line}" for line in rest)
    return _fenced("\n".join(lines))


def _return_tree_block(ret: ToolReturnPart) -> str:
    """Render an orphan tool return (no call with its id in the slice) standalone.

    Practically unreachable — a tool call and its return live in the same
    agent run, after the history cursor, so they're sliced together. Rendered
    defensively so an orphan return is never silently dropped, which would also
    skew the step count.
    """
    first, *rest = ret.model_response_str().split("\n")
    lines = [f"{_TREE_RETURN_MARKER}  {first}"]
    lines.extend(f"   {line}" for line in rest)
    return _fenced("\n".join(lines))


def _render_tree_blocks(messages: Sequence[ModelMessage]) -> list[str]:
    """Project the turn's ``message_history`` slice into full tree blocks.

    The source of the reply poster's step COUNT (``len(...)``), which gates
    whether a transcript row is persisted for tool-call replay. Walks the delta
    in order, emitting one string per visual block:

    * a model ``TextPart`` → a prose block (whitespace-only skipped);
    * a ``ToolCallPart`` → ``● tool(args)`` with its matched ``ToolReturnPart``
      (looked up by ``tool_call_id``) nested under ``⎿`` — a call and its
      result are ONE block, so the step count credits a tool use once.

    Skips non-rendered parts (``ThinkingPart``, ``FilePart``,
    ``BuiltinTool*Part``, ``UserPromptPart`` / ``SystemPromptPart``,
    ``RetryPromptPart``).

    Pairing is purely by id and independent of message order: a return is
    folded into its call iff a call with that id exists anywhere in the slice;
    a return whose call is absent renders standalone (so nothing is dropped,
    and the orphan path can't double-render a return that arrives before its
    call). Output order follows message order. Duplicate ``tool_call_id``s
    don't occur in well-formed pydantic-ai history; on a collision the last
    return for an id wins.

    Caller wraps this in a try/except — ``model_response_str`` can raise on
    malformed payloads.
    """
    # Two index passes (order-independent): which ids have a call in the
    # slice, and the return for each id. A return is then an orphan iff its id
    # has no call here — decided without relying on walk order.
    call_ids: set[str] = set()
    returns_by_id: dict[str, ToolReturnPart] = {}
    for msg in messages:
        if isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, ToolCallPart):
                    call_ids.add(part.tool_call_id)
        elif isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, ToolReturnPart):
                    returns_by_id[part.tool_call_id] = part

    out: list[str] = []
    for msg in messages:
        if isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, TextPart):
                    rendered = _render_text_part(part)
                    if rendered is not None:
                        out.append(rendered)
                elif isinstance(part, ToolCallPart):
                    out.append(_tool_tree_block(part, returns_by_id.get(part.tool_call_id)))
        elif isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, ToolReturnPart) and part.tool_call_id not in call_ids:
                    out.append(_return_tree_block(part))
    return out


# --- Components-V2 step-message renderer ------------------------------------
# Each renderable step becomes one or more persistent Components-V2 messages
# (a red ``Container`` of ``TextDisplay``s). Tool calls/results are short-form
# with the name in monospace; an ``agent_message`` carries the FULL prose,
# chunked to the v2 cap; a ``handoff`` shows the bare target. Unknown/future
# kinds are a safe no-op so a new calfkit event can never fault the drain.

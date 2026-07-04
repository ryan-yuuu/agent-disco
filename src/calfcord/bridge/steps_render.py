"""Pure step-render helpers — no Discord, no Kafka, no state (spec §5.1).

Two render surfaces share this module:

* **Live step messages** — :func:`render_step_message` turns ONE normalized
  :class:`~calfcord.bridge.step_events.StepEvent` into the body/bodies of the
  persistent Components-V2 step messages: a ``🔧 `tool` called`` /
  ``✅ `tool` returned`` short line, a ``➡️ handed off to `peer``` note, or the
  full agent text split into ≤-cap chunks. The stateful posting lives in
  :mod:`calfcord.bridge.progress`.
* **Full transcript** — :func:`_render_tree_blocks` projects a turn's
  ``message_history`` slice into the Claude-Code-style ``● tool(args)`` /
  ``⎿ result`` blocks; its block COUNT gates whether the reply poster persists a
  transcript row (for tool-call replay). This surface operates on
  ``Sequence[ModelMessage]`` because it renders persisted deltas, and its output
  is byte-for-byte stable so stored transcripts keep rendering the same.

Everything here is pure: no I/O, no time, no mutable module state. That keeps
both surfaces trivially unit-testable.
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

from calfcord.bridge.step_events import StepEvent

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

_V2_TEXT_LIMIT: Final[int] = 4000
"""Discord's Components-V2 hard cap — per ``TextDisplay`` AND per whole message
(the sum of all text across every container/text display). Verified against the
live API (4000 accepted, 4001 rejected); discord.py does not enforce it
client-side. The renderer honors it *transitively*: it chunks agent text to
:data:`_V2_CHUNK` (< this) and posts one ``TextDisplay`` per message, so no
explicit 4000-char check is needed."""

_V2_CHUNK: Final[int] = 3900
"""Chunk target for a full ``agent_message`` body — one chunk per v2 message,
kept under :data:`_V2_TEXT_LIMIT` for headroom."""


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
    # separate messages). This upholds "render_step_message never emits an empty
    # body". Non-empty input always leaves at least one non-empty chunk.
    return [chunk for chunk in chunks if chunk]


def render_step_message(step: StepEvent) -> list[str]:
    """Render ONE :class:`StepEvent` into Components-V2 message bodies.

    Returns one body per message to post (usually a single-element list; a long
    ``agent_message`` yields several). An empty list means "post nothing".
    """
    if step.kind == "tool_call":
        return [f"🔧 `{step.name}` called"]
    if step.kind == "tool_result":
        if step.is_error:
            return [f"❌ `{step.name}` errored"]
        return [f"✅ `{step.name}` returned"]
    if step.kind == "handoff":
        target = (step.target or "").removeprefix("/")
        return [f"➡️ handed off to `{target}`"]
    if step.kind == "agent_message":
        text = step.text.strip()
        if not text:
            return []
        return _chunk_text(text, _V2_CHUNK)
    # Defensive: unreachable for the current StepKind set, but a future calfkit
    # kind (e.g. ``agent_thinking``) must render nothing rather than fault the
    # drain — logged so the coverage gap is visible.
    logger.warning("steps: no v2 renderer for step kind %r; rendering nothing", step.kind)
    return []

"""Live-progress renderer: persistent Components-V2 step messages (spec §5.2).

The bridge's :class:`~calfcord.bridge.mention_handler.MentionHandler` drains a
run's ``stream()`` and, for every non-A2A
:class:`~calfcord.bridge.step_events.StepEvent`, calls
:meth:`ProgressRenderer.on_step`; a ``finally`` always calls
:meth:`ProgressRenderer.finish`.

Each renderable step is rendered by
:func:`~calfcord.bridge.steps_render.render_step_message` to one or more short
bodies — a ``🔧 `tool` called`` / ``✅ `tool` returned`` line, a
``➡️ handed off to `peer``` note, or the full agent text split into
≤-cap chunks — and each body is POSTED as a persistent, inline **Components-V2**
message under the emitting agent's persona. Nothing is edited and nothing is
deleted: the messages persist as the turn's visible trace. Because a v2 message
carries no ``content`` (only components), the bridge's history fetcher excludes
these from an agent's ``message_history`` — the model's tool memory rides the
separate transcript replay, so the display never double-counts.

**No lifecycle state.** Unlike the old transient-message renderer there is no
per-correlation entry, no message-id tracking, no debounce, and no post/edit/
delete dance — so a mid-run restart strands nothing.

**Persona** is resolved per step. For agent-authored steps (``agent_message``,
``handoff``) the emitter's persona is used so a peer after a handoff stamps its
own identity. For tool steps (``tool_call``, ``tool_result``) the *owning agent*
—the agent currently in control of the run—is used instead, because a tool is a
utility, not a conversational participant: posting ``✅ `todo` returned`` under a
``todo`` persona would create a spurious identity in Discord. The owning agent is
tracked by the handler (initialized to the mention target, updated on handoff)
and passed in via ``owning_agent``.

**Failure semantics.** Every send is best-effort
(:func:`_best_effort_progress`): a transient/gone step message must never crash
the run or affect the terminal reply. A failed *Discord* send drops just that one
line and the loop keeps posting the rest. A systematic non-Discord error (e.g. a
sender that was never started, a non-text channel) is not caught here — it
propagates to the drain's own ``except Exception``, which drops the whole step
(logged) and keeps draining; the terminal reply is unaffected either way.

**Typing** is disabled for now — the fire call is commented out; the notifier is
still accepted (dormant) for a one-line re-enable.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from typing import TYPE_CHECKING

import discord

from calfcord.bridge.persona_resolve import persona_for
from calfcord.bridge.steps_render import render_step_message

if TYPE_CHECKING:
    from calfcord.bridge.mention_handler import MentionRequest
    from calfcord.bridge.step_events import StepEvent
    from calfcord.discord.persona import DiscordPersonaSender
    from calfcord.discord.typing import TypingNotifier

logger = logging.getLogger(__name__)

_V2_ACCENT = discord.Colour(0xE74C3C)
"""Accent stripe (Discord red) on every step message's container."""


class _StepView(discord.ui.LayoutView):
    """One step message: an accent :class:`~discord.ui.Container` wrapping the
    rendered body as a single :class:`~discord.ui.TextDisplay`. ``timeout=None``
    — the view has no interactive components, so it never needs dispatching."""

    def __init__(self, body: str) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(content=body),
                accent_colour=_V2_ACCENT,
            )
        )


def _build_step_view(body: str) -> discord.ui.LayoutView:
    """Wrap one rendered step body in a Components-V2 accent container."""
    return _StepView(body)


async def _best_effort_progress[T](coro: Awaitable[T], *, channel_id: int) -> T | None:
    """Await a best-effort step-message send, swallowing the usual failures so a
    transient/gone message can never crash the run. Returns the call's result,
    or ``None`` if it failed.

    ``NotFound`` (already gone) is DEBUG; ``Forbidden`` and the broader
    ``DiscordException`` (which also funnels the sibling ``RateLimited``, NOT a
    subclass of ``HTTPException``) are WARNING. ``CancelledError`` is a
    ``BaseException`` and is intentionally not caught, so shutdown stays clean.
    """
    try:
        return await coro
    except discord.NotFound:
        logger.debug("progress: step post hit NotFound channel_id=%d (already gone)", channel_id)
    except discord.Forbidden:
        logger.warning("progress: step post Forbidden channel_id=%d", channel_id)
    except discord.DiscordException as e:
        logger.warning(
            "progress: step post failed channel_id=%d status=%s: %s",
            channel_id,
            getattr(e, "status", None),
            e,
        )
    return None


class ProgressRenderer:
    """Posts each run step as a persistent Components-V2 message.

    Satisfies the ``ProgressRenderer`` protocol the
    :class:`~calfcord.bridge.mention_handler.MentionHandler` injects. Construct
    once per bridge process from the REST-only persona sender and (optionally) a
    typing notifier (currently dormant).
    """

    def __init__(self, persona_sender: DiscordPersonaSender, typing_notifier: TypingNotifier | None = None) -> None:
        self._persona_sender = persona_sender
        # Dormant: typing is disabled for now (see on_step). Kept so re-enabling
        # is a one-line change and the gateway wiring stays intact.
        self._typing = typing_notifier

    async def on_step(self, step: StepEvent, req: MentionRequest, *, owning_agent: str) -> None:
        """Post one persistent v2 message per rendered body for this step.

        Renders the step (:func:`render_step_message`) — a tool call/result short
        line, a handoff note, or the full agent text split into ≤-cap chunks —
        and posts each body under the appropriate persona, into the originating
        thread when the wire came from one, else the parent channel. A step that
        renders nothing posts nothing. Every send is best-effort.

        ``owning_agent`` is the agent currently in control of the run (the mention
        target initially, updated on handoff). It is used as the persona for
        ``tool_call``/``tool_result`` steps so tool progress lines don't appear
        under the tool's own identity. ``agent_message``/``handoff`` steps keep
        ``step.emitter`` — the genuine peer-emitter cases.
        """
        bodies = render_step_message(step)
        if not bodies:
            return
        thread_id = req.source_channel_id if req.source_channel_id != req.channel_id else None
        if step.kind in ("tool_call", "tool_result"):
            persona = persona_for(owning_agent)
        else:
            persona = persona_for(step.emitter)
        # Typing disabled for now — re-enable by uncommenting (the notifier is
        # still wired through the gateway, just dormant):
        # if self._typing is not None:
        #     self._typing.fire(thread_id or req.channel_id)
        for body in bodies:
            await _best_effort_progress(
                self._persona_sender.send_components(
                    persona=persona,
                    channel_id=req.channel_id,
                    view=_build_step_view(body),
                    thread_id=thread_id,
                ),
                channel_id=req.channel_id,
            )

    async def finish(self, correlation_id: str) -> None:
        """No-op: step messages are persistent, so there is nothing to tear down.

        Kept to satisfy the ``ProgressRenderer`` protocol — the handler calls it
        in a ``finally``. (The old transient post/edit/delete lifecycle is gone.)
        """

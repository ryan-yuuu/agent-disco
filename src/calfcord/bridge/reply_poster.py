"""Post an agent's final reply to Discord under its persona, chunk by chunk.

On the calfkit 0.12 caller surface the final reply is the return value of
``handle.result()`` (awaited by the :class:`~calfcord.bridge.mention_handler.MentionHandler`),
not a Kafka outbox message. Every reply is delivered as consecutive
≤ :data:`~calfcord.discord.chunking.CHUNK_SAFE_SIZE` chunks — one chunk for a
normal-length reply — with the first chunk carrying the inline-reply anchor and
(if the turn used tools) the durable transcript row (for tool-call replay).
Chunking is the only delivery mechanism: there is no retry that re-invokes the
agent to shorten a rejected reply.

Per-chunk failures are logged independently so partial delivery survives; a
single Discord 5xx per chunk is smoothed with one delayed re-send.
:meth:`post_reply` reports ``"posted"`` / ``"empty"`` / ``"lost"`` so the
handler can set the sticky owner or surface an operator notice. ``post_notice``
posts a plain bridge-authored message (no persona) for operator-facing notices
(roster unavailable, no agent online, agent error).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Final, Literal

import discord
from calfkit._vendor.pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    UserPromptPart,
)

from calfcord.bridge.mention_handler import MentionRequest
from calfcord.bridge.transcript_tree import _render_tree_blocks
from calfcord.bridge.transcripts import TranscriptRow, TranscriptStoreLike
from calfcord.bridge.wire import WireMessage
from calfcord.discord.chunking import chunk_split
from calfcord.discord.persona import DiscordPersonaSender, Persona, ReplyContext

logger = logging.getLogger(__name__)

_SERVER_ERROR_RETRY_DELAY_SECONDS = 2.0
"""Delay before the one extra attempt on a first-try Discord 5xx (matches the
old outbox value; a single-worker poster can't afford a long sleep)."""

_OPERATOR_ACTIONABLE_STATUSES: Final[frozenset[int]] = frozenset({401, 403})
"""Discord statuses that mean the bot is *misconfigured* — 401 (bad token)
and 403 (missing Manage Webhooks) — not transient. They silently break EVERY
reply, so they log at ERROR (surfacing in alerting); rate-limit / 404 / 5xx are
transient or environmental and stay at WARNING."""


def _turn_delta(result: Any, initial_len: int) -> list[ModelMessage]:
    """THIS turn's structured slice — its tool calls/returns, for next-turn replay.

    The cumulative ``message_history`` minus three things: the channel-history
    prefix (``initial_len``), the turn's own committed user prompt, and the
    trailing final-answer ``ModelResponse``. Empty for a pure-text turn.

    The prompt has to go. ``Client.start`` unconditionally stages it
    (``client/caller.py``) and the agent commits it to the history BEFORE the
    model loop (``nodes/agent.py``), so it sits at exactly ``initial_len`` — a
    plain ``[initial_len:-1]`` captures it, and replay then splices it back into
    a history that already carries that turn as a channel record. The model sees
    the prompt twice (pydantic-ai merges the adjacent requests, so it reads as
    one doubled message) and the envelope pays for it twice.

    Skipped by inspection rather than ``initial_len + 1``: if calfkit ever stops
    committing the prompt, this degrades to keeping the whole delta rather than
    silently eating the turn's first tool call.
    """
    history = result.message_history
    if not history:
        return []
    start = initial_len
    if start < len(history) and _is_user_prompt(history[start]):
        start += 1
    return list(history[start:-1])


def _is_user_prompt(message: ModelMessage) -> bool:
    return isinstance(message, ModelRequest) and any(
        isinstance(p, UserPromptPart) for p in message.parts
    )


def _render_step_count(delta: list[ModelMessage]) -> int:
    """Count renderable step blocks in ``delta``, defensively (a tool call + its
    result render as one block). ``_render_tree_blocks`` can raise on malformed
    args, so a failure degrades to zero steps — the reply still posts, but no
    transcript row is written, so this turn's tool calls won't be available for
    replay on the next turn."""
    try:
        return len(_render_tree_blocks(delta))
    except Exception:
        logger.exception(
            "reply poster: step-count render raised; posting without a transcript row "
            "(tool-call replay will miss this turn)"
        )
        return 0


class ReplyPoster:
    """Posts agent replies under their persona as chunked messages."""

    def __init__(self, persona_sender: DiscordPersonaSender, transcript_store: TranscriptStoreLike) -> None:
        self._personas = persona_sender
        self._store = transcript_store

    async def post_reply(
        self,
        req: MentionRequest,
        persona: Persona,
        result: Any,
        *,
        initial_len: int,
        correlation_id: str,
    ) -> Literal["posted", "empty", "lost"]:
        """Chunk-split ``result``'s final answer and post each chunk under ``persona``.

        The first chunk carries the inline-reply anchor + (if the turn used
        tools) the persisted transcript row; later chunks are bare
        continuations. Per-chunk failures are logged independently so partial
        delivery survives.

        Returns ``"posted"`` if at least one chunk was delivered, ``"empty"``
        if there was nothing to post (Discord rejects an empty webhook send;
        nothing to deliver is a no-op, not a loss), or ``"lost"`` if every
        chunk failed so the handler can surface an operator notice rather than
        ghost the user.
        """
        text = (result.output or "").strip()
        chunks = chunk_split(text)
        if not chunks:
            return "empty"
        wire = req.wire  # already a validated WireMessage (built at the gateway)
        delta = _turn_delta(result, initial_len)
        rendered = _render_step_count(delta)
        write_transcript = bool(rendered) and self._store.enabled
        total = len(chunks)
        posted_any = False
        failures: list[int | None] = []
        for i, chunk in enumerate(chunks):
            try:
                sent = await _send_with_one_retry_on_outage(
                    self._personas,
                    persona=persona,
                    channel_id=wire.channel_id,
                    content=chunk,
                    reply_to=ReplyContext.from_wire(wire) if i == 0 else None,
                    thread_id=wire.thread_id,
                )
            except (discord.DiscordException, TypeError, RuntimeError) as e:
                # TypeError/RuntimeError cover non-Discord sender errors (non-text
                # channel / sender not started). Auth/permission failures are
                # operator-actionable misconfigurations that silently break every
                # reply -> ERROR (so alerting sees them); rate-limit / 404 / 5xx
                # are transient -> WARNING.
                status = getattr(e, "status", None)
                failures.append(status)
                log = logger.error if status in _OPERATOR_ACTIONABLE_STATUSES else logger.warning
                log(
                    "reply chunk %d/%d failed channel_id=%s correlation_id=%s status=%s: %s",
                    i + 1,
                    total,
                    wire.channel_id,
                    correlation_id,
                    status,
                    e,
                    exc_info=True,
                )
                continue
            posted_any = True
            if i == 0 and write_transcript:
                await _write_transcript(
                    self._store,
                    correlation_id=correlation_id,
                    wire=wire,
                    agent_id=persona.name,
                    final_message_id=sent.id,
                    delta=delta,
                )
        if not posted_any:
            dominant = max(set(failures), key=failures.count)
            logger.error(
                "reply delivered 0/%d chunks correlation_id=%s dominant_status=%s; reply fully lost",
                total,
                correlation_id,
                dominant,
            )
            return "lost"
        return "posted"

    async def post_notice(self, req: MentionRequest, text: str) -> None:
        """Post a plain operator-facing notice as an inline reply (no persona).

        Notices (roster unavailable, no agent online, agent error) are bridge-
        authored, not agent output, so they go via the triggering message's native
        inline reply rather than a persona webhook. Best-effort: a failure to post
        a notice must not escape into the handler."""
        try:
            await req.reply_target.reply(text)
        except discord.DiscordException:
            logger.warning("failed to post notice to channel_id=%s", req.channel_id, exc_info=True)


async def _send_with_one_retry_on_outage(
    persona_sender: DiscordPersonaSender,
    *,
    persona: Persona,
    channel_id: int,
    content: str,
    reply_to: ReplyContext | None,
    thread_id: int | None = None,
) -> Any:
    """Send via the persona webhook with exactly one extra attempt on a first-try
    5xx (after a short delay). Any other first-try error, or a second-try error,
    is re-raised for the caller to triage. This is transport smoothing for a
    Discord-side blip — the same bytes are re-sent; no agent is involved."""
    try:
        return await persona_sender.send(
            persona=persona,
            channel_id=channel_id,
            content=content,
            reply_to=reply_to,
            thread_id=thread_id,
        )
    except discord.DiscordServerError as e:
        logger.warning(
            "discord 5xx on persona post; retrying once after %.1fs status=%s: %s",
            _SERVER_ERROR_RETRY_DELAY_SECONDS,
            e.status,
            e,
        )
    await asyncio.sleep(_SERVER_ERROR_RETRY_DELAY_SECONDS)
    return await persona_sender.send(
        persona=persona,
        channel_id=channel_id,
        content=content,
        reply_to=reply_to,
        thread_id=thread_id,
    )


async def _write_transcript(
    transcript_store: TranscriptStoreLike,
    *,
    correlation_id: str,
    wire: WireMessage,
    agent_id: str,
    final_message_id: int,
    delta: list[ModelMessage],
) -> None:
    """Persist the completed turn's transcript row (best-effort, idempotent on
    ``correlation_id``). A store failure is logged and swallowed — the reply is
    already posted; tool-call replay simply finds no row for this turn
    (documented degradation)."""
    try:
        delta_json = ModelMessagesTypeAdapter.dump_json(delta).decode()
        await transcript_store.write_turn(
            TranscriptRow(
                correlation_id=correlation_id,
                conversation_key=str(wire.source_channel_id or wire.channel_id),
                agent_id=str(agent_id),
                final_message_id=str(final_message_id),
                delta_json=delta_json,
                created_at=int(time.time()),
            )
        )
    except Exception:
        logger.exception(
            "failed to write transcript correlation_id=%s reply_id=%s; tool-call replay will miss this turn",
            correlation_id,
            final_message_id,
        )

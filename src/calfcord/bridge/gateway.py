"""Discord ingress gateway daemon and CLI entry point.

Holds the long-lived gateway WebSocket and wires the per-``!mention`` orchestration
together on the calfkit 0.12 **caller surface**. Run via::

    uv run calfkit-bridge

The daemon depends on a running Kafka broker reachable at ``CALF_HOST_URL``
(defaults to ``localhost``) and a Discord bot configured via the ``DISCORD_*``
environment variables (see ``.env.example``).

The bridge is a pure calfkit :class:`~calfkit.client.Client` (no embedded Worker,
no consumers). For each ``!mention`` it builds a :class:`MentionRequest` and runs
:class:`~calfcord.bridge.mention_handler.MentionHandler.handle` as a tracked task:
the handler resolves the target against the live mesh roster, ``start()``s the
agent, drains its run ``stream()`` (live progress + A2A projection), and posts the
terminal reply under the responding agent's persona. Non-``!mention`` ("ambient")
messages go unanswered (C2). The bridge owns SIGINT/SIGTERM for its foreground
(the Discord gateway) and tears down by cancelling in-flight handler tasks then
closing the client.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import discord
from aiokafka.errors import KafkaConnectionError
from calfkit.client import Client, MeshViewConfig

from calfcord._provisioning import PROVISIONING
from calfcord.agents.memory import MemoryPromptDeps
from calfcord.bridge.a2a_project import A2AProjector
from calfcord.bridge.egress import A2AChannelResolver, create_thread_from_message
from calfcord.bridge.history import ChannelHistoryFetcher, DiscordHistoryProvider
from calfcord.bridge.mention_handler import MentionHandler, MentionRequest
from calfcord.bridge.normalizer import MessageNormalizer, extract_mention_ids
from calfcord.bridge.overrides import EffortOverrides
from calfcord.bridge.progress import ProgressRenderer
from calfcord.bridge.reply_poster import ReplyPoster
from calfcord.bridge.roster import MeshRoster
from calfcord.bridge.settings import (
    BridgeSettings,
    load_settings,
    resolve_settings_path,
)
from calfcord.bridge.slash import SlashCommandManager
from calfcord.bridge.transcripts import (
    NullTranscriptStore,
    TranscriptStore,
    TranscriptStoreLike,
)
from calfcord.bridge.wire import WireMessage
from calfcord.discord.persona import DiscordPersonaSender
from calfcord.discord.settings import DiscordSettings
from calfcord.discord.typing import TypingNotifier
from calfcord.health.heartbeat import write_beat
from calfcord.health.refresher import run_refresher

logger = logging.getLogger(__name__)

# The component name the bridge writes its heartbeat under and that
# ``disco _healthcheck bridge`` reads back (design §4.2 / §12.1).
_HEALTH_COMPONENT = "bridge"

_SEEN_MESSAGE_IDS_CAPACITY = 1024

# The only Discord message types that carry human intent and may therefore drive
# routing. Everything else is constructed by Discord (or by an interaction), yet
# is stamped with the *human* as its author and can carry non-empty text — so by
# the time one reaches the routing decision it is indistinguishable from real
# input. Discord pushes these over the same MESSAGE_CREATE gateway event, so
# ingress is where they have to be caught.
#
# ``thread_created`` (18) is why this exists: opening a thread via a channel's
# "new thread" button posts one into the PARENT channel whose ``content`` is the
# thread's TITLE (discord.py renders it "X started a thread: **<content>**").
# Unfiltered, that title routes in the parent — so the parent's sticky agent
# answers a thread the user opened for a different agent, and a title carrying a
# ``!mention`` dispatches that agent a second time outside the thread.
#
# An allowlist, not ``Message.is_system()``: that helper reports
# ``thread_starter_message``, ``poll_result``, ``chat_input_command`` and
# ``context_menu_command`` as NON-system, which would let them through here.
_ROUTABLE_MESSAGE_TYPES = frozenset(
    {
        discord.MessageType.default,
        discord.MessageType.reply,
    }
)

# Durable, fixed inbox topic for the bridge's caller surface. A stable name
# (vs an auto-generated per-restart one) avoids leaking orphan topics on a
# no-auto-delete broker (Tansu); only one bridge runs, so there is no contention.
_BRIDGE_INBOX_TOPIC = "discord.bridge.inbox"

# Mesh liveness staleness (R-A6): the calfkit default of 3x30s heartbeats. A
# gracefully-stopped agent tombstones immediately; this only gates the window
# after an ungraceful crash.
_MESH_STALE_AFTER_SECONDS = 90.0

_UNSTICK_COMMAND_RE = re.compile(r"^\s*!unstick(?:\s|$)", re.IGNORECASE)
# Mirrors ``mention_handler._ROSTER_UNAVAILABLE``'s shape: the two notices cover the
# same class of transient infra blip, and a user hitting both should not have to work
# out that they mean the same thing.
_BROKER_UNREACHABLE = (
    "I can't reach the message broker right now — it may be restarting. "
    "Please try again in a moment."
)

_UNSTICK_NOTICE = "Sticky replies cleared for this thread."
_NEW_THREAD_COMMAND_RE = re.compile(r"^\s*!new(?:\s|$)", re.IGNORECASE)
_NEW_THREAD_IN_THREAD_NOTICE = "`!new` can only be used from a parent channel, not inside a thread."
_NEW_THREAD_CREATE_FAILED_NOTICE = "I couldn't create a new thread for that message."
_NEW_THREAD_NO_AGENT_NOTICE = "No agent was mentioned and this parent channel does not have a sticky agent."
_NEW_THREAD_TITLE_LIMIT = 100

# A2A audit channel/category, moved from the tools service to the bridge (spec §10).
_A2A_CHANNEL_NAME_ENV = "CALFKIT_A2A_CHANNEL_NAME"
_A2A_CHANNEL_CATEGORY_ENV = "CALFKIT_A2A_CHANNEL_CATEGORY"
_A2A_CHANNEL_NAME_DEFAULT = "private-a2a-chats"


def _a2a_channel_name() -> str:
    """The unified A2A audit channel name (``CALFKIT_A2A_CHANNEL_NAME`` or default)."""
    value = os.getenv(_A2A_CHANNEL_NAME_ENV)
    return value.strip() if value and value.strip() else _A2A_CHANNEL_NAME_DEFAULT


def _a2a_category_name() -> str | None:
    """The optional Discord category for the A2A channel, or ``None`` when unset."""
    value = os.getenv(_A2A_CHANNEL_CATEGORY_ENV)
    return value.strip() if value and value.strip() else None


def _is_new_thread_command(content: str) -> bool:
    return _NEW_THREAD_COMMAND_RE.match(content) is not None


def _thread_title_from_content(content: str) -> str:
    title = content[:_NEW_THREAD_TITLE_LIMIT]
    return title or "!new"


def _resolve_health_home() -> Path:
    """Resolve the install home the heartbeat lands under, matching the reader.

    The ``disco _healthcheck bridge`` probe resolves the beat directory as
    ``_resolve_home() or Path()`` (``cli/main.py``): ``$CALFCORD_HOME`` when the
    shim exported it, else the launch directory. The bridge MUST mirror that
    exact resolution so the beat it writes lands where the probe looks for it; an
    empty ``CALFCORD_HOME=`` counts as unset (same guard as the CLI) rather than
    rooting state at ``/state/health``.
    """
    home = os.environ.get("CALFCORD_HOME")
    return Path(home) if home else Path()


@asynccontextmanager
async def _open_transcript_store(
    settings: DiscordSettings,
) -> AsyncIterator[TranscriptStoreLike]:
    """Open the transcript store, degrading to a no-op store on failure.

    Constructs the real :class:`TranscriptStore` and connects it. If the open
    fails (bad path, disk error, corrupt DB, …) the bridge MUST NOT abort — a
    crash here would take down all Discord routing, not just transcripts. Instead
    we log a loud ERROR and substitute a :class:`NullTranscriptStore` so the run
    continues with transcripts and tool-call replay disabled.
    Yields whichever store is in effect; the real store's connection (if any) is
    closed on exit, and ``NullTranscriptStore.close`` is a harmless no-op.
    """
    store = TranscriptStore(settings.transcript_db_path)
    yielded: TranscriptStoreLike = store
    try:
        try:
            await store.connect()
        except Exception:
            logger.error(
                "transcript store failed to open at %s — step transcripts and "
                "tool-call replay are DISABLED for this run",
                settings.transcript_db_path,
                exc_info=True,
            )
            yielded = NullTranscriptStore()
        yield yielded
    finally:
        await yielded.close()


async def _prune_on_startup(
    store: TranscriptStoreLike, settings: DiscordSettings
) -> None:
    """Best-effort startup sweep: drop transcript rows past the retention window.

    The bridge is the sole writer and restarts on every deploy, so a startup
    prune bounds the store's growth without a background task. Disabled when
    ``transcript_retention_days <= 0`` (keep forever). Best-effort: a prune
    failure must NEVER abort startup — any exception is logged and swallowed.
    """
    if settings.transcript_retention_days <= 0:
        return
    try:
        cutoff = int(time.time()) - settings.transcript_retention_days * 86400
        pruned = await store.prune_older_than(cutoff)
        if pruned:
            logger.info(
                "pruned %d transcript row(s) older than %d days",
                pruned,
                settings.transcript_retention_days,
            )
    except Exception:
        logger.exception("transcript retention prune failed at startup; continuing")


class DiscordIngressGateway:
    """Long-lived gateway daemon. Translates Discord ``!mention``s into agent runs."""

    def __init__(
        self,
        settings: DiscordSettings,
        *,
        calfkit_client: Client,
        persona_sender: DiscordPersonaSender,
        transcript_store: TranscriptStoreLike,
        roster: MeshRoster,
        overrides: EffortOverrides,
        a2a: A2AProjector,
        progress: ProgressRenderer,
        reply: ReplyPoster,
        memory_deps: MemoryPromptDeps,
        bridge_settings: BridgeSettings | None = None,
        sticky_store: Any | None = None,
    ) -> None:
        self._settings = settings
        self._bridge_settings = bridge_settings or BridgeSettings()
        self._transcript_store = transcript_store
        self._sticky_store = sticky_store
        self._reply = reply
        self._client = _GatewayClient(self)

        # The history fetcher holds the gateway's Discord client; it only calls
        # ``get_channel``/``fetch_channel`` at fetch time (inside ``_on_message``,
        # post-handshake), so constructing it here with the not-yet-connected
        # client is safe. Agent turns are recognized by bot-owned ``webhook_id``
        # (R-A3) via the persona sender's id set.
        fetcher = ChannelHistoryFetcher(self._client, persona_sender.owns_webhook)
        history = DiscordHistoryProvider(
            fetcher,
            transcript_store,
            max_json_bytes=self._bridge_settings.message_history.max_json_bytes,
        )
        handler_sticky = (
            sticky_store if self._bridge_settings.sticky_replies.enabled else None
        )
        self._handler = MentionHandler(
            client=calfkit_client,
            roster=roster,
            history=history,
            overrides=overrides,
            a2a=a2a,
            progress=progress,
            reply=reply,
            memory_deps=memory_deps,
            sticky=handler_sticky,
        )

        # The MessageNormalizer needs bot_user_id, known only at on_ready.
        self._message_normalizer: MessageNormalizer | None = None
        self._bot_user_id: int | None = None

        # Discord-connection liveness (design §12.1): ``_connected`` flips True on
        # on_ready/on_resumed and False on on_disconnect; the timer-refresher gates
        # every beat write on it, so a dropped gateway ages the beat past its TTL.
        self._connected: bool = False
        self._bot_identity: str | None = None

        self._slash = SlashCommandManager(
            client=self._client,
            overrides=overrides,
            owner_user_id=settings.owner_user_id,
            guild_id=settings.guild_id,
        )
        self._slash.register_thinking_effort()
        self._slash.register_clear()

        # In-flight ``handle()`` tasks, tracked so shutdown cancels them before the
        # broker stops. A bounded LRU of Discord message ids dedupes redelivery
        # (discord.py can replay MESSAGE_CREATE on gateway reconnect).
        self._inflight: set[asyncio.Task[None]] = set()
        self._seen_message_ids: OrderedDict[int, None] = OrderedDict()

    @property
    def connected(self) -> bool:
        """Whether the Discord gateway is currently connected (§12.1)."""
        return self._connected

    @property
    def bot_identity(self) -> str | None:
        """The bot's display identity (``name (id)``), or ``None`` before ready (§12.3)."""
        return self._bot_identity

    async def start(self) -> None:
        """Connect to the Discord gateway. Blocks until cancelled or disconnect."""
        logger.info(
            "DiscordIngressGateway starting (guild_id=%s)", self._settings.guild_id
        )
        await self._client.start(self._settings.bot_token.get_secret_value())

    async def close(self) -> None:
        """Disconnect the Discord gateway cleanly. Idempotent."""
        if not self._client.is_closed():
            await self._client.close()

    async def drain_inflight(self) -> None:
        """Cancel and await any in-flight ``handle()`` tasks (shutdown).

        Called before the calfkit client closes so a parked ``result()`` await is
        cancelled cleanly rather than erroring when the broker stops.
        """
        if not self._inflight:
            return
        tasks = list(self._inflight)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _on_ready(self) -> None:
        bot_user = self._client.user
        assert bot_user is not None, "on_ready fires after authentication completes"
        self._bot_user_id = bot_user.id
        self._message_normalizer = MessageNormalizer(
            human_owner_id=self._settings.owner_user_id
        )
        logger.info("gateway ready as %s (id=%s)", bot_user, bot_user.id)

        # Discord is connected as of on_ready — record liveness + identity, then
        # write the FIRST heartbeat BEFORE slash-sync (§12.1): slash-sync can be
        # slow / 429 on a cold tree, and readiness must not hinge on it.
        self._connected = True
        self._bot_identity = f"{bot_user} ({bot_user.id})"
        try:
            write_beat(
                _resolve_health_home(),
                _HEALTH_COMPONENT,
                status="healthy",
                identity=self._bot_identity,
                now=datetime.now(UTC),
            )
        except Exception:
            logger.exception(
                "failed to write initial bridge heartbeat; continuing boot"
            )

        await self._slash.sync(self._settings.guild_id)

    async def _on_disconnect(self) -> None:
        """Mark the bridge disconnected when the Discord gateway drops (§12.1)."""
        self._connected = False
        logger.warning(
            "discord gateway disconnected; bridge heartbeat will go stale until reconnect"
        )

    async def _on_resumed(self) -> None:
        """Mark the bridge connected again when a dropped session resumes (§12.1)."""
        self._connected = True
        logger.info("discord gateway resumed; bridge heartbeat restored")

    async def _on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        if (
            self._settings.guild_id is not None
            and message.guild.id != self._settings.guild_id
        ):
            return
        if self._message_normalizer is None:
            return  # pre-ready; defensive
        if message.type not in _ROUTABLE_MESSAGE_TYPES:
            logger.debug(
                "ignoring non-routable message id=%s type=%s", message.id, message.type
            )
            return
        # Skip the bot's own non-webhook posts (e.g. /clear markers, notices).
        # Webhook posts (the bot acting as an agent persona) flow through so the
        # author-stamping / peer-visibility paths see them.
        if (
            self._bot_user_id is not None
            and message.author.id == self._bot_user_id
            and message.webhook_id is None
        ):
            return
        if self._already_seen(message.id):
            logger.debug("ignoring redelivered message id=%s", message.id)
            return

        try:
            wire = self._message_normalizer.normalize(message)
        except Exception:
            logger.exception("failed to normalize message id=%s", message.id)
            return

        if _UNSTICK_COMMAND_RE.match(message.content):
            req = self._build_request(message, wire, ())
            if self._sticky_store is not None:
                await self._sticky_store.clear_sticky_owner(
                    str(wire.source_channel_id or wire.channel_id)
                )
            await self._reply.post_notice(req, _UNSTICK_NOTICE)
            return

        if _is_new_thread_command(message.content):
            await self._handle_new_thread_command(message, wire)
            return

        mention_ids = extract_mention_ids(message.content)
        route_kind = "explicit"
        if not mention_ids:
            # Agent persona webhooks are Discord messages too. Ambient webhook
            # posts must never feed back into sticky routing and loop.
            if getattr(message, "webhook_id", None) is not None:
                return
            if (
                not self._bridge_settings.sticky_replies.enabled
                or self._sticky_store is None
            ):
                return
            owner = await self._sticky_store.get_sticky_owner(
                str(wire.source_channel_id or wire.channel_id)
            )
            if not owner:
                return
            mention_ids = (owner,)
            route_kind = "sticky"

        req = self._build_request(message, wire, mention_ids, route_kind=route_kind)
        self._spawn_handle(req)

    async def _handle_new_thread_command(self, message: discord.Message, wire: WireMessage) -> None:
        """Create a message thread for ``!new`` and optionally dispatch into it."""
        if wire.source_channel_id != wire.channel_id:
            req = self._build_request(message, wire, ())
            await self._reply.post_notice(req, _NEW_THREAD_IN_THREAD_NOTICE)
            return

        req = self._build_request(message, wire, ())
        try:
            thread_id = await create_thread_from_message(
                message,
                name=_thread_title_from_content(message.content),
            )
        except Exception:
            logger.warning(
                "failed to create !new thread for message_id=%s channel_id=%s",
                wire.message_id,
                wire.channel_id,
                exc_info=True,
            )
            await self._reply.post_notice(req, _NEW_THREAD_CREATE_FAILED_NOTICE)
            return

        thread_wire = wire.model_copy(update={"source_channel_id": thread_id})
        mention_ids = extract_mention_ids(message.content)
        route_kind: Literal["explicit", "sticky"] = "explicit"
        if not mention_ids:
            owner = await self._sticky_owner_for_parent(wire.channel_id)
            if owner is None:
                thread_req = self._build_request(message, thread_wire, ())
                await self._reply.post_notice(thread_req, _NEW_THREAD_NO_AGENT_NOTICE)
                return
            mention_ids = (owner,)
            route_kind = "sticky"

        thread_req = self._build_request(
            message,
            thread_wire,
            mention_ids,
            route_kind=route_kind,
        )
        self._spawn_handle(thread_req)

    async def _sticky_owner_for_parent(self, channel_id: int) -> str | None:
        if (
            not self._bridge_settings.sticky_replies.enabled
            or self._sticky_store is None
        ):
            return None
        return await self._sticky_store.get_sticky_owner(str(channel_id))

    def _build_request(
        self,
        message: discord.Message,
        wire: WireMessage,
        mention_ids: tuple[str, ...],
        *,
        route_kind: Literal["explicit", "sticky"] = "explicit",
    ) -> MentionRequest:
        """Build the handler request from an already-normalized wire."""
        req = MentionRequest(
            content=wire.content,
            mention_ids=mention_ids,
            author_label=wire.author.display_name,
            message_id=wire.message_id,
            source_channel_id=wire.source_channel_id or wire.channel_id,
            channel_id=wire.channel_id,
            wire=wire,
            reply_target=message,
            route_kind=route_kind,
        )
        return req

    def _spawn_handle(self, req: MentionRequest) -> None:
        """Run one ``!mention`` as a tracked background task.

        Each mention is independent and may be long-running (an agent run + A2A
        consults), so it must not block the Discord event loop. The task is tracked
        in :attr:`_inflight` so shutdown can cancel it, and its result is reaped by
        :meth:`_on_handle_done` (which surfaces an unexpected crash to the user).
        """
        task = asyncio.create_task(self._run_handler(req))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _run_handler(self, req: MentionRequest) -> None:
        """Run the handler for ``req``, surfacing an unexpected crash to the user.

        The handler already posts user-facing notices for *expected* failures
        (roster unavailable, no agent online, fault, drop). An *unexpected* crash
        (a bug) would otherwise leave the user with silence, so post a generic
        best-effort notice. ``CancelledError`` (shutdown) propagates untouched.

        A broker-connectivity failure is split out because it is neither: the substrate
        runs the broker under ``restart: always`` with a 15s backoff, so a mention can
        legitimately land while it is bouncing. The bridge touches the broker lazily —
        the first mention is what starts it (D-11, see :func:`main`) — so that mention
        is the one that raises. calfkit's ``_ensure_started`` leaves the client startable
        after a failed start, so the NEXT mention self-heals with no retry of our own
        (a retry here would bypass its ``_start_lock``, which is what makes concurrent
        first mentions safe). "Try again in a moment" is therefore the literal truth,
        and the crash notice's "an operator should check the bridge logs" would send
        someone hunting a bug that does not exist.
        """
        try:
            await self._handler.handle(req)
        except asyncio.CancelledError:
            raise
        except KafkaConnectionError as exc:
            # Not a crash: no traceback, and below ERROR so a self-healing blip does not
            # read as a fault (or trip error alerting) in the operator's log.
            logger.warning(
                "broker unreachable handling message_id=%s (%s); it is likely restarting "
                "— the next mention will retry",
                req.message_id,
                exc,
            )
            with contextlib.suppress(Exception):
                await self._reply.post_notice(req, _BROKER_UNREACHABLE)
        except Exception:
            logger.exception(
                "mention handler crashed for message_id=%s", req.message_id
            )
            with contextlib.suppress(Exception):
                await self._reply.post_notice(
                    req,
                    "Something went wrong handling that message; please try again. "
                    "If this keeps happening, an operator should check the bridge logs.",
                )

    def _already_seen(self, message_id: int) -> bool:
        """Bounded-LRU dedupe of Discord ``message.id`` (reconnect redelivery)."""
        if message_id in self._seen_message_ids:
            self._seen_message_ids.move_to_end(message_id)
            return True
        self._seen_message_ids[message_id] = None
        if len(self._seen_message_ids) > _SEEN_MESSAGE_IDS_CAPACITY:
            self._seen_message_ids.popitem(last=False)
        return False


class _GatewayClient(discord.Client):
    """``discord.Client`` subclass that delegates events to a ``DiscordIngressGateway``."""

    def __init__(self, gateway: DiscordIngressGateway) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        # ``members`` is deliberately NOT requested: nothing here consumes member
        # events/cache (author identity arrives in MESSAGE_CREATE). Requesting it
        # would hard-fail boot with PrivilegedIntentsRequired if the portal toggle
        # is off, for no benefit. The docs still ask users to enable the portal
        # toggle as future-proofing (an enabled-but-unrequested intent is inert).
        super().__init__(intents=intents)
        self._gateway = gateway

    async def on_ready(self) -> None:
        await self._gateway._on_ready()

    async def on_disconnect(self) -> None:
        await self._gateway._on_disconnect()

    async def on_resumed(self) -> None:
        await self._gateway._on_resumed()

    async def on_message(self, message: discord.Message) -> None:
        await self._gateway._on_message(message)


def main() -> None:
    """CLI entry point. Loads config, constructs the gateway, runs until SIGINT/SIGTERM."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    settings = DiscordSettings()  # type: ignore[call-arg]
    if settings.guild_id is None:
        raise SystemExit(
            "DISCORD_GUILD_ID is required (global slash sync is too slow for dev)"
        )

    server_urls = os.getenv("CALF_HOST_URL") or "localhost"

    async def _run() -> None:
        # The bridge owns its persona sender (it posts on behalf of every agent).
        # The calfkit Client is a pure caller surface: a durable inbox + the mesh
        # view; no Worker, no consumers. Nested ``async with`` (not combined) keeps
        # each context's rationale attached to it.
        async with DiscordPersonaSender(settings) as persona_sender:  # noqa: SIM117
            async with Client.connect(
                server_urls,
                inbox_topic=_BRIDGE_INBOX_TOPIC,
                provisioning=PROVISIONING,
                mesh_config=MeshViewConfig(stale_after=_MESH_STALE_AFTER_SECONDS),
            ) as calfkit_client:
                # No eager broker pre-start here (D-11 revisited): the first
                # ``client.agent(name).start(...)`` self-ensures the broker BEFORE it
                # publishes — calfkit's ``AgentGateway.start`` awaits
                # ``_ensure_started`` (→ ``broker.start()``) ahead of ``_publish_call``,
                # and with ``provisioning=PROVISIONING`` that provisions the durable
                # inbox and starts its groupless reply subscriber consuming before the
                # request is sent, so a reply can't land on an unprovisioned/unconsumed
                # inbox (calfkit's ``_ensure_started`` docstring calls out exactly the
                # provisioning-enabled case). Nothing between here and the first mention
                # publishes to the broker, and the mesh roster read opens its own
                # independent reader — so there is nothing left to pre-start. (The CLI
                # probes DO keep an ``events()`` pre-start, but for a different reason:
                # there it doubles as the broker-reachability check.)
                async with _open_transcript_store(settings) as transcript_store:
                    await _prune_on_startup(transcript_store, settings)

                    typing_notifier = TypingNotifier(persona_sender.client)
                    overrides = EffortOverrides(transcript_store)
                    await (
                        overrides.hydrate()
                    )  # restore /thinking-effort overrides across restarts (D-8)
                    # A2A audit projection: the resolver only uses the sender's
                    # ``.client`` (a REST login), which the persona sender provides.
                    resolver = A2AChannelResolver(
                        persona_sender,
                        settings.guild_id,
                        channel_name=_a2a_channel_name(),
                        category_name=_a2a_category_name(),
                    )
                    bridge_settings = load_settings(resolve_settings_path())
                    gateway = DiscordIngressGateway(
                        settings,
                        calfkit_client=calfkit_client,
                        persona_sender=persona_sender,
                        transcript_store=transcript_store,
                        roster=MeshRoster(calfkit_client),
                        overrides=overrides,
                        a2a=A2AProjector(resolver, persona_sender),
                        progress=ProgressRenderer(persona_sender, typing_notifier),
                        reply=ReplyPoster(persona_sender, transcript_store),
                        memory_deps=MemoryPromptDeps(),
                        bridge_settings=bridge_settings,
                        sticky_store=transcript_store,
                    )
                    try:
                        stop = asyncio.Event()
                        loop = asyncio.get_running_loop()
                        for sig in (signal.SIGINT, signal.SIGTERM):
                            loop.add_signal_handler(sig, stop.set)

                        gateway_task = asyncio.create_task(gateway.start())
                        stop_task = asyncio.create_task(stop.wait())
                        # Refresh the bridge heartbeat on a timer, gated on the live
                        # Discord connection so a dropped gateway ages the beat (§12.1).
                        refresher_task = asyncio.create_task(
                            run_refresher(
                                _resolve_health_home(),
                                _HEALTH_COMPONENT,
                                is_healthy=lambda: gateway.connected,
                                identity=lambda: gateway.bot_identity,
                            )
                        )
                        try:
                            done, _ = await asyncio.wait(
                                {gateway_task, stop_task},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            # A fatal gateway crash (not a signal) must surface as a
                            # non-zero exit so the supervisor restarts us; asyncio.wait
                            # does not propagate a task's exception.
                            if gateway_task in done and not gateway_task.cancelled():
                                exc = gateway_task.exception()
                                if exc is not None:
                                    raise exc
                        finally:
                            for t in (gateway_task, stop_task, refresher_task):
                                if not t.done():
                                    t.cancel()
                            await asyncio.gather(refresher_task, return_exceptions=True)
                            await gateway.close()
                    finally:
                        # Cancel in-flight handler tasks BEFORE the client context
                        # exits (which closes the broker), then close typing — a
                        # cancelled run can still have fired a typing task.
                        await gateway.drain_inflight()
                        await typing_notifier.aclose()

    asyncio.run(_run())


if __name__ == "__main__":
    main()

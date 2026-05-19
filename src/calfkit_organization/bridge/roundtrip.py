"""Discord ↔ agent round-trip: invoke an agent and post its reply back to Discord.

Replaces the old fire-and-forget ``KafkaPublisher`` with an awaitable
request/response shape. The bridge holds the calfkit :class:`Client` whose
dispatcher is bound to the named reply topic ``discord.outbox``; every agent
:class:`ReturnCall` lands there. :class:`BridgeRoundTrip` uses
:meth:`Client.execute_node` to publish the inbound wire to
``discord.channel.{cid}.in`` and await the reply, then resolves the
responding agent's persona via :class:`AgentRegistry` and posts the reply
to the originating channel under that persona.

Identity is resolved from ``NodeResult.emitter_node_id``, which calfkit
0.3.0 populates from the inbound ``x-calf-emitter`` Kafka header — no
application-level identity stamping needed.

Concurrency: every inbound Discord message produces a fresh
:meth:`handle` coroutine. A semaphore caps outstanding invocations to
prevent runaway memory + Discord rate-limit pressure when the LLM stalls.

**Multi-agent reply semantics**: calfkit's reply dispatcher resolves at
most one reply per ``correlation_id`` (the rest are logged-and-dropped at
the dispatcher). When multiple agents both gate-accept the same inbound
event, only the first to finish reaches this code path; the others' work
is silently lost at the consumer. Acceptable for v1 (slash/mention flows
target a single agent); migrate to a non-dedupe outbox consumer when
ambient multi-agent flows matter.

**Per-call thinking-effort overrides** (v1): when ``wire.slash_target`` is
set, the round-trip reads ``state/agents/<target>.json`` and attaches a
provider-specific ``model_settings`` dict to the calfkit invocation so the
agent uses the configured effort on this exact call. Ambient messages
(``slash_target is None``) flow without overrides because the bridge does
not know which subscribed agent will gate-accept the event; those calls
fall back to whatever was baked into the agent's model client.

**Cross-process state writes**: both this module (writing
``thinking_effort``) and the agent runner (writing ``channels`` on first
boot — see ``agents/runner.py``) open the same per-agent JSON. In steady
state the agent never writes; the only realistic conflict is a bridge
``/thinking-effort`` write racing the agent's first-boot bootstrap, which
is operator-driven and rare. If runtime channel mutation is added later,
a cross-process file lock will be needed.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from calfkit.client import Client

from calfkit_organization.agents.definition import Provider
from calfkit_organization.agents.factory import DEFAULT_PROVIDER, resolve_provider
from calfkit_organization.agents.state import AgentRuntimeState
from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.bridge.thinking import build_model_settings
from calfkit_organization.bridge.wire import WireMessage
from calfkit_organization.discord.persona import (
    DiscordPersonaSender,
    Persona,
    ReplyContext,
)

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 120.0
_DEFAULT_MAX_IN_FLIGHT = 32
_DEFAULT_INGRESS_TOPIC_TEMPLATE = "discord.channel.{cid}.in"


class BridgeRoundTrip:
    """Invoke an agent and post its reply back to Discord.

    Owned by :class:`DiscordIngressGateway`; one instance per bridge process.
    The bridge's ``DiscordPersonaSender`` and ``AgentRegistry`` are shared;
    the calfkit ``Client`` must be connected with ``reply_topic="discord.outbox"``
    so the dispatcher hears agent ReturnCalls.
    """

    def __init__(
        self,
        calfkit_client: Client,
        registry: AgentRegistry,
        persona_sender: DiscordPersonaSender,
        *,
        state_dir: Path | None = None,
        default_provider: Provider = DEFAULT_PROVIDER,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_in_flight: int = _DEFAULT_MAX_IN_FLIGHT,
        ingress_topic_template: str = _DEFAULT_INGRESS_TOPIC_TEMPLATE,
    ) -> None:
        self._client = calfkit_client
        self._registry = registry
        self._persona_sender = persona_sender
        self._state_dir = state_dir
        self._default_provider = default_provider
        self._timeout_seconds = timeout_seconds
        self._semaphore = asyncio.Semaphore(max_in_flight)
        self._ingress_topic_template = ingress_topic_template
        # Validate every agent's provider at boot so an env-var typo
        # surfaces here (fail-fast) rather than as an uncaught ValueError
        # inside every targeted invocation's _resolve_model_settings.
        # Results are discarded; resolve_provider is cheap enough to re-run.
        for spec in registry.all():
            resolve_provider(spec, default_provider=default_provider)

    async def handle(self, wire: WireMessage) -> None:
        """Invoke the addressed agent and post its reply.

        When ``wire.slash_target`` is set, the persisted ``thinking_effort``
        for that agent is loaded and forwarded as a per-call
        ``model_settings`` override (see module docstring). Ambient messages
        flow without an override.

        Drops the event (logs only) on:
            - timeout (no agent responded within ``timeout_seconds``)
            - non-agent emitter on the reply (e.g. client republish)
            - unknown emitter id (registry miss)
            - empty agent output (no text to post)

        Discord HTTP errors from :meth:`DiscordPersonaSender.send` propagate.
        """
        model_settings = self._resolve_model_settings(wire)
        async with self._semaphore:
            try:
                result = await self._client.execute_node(
                    user_prompt=wire.content,
                    topic=self._ingress_topic_template.format(cid=wire.channel_id),
                    correlation_id=wire.event_id,
                    deps={"discord": wire.model_dump(mode="json")},
                    output_type=str,
                    timeout=self._timeout_seconds,
                    model_settings=model_settings,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "agent reply timed out event_id=%s channel=%s",
                    wire.event_id,
                    wire.channel_id,
                )
                return

            if result.emitter_node_kind != "agent" or not result.emitter_node_id:
                logger.warning(
                    "non-agent emitter on reply event_id=%s id=%s kind=%s",
                    wire.event_id,
                    result.emitter_node_id,
                    result.emitter_node_kind,
                )
                return

            spec = self._registry.by_id(result.emitter_node_id)
            if spec is None:
                logger.warning(
                    "unknown agent emitter=%s event_id=%s",
                    result.emitter_node_id,
                    wire.event_id,
                )
                return

            text = (result.output or "").strip()
            if not text:
                logger.info(
                    "agent %s returned empty output event_id=%s; skipping post",
                    result.emitter_node_id,
                    wire.event_id,
                )
                return

            sent = await self._persona_sender.send(
                persona=Persona(name=spec.display_name, avatar_url=spec.avatar_url),
                channel_id=wire.channel_id,
                content=text,
                reply_to=ReplyContext.from_wire(wire),
            )
            logger.info(
                "posted reply event_id=%s agent=%s reply_id=%s channel=%s",
                wire.event_id,
                result.emitter_node_id,
                sent.id,
                wire.channel_id,
            )

    def _resolve_model_settings(self, wire: WireMessage) -> dict[str, Any] | None:
        """Compute per-call ``model_settings`` for ``wire``, or ``None``.

        Returns ``None`` for ambient messages (``slash_target is None``)
        and for any state-file error (missing file, parse error, unknown
        target) — the agent then falls back to its constructor defaults.
        """
        target = wire.slash_target
        if target is None or self._state_dir is None:
            return None

        spec = self._registry.by_id(target)
        if spec is None:
            logger.warning(
                "slash_target=%r missing from registry; skipping model_settings",
                target,
            )
            return None

        state_path = self._state_dir / f"{target}.json"
        try:
            raw = state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            logger.warning(
                "failed to read state for agent=%s path=%s; skipping model_settings",
                target,
                state_path,
                exc_info=True,
            )
            return None

        try:
            state = AgentRuntimeState.model_validate_json(raw)
        except ValueError:
            logger.warning(
                "malformed state for agent=%s path=%s; skipping model_settings",
                target,
                state_path,
                exc_info=True,
            )
            return None

        provider = resolve_provider(spec, default_provider=self._default_provider)
        return build_model_settings(provider, state.thinking_effort)

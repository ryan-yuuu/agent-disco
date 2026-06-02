"""Bridge-side state-event consumer.

Exposes:
- ``make_state_consumer_handler``: pure-async handler factory closing over the
  AgentRegistry and two callbacks (on_first_seen, on_departed). Returned
  function takes an AgentStateMessage and dispatches by discriminator. Unit-
  testable.
- ``register_state_consumer``: side-effecting wiring. Registers the FastStream
  subscriber on the agent.state topic. Called once at bridge boot before the
  bridge's Worker.run() (or equivalent FastStream startup).

Dispatch:
- AgentStateEvent -> registry.upsert_from_state_event(definition). If first-seen,
  call on_first_seen(agent_id).
- AgentDepartureEvent -> registry.remove(agent_id). If removed, call on_departed.

NOTE: ``AgentRegistry.upsert_from_state_event`` and ``AgentRegistry.remove`` do
not exist on the registry yet -- they are added in PR 3 alongside the wiring
that actually invokes this consumer. PR 1 ships this module as scaffolding only;
no code path in PR 1 calls ``register_state_consumer`` or runs the returned
handler, so the missing methods are not exercised at runtime. The attribute
lookups inside ``handle`` are deferred until call time, so the module imports
cleanly. Unit tests pass a duck-typed stub that implements both methods.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from calfkit.client import Client

from calfcord.bridge.registry import AgentRegistry
from calfcord.control_plane.builders import state_event_to_definition
from calfcord.control_plane.schema import (
    CONTROL_PLANE_SCHEMA_VERSION,
    AgentDepartureEvent,
    AgentStateEvent,
    AgentStateMessage,
)
from calfcord.control_plane.topics import AGENT_STATE_TOPIC

logger = logging.getLogger(__name__)


StateConsumerHandler = Callable[[AgentStateMessage], Awaitable[None]]


def make_state_consumer_handler(
    registry: AgentRegistry,
    on_first_seen: Callable[[str], None],
    on_departed: Callable[[str], None],
) -> StateConsumerHandler:
    """Build the async handler closure for the bridge's state-event consumer."""

    async def handle(message: AgentStateMessage) -> None:
        if message.schema_version != CONTROL_PLANE_SCHEMA_VERSION:
            logger.warning(
                "bridge state-consumer ignoring message schema_version=%d (expected %d)",
                message.schema_version, CONTROL_PLANE_SCHEMA_VERSION,
            )
            return

        if isinstance(message, AgentStateEvent):
            definition = state_event_to_definition(message)
            was_first_seen = registry.upsert_from_state_event(definition)
            if was_first_seen:
                logger.info(
                    "first-seen agent=%s cause=%s", message.agent_id, message.cause,
                )
                on_first_seen(message.agent_id)
            else:
                logger.debug(
                    "re-announce agent=%s cause=%s",
                    message.agent_id, message.cause,
                )
            return

        if isinstance(message, AgentDepartureEvent):
            removed = registry.remove(message.agent_id)
            if removed:
                logger.info(
                    "agent departed agent=%s reason=%s",
                    message.agent_id, message.reason,
                )
                on_departed(message.agent_id)
            else:
                logger.debug(
                    "departure for unknown agent=%s (already removed or never seen)",
                    message.agent_id,
                )
            return

        # Defensive: the discriminated union should make this unreachable.
        logger.warning(
            "bridge state-consumer received unknown message kind=%r; ignoring",
            type(message).__name__,
        )

    return handle


def register_state_consumer(
    client: Client,
    registry: AgentRegistry,
    on_first_seen: Callable[[str], None],
    on_departed: Callable[[str], None],
) -> None:
    """Register the bridge's state-event subscriber on the FastStream broker.

    Must be called BEFORE the bridge's FastStream broker starts.
    """
    handler = make_state_consumer_handler(registry, on_first_seen, on_departed)
    client._connection.subscriber(
        AGENT_STATE_TOPIC,
        group_id="bridge-state-consumer",
    )(handler)

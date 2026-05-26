"""Agent-side control-plane sink.

Exposes:
- ``make_control_sink_handler``: pure-async handler factory. Closes over the
  client (for state-event publish) and the AgentDefinitionRef (for the .md write
  and post-mutation announce). Returned function takes an AgentControlEnvelope
  and applies the command. Unit-testable without FastStream.
- ``register_control_sink``: side-effecting wiring. Calls FastStream's broker
  subscriber decorator on the agent's targeted control topic and the broadcast
  discovery topic. Called once per agent at boot, BEFORE Worker.run() starts
  FastStream.

Dispatch is by payload type (discriminated union on ``op``). The sink writes
to the local .md via md_writer.update_thinking_effort and publishes a fresh
state event with cause="command_applied" on success. On disk-write failure
(validation, OSError, FileNotFoundError) the bridge's in-memory state will
drift until the agent's next startup announce -- accepted v1 trade-off.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from calfkit.client import Client

from calfkit_organization.agents.md_writer import update_thinking_effort
from calfkit_organization.control_plane.builders import build_state_event
from calfkit_organization.control_plane.definition_ref import AgentDefinitionRef
from calfkit_organization.control_plane.publish import publish_state_event
from calfkit_organization.control_plane.schema import (
    CONTROL_PLANE_SCHEMA_VERSION,
    AgentControlEnvelope,
    DiscoveryPingOp,
    SetThinkingEffortOp,
)
from calfkit_organization.control_plane.topics import (
    BRIDGE_DISCOVERY_TOPIC,
    control_topic_for,
)

logger = logging.getLogger(__name__)


ControlSinkHandler = Callable[[AgentControlEnvelope], Awaitable[None]]


def make_control_sink_handler(
    client: Client, definition_ref: AgentDefinitionRef
) -> ControlSinkHandler:
    """Build the async handler closure for an agent's control-plane sink.

    The returned coroutine is what the FastStream subscriber will invoke for
    each inbound envelope. Factored out from ``register_control_sink`` so unit
    tests can exercise the handler directly without spinning up FastStream.
    """
    aid = definition_ref.current.agent_id

    async def handle(envelope: AgentControlEnvelope) -> None:
        if envelope.schema_version != CONTROL_PLANE_SCHEMA_VERSION:
            logger.warning(
                "agent=%s ignoring control envelope schema_version=%d (expected %d)",
                aid, envelope.schema_version, CONTROL_PLANE_SCHEMA_VERSION,
            )
            return

        command = envelope.command
        if isinstance(command, SetThinkingEffortOp):
            if command.agent_id != aid:
                logger.warning(
                    "agent=%s received SetThinkingEffortOp for agent_id=%r on its own "
                    "control topic; ignoring (publish-vs-payload mismatch)",
                    aid, command.agent_id,
                )
                return
            try:
                new_def = update_thinking_effort(
                    definition_ref.current.source_path, command.value,
                )
            except (FileNotFoundError, ValueError, OSError):
                logger.exception(
                    "agent=%s failed to apply set_thinking_effort value=%s "
                    "request_id=%s issued_by=%s; bridge will drift until next announce",
                    aid, command.value, command.request_id, command.issued_by,
                )
                return
            definition_ref.swap(new_def)
            await publish_state_event(
                client, build_state_event(new_def, cause="command_applied"),
            )
            logger.info(
                "agent=%s applied set_thinking_effort value=%s request_id=%s issued_by=%s",
                aid, command.value, command.request_id, command.issued_by,
            )
            return

        if isinstance(command, DiscoveryPingOp):
            await publish_state_event(
                client,
                build_state_event(
                    definition_ref.current, cause="discovery_response",
                ),
            )
            logger.debug(
                "agent=%s responded to discovery_ping request_id=%s",
                aid, command.request_id,
            )
            return

        # Defensive: pydantic's discriminated union should catch unknown ops
        # at parse time. If a new op type slips through (e.g. a future schema
        # version compatibility window), log + skip.
        logger.warning(
            "agent=%s received unknown control op type=%r; ignoring",
            aid, type(command).__name__,
        )

    return handle


def register_control_sink(
    client: Client, definition_ref: AgentDefinitionRef
) -> None:
    """Register the agent's control-plane subscriber on the FastStream broker.

    Must be called BEFORE the agent's Worker.run() starts FastStream -- once
    FastStream is running, subscriber registration on the same broker is not
    a supported pattern.

    The single subscriber listens on both the agent's targeted control topic
    and the broadcast discovery topic. Group_id is ``control-{agent_id}`` so
    each agent is in its own consumer group: broadcasts fan out, targeted
    commands stay targeted (only this agent is in this group anyway).
    """
    aid = definition_ref.current.agent_id
    handler = make_control_sink_handler(client, definition_ref)
    client._connection.subscriber(
        control_topic_for(aid),
        BRIDGE_DISCOVERY_TOPIC,
        group_id=f"control-{aid}",
    )(handler)

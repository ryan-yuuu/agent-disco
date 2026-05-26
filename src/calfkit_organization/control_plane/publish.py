"""Publish helpers for the control plane.

These functions reach into ``client._connection`` (a FastStream KafkaBroker)
because calfkit's public ``Client`` API exposes only ``invoke_node`` / ``execute_node``,
both of which are agent invocations -- not what we want for plain control-plane
messages. The private-attribute access is documented here in one place so a
future calfkit upgrade that exposes a public ``Client.publish`` is a single-
file swap.

The same broker is used for calfkit's own agent invocation traffic; both flows
coexist on different topics.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from calfkit.client import Client

from calfkit_organization.control_plane.schema import (
    AgentControlCommand,
    AgentControlEnvelope,
    AgentDepartureEvent,
    AgentStateEvent,
    DiscoveryPingOp,
)
from calfkit_organization.control_plane.topics import (
    AGENT_STATE_TOPIC,
    BRIDGE_DISCOVERY_TOPIC,
    control_topic_for,
)


async def publish_control_command(
    client: Client, agent_id: str, command: AgentControlCommand
) -> None:
    """Publish a targeted control command to the agent's control topic.

    Fire-and-forget: returns when the broker has accepted the message;
    does not wait for the agent to consume or apply.
    """
    envelope = AgentControlEnvelope(command=command)
    await client._connection.publish(
        envelope.model_dump_json(),
        topic=control_topic_for(agent_id),
    )


async def publish_discovery_ping(client: Client) -> None:
    """Broadcast a discovery ping. Every running agent's control sink re-announces."""
    envelope = AgentControlEnvelope(
        command=DiscoveryPingOp(
            issued_at=datetime.now(UTC),
            request_id=str(uuid.uuid4()),
        ),
    )
    await client._connection.publish(
        envelope.model_dump_json(),
        topic=BRIDGE_DISCOVERY_TOPIC,
    )


async def publish_state_event(client: Client, event: AgentStateEvent) -> None:
    """Agent publishes its current state on startup, on each applied command,
    or in response to a discovery ping."""
    await client._connection.publish(
        event.model_dump_json(),
        topic=AGENT_STATE_TOPIC,
    )


async def publish_departure(client: Client, agent_id: str) -> None:
    """Best-effort graceful goodbye on agent shutdown.

    Bridge consumes from agent.state, dispatches on the ``kind`` discriminator,
    and removes the agent from its registry projection. Crashes / SIGKILL leave
    the bridge with a stale entry until the agent next restarts.
    """
    event = AgentDepartureEvent(
        agent_id=agent_id,
        departed_at=datetime.now(UTC),
    )
    await client._connection.publish(
        event.model_dump_json(),
        topic=AGENT_STATE_TOPIC,
    )

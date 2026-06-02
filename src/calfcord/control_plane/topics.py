"""Kafka topic names for the control plane.

Three topics total:
- agent.state: shared topic, all agents publish here on startup, on each applied
  command, and on graceful shutdown. The bridge subscribes with auto_offset_reset=
  latest and projects events into its in-memory AgentRegistry.
- agent.<id>.control.in: per-agent targeted command topic. Bridge publishes
  SetThinkingEffortOp here; only the named agent's control sink consumes it.
- bridge.discovery: broadcast topic. Bridge publishes a DiscoveryPingOp on boot
  so already-running agents re-announce. Each agent subscribes in its own consumer
  group so every agent receives every ping.
"""
from __future__ import annotations

AGENT_STATE_TOPIC = "agent.state"
BRIDGE_DISCOVERY_TOPIC = "bridge.discovery"
AGENT_CONTROL_TOPIC_TEMPLATE = "agent.{agent_id}.control.in"


def control_topic_for(agent_id: str) -> str:
    """Return the targeted control topic for ``agent_id``."""
    return AGENT_CONTROL_TOPIC_TEMPLATE.format(agent_id=agent_id)

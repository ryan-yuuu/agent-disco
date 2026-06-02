"""Discord ↔ Calfkit-topic bridge.

Public surface:
    AgentRegistry                   — agent roster index (definitions live in
                                      :mod:`calfcord.agents`)
    WireMessage, WireAuthor         — typed Discord event payload on Kafka
    MessageNormalizer, SlashNormalizer — discord types → WireMessage
    BridgeIngress                   — fire-and-forget agent invocation
    PendingWires                    — process-local correlation_id → wire map
    build_outbox_consumer           — long-lived consumer that posts every
                                      agent reply to Discord
    SlashCommandManager             — registers, syncs, dispatches per-agent slashes
    A2AChannelResolver              — egress helper for agent-to-agent channels
    DiscordIngressGateway, main     — the bridge daemon and CLI entry
"""

from calfcord.bridge.egress import A2AChannelResolver
from calfcord.bridge.gateway import DiscordIngressGateway, main
from calfcord.bridge.ingress import BridgeIngress
from calfcord.bridge.normalizer import (
    MessageNormalizer,
    SlashNormalizer,
    UnknownAgentMentionError,
)
from calfcord.bridge.outbox import build_outbox_consumer
from calfcord.bridge.pending_wires import PendingWires
from calfcord.bridge.registry import AgentRegistry
from calfcord.bridge.slash import SlashCommandManager
from calfcord.bridge.wire import WireAuthor, WireMessage

__all__ = [
    "A2AChannelResolver",
    "AgentRegistry",
    "BridgeIngress",
    "DiscordIngressGateway",
    "MessageNormalizer",
    "PendingWires",
    "SlashCommandManager",
    "SlashNormalizer",
    "UnknownAgentMentionError",
    "WireAuthor",
    "WireMessage",
    "build_outbox_consumer",
    "main",
]

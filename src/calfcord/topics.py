"""Project-wide Kafka topic constants for cross-process contracts.

Topics that are produced by one process and consumed by another live here so
producer and consumer always agree on the literal. Putting these in a tiny
zero-dependency module lets any package (``bridge/``, ``agents/``) import them
without risking import cycles, and removes the need for "drift-guard" contract
tests that re-assert the same string in two places.

Add a topic here only when **multiple processes** subscribe or publish to it.
Per-agent topics (``agent.{id}.in``, channel topics) stay where they're consumed
— they're parameterized strings, not cross-process contracts.
"""

from __future__ import annotations

DISCORD_OUTBOX_TOPIC = "discord.outbox"
"""The conventional ``discord.outbox`` reply-topic literal.

An agent reply is the agent node's ``ReturnCall`` envelope, emitted to the
inbound frame's ``callback_topic``. Historically the bridge pointed that callback
at this shared topic and ran an outbox consumer over it; the calfkit 0.12 bridge
reads replies directly off its caller-surface :class:`~calfkit.Client` stream
instead. The literal is kept as the single source of truth for the name so the
other runners can keep their own reply topics DISTINCT from it (the tools and MCP
runners deliberately pick ``calfkit.tools.reply`` / ``calfkit.mcp.reply`` so a
target-agent ``ReturnCall`` is never mistaken for a Discord-bound reply)."""

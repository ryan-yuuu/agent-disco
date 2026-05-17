"""Unit tests for KafkaPublisher using TestKafkaBroker (in-memory faststream)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from calfkit.client import Client
from calfkit.models.envelope import Envelope
from faststream.kafka import TestKafkaBroker

from calfkit_organization.bridge.publisher import KafkaPublisher
from calfkit_organization.bridge.wire import WireAuthor, WireMessage


def _make_wire(**overrides) -> WireMessage:
    defaults = dict(
        event_id="evt-abc",
        kind="message",
        message_id=1000,
        channel_id=2000,
        guild_id=3000,
        content="hello world",
        author=WireAuthor(
            discord_user_id=4000,
            display_name="alice",
            is_bot=False,
            is_webhook=False,
            is_human_owner=True,
        ),
        created_at=datetime.now(UTC),
    )
    return WireMessage(**(defaults | overrides))


async def test_publish_routes_to_channel_topic_and_round_trips_wire():
    client = Client.connect(server_urls="localhost")
    broker = client.broker

    received: list[Envelope] = []
    received_event = asyncio.Event()

    @broker.subscriber("discord.channel.2000", group_id="test-capture")
    async def capture(envelope: Envelope) -> None:
        received.append(envelope)
        received_event.set()

    publisher = KafkaPublisher(client, cleanup_timeout_seconds=0.3)
    wire = _make_wire()

    async with TestKafkaBroker(broker):
        await publisher.publish(wire)
        await asyncio.wait_for(received_event.wait(), timeout=2.0)

    await publisher.close()
    await client.close()

    assert len(received) == 1
    envelope = received[0]
    discord_payload = envelope.context.deps.provided_deps["discord"]
    assert discord_payload["event_id"] == "evt-abc"
    assert discord_payload["channel_id"] == 2000
    assert discord_payload["kind"] == "message"
    assert discord_payload["content"] == "hello world"
    assert envelope.context.deps.correlation_id == "evt-abc"


async def test_publish_routes_thread_message_to_parent_channel_topic():
    """Sanity check: wires with channel_id=parent route to discord.channel.{parent}."""
    client = Client.connect(server_urls="localhost")
    broker = client.broker

    received: list[Envelope] = []
    received_event = asyncio.Event()

    @broker.subscriber("discord.channel.555", group_id="test-capture-parent")
    async def capture(envelope: Envelope) -> None:
        received.append(envelope)
        received_event.set()

    publisher = KafkaPublisher(client, cleanup_timeout_seconds=0.3)
    wire = _make_wire(channel_id=555)

    async with TestKafkaBroker(broker):
        await publisher.publish(wire)
        await asyncio.wait_for(received_event.wait(), timeout=2.0)

    await publisher.close()
    await client.close()

    assert len(received) == 1
    assert received[0].context.deps.provided_deps["discord"]["channel_id"] == 555


async def test_publish_carries_user_prompt_in_state():
    """The wire content should land as a staged user prompt in the envelope's State."""
    client = Client.connect(server_urls="localhost")
    broker = client.broker

    received: list[Envelope] = []
    received_event = asyncio.Event()

    @broker.subscriber("discord.channel.7777", group_id="test-capture-state")
    async def capture(envelope: Envelope) -> None:
        received.append(envelope)
        received_event.set()

    publisher = KafkaPublisher(client, cleanup_timeout_seconds=0.3)
    wire = _make_wire(channel_id=7777, content="book me a haircut")

    async with TestKafkaBroker(broker):
        await publisher.publish(wire)
        await asyncio.wait_for(received_event.wait(), timeout=2.0)

    await publisher.close()
    await client.close()

    state = received[0].context.state
    assert state.uncommitted_message is not None
    parts = state.uncommitted_message.parts
    assert any("book me a haircut" in getattr(p, "content", "") for p in parts)


async def test_cleanup_task_clears_after_timeout():
    client = Client.connect(server_urls="localhost")
    broker = client.broker

    publisher = KafkaPublisher(client, cleanup_timeout_seconds=0.2)
    wire = _make_wire(channel_id=8888)

    async with TestKafkaBroker(broker):
        await publisher.publish(wire)
        assert len(publisher._pending_cleanups) == 1
        await asyncio.sleep(0.5)
        assert len(publisher._pending_cleanups) == 0

    await client.close()


async def test_close_cancels_pending_cleanups():
    client = Client.connect(server_urls="localhost")
    broker = client.broker

    publisher = KafkaPublisher(client, cleanup_timeout_seconds=900)
    wire = _make_wire(channel_id=9999)

    async with TestKafkaBroker(broker):
        await publisher.publish(wire)
        assert len(publisher._pending_cleanups) == 1
        await publisher.close()
        assert len(publisher._pending_cleanups) == 0

    await client.close()

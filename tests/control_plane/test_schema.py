"""Round-trip and discriminator dispatch tests for control-plane schema types."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from calfkit_organization.control_plane.schema import (
    CONTROL_PLANE_SCHEMA_VERSION,
    AgentControlEnvelope,
    AgentDepartureEvent,
    AgentStateEvent,
    AgentStateMessage,
    DiscoveryPingOp,
    SetThinkingEffortOp,
)


def test_set_thinking_effort_op_round_trip() -> None:
    op = SetThinkingEffortOp(
        agent_id="scribe",
        value="high",
        request_id="req-1",
        issued_by="user-123",
    )
    parsed = SetThinkingEffortOp.model_validate_json(op.model_dump_json())
    assert parsed == op
    assert parsed.op == "set_thinking_effort"


def test_discovery_ping_op_round_trip() -> None:
    op = DiscoveryPingOp(
        issued_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        request_id="ping-1",
    )
    parsed = DiscoveryPingOp.model_validate_json(op.model_dump_json())
    assert parsed == op
    assert parsed.op == "discovery_ping"


def test_envelope_with_set_thinking_effort_round_trip() -> None:
    envelope = AgentControlEnvelope(
        command=SetThinkingEffortOp(
            agent_id="scribe",
            value="medium",
            request_id="req-2",
            issued_by="user-9",
        ),
    )
    parsed = AgentControlEnvelope.model_validate_json(envelope.model_dump_json())
    assert isinstance(parsed.command, SetThinkingEffortOp)
    assert parsed.command.agent_id == "scribe"
    assert parsed.command.value == "medium"
    assert parsed.schema_version == CONTROL_PLANE_SCHEMA_VERSION


def test_envelope_with_discovery_ping_round_trip() -> None:
    envelope = AgentControlEnvelope(
        command=DiscoveryPingOp(
            issued_at=datetime(2026, 5, 25, 8, 0, tzinfo=UTC),
            request_id="ping-9",
        ),
    )
    parsed = AgentControlEnvelope.model_validate_json(envelope.model_dump_json())
    assert isinstance(parsed.command, DiscoveryPingOp)
    assert parsed.command.request_id == "ping-9"


def test_envelope_unknown_op_raises() -> None:
    payload = (
        '{"schema_version": 1, "command": {"op": "nope", "agent_id": "x", '
        '"value": "high", "request_id": "r", "issued_by": "u"}}'
    )
    with pytest.raises(ValidationError):
        AgentControlEnvelope.model_validate_json(payload)


def test_agent_state_event_round_trip() -> None:
    event = AgentStateEvent(
        agent_id="scribe",
        slash="/scribe",
        display_name="Scribe",
        description="Takes notes.",
        avatar_url="https://example.com/avatar.png",
        role="assistant",
        history_turns=20,
        thinking_effort="high",
        provider="anthropic",
        emitted_at=datetime(2026, 5, 25, 12, 0, tzinfo=UTC),
        cause="startup",
    )
    parsed = AgentStateEvent.model_validate_json(event.model_dump_json())
    assert parsed == event
    assert parsed.kind == "state"


def test_agent_departure_event_round_trip() -> None:
    event = AgentDepartureEvent(
        agent_id="scribe",
        departed_at=datetime(2026, 5, 25, 12, 0, tzinfo=UTC),
    )
    parsed = AgentDepartureEvent.model_validate_json(event.model_dump_json())
    assert parsed == event
    assert parsed.kind == "departure"
    assert parsed.reason == "shutdown"


def test_state_message_discriminator_state() -> None:
    adapter = TypeAdapter(AgentStateMessage)
    event = AgentStateEvent(
        agent_id="scribe",
        slash="/scribe",
        display_name="Scribe",
        description="Takes notes.",
        role="assistant",
        history_turns=10,
        emitted_at=datetime(2026, 5, 25, tzinfo=UTC),
        cause="startup",
    )
    parsed = adapter.validate_json(event.model_dump_json())
    assert isinstance(parsed, AgentStateEvent)


def test_state_message_discriminator_departure() -> None:
    adapter = TypeAdapter(AgentStateMessage)
    event = AgentDepartureEvent(
        agent_id="scribe",
        departed_at=datetime(2026, 5, 25, tzinfo=UTC),
    )
    parsed = adapter.validate_json(event.model_dump_json())
    assert isinstance(parsed, AgentDepartureEvent)


def test_state_message_unknown_kind_raises() -> None:
    adapter = TypeAdapter(AgentStateMessage)
    payload = (
        '{"kind": "mystery", "schema_version": 1, "agent_id": "x", '
        '"departed_at": "2026-01-01T00:00:00+00:00"}'
    )
    with pytest.raises(ValidationError):
        adapter.validate_json(payload)


def test_schema_version_default_on_envelope() -> None:
    envelope = AgentControlEnvelope(
        command=DiscoveryPingOp(
            issued_at=datetime(2026, 1, 1, tzinfo=UTC),
            request_id="r",
        ),
    )
    assert envelope.schema_version == CONTROL_PLANE_SCHEMA_VERSION


def test_schema_version_default_on_state_event() -> None:
    event = AgentStateEvent(
        agent_id="scribe",
        slash="/scribe",
        display_name="Scribe",
        description="d",
        role="assistant",
        history_turns=10,
        emitted_at=datetime(2026, 1, 1, tzinfo=UTC),
        cause="startup",
    )
    assert event.schema_version == CONTROL_PLANE_SCHEMA_VERSION


def test_schema_version_default_on_departure_event() -> None:
    event = AgentDepartureEvent(
        agent_id="scribe",
        departed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert event.schema_version == CONTROL_PLANE_SCHEMA_VERSION

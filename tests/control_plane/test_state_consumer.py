"""Tests for the bridge-side state-event consumer handler.

A stub registry is used because the real ``AgentRegistry`` does not yet expose
``upsert_from_state_event`` / ``remove`` -- those methods are added in PR 3.
The handler is duck-typed so a stub satisfies its needs at unit-test time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from calfcord.control_plane.schema import (
    AgentControlEnvelope,
    AgentDepartureEvent,
    AgentStateEvent,
    SetThinkingEffortOp,
)
from calfcord.control_plane.state_consumer import (
    make_state_consumer_handler,
)


class _StubRegistry:
    def __init__(self) -> None:
        self.upserts: list[tuple[str, bool]] = []
        self.removes: list[tuple[str, bool]] = []
        self.next_upsert_first_seen: bool = True
        self.next_remove_returns: bool = True

    def upsert_from_state_event(self, definition: Any) -> bool:
        was_first_seen = self.next_upsert_first_seen
        self.upserts.append((definition.agent_id, was_first_seen))
        return was_first_seen

    def remove(self, agent_id: str) -> bool:
        ret = self.next_remove_returns
        self.removes.append((agent_id, ret))
        return ret


def _state_event(agent_id: str = "scribe", **overrides: object) -> AgentStateEvent:
    base: dict[str, object] = {
        "agent_id": agent_id,
        "display_name": agent_id.capitalize(),
        "description": "Takes notes.",
        "role": "assistant",
        "history_turns": 20,
        "thinking_effort": "high",
        "provider": "anthropic",
        "emitted_at": datetime(2026, 5, 25, tzinfo=UTC),
        "cause": "startup",
    }
    base.update(overrides)
    return AgentStateEvent(**base)  # type: ignore[arg-type]


def _departure(agent_id: str = "scribe") -> AgentDepartureEvent:
    return AgentDepartureEvent(
        agent_id=agent_id,
        departed_at=datetime(2026, 5, 25, tzinfo=UTC),
    )


async def test_state_event_for_new_agent_fires_first_seen() -> None:
    registry = _StubRegistry()
    registry.next_upsert_first_seen = True

    first_seen: list[str] = []
    departed: list[str] = []
    handler = make_state_consumer_handler(
        registry,  # type: ignore[arg-type]
        on_first_seen=first_seen.append,
        on_departed=departed.append,
    )

    await handler(_state_event("scribe"))

    assert registry.upserts == [("scribe", True)]
    assert first_seen == ["scribe"]
    assert departed == []


async def test_state_event_for_known_agent_does_not_fire_first_seen() -> None:
    registry = _StubRegistry()
    registry.next_upsert_first_seen = False

    first_seen: list[str] = []
    departed: list[str] = []
    handler = make_state_consumer_handler(
        registry,  # type: ignore[arg-type]
        on_first_seen=first_seen.append,
        on_departed=departed.append,
    )

    await handler(_state_event("scribe", cause="command_applied"))

    assert registry.upserts == [("scribe", False)]
    assert first_seen == []
    assert departed == []


async def test_departure_for_known_agent_fires_on_departed() -> None:
    registry = _StubRegistry()
    registry.next_remove_returns = True

    first_seen: list[str] = []
    departed: list[str] = []
    handler = make_state_consumer_handler(
        registry,  # type: ignore[arg-type]
        on_first_seen=first_seen.append,
        on_departed=departed.append,
    )

    await handler(_departure("scribe"))

    assert registry.removes == [("scribe", True)]
    assert first_seen == []
    assert departed == ["scribe"]


async def test_departure_for_unknown_agent_does_not_fire_on_departed() -> None:
    registry = _StubRegistry()
    registry.next_remove_returns = False

    first_seen: list[str] = []
    departed: list[str] = []
    handler = make_state_consumer_handler(
        registry,  # type: ignore[arg-type]
        on_first_seen=first_seen.append,
        on_departed=departed.append,
    )

    await handler(_departure("ghost"))

    assert registry.removes == [("ghost", False)]
    assert first_seen == []
    assert departed == []


async def test_wrong_schema_version_is_ignored() -> None:
    registry = _StubRegistry()
    first_seen: list[str] = []
    departed: list[str] = []
    handler = make_state_consumer_handler(
        registry,  # type: ignore[arg-type]
        on_first_seen=first_seen.append,
        on_departed=departed.append,
    )

    event = _state_event("scribe")
    # Build a bumped-schema copy.
    bumped = event.model_copy(update={"schema_version": 999})
    await handler(bumped)

    assert registry.upserts == []
    assert registry.removes == []
    assert first_seen == []
    assert departed == []


# Quiet unused-import warnings for symbols imported for type-completeness only.
_ = AgentControlEnvelope
_ = SetThinkingEffortOp

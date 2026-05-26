"""Tests for control-plane topic constants and helpers."""

from __future__ import annotations

import pytest

from calfkit_organization.control_plane.topics import (
    AGENT_CONTROL_TOPIC_TEMPLATE,
    AGENT_STATE_TOPIC,
    BRIDGE_DISCOVERY_TOPIC,
    control_topic_for,
)


def test_agent_state_topic_constant() -> None:
    assert AGENT_STATE_TOPIC == "agent.state"


def test_bridge_discovery_topic_constant() -> None:
    assert BRIDGE_DISCOVERY_TOPIC == "bridge.discovery"


def test_template_constant() -> None:
    assert AGENT_CONTROL_TOPIC_TEMPLATE == "agent.{agent_id}.control.in"


def test_control_topic_for_scribe() -> None:
    assert control_topic_for("scribe") == "agent.scribe.control.in"


@pytest.mark.parametrize(
    ("agent_id", "expected"),
    [
        ("scheduler", "agent.scheduler.control.in"),
        ("a", "agent.a.control.in"),
        ("agent_with_underscores", "agent.agent_with_underscores.control.in"),
        ("with-hyphen", "agent.with-hyphen.control.in"),
    ],
)
def test_control_topic_for_various_ids(agent_id: str, expected: str) -> None:
    assert control_topic_for(agent_id) == expected

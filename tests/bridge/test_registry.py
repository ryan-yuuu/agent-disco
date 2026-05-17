"""Unit tests for AgentSpec validators and AgentRegistry duplicate detection / TOML loading."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from calfkit_organization.bridge.registry import AgentRegistry, AgentSpec


def _make_spec(**overrides) -> AgentSpec:
    defaults = dict(
        agent_id="scheduler",
        slash="/scheduler",
        display_name="Aksel (Scheduler)",
        description="Calendar mechanics.",
    )
    return AgentSpec(**(defaults | overrides))


class TestAgentSpecValidators:
    def test_valid_construction(self) -> None:
        spec = _make_spec()
        assert spec.agent_id == "scheduler"
        assert spec.slash == "/scheduler"

    @pytest.mark.parametrize("bad_id", ["Scheduler", "sched uler", "x" * 33, "", "sched.uler"])
    def test_invalid_agent_id_rejected(self, bad_id: str) -> None:
        with pytest.raises(ValidationError, match="agent_id"):
            _make_spec(agent_id=bad_id)

    @pytest.mark.parametrize("bad_slash", ["scheduler", "/Scheduler", "/x" * 20, "/", "/sched.uler"])
    def test_invalid_slash_rejected(self, bad_slash: str) -> None:
        with pytest.raises(ValidationError, match="slash"):
            _make_spec(slash=bad_slash)

    def test_display_name_clyde_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Clyde"):
            _make_spec(display_name="clyde")

    @pytest.mark.parametrize("bad_name", ["", "x" * 81])
    def test_display_name_length_rejected(self, bad_name: str) -> None:
        with pytest.raises(ValidationError, match="display_name"):
            _make_spec(display_name=bad_name)

    @pytest.mark.parametrize("bad_desc", ["", "x" * 101])
    def test_description_length_rejected(self, bad_desc: str) -> None:
        with pytest.raises(ValidationError, match="description"):
            _make_spec(description=bad_desc)


class TestAgentRegistryDuplicates:
    def test_duplicate_agent_id_rejected(self) -> None:
        a = _make_spec()
        b = _make_spec(slash="/other", display_name="Other")
        with pytest.raises(ValueError, match="duplicate agent_id"):
            AgentRegistry([a, b])

    def test_duplicate_slash_rejected(self) -> None:
        a = _make_spec()
        b = _make_spec(agent_id="other", display_name="Other")
        with pytest.raises(ValueError, match="duplicate slash"):
            AgentRegistry([a, b])

    def test_duplicate_display_name_rejected(self) -> None:
        a = _make_spec()
        b = _make_spec(agent_id="other", slash="/other")
        with pytest.raises(ValueError, match="duplicate display_name"):
            AgentRegistry([a, b])


class TestAgentRegistryLookups:
    @pytest.fixture
    def registry(self) -> AgentRegistry:
        return AgentRegistry(
            [
                _make_spec(),
                _make_spec(
                    agent_id="finance",
                    slash="/finance",
                    display_name="Finn (Finance)",
                    description="Bookkeeping.",
                ),
            ]
        )

    def test_by_id(self, registry: AgentRegistry) -> None:
        assert registry.by_id("scheduler").agent_id == "scheduler"
        assert registry.by_id("missing") is None

    def test_by_slash(self, registry: AgentRegistry) -> None:
        assert registry.by_slash("/finance").agent_id == "finance"
        assert registry.by_slash("/nope") is None

    def test_by_display_name(self, registry: AgentRegistry) -> None:
        assert registry.by_display_name("Aksel (Scheduler)").agent_id == "scheduler"
        assert registry.by_display_name("Unknown") is None

    def test_all_returns_specs_in_order(self, registry: AgentRegistry) -> None:
        all_specs = registry.all()
        assert [s.agent_id for s in all_specs] == ["scheduler", "finance"]


class TestFromToml:
    def test_loads_valid_file(self, tmp_path: Path) -> None:
        config = tmp_path / "agents.toml"
        config.write_text(
            """
[[agents]]
agent_id = "scheduler"
slash = "/scheduler"
display_name = "Aksel (Scheduler)"
description = "Calendar."

[[agents]]
agent_id = "finance"
slash = "/finance"
display_name = "Finn (Finance)"
description = "Bookkeeping."
"""
        )
        registry = AgentRegistry.from_toml(config)
        assert registry.by_id("scheduler") is not None
        assert registry.by_id("finance") is not None

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            AgentRegistry.from_toml(tmp_path / "nonexistent.toml")

    def test_malformed_toml_raises(self, tmp_path: Path) -> None:
        config = tmp_path / "agents.toml"
        config.write_text("this is = not [[valid toml")
        with pytest.raises(Exception):  # tomllib.TOMLDecodeError is a subclass of ValueError
            AgentRegistry.from_toml(config)

    def test_invalid_agent_entry_raises(self, tmp_path: Path) -> None:
        config = tmp_path / "agents.toml"
        config.write_text(
            """
[[agents]]
agent_id = "BAD ID"
slash = "/scheduler"
display_name = "Aksel"
description = "Calendar."
"""
        )
        with pytest.raises(ValidationError):
            AgentRegistry.from_toml(config)

    def test_empty_agents_section(self, tmp_path: Path) -> None:
        config = tmp_path / "agents.toml"
        config.write_text("# no agents\n")
        registry = AgentRegistry.from_toml(config)
        assert registry.all() == ()

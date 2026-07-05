"""Pin the leaf module's contract independent of any consumer.

The four duplication sites
(:class:`AgentDefinition.agent_id`, :class:`PhonebookEntry.agent_id`,
:class:`RoutingDecision.agent_id`, the bridge normalizer's mention scanner)
all import from this module, so the regex can't drift between them.
These tests pin the regex itself.
"""

from __future__ import annotations

import pytest

from calfcord.agents.identifier import (
    AGENT_ID_CHARSET,
    AGENT_ID_PATTERN,
    MCP_SLOT_PREFIX,
    MENTION_PREFIX,
    RESERVED_AGENT_IDS,
    reserved_agent_id_error,
)


class TestAgentIdPattern:
    @pytest.mark.parametrize(
        "value",
        ["scribe", "agent-1", "agent_2", "a", "x" * 32, "0", "a-b_c"],
    )
    def test_valid_ids_match(self, value: str) -> None:
        assert AGENT_ID_PATTERN.fullmatch(value) is not None

    @pytest.mark.parametrize(
        "value",
        [
            "",  # empty
            "Scribe",  # uppercase
            "x" * 33,  # over 32 chars
            "agent.id",  # dot not allowed
            "agent id",  # space not allowed
            "agent!",  # special char
            "@scribe",  # @ not part of charset
            "!scribe",  # the mention prefix is not part of the charset
        ],
    )
    def test_invalid_ids_reject(self, value: str) -> None:
        assert AGENT_ID_PATTERN.fullmatch(value) is None


class TestAgentIdCharset:
    def test_charset_constant_matches_validator_charset(self) -> None:
        # The normalizer builds its mention regex from this constant.
        # Pinning it guards against silent drift in the leaf module.
        assert AGENT_ID_CHARSET == "a-z0-9_-"

    def test_mention_prefix_is_bang_and_outside_the_charset(self) -> None:
        # The bridge scanner triggers on this exact character. Pinning it keeps the
        # trigger and the docs in lockstep, and asserts the prefix can never be a
        # legal agent-id char (else the scanner couldn't split prefix from id).
        assert MENTION_PREFIX == "!"
        assert AGENT_ID_PATTERN.fullmatch(MENTION_PREFIX) is None


class TestReservedAgentIds:
    """The workspace-slot names an agent id may never take (create-time guard).

    Agents share one process/slot namespace with the substrate (``broker``/
    ``bridge``), the ``tools`` singleton, and the ``mcp-<server>`` slots — an
    agent named after any of them would collide in the compose ``processes``
    dict or the ``state/run/<slot>.pid`` pidfile namespace. This module is the
    single source both the parse-time validator and the compose slot set import.
    """

    def test_reserved_set_pins_the_slot_names(self) -> None:
        # ``process-compose`` is reserved for the supervisor's own log: an agent by
        # that name would share state/logs/process-compose.log with the live
        # supervisor — and rotate-at-spawn would rename the supervisor's log out
        # from under it.
        assert frozenset({"broker", "bridge", "tools", "process-compose", "unstick"}) == RESERVED_AGENT_IDS
        assert MCP_SLOT_PREFIX == "mcp-"

    def test_reserved_set_matches_the_supervisor_slot_namespace(self) -> None:
        """The supervisor cannot import this module at module level (the agents
        package init pulls calfkit; the supervisor stays import-light), so the two
        process-slot literals are defined twice — this pins that subset so it
        cannot drift while allowing command-reserved names such as ``unstick``."""
        from calfcord.supervisor import compose

        assert compose._RESERVED_PROCESS_NAMES < RESERVED_AGENT_IDS
        assert MCP_SLOT_PREFIX == compose.MCP_SLOT_PREFIX

    @pytest.mark.parametrize("name", ["broker", "bridge", "tools", "process-compose", "unstick"])
    def test_reserved_names_yield_an_error(self, name: str) -> None:
        message = reserved_agent_id_error(name)
        assert message is not None
        assert name in message
        # The message must say WHY the name is off-limits (the process it collides with).
        assert "reserved" in message

    def test_unstick_error_names_the_command_collision(self) -> None:
        message = reserved_agent_id_error("unstick")
        assert message is not None
        assert "!unstick" in message
        assert "routing command" in message
        assert "pick another agent name" in message

    def test_process_compose_error_names_the_log_collision(self) -> None:
        # The collision is the supervisor's LOG file, not a process slot — the
        # reason shown must say so, not claim a bogus process-namespace clash.
        message = reserved_agent_id_error("process-compose")
        assert message is not None
        assert "log" in message
        assert "pick another agent name" in message

    @pytest.mark.parametrize("name", ["mcp-github", "mcp-", "mcp-x"])
    def test_mcp_prefix_yields_an_error(self, name: str) -> None:
        message = reserved_agent_id_error(name)
        assert message is not None
        assert "mcp-" in message
        assert "reserved" in message

    @pytest.mark.parametrize("name", ["scribe", "assistant", "mcp", "mcpx", "toolsmith", "bridges", "unstuck"])
    def test_ordinary_names_pass(self, name: str) -> None:
        assert reserved_agent_id_error(name) is None

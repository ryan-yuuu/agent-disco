"""Unit tests for the per-invocation peer-roster builder."""

from __future__ import annotations

from calfkit_organization.agents.peer_roster import build_temp_instructions
from calfkit_organization.agents.phonebook import PhonebookEntry


def _entry(
    agent_id: str,
    *,
    description: str = "test",
    tools: tuple[str, ...] = (),
) -> PhonebookEntry:
    return PhonebookEntry(
        agent_id=agent_id,
        display_name=agent_id.title(),
        description=description,
        tools=tools,
    )


class TestBuildTempInstructions:
    def test_returns_none_when_target_lacks_private_chat(self) -> None:
        """No A2A tool → no need to advertise peers; save the tokens."""
        phonebook = [_entry("alice", tools=()), _entry("bob", tools=())]
        assert build_temp_instructions(phonebook, "alice") is None

    def test_returns_none_when_target_not_in_phonebook(self) -> None:
        """Unknown target — nothing meaningful to say. Caller will hit
        its own error path elsewhere."""
        phonebook = [_entry("alice", tools=("private_chat",))]
        assert build_temp_instructions(phonebook, "ghost") is None

    def test_returns_none_when_no_peers_after_excluding_target(self) -> None:
        """A lone agent with private_chat has no one to call. Still
        return None — an empty roster string would be worse than nothing
        (it implies "there is a roster, it's empty")."""
        phonebook = [_entry("alice", tools=("private_chat",))]
        assert build_temp_instructions(phonebook, "alice") is None

    def test_lists_peers_with_descriptions_and_excludes_target(self) -> None:
        phonebook = [
            _entry("alice", description="Scheduler bot.", tools=("private_chat",)),
            _entry("bob", description="Note-taker.", tools=()),
            _entry("carol", description="Researcher.", tools=("private_chat",)),
        ]
        result = build_temp_instructions(phonebook, "alice")
        assert result is not None
        assert "alice" not in result  # excluded as the target
        assert "bob: Note-taker." in result
        assert "carol: Researcher." in result

    def test_peer_roster_advertises_only_via_private_chat(self) -> None:
        """The instruction header must mention the actual tool name so
        the LLM knows the connection between this roster and the tool
        available in its schema."""
        phonebook = [
            _entry("alice", tools=("private_chat",)),
            _entry("bob"),
        ]
        result = build_temp_instructions(phonebook, "alice")
        assert result is not None
        assert "private_chat" in result

    def test_peers_listed_with_other_tools_still_appear(self) -> None:
        """A peer's *own* tools don't gate visibility — only the target's
        do. A non-A2A peer is still a valid private_chat target as long
        as it's known, because A2A delivery uses the target's
        agent.{id}.in inbox (no tool needed on the receiving side)."""
        phonebook = [
            _entry("alice", tools=("private_chat",)),
            _entry("bob", tools=("calendar",)),
        ]
        result = build_temp_instructions(phonebook, "alice")
        assert result is not None
        assert "bob" in result

"""The shared agent/tool pick-lists in :mod:`calfcord.cli._agents`."""

from __future__ import annotations

from calfcord.cli._agents import pick_tools
from calfcord.cli._prompts import Choice


class _RecordingPrompter:
    """Records exactly what the checkbox was asked to show."""

    def __init__(self) -> None:
        self.message = ""
        self.instruction = ""

    def checkbox(self, message: str, choices: list[Choice], *, instruction: str = "") -> list[str]:
        self.message = message
        self.instruction = instruction
        return [c.value for c in choices if c.checked]

    def select(self, message: str, choices: list[Choice], *, default: str | None = None) -> str:
        raise AssertionError("pick_tools only uses the checkbox")

    def text(self, message: str, *, default: str = "") -> str:
        raise AssertionError("pick_tools only uses the checkbox")

    def secret(self, message: str) -> str:
        raise AssertionError("pick_tools only uses the checkbox")

    def confirm(self, message: str, *, default: bool = False) -> bool:
        raise AssertionError("pick_tools only uses the checkbox")

    def pause(self, message: str) -> None:
        raise AssertionError("pick_tools only uses the checkbox")


class TestPickToolsSeparatesTheQuestionFromTheGuidance:
    """The title asks; the instruction explains. They are different jobs.

    The guidance used to be crammed into the panel title as a parenthetical,
    which made the title a paragraph — while ``instruction``, which the Protocol
    declares precisely for this, went unused and looked like dead speculation.
    Both problems had the same fix.
    """

    def _pick(self) -> _RecordingPrompter:
        prompter = _RecordingPrompter()
        pick_tools(prompter, name="scribe", mcp_servers_fn=list, live_tools_fn=dict)
        return prompter

    def test_the_title_is_just_the_question(self) -> None:
        message = self._pick().message
        assert "scribe" in message
        assert "deselect" not in message, "guidance belongs in the instruction, not the title"

    def test_the_guidance_is_passed_as_the_instruction(self) -> None:
        instruction = self._pick().instruction
        assert "deselect" in instruction

    def test_the_instruction_does_not_restate_the_key_mechanics(self) -> None:
        """The hint in the panel border already says space/enter for every list."""
        instruction = self._pick().instruction
        assert "space toggles" not in instruction

"""The shared agent/tool pick-lists in :mod:`calfcord.cli._agents`."""

from __future__ import annotations

from pathlib import Path

from calfcord.agents.identifier import AGENT_ID_PATTERN
from calfcord.cli._agents import CREATE_SENTINEL, pick_agent, pick_tools
from calfcord.cli._prompts import Choice
from calfcord.cli.tui.state import SelectState


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


class _ScriptedPrompter:
    """Answers ``select`` with a scripted row and records what it was shown.

    The scripted answer is asserted to be one of the values actually offered:
    the real widget can only ever return a row it painted, so a fake free to
    return anything else would let a test pass on an answer no operator could
    have given.
    """

    def __init__(self, answer: str) -> None:
        self._answer = answer
        self.message = ""
        self.choices: list[Choice] = []

    def select(self, message: str, choices: list[Choice], *, default: str | None = None) -> str:
        self.message = message
        self.choices = choices
        offered = [c.value for c in choices]
        assert self._answer in offered, f"scripted answer {self._answer!r} is not among {offered}"
        return self._answer

    def text(self, message: str, *, default: str = "") -> str:
        raise AssertionError("pick_agent only uses the select")

    def secret(self, message: str) -> str:
        raise AssertionError("pick_agent only uses the select")

    def confirm(self, message: str, *, default: bool = False) -> bool:
        raise AssertionError("pick_agent only uses the select")

    def pause(self, message: str) -> None:
        raise AssertionError("pick_agent only uses the select")

    def checkbox(self, message: str, choices: list[Choice], *, instruction: str = "") -> list[str]:
        raise AssertionError("pick_agent only uses the select")


def _roster(agents_dir: Path, *names: str) -> Path:
    """Create ``agents_dir`` holding one ``.md`` per name.

    ``detect_agents`` only globs stems, so the bodies are irrelevant here.
    """
    agents_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (agents_dir / f"{name}.md").write_text("---\nname: x\n---\nbody\n")
    return agents_dir


class TestTheCreateRowSitsLast:
    """Bottom placement keeps 'start what I have' the enter-through default.

    ``SelectState`` opens on row 0, so a create row at the top would make
    launching a wizard the default answer to a command whose common intent is
    to start an existing agent. Wrap-around navigation (``ListState.up``) still
    puts it one keypress away.
    """

    def test_create_is_appended_after_the_agents(self, tmp_path: Path) -> None:
        agents_dir = _roster(tmp_path / "agents", "assistant", "scribe")
        prompter = _ScriptedPrompter("assistant")

        pick_agent(prompter, agents_dir=agents_dir, message="Which agent?", create_fn=lambda: "new")

        assert [c.value for c in prompter.choices] == ["assistant", "scribe", CREATE_SENTINEL]

    def test_the_label_marks_it_as_new_and_as_opening_a_further_flow(self, tmp_path: Path) -> None:
        """``+`` says new; the ellipsis is the convention for 'this opens more'."""
        agents_dir = _roster(tmp_path / "agents", "assistant")
        prompter = _ScriptedPrompter("assistant")

        pick_agent(prompter, agents_dir=agents_dir, message="Which agent?", create_fn=lambda: "new")

        label = next(c.label for c in prompter.choices if c.value == CREATE_SENTINEL)
        assert label.startswith("+")
        assert label.endswith("…")


class TestPickingARow:
    def test_picking_an_agent_returns_it_and_creates_nothing(self, tmp_path: Path) -> None:
        agents_dir = _roster(tmp_path / "agents", "assistant", "scribe")
        prompter = _ScriptedPrompter("scribe")
        created = False

        def create_fn() -> str:
            nonlocal created
            created = True
            return "new"

        assert pick_agent(prompter, agents_dir=agents_dir, message="?", create_fn=create_fn) == "scribe"
        assert not created, "picking an existing agent must not run the create flow"

    def test_picking_create_returns_the_newly_created_name(self, tmp_path: Path) -> None:
        agents_dir = _roster(tmp_path / "agents", "assistant")
        prompter = _ScriptedPrompter(CREATE_SENTINEL)

        result = pick_agent(prompter, agents_dir=agents_dir, message="?", create_fn=lambda: "researcher")

        assert result == "researcher"

    def test_a_create_that_failed_leaves_nothing_to_start(self, tmp_path: Path) -> None:
        """``create_fn`` returning None means it already reported the failure."""
        agents_dir = _roster(tmp_path / "agents", "assistant")
        prompter = _ScriptedPrompter(CREATE_SENTINEL)

        assert pick_agent(prompter, agents_dir=agents_dir, message="?", create_fn=lambda: None) is None


class TestTheSentinelCannotCollideWithAnAgent:
    """A collision raises in ``ListState``, so NO agent could be started at all.

    See :data:`CREATE_SENTINEL` for why the separator — and not
    ``AGENT_ID_PATTERN`` — is what rules one out.
    """

    def test_the_sentinel_could_never_be_a_filename_stem(self) -> None:
        assert "/" in CREATE_SENTINEL
        # Even the file that looks like it would yield the sentinel does not.
        assert Path(f"{CREATE_SENTINEL}.md").stem != CREATE_SENTINEL

    def test_the_sentinel_is_not_a_valid_agent_id(self) -> None:
        """Belt and braces: were it ever to leak to ``roster.agent_start``, that
        command's name-shape check rejects it loudly rather than starting
        something unexpected. (Necessary, but NOT what prevents the collision.)
        """
        assert not AGENT_ID_PATTERN.fullmatch(CREATE_SENTINEL)

    def test_an_adversarially_named_file_cannot_break_the_picker(self, tmp_path: Path) -> None:
        """The regression test for the crash the ``+create`` sentinel allowed.

        Such a file is already broken — ``parse_agent_md`` enforces
        ``stem == name`` and the loader would reject it — but a broken file must
        degrade the way it did before the create row existed: the row is offered
        and ``agent start`` refuses it by name shape. It must not take the whole
        picker down, least of all while the operator is trying to diagnose it.
        """
        agents_dir = _roster(tmp_path / "agents", "scribe")
        (agents_dir / "+create.md").write_text("---\nname: x\n---\nbody\n")
        prompter = _ScriptedPrompter("scribe")

        assert pick_agent(prompter, agents_dir=agents_dir, message="?", create_fn=lambda: "new") == "scribe"

        values = [c.value for c in prompter.choices]
        assert len(values) == len(set(values)), "duplicate values make ListState raise"
        # The fakes never build one, which is exactly how the crash slipped
        # through: assert the REAL widget accepts the rows the picker composed.
        SelectState(prompter.choices)


class TestAnEmptyRoster:
    """With create on offer, an empty roster is answerable rather than a dead end."""

    def test_create_is_the_only_row(self, tmp_path: Path) -> None:
        agents_dir = _roster(tmp_path / "agents")
        prompter = _ScriptedPrompter(CREATE_SENTINEL)

        result = pick_agent(prompter, agents_dir=agents_dir, message="?", create_fn=lambda: "first")

        assert result == "first"
        assert [c.value for c in prompter.choices] == [CREATE_SENTINEL]
        SelectState(prompter.choices)  # a one-row list must satisfy the real widget

    def test_the_searched_directory_is_still_named(self, tmp_path: Path, capsys) -> None:
        """How an operator spots a wrong ``$CALFCORD_HOME`` — don't lose it.

        Asserts the picker still OPENED, so this pins the fact to the new
        offer-create path rather than passing on the old dead-end, which
        happened to print the directory on its way out.
        """
        agents_dir = _roster(tmp_path / "agents")
        prompter = _ScriptedPrompter(CREATE_SENTINEL)

        pick_agent(prompter, agents_dir=agents_dir, message="?", create_fn=lambda: "first")

        assert prompter.choices, "the directory must be named ABOVE the picker, not instead of it"
        assert str(agents_dir) in capsys.readouterr().out

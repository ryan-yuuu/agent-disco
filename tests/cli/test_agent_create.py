"""Tests for ``disco agent create`` — the reusable agent-creation flow.

The flow is pure logic over an injected :class:`Prompter`, so these tests never
touch a TTY or InquirerPy. A scripted :class:`FakePrompter` dequeues one answer
per prompt kind in call order; the provider sub-flow is delegated to
:func:`calfcord.cli._providers.configure_provider`, which would reach a real SDK
/ model catalog, so every test monkeypatches it (in ``agent_create``'s
namespace) to a fixed ``(provider, model)`` — no network, key, or OAuth ever
fires. We assert on the written ``agents/<name>.md`` (via ``parse_agent_md``),
the printed guidance (via ``capsys``), and the exit code.
"""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path

import frontmatter
import pytest

from calfcord.agents.definition import parse_agent_md
from calfcord.cli import _agents, agent_create
from calfcord.cli._agents import MCP_DISCOVER_ROW, STARTER_AGENT_NAME
from calfcord.cli._prompts import Choice, Prompter

_FIXED_PROVIDER = ("anthropic", "claude-haiku-4-5")


class FakePrompter:
    """A scripted :class:`Prompter`: each method pops the next queued answer.

    Answers are queued per prompt kind so a test only scripts the kinds its path
    actually hits, in call order. Running a queue dry raises rather than hanging,
    so a miscounted script surfaces as a clear failure. ``checkbox`` records the
    choices it was offered (``last_checkbox_choices``) and, with no scripted
    result, returns every pre-checked row (mirrors InquirerPy's enter-on-default).
    """

    def __init__(
        self,
        *,
        selects: list[str] | None = None,
        texts: list[str] | None = None,
        secrets: list[str] | None = None,
        confirms: list[bool] | None = None,
        checkboxes: list[list[str]] | None = None,
    ) -> None:
        self._selects = deque(selects or [])
        self._texts = deque(texts or [])
        self._secrets = deque(secrets or [])
        self._confirms = deque(confirms or [])
        self._checkboxes = deque(checkboxes or [])
        self.last_checkbox_choices: list[Choice] = []

    def select(self, message: str, choices: list[Choice], *, default: str | None = None) -> str:
        if not self._selects:
            raise AssertionError(f"unexpected select(): {message!r}")
        return self._selects.popleft()

    def text(self, message: str, *, default: str = "") -> str:
        if not self._texts:
            raise AssertionError(f"unexpected text(): {message!r}")
        return self._texts.popleft()

    def secret(self, message: str) -> str:
        if not self._secrets:
            raise AssertionError(f"unexpected secret(): {message!r}")
        return self._secrets.popleft()

    def confirm(self, message: str, *, default: bool = False) -> bool:
        if not self._confirms:
            raise AssertionError(f"unexpected confirm(): {message!r}")
        return self._confirms.popleft()

    def pause(self, message: str) -> None:
        return None

    def checkbox(self, message: str, choices: list[Choice], *, instruction: str = "") -> list[str]:
        self.last_checkbox_choices = choices
        if not self._checkboxes:
            return [c.value for c in choices if c.checked]
        return self._checkboxes.popleft()


@pytest.fixture(autouse=True)
def _stub_configure_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the provider sub-flow with a fixed ``(provider, model)``.

    ``configure_provider`` is imported into ``agent_create``'s namespace, so the
    stub is installed there. It consumes no prompts, so tests don't script
    provider answers — keeping every create-flow test free of any provider SDK /
    network.
    """

    def _fixed(prompter: object, **_: object) -> tuple[str, str]:
        return _FIXED_PROVIDER

    monkeypatch.setattr(agent_create, "configure_provider", _fixed)


def _prompter(
    *,
    name: str,
    description: str = "d",
    checkboxes: list[list[str]] | None = None,
    confirms: list[bool] | None = None,
) -> FakePrompter:
    """Script one create pass: text(name), text(description), checkbox(tools).

    The provider sub-flow is stubbed (consumes no prompts). ``run`` offers an
    "edit prompt now?" confirm after the write; supply ``confirms=[False]`` for
    that path (the default below declines it so no ``$EDITOR`` is ever launched).
    """
    return FakePrompter(
        texts=[name, description],
        checkboxes=checkboxes,
        confirms=confirms if confirms is not None else [False],
    )


def test_run_creates_agent_md(tmp_path: Path) -> None:
    """A full create pass writes a re-parseable ``<name>.md`` with the chosen fields."""
    agents_dir = tmp_path / "agents"
    env_path = tmp_path / ".env"
    # Memory defaults on, so a tools pick that omits write_file is topped up.
    prompter = _prompter(name="scribe", description="Takes notes", checkboxes=[["read_file", "web_search"]])

    rc = agent_create.run(prompter, agents_dir=agents_dir, env_path=env_path, name=None, home=None)
    assert rc == 0

    md = agents_dir / "scribe.md"
    assert md.is_file()
    agent = parse_agent_md(md)
    assert agent.agent_id == "scribe"
    assert agent.description == "Takes notes"
    assert agent.provider == "anthropic"
    assert agent.model == "claude-haiku-4-5"
    assert agent.memory is True
    assert frontmatter.load(md).metadata["memory"] is True
    assert set(agent.tools) == {"read_file", "web_search", "write_file"}


def test_run_prints_created_and_next_step(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Success names the agent, then (on a dev run with no supervisor home) degrades to
    the honest manual bring-online sequence: open the workspace if it isn't running,
    then start the agent — ``disco start`` + ``disco agent start``."""
    agents_dir = tmp_path / "agents"
    prompter = _prompter(name="scribe")
    assert agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name=None, home=None) == 0

    out = capsys.readouterr().out
    assert "Created agent 'scribe'." in out
    assert "Bring scribe online:" in out
    assert "disco start" in out
    assert "disco agent start scribe" in out


def test_run_passes_name_default_through(tmp_path: Path) -> None:
    """A given ``name`` pre-fills the prompt; the fake returns it, so it's used."""
    agents_dir = tmp_path / "agents"
    # The name prompt returns the default that ``run`` passed as name_default.
    prompter = FakePrompter(texts=["scout", "d"], confirms=[False])
    assert agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name="scout", home=None) == 0
    assert (agents_dir / "scout.md").is_file()


def test_run_slugifies_typed_name(tmp_path: Path) -> None:
    """A typed friendly name is slugified into a valid stem before write."""
    agents_dir = tmp_path / "agents"
    prompter = _prompter(name="My Helper!")
    assert agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name=None, home=None) == 0
    assert (agents_dir / "my_helper.md").is_file()
    assert parse_agent_md(agents_dir / "my_helper.md").agent_id == "my_helper"


def test_run_blank_description_uses_default(tmp_path: Path) -> None:
    """A blank description falls back to the seed default."""
    agents_dir = tmp_path / "agents"
    prompter = _prompter(name="scribe", description="")
    assert agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name=None, home=None) == 0
    assert parse_agent_md(agents_dir / "scribe.md").description == _agents.DEFAULT_DESCRIPTION


def test_run_tricky_description_roundtrips(tmp_path: Path) -> None:
    """A YAML-significant description ('Has: colon') survives the create path verbatim."""
    agents_dir = tmp_path / "agents"
    prompter = _prompter(name="scribe", description="Has: colon")
    assert agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name=None, home=None) == 0
    assert parse_agent_md(agents_dir / "scribe.md").description == "Has: colon"


def test_run_offers_prompt_edit_when_confirmed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Confirming the optional prompt step calls ``edit_system_prompt`` on the new file."""
    agents_dir = tmp_path / "agents"
    seen: list[Path] = []

    def _spy(md_path: Path) -> None:
        seen.append(md_path)

    # The lazy import in create_agent resolves ``edit_system_prompt`` from the
    # agent_edit module, so patch it there.
    from calfcord.cli import agent_edit

    monkeypatch.setattr(agent_edit, "edit_system_prompt", _spy)

    prompter = _prompter(name="scribe", confirms=[True])
    assert agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name=None, home=None) == 0
    assert seen == [agents_dir / "scribe.md"]


def test_run_declining_prompt_edit_does_not_launch_editor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Declining the optional prompt step never touches ``edit_system_prompt``."""
    agents_dir = tmp_path / "agents"

    def _boom(md_path: Path) -> None:
        raise AssertionError("edit_system_prompt must not run when the operator declines")

    from calfcord.cli import agent_edit

    monkeypatch.setattr(agent_edit, "edit_system_prompt", _boom)

    prompter = _prompter(name="scribe", confirms=[False])
    assert agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name=None, home=None) == 0


def test_run_write_failure_returns_1_without_banner(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forced write failure returns 1, prints 'error:', and never prints the banner.

    The create path validates before writing, so to force a *write* failure we
    monkeypatch the atomic-write helper to raise ``OSError`` — ``run`` must
    surface it and stop, leaving no half-created agent and no success banner.
    """

    def _boom(path: Path, payload: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(_agents, "atomic_write", _boom)

    agents_dir = tmp_path / "agents"
    prompter = _prompter(name="scribe")
    rc = agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name="scribe", home=None)

    out = capsys.readouterr().out
    assert rc == 1
    assert "error: could not create agent 'scribe'" in out
    assert "Created agent" not in out
    assert not (agents_dir / "scribe.md").exists()


async def _workspace_up(_home: Path) -> bool:
    return True


async def _workspace_down(_home: Path) -> bool:
    return False


def test_create_for_start_returns_the_name_when_the_workspace_is_open(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The ordinary path: hand the name back and let the caller start it.

    No next-steps hint here — the agent comes online on the very next line, so
    telling the operator how to bring it online would be noise.
    """
    name = agent_create.create_for_start(
        _prompter(name="scribe"),
        agents_dir=tmp_path / "agents",
        env_path=tmp_path / ".env",
        home=tmp_path / "home",
        workspace_running_fn=_workspace_up,
    )

    assert name == "scribe"
    assert "Bring scribe online" not in capsys.readouterr().out


def test_create_for_start_gives_the_two_steps_in_the_order_they_are_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A closed workspace: say the steps, in the order they must be done.

    The bare ``disco agent start`` that got here never typed a name, so
    ``disco agent start <name>`` is the one command the operator has not met —
    but it is USELESS before ``disco start``, so the order is the point. Printing
    the pair here (rather than letting the roster refuse) is also what keeps them
    ordered: the refusal names only ``disco start``, and any hint the create step
    printed would land above it, listing step 2 before step 1.
    """
    name = agent_create.create_for_start(
        _prompter(name="scribe"),
        agents_dir=tmp_path / "agents",
        env_path=tmp_path / ".env",
        home=tmp_path / "home",
        workspace_running_fn=_workspace_down,
    )

    assert name is None, "a start that cannot succeed must not be attempted"
    out = capsys.readouterr().out
    assert "Created agent 'scribe'." in out, "the agent IS on disk — say so"
    assert out.index("disco start") < out.index("disco agent start scribe"), (
        "the workspace must be opened before the agent can be clocked in"
    )


def test_create_for_start_says_nothing_about_starting_a_failed_create(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No agent landed on disk, so naming a command to start one would mislead."""

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(agent_create, "create_agent", _boom)

    name = agent_create.create_for_start(
        _prompter(name="scribe"),
        agents_dir=tmp_path / "agents",
        env_path=tmp_path / ".env",
        home=tmp_path / "home",
        workspace_running_fn=_workspace_down,
    )

    assert name is None
    out = capsys.readouterr().out
    assert "disco agent start" not in out
    assert "error:" in out


def test_create_agent_returns_name_and_provider(tmp_path: Path) -> None:
    """The extracted flow returns ``(name, provider)`` for the caller's guidance."""
    agents_dir = tmp_path / "agents"
    prompter = _prompter(name="scribe")
    name, provider = agent_create.create_agent(
        prompter,
        agents_dir=agents_dir,
        env_path=tmp_path / ".env",
        prune_seed=False,
        offer_prompt=False,
    )
    assert name == "scribe"
    assert provider == "anthropic"


def test_create_agent_returns_created_agent_with_named_fields(tmp_path: Path) -> None:
    """The result is a ``CreatedAgent`` exposing ``.name``/``.provider`` so callers
    can't transpose the two same-typed strings (``init`` reads ``.provider``,
    ``agent create`` reads ``.name``)."""
    agents_dir = tmp_path / "agents"
    prompter = _prompter(name="scribe")
    created = agent_create.create_agent(
        prompter,
        agents_dir=agents_dir,
        env_path=tmp_path / ".env",
        prune_seed=False,
        offer_prompt=False,
    )
    assert isinstance(created, agent_create.CreatedAgent)
    assert created.name == "scribe"
    assert created.provider == "anthropic"


def test_create_agent_blank_name_with_one_non_assistant_edits_it_in_place(tmp_path: Path) -> None:
    """With ``name_default=None`` and exactly one existing non-``assistant`` agent, a
    blank typed name keeps the lone agent as the default — so the flow edits that
    agent in place and returns its name (it does not fall back to ``assistant``)."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "scribe.md").write_text(
        "---\n"
        "name: scribe\n"
        "description: old\n"
        "provider: openai\n"
        "model: gpt-5\n"
        "tools: [read_file]\n"
        "---\n\n"
        "You are Scribe, the note-taker.\n",
        encoding="utf-8",
    )

    # Blank typed name → keeps the lone-agent default ("scribe").
    prompter = FakePrompter(texts=["   ", "updated desc"], confirms=[False])
    created = agent_create.create_agent(
        prompter,
        agents_dir=agents_dir,
        env_path=tmp_path / ".env",
        name_default=None,
        prune_seed=False,
        offer_prompt=False,
    )

    assert created.name == "scribe"
    assert not (agents_dir / "assistant.md").exists()
    assert {p.stem for p in agents_dir.glob("*.md")} == {"scribe"}
    assert parse_agent_md(agents_dir / "scribe.md").description == "updated desc"


def test_create_agent_prune_seed_false_keeps_pristine_assistant(tmp_path: Path) -> None:
    """With ``prune_seed=False`` (the ``agent create`` default) a pristine seed survives.

    Adding a second agent must never delete the operator's starter — only
    ``init``'s first-run opt-in prunes a pristine seed.
    """
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True)
    seed = agents_dir / "assistant.md"
    seed.write_text(
        "---\n"
        "name: assistant\n"
        f"description: {_agents.DEFAULT_DESCRIPTION}\n"
        "tools: []\n"
        "---\n\n"
        "You are Assistant, a helpful general-purpose AI teammate. Answer clearly.\n",
        encoding="utf-8",
    )

    prompter = _prompter(name="scribe")
    agent_create.create_agent(
        prompter,
        agents_dir=agents_dir,
        env_path=tmp_path / ".env",
        prune_seed=False,
        offer_prompt=False,
    )

    assert (agents_dir / "scribe.md").is_file()
    # The pristine starter is left intact (byte-for-byte unchanged is not
    # required, but it must still parse as the seeded assistant).
    assert seed.is_file()
    assert parse_agent_md(seed).description == _agents.DEFAULT_DESCRIPTION


def test_create_agent_does_not_write_default_provider_env(tmp_path: Path) -> None:
    """``create_agent`` must not touch ``CALFKIT_AGENT_DEFAULT_PROVIDER`` (init's concern)."""
    from calfcord.cli._envfile import read_env

    agents_dir = tmp_path / "agents"
    env_path = tmp_path / ".env"
    prompter = _prompter(name="scribe")
    agent_create.create_agent(
        prompter,
        agents_dir=agents_dir,
        env_path=env_path,
        prune_seed=False,
        offer_prompt=False,
    )
    # The agent carries an explicit provider/model; the install-wide default
    # provider env var is never written by this flow.
    assert "CALFKIT_AGENT_DEFAULT_PROVIDER" not in read_env(env_path)


def test_fake_prompter_satisfies_protocol() -> None:
    """Guard that the test fake stays structurally compatible with the seam."""
    assert isinstance(FakePrompter(), Prompter)


def test_pick_tools_prechecks_discover_row_named_rows_unchecked(monkeypatch) -> None:
    """The create wizard pre-checks builtins AND the MCP discover row (so a
    wizard agent matches a hand-authored ``mcp: true`` default); the *named*
    ``mcp/<server>`` and live per-tool rows start unchecked."""
    from calfcord.cli import _agents
    from calfcord.tools.discord import DISCORD_TOOL_NAMES

    prompter = FakePrompter(checkboxes=[["terminal"]])
    selected = _agents.pick_tools(
        prompter,
        "helper",
        mcp_servers_fn=lambda: ["github"],
        live_tools_fn=lambda: {"github": ["search"]},
    )
    assert selected.tools == ["terminal"]
    assert selected.mcp == []
    by_value = {c.value: c for c in prompter.last_checkbox_choices}
    assert by_value[MCP_DISCOVER_ROW].checked is True
    assert by_value["mcp/github"].checked is False
    assert by_value["mcp/github/search"].checked is False
    # Builtins — including bridge Discord reads — are the pre-checked default.
    assert by_value["terminal"].checked is True
    for name in DISCORD_TOOL_NAMES:
        assert by_value[name].checked is True


def test_pick_tools_default_selection_yields_discover(monkeypatch) -> None:
    """Leaving the pre-checked discover row ticked yields ``mcp=True`` — the
    wizard's default posture, matching a frontmatter that omits ``mcp:``."""
    from calfcord.cli import _agents

    prompter = FakePrompter(checkboxes=[["terminal", MCP_DISCOVER_ROW]])
    selected = _agents.pick_tools(prompter, "helper", mcp_servers_fn=lambda: [], live_tools_fn=lambda: {})
    assert selected.mcp is True


def test_pick_tools_accepting_defaults_omits_tools_including_discord() -> None:
    """Enter-on-default (every pre-checked row) must yield ``tools=None`` so the
    created agent keeps live discovery — Discord reads included — rather than a
    pinned snapshot."""
    from calfcord.cli import _agents
    from calfcord.tools import TOOL_REGISTRY
    from calfcord.tools.discord import DISCORD_TOOL_NAMES

    # No scripted checkbox answer → FakePrompter returns every pre-checked row.
    prompter = FakePrompter()
    selected = _agents.pick_tools(prompter, "helper", mcp_servers_fn=lambda: [], live_tools_fn=lambda: {})
    assert selected.tools is None
    assert selected.mcp is True
    checked = {c.value for c in prompter.last_checkbox_choices if c.checked}
    assert checked >= DISCORD_TOOL_NAMES
    assert checked >= set(TOOL_REGISTRY)


def test_pick_tools_splits_selected_mcp_rows(monkeypatch) -> None:
    """The create checkbox keeps ``mcp/...`` as UI values but returns canonical
    split grants for the writer."""
    from calfcord.cli import _agents

    prompter = FakePrompter(checkboxes=[["terminal", "mcp/github", "mcp/github/search"]])
    selected = _agents.pick_tools(
        prompter,
        "helper",
        mcp_servers_fn=lambda: ["github"],
        live_tools_fn=lambda: {"github": ["search"]},
    )
    assert selected.tools == ["terminal"]
    assert selected.mcp == ["github", "github/search"]


def _write_agent_tri(agents_dir: Path, mcp) -> Path:
    """Create an agent via the create-path ``write_agent`` and return its path."""
    return _agents.write_agent(
        agents_dir,
        name="scout",
        description="A scout.",
        provider="anthropic",
        model="claude-sonnet-4-5",
        tools=["read_file"],
        mcp=mcp,
    )


def test_write_agent_discover_omits_mcp_key(tmp_path: Path) -> None:
    """The create path writes discover (``mcp=True``) as an omitted key, which
    parses back to the discover default."""
    md = _write_agent_tri(tmp_path, True)
    assert "mcp" not in frontmatter.load(md).metadata
    assert parse_agent_md(md).mcp is True


def test_write_agent_opt_out_writes_false(tmp_path: Path) -> None:
    """The create path writes opt-out (``mcp=False`` / ``[]``) as ``mcp: false``,
    NOT an omitted key — so a wizard-created builtins-only agent doesn't silently
    discover every MCP server."""
    for value in (False, []):
        md = _write_agent_tri(tmp_path, value)
        assert frontmatter.load(md).metadata["mcp"] is False
        assert parse_agent_md(md).mcp is False


def test_write_agent_named_grants_write_list(tmp_path: Path) -> None:
    md = _write_agent_tri(tmp_path, ["github"])
    assert frontmatter.load(md).metadata["mcp"] == ["github"]
    assert parse_agent_md(md).mcp == ("github",)


def test_write_agent_defaults_memory_on(tmp_path: Path) -> None:
    """New agents get an explicit ``memory: true`` so create/init teammates
    start with the notepad enabled without changing the schema default for
    omitted fields."""
    md = _agents.write_agent(
        tmp_path,
        name="scribe",
        description="Takes notes.",
        provider="anthropic",
        model="claude-sonnet-4-5",
        tools=None,
    )
    assert frontmatter.load(md).metadata["memory"] is True
    assert parse_agent_md(md).memory is True


def test_write_agent_memory_false_is_honored(tmp_path: Path) -> None:
    md = _agents.write_agent(
        tmp_path,
        name="ephemeral",
        description="No notepad.",
        provider="anthropic",
        model="claude-sonnet-4-5",
        tools=["terminal"],
        memory=False,
    )
    assert frontmatter.load(md).metadata["memory"] is False
    assert parse_agent_md(md).memory is False
    assert parse_agent_md(md).tools == ("terminal",)


def test_write_agent_memory_on_adds_missing_fs_tools(tmp_path: Path) -> None:
    """An explicit tools list missing read_file/write_file is topped up when
    memory is on, matching the factory's requirement without failing create."""
    md = _agents.write_agent(
        tmp_path,
        name="scribe",
        description="Takes notes.",
        provider="anthropic",
        model="claude-sonnet-4-5",
        tools=["terminal", "web_search"],
        memory=True,
    )
    assert parse_agent_md(md).tools == ("terminal", "web_search", "read_file", "write_file")


def test_write_agent_update_preserves_existing_memory(tmp_path: Path) -> None:
    """Re-running create against an on-disk agent must not flip its memory bit."""
    md = tmp_path / "scribe.md"
    md.write_text(
        "---\n"
        "name: scribe\n"
        "description: old\n"
        "provider: anthropic\n"
        "model: claude-sonnet-4-5\n"
        "memory: false\n"
        "---\n"
        "Body stays.\n",
        encoding="utf-8",
    )
    _agents.write_agent(
        tmp_path,
        name="scribe",
        description="new",
        provider="openai",
        model="gpt-5",
        tools=["read_file"],
        memory=True,
    )
    agent = parse_agent_md(md)
    assert agent.memory is False
    assert agent.description == "new"
    assert md.read_text(encoding="utf-8").endswith("Body stays.\n")


def test_write_agent_update_memory_on_tops_up_fs_tools(tmp_path: Path) -> None:
    """Updating a memory-on agent with a restricted tools list still gets fs tools."""
    md = tmp_path / "scribe.md"
    md.write_text(
        "---\n"
        "name: scribe\n"
        "description: old\n"
        "provider: anthropic\n"
        "model: claude-sonnet-4-5\n"
        "memory: true\n"
        "tools: [read_file, write_file]\n"
        "---\n"
        "Body stays.\n",
        encoding="utf-8",
    )
    _agents.write_agent(
        tmp_path,
        name="scribe",
        description="new",
        provider="anthropic",
        model="claude-sonnet-4-5",
        tools=["terminal"],
        memory=False,  # ignored on update; on-disk memory stays true
    )
    agent = parse_agent_md(md)
    assert agent.memory is True
    assert agent.tools == ("terminal", "read_file", "write_file")


def test_write_agent_update_memory_off_does_not_add_fs_tools(tmp_path: Path) -> None:
    """Updating a memory-off agent must not inject filesystem tools."""
    md = tmp_path / "scribe.md"
    md.write_text(
        "---\n"
        "name: scribe\n"
        "description: old\n"
        "provider: anthropic\n"
        "model: claude-sonnet-4-5\n"
        "memory: false\n"
        "tools: [terminal]\n"
        "---\n"
        "Body stays.\n",
        encoding="utf-8",
    )
    _agents.write_agent(
        tmp_path,
        name="scribe",
        description="new",
        provider="anthropic",
        model="claude-sonnet-4-5",
        tools=["terminal", "web_search"],
        memory=True,  # ignored on update
    )
    agent = parse_agent_md(md)
    assert agent.memory is False
    assert agent.tools == ("terminal", "web_search")


def test_create_agent_forwards_live_tools_fn_to_pick_tools(tmp_path: Path) -> None:
    """``create_agent`` threads an injected ``live_tools_fn`` into the tools
    checkbox, so a caller (``init``) can supply the live MCP view — or suppress
    it — instead of always probing the broker's default capability view."""
    agents_dir = tmp_path / "agents"
    prompter = _prompter(name="scout")

    agent_create.create_agent(
        prompter,
        agents_dir=agents_dir,
        env_path=tmp_path / ".env",
        prune_seed=False,
        offer_prompt=False,
        live_tools_fn=lambda: {"github": ["search"]},
    )

    # The injected view is the ONLY source of an mcp row here (conftest stubs
    # _default_mcp_servers -> []), so its presence proves the fn was forwarded.
    by_value = {c.value: c for c in prompter.last_checkbox_choices}
    assert "mcp/github/search" in by_value


def test_standalone_create_agent_probes_live_view_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no injected ``live_tools_fn`` (standalone ``agent create``), the
    tools step still resolves the live capability view — only ``init``
    suppresses it. Guards against the default silently going no-op."""
    from calfcord.cli import agent_tools

    calls: list[object] = []

    def _spy() -> dict[str, list[str]]:
        calls.append(())
        return {}

    monkeypatch.setattr(agent_tools, "_default_live_tools", _spy)

    agent_create.create_agent(
        _prompter(name="scout"),
        agents_dir=tmp_path / "agents",
        env_path=tmp_path / ".env",
        prune_seed=False,
        offer_prompt=False,
    )

    assert calls == [()]


# ---------------------------------------------------------------------------
# Change B — standalone create requires an explicit name (no silent default,
# no silent overwrite of an existing agent).
# ---------------------------------------------------------------------------


def _seed_agent(agents_dir: Path, name: str, *, description: str = "old") -> None:
    """Write a minimal valid ``<name>.md`` so existing-name gating has a target."""
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{name}.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "provider: openai\n"
        "model: gpt-5\n"
        "tools: [read_file]\n"
        "---\n\n"
        f"You are {name}.\n",
        encoding="utf-8",
    )


def test_run_blank_name_reprompts_no_silent_default(tmp_path: Path) -> None:
    """Standalone create has NO name default: a blank answer re-prompts (keep-asking)
    rather than silently falling back to an existing agent or the starter name."""
    agents_dir = tmp_path / "agents"
    _seed_agent(agents_dir, "assistant", description="lone-existing")
    # First name answer is blank (must re-ask), then a real name is supplied.
    prompter = FakePrompter(texts=["", "scribe", "d"], confirms=[False])
    rc = agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name=None, home=None)
    assert rc == 0
    # The blank was NOT taken as "edit the lone existing agent": a fresh scribe
    # landed and the pre-existing assistant is untouched.
    assert (agents_dir / "scribe.md").is_file()
    assert parse_agent_md(agents_dir / "assistant.md").description == "lone-existing"


def test_run_existing_name_gate_declined_reprompts_for_different_name(tmp_path: Path) -> None:
    """Naming an existing agent triggers the explicit 'update it?' gate; declining
    (default No) re-prompts for a different name — never a silent overwrite."""
    agents_dir = tmp_path / "agents"
    _seed_agent(agents_dir, "scribe", description="original")
    # Type the existing name -> gate confirm=False -> re-prompt -> type a new name.
    prompter = FakePrompter(texts=["scribe", "scout", "d"], confirms=[False, False])
    rc = agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name=None, home=None)
    assert rc == 0
    # The declined existing agent is untouched; the fresh different-named one exists.
    assert parse_agent_md(agents_dir / "scribe.md").description == "original"
    assert (agents_dir / "scout.md").is_file()


def test_run_existing_name_gate_accepted_updates_in_place(tmp_path: Path) -> None:
    """Accepting the 'update it?' gate edits the existing agent in place."""
    agents_dir = tmp_path / "agents"
    _seed_agent(agents_dir, "scribe", description="original")
    # Type the existing name -> gate confirm=True -> proceed to update; new desc.
    prompter = FakePrompter(texts=["scribe", "updated desc"], confirms=[True, False])
    rc = agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name=None, home=None)
    assert rc == 0
    assert parse_agent_md(agents_dir / "scribe.md").description == "updated desc"
    assert {p.stem for p in agents_dir.glob("*.md")} == {"scribe"}


def test_run_positional_name_pre_answers_the_prompt(tmp_path: Path) -> None:
    """A positional ``disco agent create scribe`` pre-answers the name prompt even
    under the required-name policy (no blank re-ask when the CLI supplied a name)."""
    agents_dir = tmp_path / "agents"
    prompter = FakePrompter(texts=["scribe", "d"], confirms=[False])
    rc = agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name="scribe", home=None)
    assert rc == 0
    assert (agents_dir / "scribe.md").is_file()


@pytest.mark.parametrize("reserved", ["tools", "broker", "bridge", "process-compose", "mcp-github"])
def test_run_reserved_name_reprompts_with_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], reserved: str
) -> None:
    """A reserved workspace-slot name (`tools`/`broker`/`bridge`/`mcp-*`) is
    rejected AT THE NAME PROMPT with the reason, and the flow re-asks — the
    operator never walks the whole wizard just to fail at the write."""
    agents_dir = tmp_path / "agents"
    prompter = FakePrompter(texts=[reserved, "scribe", "d"], confirms=[False])
    rc = agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name=None, home=None)
    assert rc == 0
    assert not (agents_dir / f"{reserved}.md").exists()
    assert (agents_dir / "scribe.md").is_file()
    assert "reserved" in capsys.readouterr().out


def test_create_agent_init_path_rejects_reserved_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``init``'s create path (require_name=False) also refuses a reserved typed
    name and re-prompts, so a first-run can't author an unstartable agent."""
    agents_dir = tmp_path / "agents"
    prompter = FakePrompter(texts=["tools", "scribe", "d"], confirms=[False])
    created = agent_create.create_agent(
        prompter,
        agents_dir=agents_dir,
        env_path=tmp_path / ".env",
        name_default=None,
        prune_seed=True,
        offer_prompt=False,
        require_name=False,
    )
    assert created.name == "scribe"
    assert not (agents_dir / "tools.md").exists()
    assert "reserved" in capsys.readouterr().out


def test_create_agent_init_path_keeps_starter_default(tmp_path: Path) -> None:
    """``init``'s path (``require_name=False``, ``name_default=None``) is UNCHANGED:
    a blank name enter-through still defaults to the seeded 'assistant'."""
    agents_dir = tmp_path / "agents"
    # Blank name -> init path keeps the STARTER default ('assistant'); no gate confirm.
    prompter = FakePrompter(texts=["", "d"], confirms=[False])
    created = agent_create.create_agent(
        prompter,
        agents_dir=agents_dir,
        env_path=tmp_path / ".env",
        name_default=None,
        prune_seed=True,
        offer_prompt=False,
        require_name=False,
    )
    assert created.name == STARTER_AGENT_NAME
    assert (agents_dir / f"{STARTER_AGENT_NAME}.md").is_file()


# ---------------------------------------------------------------------------
# Change A — standalone create ends LIVE: offer to start the agent, opening the
# workspace only if it isn't running (the roster spawns off Process Compose, so
# a live workspace needs no reload), then confirming presence on the mesh. All
# world-touching calls are injected seams.
# ---------------------------------------------------------------------------


class _FinishRecorder:
    """Records the live-finish orchestration seams so tests assert what ran.

    Every seam is an async stub returning a scripted exit code / presence result;
    ``pc_binary`` reports the supervisor as available so the native path (not the
    dev degrade) is exercised.
    """

    def __init__(
        self,
        *,
        running: bool = False,
        start_rc: int = 0,
        agent_rc: int = 0,
        tools_rc: int = 0,
        present: bool = True,
    ) -> None:
        self._running = running
        self._start_rc = start_rc
        self._agent_rc = agent_rc
        self._tools_rc = tools_rc
        self._present = present
        self.calls: list[str] = []
        self.start_kwargs: list[dict] = []
        self.tools_kwargs: list[dict] = []
        self.agent_kwargs: list[dict] = []
        self.presence_kwargs: list[dict] = []

    async def workspace_running(self, home: Path) -> bool:
        self.calls.append("workspace_running")
        return self._running

    async def start(self, home, **kwargs) -> int:
        self.calls.append("start")
        self.start_kwargs.append({"home": home, **kwargs})
        return self._start_rc

    async def tools_start(self, home, **kwargs) -> int:
        self.calls.append("tools")
        self.tools_kwargs.append({"home": home, **kwargs})
        return self._tools_rc

    async def agent_start(self, home, **kwargs) -> int:
        self.calls.append("agent_start")
        self.agent_kwargs.append({"home": home, **kwargs})
        return self._agent_rc

    async def presence(self, server_urls, **kwargs) -> bool:
        self.calls.append("presence")
        self.presence_kwargs.append({"server_urls": server_urls, **kwargs})
        return self._present

    def pc_binary(self) -> str:
        return "process-compose"


def _run_live(
    prompter: FakePrompter,
    tmp_path: Path,
    finish: _FinishRecorder,
    *,
    name: str | None = None,
) -> int:
    """Drive ``agent_create.run`` with every orchestration seam stubbed."""
    return agent_create.run(
        prompter,
        agents_dir=tmp_path / "agents",
        env_path=tmp_path / ".env",
        name=name,
        home=tmp_path,
        server_urls="localhost:9092",
        start_fn=finish.start,
        tools_start_fn=finish.tools_start,
        agent_start_fn=finish.agent_start,
        presence_fn=finish.presence,
        workspace_running_fn=finish.workspace_running,
        pc_binary_fn=finish.pc_binary,
    )


def test_run_start_now_yes_workspace_not_running_opens_and_starts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Start-now yes with the workspace DOWN: open the workspace (no reload), then
    bring the agent online; presence seen prints the exact online line."""
    finish = _FinishRecorder(running=False, present=True)
    # confirms: [edit-prompt=No, Start now?=Yes]
    prompter = FakePrompter(texts=["scribe", "d"], confirms=[False, True])
    rc = _run_live(prompter, tmp_path, finish, name="scribe")
    assert rc == 0
    # No stop (workspace wasn't running) — open the workspace (substrate + tools host),
    # then bring the agent online. The tools host comes up with the workspace so the
    # new agent's first tool call has a live host (same as `disco start`).
    assert finish.calls == ["workspace_running", "start", "tools", "agent_start", "presence"]
    assert finish.agent_kwargs[0]["name"] == "scribe"
    assert finish.tools_kwargs[0]["name"] == "tools"
    out = capsys.readouterr().out
    assert "scribe is online — say !scribe hello in Discord" in out


def test_run_start_now_suppresses_the_workspace_next_step_signpost(tmp_path: Path) -> None:
    """Start-now opens the workspace with ``banner=False``.

    The same contradiction the init wizard had: ``lifecycle.start``'s closing signpost
    says "No agents running yet -> disco agent start <name>", and start-now's very next
    act is to start the agent. The signpost is for the operator who is being handed the
    prompt back with a decision to make — not for a flow that has already made it.
    """
    finish = _FinishRecorder(running=False, present=True)
    prompter = FakePrompter(texts=["scribe", "d"], confirms=[False, True])
    assert _run_live(prompter, tmp_path, finish, name="scribe") == 0
    assert finish.start_kwargs[0]["banner"] is False


def test_run_start_now_running_does_not_restart_tools_host(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With the workspace already UP, the new agent spawns straight in — the workspace
    (and its tools host) is not re-opened, so the tools host is NOT bounced."""
    finish = _FinishRecorder(running=True, present=True)
    prompter = FakePrompter(texts=["scribe", "d"], confirms=[False, True])
    rc = _run_live(prompter, tmp_path, finish, name="scribe")
    assert rc == 0
    assert "tools" not in finish.calls  # no re-open of the running workspace
    assert finish.tools_kwargs == []


def test_run_start_now_not_running_tools_host_failure_is_advisory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """On the cold-open path a tools-host failure is advisory: the agent still starts,
    run still returns 0, and the operator is pointed at `disco tools start`."""
    finish = _FinishRecorder(running=False, present=True, tools_rc=1)
    prompter = FakePrompter(texts=["scribe", "d"], confirms=[False, True])
    rc = _run_live(prompter, tmp_path, finish, name="scribe")
    assert rc == 0
    assert "agent_start" in finish.calls  # the agent still clocks in despite the tools failure
    assert "disco tools start" in capsys.readouterr().out


def test_run_start_now_yes_presence_timeout_degrades(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Agent starts but presence is not seen in time: the honest 'try it yourself /
    disco doctor' downgrade prints instead of a green light that lies."""
    finish = _FinishRecorder(running=False, present=False)
    prompter = FakePrompter(texts=["scribe", "d"], confirms=[False, True])
    rc = _run_live(prompter, tmp_path, finish, name="scribe")
    assert rc == 0
    out = capsys.readouterr().out
    assert "scribe is online — say" not in out
    assert "disco doctor" in out


def test_run_start_now_presence_fn_raising_degrades_not_crashes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The presence watcher opens its own broker connection — a raise mid-watch
    (broker drop) must DEGRADE to the honest 'try it yourself / disco doctor'
    fallback, never crash the CLI after the agent already started."""
    finish = _FinishRecorder(running=True)

    async def exploding_presence(server_urls, **kwargs) -> bool:
        raise RuntimeError("broker dropped mid-watch")

    finish.presence = exploding_presence  # the seam _run_live hands to run()
    prompter = FakePrompter(texts=["scribe", "d"], confirms=[False, True])
    rc = _run_live(prompter, tmp_path, finish, name="scribe")
    assert rc == 0
    out = capsys.readouterr().out
    assert "scribe is online — say" not in out  # no green light that lies
    assert "disco doctor" in out


def test_run_start_now_yes_workspace_running_spawns_directly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Start-now yes with the workspace UP: the roster spawns off Process Compose,
    so the brand-new agent starts DIRECTLY — no reload consent, no stop/start,
    no in-flight work lost."""
    finish = _FinishRecorder(running=True, present=True)
    # confirms: [edit-prompt=No, Start now?=Yes] — a reload consent prompt here
    # would dequeue-empty and raise, pinning that the obsolete prompt is gone.
    prompter = FakePrompter(texts=["scribe", "d"], confirms=[False, True])
    rc = _run_live(prompter, tmp_path, finish, name="scribe")
    assert rc == 0
    assert finish.calls == ["workspace_running", "agent_start", "presence"]
    assert finish.agent_kwargs[0]["name"] == "scribe"


def test_run_start_now_no_running_prints_agent_start_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Declining 'Start now?' with the workspace UP prints ONLY the agent-start
    step (no stop, no start — a live workspace needs no reload), and
    orchestrates nothing."""
    finish = _FinishRecorder(running=True)
    # confirms: [edit-prompt=No, Start now?=No]
    prompter = FakePrompter(texts=["scribe", "d"], confirms=[False, False])
    rc = _run_live(prompter, tmp_path, finish, name="scribe")
    assert rc == 0
    assert finish.calls == ["workspace_running"]
    out = capsys.readouterr().out
    assert "disco stop" not in out
    assert "disco start" not in out
    assert "disco agent start scribe" in out


def test_run_start_now_no_not_running_prints_plain_manual(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Declining 'Start now?' with the workspace DOWN prints the plain manual (no
    stop line), and orchestrates nothing."""
    finish = _FinishRecorder(running=False)
    prompter = FakePrompter(texts=["scribe", "d"], confirms=[False, False])
    rc = _run_live(prompter, tmp_path, finish, name="scribe")
    assert rc == 0
    assert finish.calls == ["workspace_running"]
    out = capsys.readouterr().out
    assert "disco stop" not in out
    assert "disco start" in out
    assert "disco agent start scribe" in out


def test_run_start_now_workspace_open_failure_propagates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """If opening the workspace fails, the non-zero code propagates and the agent is
    never started (no false 'online')."""
    finish = _FinishRecorder(running=False, start_rc=1)
    prompter = FakePrompter(texts=["scribe", "d"], confirms=[False, True])
    rc = _run_live(prompter, tmp_path, finish, name="scribe")
    assert rc == 1
    assert "agent_start" not in finish.calls
    assert "presence" not in finish.calls


def test_run_start_now_agent_start_failure_propagates_without_online_claim(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """If the agent spawn itself fails (crash-on-boot rc 1), the non-zero code
    propagates, presence is never watched, and NO 'online' line prints."""
    finish = _FinishRecorder(running=True, agent_rc=1)
    prompter = FakePrompter(texts=["scribe", "d"], confirms=[False, True])
    rc = _run_live(prompter, tmp_path, finish, name="scribe")
    assert rc == 1
    assert "presence" not in finish.calls
    out = capsys.readouterr().out
    assert "online" not in out


def test_run_start_now_presence_failure_names_the_cause(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A raising presence watch degrades honestly AND says why — the operator
    should not have to guess whether the watch timed out or blew up."""
    finish = _FinishRecorder(running=True)

    async def exploding_presence(server_urls, **kwargs) -> bool:
        raise RuntimeError("broker dropped mid-watch")

    finish.presence = exploding_presence
    prompter = FakePrompter(texts=["scribe", "d"], confirms=[False, True])
    rc = _run_live(prompter, tmp_path, finish, name="scribe")
    assert rc == 0
    out = capsys.readouterr().out
    assert "broker dropped mid-watch" in out
    assert "disco doctor" in out


def test_run_dev_run_degrades_without_prompting_start(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dev run (home=None) never prompts 'Start now?' (it can't orchestrate the
    install-scoped supervisor); it prints the honest manual next-steps instead."""
    # Only the edit-prompt confirm is scripted; a 'Start now?' prompt here would
    # dequeue-empty and raise, proving the degrade path never prompts to start.
    prompter = FakePrompter(texts=["scribe", "d"], confirms=[False])

    def _must_not_probe() -> str:
        # A dev run must NOT probe the binary — it returns before that (probing would
        # break import-light). This pins that structurally, mirroring init's dev test.
        raise AssertionError("dev run must not probe the supervisor binary")

    rc = agent_create.run(
        prompter,
        agents_dir=tmp_path / "agents",
        env_path=tmp_path / ".env",
        name="scribe",
        home=None,
        pc_binary_fn=_must_not_probe,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Bring scribe online:" in out
    assert "disco start" in out
    # Dev run stays silent — no install-defect banner (symmetry with init's dev test).
    assert "can't be started automatically" not in out


def test_run_missing_process_compose_binary_names_the_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A native install missing the process-compose binary degrades to the manual
    next-steps and NAMES the actionable reason instead of swallowing it (mirrors init;
    §12.6). Nothing is orchestrated and no 'Start now?' is prompted."""

    class _MissingBinary(_FinishRecorder):
        def pc_binary(self) -> str:
            raise RuntimeError("process-compose binary not found; re-run the installer")

    finish = _MissingBinary()
    prompter = FakePrompter(texts=["scribe", "d"], confirms=[False])
    rc = _run_live(prompter, tmp_path, finish, name="scribe")
    out = capsys.readouterr().out
    assert rc == 0
    assert finish.calls == []  # nothing orchestrated — degraded before any orchestration seam ran
    assert "can't be started automatically" in out  # the defect banner (anchors the dev test's "not in")
    assert "process-compose binary not found; re-run the installer" in out


def test_run_start_now_confirm_runs_outside_asyncio_loop(tmp_path: Path) -> None:
    """The 'Start now?' confirm must run on the SYNC side of the asyncio boundary.

    InquirerPy's ``.execute()`` itself calls ``asyncio.run()`` (via prompt_toolkit's
    ``Application.prompt()``), so calling ``confirm`` from inside ``asyncio.run``
    raises ``RuntimeError: asyncio.run() cannot be called from a running event
    loop`` — the exact crash users hit at the end of ``disco agent create`` on a
    native install. This pins the structural fix: the confirm (and the workspace
    probe that informs its manual next-steps) are hoisted out of the async body
    into the sync caller, so the prompter is never re-entered from inside a loop.
    """

    class _LoopGuardPrompter(FakePrompter):
        """FakePrompter whose ``confirm`` fails if a loop is running.

        ``asyncio.get_running_loop()`` raises ``RuntimeError`` when no loop is
        running on this thread — exactly the condition ``confirm`` requires.
        """

        def confirm(self, message: str, *, default: bool = False) -> bool:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return super().confirm(message, default=default)  # good: no loop running
            raise AssertionError(
                f"confirm() must run outside the asyncio loop, but found {loop!r} "
                f"running (message={message!r})"
            )

    finish = _FinishRecorder(running=True, present=True)
    # confirms: [edit-prompt=No, Start now?=Yes] — the second confirm is the one
    # the bug used to call from inside asyncio.run(_start_now(...)).
    prompter = _LoopGuardPrompter(texts=["scribe", "d"], confirms=[False, True])
    rc = _run_live(prompter, tmp_path, finish, name="scribe")
    assert rc == 0
    assert finish.calls == ["workspace_running", "agent_start", "presence"]

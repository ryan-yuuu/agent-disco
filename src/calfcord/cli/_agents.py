"""Shared agent-directory inspection and ``.md`` write helpers for the CLI.

Both ``disco init`` (which *reports* the agents an install would load) and
``disco agent tools`` (which *picks* one to edit) need the same answer to
"which ``.md`` files are live agents?". Factoring :func:`detect_agents` here
keeps that one definition from drifting between the two callers — a mismatch
would let ``init`` report an agent the editor can't open, or vice versa.

This module also owns the agent-file *write* and *identity* helpers that both
``disco init`` (first-run setup) and ``disco agent create`` build on:
slugifying a typed name into a valid stem, deriving a default display name and
body, the create/update :func:`write_agent` path, and the tools-checkbox
builder :func:`pick_tools`. They live here rather than in ``init`` so the two
commands share one implementation and can't drift.

The skip rules in :func:`detect_agents` mirror the loader's
(:func:`calfcord.agents.loader.load_agents_dir`): dot-prefixed files and
``*.template.md`` reference templates are not live agents, so the names returned
here match exactly what ``calfkit-agent`` would run.

:func:`pick_tools` defers its ``TOOL_REGISTRY`` import into the function body so
the rest of the module (e.g. :func:`detect_agents`) stays light — importing the
registry eagerly composes the tool surface (importing the vendored
``calfkit-tools`` nodes).
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import frontmatter

from calfcord.agents import md_writer
from calfcord.agents.definition import AgentDefinition, parse_agent_md
from calfcord.agents.identifier import AGENT_ID_PATTERN

# ``Choice`` is needed at runtime (pick_agent builds rows); ``Prompter`` is only a
# type. Importing the seam is cycle-free: ``_prompts`` holds only the Protocol and
# a factory whose TUI import is lazy, so it never reaches back here.
from calfcord.cli._prompts import Choice
from calfcord.cli.tui import render

if TYPE_CHECKING:
    from calfcord.cli._prompts import Prompter

logger = logging.getLogger(__name__)

# The starter agent's name and the *exact* description the installer seeds it
# with. The prune-pristine check in :func:`write_agent` keys off this string:
# an ``assistant.md`` still carrying it is an untouched seed (safe to remove
# when the operator names a different agent); any other description means the
# operator customized it and it must be preserved.
STARTER_AGENT_NAME = "assistant"
DEFAULT_DESCRIPTION = "General-purpose AI teammate — answers questions and helps with tasks."

# Tools that grant code-execution or filesystem-write reach into the
# ``calfkit-tools`` host. Selecting any of them drives the one-line security
# caution, because anyone who can !mention the agent can then drive them.
# ``terminal`` and ``execute_code`` run arbitrary code on the host (see
# docs/adr/0005); ``write_file``/``patch`` mutate the shared workspace.
_DANGEROUS_TOOLS = frozenset(
    {"terminal", "process", "execute_code", "write_file", "patch"}
)

# Permitted characters for an agent-name *stem*. The on-disk identifier must
# satisfy ``AgentDefinition.agent_id`` (``[a-z0-9_-]{1,32}``); we slugify toward
# that here so a friendly typed name ("My Helper") becomes a valid stem
# ("my_helper") rather than failing validation at write time.
_STEM_INVALID = re.compile(r"[^a-z0-9_-]+")

# The create row's value, and why this exact spelling is safe. It shares a
# namespace with agent names (both are ``Choice.value``s in one select, and
# ``ListState`` requires those to be unique), so it must be a string no agent
# can ever be called — a collision does not merely mis-render, it RAISES, so the
# picker never opens and no agent can be started at all.
#
# The leading separator is what buys that, and it is worth being exact about why,
# because the obvious argument is wrong: ``AGENT_ID_PATTERN`` does NOT keep this
# namespace clean. It governs the WRITE path (``slug_stem`` rewrites a typed name
# toward it), while :func:`detect_agents` — which supplies every other row —
# globs raw ``.md`` stems and validates nothing. An earlier version of this
# constant was ``+create`` and reasoned that ``+`` was outside the pattern; a
# hand-placed ``+create.md`` duly collided and crashed the picker. A stem is a
# single path component, so it can never contain ``/`` — that holds no matter
# what ``detect_agents`` filters, which is the property this needs.
#
# It is also not a valid agent id, so were it ever to leak through to
# ``roster.agent_start`` it would fail that command's name-shape check loudly
# rather than start something unexpected. That is a backstop, not the guarantee.
CREATE_SENTINEL = "/create"

# ``+`` reads as "new"; the ellipsis is the long-standing convention for a row
# that OPENS something rather than acting immediately. Together they do the work
# a colour would do elsewhere — deliberately, since :mod:`calfcord.cli.tui.theme`
# is monochrome and every unselected row is already dim, leaving no styling
# headroom to mark this row as different.
_CREATE_LABEL = "+ Create a new agent…"


# The synthetic checkbox row that represents MCP discover mode (``mcp: true``).
# Deliberately colon-separated, not ``mcp/``-prefixed, so it never collides with a
# named ``mcp/<server>`` grant row when the split partitions the selection.
MCP_DISCOVER_ROW = "mcp:discover"


@dataclass(frozen=True)
class ToolGrantSelection:
    """Structured result from the tools checkbox.

    ``tools=None`` means omit ``tools:`` and discover all live builtins.
    ``mcp`` is the field's tri-state: ``True`` (discover every live MCP server),
    ``False`` / ``[]`` (opt out), or a list of canonical ``server`` /
    ``server/tool`` grants.
    """

    tools: list[str] | None
    mcp: bool | list[str]


def pick_agent(
    prompter: Prompter,
    *,
    agents_dir: Path,
    message: str,
    create_fn: Callable[[], str | None],
) -> str | None:
    """Prompt for one of the DEFINED agents — or for a brand-new one.

    The "which agent?" pick-list. Returns the chosen agent's name; ``None`` means
    "nothing to start", and whatever produced it has already said why, so callers
    map it onto exit 1 without adding a message of their own.

    The list always ends with a ``+ Create a new agent…`` row, because "none of
    these" was the one answer it could not take: an operator whose roster held
    nothing they wanted had to quit and run another command. Choosing it returns
    whatever ``create_fn`` produces — a new agent's name, or ``None`` if the
    create failed (in which case ``create_fn`` owns reporting it).

    ``create_fn`` is injected rather than imported because
    :mod:`calfcord.cli.agent_create` imports *this* module, so reaching the other
    way would cycle; it also keeps this module's one job — asking the question —
    separate from the flow that answers it. It is REQUIRED, not defaulted: the
    only caller always creates, so a ``None`` default would be a branch no run
    ever reaches, and it would let the next caller acquire a create-less picker
    without ever deciding to.

    Placement is deliberate. :class:`~calfcord.cli.tui.state.SelectState` opens on
    row 0, so a create row at the top would make launching a wizard the
    enter-through default of a command whose usual intent is to start an agent
    that already exists. Last costs nothing in reach: ``ListState`` navigation
    wraps, so one ``↑`` lands on it however long the roster is.

    An empty roster opens the picker rather than refusing it, and the reasoning
    here used to run the other way: a choice-less list is unanswerable — no key
    means "none of these" — so it stranded the operator with only Ctrl-C, and
    naming the command that makes an agent was the honest reply. Offering to
    create inverts that: the list is no longer choice-less, it holds exactly one
    honest answer, and offering it beats printing a command to go and type.

    This lists what is **defined** on disk, not what is **running**. That suits
    ``start``; it would be wrong for ``stop``/``restart``, whose real question is
    "which of the running agents?" — answerable only with a broker probe, and a
    defined-agent list there would invite picking one that is already stopped.

    ``agent edit`` / ``agent tools`` keep their own near-identical pick-lists
    rather than calling this, and that is deliberate: they return a ``Path``, they
    have a name-given branch this has no equivalent of, and their empty-roster
    dead end is honest — there is nothing to edit, and offering to create inside
    "which agent do you want to edit?" answers a different question than the one
    asked. The part that could actually drift between them, ``detect_agents``, is
    already shared.
    """
    agents = detect_agents(agents_dir)
    if not agents:
        # The dead-end this replaced still carried one fact worth keeping: WHICH
        # directory was searched is how an operator recognizes a wrong
        # ``$CALFCORD_HOME``. As dim detail above the picker it survives without
        # the dead end it used to arrive with.
        render.note(f"no agents in {agents_dir}")

    choices = [Choice(a, a) for a in agents]
    choices.append(Choice(CREATE_SENTINEL, _CREATE_LABEL))

    chosen = prompter.select(message, choices)
    return create_fn() if chosen == CREATE_SENTINEL else chosen


def detect_agents(agents_dir: Path) -> list[str]:
    """Return the agent names (``.md`` stems) ``agents_dir`` would load, sorted.

    Returns an empty list when ``agents_dir`` is not an existing directory, so
    callers can treat "no dir" and "empty dir" identically — both mean "no
    agents to act on". Dotfiles and ``*.template.md`` templates are skipped to
    match the loader; the result is sorted for deterministic prompts/output.
    """
    if not agents_dir.is_dir():
        return []
    return sorted(
        p.stem
        for p in agents_dir.glob("*.md")
        if not p.name.startswith(".") and not p.name.endswith(".template.md")
    )


def slug_stem(raw: str) -> str:
    """Coerce a typed agent name into a safe ``.md`` filename stem.

    The frontmatter ``name`` (and thus the filename) must match
    ``[a-z0-9_-]{1,32}``; an operator typing "My Helper" should not hit a
    validation error, so we lowercase, turn runs of disallowed characters into
    single underscores, and trim the result. A name that slugifies to nothing
    (e.g. all punctuation) falls back to the starter name rather than producing
    an empty, invalid stem — keeping the wizard moving instead of aborting.
    """
    slug = _STEM_INVALID.sub("_", raw.strip().lower()).strip("_-")
    slug = slug[:32].strip("_-")
    result = slug or STARTER_AGENT_NAME
    # Postcondition: the stem is a valid agent id, so callers can write it as a
    # filename without a second validation. The fallback guarantees non-empty,
    # and slugification guarantees the charset/length, so this can only fire on
    # a bug in those rules — assert rather than silently emit an invalid stem.
    assert AGENT_ID_PATTERN.fullmatch(result), f"slugified stem {result!r} is not a valid agent id"
    return result


def existing_agent(agents_dir: Path, name: str) -> AgentDefinition | None:
    """Return the parsed agent at ``agents_dir/<name>.md`` if it parses, else ``None``.

    Used purely to pre-fill the description/model prompts with the operator's
    current values on a re-run that targets an existing agent. A missing or
    malformed file is not an error here — we simply offer the defaults — so the
    parse is guarded; the actual write path validates strictly.
    """
    target = agents_dir / f"{name}.md"
    if not target.is_file():
        return None
    try:
        return parse_agent_md(target)
    except (ValueError, OSError):
        return None


def agent_body(name: str) -> str:
    """Render the generic system-prompt body for a brand-new agent.

    A minimal, generic prompt so the agent answers sensibly from the first boot
    without further editing. Addresses the agent by a human-friendly rendering of
    its slug ``name`` (underscores/dashes title-cased, "my_helper" → "My Helper")
    so the greeting reads naturally. Kept separate from the frontmatter so the
    create path can serialize identity fields through ``frontmatter.dumps`` (which
    YAML-quotes free-text values safely) rather than string interpolation.
    """
    human = name.replace("_", " ").replace("-", " ").title()
    return (
        f"You are {human}, a helpful AI teammate in this Discord workspace. Answer\n"
        "questions and help with tasks clearly and concisely. If you don't know something,\n"
        "say so rather than guessing.\n"
        "\n"
        "You talk to people through Discord, so you can use Discord-flavored markdown in\n"
        "your replies. Tables do not render.\n"
    )


def atomic_write(path: Path, payload: str) -> None:
    """Write ``payload`` to ``path`` via a same-dir tmp file + atomic rename.

    A partial agent file would make the next ``calfkit-agent`` boot fail to
    load the directory, so the create path must never leave a half-written file
    behind on error. ``path.parent`` is created first because a fresh install's
    ``agents/`` directory may not exist yet (unlike :mod:`md_writer`'s in-place
    rewrite, which can assume the file — and so the dir — already exists).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def is_pristine_seed(agents_dir: Path) -> bool:
    """True when ``agents_dir/assistant.md`` is the untouched seeded starter.

    "Untouched" is detected by the seed's two stable identity markers: its
    ``agent_id`` is still ``assistant`` and its description is still the exact
    seed default. If the operator customized the description, both halves no
    longer hold and the file must be preserved. A missing or malformed
    ``assistant.md`` is treated as "not a pristine seed" (nothing safe to
    prune), so the parse is guarded — a broken file is never deleted on a guess.
    """
    seed = agents_dir / f"{STARTER_AGENT_NAME}.md"
    if not seed.is_file():
        return False
    try:
        parsed = parse_agent_md(seed)
    except (ValueError, OSError):
        return False
    return parsed.agent_id == STARTER_AGENT_NAME and parsed.description == DEFAULT_DESCRIPTION


# Filesystem tools a memory-enabled agent needs. Kept here (not imported from
# the factory) so the create path can satisfy the same constraint without
# pulling agent-runtime code into the CLI write helpers.
_MEMORY_REQUIRED_TOOLS = ("read_file", "write_file")


def _ensure_memory_tools(tools: list[str] | None, *, memory: bool) -> list[str] | None:
    """Return ``tools`` with memory's required filesystem tools present when needed.

    Memory-enabled agents manage their notepad with ``read_file`` / ``write_file``.
    An omitted ``tools:`` (``None``) already discovers every builtin and is fine.
    An explicit list that dropped either tool would otherwise pass create and
    fail later at agent build — so top them up here and leave ordering stable
    (required tools appended only when missing).
    """
    if not memory or tools is None:
        return tools
    ensured = list(tools)
    for required in _MEMORY_REQUIRED_TOOLS:
        if required not in ensured:
            ensured.append(required)
    return ensured


def write_agent(
    agents_dir: Path,
    *,
    name: str,
    description: str,
    provider: str,
    model: str,
    tools: list[str] | None,
    mcp: bool | list[str] | tuple[str, ...] = True,
    memory: bool = True,
    prune_seed: bool = False,
) -> Path:
    """Create or update ``agents_dir/<name>.md`` for the wizard's agent.

    Two paths, both validate-before-write so a bad value never lands on disk:

    * **Target exists** — update the agent in place, preserving its body:
      rewrite ``description``/``provider``/``model`` via
      :func:`md_writer._update_fields`, then the split tool grants via
      :func:`md_writer.update_tool_grants`. Both are validated-atomic, so a bad
      value leaves the file untouched. The existing ``memory`` setting is left
      alone — re-running create against an on-disk agent must not silently flip
      it — but if that existing value is on, the same ``read_file`` /
      ``write_file`` top-up applied on create runs against the new tools list
      so an update cannot leave a memory-enabled agent factory-rejected.
    * **Target missing** — build the frontmatter as a mapping and serialize it
      with :func:`frontmatter.dumps` (NOT string interpolation), which
      YAML-quotes free-text values so a description like ``"Calendar: book
      meetings"`` or one carrying quotes/``#``/leading punctuation can't corrupt
      the file. New agents default to ``memory: true`` (written explicitly) so
      ``disco agent create`` / ``disco init`` teammates start with the
      persistent notepad on; the schema default for an *omitted* field stays
      ``false`` so hand-authored and pre-existing agents are unchanged. When
      memory is on and ``tools`` is an explicit list missing ``read_file`` /
      ``write_file``, those tools are added before validation so the wizard
      cannot produce an agent the factory would reject. The synthetic
      :class:`AgentDefinition` is built *first* (mirroring
      :func:`md_writer._update_fields`), so an invalid value raises before any
      disk write. After the atomic write, when ``prune_seed`` is set and the
      operator named a *different* agent (``name != "assistant"``) on an install
      still carrying the *pristine* seeded ``assistant.md``, that seed is deleted
      so they end with one clean agent. ``init`` opts in for its first-run setup;
      ``agent create`` leaves it off so a second agent never removes the starter.
      A *customized* ``assistant.md`` (or naming the agent ``assistant`` itself)
      is never deleted.

    Raises:
        ValueError: a field value fails :class:`AgentDefinition` validation
            (create path) or the existing ``.md``/new value is invalid (update
            path). No partial file is written.
        OSError: a filesystem error during the atomic write. No partial file is
            written.
    """
    target = agents_dir / f"{name}.md"

    if target.exists():
        # Honor the on-disk memory bit (do not apply the create-time default),
        # but still satisfy the factory's fs-tool requirement when it is on.
        existing_memory = parse_agent_md(target).memory
        tools = _ensure_memory_tools(tools, memory=existing_memory)
        md_writer._update_fields(target, {"description": description, "provider": provider, "model": model})
        md_writer.update_tool_grants(target, tools=tools, mcp=mcp)
        return target

    tools = _ensure_memory_tools(tools, memory=memory)
    body = agent_body(name)
    metadata = {
        "name": name,
        "description": description,
        "provider": provider,
        "model": model,
        "memory": memory,
    }
    if tools is not None:
        metadata["tools"] = list(tools)
    # Shared with the update path (md_writer.update_tool_grants) so the two
    # cannot diverge on how a tri-state serializes.
    md_writer.apply_mcp_metadata(metadata, mcp)
    # Validate the full definition in memory FIRST (mirrors
    # md_writer._update_fields): a bad free-text value raises here, before any
    # bytes touch disk, so the create path can never leave an unloadable file.
    AgentDefinition(**{**metadata, "system_prompt": body, "source_path": target})

    payload = frontmatter.dumps(frontmatter.Post(body, **metadata))
    if not payload.endswith("\n"):
        payload += "\n"
    atomic_write(target, payload)

    # Prune the pristine starter only when the caller opted in (``init``'s
    # first-run "one clean agent" goal) and a *different* agent was created;
    # naming the agent ``assistant`` would have hit the update path above.
    # ``agent create`` leaves ``prune_seed`` False so adding a second agent
    # never deletes the operator's starter.
    if prune_seed and name != STARTER_AGENT_NAME and is_pristine_seed(agents_dir):
        (agents_dir / f"{STARTER_AGENT_NAME}.md").unlink(missing_ok=True)
        logger.info("pruned pristine seed assistant.md after creating %s", target)

    return target


def pick_tools(
    prompter: Prompter,
    name: str,
    *,
    mcp_servers_fn: Callable[[], list[str]] | None = None,
    live_tools_fn: Callable[[], dict[str, list[str]]] | None = None,
) -> ToolGrantSelection:
    """Prompt for the agent's tools and return split builtin/MCP grants.

    Every builtin (sorted :data:`calfcord.tools.TOOL_REGISTRY`) is offered
    pre-checked so the default is the same "all builtins" set a frontmatter that
    omits ``tools:`` would expand to, and the MCP discover row is likewise
    pre-checked so a wizard-created agent matches a hand-authored one (which
    defaults to ``mcp: true``). The *named* MCP rows (``mcp/<server>`` from
    mcp.json plus live-discovered per-tool rows) start UNCHECKED — deselect
    discover to opt out or to pick named servers instead. Row building is shared
    with the ``agent tools`` editor (:func:`calfcord.cli.agent_tools._build_choices`)
    so the two surfaces can't drift. If a write/shell tool ends up selected we
    print the security caution, because anyone who can !mention the agent can
    then drive it.
    """
    from calfcord.cli.agent_tools import (
        _build_choices,
        _default_live_tools,
        _default_mcp_servers,
    )
    from calfcord.tools import TOOL_REGISTRY

    choices = _build_choices(
        # Builtins AND MCP-discover pre-checked, so a wizard-created agent's default
        # matches a hand-authored one (omitted ``tools:`` + ``mcp: true``); named MCP
        # rows start unchecked. Deselect discover to opt out or to pick named servers.
        set(TOOL_REGISTRY) | {MCP_DISCOVER_ROW},
        mcp_servers=(mcp_servers_fn or _default_mcp_servers)(),
        live_tools=(live_tools_fn or _default_live_tools)(),
    )

    selected = prompter.checkbox(
        f"Tools for {name}",
        choices,
        # The title asks; the instruction explains. This guidance used to be a
        # parenthetical inside the title, which turned the question into a
        # paragraph — and left ``instruction`` unused and looking like dead
        # speculation. It deliberately does NOT restate the key mechanics: the
        # hint in the panel's border already says space/enter for every list.
        instruction="All selected — deselect any you don't want.",
    )

    if _DANGEROUS_TOOLS.intersection(selected):
        print(
            "note: these tools include code execution + file write access in the "
            "calfkit-tools launch dir, drivable by anyone who can !mention this agent "
            "— keep the bot off public Discord (docs/security.md §3.4)."
        )

    return _split_tool_selection(selected, set(TOOL_REGISTRY))


def _split_tool_selection(selected: list[str], builtin_names: set[str]) -> ToolGrantSelection:
    """Convert checkbox UI values into canonical frontmatter fields.

    The MCP surface is tri-state: the synthetic :data:`MCP_DISCOVER_ROW` maps to
    ``mcp=True`` (discover every live server) and, being exclusive, subsumes any
    named ``mcp/<server>`` rows also ticked; otherwise the named rows form the
    grant list, and an empty selection is ``[]`` (opt out).
    """
    builtin_tokens = [
        token for token in selected if not token.startswith("mcp/") and token != MCP_DISCOVER_ROW
    ]
    selected_known_builtins = {token for token in builtin_tokens if token in builtin_names}
    has_unknown_builtin = any(token not in builtin_names for token in builtin_tokens)

    tools = None if not has_unknown_builtin and selected_known_builtins == builtin_names else builtin_tokens

    mcp: bool | list[str]
    if MCP_DISCOVER_ROW in selected:
        mcp = True
    else:
        mcp = [token.removeprefix("mcp/") for token in selected if token.startswith("mcp/")]
    return ToolGrantSelection(tools=tools, mcp=mcp)

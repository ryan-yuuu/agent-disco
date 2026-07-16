"""``disco agent create [<name>]`` — the reusable agent-creation flow.

This is the one place the "name → describe → provider/model → tools → write"
sequence lives, so the two surfaces that need it — the standalone ``agent
create`` command *and* ``init``'s first-run setup — can never drift on
*how* an agent is brought into being. :func:`create_agent` is the extracted
flow; :func:`run` is the thin ``agent create`` wrapper around it (no seed prune,
offers the optional ``$EDITOR`` prompt step, then offers to start the agent).

Two design rules keep the two callers honest:

* **``create_agent`` never touches ``CALFKIT_AGENT_DEFAULT_PROVIDER``.** The
  agent it writes carries an *explicit* ``provider``/``model`` in its
  frontmatter, so the install-wide default-provider env var is irrelevant to it
  — that env default is purely ``init``'s concern (first-run wants a sensible
  default for *future* agents). Writing it here would let ``agent create`` of a
  one-off OpenAI agent silently flip the install default, surprising the next
  ``init`` re-run.

* **Provider/model/tools all flow through the validated seams.**
  :func:`~calfcord.cli._providers.configure_provider` owns provider-select,
  credential capture, and the live model pick (so an operator can never type a
  slug the provider rejects); :func:`~calfcord.cli._agents.pick_tools` owns the
  pre-checked tool checkbox; :func:`~calfcord.cli._agents.write_agent` owns the
  validate-before-write disk path. This module only sequences them.

``configure_provider`` is imported at module scope (not lazily) so tests can
monkeypatch ``agent_create.configure_provider`` to a fixed ``(provider, model)``
and drive the whole flow without a provider SDK, network, key, or OAuth — the
same pattern the ``init`` tests use.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from calfcord.agents.identifier import reserved_agent_id_error
from calfcord.cli._agents import (
    DEFAULT_DESCRIPTION,
    STARTER_AGENT_NAME,
    ToolGrantSelection,
    detect_agents,
    existing_agent,
    pick_tools,
    slug_stem,
    write_agent,
)
from calfcord.cli._envfile import read_env
from calfcord.cli._providers import configure_provider
from calfcord.cli._supervisor import default_pc_binary, open_workspace, supervisor_unavailable_reason
from calfcord.cli.tui import render

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from calfcord.agents.definition import AgentDefinition
    from calfcord.cli._prompts import Prompter

# The install-wide default-provider env var ``init`` reads to pre-select the
# provider menu. ``create_agent`` only *reads* it (as the menu default); it
# never writes it — see the module docstring's "never touches" rule.
_DEFAULT_PROVIDER_VAR = "CALFKIT_AGENT_DEFAULT_PROVIDER"

# Sentinel for the ``home`` / ``server_urls`` params of :func:`run` that means
# "resolve from the environment" — distinct from a passed ``home=None`` (a dev
# run with no ``$CALFCORD_HOME``) so ``main.py`` can call ``run`` without wiring
# either, while tests inject explicit values. ``Any``-typed so the sentinel can
# sit as the default of a ``Path | None`` / ``str`` parameter.
_ENV_DEFAULT: Any = object()

# How long the standalone-create live finish waits for the just-started agent to
# register on the mesh before downgrading to the honest "try it yourself" hint —
# the same bound (and intent) as ``init``'s live finish (§4.6 / §12.6).
_ONLINE_TIMEOUT_S = 60.0


class CreatedAgent(NamedTuple):
    """What :func:`create_agent` produced: the agent's resolved ``name`` and ``provider``.

    A named pair (not a bare ``tuple[str, str]``) so the two same-typed strings
    can't be unpacked in the wrong order — callers read ``.name`` / ``.provider``.
    ``init`` uses ``.provider`` to persist the install default; ``agent create``
    uses ``.name`` for its restart hint.
    """

    name: str
    provider: str


def _resolve_name(
    prompter: Prompter,
    *,
    agents_dir: Path,
    name_default: str | None,
    require_name: bool,
) -> tuple[str, AgentDefinition | None]:
    """Resolve the target agent name and the existing agent it edits (if any).

    Two policies keyed off ``require_name`` so the two create surfaces get the
    naming semantics each needs without a second create flow:

    * **``init``'s first-run path (``require_name=False``).** A blank answer is a
      *default*: ``name_default`` when given, else the lone existing agent's stem
      (a re-run editing it in place), else the starter name. First-run wants a
      sensible default so enter-through keeps the wizard moving.
    * **standalone ``agent create`` (``require_name=True``).** NO silent default:
      a blank answer re-prompts (:func:`_resolve_required_name`), so an
      enter-through can never quietly edit an existing agent, and naming an
      existing agent is gated by an explicit "update it?" confirm.

    Both policies refuse a RESERVED workspace-slot name (``tools``/``broker``/
    ``bridge``/``mcp-*``) at the prompt — with the reason — rather than letting
    the operator walk the whole wizard just to fail at the validate-before-write.
    """
    if require_name:
        return _resolve_required_name(prompter, agents_dir=agents_dir, seed=name_default or "")

    if name_default is None:
        existing = detect_agents(agents_dir)
        name_default = existing[0] if len(existing) == 1 else STARTER_AGENT_NAME
    while True:
        typed_name = prompter.text("Agent name:", default=name_default)
        name = slug_stem(typed_name) if typed_name.strip() else name_default
        reserved = reserved_agent_id_error(name)
        if reserved is not None:
            # Loop rather than error out: init's first-run must keep moving, and
            # the message says exactly why the name is off-limits.
            print(f"  {reserved}")
            continue
        return name, existing_agent(agents_dir, name)


def _resolve_required_name(
    prompter: Prompter, *, agents_dir: Path, seed: str
) -> tuple[str, AgentDefinition | None]:
    """The standalone-create name loop: required, no silent overwrite (Change B).

    Keeps asking until the operator supplies a non-blank name. A positional
    ``disco agent create <name>`` pre-fills ``seed`` (so it pre-answers the first
    prompt); an empty answer with no seed re-prompts rather than defaulting to an
    existing agent. When the entered name matches an existing agent, an explicit
    ``update it? [y/N]`` confirm gates the edit — declining clears the pre-fill and
    re-prompts for a different name, so an enter-through can never silently clobber
    an agent. Ctrl-C remains the clean abort out of the loop.
    """
    while True:
        typed = prompter.text("Agent name:", default=seed)
        if not typed.strip():
            # No silent default: a blank answer re-asks rather than falling back to
            # an existing agent (which an enter-through would then edit in place).
            seed = ""
            continue
        name = slug_stem(typed)
        reserved = reserved_agent_id_error(name)
        if reserved is not None:
            # A reserved workspace-slot name (tools/broker/bridge/mcp-*): say why
            # and re-ask, clearing the pre-fill so the bad name isn't offered back.
            print(f"  {reserved}")
            seed = ""
            continue
        prior = existing_agent(agents_dir, name)
        if prior is not None and not prompter.confirm(
            f"agent {name!r} already exists — update it?", default=False
        ):
            # Declined the overwrite: re-prompt for a different name, clearing the
            # pre-fill so the conflicting name isn't offered back as the default.
            seed = ""
            continue
        return name, prior


def create_agent(
    prompter: Prompter,
    *,
    agents_dir: Path,
    env_path: Path,
    name_default: str | None = None,
    prune_seed: bool = False,
    offer_prompt: bool = True,
    require_name: bool = False,
    select_tools: bool = True,
    live_tools_fn: Callable[[], dict[str, list[str]]] | None = None,
) -> CreatedAgent:
    """Run the shared create flow and return the created agent's ``name`` + ``provider``.

    The single create sequence both ``agent create`` (``prune_seed=False``,
    ``offer_prompt=True``, ``require_name=True``) and ``init``'s first-run setup
    (``prune_seed=True``, ``offer_prompt=False``, ``require_name=False``) build on,
    so the two can't drift on how an agent is created. Steps:

    1. **Name.** With ``require_name=False`` (``init``) a blank answer is a
       *default*: ``name_default`` when given, else the lone existing agent's stem
       (a re-run editing it in place), else the starter name. With
       ``require_name=True`` (standalone ``agent create``) there is NO silent
       default — a blank answer re-prompts and naming an existing agent is gated by
       an explicit confirm (:func:`_resolve_name`). The typed value is slugified so
       it can't yield an invalid filename.
    2. **Description.** Pre-filled from the target agent's current description on
       a re-run (so editing in place shows the existing value), else the seed
       default; a blank answer falls back to the seed default.
    3. **Provider + credentials + model.** Delegated wholesale to
       :func:`~calfcord.cli._providers.configure_provider`, which owns
       provider-select, key/Codex auth, and the live model pick. The provider
       menu is pre-selected from the install default-provider env var (read,
       never written here) so a fresh agent biases toward the operator's usual
       choice; ``current_model`` pre-selects an existing agent's model on a
       re-run.
    4. **Tools.** Normally the pre-checked builtin-tool checkbox via
       :func:`~calfcord.cli._agents.pick_tools`. ``select_tools=False`` is
       ``init``'s onboarding policy: omit ``tools:`` (all live builtins) and
       skip the checkbox altogether. ``live_tools_fn`` is passed through to the
       picker when it is used: standalone ``agent create`` leaves it ``None``
       (probe the broker's capability view for live ``mcp/<server>/<tool>`` rows).
    5. **Write.** :func:`~calfcord.cli._agents.write_agent` (validate before
       write; ``prune_seed`` only when the caller opted in).
    6. **Optional prompt edit.** When ``offer_prompt`` and the operator
       confirms, open the new agent's system prompt in ``$EDITOR`` via
       :func:`calfcord.cli.agent_edit.edit_system_prompt` (imported lazily to
       keep this module free of the subprocess/editor concern unless used).

    Returns the created agent's ``name`` and ``provider`` (as a
    :class:`CreatedAgent`) so the caller can word its own success/next-steps
    guidance (``init`` persists the provider as the install default; ``agent
    create`` just names the agent in its restart hint). Lets :class:`ValueError` /
    :class:`OSError` from :func:`~calfcord.cli._agents.write_agent` propagate — the
    caller decides how to report a write failure (and must not print a success
    banner on one).
    """
    current = read_env(env_path)

    # 1. Name (+ the agent it targets, if one already exists on disk). The two
    # naming policies (init's silent default vs. standalone's required name) live
    # in :func:`_resolve_name` so this body stays identical for both callers.
    name, prior = _resolve_name(
        prompter, agents_dir=agents_dir, name_default=name_default, require_name=require_name
    )

    # 2. Description. Pre-fill from the target (if it already exists) so a re-run
    # shows the current value; a blank answer falls back to the seed default.
    desc_default = (prior.description if prior else None) or DEFAULT_DESCRIPTION
    typed_desc = prompter.text("Agent description:", default=desc_default)
    description = typed_desc.strip() or DEFAULT_DESCRIPTION

    # 3. Provider + credentials + model. ``configure_provider`` writes only the
    # credential side effect; we read (never write) the install default-provider
    # env var purely to pre-select the menu.
    provider, model = configure_provider(
        prompter,
        env_path=env_path,
        current=current,
        default_provider=current.get(_DEFAULT_PROVIDER_VAR) or "anthropic",
        cheap=False,
        current_model=prior.model if prior else None,
    )

    # 4. Tools. Init deliberately starts with every builtin enabled, represented
    # by an omitted ``tools:`` field; custom grants remain available afterward
    # through ``disco agent tools <name>``.
    tool_grants = (
        pick_tools(prompter, name, live_tools_fn=live_tools_fn)
        if select_tools
        else ToolGrantSelection(tools=None, mcp=[])
    )

    # 5. Write (validate-before-write; prune only when the caller opted in).
    md_path = write_agent(
        agents_dir,
        name=name,
        description=description,
        provider=provider,
        model=model,
        tools=tool_grants.tools,
        mcp=tool_grants.mcp,
        prune_seed=prune_seed,
    )

    # 6. Optional system-prompt edit. Imported lazily so merely importing this
    # module (which ``init`` does at startup) never pulls in the editor/
    # subprocess machinery unless an operator actually opts to edit the prompt.
    if offer_prompt and prompter.confirm(
        "Edit this agent's system prompt now? (opens $EDITOR)", default=False
    ):
        from calfcord.cli.agent_edit import edit_system_prompt

        edit_system_prompt(md_path)

    return CreatedAgent(name=name, provider=provider)


def run(
    prompter: Prompter,
    *,
    agents_dir: Path,
    env_path: Path,
    name: str | None = None,
    home: Path | None = _ENV_DEFAULT,
    server_urls: str = _ENV_DEFAULT,
    # --- injected world-touching seams (default to the real thing) ----------
    start_fn: Callable[..., Awaitable[int]] | None = None,
    tools_start_fn: Callable[..., Awaitable[int]] | None = None,
    agent_start_fn: Callable[..., Awaitable[int]] | None = None,
    presence_fn: Callable[..., Awaitable[bool]] | None = None,
    workspace_running_fn: Callable[[Path], Awaitable[bool]] | None = None,
    pc_binary_fn: Callable[[], str] | None = None,
) -> int:
    """``disco agent create [<name>]``: create one agent and bring it online.

    The standalone create command. It runs :func:`create_agent` with
    ``prune_seed=False`` (adding an agent must never delete the operator's
    starter — only ``init``'s first-run prunes a *pristine* seed),
    ``offer_prompt=True`` (jump straight into editing the new agent's system
    prompt), and ``require_name=True`` (Change B: no silent default, no silent
    overwrite of an existing agent). A positional ``name`` pre-fills the prompt.

    On success it names the created agent then hands off to :func:`_finish_create`
    (Change A): on a native install it offers ``Start <name> now?`` and, on yes,
    brings the agent online for REAL — opening the workspace first only if it
    isn't running (the roster runs detached, outside Process Compose, so a live workspace
    needs no touch), then confirming presence on the mesh — instead of printing
    a ``disco agent start`` steer. On a dev run (no ``$CALFCORD_HOME``) or a
    missing supervisor binary it degrades to honest manual next-steps.

    ``home`` / ``server_urls`` default to a sentinel that resolves them from the
    environment (``$CALFCORD_HOME`` / ``$CALF_HOST_URL``) so ``main.py`` need not
    wire them; tests inject explicit values plus the orchestration seams to drive
    the whole flow promptless and offline.

    Per the CLI error-handling convention, a write failure (``ValueError``/
    ``OSError`` from the validate-before-write path) is reported as a single
    ``error:`` line and returns 1 with no success banner — printing "Created agent
    ..." on a failed write would send the operator off to boot processes against
    an agent that isn't there.
    """
    if home is _ENV_DEFAULT:
        env_home = os.environ.get("CALFCORD_HOME")
        home = Path(env_home) if env_home else None
    if server_urls is _ENV_DEFAULT:
        server_urls = os.getenv("CALF_HOST_URL") or "localhost"

    # The header belongs to the standalone command, not to ``create_agent`` —
    # ``init`` draws its own phase headers around that same shared flow, so
    # putting it inside would print two headers back to back there.
    render.header("disco agent create", subtitle="Add a teammate to your org.")

    try:
        created = create_agent(
            prompter,
            agents_dir=agents_dir,
            env_path=env_path,
            name_default=name,
            prune_seed=False,
            offer_prompt=True,
            require_name=True,
        )
    except (ValueError, OSError) as e:
        # The create path validates before writing, so this is either an invalid
        # value the validator rejected or a filesystem failure during the atomic
        # write — both leave no usable agent on disk. Report and stop without a
        # success banner.
        print(f"error: could not create agent {(name or '?')!r}: {e}")
        return 1

    render.success(f"Created agent {created.name!r}.")
    return _finish_create(
        prompter,
        name=created.name,
        home=home,
        server_urls=server_urls,
        start_fn=start_fn,
        tools_start_fn=tools_start_fn,
        agent_start_fn=agent_start_fn,
        presence_fn=presence_fn,
        workspace_running_fn=workspace_running_fn,
        pc_binary_fn=pc_binary_fn,
    )


def _finish_create(
    prompter: Prompter,
    *,
    name: str,
    home: Path | None,
    server_urls: str,
    start_fn: Callable[..., Awaitable[int]] | None,
    tools_start_fn: Callable[..., Awaitable[int]] | None,
    agent_start_fn: Callable[..., Awaitable[int]] | None,
    presence_fn: Callable[..., Awaitable[bool]] | None,
    workspace_running_fn: Callable[[Path], Awaitable[bool]] | None,
    pc_binary_fn: Callable[[], str] | None,
) -> int:
    """Offer to bring the just-created agent online, ends-live (Change A).

    Standalone-create's OWN live finish — deliberately separate from ``init``'s
    ``_run_finish`` so the shared :func:`create_agent` body stays reusable and
    prompt-identical for both. Only possible on a **native install** (the
    supervisor is install-scoped); on a dev run (``home is None``) or a missing
    process-compose binary it DEGRADES to honest manual next-steps rather than
    prompting "Start now?" and being unable to honor it (no green light that lies).

    On the native path it resolves the orchestration coroutines lazily (import-
    light; and the presence watcher is REUSED from ``init`` rather than duplicated)
    and runs the async :func:`_start_now`.

    The workspace probe and the "Start now?" confirm are both run HERE, on the
    sync side of the asyncio boundary, NOT inside ``asyncio.run(_start_now(...))``.

    History, because the reason has changed: this used to be *mandatory*. The old
    InquirerPy prompter's ``.execute()`` called ``asyncio.run()`` internally (via
    prompt_toolkit's ``Application.prompt()``), so nesting a prompt inside our own
    ``asyncio.run`` raised ``RuntimeError: asyncio.run() cannot be called from a
    running event loop`` — the exact crash users hit at the end of
    ``disco agent create``. The Rich/readchar prompter that replaced it owns no
    event loop, so that constraint is gone.

    The shape is kept anyway, now as a preference rather than a rule: asking
    everything first, THEN doing the work, keeps the prompts out of the middle of
    an orchestration and the failure modes separable. Don't reintroduce a prompt
    inside the async section on the grounds that it "works now" — it would, but
    the flow reads worse.
    """
    pc_binary_fn = pc_binary_fn or default_pc_binary
    # Dev run (``home is None`` — no install-scoped supervisor by design) degrades
    # silently; a native install whose supervisor binary won't resolve is a fixable
    # DEFECT — name the actionable reason rather than swallow it (§12.6). Mirrors init.
    if home is None:
        _print_manual_next_steps(name, running=None)
        return 0
    reason = supervisor_unavailable_reason(pc_binary_fn)
    if reason is not None:
        print("Your agent is created, but the workspace can't be started automatically. Fix this first:")
        print(f"  {reason}")
        _print_manual_next_steps(name, running=None)
        return 0

    if (
        start_fn is None
        or agent_start_fn is None
        or presence_fn is None
        or workspace_running_fn is None
    ):
        # Reuse ``init``'s presence watcher (imported lazily — init imports this
        # module at top, so a top-level import here would cycle) so the "is it
        # online?" logic has ONE home. The supervisor coroutines are likewise
        # deferred so `agent create` stays import-light until it actually orchestrates.
        from calfcord.cli.init import _wait_for_agent_online
        from calfcord.supervisor import lifecycle, roster

        start_fn = start_fn or lifecycle.start
        agent_start_fn = agent_start_fn or roster.agent_start
        presence_fn = presence_fn or _wait_for_agent_online
        workspace_running_fn = workspace_running_fn or _default_workspace_running

    # Probe the workspace state and ask the operator BEFORE entering asyncio. Both
    # must run sync-side: the probe's result informs the confirm's manual next-steps
    # branch, and the confirm itself cannot run inside ``asyncio.run`` (see the
    # docstring). The probe is its own ``asyncio.run`` because the real
    # ``workspace_is_up`` is async (a supervisor REST round-trip); the agent
    # orchestration is a SECOND ``asyncio.run`` so the prompt sits cleanly between
    # them rather than interleaved with IO.
    running = asyncio.run(workspace_running_fn(home))
    if not prompter.confirm(f"Start {name} now?", default=True):
        _print_manual_next_steps(name, running=running)
        return 0

    return asyncio.run(
        _start_now(
            name=name,
            home=home,
            server_urls=server_urls,
            running=running,
            start_fn=start_fn,
            tools_start_fn=tools_start_fn,
            agent_start_fn=agent_start_fn,
            presence_fn=presence_fn,
        )
    )


async def _start_now(
    *,
    name: str,
    home: Path,
    server_urls: str,
    running: bool,
    start_fn: Callable[..., Awaitable[int]],
    tools_start_fn: Callable[..., Awaitable[int]] | None,
    agent_start_fn: Callable[..., Awaitable[int]],
    presence_fn: Callable[..., Awaitable[bool]],
) -> int:
    """The native live finish: open the workspace if closed, start, watch presence.

    The operator's "Start now?" decision and the workspace-state probe have
    already happened on the sync side of the asyncio boundary in
    :func:`_finish_create`; this function is PURE orchestration — no prompting —
    so it can run safely inside ``asyncio.run``.

    Branches on the pre-resolved workspace state (``running``):

    * **not running** → :func:`_supervisor.open_workspace` opens the substrate
      (broker + bridge) AND the tools host — the same "open the workspace" ``disco
      start`` runs, so the brand-new agent's first tool call has a live host — then
      ``roster.agent_start`` spawns the agent;
    * **running** → nothing to reopen: the roster (agent + tools host) lives off
      Process Compose, so a brand-new agent spawns straight into the live workspace via
      ``roster.agent_start`` — no reload, no in-flight work lost, and the already-up
      tools host is left untouched.

    After a successful start it reuses the presence watcher and only claims the
    agent is online once it is SEEN on the mesh; a timeout (or a broker blip
    mid-watch) downgrades to the honest "try it yourself / disco doctor" hint.
    Returns ``0`` once the agent has started (presence is advisory); a workspace or
    agent-start failure propagates the underlying non-zero code.
    """
    if not running:
        # Open the workspace the SAME way `disco start` does — substrate THEN the tools
        # host — via the one shared `open_workspace`, so a cold-open `agent create` can't
        # leave the new agent's first tool call hanging on an un-started host. The
        # launcher is the install shim every supervised process execs under; the
        # tools-host start inside `open_workspace` is advisory (warn-and-continue).
        launcher = str(home / "shims" / "disco")
        rc = await open_workspace(
            home,
            server_urls=server_urls,
            launcher=launcher,
            # Start-now IS the next step the signpost would name, and it runs on the
            # next line — so the signpost would only tell the operator to do what is
            # already being done for them.
            banner=False,
            start_fn=start_fn,
            tools_start_fn=tools_start_fn,
        )
        if rc != 0:
            return rc

    rc = await agent_start_fn(home, name=name, server_urls=server_urls)
    if rc != 0:
        return rc

    # Presence is advisory — the org is already live once the agent started. A
    # broker drop mid-watch must not crash the CLI after the agent came up, so any
    # failure degrades to the same honest fallback as a clean timeout — but the
    # CAUSE is kept and named, so the operator can tell a watch blow-up from a
    # plain not-seen-yet. ``except Exception`` (not bare) deliberately lets
    # ``asyncio.CancelledError`` propagate.
    watch_error: Exception | None = None
    try:
        detected = await presence_fn(server_urls, agent_id=name, timeout_s=_ONLINE_TIMEOUT_S)
    except Exception as exc:
        detected = False
        watch_error = exc
    if detected:
        print(f"{name} is online — say !{name} hello in Discord")
    else:
        cause = f" (presence watch failed: {watch_error})" if watch_error is not None else ""
        print(
            f"  {name} is starting — try `!{name} hello` in Discord. "
            f"If nothing replies, run `disco doctor`.{cause}"
        )
    return 0


async def _default_workspace_running(home: Path) -> bool:
    """Whether this home's supervisor REST surface is answering (the workspace is up).

    The same local ``project_state`` probe the roster ops use to decide "is the
    office open?" (:func:`calfcord.supervisor._workspace.workspace_is_up`), so the
    create finish and the roster verbs agree on what "running" means.
    """
    from calfcord.supervisor._workspace import resolve_client, workspace_is_up

    client = resolve_client(None, os.fspath(home))
    return await workspace_is_up(client)


def _print_manual_next_steps(name: str, *, running: bool | None) -> None:
    """Print the honest manual bring-online sequence.

    The roster spawns off Process Compose, so a brand-new agent never needs a
    workspace reload: a RUNNING workspace only needs ``disco agent start``; a
    closed one needs ``disco start`` first. ``running=None`` (a dev run / missing
    supervisor, where the create finish couldn't probe) takes the two-step form —
    the same honest steps ``init`` degrades to.
    """
    print(f"Bring {name} online:")
    if not running:
        print("    disco start")
    print(f"    disco agent start {name}")

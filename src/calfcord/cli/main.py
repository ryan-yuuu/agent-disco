"""``calfcord-cli`` argparse entry point — the management command dispatcher.

The native ``calfcord`` shim translates user-facing management subcommands
(``calfcord init``, ``calfcord agent ...``) into ``calfcord-cli <subcommand>``
and execs them through the same locked venv as the runners. ``prog="calfcord"``
so ``--help`` reads as the command the user actually types. Future verbs
register additional subparsers; the shim only needs to know the top-level verb
(``init`` / ``doctor`` / ``agent`` / ``router``) to dispatch them here. The ``run`` /
``mcp`` / ``auth`` verbs are translated to console scripts in the shim itself, not here.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from calfcord.cli import (
    agent_create,
    agent_edit,
    agent_inspect,
    agent_lifecycle,
    agent_tools,
    doctor,
    init,
    router_setup,
)
from calfcord.cli._agents import detect_agents
from calfcord.cli._fields import FIELDS
from calfcord.cli._prompts import make_prompter
from calfcord.health.check import default_broker_probe, healthcheck
from calfcord.supervisor import lifecycle


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="calfcord",
        description="Manage a calfcord install (configure, inspect).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Guided first-run configuration of the install's .env.")

    doctor_p = sub.add_parser(
        "doctor",
        help="Preflight an install: config, broker, Discord token + app id, and agents.",
    )
    doctor_p.add_argument(
        "--offline",
        action="store_true",
        help="Skip the live Discord token check (no network).",
    )

    # ``agent`` is a verb group, not a leaf: ``required=True`` on its
    # sub-parsers makes a bare ``calfcord agent`` print help + exit non-zero
    # rather than silently no-op.
    agent_p = sub.add_parser("agent", help="Create, inspect, and edit agents.")
    agent_sub = agent_p.add_subparsers(dest="agent_command", required=True)

    create_p = agent_sub.add_parser("create", help="Create a new agent (guided wizard).")
    create_p.add_argument("name", nargs="?", help="Agent name (omit to be prompted).")

    list_p = agent_sub.add_parser("list", help="List all agents.")
    list_p.add_argument("--json", action="store_true", help="Emit a JSON array instead of a table.")

    show_p = agent_sub.add_parser("show", help="Show one agent's full config.")
    show_p.add_argument("name", help="Agent name.")
    show_p.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")

    edit_p = agent_sub.add_parser("edit", help="Interactively edit any of an agent's config fields.")
    edit_p.add_argument("name", nargs="?", help="Agent name (omit to pick interactively).")

    set_p = agent_sub.add_parser("set", help="Set config fields non-interactively (scripting/CI).")
    set_p.add_argument("name", help="Agent name.")
    _add_set_flags(set_p)

    rename_p = agent_sub.add_parser("rename", help="Rename an agent (file, slash command, and state).")
    rename_p.add_argument("old", help="Current agent name.")
    rename_p.add_argument("new", help="New agent name.")

    delete_p = agent_sub.add_parser("delete", help="Delete an agent.")
    delete_p.add_argument("name", help="Agent name.")
    delete_p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    delete_p.add_argument("--keep-state", action="store_true", help="Keep the agent's channel-subscription state file.")

    tools_p = agent_sub.add_parser("tools", help="Interactively edit an agent's tool list.")
    tools_p.add_argument("name", nargs="?", help="Agent name (omit to pick interactively).")

    # ``router`` mirrors ``agent``: a verb group, not a leaf. ``required=True``
    # makes a bare ``calfcord router`` print help + exit non-zero so the group
    # can grow further commands later.
    router_p = sub.add_parser("router", help="Manage the ambient-message router.")
    router_sub = router_p.add_subparsers(dest="router_command", required=True)
    router_sub.add_parser("setup", help="Configure the OPTIONAL ambient router (provider, model).")

    # Substrate lifecycle (design §2 / §13): bring the always-on office (broker +
    # bridge) up detached, close it, and glance at the org board. These are thin
    # veneers over :mod:`calfcord.supervisor.lifecycle`; the heavy contract (the
    # detached launch flags, the #494 priming reconcile, the readiness gate) lives
    # there. They take no flags today — the lifecycle entry points are nullary
    # beyond the resolved install paths.
    sub.add_parser("start", help="Open the workspace: broker + bridge, detached, health-gated.")
    sub.add_parser("stop", help="Close the workspace (stops the supervised substrate).")
    sub.add_parser("status", help="Show the org board: substrate + roster health.")

    # Hidden internal subcommand: the Process Compose readiness exec probe runs
    # ``calfcord _healthcheck <component>`` on the agent/tools hosts. No ``help=``
    # so it stays out of the user-facing command listing (design §4.2 / §13.2).
    health_p = sub.add_parser("_healthcheck")
    health_p.add_argument("component", help="The component to probe (broker, bridge, an agent id, ...).")
    return parser


def _resolve_home() -> Path | None:
    """Return the install home from ``$CALFCORD_HOME``, or ``None`` for dev runs.

    The shim exports ``CALFCORD_HOME`` so config + agents resolve under the
    install layout; a bare ``uv run calfcord-cli init`` (no shim) leaves it
    unset, which selects the project-local ``./.env`` / ``./agents`` defaults.
    An empty value is treated as unset so a stray ``CALFCORD_HOME=`` does not
    point config at ``"/config/.env"``.
    """
    home = os.environ.get("CALFCORD_HOME")
    return Path(home) if home else None


def _resolve_state_dir(home: Path | None) -> Path:
    """Per-agent state dir (channel-subscription JSON), needed by rename/delete.

    Mirrors the runner's resolution so the CLI moves/removes the exact file the
    agent reads: ``CALFKIT_STATE_DIR`` wins (the shim sets it to
    ``$H/state/agents``); otherwise ``$H/state/agents`` on a native install, or
    the dev ``./state/agents`` default.
    """
    override = os.environ.get("CALFKIT_STATE_DIR")
    if override:
        return Path(override)
    if home is not None:
        return home / "state" / "agents"
    return Path("state") / "agents"


def _add_set_flags(set_p: argparse.ArgumentParser) -> None:
    """Add one ``agent set`` flag per editable field, driven by the FIELDS registry.

    The single ``provider_model`` row becomes two flags (``--provider`` /
    ``--model``) so a provider switch can carry its model; every other field gets
    its declared flag with ``dest`` = the field key, so the dispatcher hands
    ``run_set`` a clean ``{key: value}`` dict with no second mapping to drift.
    """
    for field in FIELDS:
        if field.kind == "provider_model":
            continue
        suffix = f" ({field.int_min}-{field.int_max})" if field.kind == "int" else ""
        set_p.add_argument(field.flag, dest=field.key, default=None, help=field.label + suffix)
    set_p.add_argument("--provider", dest="provider", default=None, help="Model provider")
    set_p.add_argument("--model", dest="model", default=None, help="Model id")


def _collect_set_updates(args: argparse.Namespace) -> dict[str, str]:
    """Gather the provided ``agent set`` flags into a ``{field_key: value}`` dict.

    A ``--system-prompt @file`` value is expanded to the file's contents so an
    operator can script a multi-line prompt; every other value is the raw string.
    Raises OSError if an ``@file`` can't be read (the caller surfaces it cleanly).
    """
    updates: dict[str, str] = {}
    for field in FIELDS:
        if field.kind == "provider_model":
            continue
        value = getattr(args, field.key)
        if value is None:
            continue
        if field.key == "system_prompt" and value.startswith("@"):
            value = Path(value[1:]).read_text(encoding="utf-8")
        updates[field.key] = value
    for key in ("provider", "model"):
        value = getattr(args, key)
        if value is not None:
            updates[key] = value
    return updates


def _run_agent(args: argparse.Namespace) -> int:
    """Dispatch a ``calfcord agent <verb>`` command, resolving the install paths once."""
    home = _resolve_home()
    env_path, agents_dir = init.resolve_paths(home)
    cmd = args.agent_command
    if cmd == "create":
        return agent_create.run(make_prompter(), agents_dir=agents_dir, env_path=env_path, name=args.name)
    if cmd == "list":
        return agent_inspect.run_list(agents_dir, as_json=args.json)
    if cmd == "show":
        return agent_inspect.run_show(agents_dir, args.name, as_json=args.json)
    if cmd == "edit":
        return agent_edit.run(make_prompter(), agents_dir=agents_dir, env_path=env_path, name=args.name)
    if cmd == "set":
        try:
            updates = _collect_set_updates(args)
        except OSError as e:
            print(f"error: {e}")
            return 1
        return agent_lifecycle.run_set(agents_dir, args.name, updates)
    if cmd == "rename":
        return agent_lifecycle.run_rename(agents_dir, _resolve_state_dir(home), args.old, args.new)
    if cmd == "delete":
        return agent_lifecycle.run_delete(
            make_prompter(), agents_dir, _resolve_state_dir(home), args.name,
            yes=args.yes, keep_state=args.keep_state,
        )
    # ``tools`` (and any unhandled verb — argparse ``required=True`` prevents the latter)
    return agent_tools.run(make_prompter(), agents_dir=agents_dir, name=args.name)


def _run_healthcheck(component: str) -> int:
    """Run the readiness probe for ``component`` and return its exit code.

    The Process Compose exec probe shells out to ``calfcord _healthcheck
    <component>`` on the agent/tools hosts (design §4.2 / §13.2). The broker probe
    is metadata reachability built from ``CALF_HOST_URL`` (same default the runners
    use); every other component is judged by heartbeat freshness under the resolved
    home's ``state/health/``. ``now`` is the real clock — freshness is wall-time
    here, injectable only in the unit-tested :func:`~calfcord.health.check.healthcheck`.
    """
    home = _resolve_home() or Path()
    # Only the broker path needs (and awaits) a broker probe; a heartbeat check
    # must not pay the admin-client cost, so the probe is built lazily and the
    # heartbeat path gets a stub that is never awaited.
    if component == "broker":
        server_urls = os.getenv("CALF_HOST_URL") or "localhost"
        broker_probe = default_broker_probe(server_urls)
    else:
        async def broker_probe() -> bool:
            raise AssertionError("broker probe awaited for a non-broker component")

    return asyncio.run(
        healthcheck(home, component, now=datetime.now(UTC), broker_probe=broker_probe)
    )


def _run_lifecycle(command: str) -> int:
    """Dispatch a substrate-lifecycle verb (``start`` / ``stop`` / ``status``).

    The Process Compose supervisor is *install-scoped*: its lock, derived REST
    port, generated project, and logs all live under ``$CALFCORD_HOME/state``,
    and ``start`` supervises processes by execing the install's shim. A dev run
    (no ``CALFCORD_HOME``) has neither a shim nor a stable home, so these verbs
    refuse to run there with an actionable message rather than launching a
    half-built supervisor against the project-local dev tree.

    ``server_urls`` comes from ``CALF_HOST_URL`` (defaulting to ``localhost``,
    the same default the runners and the broker healthcheck use); the roster is
    the install's defined agents (:func:`detect_agents`, the seam ``agent list``
    consumes) so the generated project declares one disabled slot per ``.md``.
    The lifecycle coroutine's POSIX exit code is propagated unchanged.
    """
    home = _resolve_home()
    if home is None:
        print(
            f"error: `calfcord {command}` needs a native install — set CALFCORD_HOME "
            "(or run the installer) so the supervisor has a stable home and shim."
        )
        return 1

    if command == "stop":
        return asyncio.run(lifecycle.stop(home))
    if command == "status":
        return asyncio.run(lifecycle.status(home))

    # ``start`` additionally needs the shim launcher every supervised process
    # execs under, the broker URL, and the roster to declare.
    _, agents_dir = init.resolve_paths(home)
    launcher = str(home / "shims" / "calfcord")
    server_urls = os.getenv("CALF_HOST_URL") or "localhost"
    return asyncio.run(
        lifecycle.start(
            home,
            server_urls=server_urls,
            launcher=launcher,
            agent_ids=detect_agents(agents_dir),
        )
    )


def _dispatch(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Route a parsed command to its handler (the interactive, prompt-driven part)."""
    if args.command in ("start", "stop", "status"):
        return _run_lifecycle(args.command)

    if args.command == "init":
        env_path, agents_dir = init.resolve_paths(_resolve_home())
        return init.run(make_prompter(), env_path=env_path, agents_dir=agents_dir)

    if args.command == "doctor":
        # Preflight the same config/.env + agents/ the runners load.
        env_path, agents_dir = init.resolve_paths(_resolve_home())
        return doctor.run(env_path=env_path, agents_dir=agents_dir, offline=args.offline)

    if args.command == "agent":
        return _run_agent(args)

    if args.command == "_healthcheck":
        return _run_healthcheck(args.component)

    if args.command == "router" and args.router_command == "setup":
        # Reuse init's path resolution so the router wizard writes the same
        # config/.env the runners load (native: $H/config/.env; dev: ./.env).
        env_path, _ = init.resolve_paths(_resolve_home())
        return router_setup.run(make_prompter(), env_path=env_path)

    parser.error(f"unknown command: {args.command}")
    return 2  # unreachable; parser.error exits


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    # The dispatch drives interactive prompts; trap the two ways an operator/host
    # ends one abruptly so the management CLI exits cleanly instead of dumping a
    # traceback (matching every other calfcord entry point). The run_* handlers
    # already map their own filesystem errors to exit codes, so an interrupt or a
    # raw-mode failure is all that should escape to here.
    try:
        return _dispatch(parser, args)
    except KeyboardInterrupt:
        print("\naborted.")
        return 130
    except EOFError:
        print("error: this command needs an interactive terminal (stdin reached EOF).")
        return 1
    except OSError:
        # InquirerPy/prompt_toolkit raises OSError (EINVAL) when it can't put a
        # non-TTY stdin (piped / CI) into raw mode. Surface that cleanly, but only
        # when stdin genuinely isn't a TTY — re-raise anything else rather than
        # masking a real bug behind a friendly message.
        if not sys.stdin.isatty():
            print("error: this command needs an interactive terminal (stdin is not a TTY).")
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())

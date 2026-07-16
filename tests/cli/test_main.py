"""Tests for the ``calfcord-cli`` argparse entry point and path resolution.

These confirm the entry point is importable and wired (``--help`` exits 0,
the ``init`` subcommand is registered) and that :func:`init.resolve_paths`
honours the native (``$CALFCORD_HOME``) vs dev layouts and the
``CALFKIT_AGENTS_DIR`` override the shim/runners already respect.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from calfcord.cli import (
    agent_create,
    agent_edit,
    agent_inspect,
    agent_lifecycle,
    deploy,
    doctor,
    explain,
    init,
    logs,
    mcp_admin,
    tool_aliases,
)
from calfcord.cli import main as main_mod
from calfcord.cli._agents import CREATE_SENTINEL
from calfcord.cli.main import main
from calfcord.supervisor import component, lifecycle, mcp_roster, roster


def test_main_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_main_help_epilog_signposts_getting_started(capsys: pytest.CaptureFixture[str]) -> None:
    """Top-level ``--help`` teaches the getting-started arc: init → add a teammate →
    the org board, plus where concepts + docs live. The epilog must survive the raw
    formatter (its indentation/newlines are load-bearing)."""
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "Getting started:" in out
    assert "disco init" in out
    assert "disco agent create <name>" in out
    assert "disco status" in out
    assert "disco explain topology" in out
    assert "docs/using-disco.md" in out


def test_main_agent_help_unaffected_by_top_level_epilog(capsys: pytest.CaptureFixture[str]) -> None:
    """The getting-started epilog is scoped to the TOP-LEVEL parser; subparsers
    (e.g. ``disco agent --help``) must not inherit it."""
    with pytest.raises(SystemExit):
        main(["agent", "--help"])
    out = capsys.readouterr().out
    assert "Getting started:" not in out


def test_main_init_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["init", "--help"])
    assert exc.value.code == 0


def test_main_requires_subcommand() -> None:
    # No subcommand → argparse errors out (exit 2), never a silent success.
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_resolve_paths_native_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    home = tmp_path / "home"
    env_path, agents_dir = init.resolve_paths(home)
    assert env_path == home / "config" / ".env"
    assert agents_dir == home / "agents"


def test_resolve_paths_dev_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    env_path, agents_dir = init.resolve_paths(None)
    assert env_path == Path(".env")
    assert agents_dir == Path("agents")


def test_resolve_paths_agents_dir_override_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CALFKIT_AGENTS_DIR", str(tmp_path / "custom-agents"))
    # Override beats both the native-home and dev defaults.
    _, native_agents = init.resolve_paths(tmp_path / "home")
    _, dev_agents = init.resolve_paths(None)
    assert native_agents == Path(os.environ["CALFKIT_AGENTS_DIR"])
    assert dev_agents == Path(os.environ["CALFKIT_AGENTS_DIR"])


# --- _require_home: the shared native-install guard ------------------------


def test_require_home_returns_resolved_home_silently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # On a native install the guard returns the resolved home and prints nothing,
    # so the caller proceeds to drive the supervisor.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    assert main_mod._require_home("deploy") == home
    assert capsys.readouterr().out == ""


def test_require_home_dev_run_prints_message_and_returns_none(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A dev run (no CALFCORD_HOME) returns None after printing the actionable
    # native-install steer, so the caller can `return 1` without crashing
    # downstream in os.fspath(None). The default detail is the supervisor home.
    monkeypatch.delenv("CALFCORD_HOME", raising=False)
    assert main_mod._require_home("agent stop") is None
    out = capsys.readouterr().out
    assert out == (
        "error: `disco agent stop` needs a native install — set CALFCORD_HOME "
        "(or run the installer) so the supervisor has a stable home.\n"
    )


def test_require_home_detail_customizes_trailing_clause(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # `deploy`/`logs`/`start` pass a verb-specific rationale (manifest / logs dir
    # / shim) that the guard substitutes after "so ".
    monkeypatch.delenv("CALFCORD_HOME", raising=False)
    assert main_mod._require_home("deploy", detail="the manifest can reference a stable home and shim.") is None
    out = capsys.readouterr().out
    assert out == (
        "error: `disco deploy` needs a native install — set CALFCORD_HOME "
        "(or run the installer) so the manifest can reference a stable home and shim.\n"
    )


# --- agent verb group: help + dispatch -------------------------------------


@pytest.mark.parametrize(
    "verb",
    ["create", "list", "show", "edit", "set", "rename", "delete", "tools"],
)
def test_main_agent_subcommand_help_exits_zero(verb: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["agent", verb, "--help"])
    assert exc.value.code == 0


def test_main_agent_requires_subcommand() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["agent"])
    assert exc.value.code != 0


def _use_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the resolver at a temp agents dir via the env override."""
    monkeypatch.setenv("CALFKIT_AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.delenv("CALFCORD_HOME", raising=False)


def test_main_agent_list_dispatches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _use_dirs(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def _run_list(agents_dir: Path, *, as_json: bool) -> int:
        captured.update(agents_dir=agents_dir, as_json=as_json)
        return 0

    monkeypatch.setattr(agent_inspect, "run_list", _run_list)
    assert main(["agent", "list", "--json"]) == 0
    assert captured == {"agents_dir": tmp_path / "agents", "as_json": True}


def test_main_agent_set_collects_flags_and_provider_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _use_dirs(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def _run_set(agents_dir: Path, name: str, updates: dict[str, str]) -> int:
        captured.update(name=name, updates=updates)
        return 0

    monkeypatch.setattr(agent_lifecycle, "run_set", _run_set)
    rc = main(
        [
            "agent",
            "set",
            "scribe",
            "--description",
            "Has: colon",
            "--thinking-effort",
            "high",
            "--tools",
            "read_file,shell",
            "--provider",
            "openai",
            "--model",
            "gpt-5-nano",
        ]
    )
    assert rc == 0
    assert captured["name"] == "scribe"
    assert captured["updates"] == {
        "description": "Has: colon",
        "thinking_effort": "high",
        "tools": "read_file,shell",
        "provider": "openai",
        "model": "gpt-5-nano",
    }


def test_main_agent_set_expands_prompt_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _use_dirs(monkeypatch, tmp_path)
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("You are Scribe.\nBe terse.\n")
    captured: dict[str, object] = {}

    def _run_set(agents_dir: Path, name: str, updates: dict[str, str]) -> int:
        captured.update(updates=updates)
        return 0

    monkeypatch.setattr(agent_lifecycle, "run_set", _run_set)
    assert main(["agent", "set", "scribe", f"--system-prompt=@{prompt_file}"]) == 0
    assert captured["updates"] == {"system_prompt": "You are Scribe.\nBe terse.\n"}


def test_main_agent_set_missing_prompt_file_errors_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _use_dirs(monkeypatch, tmp_path)
    assert main(["agent", "set", "scribe", "--system-prompt=@/no/such/file.md"]) == 1
    assert "error:" in capsys.readouterr().out


def test_main_agent_rename_dispatches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _use_dirs(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def _run_rename(agents_dir: Path, old: str, new: str) -> int:
        captured.update(agents_dir=agents_dir, old=old, new=new)
        return 0

    monkeypatch.setattr(agent_lifecycle, "run_rename", _run_rename)
    assert main(["agent", "rename", "scribe", "penny"]) == 0
    assert captured == {
        "agents_dir": tmp_path / "agents",
        "old": "scribe",
        "new": "penny",
    }


def test_main_agent_delete_passes_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _use_dirs(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def _run_delete(prompter: object, agents_dir: Path, name: str, *, yes: bool) -> int:
        captured.update(name=name, yes=yes)
        return 0

    monkeypatch.setattr(agent_lifecycle, "run_delete", _run_delete)
    assert main(["agent", "delete", "scribe", "--yes"]) == 0
    assert captured == {"name": "scribe", "yes": True}


# --- main(): interrupt + raw-mode trapping ---------------------------------


def test_main_traps_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A ^C during the interactive dispatch exits 130 with ``aborted.``, not a traceback."""

    def _interrupt(parser: object, args: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(main_mod, "_dispatch", _interrupt)
    assert main(["init"]) == 130
    assert "aborted." in capsys.readouterr().out


def test_main_maps_oserror_to_clean_exit_when_stdin_not_a_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A raw-mode ``OSError`` on a non-TTY stdin → exit 1 + a hint.

    Synthetic by design: this pins ``main``'s *handler*, not the reader. The
    handler cares only that the exception is an ``OSError`` and that stdin is not
    a TTY, so the errno is arbitrary here.

    It therefore CANNOT prove the real path works — the reader's exception has to
    reach this branch in the first place, and the shipped bug was that it did not
    (``termios.error`` does not subclass ``OSError``). ``tests/cli/tui/
    test_non_tty_end_to_end.py`` covers that join against a real closed stdin;
    this one keeps its own half honest.
    """

    def _raise(parser: object, args: object) -> int:
        raise OSError(errno.ENOTTY, "stdin is not an interactive terminal")

    monkeypatch.setattr(main_mod, "_dispatch", _raise)
    monkeypatch.setattr(main_mod.sys.stdin, "isatty", lambda: False)

    assert main(["init"]) == 1
    assert "interactive terminal" in capsys.readouterr().out


def test_main_reports_a_missing_launcher_as_a_launch_failure_not_a_tty_problem(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A FileNotFoundError from spawning the shim must surface as what it is — a
    missing/non-executable launcher, naming the path — even on a non-TTY stdin
    (it must NOT be misdiagnosed as 'needs an interactive terminal')."""

    def _raise(parser: object, args: object) -> int:
        raise FileNotFoundError(2, "No such file or directory", "/opt/disco/shims/disco")

    monkeypatch.setattr(main_mod, "_dispatch", _raise)
    monkeypatch.setattr(main_mod.sys.stdin, "isatty", lambda: False)

    assert main(["agent", "start", "scribe"]) == 1
    out = capsys.readouterr().out
    assert "/opt/disco/shims/disco" in out
    assert "interactive terminal" not in out


def test_main_reports_a_non_executable_launcher_with_its_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _raise(parser: object, args: object) -> int:
        raise PermissionError(13, "Permission denied", "/opt/disco/shims/disco")

    monkeypatch.setattr(main_mod, "_dispatch", _raise)
    monkeypatch.setattr(main_mod.sys.stdin, "isatty", lambda: False)

    assert main(["agent", "start", "scribe"]) == 1
    out = capsys.readouterr().out
    assert "/opt/disco/shims/disco" in out
    assert "interactive terminal" not in out


def test_main_reraises_oserror_on_a_real_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``OSError`` with stdin a real TTY is a genuine bug — it must propagate,
    not be masked behind the friendly non-TTY message."""

    def _raise(parser: object, args: object) -> int:
        raise OSError(5, "I/O error")

    monkeypatch.setattr(main_mod, "_dispatch", _raise)
    monkeypatch.setattr(main_mod.sys.stdin, "isatty", lambda: True)

    with pytest.raises(OSError):
        main(["init"])


# --- doctor: help + dispatch -----------------------------------------------


def test_main_doctor_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["doctor", "--help"])
    assert exc.value.code == 0


def test_main_doctor_dispatches_with_resolved_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The shim exports CALFCORD_HOME; doctor must run against the install's config/.env + agents/,
    # and (phase 4) the install home must be threaded through so the runtime section activates.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    def _run(*, env_path: Path, agents_dir: Path, offline: bool = False, home: object = None, **kwargs: object) -> int:
        captured.update(env_path=env_path, agents_dir=agents_dir, offline=offline, home=home)
        return 0

    monkeypatch.setattr(doctor, "run", _run)
    assert main(["doctor", "--offline"]) == 0
    assert captured["env_path"] == home / "config" / ".env"
    assert captured["agents_dir"] == home / "agents"
    assert captured["offline"] is True
    # The resolved install home is passed so doctor's runtime daemon-health section runs.
    assert captured["home"] == home


def test_main_doctor_passes_none_home_in_dev_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A dev run (no CALFCORD_HOME) has no install heartbeats; doctor must receive
    # home=None so the runtime section is correctly skipped (not a half-built probe).
    monkeypatch.delenv("CALFCORD_HOME", raising=False)
    monkeypatch.setenv("CALFKIT_AGENTS_DIR", str(tmp_path / "agents"))
    captured: dict[str, object] = {}

    def _run(*, env_path: Path, agents_dir: Path, offline: bool = False, home: object = None, **kwargs: object) -> int:
        captured.update(home=home)
        return 0

    monkeypatch.setattr(doctor, "run", _run)
    assert main(["doctor"]) == 0
    assert captured["home"] is None


def test_main_doctor_fix_flag_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # `--fix` is DEFERRED (no auto-repair plumbing in doctor.run), so the flag was
    # removed rather than advertised in --help while doing nothing. An unrecognized
    # flag must make argparse error (exit 2), never silently accept-and-no-op.
    def _boom(**kwargs: object) -> int:
        raise AssertionError("doctor.run must not run for an unrecognized flag")

    monkeypatch.setattr(doctor, "run", _boom)
    with pytest.raises(SystemExit) as exc:
        main(["doctor", "--fix"])
    assert exc.value.code == 2


# --- _healthcheck: hidden readiness-probe subcommand -----------------------


def test_main_healthcheck_broker_exits_with_probe_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The hidden _healthcheck broker probe returns 0 when the injected production
    # probe reports the broker reachable. main resolves home + builds the probe
    # from CALF_HOST_URL; we patch the probe builder so no live Kafka is needed.
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CALF_HOST_URL", "localhost:9092")
    captured: dict[str, object] = {}

    async def _reachable() -> bool:
        return True

    def _builder(server_urls: str) -> object:
        captured["server_urls"] = server_urls
        return _reachable

    monkeypatch.setattr(main_mod, "default_broker_probe", _builder)
    assert main(["_healthcheck", "broker"]) == 0
    # The probe is built from CALF_HOST_URL, exactly as the runners read it.
    assert captured["server_urls"] == "localhost:9092"


def test_main_healthcheck_broker_unreachable_exits_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CALF_HOST_URL", "localhost:9092")

    async def _unreachable() -> bool:
        return False

    monkeypatch.setattr(main_mod, "default_broker_probe", lambda server_urls: _unreachable)
    assert main(["_healthcheck", "broker"]) == 1


def test_main_healthcheck_defaults_host_url_to_localhost(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # CALF_HOST_URL unset → "localhost" (same default the runners use), so the
    # probe is still buildable on a bare dev box.
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CALF_HOST_URL", raising=False)
    captured: dict[str, object] = {}

    async def _reachable() -> bool:
        return True

    def _builder(server_urls: str) -> object:
        captured["server_urls"] = server_urls
        return _reachable

    monkeypatch.setattr(main_mod, "default_broker_probe", _builder)
    assert main(["_healthcheck", "broker"]) == 0
    assert captured["server_urls"] == "localhost"


def test_main_healthcheck_bridge_reads_heartbeat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A non-broker component is judged by heartbeat freshness under the resolved
    # home's state/health/ — no broker probe is consulted at all. A fresh beat → 0.
    from datetime import UTC, datetime

    from calfcord.health.heartbeat import write_beat

    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    write_beat(home, "bridge", status="healthy", now=datetime.now(UTC))

    # Building a broker probe for a non-broker check would be a bug; make the
    # builder explode so the test fails loudly if the broker path is taken.
    def _explode(server_urls: str) -> object:
        raise AssertionError("broker probe must not be built for a heartbeat check")

    monkeypatch.setattr(main_mod, "default_broker_probe", _explode)
    assert main(["_healthcheck", "bridge"]) == 0


def test_main_healthcheck_bridge_missing_beat_exits_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))
    assert main(["_healthcheck", "bridge"]) == 1


def test_main_agent_create_and_edit_dispatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _use_dirs(monkeypatch, tmp_path)
    seen: list[tuple[str, str | None]] = []

    def _create(prompter: object, *, agents_dir: Path, env_path: Path, name: str | None) -> int:
        seen.append(("create", name))
        return 0

    def _edit(prompter: object, *, agents_dir: Path, env_path: Path, name: str | None) -> int:
        seen.append(("edit", name))
        return 0

    monkeypatch.setattr(agent_create, "run", _create)
    monkeypatch.setattr(agent_edit, "run", _edit)
    assert main(["agent", "create", "scribe"]) == 0
    assert main(["agent", "edit"]) == 0
    assert seen == [("create", "scribe"), ("edit", None)]


# --- substrate lifecycle: start / stop / status ----------------------------


@pytest.mark.parametrize("verb", ["start", "stop", "status"])
def test_main_lifecycle_help_exits_zero(verb: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main([verb, "--help"])
    assert exc.value.code == 0


def test_main_start_dispatches_with_resolved_args(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # ``start`` must resolve home from CALFCORD_HOME, build the launcher as the
    # install's shim, and read server_urls from CALF_HOST_URL — then asyncio.run
    # lifecycle.start and propagate its exit code. (The defined roster is read by
    # lifecycle.start itself for its banner signpost; nothing is threaded here.)
    home = tmp_path / "home"
    (home / "agents").mkdir(parents=True)
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.setenv("CALF_HOST_URL", "broker.example:9092")
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)

    captured: dict[str, object] = {}

    async def _start(home_arg, *, server_urls, launcher, **kwargs):
        captured.update(
            home=home_arg,
            server_urls=server_urls,
            launcher=launcher,
        )
        return 0

    monkeypatch.setattr(lifecycle, "start", _start)
    monkeypatch.setattr(component, "component_start", _noop_tools_start)
    assert main(["start"]) == 0
    assert captured["home"] == home
    assert captured["server_urls"] == "broker.example:9092"
    assert captured["launcher"] == str(home / "shims" / "disco")


def test_main_start_propagates_nonzero_exit_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "agents").mkdir(parents=True)
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)

    async def _start(*args, **kwargs):
        return 1

    monkeypatch.setattr(lifecycle, "start", _start)
    assert main(["start"]) == 1


def test_main_start_without_home_errors_native_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Dev runs (no CALFCORD_HOME) have no shim to launch under, so ``start`` must
    # fail with a clear native-install message rather than asyncio.run a half-built
    # invocation.
    monkeypatch.delenv("CALFCORD_HOME", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("lifecycle.start must not run without a home")

    monkeypatch.setattr(lifecycle, "start", _boom)
    assert main(["start"]) == 1
    out = capsys.readouterr().out
    assert "native install" in out


def test_main_start_defaults_host_url_to_localhost(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "agents").mkdir(parents=True)
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALF_HOST_URL", raising=False)
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)

    captured: dict[str, object] = {}

    async def _start(home_arg, *, server_urls, **kwargs):
        captured["server_urls"] = server_urls
        return 0

    monkeypatch.setattr(lifecycle, "start", _start)
    monkeypatch.setattr(component, "component_start", _noop_tools_start)
    assert main(["start"]) == 0
    assert captured["server_urls"] == "localhost"


async def _noop_tools_start(home, **kwargs) -> int:
    """A component_start double for `disco start` tests that don't care about the
    tools host — keeps them hermetic (no real supervisor probe) now that `start`
    brings the tools host up after the substrate."""
    return 0


def test_main_start_also_starts_tools_host_after_substrate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`disco start` opens the workspace = substrate + the tools host (identity-
    agnostic infra every tool-using agent needs). The tools host is brought up AFTER
    the substrate, via the same ``component_start`` ``disco tools start`` uses, at the
    ``tools`` slot and under the install shim launcher."""
    home = tmp_path / "home"
    (home / "agents").mkdir(parents=True)
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.setenv("CALF_HOST_URL", "broker.example:9092")
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)

    order: list[str] = []
    tools_calls: list[dict] = []

    async def _start(home_arg, *, server_urls, launcher, **kwargs):
        order.append("substrate")
        return 0

    async def _tools(home_arg, *, name, launcher, **kwargs):
        order.append("tools")
        tools_calls.append({"home": home_arg, "name": name, "launcher": launcher})
        return 0

    monkeypatch.setattr(lifecycle, "start", _start)
    monkeypatch.setattr(component, "component_start", _tools)
    assert main(["start"]) == 0
    assert order == ["substrate", "tools"]
    assert tools_calls[0]["name"] == "tools"
    assert tools_calls[0]["home"] == home
    assert tools_calls[0]["launcher"] == str(home / "shims" / "disco")


def test_main_start_substrate_failure_skips_tools_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A substrate that fails to open short-circuits: the tools host is NOT spawned
    against a workspace that never came up, and the non-zero code propagates."""
    home = tmp_path / "home"
    (home / "agents").mkdir(parents=True)
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)

    async def _start(*args, **kwargs):
        return 1

    async def _boom_tools(*args, **kwargs):
        raise AssertionError("the tools host must not start when the substrate failed")

    monkeypatch.setattr(lifecycle, "start", _start)
    monkeypatch.setattr(component, "component_start", _boom_tools)
    assert main(["start"]) == 1


def test_main_start_tools_host_failure_warns_but_returns_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The tools-host start is advisory: if the substrate is up but the tools host
    fails, ``disco start`` still reports the workspace open (exit 0) and warns how to
    bring the tools host up — it never fails an otherwise-open workspace over it."""
    home = tmp_path / "home"
    (home / "agents").mkdir(parents=True)
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)

    async def _start(*args, **kwargs):
        return 0

    async def _tools(*args, **kwargs):
        return 1

    monkeypatch.setattr(lifecycle, "start", _start)
    monkeypatch.setattr(component, "component_start", _tools)
    assert main(["start"]) == 0  # workspace is open even if the tools host lagged
    out = capsys.readouterr().out
    assert "disco tools start" in out


def test_main_stop_dispatches_with_resolved_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))

    captured: dict[str, object] = {}

    async def _stop(home_arg, **kwargs):
        captured["home"] = home_arg
        return 0

    monkeypatch.setattr(lifecycle, "stop", _stop)
    assert main(["stop"]) == 0
    assert captured["home"] == home


def test_main_stop_propagates_exit_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))

    async def _stop(*args, **kwargs):
        return 3

    monkeypatch.setattr(lifecycle, "stop", _stop)
    assert main(["stop"]) == 3


def test_main_stop_contended_lock_prints_one_clean_error_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`disco stop` racing an in-flight roster verb (which holds the lifecycle lock
    SHARED for its spawn-confirm window) must exit 1 with ONE clean error line —
    never a raw RuntimeError traceback escaping main()."""
    from calfcord.supervisor import _workspace

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))

    # The REAL lifecycle.stop runs: the contended lock raises before any REST call.
    with _workspace.slot_mutation(str(home), "assistant"):
        rc = main(["stop"])

    assert rc == 1
    out = capsys.readouterr().out
    assert out.startswith("error:")
    assert "in progress" in out
    assert "Traceback" not in out


def test_main_status_dispatches_with_resolved_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))

    captured: dict[str, object] = {}

    async def _status(home_arg, **kwargs):
        captured["home"] = home_arg
        return 0

    monkeypatch.setattr(lifecycle, "status", _status)
    assert main(["status"]) == 0
    assert captured["home"] == home


def test_main_stop_without_home_errors_native_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CALFCORD_HOME", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("lifecycle.stop must not run without a home")

    monkeypatch.setattr(lifecycle, "stop", _boom)
    assert main(["stop"]) == 1
    assert "native install" in capsys.readouterr().out


def test_main_status_without_home_errors_native_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CALFCORD_HOME", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("lifecycle.status must not run without a home")

    monkeypatch.setattr(lifecycle, "status", _boom)
    assert main(["status"]) == 1
    assert "native install" in capsys.readouterr().out


# --- roster lifecycle: agent start / stop / restart / ps -------------------


@pytest.mark.parametrize("verb", ["start", "stop", "restart", "ps"])
def test_main_agent_roster_help_exits_zero(verb: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["agent", verb, "--help"])
    assert exc.value.code == 0


@pytest.mark.parametrize("verb", ["stop", "restart"])
def test_main_agent_roster_requires_name_or_all(verb: str) -> None:
    # Exactly one of <name> | --all is required: a bare `agent stop` (neither)
    # must error (exit 2), never silently act on nothing. The name is now
    # optional (nargs="?") so the mutual-exclusion is enforced in the dispatcher
    # via parser.error (which exits 2), not by argparse's required-positional.
    #
    # ``start`` is deliberately absent from this list: a bare `agent start` opens
    # a picker instead (TestBareAgentStartPicksInteractively). stop/restart keep
    # erroring because their honest pick-list is the RUNNING roster, which needs a
    # broker probe — a DEFINED-agent picker there would invite choosing an agent
    # that is not running.
    with pytest.raises(SystemExit) as exc:
        main(["agent", verb])
    assert exc.value.code == 2


async def _workspace_up(_home: Path) -> bool:
    """The supervisor probe `create_for_start` runs, answering 'workspace open'."""
    return True


class _PickingPrompter:
    """A prompter that answers the agent picker and records what it was shown."""

    def __init__(self, choose: str) -> None:
        self._choose = choose
        self.offered: list = []

    def select(self, message: str, choices: list, *, default: str | None = None) -> str:
        self.offered = choices
        # The real widget can only return a row it painted. Without this, a test
        # that scripts a row the picker never offered still passes — so deleting
        # the create row would leave the create tests green, testing a row no
        # operator could select.
        offered = [c.value for c in choices]
        assert self._choose in offered, f"{self._choose!r} was never offered: {offered}"
        return self._choose

    def text(self, message: str, *, default: str = "") -> str:
        raise AssertionError("the picker only selects")

    def secret(self, message: str) -> str:
        raise AssertionError("the picker only selects")

    def confirm(self, message: str, *, default: bool = False) -> bool:
        raise AssertionError("the picker only selects")

    def pause(self, message: str) -> None:
        raise AssertionError("the picker only selects")

    def checkbox(self, message: str, choices: list, *, instruction: str = "") -> list[str]:
        raise AssertionError("the picker only selects")


class TestBareAgentStartPicksInteractively:
    """`disco agent start` with no name offers the defined agents.

    The bare verb used to be a parser error, which told the operator what they did
    wrong but not what to do instead — while the CLI already knew the whole answer,
    since `agent list` reads it off disk. Same shape as `agent tools` / `agent
    edit`, which have always picked.
    """

    def _wire(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, agents: list[str]) -> list[str]:
        home = tmp_path / "home"
        (home / "agents").mkdir(parents=True)
        for name in agents:
            (home / "agents" / f"{name}.md").write_text(f"---\nname: {name}\n---\nbody\n")
        monkeypatch.setenv("CALFCORD_HOME", str(home))

        started: list[str] = []

        async def _start(_home: Path, *, name: str, server_urls: str) -> int:
            started.append(name)
            return 0

        monkeypatch.setattr(roster, "agent_start", _start)
        return started

    def test_the_picked_agent_is_started(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        started = self._wire(monkeypatch, tmp_path, ["archivist", "scribe"])
        monkeypatch.setattr(main_mod, "make_prompter", lambda: _PickingPrompter("scribe"))
        assert main(["agent", "start"]) == 0
        assert started == ["scribe"]

    def test_the_picker_offers_every_defined_agent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._wire(monkeypatch, tmp_path, ["archivist", "scribe"])
        picker = _PickingPrompter("scribe")
        monkeypatch.setattr(main_mod, "make_prompter", lambda: picker)
        main(["agent", "start"])
        # Filters the create row rather than expecting an exact list: this test's
        # subject is the ROSTER's completeness and order. That the list also ends
        # with a create row is TestBareAgentStartCanCreate's subject, and pinning
        # it here too would make one behaviour change break two unrelated tests.
        agents = [c.value for c in picker.offered if c.value != CREATE_SENTINEL]
        assert agents == ["archivist", "scribe"]

    def test_an_empty_roster_offers_to_create_rather_than_dead_ending(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An empty roster is now answerable: the one honest answer is offered.

        This used to assert the opposite — that no picker opened, and that the
        operator was told to go and run ``disco agent create``. That was right
        while a choice-less list was the only alternative; now that creating is
        a row, the list has exactly one honest answer and offering it beats
        naming a command to type.
        """
        self._wire(monkeypatch, tmp_path, [])
        picker = _PickingPrompter(CREATE_SENTINEL)
        monkeypatch.setattr(main_mod, "make_prompter", lambda: picker)
        monkeypatch.setattr(
            agent_create,
            "create_agent",
            lambda *a, **k: agent_create.CreatedAgent(name="first", provider="anthropic"),
        )
        monkeypatch.setattr(agent_create, "_default_workspace_running", _workspace_up)

        assert main(["agent", "start"]) == 0
        assert [c.value for c in picker.offered] == [CREATE_SENTINEL]

    def test_a_named_start_never_opens_the_picker(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        started = self._wire(monkeypatch, tmp_path, ["archivist", "scribe"])

        def _never() -> object:
            raise AssertionError("an explicit name must not be second-guessed")

        monkeypatch.setattr(main_mod, "make_prompter", _never)
        assert main(["agent", "start", "archivist"]) == 0
        assert started == ["archivist"]

    def test_start_all_never_opens_the_picker(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._wire(monkeypatch, tmp_path, ["archivist", "scribe"])
        swept: list[list[str]] = []

        async def _start_all(_home: Path, *, agent_ids: list[str], server_urls: str) -> int:
            swept.append(agent_ids)
            return 0

        monkeypatch.setattr(roster, "agent_start_all", _start_all)

        def _never() -> object:
            raise AssertionError("--all is already a complete answer")

        monkeypatch.setattr(main_mod, "make_prompter", _never)
        assert main(["agent", "start", "--all"]) == 0
        assert swept == [["archivist", "scribe"]]

    def test_a_name_and_all_together_still_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The picker resolves 'neither'. 'Both' is still contradictory."""
        self._wire(monkeypatch, tmp_path, ["scribe"])
        with pytest.raises(SystemExit) as exc:
            main(["agent", "start", "scribe", "--all"])
        assert exc.value.code == 2
        assert "mutually exclusive" in capsys.readouterr().err


class TestBareAgentStartCanCreate:
    """`disco agent start` offers creating when none of the roster is what you want.

    The picker answers "which agent?" — but "none of these" was unanswerable, and
    the operator's real next move (make one) meant quitting to another command.
    The create row reuses ``agent_create.create_agent``, the flow ``agent create``
    and ``init`` already share, so there is exactly one way an agent comes into
    being.
    """

    def _wire(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, agents: list[str]
    ) -> tuple[list[str], dict]:
        home = tmp_path / "home"
        (home / "agents").mkdir(parents=True)
        for name in agents:
            (home / "agents" / f"{name}.md").write_text(f"---\nname: {name}\n---\nbody\n")
        monkeypatch.setenv("CALFCORD_HOME", str(home))

        started: list[str] = []
        captured: dict = {}

        async def _start(_home: Path, *, name: str, server_urls: str) -> int:
            started.append(name)
            return 0

        def _create(prompter, **kwargs) -> agent_create.CreatedAgent:
            captured.update(kwargs)
            return agent_create.CreatedAgent(name="researcher", provider="anthropic")

        monkeypatch.setattr(roster, "agent_start", _start)
        monkeypatch.setattr(agent_create, "create_agent", _create)
        # An OPEN workspace, so the create row hands the name on to be started.
        # Left unstubbed this probes a real supervisor REST port and reports
        # closed, which is its own case — TestCreatingWithAClosedWorkspace.
        monkeypatch.setattr(agent_create, "_default_workspace_running", _workspace_up)
        return started, captured

    def test_choosing_create_starts_the_newly_created_agent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        started, _ = self._wire(monkeypatch, tmp_path, ["scribe"])
        monkeypatch.setattr(main_mod, "make_prompter", lambda: _PickingPrompter(CREATE_SENTINEL))

        assert main(["agent", "start"]) == 0
        assert started == ["researcher"], "the agent just created is the one that starts"

    def test_the_wizard_is_asked_for_a_brand_new_agent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The three policies that make this a create, not an edit or a first-run.

        ``require_name`` so an enter-through cannot silently target an existing
        agent; ``prune_seed=False`` so adding an agent never deletes the starter
        (only ``init``'s first-run may); ``offer_prompt`` to keep parity with
        ``disco agent create``.
        """
        _, captured = self._wire(monkeypatch, tmp_path, ["scribe"])
        monkeypatch.setattr(main_mod, "make_prompter", lambda: _PickingPrompter(CREATE_SENTINEL))

        main(["agent", "start"])

        assert captured["require_name"] is True
        assert captured["prune_seed"] is False
        assert captured["offer_prompt"] is True

    def test_the_wizard_writes_into_this_installs_paths(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``env_path`` is threaded, not defaulted — the provider step needs it."""
        _, captured = self._wire(monkeypatch, tmp_path, ["scribe"])
        monkeypatch.setattr(main_mod, "make_prompter", lambda: _PickingPrompter(CREATE_SENTINEL))

        main(["agent", "start"])

        expected_env, expected_agents = init.resolve_paths(tmp_path / "home")
        assert captured["agents_dir"] == expected_agents
        assert captured["env_path"] == expected_env

    def test_a_closed_workspace_yields_ordered_steps_and_no_doomed_start(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The whole point of the ordering, end to end through the dispatcher.

        `agent start` needs an open workspace, so starting a just-created agent
        into a closed one is certain to be refused. The operator gets the two
        commands that fix it, in the order they must run them — not step 2 above
        step 1, which is what a hint printed at create time would produce.
        """
        started, _ = self._wire(monkeypatch, tmp_path, ["scribe"])

        async def _down(_home: Path) -> bool:
            return False

        monkeypatch.setattr(agent_create, "_default_workspace_running", _down)
        monkeypatch.setattr(main_mod, "make_prompter", lambda: _PickingPrompter(CREATE_SENTINEL))

        assert main(["agent", "start"]) == 1
        assert started == [], "a start that cannot succeed must not be attempted"
        out = capsys.readouterr().out
        assert out.index("disco start") < out.index("disco agent start researcher")

    @pytest.mark.parametrize("failure", [ValueError("bad model"), OSError("disk full")])
    def test_a_failed_create_starts_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        failure: Exception,
    ) -> None:
        """A create that left no agent on disk must not be followed by a start.

        ``create_agent`` validates before writing, so both failures mean nothing
        usable landed. Starting a name that isn't there would spawn a child that
        dies into a log file with the operator seeing only "exited immediately".
        """
        started, _ = self._wire(monkeypatch, tmp_path, ["scribe"])

        def _boom(prompter, **kwargs):
            raise failure

        monkeypatch.setattr(agent_create, "create_agent", _boom)
        monkeypatch.setattr(main_mod, "make_prompter", lambda: _PickingPrompter(CREATE_SENTINEL))

        assert main(["agent", "start"]) == 1
        assert started == []
        assert "error:" in capsys.readouterr().out


@pytest.mark.parametrize("verb", ["start", "stop", "restart"])
def test_main_agent_roster_name_and_all_are_mutually_exclusive(
    verb: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Passing BOTH a name and --all is contradictory (one targets a single agent,
    # the other every agent on this host) — parser.error (exit 2), and neither the
    # singular nor the bulk roster fn runs.
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))

    def _boom(*args, **kwargs):
        raise AssertionError("no roster fn should run when name and --all collide")

    monkeypatch.setattr(roster, f"agent_{verb}", _boom)
    monkeypatch.setattr(roster, f"agent_{verb}_all", _boom)
    with pytest.raises(SystemExit) as exc:
        main(["agent", verb, "assistant", "--all"])
    assert exc.value.code == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_main_agent_start_dispatches_with_resolved_args(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # `agent start <name>` resolves home from CALFCORD_HOME, reads server_urls
    # from CALF_HOST_URL, asyncio.runs roster.agent_start with the resolved
    # home + name + server_urls, and propagates its exit code.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.setenv("CALF_HOST_URL", "broker.example:9092")

    captured: dict[str, object] = {}

    async def _start(home_arg, *, name, server_urls, **kwargs):
        captured.update(home=home_arg, name=name, server_urls=server_urls)
        return 0

    monkeypatch.setattr(roster, "agent_start", _start)
    assert main(["agent", "start", "assistant"]) == 0
    assert captured == {
        "home": home,
        "name": "assistant",
        "server_urls": "broker.example:9092",
    }


def test_main_agent_start_propagates_nonzero_exit_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))

    async def _start(*args, **kwargs):
        return 1

    monkeypatch.setattr(roster, "agent_start", _start)
    assert main(["agent", "start", "assistant"]) == 1


def test_main_agent_start_defaults_host_url_to_localhost(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # CALF_HOST_URL unset → "localhost" (the same default the runners + lifecycle
    # use), so the broker-wide duplicate probe is still buildable on a dev box.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALF_HOST_URL", raising=False)

    captured: dict[str, object] = {}

    async def _start(home_arg, *, name, server_urls, **kwargs):
        captured["server_urls"] = server_urls
        return 0

    monkeypatch.setattr(roster, "agent_start", _start)
    assert main(["agent", "start", "assistant"]) == 0
    assert captured["server_urls"] == "localhost"


def test_main_agent_start_without_home_errors_native_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Roster ops drive the install-scoped supervisor (derived port under
    # $CALFCORD_HOME), so a dev run with no home refuses with a clear message
    # rather than asyncio.run a half-built invocation against the dev tree.
    monkeypatch.delenv("CALFCORD_HOME", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("roster.agent_start must not run without a home")

    monkeypatch.setattr(roster, "agent_start", _boom)
    assert main(["agent", "start", "assistant"]) == 1
    assert "native install" in capsys.readouterr().out


def test_main_agent_stop_dispatches_with_resolved_args(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # `agent stop <name>` needs no broker probe — just the resolved home + name.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))

    captured: dict[str, object] = {}

    async def _stop(home_arg, *, name, **kwargs):
        captured.update(home=home_arg, name=name)
        return 0

    monkeypatch.setattr(roster, "agent_stop", _stop)
    assert main(["agent", "stop", "assistant"]) == 0
    assert captured == {"home": home, "name": "assistant"}


def test_main_agent_stop_propagates_exit_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))

    async def _stop(*args, **kwargs):
        return 3

    monkeypatch.setattr(roster, "agent_stop", _stop)
    assert main(["agent", "stop", "assistant"]) == 3


def test_main_agent_stop_without_home_errors_native_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CALFCORD_HOME", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("roster.agent_stop must not run without a home")

    monkeypatch.setattr(roster, "agent_stop", _boom)
    assert main(["agent", "stop", "assistant"]) == 1
    assert "native install" in capsys.readouterr().out


def test_main_agent_restart_dispatches_with_resolved_args(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))

    captured: dict[str, object] = {}

    async def _restart(home_arg, *, name, **kwargs):
        captured.update(home=home_arg, name=name)
        return 0

    monkeypatch.setattr(roster, "agent_restart", _restart)
    assert main(["agent", "restart", "assistant"]) == 0
    assert captured == {"home": home, "name": "assistant"}


def test_main_agent_restart_propagates_exit_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))

    async def _restart(*args, **kwargs):
        return 2

    monkeypatch.setattr(roster, "agent_restart", _restart)
    assert main(["agent", "restart", "assistant"]) == 2


def test_main_agent_restart_without_home_errors_native_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CALFCORD_HOME", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("roster.agent_restart must not run without a home")

    monkeypatch.setattr(roster, "agent_restart", _boom)
    assert main(["agent", "restart", "assistant"]) == 1
    assert "native install" in capsys.readouterr().out


# --- roster lifecycle: agent start/stop/restart --all (behavior #1) ---------
#
# `--all` is the uniform-surface bulk verb (decision B), LOCAL-only (this host's
# supervisor). `start --all` targets every DEFINED agent (so main must pass the
# detected .md ids); `stop --all` / `restart --all` target every RUNNING local
# agent (the bulk fn reads the supervisor itself, so main passes no ids).


def test_main_agent_start_all_dispatches_with_defined_agent_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `agent start --all` resolves home + server_urls and passes the DEFINED agent
    # ids (the same detect_agents seam `start`/`agent list` use) to agent_start_all.
    home = tmp_path / "home"
    agents = home / "agents"
    agents.mkdir(parents=True)
    (agents / "assistant.md").write_text("---\nname: assistant\nmodel: gpt-5-nano\n---\nYou are assistant.\n")
    (agents / "scribe.md").write_text("---\nname: scribe\nmodel: gpt-5-nano\n---\nYou are scribe.\n")
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.setenv("CALF_HOST_URL", "broker.example:9092")
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)

    captured: dict[str, object] = {}

    async def _start_all(home_arg, *, agent_ids, server_urls, **kwargs):
        captured.update(home=home_arg, agent_ids=list(agent_ids), server_urls=server_urls)
        return 0

    def _single_boom(*args, **kwargs):
        raise AssertionError("--all must dispatch to agent_start_all, not the singular")

    monkeypatch.setattr(roster, "agent_start_all", _start_all)
    monkeypatch.setattr(roster, "agent_start", _single_boom)
    assert main(["agent", "start", "--all"]) == 0
    assert captured["home"] == home
    assert captured["agent_ids"] == ["assistant", "scribe"]
    assert captured["server_urls"] == "broker.example:9092"


@pytest.mark.parametrize("verb", ["stop", "restart"])
def test_main_agent_stop_restart_all_dispatch_with_home_only(
    verb: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `agent stop --all` / `restart --all` target every RUNNING local agent — the
    # bulk fn reads the supervisor itself, so main passes only the resolved home
    # plus (for the SPAWN verb, restart) the broker URL its gate/probe read —
    # stop needs no broker at all, so no server_urls may leak into it.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.setenv("CALF_HOST_URL", "broker.example:9092")

    captured: dict[str, object] = {}

    async def _all(home_arg, **kwargs):
        captured.update(home=home_arg, kwargs=kwargs)
        return 0

    def _single_boom(*args, **kwargs):
        raise AssertionError(f"--all must dispatch to agent_{verb}_all, not the singular")

    monkeypatch.setattr(roster, f"agent_{verb}_all", _all)
    monkeypatch.setattr(roster, f"agent_{verb}", _single_boom)
    assert main(["agent", verb, "--all"]) == 0
    assert captured["home"] == home
    # No name leaks into the bulk call either way.
    assert "name" not in captured["kwargs"]
    if verb == "stop":
        assert "server_urls" not in captured["kwargs"]
    else:
        assert captured["kwargs"]["server_urls"] == "broker.example:9092"


@pytest.mark.parametrize("verb", ["start", "stop", "restart"])
def test_main_agent_all_propagates_exit_code(verb: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "agents").mkdir(parents=True)
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)

    async def _all(*args, **kwargs):
        return 1

    monkeypatch.setattr(roster, f"agent_{verb}_all", _all)
    assert main(["agent", verb, "--all"]) == 1


@pytest.mark.parametrize("verb", ["start", "stop", "restart"])
def test_main_agent_all_without_home_errors_native_install(
    verb: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # `--all` drives the install-scoped supervisor too, so a dev run with no home
    # refuses with the same native-install steer rather than running a bulk sweep.
    monkeypatch.delenv("CALFCORD_HOME", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError(f"agent_{verb}_all must not run without a home")

    monkeypatch.setattr(roster, f"agent_{verb}_all", _boom)
    assert main(["agent", verb, "--all"]) == 1
    assert "native install" in capsys.readouterr().out


def test_main_agent_ps_dispatches_with_resolved_args(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # `agent ps` takes no name; it resolves home + server_urls and asyncio.runs
    # roster.agent_ps (the running view), distinct from `agent list` (defined).
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.setenv("CALF_HOST_URL", "broker.example:9092")

    captured: dict[str, object] = {}

    async def _ps(home_arg, *, server_urls, **kwargs):
        captured.update(home=home_arg, server_urls=server_urls)
        return 0

    monkeypatch.setattr(roster, "agent_ps", _ps)
    assert main(["agent", "ps"]) == 0
    assert captured == {"home": home, "server_urls": "broker.example:9092"}


def test_main_agent_ps_propagates_exit_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))

    async def _ps(*args, **kwargs):
        return 4

    monkeypatch.setattr(roster, "agent_ps", _ps)
    assert main(["agent", "ps"]) == 4


def test_main_agent_ps_without_home_errors_native_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CALFCORD_HOME", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("roster.agent_ps must not run without a home")

    monkeypatch.setattr(roster, "agent_ps", _boom)
    assert main(["agent", "ps"]) == 1
    assert "native install" in capsys.readouterr().out


def test_main_agent_list_and_ps_are_distinct(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # `agent list` (defined agents) must NOT route to the roster `agent ps`
    # (running agents). They share the `agent` verb group but are different ops.
    _use_dirs(monkeypatch, tmp_path)

    def _run_list(agents_dir: Path, *, as_json: bool) -> int:
        return 0

    def _ps_boom(*args, **kwargs):
        raise AssertionError("`agent list` must not dispatch to roster.agent_ps")

    monkeypatch.setattr(agent_inspect, "run_list", _run_list)
    monkeypatch.setattr(roster, "agent_ps", _ps_boom)
    assert main(["agent", "list"]) == 0


# --- tools lifecycle: start / stop (singleton-component veneers) -------------
#
# `tools` is a SINGLETON roster component: its start/stop are thin veneers over
# the generic component_start/component_stop, dispatched with the component's
# Process Compose slot name. It has NO config surface, so the CLI wiring is the
# entire veneer — these tests pin that the resolved install home and the slot
# name reach component_start/stop and that their exit codes propagate.


@pytest.mark.parametrize("group", ["tools"])
@pytest.mark.parametrize("verb", ["start", "stop"])
def test_main_component_lifecycle_help_exits_zero(group: str, verb: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main([group, verb, "--help"])
    assert exc.value.code == 0


@pytest.mark.parametrize("group", ["tools"])
def test_main_component_requires_subcommand(group: str) -> None:
    # `tools` is a verb group: a bare `disco tools` must error (exit 2),
    # never a silent no-op (so the group can grow further commands later).
    with pytest.raises(SystemExit) as exc:
        main([group])
    assert exc.value.code == 2


@pytest.mark.parametrize("group", ["tools"])
def test_main_component_start_dispatches_with_home_and_slot_name(
    group: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `<group> start` is async and drives the install-scoped supervisor: it passes
    # the $CALFCORD_HOME dir itself (what pc_port_for keys on, identical to agent
    # start/stop and the substrate lifecycle) and the component's slot name. It
    # must NOT consult CALF_HOST_URL (component lifecycle does not probe the
    # broker). Exit code is propagated unchanged.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    monkeypatch.delenv("CALF_HOST_URL", raising=False)
    captured: dict[str, object] = {}

    async def _start(home_arg, *, name, **kwargs):
        captured.update(home=home_arg, name=name)
        return 0

    monkeypatch.setattr(component, "component_start", _start)
    assert main([group, "start"]) == 0
    assert captured["home"] == home
    assert captured["name"] == group


@pytest.mark.parametrize("group", ["tools"])
def test_main_component_stop_dispatches_with_home_and_slot_name(
    group: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `<group> stop` is async and needs only the install home + the slot name; no
    # config check, no broker probe.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    async def _stop(home_arg, *, name, **kwargs):
        captured.update(home=home_arg, name=name)
        return 0

    monkeypatch.setattr(component, "component_stop", _stop)
    assert main([group, "stop"]) == 0
    assert captured["home"] == home
    assert captured["name"] == group


@pytest.mark.parametrize("group", ["tools"])
def test_main_component_start_propagates_exit_code(group: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)

    async def _start(*args, **kwargs):
        return 1

    monkeypatch.setattr(component, "component_start", _start)
    assert main([group, "start"]) == 1


@pytest.mark.parametrize("group", ["tools"])
def test_main_component_stop_propagates_exit_code(group: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)

    async def _stop(*args, **kwargs):
        return 3

    monkeypatch.setattr(component, "component_stop", _stop)
    assert main([group, "stop"]) == 3


@pytest.mark.parametrize("group", ["tools"])
def test_main_component_start_and_stop_pass_the_same_home(
    group: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # pc_port_for keys on the home dir, so start and stop MUST pass the identical
    # home value (the $CALFCORD_HOME root) or they'd talk to different REST ports.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    seen: dict[str, object] = {}

    async def _start(home_arg, *, name, **kwargs):
        seen["start_home"] = home_arg
        return 0

    async def _stop(home_arg, *, name, **kwargs):
        seen["stop_home"] = home_arg
        return 0

    monkeypatch.setattr(component, "component_start", _start)
    monkeypatch.setattr(component, "component_stop", _stop)
    assert main([group, "start"]) == 0
    assert main([group, "stop"]) == 0
    assert seen["start_home"] == seen["stop_home"] == home


@pytest.mark.parametrize("group", ["tools"])
@pytest.mark.parametrize("verb", ["start", "stop"])
def test_main_component_lifecycle_without_home_errors_native_install(
    group: str,
    verb: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Component lifecycle drives the install-scoped supervisor (port derived from
    # $CALFCORD_HOME), so a dev run with no home must refuse with a clear message
    # rather than crash inside os.fspath(None) — mirroring agent/substrate.
    monkeypatch.delenv("CALFCORD_HOME", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError(f"{group} lifecycle must not run without a home")

    monkeypatch.setattr(component, "component_start", _boom)
    monkeypatch.setattr(component, "component_stop", _boom)
    assert main([group, verb]) == 1
    assert "native install" in capsys.readouterr().out


# --- tools restart + --all synonym (behavior #1, uniform) -------------------
#
# The four roster verbs are uniform across agent (multi-instance) and the
# singletons. For a singleton the new `restart` subcommand dispatches through the
# generic component_restart, and `--all` is a documented SYNONYM that just calls
# the singular component fn (there is one instance on this host to act on).


@pytest.mark.parametrize("group", ["tools"])
def test_main_component_restart_help_exits_zero(group: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main([group, "restart", "--help"])
    assert exc.value.code == 0


@pytest.mark.parametrize("group", ["tools"])
def test_main_component_restart_dispatches_with_home_and_slot_name(
    group: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `<group> restart` drives the generic component_restart with the slot name and
    # the $CALFCORD_HOME dir (what pc_port_for keys on), no broker probe.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    monkeypatch.delenv("CALF_HOST_URL", raising=False)
    captured: dict[str, object] = {}

    async def _restart(home_arg, *, name, **kwargs):
        captured.update(home=home_arg, name=name)
        return 0

    monkeypatch.setattr(component, "component_restart", _restart)
    assert main([group, "restart"]) == 0
    assert captured["home"] == home
    assert captured["name"] == group


@pytest.mark.parametrize("group", ["tools"])
def test_main_component_restart_propagates_exit_code(
    group: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)

    async def _restart(*args, **kwargs):
        return 2

    monkeypatch.setattr(component, "component_restart", _restart)
    assert main([group, "restart"]) == 2


@pytest.mark.parametrize("group", ["tools"])
@pytest.mark.parametrize(
    "verb,fn",
    [("start", "component_start"), ("stop", "component_stop"), ("restart", "component_restart")],
)
def test_main_component_all_is_synonym_for_singular(
    group: str, verb: str, fn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # For a one-process-per-host singleton, `--all` is an honest SYNONYM: it just
    # calls the SAME singular component fn with the slot name (it targets the one
    # instance), so `<group> <verb> --all` is indistinguishable from `<group> <verb>`.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    async def _fn(home_arg, *, name, **kwargs):
        captured.update(home=home_arg, name=name)
        return 0

    monkeypatch.setattr(component, fn, _fn)
    assert main([group, verb, "--all"]) == 0
    assert captured["home"] == home
    assert captured["name"] == group


# --- explain: read-only teaching screens (no native-install guard) ----------
#
# `explain` is a verb group whose only topic today is `topology`. It is a PURE
# teaching screen — no supervisor, no broker, no install home — so it dispatches
# without the native-install guard every supervisor-scoped verb carries, and runs
# identically in dev and on a native install.


def test_main_explain_requires_subcommand() -> None:
    # `explain` is a verb group: a bare `disco explain` must error (exit 2),
    # never silently no-op — the required sub-subparser enforces this so the group
    # can grow further topics later.
    with pytest.raises(SystemExit) as exc:
        main(["explain"])
    assert exc.value.code == 2


def test_main_explain_topology_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["explain", "topology", "--help"])
    assert exc.value.code == 0


def test_main_explain_topology_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    # `explain topology` dispatches to explain.run with the topology topic and
    # propagates its exit code. The topic registry (explain.run) is the single
    # source of truth for what can be taught.
    captured: dict[str, object] = {}

    def _run(topic: str) -> int:
        captured["topic"] = topic
        return 0

    monkeypatch.setattr(explain, "run", _run)
    assert main(["explain", "topology"]) == 0
    assert captured["topic"] == "topology"


def test_main_explain_propagates_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(explain, "run", lambda topic: 1)
    assert main(["explain", "topology"]) == 1


def test_main_explain_needs_no_native_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A pure teaching screen must run in a dev tree (no CALFCORD_HOME) WITHOUT the
    # native-install guard the supervisor-scoped verbs carry.
    monkeypatch.delenv("CALFCORD_HOME", raising=False)
    captured: dict[str, object] = {}

    def _run(topic: str) -> int:
        captured["topic"] = topic
        return 0

    monkeypatch.setattr(explain, "run", _run)
    assert main(["explain", "topology"]) == 0
    assert captured["topic"] == "topology"
    assert "native install" not in capsys.readouterr().out


# --- logs: tail unified or per-component supervisor logs ---------------------
#
# `logs [component] [-f]` reads the install's `state/logs/<name>.log` files, so it
# carries the native-install guard (a dev run has no $CALFCORD_HOME state dir).
# main resolves home + agents_dir once and forwards the optional component + the
# follow flag; the file-reading logic lives in the cohesive logs module.


def test_main_logs_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["logs", "--help"])
    assert exc.value.code == 0


def test_main_logs_no_component_dispatches_with_resolved_args(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A bare `disco logs` tails ALL component logs: main resolves the install
    # home from CALFCORD_HOME and the agents dir via init.resolve_paths, then hands
    # logs.tail home + agents_dir with component=None and follow=False.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    def _tail(home_arg: Path, *, agents_dir: Path, component: str | None, follow: bool) -> int:
        captured.update(home=home_arg, agents_dir=agents_dir, component=component, follow=follow)
        return 0

    monkeypatch.setattr(logs, "tail", _tail)
    assert main(["logs"]) == 0
    assert captured["home"] == home
    assert captured["agents_dir"] == home / "agents"
    assert captured["component"] is None
    assert captured["follow"] is False


def test_main_logs_named_component_is_forwarded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    def _tail(home_arg: Path, *, agents_dir: Path, component: str | None, follow: bool) -> int:
        captured.update(component=component, follow=follow)
        return 0

    monkeypatch.setattr(logs, "tail", _tail)
    assert main(["logs", "broker"]) == 0
    assert captured["component"] == "broker"
    assert captured["follow"] is False


def test_main_logs_follow_flag_is_forwarded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Both `-f` and the long `--follow` form set follow=True.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    def _tail(home_arg: Path, *, agents_dir: Path, component: str | None, follow: bool) -> int:
        captured.update(component=component, follow=follow)
        return 0

    monkeypatch.setattr(logs, "tail", _tail)
    assert main(["logs", "bridge", "-f"]) == 0
    assert captured == {"component": "bridge", "follow": True}
    assert main(["logs", "--follow"]) == 0
    assert captured == {"component": None, "follow": True}


def test_main_logs_propagates_exit_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    monkeypatch.setattr(logs, "tail", lambda *a, **k: 1)
    assert main(["logs", "nope"]) == 1


def test_main_logs_without_home_errors_native_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # `logs` reads $CALFCORD_HOME/state/logs/*, so a dev run with no home must
    # refuse with the same actionable native-install message every supervisor-
    # scoped verb uses — rather than reading a nonexistent dev log dir.
    monkeypatch.delenv("CALFCORD_HOME", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("logs.tail must not run without a home")

    monkeypatch.setattr(logs, "tail", _boom)
    assert main(["logs"]) == 1
    assert "native install" in capsys.readouterr().out


# --- deploy: generate graduation manifests ----------------------------------
#
# `deploy <systemd|k8s|docker> [--output PATH]` renders heavier-tier manifests
# from the install's roster + paths. It emits the install shim path (systemd), so
# it carries the native-install guard. main resolves home, env_path, agents_dir
# and server_urls, then forwards target + out_path to the cohesive deploy module.


def test_main_deploy_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["deploy", "--help"])
    assert exc.value.code == 0


def test_main_deploy_requires_target() -> None:
    # `deploy` needs a positional target: a bare `disco deploy` must error
    # (exit 2), never silently act on nothing.
    with pytest.raises(SystemExit) as exc:
        main(["deploy"])
    assert exc.value.code == 2


def test_main_deploy_rejects_unknown_target() -> None:
    # The target is constrained by argparse `choices=`, so a bad target errors at
    # parse time (exit 2) before any handler runs.
    with pytest.raises(SystemExit) as exc:
        main(["deploy", "nope"])
    assert exc.value.code == 2


@pytest.mark.parametrize("target", ["systemd", "k8s", "docker"])
def test_main_deploy_dispatches_with_resolved_args(
    target: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Each target resolves home from CALFCORD_HOME, env_path + agents_dir via
    # init.resolve_paths, server_urls from CALF_HOST_URL, and forwards them (with
    # out_path defaulting to None for stdout) to deploy.run, propagating its code.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.setenv("CALF_HOST_URL", "broker.example:9092")
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    def _run(
        target_arg: str,
        *,
        home: Path,
        env_path: Path,
        agents_dir: Path,
        server_urls: str,
        out_path: Path | None = None,
    ) -> int:
        captured.update(
            target=target_arg,
            home=home,
            env_path=env_path,
            agents_dir=agents_dir,
            server_urls=server_urls,
            out_path=out_path,
        )
        return 0

    monkeypatch.setattr(deploy, "run", _run)
    assert main(["deploy", target]) == 0
    assert captured["target"] == target
    assert captured["home"] == home
    assert captured["env_path"] == home / "config" / ".env"
    assert captured["agents_dir"] == home / "agents"
    assert captured["server_urls"] == "broker.example:9092"
    assert captured["out_path"] is None


def test_main_deploy_output_flag_is_forwarded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # `--output PATH` (and the `-o` short form) writes the manifest to a file
    # instead of stdout: main forwards the resolved Path as out_path.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    def _run(target_arg: str, *, out_path: Path | None = None, **kwargs: object) -> int:
        captured["out_path"] = out_path
        return 0

    monkeypatch.setattr(deploy, "run", _run)
    out_file = tmp_path / "calfcord.service"
    assert main(["deploy", "systemd", "--output", str(out_file)]) == 0
    assert captured["out_path"] == out_file
    assert main(["deploy", "systemd", "-o", str(out_file)]) == 0
    assert captured["out_path"] == out_file


def test_main_deploy_defaults_host_url_to_localhost(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # CALF_HOST_URL unset → "localhost" (the same default the runners + start use).
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALF_HOST_URL", raising=False)
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    def _run(target_arg: str, *, server_urls: str, **kwargs: object) -> int:
        captured["server_urls"] = server_urls
        return 0

    monkeypatch.setattr(deploy, "run", _run)
    assert main(["deploy", "k8s"]) == 0
    assert captured["server_urls"] == "localhost"


def test_main_deploy_propagates_exit_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    monkeypatch.setattr(deploy, "run", lambda *a, **k: 2)
    assert main(["deploy", "systemd"]) == 2


def test_main_deploy_without_home_errors_native_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # deploy emits the install shim path (`<home>/shims/disco`), which has no
    # meaning on a dev run, so a missing CALFCORD_HOME refuses with the actionable
    # native-install message rather than rendering a manifest pointing at nothing.
    monkeypatch.delenv("CALFCORD_HOME", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("deploy.run must not run without a home")

    monkeypatch.setattr(deploy, "run", _boom)
    assert main(["deploy", "systemd"]) == 1
    assert "native install" in capsys.readouterr().out


# --- mcp lifecycle verbs ------------------------------------------------------


def _mcp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    return home


def test_main_mcp_start_dispatches_named_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = _mcp_home(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    async def _start(home_arg, *, server, **kwargs):
        captured.update(home=home_arg, server=server)
        return 0

    monkeypatch.setattr(mcp_roster, "mcp_start", _start)
    assert main(["mcp", "start", "github"]) == 0
    assert captured == {"home": home, "server": "github"}


def test_main_mcp_start_all_passes_configured_servers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # `mcp start --all` enumerates mcp.json via the no-secrets reader and
    # passes the names — the "re-pick up mcp.json" sweep.
    home = _mcp_home(tmp_path, monkeypatch)
    config = home / "config"
    config.mkdir()
    (config / "mcp.json").write_text(
        '{"mcpServers": {"github": {"command": "x"}, "docs": {"type": "http", "url": "https://d"}}}'
    )
    monkeypatch.delenv("CALFCORD_MCP_CONFIG", raising=False)
    captured: dict[str, object] = {}

    async def _start_all(home_arg, *, servers, **kwargs):
        captured.update(home=home_arg, servers=list(servers))
        return 0

    def _single_boom(*args, **kwargs):
        raise AssertionError("--all must dispatch to mcp_start_all, not the singular")

    monkeypatch.setattr(mcp_roster, "mcp_start_all", _start_all)
    monkeypatch.setattr(mcp_roster, "mcp_start", _single_boom)
    assert main(["mcp", "start", "--all"]) == 0
    assert captured == {"home": home, "servers": ["github", "docs"]}


def test_main_mcp_start_all_invalid_config_errors_actionably(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    home = _mcp_home(tmp_path, monkeypatch)
    config = home / "config"
    config.mkdir()
    (config / "mcp.json").write_text("{not json")
    monkeypatch.delenv("CALFCORD_MCP_CONFIG", raising=False)
    assert main(["mcp", "start", "--all"]) == 1
    assert "error" in capsys.readouterr().out.lower()


@pytest.mark.parametrize("verb", ["stop", "restart"])
def test_main_mcp_stop_restart_dispatch_named(verb: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = _mcp_home(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    async def _op(home_arg, *, server, **kwargs):
        captured.update(home=home_arg, server=server)
        return 0

    monkeypatch.setattr(mcp_roster, f"mcp_{verb}", _op)
    assert main(["mcp", verb, "github"]) == 0
    assert captured == {"home": home, "server": "github"}


@pytest.mark.parametrize("verb", ["stop", "restart"])
def test_main_mcp_stop_restart_all_dispatch_home_only(
    verb: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _mcp_home(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    async def _op_all(home_arg, **kwargs):
        captured.update(home=home_arg)
        return 0

    monkeypatch.setattr(mcp_roster, f"mcp_{verb}_all", _op_all)
    assert main(["mcp", verb, "--all"]) == 0
    assert captured == {"home": home}


def test_main_mcp_requires_exactly_one_target(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _mcp_home(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as excinfo:
        main(["mcp", "start"])
    assert excinfo.value.code == 2
    with pytest.raises(SystemExit) as excinfo:
        main(["mcp", "start", "github", "--all"])
    assert excinfo.value.code == 2


def test_main_mcp_without_home_errors_native_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.delenv("CALFCORD_HOME", raising=False)
    assert main(["mcp", "start", "github"]) == 1
    assert "CALFCORD_HOME" in capsys.readouterr().out


def test_main_start_fails_fast_on_broken_mcp_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    # The substrate-only project declares nothing for MCP servers, but `disco
    # start` deliberately KEEPS mcp.json validation as a fail-fast: an invalid
    # config fails the workspace open actionably here, before it would surface
    # later as N doomed `disco mcp start` attempts. The workspace must not open.
    home = tmp_path / "home"
    (home / "agents").mkdir(parents=True)
    config = home / "config"
    config.mkdir()
    (config / "mcp.json").write_text("{not json")
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    monkeypatch.delenv("CALFCORD_MCP_CONFIG", raising=False)

    async def _start(*args, **kwargs):
        raise AssertionError("lifecycle.start must not run on a broken mcp.json")

    monkeypatch.setattr(lifecycle, "start", _start)
    assert main(["start"]) == 1
    assert "error" in capsys.readouterr().out.lower()


def test_main_mcp_add_dispatches_with_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = _mcp_home(tmp_path, monkeypatch)
    monkeypatch.delenv("CALFCORD_MCP_CONFIG", raising=False)
    captured: dict[str, object] = {}

    def _add(prompter, **kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(mcp_admin, "run_add", _add)
    assert (
        main(
            [
                "mcp",
                "add",
                "github",
                "--command",
                "npx -y srv",
                "--env",
                "GITHUB_TOKEN",
                "--force",
                "--start",
            ]
        )
        == 0
    )
    assert captured["server"] == "github"
    assert captured["command"] == "npx -y srv"
    assert captured["env"] == ["GITHUB_TOKEN"]
    assert captured["force"] is True
    assert captured["start"] is True
    assert captured["home"] == home
    assert captured["config_path"] == home / "config" / "mcp.json"


def test_main_mcp_add_works_without_home_dev_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """add/list/remove are config edits — they must work on dev runs (no
    CALFCORD_HOME), targeting ./mcp.json; only the lifecycle verbs need the
    supervisor home."""
    monkeypatch.delenv("CALFCORD_HOME", raising=False)
    monkeypatch.delenv("CALFCORD_MCP_CONFIG", raising=False)
    captured: dict[str, object] = {}

    def _add(prompter, **kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(mcp_admin, "run_add", _add)
    assert main(["mcp", "add", "github", "--command", "srv"]) == 0
    assert captured["home"] is None
    assert captured["config_path"] == Path("mcp.json")


def test_main_mcp_list_dispatches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = _mcp_home(tmp_path, monkeypatch)
    monkeypatch.delenv("CALFCORD_MCP_CONFIG", raising=False)
    captured: dict[str, object] = {}

    def _list(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(mcp_admin, "run_list", _list)
    assert main(["mcp", "list"]) == 0
    assert captured["home"] == home


def test_main_mcp_remove_dispatches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _mcp_home(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    def _remove(prompter, **kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(mcp_admin, "run_remove", _remove)
    assert main(["mcp", "remove", "github", "--force"]) == 0
    assert captured["server"] == "github"
    assert captured["force"] is True


# --- tools alias subcommand --------------------------------------------------


def _fail_if_called(*args: object, **kwargs: object) -> int:
    raise AssertionError("should not have been called")


def _patch_workspace(monkeypatch: pytest.MonkeyPatch, *, up: bool) -> None:
    """Stub the supervisor workspace probe ``_apply_alias_restart`` uses."""
    from calfcord.supervisor import _workspace

    monkeypatch.setattr(_workspace, "resolve_client", lambda client, home: object())

    async def _is_up(client: object) -> bool:
        return up

    monkeypatch.setattr(_workspace, "workspace_is_up", _is_up)


def test_main_tools_alias_add_dispatches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    def _add(*, env_path, src, dst, tool_names, aliasable_names, apply_restart):
        captured.update(
            env_path=env_path,
            src=src,
            dst=dst,
            tool_names=set(tool_names),
            aliasable_names=set(aliasable_names),
            apply_restart=apply_restart,
        )
        return 0

    monkeypatch.setattr(tool_aliases, "run_alias_add", _add)
    assert main(["tools", "alias", "add", "terminal", "terminal_eu"]) == 0
    expected_env, _ = init.resolve_paths(home)
    assert captured["env_path"] == expected_env
    assert captured["src"] == "terminal"
    assert captured["dst"] == "terminal_eu"
    assert captured["apply_restart"] is None  # no --restart → hint, not actuation
    # The canonical surface is computed from ALL_TOOLS: terminal is aliasable,
    # todo (per-session state) is a real tool but NOT aliasable.
    assert "terminal" in captured["tool_names"]
    assert "terminal" in captured["aliasable_names"]
    assert "todo" in captured["tool_names"]
    assert "todo" not in captured["aliasable_names"]


def test_main_tools_alias_list_dispatches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    captured: dict[str, object] = {}

    def _list(*, env_path):
        captured["env_path"] = env_path
        return 0

    monkeypatch.setattr(tool_aliases, "run_alias_list", _list)
    assert main(["tools", "alias", "list"]) == 0
    expected_env, _ = init.resolve_paths(home)
    assert captured["env_path"] == expected_env


def test_main_tools_alias_remove_dispatches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    captured: dict[str, object] = {}

    def _remove(*, env_path, dst, apply_restart):
        captured.update(env_path=env_path, dst=dst, apply_restart=apply_restart)
        return 0

    monkeypatch.setattr(tool_aliases, "run_alias_remove", _remove)
    assert main(["tools", "alias", "remove", "terminal_eu"]) == 0
    assert captured["dst"] == "terminal_eu"
    assert captured["apply_restart"] is None


def test_main_tools_alias_add_restart_injects_callback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))
    captured: dict[str, object] = {}

    def _add(*, env_path, src, dst, tool_names, aliasable_names, apply_restart):
        captured["apply_restart"] = apply_restart
        return 0

    monkeypatch.setattr(tool_aliases, "run_alias_add", _add)
    assert main(["tools", "alias", "add", "terminal", "terminal_eu", "--restart"]) == 0
    # --restart injects the actuation callback (the workspace-gated restart).
    assert callable(captured["apply_restart"])
    assert captured["apply_restart"] is main_mod._apply_alias_restart


def test_main_tools_alias_remove_restart_injects_callback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))
    captured: dict[str, object] = {}

    def _remove(*, env_path, dst, apply_restart):
        captured["apply_restart"] = apply_restart
        return 0

    monkeypatch.setattr(tool_aliases, "run_alias_remove", _remove)
    assert main(["tools", "alias", "remove", "terminal_eu", "--restart"]) == 0
    assert callable(captured["apply_restart"])


class TestApplyAliasRestart:
    """``_apply_alias_restart`` — the ``--restart`` actuation: gated on a
    running workspace, then restart the tools host + running agents."""

    def test_dev_tree_no_supervisor(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.setattr(main_mod, "_resolve_home", lambda: None)
        # _run_component must NOT be called on a dev tree.
        monkeypatch.setattr(main_mod, "_run_component", _fail_if_called)
        main_mod._apply_alias_restart()
        assert "next" in capsys.readouterr().out.lower()

    def test_workspace_down_does_not_restart(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(main_mod, "_resolve_home", lambda: tmp_path)
        _patch_workspace(monkeypatch, up=False)
        monkeypatch.setattr(main_mod, "_run_component", _fail_if_called)
        main_mod._apply_alias_restart()
        assert "workspace not running" in capsys.readouterr().out.lower()

    def test_workspace_up_restarts_tools_and_agents(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(main_mod, "_resolve_home", lambda: tmp_path)
        _patch_workspace(monkeypatch, up=True)
        calls: list[object] = []
        monkeypatch.setattr(
            main_mod,
            "_run_component",
            lambda comp, verb: calls.append((comp, verb)) or 0,
        )

        async def _restart_all(home):
            calls.append(("agents", home))
            return 0

        monkeypatch.setattr(roster, "agent_restart_all", _restart_all)
        main_mod._apply_alias_restart()
        assert ("tools", "restart") in calls
        assert ("agents", tmp_path) in calls


def test_main_tools_start_still_dispatches_to_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The alias branch must not break the start/stop/restart verbs."""
    captured: dict[str, object] = {}

    def _run_component(comp, verb):
        captured.update(comp=comp, verb=verb)
        return 0

    monkeypatch.setattr(main_mod, "_run_component", _run_component)
    assert main(["tools", "start"]) == 0
    assert captured == {"comp": "tools", "verb": "start"}


# --- bridge: `disco bridge restart` ----------------------------------------


def test_main_bridge_requires_subcommand() -> None:
    """A bare ``disco bridge`` prints help and exits non-zero (required verb)."""
    with pytest.raises(SystemExit) as exc:
        main(["bridge"])
    assert exc.value.code != 0


def test_main_bridge_restart_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    """``disco bridge restart`` routes to ``_run_bridge`` with the verb."""
    captured: dict[str, str] = {}

    def _run_bridge(verb):
        captured["verb"] = verb
        return 0

    monkeypatch.setattr(main_mod, "_run_bridge", _run_bridge)
    assert main(["bridge", "restart"]) == 0
    assert captured == {"verb": "restart"}


def test_main_bridge_restart_native_calls_lifecycle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On a native install, ``_run_bridge`` resolves the home and delegates to
    :func:`lifecycle.restart_bridge`, returning its exit code."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    calls: list[object] = []

    async def _restart(passed_home):
        calls.append(passed_home)
        return 0

    monkeypatch.setattr(lifecycle, "restart_bridge", _restart)
    assert main(["bridge", "restart"]) == 0
    assert calls == [home]


def test_main_bridge_restart_propagates_a_nonzero_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-zero from ``lifecycle.restart_bridge`` (e.g. the bridge didn't come back
    ready) flows out through ``_run_bridge`` → ``main`` unchanged — the PR's honest
    exit codes reach the shell."""
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path))

    async def _restart(passed_home):
        return 2

    monkeypatch.setattr(lifecycle, "restart_bridge", _restart)
    assert main(["bridge", "restart"]) == 2


def test_main_bridge_restart_dev_run_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dev run (no CALFCORD_HOME) refuses with the shared native-install message
    and never reaches the supervisor."""
    monkeypatch.delenv("CALFCORD_HOME", raising=False)
    called = False

    async def _restart(passed_home):
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(lifecycle, "restart_bridge", _restart)
    assert main(["bridge", "restart"]) == 1
    assert not called
    assert "needs a native install" in capsys.readouterr().out

"""Tests for the ``calfcord-cli`` argparse entry point and path resolution.

These confirm the entry point is importable and wired (``--help`` exits 0,
the ``init`` subcommand is registered) and that :func:`init.resolve_paths`
honours the native (``$CALFCORD_HOME``) vs dev layouts and the
``CALFKIT_AGENTS_DIR`` override the shim/runners already respect.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from calfcord.cli import agent_create, agent_edit, agent_inspect, agent_lifecycle, doctor, init, router_setup
from calfcord.cli import main as main_mod
from calfcord.cli.main import main
from calfcord.supervisor import lifecycle, roster


def test_main_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_main_init_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["init", "--help"])
    assert exc.value.code == 0


def test_main_requires_subcommand() -> None:
    # No subcommand → argparse errors out (exit 2), never a silent success.
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_main_router_setup_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["router", "setup", "--help"])
    assert exc.value.code == 0


def test_main_router_requires_subcommand() -> None:
    # ``router`` is a verb group: a bare ``calfcord router`` must error (exit 2),
    # never silently no-op — the required sub-subparser enforces this.
    with pytest.raises(SystemExit) as exc:
        main(["router"])
    assert exc.value.code != 0


def test_main_router_setup_dispatches_with_resolved_env_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The shim exports CALFCORD_HOME; main must resolve the install's config/.env
    # via init.resolve_paths and hand exactly that path to router_setup.run.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)

    captured: dict[str, object] = {}

    def _sentinel(prompter: object, *, env_path: Path) -> int:
        captured["env_path"] = env_path
        return 0

    monkeypatch.setattr(router_setup, "run", _sentinel)

    assert main(["router", "setup"]) == 0
    assert captured["env_path"] == home / "config" / ".env"


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
    """Point the resolver at temp agents/state dirs via the env overrides."""
    monkeypatch.setenv("CALFKIT_AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.setenv("CALFKIT_STATE_DIR", str(tmp_path / "state"))
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


def test_main_agent_set_collects_flags_and_provider_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _use_dirs(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def _run_set(agents_dir: Path, name: str, updates: dict[str, str]) -> int:
        captured.update(name=name, updates=updates)
        return 0

    monkeypatch.setattr(agent_lifecycle, "run_set", _run_set)
    rc = main([
        "agent", "set", "scribe",
        "--description", "Has: colon",
        "--thinking-effort", "high",
        "--tools", "read_file,shell",
        "--provider", "openai",
        "--model", "gpt-5-nano",
    ])
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


def test_main_agent_rename_passes_state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _use_dirs(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def _run_rename(agents_dir: Path, state_dir: Path, old: str, new: str) -> int:
        captured.update(agents_dir=agents_dir, state_dir=state_dir, old=old, new=new)
        return 0

    monkeypatch.setattr(agent_lifecycle, "run_rename", _run_rename)
    assert main(["agent", "rename", "scribe", "penny"]) == 0
    assert captured == {
        "agents_dir": tmp_path / "agents",
        "state_dir": tmp_path / "state",
        "old": "scribe",
        "new": "penny",
    }


def test_main_agent_delete_passes_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _use_dirs(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def _run_delete(
        prompter: object, agents_dir: Path, state_dir: Path, name: str, *, yes: bool, keep_state: bool
    ) -> int:
        captured.update(name=name, yes=yes, keep_state=keep_state)
        return 0

    monkeypatch.setattr(agent_lifecycle, "run_delete", _run_delete)
    assert main(["agent", "delete", "scribe", "--yes", "--keep-state"]) == 0
    assert captured == {"name": "scribe", "yes": True, "keep_state": True}


# --- main(): interrupt + raw-mode trapping ---------------------------------


def test_main_traps_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ^C during the interactive dispatch exits 130 with ``aborted.``, not a traceback."""

    def _interrupt(parser: object, args: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(main_mod, "_dispatch", _interrupt)
    assert main(["init"]) == 130
    assert "aborted." in capsys.readouterr().out


def test_main_maps_oserror_to_clean_exit_when_stdin_not_a_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """InquirerPy's raw-mode ``OSError`` (EINVAL) on a non-TTY stdin → exit 1 + a hint."""

    def _raise(parser: object, args: object) -> int:
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(main_mod, "_dispatch", _raise)
    monkeypatch.setattr(main_mod.sys.stdin, "isatty", lambda: False)

    assert main(["init"]) == 1
    assert "interactive terminal" in capsys.readouterr().out


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
    # The shim exports CALFCORD_HOME; doctor must run against the install's config/.env + agents/.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    def _run(*, env_path: Path, agents_dir: Path, offline: bool = False, client_factory: object = None) -> int:
        captured.update(env_path=env_path, agents_dir=agents_dir, offline=offline)
        return 0

    monkeypatch.setattr(doctor, "run", _run)
    assert main(["doctor", "--offline"]) == 0
    assert captured["env_path"] == home / "config" / ".env"
    assert captured["agents_dir"] == home / "agents"
    assert captured["offline"] is True


# --- _healthcheck: hidden readiness-probe subcommand -----------------------


def test_main_healthcheck_broker_exits_with_probe_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_main_healthcheck_broker_unreachable_exits_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CALF_HOST_URL", "localhost:9092")

    async def _unreachable() -> bool:
        return False

    monkeypatch.setattr(main_mod, "default_broker_probe", lambda server_urls: _unreachable)
    assert main(["_healthcheck", "broker"]) == 1


def test_main_healthcheck_defaults_host_url_to_localhost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_main_healthcheck_bridge_reads_heartbeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_main_healthcheck_bridge_missing_beat_exits_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_main_start_dispatches_with_resolved_args(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # ``start`` must resolve home from CALFCORD_HOME, build the launcher as the
    # install's shim, read server_urls from CALF_HOST_URL, and enumerate the
    # agents dir for the roster — then asyncio.run lifecycle.start and propagate
    # its exit code.
    home = tmp_path / "home"
    agents = home / "agents"
    agents.mkdir(parents=True)
    (agents / "assistant.md").write_text(
        "---\nname: assistant\nmodel: gpt-5-nano\n---\nYou are assistant.\n"
    )
    (agents / "scribe.md").write_text(
        "---\nname: scribe\nmodel: gpt-5-nano\n---\nYou are scribe.\n"
    )
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.setenv("CALF_HOST_URL", "broker.example:9092")
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)

    captured: dict[str, object] = {}

    async def _start(home_arg, *, server_urls, launcher, agent_ids, **kwargs):
        captured.update(
            home=home_arg,
            server_urls=server_urls,
            launcher=launcher,
            agent_ids=list(agent_ids),
        )
        return 0

    monkeypatch.setattr(lifecycle, "start", _start)
    assert main(["start"]) == 0
    assert captured["home"] == home
    assert captured["server_urls"] == "broker.example:9092"
    assert captured["launcher"] == str(home / "shims" / "calfcord")
    # Roster is the sorted .md stems (the same seam `agent list` uses).
    assert captured["agent_ids"] == ["assistant", "scribe"]


def test_main_start_propagates_nonzero_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_main_start_defaults_host_url_to_localhost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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
    assert main(["start"]) == 0
    assert captured["server_urls"] == "localhost"


def test_main_stop_dispatches_with_resolved_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_main_stop_propagates_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))

    async def _stop(*args, **kwargs):
        return 3

    monkeypatch.setattr(lifecycle, "stop", _stop)
    assert main(["stop"]) == 3


def test_main_status_dispatches_with_resolved_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


@pytest.mark.parametrize("verb", ["start", "stop", "restart"])
def test_main_agent_roster_requires_name(verb: str) -> None:
    # The name is positional + required: a bare `agent start` (no name) must
    # error (exit 2), never silently act on nothing.
    with pytest.raises(SystemExit) as exc:
        main(["agent", verb])
    assert exc.value.code == 2


def test_main_agent_start_dispatches_with_resolved_args(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_main_agent_start_propagates_nonzero_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))

    async def _start(*args, **kwargs):
        return 1

    monkeypatch.setattr(roster, "agent_start", _start)
    assert main(["agent", "start", "assistant"]) == 1


def test_main_agent_start_defaults_host_url_to_localhost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_main_agent_stop_dispatches_with_resolved_args(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_main_agent_stop_propagates_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_main_agent_restart_dispatches_with_resolved_args(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_main_agent_restart_propagates_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_main_agent_ps_dispatches_with_resolved_args(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_main_agent_ps_propagates_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_main_agent_list_and_ps_are_distinct(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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

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

from calfcord.cli import init, router_setup
from calfcord.cli.main import main


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

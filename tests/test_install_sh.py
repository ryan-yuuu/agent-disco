"""Behavioural tests for the native installer's seeding + shim env wiring.

``scripts/install.sh`` is the no-prerequisites ``curl | bash`` installer. Two
pieces of its logic are easy to get subtly wrong and impossible to unit-test
from Python directly, so we drive the *actual shell* here:

* ``seed_agents`` — must give the native install a stable agents home and
  drop in the starter agent on first install, **without** clobbering an
  operator who removed the starter or added their own agents.
* the generated ``disco`` shim's ``_default_env`` block — must default
  ``CALFKIT_AGENTS_DIR`` under the install home and ``CALFCORD_WORKSPACE_DIR``
  to the *launch* directory, while letting an operator override either of them
  via the shell env or ``config/.env``.

The installer guards ``main "$@"`` so the file can be *sourced* (rather than
executed, which would hit the network), letting these tests call individual
functions in a throwaway ``CALFCORD_HOME``. The shim env behaviour is observed
end-to-end via a fake ``uv`` that simply prints the three env vars it inherits.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

INSTALL_SH = Path(__file__).resolve().parents[1] / "scripts" / "install.sh"

_UNSET = "__UNSET__"

# A stand-in for ``uv`` that ignores its args and reports the two dir env vars
# the shim is responsible for defaulting. ``${VAR-__UNSET__}`` (no colon)
# distinguishes "shim did not export it" (unset) from "exported as empty".
_FAKE_UV = """#!/usr/bin/env bash
printf 'CALFKIT_AGENTS_DIR=%s\\n' "${CALFKIT_AGENTS_DIR-__UNSET__}"
printf 'CALFCORD_WORKSPACE_DIR=%s\\n' "${CALFCORD_WORKSPACE_DIR-__UNSET__}"
"""


def _source_and_run(
    snippet: str, *, home: Path, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """Source ``install.sh`` (main is guarded off) and run ``snippet`` in bash."""
    env = {**os.environ, "CALFCORD_HOME": str(home)}
    if extra_env:
        env.update(extra_env)
    script = f'source "{INSTALL_SH}"\n{snippet}'
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, check=False)


def _make_source_dest(tmp: Path, *, with_assistant: bool = True) -> Path:
    """Build a fake unpacked-source dir (the installer's ``$INSTALLED_DEST``)."""
    dest = tmp / "src"
    (dest / "agents").mkdir(parents=True)
    if with_assistant:
        (dest / "agents" / "assistant.md").write_text("---\nname: assistant\n---\nhi\n")
    return dest


def _install_shims(home: Path) -> None:
    result = _source_and_run("write_shims", home=home)
    assert result.returncode == 0, result.stderr
    assert (home / "shims" / "disco").exists()


def _run_shim(home: Path, *, cwd: Path, env_file: str = "", extra_env: dict[str, str] | None = None) -> dict[str, str]:
    """Invoke the generated ``disco`` shim and capture the env the fake uv saw."""
    (home / "bin").mkdir(parents=True, exist_ok=True)
    uv = home / "bin" / "uv"
    uv.write_text(_FAKE_UV)
    uv.chmod(0o755)
    (home / "current").mkdir(exist_ok=True)
    (home / "config").mkdir(exist_ok=True)
    (home / "config" / ".env").write_text(env_file)

    env = {**os.environ, "CALFCORD_HOME": str(home)}
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [str(home / "shims" / "disco"), "calfkit-agent"],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"shim failed: {result.stderr}"
    parsed: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        parsed[key] = value
    return parsed


# --------------------------------------------------------------- seed_agents ---


def test_seed_agents_seeds_starter_and_agents_dir(tmp_path: Path) -> None:
    home = tmp_path / "home"
    dest = _make_source_dest(tmp_path)
    result = _source_and_run(f'seed_agents "{dest}"', home=home)
    assert result.returncode == 0, result.stderr
    assert (home / "agents" / "assistant.md").read_text().startswith("---")
    # seed_agents pre-creates the runtime's CALFKIT_AGENTS_DIR (.../agents).
    assert (home / "agents").is_dir()


def test_seed_agents_does_not_clobber_existing_agents(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "agents").mkdir(parents=True)
    (home / "agents" / "mine.md").write_text("---\nname: mine\n---\nkeep me\n")
    dest = _make_source_dest(tmp_path)

    result = _source_and_run(f'seed_agents "{dest}"', home=home)
    assert result.returncode == 0, result.stderr
    # Operator's agent untouched, and the starter was NOT injected alongside it.
    assert (home / "agents" / "mine.md").read_text() == "---\nname: mine\n---\nkeep me\n"
    assert not (home / "agents" / "assistant.md").exists()


def test_seed_agents_is_noop_when_source_lacks_starter(tmp_path: Path) -> None:
    home = tmp_path / "home"
    dest = _make_source_dest(tmp_path, with_assistant=False)
    result = _source_and_run(f'seed_agents "{dest}"', home=home)
    assert result.returncode == 0, result.stderr
    assert (home / "agents").is_dir()
    assert list((home / "agents").iterdir()) == []


# ---------------------------------------------------------- shim _default_env ---


def test_shim_defaults_to_home_dirs_and_launch_cwd(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _install_shims(home)
    launch = tmp_path / "workdir"
    launch.mkdir()

    seen = _run_shim(home, cwd=launch)
    assert seen["CALFKIT_AGENTS_DIR"] == str(home / "agents")
    # Workspace follows the directory the command was launched from.
    assert os.path.realpath(seen["CALFCORD_WORKSPACE_DIR"]) == os.path.realpath(str(launch))


def test_shim_defers_to_env_file_override(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _install_shims(home)
    launch = tmp_path / "workdir"
    launch.mkdir()

    # When config/.env pins the workspace, the shim must NOT export $PWD over it
    # (it leaves the value for `uv run --env-file` to apply). The fake uv, which
    # does not read --env-file, therefore sees it unset.
    seen = _run_shim(home, cwd=launch, env_file="CALFCORD_WORKSPACE_DIR=/pinned/ws\n")
    assert seen["CALFCORD_WORKSPACE_DIR"] == _UNSET


def test_shim_empty_env_value_does_not_defeat_default(tmp_path: Path) -> None:
    """A bare ``KEY=`` in config/.env counts as UNSET, so the default still applies.

    ``.env.example`` ships ``CALFCORD_WORKSPACE_DIR=`` (empty); the shim must
    treat that as "not set" and still export the launch dir, otherwise
    ``uv run --env-file`` would inject an empty value and the documented
    "workspace = launch dir" default would never happen on a default install.
    """
    home = tmp_path / "home"
    _install_shims(home)
    launch = tmp_path / "workdir"
    launch.mkdir()

    seen = _run_shim(home, cwd=launch, env_file="CALFCORD_WORKSPACE_DIR=\n")
    assert seen["CALFCORD_WORKSPACE_DIR"] != _UNSET
    assert os.path.realpath(seen["CALFCORD_WORKSPACE_DIR"]) == os.path.realpath(str(launch))


def test_shim_defers_to_nonempty_dotenv_agents_dir(tmp_path: Path) -> None:
    """A NON-empty ``CALFKIT_AGENTS_DIR=`` in config/.env still defers to --env-file.

    The shim must not export its own default over a real pinned value (it leaves
    it for ``uv run --env-file``), so the fake uv — which ignores --env-file —
    sees it unset.
    """
    home = tmp_path / "home"
    _install_shims(home)
    launch = tmp_path / "workdir"
    launch.mkdir()

    seen = _run_shim(home, cwd=launch, env_file="CALFKIT_AGENTS_DIR=/from/dotenv\n")
    assert seen["CALFKIT_AGENTS_DIR"] == _UNSET


def test_shim_defers_to_preset_shell_env(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _install_shims(home)
    launch = tmp_path / "workdir"
    launch.mkdir()

    seen = _run_shim(home, cwd=launch, extra_env={"CALFKIT_AGENTS_DIR": "/preset/agents"})
    assert seen["CALFKIT_AGENTS_DIR"] == "/preset/agents"


# --------------------------------------------------------- shim subcommands ---

# A fake ``uv`` that echoes the arguments it was exec'd with, so we can assert
# how the shim translated the user's command line (e.g. ``init`` becoming
# ``calfcord-cli init``). It strips the leading ``run --frozen ... --`` wrapper
# the shim always adds and prints just the trailing user-program argv.
_FAKE_UV_ECHO_ARGS = """#!/usr/bin/env bash
seen=()
take=0
for a in "$@"; do
  if [ "$take" -eq 1 ]; then seen+=("$a"); fi
  if [ "$a" = "--" ]; then take=1; fi
done
printf 'ARGV=%s\\n' "${seen[*]}"
"""


def _run_shim_argv(home: Path, argv: list[str]) -> str:
    """Invoke the shim with ``argv`` and return the user-program argv the fake uv saw."""
    result = _run_shim_proc(home, argv)
    assert result.returncode == 0, f"shim failed: {result.stderr}"
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key == "ARGV":
            return value
    raise AssertionError(f"fake uv did not report ARGV; stdout was: {result.stdout!r}")


def test_shim_dispatches_init_to_calfcord_cli(tmp_path: Path) -> None:
    """``disco init`` must exec ``calfcord-cli init`` through the same `uv run`."""
    home = tmp_path / "home"
    _install_shims(home)
    assert _run_shim_argv(home, ["init"]) == "calfcord-cli init"


def test_shim_dispatches_agent_to_calfcord_cli(tmp_path: Path) -> None:
    """``disco agent tools`` must exec ``calfcord-cli agent tools`` unchanged."""
    home = tmp_path / "home"
    _install_shims(home)
    assert _run_shim_argv(home, ["agent", "tools"]) == "calfcord-cli agent tools"


def test_shim_passes_runner_commands_through_unchanged(tmp_path: Path) -> None:
    """A non-management command (e.g. a runner) is not rewritten by the dispatch."""
    home = tmp_path / "home"
    _install_shims(home)
    assert _run_shim_argv(home, ["calfkit-bridge"]) == "calfkit-bridge"


def test_shim_run_maps_services_to_runner_scripts(tmp_path: Path) -> None:
    """``disco run <svc>`` is the friendly form of ``disco calfkit-<svc>``."""
    home = tmp_path / "home"
    _install_shims(home)
    assert _run_shim_argv(home, ["run", "bridge"]) == "calfkit-bridge"
    assert _run_shim_argv(home, ["run", "agent", "scribe"]) == "calfkit-agent scribe"
    assert _run_shim_argv(home, ["run", "tools"]) == "calfkit-tools"


def test_shim_auth_maps_to_calfkit_auth(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _install_shims(home)
    assert _run_shim_argv(home, ["auth", "codex", "login"]) == "calfkit-auth codex login"


def test_shim_broker_maps_to_calfcord_broker(tmp_path: Path) -> None:
    """``disco broker`` routes to the ``calfcord-broker`` launcher in the venv,
    forwarding passthrough args. (The bash installer suite additionally asserts
    the broker arm omits ``--env-file`` so the broker never reads config/.env.)"""
    home = tmp_path / "home"
    _install_shims(home)
    assert _run_shim_argv(home, ["broker", "--kafka-cluster-id", "demo"]) == "calfcord-broker --kafka-cluster-id demo"


def test_shim_dispatches_doctor_to_calfcord_cli(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _install_shims(home)
    assert _run_shim_argv(home, ["doctor"]) == "calfcord-cli doctor"


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        # Lifecycle verbs have no console script of their own — they MUST land on
        # the calfcord-cli argparse entry point, with their sub-args forwarded
        # verbatim. If the dispatch ever lets one fall through to the bare uv-run
        # passthrough, the shim would try to exec a nonexistent `logs`/`explain`/
        # `deploy` console script and `disco logs`/`explain`/`deploy` would
        # break — which this guards against.
        (["logs"], "calfcord-cli logs"),
        (["logs", "broker", "-f"], "calfcord-cli logs broker -f"),
        (["explain", "topology"], "calfcord-cli explain topology"),
        (["deploy", "systemd"], "calfcord-cli deploy systemd"),
    ],
)
def test_shim_dispatches_lifecycle_verbs_to_calfcord_cli(tmp_path: Path, argv: list[str], expected: str) -> None:
    """The new lifecycle verbs (logs/explain/deploy) route to calfcord-cli, args intact."""
    home = tmp_path / "home"
    _install_shims(home)
    assert _run_shim_argv(home, argv) == expected


def test_shim_dispatches_bridge_restart_to_calfcord_cli(tmp_path: Path) -> None:
    """``disco bridge restart`` is a management verb and must land on the
    calfcord-cli argparse entry point (``calfcord-cli bridge restart``) — distinct
    from ``disco run bridge`` (the raw ``calfkit-bridge`` runner), which must keep
    working after ``bridge`` joins the management whitelist."""
    home = tmp_path / "home"
    _install_shims(home)
    assert _run_shim_argv(home, ["bridge", "restart"]) == "calfcord-cli bridge restart"
    assert _run_shim_argv(home, ["run", "bridge"]) == "calfkit-bridge"


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        # Regression: adding the lifecycle verbs above must not perturb how the
        # pre-existing verbs route. `run <svc>` still maps to the calfkit-* runner
        # console scripts; the management verbs still land on calfcord-cli.
        (["run", "bridge"], "calfkit-bridge"),
        (["run", "agent", "scribe"], "calfkit-agent scribe"),
        (["init"], "calfcord-cli init"),
        (["doctor"], "calfcord-cli doctor"),
    ],
)
def test_shim_existing_verbs_still_route_after_lifecycle_verbs(tmp_path: Path, argv: list[str], expected: str) -> None:
    """Existing verbs keep their routing once the lifecycle verbs are in the dispatch."""
    home = tmp_path / "home"
    _install_shims(home)
    assert _run_shim_argv(home, argv) == expected


def _run_shim_proc(home: Path, argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Invoke the shim and return the CompletedProcess (for help/error paths that exit before uv)."""
    (home / "bin").mkdir(parents=True, exist_ok=True)
    uv = home / "bin" / "uv"
    uv.write_text(_FAKE_UV_ECHO_ARGS)
    uv.chmod(0o755)
    (home / "current").mkdir(exist_ok=True)
    (home / "config").mkdir(exist_ok=True)
    (home / "config" / ".env").write_text("")
    env = {**os.environ, "CALFCORD_HOME": str(home)}
    return subprocess.run(
        [str(home / "shims" / "disco"), *argv],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("flag", ["--help", "-h", "help"])
def test_shim_help_prints_usage_to_stdout(tmp_path: Path, flag: str) -> None:
    """Explicit help goes to stdout, exits 0, and lists the friendly verbs."""
    home = tmp_path / "home"
    _install_shims(home)
    result = _run_shim_proc(home, [flag])
    assert result.returncode == 0
    assert "usage" in result.stdout.lower()
    for verb in ("run", "doctor", "auth"):
        assert verb in result.stdout


def test_shim_no_args_prints_usage_to_stderr_exit_2(tmp_path: Path) -> None:
    """A bare invocation is an error: usage to stderr, exit 2 (unchanged from before)."""
    home = tmp_path / "home"
    _install_shims(home)
    result = _run_shim_proc(home, [])
    assert result.returncode == 2
    assert "usage" in result.stderr.lower()


@pytest.mark.parametrize("argv", [["run"], ["run", "nope"]])
def test_shim_unknown_subcommand_exits_2(tmp_path: Path, argv: list[str]) -> None:
    home = tmp_path / "home"
    _install_shims(home)
    assert _run_shim_proc(home, argv).returncode == 2


@pytest.mark.parametrize("argv", [["run", "--help"], ["run", "-h"]])
def test_shim_subcommand_help_exits_0(tmp_path: Path, argv: list[str]) -> None:
    home = tmp_path / "home"
    _install_shims(home)
    result = _run_shim_proc(home, argv)
    assert result.returncode == 0
    assert "usage" in result.stdout.lower()


def test_shim_exports_calfcord_home(tmp_path: Path) -> None:
    """The shim must export CALFCORD_HOME so calfcord-cli can locate config + agents."""
    home = tmp_path / "home"
    _install_shims(home)
    shim_text = (home / "shims" / "disco").read_text()
    assert 'export CALFCORD_HOME="$H"' in shim_text


def test_generated_shims_default_to_agent_disco_home(tmp_path: Path) -> None:
    """Unset CALFCORD_HOME defaults to the Agent Disco install dir, not legacy calfcord."""
    home = tmp_path / "home"
    _install_shims(home)
    shim_text = (home / "shims" / "disco").read_text()
    self_text = (home / "shims" / "disco-self").read_text()

    assert 'H="${CALFCORD_HOME:-$HOME/.agent-disco}"' in shim_text
    assert 'H="${CALFCORD_HOME:-$HOME/.agent-disco}"' in self_text
    assert "$HOME/.calfcord" not in shim_text
    assert "$HOME/.calfcord" not in self_text


def test_write_shims_removes_legacy_shims(tmp_path: Path) -> None:
    """Clean cutover: a re-run deletes any pre-rename command shims.

    The command was renamed from the old name to ``disco`` with no compat alias,
    so an install/re-run must leave no stale command on PATH — the legacy shim
    and its self-management sibling are removed if present.
    """
    home = tmp_path / "home"
    shims = home / "shims"
    shims.mkdir(parents=True)
    legacy = shims / "calfcord"
    legacy_self = shims / ("calfcord" + "-self")  # composed to avoid a literal stale ref
    legacy.write_text("#legacy\n")
    legacy_self.write_text("#legacy\n")

    result = _source_and_run("write_shims", home=home)
    assert result.returncode == 0, result.stderr
    # New command lands, legacy command is gone.
    assert (shims / "disco").exists()
    assert (shims / ("disco" + "-self")).exists()
    assert not legacy.exists()
    assert not legacy_self.exists()


@pytest.mark.skipif(not INSTALL_SH.exists(), reason="installer script missing")
def test_install_sh_parses() -> None:
    """The outer script must stay syntactically valid (``bash -n``)."""
    result = subprocess.run(["bash", "-n", str(INSTALL_SH)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


# --------------------------------------------- version lifecycle (source+invoke) ---
# These drive the activate/gc/version-marker machinery the same way the shim
# tests drive seeding: source ``install.sh`` (main guarded off) and call the
# individual functions against a throwaway ``$CALFCORD_HOME``. All offline.


def _make_version(home: Path, sha: str) -> Path:
    """Create a built ``versions/<sha>`` dir (the ``.calfcord-ok`` marker present)."""
    vdir = home / "versions" / sha
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / ".calfcord-ok").write_text("")
    return vdir


def _version_field(home: Path, key: str) -> str:
    """Read one ``KEY=value`` field out of the install's version marker (data, not source)."""
    text = (home / "version").read_text()
    for line in text.splitlines():
        if line.startswith(f"{key}="):
            return line[len(key) + 1 :]
    raise AssertionError(f"{key} not found in version marker:\n{text}")


# ------------------------------------------------------------ activate_version ---


def test_activate_version_first_activation_has_empty_previous(tmp_path: Path) -> None:
    """The very first activation points ``current`` at the dir and records no previous."""
    home = tmp_path / "home"
    aaa = _make_version(home, "aaa")
    result = _source_and_run(f'activate_version "{aaa}"', home=home)
    assert result.returncode == 0, result.stderr

    assert (home / "current").resolve() == aaa.resolve()
    assert _version_field(home, "CALFCORD_COMMIT") == "aaa"
    # No outgoing version on a first install → previous is empty.
    assert _version_field(home, "CALFCORD_PREVIOUS_COMMIT") == ""


def test_activate_version_records_outgoing_as_previous(tmp_path: Path) -> None:
    """A normal A→B update records prev=A and leaves A's dir in place."""
    home = tmp_path / "home"
    aaa = _make_version(home, "aaa")
    bbb = _make_version(home, "bbb")

    result = _source_and_run(f'activate_version "{aaa}"\nactivate_version "{bbb}"', home=home)
    assert result.returncode == 0, result.stderr

    assert (home / "current").resolve() == bbb.resolve()
    assert _version_field(home, "CALFCORD_COMMIT") == "bbb"
    assert _version_field(home, "CALFCORD_PREVIOUS_COMMIT") == "aaa"
    # The predecessor's dir survives so it can serve as a rollback target.
    assert aaa.is_dir()


def test_reactivating_current_sha_preserves_rollback_target(tmp_path: Path) -> None:
    """Re-activating the already-current sha must NOT make it its own predecessor.

    The headline Critical fix: a no-op re-install (or ``self update`` while
    already current, which has no up-to-date short-circuit) re-runs
    ``activate_version`` for the same sha. If it recorded prev == current, the
    following ``gc_versions`` would delete the genuine rollback target. Instead
    the existing previous must be preserved across the re-activation, and the
    real predecessor's dir must survive GC.
    """
    home = tmp_path / "home"
    aaa = _make_version(home, "aaa")
    bbb = home / "versions" / "bbb"

    # Mirror the real install ordering: each version dir is only built right
    # before it's activated (the installer downloads B after A is current), so
    # the GC after each activation can't prune a not-yet-built sibling. ``bbb``
    # is materialised mid-script, just before its first activation.
    script = "\n".join(
        [
            f'activate_version "{aaa}"',  # A first
            'gc_versions aaa "$PREVIOUS_SHA"',
            f'mkdir -p "{bbb}" && : > "{bbb}/.calfcord-ok"',
            f'activate_version "{bbb}"',  # A → B; prev becomes aaa
            'gc_versions bbb "$PREVIOUS_SHA"',
            f'activate_version "{bbb}"',  # re-activate the current sha (the bug)
            'gc_versions bbb "$PREVIOUS_SHA"',
        ]
    )
    result = _source_and_run(script, home=home)
    assert result.returncode == 0, result.stderr

    # Previous still points at the genuine predecessor (aaa), NOT at bbb itself.
    assert _version_field(home, "CALFCORD_PREVIOUS_COMMIT") == "aaa"
    assert _version_field(home, "CALFCORD_COMMIT") == "bbb"
    # The rollback target survived the re-activation + GC.
    assert aaa.is_dir()
    assert bbb.is_dir()


# ----------------------------------------------------------------- gc_versions ---


def test_gc_versions_keeps_current_and_previous_prunes_a_third(tmp_path: Path) -> None:
    home = tmp_path / "home"
    aaa = _make_version(home, "aaa")
    bbb = _make_version(home, "bbb")
    ccc = _make_version(home, "ccc")

    result = _source_and_run("gc_versions ccc bbb", home=home)
    assert result.returncode == 0, result.stderr

    # Current + previous kept; the unrelated third is pruned.
    assert ccc.is_dir()
    assert bbb.is_dir()
    assert not aaa.exists()


def test_gc_versions_prunes_nothing_with_only_cur_and_prev(tmp_path: Path) -> None:
    home = tmp_path / "home"
    aaa = _make_version(home, "aaa")
    bbb = _make_version(home, "bbb")

    result = _source_and_run("gc_versions bbb aaa", home=home)
    assert result.returncode == 0, result.stderr

    assert aaa.is_dir()
    assert bbb.is_dir()


# -------------------------------------------------------- disco-self rollback ---


def _run_self(home: Path, argv: list[str]) -> subprocess.CompletedProcess:
    """Invoke the generated ``disco-self`` shim against ``$CALFCORD_HOME=home``."""
    _install_shims(home)
    env = {**os.environ, "CALFCORD_HOME": str(home)}
    return subprocess.run(
        [str(home / "shims" / ("disco" + "-self")), *argv],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_self_rollback_flips_current_and_swaps_version_fields(tmp_path: Path) -> None:
    """After A→B, ``rollback`` points current at A and swaps the version fields."""
    home = tmp_path / "home"
    aaa = _make_version(home, "aaa")
    bbb = _make_version(home, "bbb")
    # Reach the post-update state (current=B, prev=A) via the real activate path.
    prep = _source_and_run(f'activate_version "{aaa}"\nactivate_version "{bbb}"', home=home)
    assert prep.returncode == 0, prep.stderr

    result = _run_self(home, ["rollback"])
    assert result.returncode == 0, result.stderr

    # current now points at A, and the marker swapped: commit=A, previous=B.
    assert (home / "current").resolve() == aaa.resolve()
    assert _version_field(home, "CALFCORD_COMMIT") == "aaa"
    assert _version_field(home, "CALFCORD_PREVIOUS_COMMIT") == "bbb"


def test_self_rollback_refuses_when_previous_lacks_ok_marker(tmp_path: Path) -> None:
    """``rollback`` refuses (exit 1) when the previous version dir is not built."""
    home = tmp_path / "home"
    aaa = _make_version(home, "aaa")
    bbb = _make_version(home, "bbb")
    prep = _source_and_run(f'activate_version "{aaa}"\nactivate_version "{bbb}"', home=home)
    assert prep.returncode == 0, prep.stderr
    # Remove the predecessor's build marker so it's no longer a valid target.
    (aaa / ".calfcord-ok").unlink()

    result = _run_self(home, ["rollback"])
    assert result.returncode == 1
    assert "no valid previous version" in result.stderr
    # current is untouched — still B.
    assert (home / "current").resolve() == bbb.resolve()


# ------------------------------------------------------ disco-self set-broker ---


def _read_config_env(home: Path) -> str:
    return (home / "config" / ".env").read_text()


def test_self_set_broker_writes_value_at_mode_600(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = _run_self(home, ["set-broker", "broker.example.com:9092"])
    assert result.returncode == 0, result.stderr

    env_file = home / "config" / ".env"
    assert "CALF_HOST_URL=broker.example.com:9092" in env_file.read_text()
    assert (env_file.stat().st_mode & 0o777) == 0o600


def test_self_set_broker_replaces_not_appends_and_keeps_other_keys(tmp_path: Path) -> None:
    """A second set-broker REPLACES the line (single occurrence) and keeps unrelated keys."""
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    (home / "config" / ".env").write_text("DISCORD_BOT_TOKEN=keepme\nCALF_HOST_URL=old:9092\n")

    result = _run_self(home, ["set-broker", "new-broker:9092"])
    assert result.returncode == 0, result.stderr

    text = _read_config_env(home)
    # Exactly one CALF_HOST_URL line, carrying the new value.
    broker_lines = [ln for ln in text.splitlines() if ln.startswith("CALF_HOST_URL=")]
    assert broker_lines == ["CALF_HOST_URL=new-broker:9092"]
    # The unrelated key is preserved.
    assert "DISCORD_BOT_TOKEN=keepme" in text


# -------------------------------------------------------------- seed_config ---


def test_seed_config_keeps_existing_env_untouched(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    existing = "OPERATOR=edits\n"
    (home / "config" / ".env").write_text(existing)
    dest = tmp_path / "src"
    dest.mkdir()
    (dest / ".env.example").write_text("EXAMPLE=value\n")

    result = _source_and_run(f'seed_config "{dest}"', home=home)
    assert result.returncode == 0, result.stderr
    # An operator's existing config is never clobbered by the seed.
    assert _read_config_env(home) == existing


def test_seed_config_creates_new_env_at_mode_600(tmp_path: Path) -> None:
    home = tmp_path / "home"
    dest = tmp_path / "src"
    dest.mkdir()
    (dest / ".env.example").write_text("EXAMPLE=value\n")

    result = _source_and_run(f'seed_config "{dest}"', home=home)
    assert result.returncode == 0, result.stderr

    env_file = home / "config" / ".env"
    assert env_file.read_text() == "EXAMPLE=value\n"
    assert (env_file.stat().st_mode & 0o777) == 0o600


# --------------------------------------------------------------- ensure_path ---
# ``ensure_path`` follows the rustup/uv env-file pattern: it writes ONE canonical
# sh-compatible activation file at ``$CALFCORD_HOME/env`` (which prepends the shim
# dir to PATH via an idempotent ``case`` guard) and sources it from each login
# shell's profile with a single hook line, **creating profiles that don't exist**.
# The headline fix: a fresh account with no dotfiles still ends up with ``disco``
# on PATH after a shell restart, where the old "append only to already-existing rc
# files" approach silently wrote nothing. These tests point ``$HOME`` at a temp
# dir so the developer's real profiles are never touched.

_PROFILES = (".profile", ".bashrc", ".zshenv")


def _run_ensure_path(home: Path, fake_home: Path, *, path: str | None = None) -> subprocess.CompletedProcess:
    """Run ``ensure_path`` with ``$HOME`` redirected to ``fake_home`` (and an
    optional ``$PATH`` override to exercise the already-on-PATH short-circuit).

    ``$PATH`` defaults to a hermetic ``/usr/bin:/bin`` so ``link_onto_path``'s
    real candidates (``$HOME/.local/bin``, ``/usr/local/bin``) are never on it:
    without this a CI runner with a writable ``/usr/local/bin`` (macOS images
    ship one) would have a real ``disco`` symlink planted in it by the suite.
    """
    extra = {"HOME": str(fake_home), "PATH": "/usr/bin:/bin"}
    if path is not None:
        extra["PATH"] = path
    return _source_and_run("ensure_path", home=home, extra_env=extra)


def test_ensure_path_creates_missing_profiles_with_hook(tmp_path: Path) -> None:
    """A fresh account with NO rc files gets .profile/.bashrc/.zprofile created,
    each sourcing the env file. This is the core quickstart-breaking bug fix."""
    home = tmp_path / "home"
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()

    result = _run_ensure_path(home, fake_home)
    assert result.returncode == 0, result.stderr

    hook = f'. "{home}/env"'
    for name in _PROFILES:
        rc = fake_home / name
        assert rc.exists(), f"{name} was not created"
        assert hook in rc.read_text()


def test_ensure_path_writes_env_file_with_case_guard(tmp_path: Path) -> None:
    """The canonical env file prepends the shim dir to PATH under a ``case`` guard
    (so it is a no-op when the dir is already present) and keeps ``$PATH`` literal
    for load-time expansion rather than expanding it at write time."""
    home = tmp_path / "home"
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()

    result = _run_ensure_path(home, fake_home)
    assert result.returncode == 0, result.stderr

    env_file = home / "env"
    assert env_file.exists()
    text = env_file.read_text()
    shim_dir = str(home / "shims")
    assert f'export PATH="{shim_dir}:$PATH"' in text
    assert "case" in text  # the idempotency guard
    assert "$PATH" in text  # literal, expanded at profile-load time


@pytest.mark.parametrize("shell", ["sh", "bash", "zsh"])
def test_env_file_is_idempotent_when_sourced(tmp_path: Path, shell: str) -> None:
    """Sourcing the env file repeatedly must prepend the shim dir exactly once,
    under every login shell the profiles hook it into (POSIX sh, bash, zsh)."""
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} not available")
    home = tmp_path / "home"
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    _run_ensure_path(home, fake_home)

    env_file = home / "env"
    shim_dir = str(home / "shims")
    script = f'export PATH=/usr/bin:/bin\n. "{env_file}"\n. "{env_file}"\nprintf %s "$PATH"'
    result = subprocess.run([shell, "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split(":").count(shim_dir) == 1


def test_ensure_path_is_idempotent_across_reruns(tmp_path: Path) -> None:
    """A second ``ensure_path`` must not re-append the hook line to any profile."""
    home = tmp_path / "home"
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()

    first = _run_ensure_path(home, fake_home)
    assert first.returncode == 0, first.stderr
    second = _run_ensure_path(home, fake_home)
    assert second.returncode == 0, second.stderr

    hook = f'. "{home}/env"'
    for name in _PROFILES:
        assert (fake_home / name).read_text().count(hook) == 1


def test_ensure_path_skips_when_shim_already_on_path(tmp_path: Path) -> None:
    """When the shim dir is already on PATH (an active hook, or a hand-wired
    PATH), ensure_path is a complete no-op: no env file, no profiles created."""
    home = tmp_path / "home"
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    shim_dir = str(home / "shims")

    result = _run_ensure_path(home, fake_home, path=f"{shim_dir}:/usr/bin:/bin")
    assert result.returncode == 0, result.stderr

    assert not (home / "env").exists()
    for name in _PROFILES:
        assert not (fake_home / name).exists()


def test_ensure_path_appends_without_clobbering_existing_profiles(tmp_path: Path) -> None:
    """Existing profiles keep their content; the hook is appended, not clobbered."""
    home = tmp_path / "home"
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    for name in _PROFILES:
        (fake_home / name).write_text(f"# existing {name}\nexport FOO=bar\n")

    result = _run_ensure_path(home, fake_home)
    assert result.returncode == 0, result.stderr

    hook = f'. "{home}/env"'
    for name in _PROFILES:
        text = (fake_home / name).read_text()
        assert f"# existing {name}" in text
        assert "export FOO=bar" in text
        assert hook in text


def test_ensure_path_leaves_legacy_export_line_and_adds_hook(tmp_path: Path) -> None:
    """Migration niceness: an old direct ``export PATH=...shims...`` line from a
    prior install is left untouched (harmless + idempotent) and the new hook is
    appended alongside it rather than rewriting the operator's dotfile."""
    home = tmp_path / "home"
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    shim_dir = str(home / "shims")
    legacy = f'export PATH="{shim_dir}:$PATH"'
    profile = fake_home / ".profile"
    profile.write_text(f"# disco\n{legacy}\n")

    result = _run_ensure_path(home, fake_home)
    assert result.returncode == 0, result.stderr

    text = profile.read_text()
    assert legacy in text  # old line untouched
    assert f'. "{home}/env"' in text  # new hook added


def test_ensure_path_wires_zsh_via_zshenv_not_zprofile(tmp_path: Path) -> None:
    """zsh reads ``.zprofile`` ONLY for login shells, so the hook was invisible
    in a non-login interactive zsh — VS Code's integrated terminal spawns
    ``/bin/zsh -i``, and restarting it never helped. ``.zshenv`` is the only zsh
    startup file read unconditionally (login, interactive, and scripts alike),
    so it is the one target that always gets us on PATH. rustup picks it for the
    same reason. It is not free: ``.zshenv`` runs before /etc/zprofile's
    path_helper, which reorders PATH and costs us the prepend ``.zprofile`` got
    — a precedence loss in exchange for a reachability win."""
    home = tmp_path / "home"
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()

    result = _run_ensure_path(home, fake_home)
    assert result.returncode == 0, result.stderr

    hook = f'. "{home}/env"'
    assert hook in (fake_home / ".zshenv").read_text()
    assert not (fake_home / ".zprofile").exists()


def test_ensure_path_writes_zshenv_under_zdotdir(tmp_path: Path) -> None:
    """When ZDOTDIR is set, zsh reads ``$ZDOTDIR/.zshenv`` and NEVER looks at
    ``~/.zshenv`` — so writing $HOME would be dead code for those users. ZDOTDIR
    is also typically not exported, so the installer (a bash child) cannot read
    it from its own env; it must ask zsh."""
    if shutil.which("zsh") is None:
        pytest.skip("zsh not available")
    home = tmp_path / "home"
    fake_home = tmp_path / "fakehome"
    zdotdir = tmp_path / "zdotdir"
    fake_home.mkdir()
    zdotdir.mkdir()

    result = _source_and_run(
        "ensure_path",
        home=home,
        extra_env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin", "ZDOTDIR": str(zdotdir)},
    )
    assert result.returncode == 0, result.stderr

    hook = f'. "{home}/env"'
    assert hook in (zdotdir / ".zshenv").read_text()
    assert not (fake_home / ".zshenv").exists()


def test_ensure_path_survives_an_unwritable_rc_file(tmp_path: Path) -> None:
    """A read-only startup file must not abort the install.

    The installer runs under `set -Eeuo pipefail` with an ERR trap, and the
    append sits in an `if` BODY — only the condition is exempt from `set -e`.
    So a 444 `~/.zshenv` (nix home-manager, chezmoi, stow all produce one) made
    `printf >>` fail, fired the trap, and killed the run with "install failed"
    — AFTER the symlink had already been planted and `disco` genuinely worked.
    The other files must still get their hook.
    """
    if os.geteuid() == 0:
        pytest.skip("root bypasses write permission checks")
    home = tmp_path / "home"
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    locked = fake_home / ".zshenv"
    locked.write_text("# managed elsewhere, read-only\n")
    locked.chmod(0o444)
    try:
        result = _run_ensure_path(home, fake_home)
        assert result.returncode == 0, result.stderr

        # The install survived and said something useful about the file it skipped.
        assert "zshenv" in result.stderr
        # The writable ones are still wired.
        hook = f'. "{home}/env"'
        assert hook in (fake_home / ".profile").read_text()
        assert hook in (fake_home / ".bashrc").read_text()
        # And the activation file — the one `source` needs — exists regardless.
        assert (home / "env").exists()
    finally:
        locked.chmod(0o644)


def test_ensure_path_symlinks_and_still_wires_profiles(tmp_path: Path) -> None:
    """The symlink covers the CURRENT shell; the profile hooks cover every
    future one. They are not alternatives — a user who removes the symlink, or
    whose PATH is rebuilt, still needs the hook. Both always run."""
    home = tmp_path / "home"
    (home / "shims").mkdir(parents=True)
    shim = home / "shims" / "disco"
    shim.write_text("#!/usr/bin/env bash\n")
    shim.chmod(0o755)  # link_onto_path verifies the link resolves to a RUNNABLE disco
    fake_home = tmp_path / "fakehome"
    local_bin = fake_home / ".local" / "bin"
    local_bin.mkdir(parents=True)

    result = _source_and_run(
        'ensure_path; printf "SYMLINK_CREATED=%s" "$SYMLINK_CREATED"',
        home=home,
        extra_env={"HOME": str(fake_home), "PATH": f"{local_bin}:/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr

    # current shell: reachable now
    assert (local_bin / "disco").is_symlink()
    assert f"SYMLINK_CREATED={local_bin}" in result.stdout
    # future shells: still wired
    assert (home / "env").exists()
    for name in _PROFILES:
        assert f'. "{home}/env"' in (fake_home / name).read_text()


# ------------------------------------------------------------ link_onto_path ---
# A `curl | bash` installer is a CHILD of the caller's shell, so it can never
# mutate the caller's PATH — which is why the env-file/profile hooks above only
# take effect in a NEW shell. The one escape is to land the command in a
# directory the running shell ALREADY searches: PATH is resolved at lookup time,
# so a new file in an already-listed dir is found with no env change and no
# rehash. That makes `disco` usable in the CURRENT terminal, with the profile
# hooks as the fallback for machines where no candidate qualifies.


def _run_link_onto_path(home: Path, candidates: list[Path], *, path: str) -> subprocess.CompletedProcess:
    """Run ``link_onto_path`` over ``candidates`` with an explicit ``$PATH``."""
    args = " ".join(f'"{c}"' for c in candidates)
    return _source_and_run(
        f'link_onto_path {args}; printf "SYMLINK_CREATED=%s" "$SYMLINK_CREATED"',
        home=home,
        extra_env={"PATH": path},
    )


def _shim_home(tmp_path: Path) -> tuple[Path, Path]:
    """Build a home with a real ``shims/disco`` in it; return (home, shim_dir).

    The shim is made executable because ``link_onto_path`` verifies the link it
    just made actually resolves to a runnable command before claiming READY.
    """
    home = tmp_path / "home"
    shim_dir = home / "shims"
    shim_dir.mkdir(parents=True)
    shim = shim_dir / "disco"
    shim.write_text("#!/usr/bin/env bash\n")
    shim.chmod(0o755)
    return home, shim_dir


def test_link_onto_path_does_not_claim_ready_when_shadowed(tmp_path: Path) -> None:
    """READY must mean "typing `disco` runs ours", not "we called ln".

    Candidates are tried in preference order, but the SHELL resolves in PATH
    order. With another tool's `disco` earlier on PATH, linking into a later dir
    leaves the foreign one winning — claiming READY there would silently point
    `disco init` at someone else's program.
    """
    home, _ = _shim_home(tmp_path)
    foreign_dir = tmp_path / "foreign"
    foreign_dir.mkdir()
    foreign = foreign_dir / "disco"
    foreign.write_text("#!/bin/sh\necho foreign\n")
    foreign.chmod(0o755)
    cand = tmp_path / "bin"
    cand.mkdir()

    # foreign_dir comes FIRST on PATH; cand is our (later) candidate.
    result = _run_link_onto_path(home, [cand], path=f"{foreign_dir}:{cand}:/usr/bin:/bin")
    assert result.returncode == 0, result.stderr

    assert "SYMLINK_CREATED=" in result.stdout
    assert str(cand) not in result.stdout


def test_link_onto_path_warns_when_another_disco_shadows_ours(tmp_path: Path) -> None:
    """Being shadowed is the one case NEITHER message can fix, so it must be
    said out loud.

    Declining READY is necessary but not sufficient: the ACTIVATE fallback tells
    the operator to source `env`, which prepends SHIM_DIR — but on a macOS login
    shell /etc/zprofile's path_helper(8) then reorders PATH and demotes SHIM_DIR
    to last, so a foreign `disco` in /usr/local/bin keeps winning even after the
    restart. Silently printing a fallback that cannot work is worse than saying
    what is actually in the way.
    """
    home, _ = _shim_home(tmp_path)
    foreign_dir = tmp_path / "foreign"
    foreign_dir.mkdir()
    foreign = foreign_dir / "disco"
    foreign.write_text("#!/bin/sh\necho foreign\n")
    foreign.chmod(0o755)
    cand = tmp_path / "bin"
    cand.mkdir()

    result = _run_link_onto_path(home, [cand], path=f"{foreign_dir}:{cand}:/usr/bin:/bin")
    assert result.returncode == 0, result.stderr

    assert str(foreign) in result.stderr  # names what is in the way
    assert "SYMLINK_CREATED=" in result.stdout
    assert str(cand) not in result.stdout


def test_link_onto_path_removes_a_link_that_does_not_resolve(tmp_path: Path) -> None:
    """A correctly-SHAPED link can still be non-functional — a relative
    CALFCORD_HOME makes the target resolve against the LINK's directory, not
    ours. Claiming READY there yields `command not found` with no instructions,
    so the link is torn back out rather than left as garbage on PATH."""
    home = tmp_path / "home"
    shim_dir = home / "shims"
    shim_dir.mkdir(parents=True)
    (shim_dir / "disco").write_text("#!/usr/bin/env bash\n")  # deliberately NOT executable
    cand = tmp_path / "bin"
    cand.mkdir()

    result = _run_link_onto_path(home, [cand], path=f"{cand}:/usr/bin:/bin")
    assert result.returncode == 0, result.stderr

    assert not (cand / "disco").exists()
    assert not (cand / "disco").is_symlink()  # cleaned up, not left dangling
    assert "SYMLINK_CREATED=" in result.stdout
    assert str(cand) not in result.stdout


def test_link_onto_path_creates_a_missing_candidate_already_on_path(tmp_path: Path) -> None:
    """A dir on PATH but absent is still searched by the shell — Fedora/RHEL put
    ~/.local/bin on PATH unconditionally. Creating it honours PATH rather than
    inventing policy, and turns a needless ACTIVATE into a READY."""
    home, _ = _shim_home(tmp_path)
    cand = tmp_path / "not-yet"  # deliberately not created

    result = _run_link_onto_path(home, [cand], path=f"{cand}:/usr/bin:/bin")
    assert result.returncode == 0, result.stderr

    assert (cand / "disco").is_symlink()
    assert f"SYMLINK_CREATED={cand}" in result.stdout


def test_link_onto_path_skips_a_dangling_foreign_symlink(tmp_path: Path) -> None:
    """A broken link is still someone else's `disco` if it doesn't point at our
    shim. ``-e`` is false for a dangling link, so the ownership guard has to test
    ``-L`` too or it would silently replace another tool's link."""
    home, _ = _shim_home(tmp_path)
    cand = tmp_path / "bin"
    cand.mkdir()
    gone = tmp_path / "someone-elses" / "disco"
    (cand / "disco").symlink_to(gone)
    assert not (cand / "disco").exists()  # dangling
    assert (cand / "disco").is_symlink()

    result = _run_link_onto_path(home, [cand], path=f"{cand}:/usr/bin:/bin")
    assert result.returncode == 0, result.stderr

    assert os.readlink(cand / "disco") == str(gone)  # untouched
    assert "SYMLINK_CREATED=" in result.stdout
    assert str(cand) not in result.stdout


# ------------------------------------------------------------- zsh_dotdir ---


def test_zsh_dotdir_returns_home_when_zshenv_itself_sets_zdotdir(tmp_path: Path) -> None:
    """The XDG idiom — a ``~/.zshenv`` whose only job is to point ZDOTDIR
    elsewhere — is the case the probe must not get wrong.

    zsh locates ``.zshenv`` from /etc/zshenv or the inherited env ONLY, then
    reads it once. So a ``~/.zshenv`` that sets ZDOTDIR is still the only
    ``.zshenv`` zsh ever reads. Probing with a plain ``zsh -c`` sources that file
    first and reports the ZDOTDIR it sets, which would send our hook to a
    ``$ZDOTDIR/.zshenv`` zsh has already finished looking for — a dead file, and
    a permanently broken ACTIVATE. ``zsh -f`` reproduces zsh's own lookup.
    """
    if shutil.which("zsh") is None:
        pytest.skip("zsh not available")
    home = tmp_path / "home"
    fake_home = tmp_path / "fakehome"
    (fake_home / ".config" / "zsh").mkdir(parents=True)
    (fake_home / ".zshenv").write_text('export ZDOTDIR="$HOME/.config/zsh"\n')

    result = _source_and_run(
        'printf "%s" "$(zsh_dotdir)"',
        home=home,
        extra_env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == str(fake_home)


def test_zsh_dotdir_honours_an_exported_zdotdir(tmp_path: Path) -> None:
    """An exported ZDOTDIR is inherited by the probe and must win."""
    if shutil.which("zsh") is None:
        pytest.skip("zsh not available")
    home = tmp_path / "home"
    fake_home = tmp_path / "fakehome"
    zdotdir = tmp_path / "zdotdir"
    fake_home.mkdir()
    zdotdir.mkdir()

    result = _source_and_run(
        'printf "%s" "$(zsh_dotdir)"',
        home=home,
        extra_env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin", "ZDOTDIR": str(zdotdir)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == str(zdotdir)


# ------------------------------------------------------- activation_hint ---


def _run_activation_hint(home: Path, *, symlinked: str = "", wired: int = 0) -> str:
    result = _source_and_run(
        f'SYMLINK_CREATED="{symlinked}"; PATH_WIRED={wired}; activation_hint',
        home=home,
        extra_env={"PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    return result.stderr


def test_activation_hint_says_ready_when_disco_is_reachable_now(tmp_path: Path) -> None:
    """The whole point of the symlink tier: when `disco` already works, telling
    the operator to restart their terminal is noise, and untrue."""
    out = _run_activation_hint(tmp_path / "home", symlinked="/somewhere/bin", wired=1)
    assert "READY" in out
    assert "ACTIVATE" not in out
    assert "source" not in out


def test_activation_hint_says_activate_only_when_it_had_to(tmp_path: Path) -> None:
    """When nothing qualified, the operator genuinely must act — and gets the
    one command that works in the shell they are already in."""
    out = _run_activation_hint(tmp_path / "home", symlinked="", wired=1)
    assert "ACTIVATE" in out
    assert "source" in out
    assert "READY" not in out


def test_activation_hint_is_silent_when_nothing_was_needed(tmp_path: Path) -> None:
    """An install whose shim dir was already on PATH needs no banner at all."""
    assert _run_activation_hint(tmp_path / "home", symlinked="", wired=0).strip() == ""


def test_link_onto_path_symlinks_into_writable_dir_already_on_path(tmp_path: Path) -> None:
    """The headline behaviour: a candidate that is on PATH, is a dir, and is
    writable gets a ``disco`` symlink — so the command works with no restart."""
    home, shim_dir = _shim_home(tmp_path)
    cand = tmp_path / "bin"
    cand.mkdir()

    result = _run_link_onto_path(home, [cand], path=f"{cand}:/usr/bin:/bin")
    assert result.returncode == 0, result.stderr

    link = cand / "disco"
    assert link.is_symlink()
    assert os.readlink(link) == str(shim_dir / "disco")
    assert f"SYMLINK_CREATED={cand}" in result.stdout


def test_link_onto_path_skips_candidate_not_on_path(tmp_path: Path) -> None:
    """Writable and a real dir is NOT enough — a dir the shell does not search
    buys nothing, so linking there would be a lie about immediate availability."""
    home, _ = _shim_home(tmp_path)
    cand = tmp_path / "bin"
    cand.mkdir()

    result = _run_link_onto_path(home, [cand], path="/usr/bin:/bin")
    assert result.returncode == 0, result.stderr

    assert not (cand / "disco").exists()
    assert "SYMLINK_CREATED=" in result.stdout
    assert str(cand) not in result.stdout


def test_link_onto_path_skips_unwritable_candidate(tmp_path: Path) -> None:
    """An on-PATH dir we cannot write (a root-owned /usr/local/bin on a stock
    Mac) is skipped rather than escalating — a piped installer has no stdin to
    prompt for sudo on."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses write permission checks")
    home, _ = _shim_home(tmp_path)
    cand = tmp_path / "bin"
    cand.mkdir()
    cand.chmod(0o555)
    try:
        result = _run_link_onto_path(home, [cand], path=f"{cand}:/usr/bin:/bin")
        assert result.returncode == 0, result.stderr
        assert not (cand / "disco").exists()
        assert "SYMLINK_CREATED=" in result.stdout
        assert str(cand) not in result.stdout
    finally:
        cand.chmod(0o755)


def test_link_onto_path_stops_at_first_qualifying_candidate(tmp_path: Path) -> None:
    """Candidates are a preference order, not a fan-out: the first hit wins and
    no second copy is planted."""
    home, _ = _shim_home(tmp_path)
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()

    result = _run_link_onto_path(home, [first, second], path=f"{first}:{second}:/usr/bin:/bin")
    assert result.returncode == 0, result.stderr

    assert (first / "disco").is_symlink()
    assert not (second / "disco").exists()
    assert f"SYMLINK_CREATED={first}" in result.stdout


def test_link_onto_path_falls_through_to_later_candidate(tmp_path: Path) -> None:
    """When the preferred candidate does not qualify, a later one still can."""
    home, _ = _shim_home(tmp_path)
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()

    # Only `second` is on PATH.
    result = _run_link_onto_path(home, [first, second], path=f"{second}:/usr/bin:/bin")
    assert result.returncode == 0, result.stderr

    assert not (first / "disco").exists()
    assert (second / "disco").is_symlink()
    assert f"SYMLINK_CREATED={second}" in result.stdout


def test_link_onto_path_never_clobbers_a_foreign_disco(tmp_path: Path) -> None:
    """A `disco` we did not create is someone else's command. Overwriting it
    would hijack an unrelated tool, so the candidate is skipped untouched."""
    home, _ = _shim_home(tmp_path)
    cand = tmp_path / "bin"
    cand.mkdir()
    foreign = cand / "disco"
    foreign.write_text("#!/bin/sh\necho not ours\n")

    result = _run_link_onto_path(home, [cand], path=f"{cand}:/usr/bin:/bin")
    assert result.returncode == 0, result.stderr

    assert not foreign.is_symlink()
    assert foreign.read_text() == "#!/bin/sh\necho not ours\n"
    assert "SYMLINK_CREATED=" in result.stdout
    assert str(cand) not in result.stdout


def test_link_onto_path_refreshes_its_own_symlink(tmp_path: Path) -> None:
    """Re-running the installer (or `disco self update`) must be idempotent: a
    link we already own is refreshed, not treated as a foreign command."""
    home, shim_dir = _shim_home(tmp_path)
    cand = tmp_path / "bin"
    cand.mkdir()
    (cand / "disco").symlink_to(shim_dir / "disco")

    result = _run_link_onto_path(home, [cand], path=f"{cand}:/usr/bin:/bin")
    assert result.returncode == 0, result.stderr

    assert os.readlink(cand / "disco") == str(shim_dir / "disco")
    assert f"SYMLINK_CREATED={cand}" in result.stdout


# -------------------------------------------------- meta() parses, never sources ---


def test_self_meta_parses_value_as_data_never_sources(tmp_path: Path) -> None:
    """A version-marker value with shell metacharacters is data, never executed.

    ``meta()`` reads the marker by line-parsing, so a value containing a command
    substitution / backticks must NOT run. We plant such a value, run a
    ``disco-self`` command that reads the marker (``version``), and assert no
    side-effect file was created.
    """
    home = tmp_path / "home"
    home.mkdir(parents=True)
    pwned = home / "PWNED"
    # A repo/ref value an attacker might try to smuggle into a sourced file.
    (home / "version").write_text(
        f"CALFCORD_COMMIT=aaa\nCALFCORD_REPO=$(touch {pwned})`touch {pwned}`\nCALFCORD_REF=main\n"
    )

    result = _run_self(home, ["version"])
    assert result.returncode == 0, result.stderr
    # The metacharacter value was treated as data — nothing executed it.
    assert not pwned.exists()
    # And the value still surfaced verbatim in the output (read, not run).
    assert "$(touch" in result.stdout


# ------------------------------------------------------------------- mcp ---


def test_shim_run_maps_mcp_to_runner_script(tmp_path: Path) -> None:
    """``disco run mcp <server>`` is the supervised slot's command — it must
    resolve to ``calfkit-mcp <server>``."""
    home = tmp_path / "home"
    _install_shims(home)
    assert _run_shim_argv(home, ["run", "mcp", "github"]) == "calfkit-mcp github"


def test_shim_dispatches_mcp_verbs_to_calfcord_cli(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _install_shims(home)
    assert _run_shim_argv(home, ["mcp", "start", "github"]) == "calfcord-cli mcp start github"


def test_seed_config_creates_mcp_json_at_mode_600(tmp_path: Path) -> None:
    """First install seeds an empty mcp.json next to config/.env, 0600 (the
    file may later carry literal credentials)."""
    home = tmp_path / "home"
    dest = tmp_path / "src"
    dest.mkdir()
    (dest / ".env.example").write_text("EXAMPLE=value\n")

    result = _source_and_run(f'seed_config "{dest}"', home=home)
    assert result.returncode == 0, result.stderr

    mcp_json = home / "config" / "mcp.json"
    assert json.loads(mcp_json.read_text()) == {"mcpServers": {}}
    assert (mcp_json.stat().st_mode & 0o777) == 0o600


def test_seed_config_keeps_existing_mcp_json_untouched(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    existing = '{"mcpServers": {"github": {"command": "x"}}}'
    (home / "config" / "mcp.json").write_text(existing)
    dest = tmp_path / "src"
    dest.mkdir()
    (dest / ".env.example").write_text("EXAMPLE=value\n")

    result = _source_and_run(f'seed_config "{dest}"', home=home)
    assert result.returncode == 0, result.stderr
    assert (home / "config" / "mcp.json").read_text() == existing

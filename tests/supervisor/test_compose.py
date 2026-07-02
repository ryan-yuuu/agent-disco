"""Golden tests for the Process Compose SUBSTRATE generator.

``build_compose_project`` is a pure function: home dir + launcher prefix in, a
process-compose project ``dict`` out (no I/O, no broker). Since Phase 2 the roster
(agents, ``tools``, ``mcp-<server>``) moved OFF Process Compose — spawned as
detached processes instead — so only the substrate (``broker`` + ``bridge``) is
declared here. The tests assert on the *parsed* structure — both the dict directly
and the round-trip through :func:`render_compose` / ``yaml.safe_load`` — rather than
brittle string matching, so a formatting change never breaks them while a contract
change (the §13.2 pinned facts) does.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from calfcord.supervisor.compose import (
    broker_is_compose_managed,
    build_compose_project,
    render_compose,
)

_HOME = "/srv/calfcord"
_LAUNCHER = "/srv/calfcord/shims/disco"


def _project() -> dict:
    return build_compose_project(home=_HOME, launcher=_LAUNCHER)


def _processes() -> dict:
    return _project()["processes"]


def test_substrate_processes_are_present() -> None:
    procs = _processes()
    assert "broker" in procs
    assert "bridge" in procs


def test_only_the_substrate_is_declared() -> None:
    # Phase 2: the roster is spawned off Process Compose, so nothing but the
    # substrate is in the generated project.
    assert set(_processes()) == {"broker", "bridge"}


def test_substrate_autostarts() -> None:
    # Substrate: the office itself autostarts under `disco start`.
    procs = _processes()
    assert procs["broker"]["disabled"] is False
    assert procs["bridge"]["disabled"] is False


def test_broker_has_no_dependencies() -> None:
    # The broker is the root of the office; nothing precedes it.
    assert "depends_on" not in _processes()["broker"]


def test_bridge_depends_on_broker_health() -> None:
    assert _processes()["bridge"]["depends_on"] == {"broker": {"condition": "process_healthy"}}


def test_substrate_readiness_probes_are_exec_only() -> None:
    procs = _processes()
    for component in ("broker", "bridge"):
        probe = procs[component]["readiness_probe"]
        assert set(probe["exec"]) == {"command"}
        # Exec only — the bridge has no HTTP server to http_get against.
        assert "http_get" not in probe
        assert probe["initial_delay_seconds"] == 2
        assert probe["period_seconds"] == 3
        assert probe["timeout_seconds"] == 5
        assert probe["success_threshold"] == 1
        assert probe["failure_threshold"] == 3


def test_readiness_probe_commands_invoke_the_launcher_healthcheck() -> None:
    procs = _processes()
    assert procs["broker"]["readiness_probe"]["exec"]["command"] == (f"{_LAUNCHER} _healthcheck broker")
    assert procs["bridge"]["readiness_probe"]["exec"]["command"] == (f"{_LAUNCHER} _healthcheck bridge")


def test_substrate_restart_always() -> None:
    # broker/bridge exit 0 on a clean signal-less return, so on_failure would
    # never fire — they must restart: always to recover an uncommanded clean exit.
    procs = _processes()
    for name in ("broker", "bridge"):
        availability = procs[name]["availability"]
        assert availability["restart"] == "always"
        assert availability["backoff_seconds"] == 2
        assert availability["max_restarts"] == 0


def test_no_process_uses_exit_on_failure() -> None:
    for proc in _processes().values():
        assert proc["availability"]["restart"] != "exit_on_failure"


def test_command_strings_invoke_the_launcher() -> None:
    procs = _processes()
    assert procs["broker"]["command"] == f"{_LAUNCHER} broker"
    assert procs["bridge"]["command"] == f"{_LAUNCHER} run bridge"


def test_launcher_prefix_is_parameterized() -> None:
    # A different launcher (e.g. a dev `uv run calfcord-cli` shim) flows through
    # untouched — the generator never reconstructs uv-run flags.
    procs = build_compose_project(home=_HOME, launcher="uv run calfcord-cli")["processes"]
    assert procs["broker"]["command"] == "uv run calfcord-cli broker"
    assert procs["bridge"]["command"] == "uv run calfcord-cli run bridge"


def test_per_process_log_locations_live_under_state_logs() -> None:
    procs = _processes()
    for name in ("broker", "bridge"):
        assert procs[name]["log_location"] == f"{_HOME}/state/logs/{name}.log"


def test_every_process_has_a_shutdown_block() -> None:
    for proc in _processes().values():
        assert proc["shutdown"] == {
            "signal": 15,
            "timeout_seconds": 10,
            "parent_only": False,
        }


def test_project_declares_the_compose_schema_version() -> None:
    # Process Compose v1.110.0 reads the "0.5" config schema (NOT the binary tag).
    assert _project()["version"] == "0.5"


def test_project_level_log_rotation_block() -> None:
    assert _project()["log_configuration"]["rotation"] == {
        "max_size_mb": 10,
        "max_age_days": 7,
        "max_backups": 5,
        "compress": True,
    }


def test_render_round_trips_to_the_same_structure() -> None:
    rendered = render_compose(home=_HOME, launcher=_LAUNCHER)
    assert isinstance(rendered, str)
    assert yaml.safe_load(rendered) == _project()


def test_render_emits_broker_before_bridge() -> None:
    # sort_keys=False keeps the readable broker-first ordering the builder emits.
    rendered = render_compose(home=_HOME, launcher=_LAUNCHER)
    order = list(yaml.safe_load(rendered)["processes"])
    assert order == ["broker", "bridge"]


# The golden tests above pin the *structure* against our reading of the §13.2
# contract; this gated lane is the complement that catches the failure they
# can't — a project that parses fine as a dict but the REAL ``process-compose``
# binary rejects (an unknown field, a wrong type, a schema-version drift). It
# writes ``render_compose`` to a tmpfile and runs ``process-compose up
# --dry-run`` ("validate the config and exit"), asserting exit 0. Gated behind
# ``CALF_TEST_PC`` with the binary on PATH — mirrors the ``test_pc_client.py``
# real-binary lane — so it skips cleanly on a host without the binary and never
# blocks the unit suite::
#
#     CALF_TEST_PC=1 PATH="$HOME/.calfcord/bin:$PATH" \
#         uv run pytest tests/supervisor/test_compose.py
_PC_GATE = pytest.mark.skipif(
    not os.getenv("CALF_TEST_PC") or shutil.which("process-compose") is None,
    reason="set CALF_TEST_PC=1 with `process-compose` on PATH to validate against the real binary",
)


@_PC_GATE
def test_rendered_compose_validates_against_the_real_binary(tmp_path: Path) -> None:
    project = tmp_path / "process-compose.yaml"
    project.write_text(render_compose(home=_HOME, launcher=_LAUNCHER))
    result = subprocess.run(
        ["process-compose", "up", "--dry-run", "-f", str(project)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "process-compose rejected the generated project "
        f"(exit={result.returncode})\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


# --- broker: compose-managed (local) vs external -----------------------------


@pytest.mark.parametrize(
    "server_urls",
    [
        "localhost",
        "localhost:9092",
        "127.0.0.1:9092",
        "127.0.0.5:9092",
        "::1",
        "[::1]:9092",
        ":9092",
        "localhost:9092,127.0.0.1:9093",
    ],
)
def test_loopback_urls_are_compose_managed(server_urls: str) -> None:
    # A loopback broker URL means calfcord itself supervises a local Tansu as the
    # ``broker`` compose process — so `start` lets `up` launch it (no pre-launch
    # probe) and the manifest declares the slot.
    assert broker_is_compose_managed(server_urls) is True


@pytest.mark.parametrize(
    "server_urls",
    [
        "broker.example.com:9092",
        "10.0.0.5:9092",
        "[2001:db8::1]:9092",
        "localhost:9092,broker.example.com:9093",
        "",
    ],
)
def test_external_urls_are_not_compose_managed(server_urls: str) -> None:
    # A real external broker (or an empty/unknown URL) is NOT supervised locally:
    # `start` keeps its fast-fail probe and the manifest must not declare a broker.
    assert broker_is_compose_managed(server_urls) is False


def test_external_broker_omits_the_broker_process() -> None:
    # Starting a local ephemeral broker nobody talks to is wrong: an external-broker
    # install must not declare a ``broker`` slot at all.
    procs = build_compose_project(home=_HOME, launcher=_LAUNCHER, broker_managed=False)["processes"]
    assert "broker" not in procs
    # The bridge is still declared.
    assert "bridge" in procs


def test_external_broker_drops_depends_on_broker() -> None:
    # With no local ``broker`` process, nothing may declare a depends_on to it —
    # process-compose would reject a dependency on an undeclared process.
    procs = build_compose_project(home=_HOME, launcher=_LAUNCHER, broker_managed=False)["processes"]
    assert "depends_on" not in procs["bridge"]


def test_external_broker_keeps_bridge_readiness_probe() -> None:
    # The bridge still carries its own readiness probe (Discord heartbeat) even
    # when the broker is external — only the broker gating changes.
    procs = build_compose_project(home=_HOME, launcher=_LAUNCHER, broker_managed=False)["processes"]
    assert "readiness_probe" in procs["bridge"]


def test_broker_managed_defaults_true_backward_compatible() -> None:
    # The default keeps the native (compose-managed) shape: broker present + gated.
    procs = build_compose_project(home=_HOME, launcher=_LAUNCHER)["processes"]
    assert "broker" in procs
    assert procs["bridge"]["depends_on"] == {"broker": {"condition": "process_healthy"}}

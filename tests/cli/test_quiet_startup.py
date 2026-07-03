"""Guard that the CLI startup import chain stays quiet on stderr.

``disco init`` imports the Codex provider, which pulls in ``openhands.sdk`` (a
banner ``print`` to stderr at import, plus a RichHandler it installs on the ROOT
logger) and ``litellm`` (its own warning handler). Left unquieted, those dirty
the wizard's interface with a banner, ``LiteLLM:WARNING`` lines, and every
library's INFO logs. ``calfcord/__init__.py`` — the package root, imported before
any submodule — sets three env defaults that each silence one of those
mechanisms before the offending import runs.

Subprocess (not in-process) on purpose: the banner prints once per process and
the root-logger config is a global side effect, so the test session's own
imports would have already fired both. A clean interpreter is the only faithful
way to observe first-import behaviour. The probe's env is stripped of the three
keys so ``setdefault`` — not an inherited value — is what gets exercised.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

# Walk the exact path the ``disco init`` wizard's credential step takes: import
# the real console-entry module (which runs ``calfcord/__init__.py`` first), then
# the ``openhands`` auth import ``_codex_login`` performs. Emit an INFO through a
# neutral logger — it reaches stderr ONLY if openhands hijacked the root logger —
# and report the resolved env defaults + a completion marker on stdout.
_PROBE = r"""
import json, logging, os, sys

import calfcord.cli.main  # runs calfcord/__init__.py before any openhands import
from openhands.sdk.llm.auth import OpenAISubscriptionAuth  # noqa: F401

logging.getLogger("quiet_startup_probe").info("PROBE_INFO_LEAKED")

sys.stdout.write(json.dumps({
    "banner": os.environ.get("OPENHANDS_SUPPRESS_BANNER"),
    "log_auto_config": os.environ.get("LOG_AUTO_CONFIG"),
    "litellm_log": os.environ.get("LITELLM_LOG"),
    "ran": True,
}))
"""


@pytest.fixture(scope="module")
def startup() -> subprocess.CompletedProcess[str]:
    """Run the import-chain probe once in a clean interpreter; share its output."""
    env = os.environ.copy()
    for key in ("OPENHANDS_SUPPRESS_BANNER", "LOG_AUTO_CONFIG", "LITELLM_LOG"):
        env.pop(key, None)
    return subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )


def _assert_ran(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    """The probe must have completed, else an absence assertion passes vacuously."""
    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert payload["ran"] is True
    return payload


def test_startup_prints_no_openhands_banner(startup: subprocess.CompletedProcess[str]) -> None:
    _assert_ran(startup)
    assert "OpenHands SDK" not in startup.stderr, startup.stderr


def test_startup_does_not_hijack_root_logger(startup: subprocess.CompletedProcess[str]) -> None:
    _assert_ran(startup)
    assert "PROBE_INFO_LEAKED" not in startup.stderr, startup.stderr


def test_startup_sets_quiet_env_defaults(startup: subprocess.CompletedProcess[str]) -> None:
    payload = _assert_ran(startup)
    assert payload["banner"] == "1"
    assert payload["log_auto_config"] == "false"
    assert payload["litellm_log"] == "ERROR"

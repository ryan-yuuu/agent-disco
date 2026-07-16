"""Guard that importing the CLI entry point does not drag in the agent stack.

``disco``'s console script is ``calfcord.cli.main``, so EVERY invocation pays
that module's import cost — including ``disco _healthcheck <component>``, which
Process Compose re-runs as an exec readiness probe every few seconds for the life
of the workspace (``supervisor/compose.py``). A probe whose whole job is reading
one JSON file must not load the agent framework to do it: importing
``agent_create`` at module scope pulled ``calfcord.agents`` -> ``calfkit`` ->
``calfkit.nodes.agent`` -> ``pydantic_ai`` and cost ~1.4s per probe (and per
``disco`` command). See ADR-0023.

The subcommand modules are therefore imported lazily, inside the dispatch arm
that needs them. These tests pin that: the fix is a latency invariant, and the
next top-level ``from calfcord.cli import <module>`` reaching the agent stack
would silently re-impose the tax with no visible symptom but a slow CLI.

Subprocess (not in-process) on purpose: ``sys.modules`` is process-global and the
test session has already imported the agent stack for other tests, so only a
clean interpreter can observe first-import behaviour faithfully.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

# Import the real console-entry module, then report which of the heavy chain's
# members ended up in sys.modules. ``ran`` guards against a probe that died
# early, which would make every absence assertion below pass vacuously.
_PROBE = r"""
import json, sys

import calfcord.cli.main  # the console-script entry point every `disco` runs

sys.stdout.write(json.dumps({
    "factory": "calfcord.agents.factory" in sys.modules,
    "calfkit_nodes_agent": "calfkit.nodes.agent" in sys.modules,
    "calfkit_provider": "calfkit.providers.pydantic_ai" in sys.modules,
    "ran": True,
}))
"""


@pytest.fixture(scope="module")
def imported() -> dict[str, object]:
    """Import ``calfcord.cli.main`` in a clean interpreter; share the module census."""
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert payload["ran"] is True
    return payload


def test_cli_entry_point_does_not_import_the_agent_factory(imported: dict[str, object]) -> None:
    """``factory`` is calfcord's own doorway into the heavy chain.

    The light members of :mod:`calfcord.agents` (``definition``, ``identifier``,
    ``md_writer``) are fair game — the parser genuinely needs ``_fields``. Only the
    factory must stay out, which is what the barrel's lazy re-export buys.
    """
    assert imported["factory"] is False


def test_cli_entry_point_does_not_import_calfkit_agent_nodes(imported: dict[str, object]) -> None:
    """The calfkit-side link that pulls the model providers in."""
    assert imported["calfkit_nodes_agent"] is False


def test_cli_entry_point_does_not_import_the_model_provider_adapter(imported: dict[str, object]) -> None:
    """The chain's most expensive leaf (~0.3s of the ~1.4s, per ADR-0023)."""
    assert imported["calfkit_provider"] is False

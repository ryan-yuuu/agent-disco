"""Guard the ``calfcord.agents`` barrel's lazy factory re-export (ADR-0023).

``factory`` is the only heavy member of this package: it reaches ``calfkit`` ->
``calfkit.nodes.agent`` -> ``calfkit.providers.pydantic_ai``. ``definition``,
``loader``, ``md_writer``, and ``identifier`` are all pure-stdlib-plus-frontmatter.
Re-exporting ``factory`` eagerly from ``__init__`` therefore made importing ANY of
them — including ``identifier``, which holds a single regex — cost the entire agent
framework. That landed on every ``disco`` command and on the ``_healthcheck``
readiness probe Process Compose re-runs every few seconds forever.

The barrel now resolves ``AgentFactory`` / ``resolve_provider`` through PEP 562
``__getattr__``, so the advertised public surface is unchanged while the import is
charged only to callers that actually touch it. Both halves of that bargain are
pinned here: the name must still resolve, and it must not be imported until asked
for.

Subprocess on purpose: ``sys.modules`` is process-global, and the wider test
session imports ``factory`` for other tests, so only a clean interpreter can
observe first-import behaviour.
"""

from __future__ import annotations

import json
import subprocess
import sys

_PROBE = r"""
import json, sys

import calfcord.agents

before = "calfcord.agents.factory" in sys.modules
resolved = calfcord.agents.AgentFactory.__name__          # triggers PEP 562 __getattr__
after = "calfcord.agents.factory" in sys.modules

sys.stdout.write(json.dumps({
    "eager": before,
    "resolved": resolved,
    "loaded_on_demand": after,
    "ran": True,
}))
"""


def _probe() -> dict[str, object]:
    result = subprocess.run([sys.executable, "-c", _PROBE], capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert payload["ran"] is True
    return payload


def test_importing_the_barrel_does_not_import_factory() -> None:
    """The latency half: the heavy member stays unimported until asked for."""
    assert _probe()["eager"] is False


def test_agent_factory_still_resolves_from_the_barrel() -> None:
    """The compatibility half: the advertised public surface still works."""
    payload = _probe()
    assert payload["resolved"] == "AgentFactory"
    assert payload["loaded_on_demand"] is True


def test_unknown_barrel_attribute_still_raises_attribute_error() -> None:
    """``__getattr__`` must not swallow real typos into an opaque ImportError."""
    import calfcord.agents

    try:
        calfcord.agents.NoSuchName  # noqa: B018
    except AttributeError as exc:
        assert "NoSuchName" in str(exc)
    else:
        raise AssertionError("expected AttributeError for an unknown barrel attribute")

"""Tests for the shared supervisor-availability CLI helper (:mod:`calfcord.cli._supervisor`)."""

from __future__ import annotations

import pytest

from calfcord.cli import _supervisor


def test_reason_is_none_when_the_binary_resolves() -> None:
    """A resolvable binary means the live finish can run — no reason to degrade."""
    assert _supervisor.supervisor_unavailable_reason(lambda: "/usr/bin/process-compose") is None


def test_reason_is_the_actionable_message_when_the_binary_is_missing() -> None:
    """``resolve_pc_binary`` signals 'missing' by raising an actionable RuntimeError;
    the helper surfaces that text as a value so the caller can name the fix."""

    def _missing() -> str:
        raise RuntimeError("process-compose binary not found; re-run the installer")

    assert (
        _supervisor.supervisor_unavailable_reason(_missing)
        == "process-compose binary not found; re-run the installer"
    )


def test_non_runtimeerror_propagates() -> None:
    """The catch stays narrow: a missing binary is a documented domain RuntimeError, but
    an OSError (e.g. a permissions fault on the bin dir) is a real fault that must
    propagate, not be laundered into a benign 'unavailable' degrade."""

    def _permission_fault() -> str:
        raise OSError("permission denied")

    with pytest.raises(OSError):
        _supervisor.supervisor_unavailable_reason(_permission_fault)

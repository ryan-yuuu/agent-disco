"""The one test that would have caught the non-TTY bug.

Every other test in this package injects a seam: the scripted ``read``, or a
monkeypatched ``readchar.readkey`` raising a ``termios.error`` the test itself
constructed. Those prove the pieces. None of them proves the **join** —
readchar → ``termios.error`` → the ``OSError`` translation → ``main``'s handler
→ exit 1 — and the join is exactly where the bug lived.

That bug shipped precisely because a test monkeypatched the exception: the unit
test stayed green while a real piped run dumped a traceback, because
``termios.error`` does not subclass ``OSError`` and never reached the handler.
A test that invents the exception it then catches cannot catch that class of
bug — it asserts the translation of a fiction.

So this drives the real binary against a real closed stdin and asserts only what
an operator would see. It is slow (a subprocess per case) and that is the price
of testing the thing itself.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def _run(args: list[str], *, cwd: Path, close_stdin: bool = False) -> subprocess.CompletedProcess[str]:
    """Run the real CLI against a real broken stdin.

    Two genuinely different states, and an earlier version of this file conflated
    them. ``DEVNULL`` opens a **real fd**, so ``sys.stdin.fileno()`` succeeds and
    only ``termios.tcgetattr`` fails. ``close_stdin`` closes fd 0 at exec, which
    makes CPython set ``sys.stdin`` to ``None`` — so ``.fileno()`` raises first,
    on a completely different path.

    Testing only the DEVNULL case was this file committing its own stated sin: it
    invented the stdin state, then proved the handling of the state it invented.
    """
    return subprocess.run(
        [sys.executable, "-m", "calfcord.cli.main", *args],
        stdin=None if close_stdin else subprocess.DEVNULL,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=60,
        preexec_fn=(lambda: os.close(0)) if close_stdin else None,
    )


# ``mcp add`` prompts before it touches disk, so a non-TTY run is pure and leaves
# the tmp cwd untouched. ``agent create`` reaches the same reader through a
# different caller, which catches OSError itself — worth covering both, since the
# two produce different messages and only one is main()'s canonical text.
@pytest.mark.parametrize("args", [["mcp", "add"], ["agent", "create", "probe"]])
def test_an_interactive_command_on_a_closed_stdin_never_dumps_a_traceback(
    args: list[str], tmp_path: Path
) -> None:
    """A traceback here is the failure the whole translation exists to prevent."""
    result = _run(args, cwd=tmp_path)
    assert "Traceback" not in result.stderr, result.stderr


@pytest.mark.parametrize("args", [["mcp", "add"], ["agent", "create", "probe"]])
def test_an_interactive_command_on_a_closed_stdin_exits_one(args: list[str], tmp_path: Path) -> None:
    """Not 0 (a lie), not a crash code — a clean, scriptable failure."""
    assert _run(args, cwd=tmp_path).returncode == 1


def test_the_operator_is_told_they_need_a_terminal(tmp_path: Path) -> None:
    """The message must name the actual problem, not a downstream symptom."""
    result = _run(["mcp", "add"], cwd=tmp_path)
    assert "interactive terminal" in result.stdout


class TestStdinIsGone:
    """fd 0 closed at exec — a different path, and one the DEVNULL cases miss.

    CPython sets ``sys.stdin`` to ``None``, so readchar's ``sys.stdin.fileno()``
    raises ``AttributeError`` *before* it ever reaches ``termios`` — sailing past
    both the ENOTTY translation and main()'s handler. Reachable from any
    supervisor or daemon that spawns the CLI without wiring stdin.
    """

    @pytest.mark.parametrize("args", [["mcp", "add"], ["agent", "create", "probe"]])
    def test_never_dumps_a_traceback(self, args: list[str], tmp_path: Path) -> None:
        result = _run(args, cwd=tmp_path, close_stdin=True)
        assert "Traceback" not in result.stderr, result.stderr

    def test_exits_one_with_an_actionable_message(self, tmp_path: Path) -> None:
        result = _run(["mcp", "add"], cwd=tmp_path, close_stdin=True)
        assert result.returncode == 1
        assert "interactive terminal" in result.stdout


def test_the_reason_survives_a_caller_that_catches_oserror_itself(tmp_path: Path) -> None:
    """``agent create`` wraps its writes in ``except OSError`` and reports its own
    message, so it intercepts this before main()'s handler. The wording is its
    own, but the CAUSE must still reach the operator rather than being reported
    as a mystery filesystem failure."""
    result = _run(["agent", "create", "probe"], cwd=tmp_path)
    assert "not an interactive terminal" in result.stdout

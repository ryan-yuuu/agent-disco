"""The widgets, driven in a REAL pseudo-terminal.

Every other test in this package injects a seam: a scripted ``read``, or a
non-terminal console that makes ``Live.refresh`` a no-op. They are fast and they
pin behaviour, but they all run with **rendering switched off** — so the thing an
operator actually looks at is never exercised.

These are the opposite trade: slow (a subprocess and a pty per case), and they
prove the parts nothing else can. Everything below has already been a bug that
the mocked suite reported as passing:

* the panel painting at all, before any key is pressed;
* arrow keys arriving as real escape bytes and moving the cursor;
* space reaching a text field instead of being eaten as a command;
* Ctrl-C reaching the process as a real SIGINT and exiting 130;
* the transient frame being erased and the cursor restored.

If these are ever the only failures, suspect the harness before the product —
that happened twice while writing them (see ``_spawn``).
"""

from __future__ import annotations

import fcntl
import os
import pathlib
import re
import select
import struct
import subprocess
import sys
import termios
import textwrap
import time

import pytest

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith(("darwin", "linux")),
    reason="pty driving is POSIX-only, which is all this CLI supports",
)

ROWS, COLS = 24, 80
UP, DOWN, LEFT, RIGHT = "\x1b[A", "\x1b[B", "\x1b[D", "\x1b[C"
HOME, END, DELETE = "\x1b[H", "\x1b[F", "\x1b[3~"
ENTER, SPACE, BACKSPACE, CTRL_C = "\r", " ", "\x7f", "\x03"

# The child is a bare `python -c`, so the package must be put on its path
# explicitly — it does not inherit this test run's environment.
_SRC = str(pathlib.Path(__file__).resolve().parents[3] / "src")

_PRELUDE = f"""
import sys
sys.path.insert(0, {_SRC!r})
from calfcord.cli._prompts import Choice
from calfcord.cli.tui import widgets
"""

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[=>]")


def _spawn(body: str, keys: list[str], *, settle: float = 0.5) -> tuple[str, str, int]:
    """Run ``body`` under a real pty, send ``keys``, return (raw, visible, status).

    ``body`` is dedented SEPARATELY from the prelude and only then joined. Both
    must be flush before concatenation: ``textwrap.dedent`` strips the *common*
    prefix, so gluing a module-level prelude (0 indent) to a class-body string
    (4 indent) leaves a mixture it cannot fix, and the child dies on
    IndentationError. That cost two debugging rounds; do not "simplify" it back.

    Three harness traps, all of which looked exactly like product bugs:

    * The child must claim the pty as its **controlling terminal**, itself, after
      ``setsid``. Done from the parent it is a no-op, and the tty driver then eats
      Ctrl-C and signals a process group the child was never in — so SIGINT never
      arrives and the widget looks broken.
    * Writing a key after the child has exited raises EIO, so poll first.
    """

    def _claim_tty() -> None:
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
    env = {**os.environ, "TERM": "xterm-256color", "COLUMNS": str(COLS), "LINES": str(ROWS)}
    env.pop("NO_COLOR", None)
    code = textwrap.dedent(_PRELUDE) + textwrap.dedent(body)
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        close_fds=True,
        preexec_fn=_claim_tty,
    )
    os.close(slave)

    out = bytearray()

    def pump(seconds: float, *, until_output: bool = False) -> None:
        """Drain the pty for ``seconds``, or until the child first speaks.

        ``until_output`` waits on a CONDITION rather than a duration, because the
        first frame costs a Python start plus a Rich import — unbounded on a
        loaded CI runner. A fixed sleep there is a flake generator; every later
        redraw is from a warm process and a short settle is honest.
        """
        deadline = time.time() + seconds
        while time.time() < deadline:
            ready, _, _ = select.select([master], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                out.extend(chunk)
                # prompt_toolkit asks a real terminal where its cursor is before
                # the first render. A pty is only a byte pipe, so the harness must
                # provide the terminal's CPR response itself.
                if b"\x1b[6n" in chunk:
                    os.write(master, b"\x1b[1;1R")
            elif until_output and out:
                return  # it has painted and gone quiet — that is the frame

    try:
        # The FIRST frame, painted before a single key is sent.
        pump(20.0, until_output=True)
        pump(settle)
        for key in keys:
            if proc.poll() is not None:
                break
            try:
                os.write(master, key.encode())
            except OSError:
                break
            pump(settle)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        pump(0.3)
    finally:
        os.close(master)

    raw = out.decode("utf-8", "replace")
    return raw, _ANSI.sub("", raw), proc.returncode


class TestSelectInARealTerminal:
    CODE = """
    rows = [Choice("a", "Anthropic"), Choice("o", "OpenAI"), Choice("c", "Codex")]
    print("RESULT=" + widgets.select("Model provider", rows))
    """

    @pytest.fixture(scope="class")
    def run(self) -> tuple[str, str, int]:
        return _spawn(self.CODE, [DOWN, ENTER])

    def test_the_frame_paints_before_any_key(self, run) -> None:
        """_loop reads a key BEFORE its first live.update — the opening frame
        comes from Live.__enter__ alone. If that regressed it would look like a
        hang, and every mocked test would still pass."""
        _, seen, _ = run
        assert "Model provider" in seen
        assert all(row in seen for row in ("Anthropic", "OpenAI", "Codex"))

    def test_the_border_and_hint_render(self, run) -> None:
        _, seen, _ = run
        assert "╭" in seen and "╰" in seen
        assert "ctrl-c cancel" in seen

    def test_a_real_arrow_key_moves_the_cursor(self, run) -> None:
        """The arrow arrives as three real bytes through readchar, not as a fixture."""
        _, seen, _ = run
        assert "RESULT=o" in seen

    def test_the_widget_is_erased_and_the_cursor_restored(self, run) -> None:
        raw, seen, _ = run
        assert "\x1b[2K" in raw, "the transient frame should have been erased"
        assert "\x1b[?25h" in raw, "the cursor must be given back"
        assert "✓" in seen, "the collapsed record should remain"

    def test_it_exits_cleanly(self, run) -> None:
        assert run[2] == 0


class TestTypingInARealTerminal:
    CODE = """
    print("RESULT=[" + widgets.text("Command") + "]")
    """

    def test_spaces_survive_real_keystrokes(self) -> None:
        """The shipped bug: space was bound as a Key, so text fields ate it and
        "npx -y pkg" became "npx-ypkg" — breaking `disco mcp add` outright."""
        _, seen, status = _spawn(self.CODE, [*"npx -y pkg", ENTER])
        assert "RESULT=[npx -y pkg]" in seen
        assert status == 0

    def test_a_default_is_editable_in_place(self) -> None:
        """A default is initial buffer content, not a placeholder.

        Moving left and typing must insert before the last character. This is the
        smallest end-to-end proof that the field owns a real editing cursor.
        """
        code = 'print("RESULT=[" + widgets.text("Name", default="scribe") + "]")'
        _, seen, status = _spawn(code, [LEFT, "r", ENTER])
        assert "RESULT=[scribre]" in seen
        assert status == 0

    def test_backspace_edits_the_supplied_default(self) -> None:
        code = 'print("RESULT=[" + widgets.text("Name", default="scribe") + "]")'
        _, seen, status = _spawn(code, [BACKSPACE, ENTER])
        assert "RESULT=[scrib]" in seen
        assert status == 0

    def test_home_end_and_delete_edit_at_the_cursor(self) -> None:
        code = 'print("RESULT=[" + widgets.text("Name", default="scribe") + "]")'
        _, seen, status = _spawn(code, [HOME, DELETE, END, "r", ENTER])
        assert "RESULT=[criber]" in seen
        assert status == 0

    def test_a_pasted_burst_is_not_dropped(self) -> None:
        _, seen, status = _spawn(self.CODE, ["npx -y package", ENTER], settle=0.05)
        assert "RESULT=[npx -y package]" in seen
        assert status == 0

    def test_the_terminal_cursor_is_visible_while_editing(self) -> None:
        raw, _, status = _spawn(self.CODE, [ENTER])
        assert "\x1b[?25h" in raw
        assert status == 0


class TestSecretInARealTerminal:
    CODE = """
    value = widgets.secret("Token")
    print("EDITED=" + str(value == "hunte32"))
    """

    def test_the_secret_is_editable_but_never_painted(self) -> None:
        raw, seen, status = _spawn(self.CODE, [*"hunter2", LEFT, BACKSPACE, "3", ENTER])
        assert "EDITED=True" in seen
        assert "hunter2" not in raw
        assert "hunte32" not in raw
        assert status == 0


class TestCheckboxInARealTerminal:
    CODE = """
    rows = [Choice("fs", "filesystem"), Choice("tm", "terminal")]
    print("RESULT=" + ",".join(widgets.checkbox("Tools", rows, instruction="Pick some.")))
    """

    def test_space_toggles_and_the_glyphs_render(self) -> None:
        _, seen, status = _spawn(self.CODE, [SPACE, DOWN, SPACE, ENTER])
        assert "Pick some." in seen, "the instruction should render"
        assert "◉" in seen and "○" in seen, "both mark states should paint"
        assert "RESULT=fs,tm" in seen
        assert status == 0


class TestInterruptInARealTerminal:
    CODE = """
    import sys
    try:
        widgets.select("Model provider", [Choice("a", "Anthropic")])
    except KeyboardInterrupt:
        print("INTERRUPTED")
        sys.exit(130)
    """

    def test_ctrl_c_reaches_the_process_and_exits_130(self) -> None:
        """A real ^C through the tty driver — the resumable-abort contract init
        teaches. The widget must not catch it."""
        raw, seen, status = _spawn(self.CODE, [CTRL_C])
        assert "INTERRUPTED" in seen
        assert status == 130
        assert "Traceback" not in seen
        assert "\x1b[?25h" in raw, "the cursor must be restored even on abort"

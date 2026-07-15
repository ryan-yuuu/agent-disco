"""The prompt_toolkit-backed single-line editor and its terminal frame."""

from __future__ import annotations

import asyncio
import errno

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.utils import get_cwidth

from calfcord.cli.tui import line_input


def test_frame_uses_terminal_columns_for_wide_unicode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(line_input, "_width", lambda: 24)
    framed = line_input._framed_line("╭─ ", "名前 ", "╮")
    assert get_cwidth(framed) == 23
    assert framed.endswith("╮")


def test_frame_truncates_a_long_title_without_breaking_its_border(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(line_input, "_width", lambda: 24)
    framed = line_input._framed_line("╭─ ", "Command (e.g. npx -y package) ", "╮")
    assert get_cwidth(framed) == 23
    assert "…" in framed
    assert framed.endswith("╮")


def test_narrow_terminal_uses_its_real_width(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(line_input, "_width", lambda: 10)
    prompt = line_input._message("Agent description")()[0][1]
    assert all(get_cwidth(row) <= 9 for row in prompt.splitlines())
    assert line_input._rprompt() == []
    assert get_cwidth(line_input._toolbar()[0][1]) <= 9


def test_missing_terminal_is_reported_as_enotty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(OSError) as caught:
        line_input.PromptToolkitLineInput().prompt("Name")
    assert caught.value.errno == errno.ENOTTY


def test_sync_editor_works_inside_an_active_asyncio_loop() -> None:
    async def run() -> str:
        with create_pipe_input() as pipe:
            pipe.send_text("edited\r")
            return line_input.PromptToolkitLineInput(input=pipe, output=DummyOutput()).prompt("Name")

    assert asyncio.run(run()) == "edited"

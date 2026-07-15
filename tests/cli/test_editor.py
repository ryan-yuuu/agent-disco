"""Resolving which editor to launch, and how.

Every claim here was checked against a real implementation rather than the
convention's folklore — see ``docs/design/cli-tui-migration.md`` §11.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from calfcord.cli import _editor


@pytest.fixture(autouse=True)
def _no_inherited_editor(monkeypatch: pytest.MonkeyPatch) -> None:
    """This machine has VISUAL/EDITOR unset, but CI might not — pin both."""
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)


class TestEnvironmentPrecedence:
    """``VISUAL`` before ``EDITOR`` — unanimous across every implementation
    checked: click, gh, gemini-cli, Codex, aider. We checked only ``EDITOR``,
    so a user who set just ``VISUAL`` was silently ignored and dropped into vi.
    """

    def test_visual_wins_over_editor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VISUAL", "myvisual")
        monkeypatch.setenv("EDITOR", "myeditor")
        assert _editor.resolve() == ["myvisual"]

    def test_editor_is_used_when_visual_is_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EDITOR", "myeditor")
        assert _editor.resolve() == ["myeditor"]

    def test_a_blank_visual_falls_through_to_editor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``VISUAL=`` is not a choice — an empty value must not win."""
        monkeypatch.setenv("VISUAL", "   ")
        monkeypatch.setenv("EDITOR", "myeditor")
        assert _editor.resolve() == ["myeditor"]

    def test_an_editor_carrying_arguments_is_split(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``EDITOR="emacs -nw"`` is a command line, not a binary name."""
        monkeypatch.setenv("EDITOR", "emacs -nw")
        assert _editor.resolve() == ["emacs", "-nw"]


class TestGuiEditorsAreToldToWait:
    """A GUI editor forks and returns instantly unless told to wait.

    Without the flag we read the temp file back before the operator has typed
    anything, report "Prompt unchanged", and silently discard their work. This
    is the bug gemini-cli auto-injects ``--wait`` to prevent.
    """

    def test_code_gets_wait_injected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EDITOR", "code")
        assert _editor.resolve() == ["code", "--wait"]

    def test_sublime_gets_its_own_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EDITOR", "subl")
        assert _editor.resolve() == ["subl", "-w"]

    def test_an_explicit_wait_is_not_duplicated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EDITOR", "code --wait")
        assert _editor.resolve() == ["code", "--wait"]

    def test_a_full_path_is_still_recognised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``EDITOR=/usr/local/bin/code`` is the same editor — match the basename."""
        monkeypatch.setenv("EDITOR", "/usr/local/bin/code")
        assert _editor.resolve() == ["/usr/local/bin/code", "--wait"]

    def test_terminal_editors_are_left_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """vim blocks by nature; a --wait would be an unknown flag and fail."""
        monkeypatch.setenv("EDITOR", "vim")
        assert _editor.resolve() == ["vim"]


class TestFallbackWhenNothingIsSet:
    """With neither var set, probe for something present rather than assume.

    The field is genuinely split — gh and jj default to nano, click probes
    vim-then-nano, aider and gemini-cli land on vi — so probing is the only
    answer that is right on every box. A hardcoded name is a guess that fails
    silently when it is absent (prompt_toolkit hardcodes /usr/bin paths and
    breaks on Homebrew and Nix).
    """

    def test_the_first_present_candidate_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_editor.shutil, "which", lambda name: "/x/" + name if name == "nano" else None)
        assert _editor.resolve() == ["nano"]

    def test_it_falls_through_to_the_next_candidate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_editor.shutil, "which", lambda name: "/x/" + name if name == "vim" else None)
        assert _editor.resolve() == ["vim"]

    def test_vi_is_the_last_resort_even_if_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """POSIX only guarantees ``ed``, but a vi-less box is not worth modelling —
        we must return *something* rather than raise like Codex does."""
        monkeypatch.setattr(_editor.shutil, "which", lambda _name: None)
        assert _editor.resolve() == ["vi"]


class TestDescribe:
    """The operator must be told which editor is opening, before it opens.

    gh's ``[(e) to launch nano, enter to skip]`` is the model: name it. Being
    dropped into an unnamed full-screen modal editor is the whole failure.
    """

    def test_it_names_a_bare_command(self) -> None:
        assert _editor.describe(["nano"]) == "nano"

    def test_it_names_only_the_binary_of_a_path(self) -> None:
        assert _editor.describe(["/usr/local/bin/code", "--wait"]) == "code"

    def test_it_keeps_arguments_out_of_the_name(self) -> None:
        assert _editor.describe(["emacs", "-nw"]) == "emacs"


class TestArgv:
    def test_the_filename_comes_last(self) -> None:
        """``code --wait <file>`` — a flag after the path is not honoured."""
        assert _editor.argv(["code", "--wait"], Path("/tmp/x.md")) == ["code", "--wait", "/tmp/x.md"]

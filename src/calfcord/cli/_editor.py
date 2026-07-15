"""Resolving which editor to launch for long-form text, and how.

Split out of :mod:`calfcord.cli.agent_edit` because getting this right needs
more care than a one-liner suggests, and the reasoning deserves a home. Each
rule below was checked against a real implementation, not against the folklore
around the ``$EDITOR`` convention.

The old one-liner — ``os.environ.get("EDITOR") or "vi"`` — had two real defects
and one unkind default:

* It ignored ``VISUAL``, which every implementation checked honours **first**
  (click, gh, gemini-cli, Codex, aider). Setting only ``VISUAL`` is common, and
  we silently ignored it and dropped the operator into vi.
* It never told a GUI editor to wait, so ``EDITOR=code`` returned instantly and
  the operator's edit was **silently discarded** (see :data:`_WAIT_FLAGS`).
* It fell back to bare ``vi``. Being dropped unannounced into a modal editor is
  the single most-reported way a CLI strands a newcomer, and this wizard's whole
  metric is time-to-first-reply.
"""

from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path

# GUI editors fork and return immediately unless told to wait for the window to
# close. We would then read the temp file back before a single keystroke landed,
# conclude "unchanged", and throw the operator's work away without a word — a
# silent data loss, the worst kind. gemini-cli injects these for the same reason.
# Keyed by binary name; the value is that editor's own spelling of "block".
_WAIT_FLAGS = {
    "code": "--wait",
    "code-insiders": "--wait",
    "codium": "--wait",
    "cursor": "--wait",
    "windsurf": "--wait",
    "zed": "--wait",
    "atom": "--wait",
    "gedit": "--wait",
    "subl": "-w",
    "sublime_text": "-w",
    "mate": "-w",
}

# Probed in order when neither VISUAL nor EDITOR is set. Deliberately a probe
# rather than a constant: the field does not agree on a default (gh and jj ship
# nano, click prefers vim, aider and gemini-cli use vi), so any single hardcoded
# name is a guess that fails on some boxes. prompt_toolkit hardcodes /usr/bin
# paths and silently breaks under Homebrew and Nix — the failure mode to avoid.
#
# ``sensible-editor`` first because on Debian it IS this decision, made by the
# distro and the user's own `select-editor` choice. Then the friendly editors
# before the modal one: this list only runs when the operator has expressed no
# preference at all, and someone with no $EDITOR is unlikely to want vi.
_FALLBACKS = ("sensible-editor", "nano", "vim", "vi")

# Last resort when even the probe finds nothing. Codex raises here instead; we
# would rather try and fail visibly than refuse to open an editor at all.
_LAST_RESORT = "vi"


def resolve() -> list[str]:
    """Return the editor to launch, as an argv prefix (no filename).

    A prefix rather than a string because the value may carry arguments
    (``EDITOR="emacs -nw"``), and because :func:`argv` must append the filename
    **last** — a path before a flag is not honoured.
    """
    for key in ("VISUAL", "EDITOR"):
        value = os.environ.get(key)
        if value and value.strip():
            # shlex so a command line ("code --wait") is honoured rather than
            # treated as one impossible binary name.
            return _ensure_wait(shlex.split(value))

    for candidate in _FALLBACKS:
        if shutil.which(candidate):
            return [candidate]
    return [_LAST_RESORT]


def _ensure_wait(command: list[str]) -> list[str]:
    """Append the wait flag if this is a GUI editor that needs one.

    Matches on the *basename*, so ``/usr/local/bin/code`` is recognised. Never
    duplicates a flag the operator already set, and never adds one to a terminal
    editor — vim would reject ``--wait`` as an unknown option.
    """
    if not command:
        return command
    flag = _WAIT_FLAGS.get(Path(command[0]).name)
    if flag is None or flag in command:
        return command
    return [*command, flag]


def describe(command: list[str]) -> str:
    """The editor's name, for telling the operator what is about to open.

    Just the binary — arguments are noise in a sentence, and a full path is
    worse than a name. Naming it is the cheap half of gh's
    ``[(e) to launch nano, enter to skip]``: the failure being prevented is an
    operator staring at an unfamiliar full-screen editor with no idea what it is.
    """
    return Path(command[0]).name if command else _LAST_RESORT


def argv(command: list[str], path: Path) -> list[str]:
    """The full argv: the editor prefix, then the file. The file goes last."""
    return [*command, str(path)]

"""Unit tests for :func:`calfcord.bridge.persona_resolve.accent_for`.

The trace container's stripe is **agent identity, never state** — the same idea
as the DiceBear avatar seeded by the agent's name, and the reason red stops being
meaningless (it is simply gone; a fault rides the seal row instead).
"""

from __future__ import annotations

import subprocess
import sys

import discord

from calfcord.bridge.persona_resolve import _ACCENTS, accent_for


class TestAccentFor:
    def test_returns_a_discord_colour(self) -> None:
        assert isinstance(accent_for("aksel"), discord.Colour)

    def test_is_deterministic_within_a_process(self) -> None:
        assert accent_for("aksel") == accent_for("aksel")

    def test_different_agents_generally_differ(self) -> None:
        # Not a guarantee — a small curated palette collides by design, and two
        # agents sharing a colour is harmless. This just pins that the mapping
        # actually varies rather than returning one constant.
        assert len({accent_for(n) for n in ("aksel", "billing", "conan", "scribe", "echo")}) > 1

    def test_only_ever_returns_a_curated_palette_colour(self) -> None:
        # Free-hue derivation can land muddy or illegible on one of Discord's
        # two themes. The palette is hand-picked, so every possible output is
        # known-good.
        for name in ("a", "bb", "ccc", "aksel", "billing", "conan", "zzz", "x" * 50):
            assert accent_for(name) in _ACCENTS

    def test_is_stable_across_processes(self) -> None:
        # THE trap: PYTHONHASHSEED is randomised per process, so hash(name) %
        # len would give an agent a different colour on every bridge restart —
        # deterministic in one test run, wrong in production. A subprocess is
        # the only way to actually catch it.
        code = (
            "from calfcord.bridge.persona_resolve import accent_for;"
            "print(','.join(str(accent_for(n).value) for n in ('aksel','billing','conan')))"
        )
        runs = {
            subprocess.run(
                [sys.executable, "-c", code], capture_output=True, text=True, check=True
            ).stdout.strip()
            for _ in range(2)
        }
        assert len(runs) == 1, f"accent changed between processes: {runs}"
        assert runs != {""}

    def test_no_accent_is_discord_red(self) -> None:
        # Red is retired: it used to stripe every message including successes,
        # which is why colour carried no signal. Failure now rides the seal row.
        assert discord.Colour(0xE74C3C) not in _ACCENTS

"""Resolve the calfcord install root for codex-local on-disk paths.

The codex auth store and prompt cache both live under the install home so
they sit beside ``config/.env``, the agents dir, and ``state/`` — and so they
move with the install when an operator relocates it (``CALFCORD_HOME``), runs
two installs on one host, or runs under systemd.

The resolution itself now lives in :mod:`calfcord.providers._paths` so codex
and grok can't drift on the empty-string guard; this module re-exports it to
preserve the historical ``calfcord.providers.codex._paths.calfcord_home``
import path used across the codex package and its tests.
"""

from __future__ import annotations

from calfcord.providers._paths import calfcord_home

__all__ = ["calfcord_home"]

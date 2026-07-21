"""Resolve calfcord install-root paths shared by the auth providers.

Every provider that persists OAuth credentials (codex, grok, …) keeps them
under the install home so they sit beside ``config/.env``, the agents dir,
and ``state/`` — and so they move with the install when an operator relocates
it (``CALFCORD_HOME``), runs two installs on one host, or runs under systemd.

This is the single source of truth for that resolution; per-provider
``_paths`` modules re-export from here so the call sites can't drift on the
empty-string guard. Every other subsystem (``cli/main.py``, ``mcp/config.py``,
``bridge/gateway.py``) roots its paths at ``$CALFCORD_HOME`` the same way.
"""

from __future__ import annotations

import os
from pathlib import Path

_AUTH_DIR_ENV = "CALFCORD_AUTH_DIR"


def calfcord_home() -> Path:
    """The install root: ``$CALFCORD_HOME``, else the shim's ``~/.agent-disco`` default.

    An empty ``CALFCORD_HOME=`` counts as unset (so a stray assignment doesn't
    root paths at ``/``), matching the guard the CLI/mcp/bridge resolvers use.
    Resolved at call time, not import, so the env is read where the path is used.
    """
    home = os.environ.get("CALFCORD_HOME")
    return Path(home) if home else Path.home() / ".agent-disco"


def provider_auth_dir() -> Path:
    """The shared credential directory for OAuth providers.

    ``CALFCORD_AUTH_DIR`` wins (explicit operator intent); otherwise credentials
    live beside the rest of the install at ``$CALFCORD_HOME/auth`` (default
    ``~/.agent-disco/auth``) so they move with a relocated or per-host install.
    An empty ``CALFCORD_AUTH_DIR=`` counts as unset. Files are vendor-namespaced
    (``openai_oauth.json`` / ``xai_oauth.json``) so providers coexist here.
    """
    override = os.environ.get(_AUTH_DIR_ENV)
    return Path(override) if override else calfcord_home() / "auth"

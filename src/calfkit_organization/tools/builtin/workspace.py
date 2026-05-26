"""Shared workspace root for all builtin filesystem and shell tools.

Every builtin tool that needs a default cwd or path-resolution root reads
from :func:`get_workspace_root`. One directory, shared by every agent on the
``calfkit-tools`` host — the "trusted shared workspace" model documented in
the project README.

The path comes from the ``CALFCORD_WORKSPACE_DIR`` environment variable. If
unset, falls back to ``<repo>/state/workspace/`` next to the other
``state/`` data this project already writes. The directory is created on
first call so a fresh checkout doesn't error on missing path before any
tool has had a chance to populate it.

Resolution is cached: the first call resolves the path, creates the
directory if needed, logs the choice, and stashes it on a module-global.
Subsequent calls return the cached value. Tests reset the cache by
mutating ``_cached_root`` directly.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_ENV_VAR = "CALFCORD_WORKSPACE_DIR"
_DEFAULT_RELATIVE = Path("state") / "workspace"

_cached_root: Path | None = None


def get_workspace_root() -> Path:
    """Return the absolute path to the shared tools workspace.

    Creates the directory if it does not yet exist. Idempotent and cached;
    safe to call from hot paths. The first call logs the resolved path at
    INFO so operators can confirm where tools are writing.
    """
    global _cached_root
    if _cached_root is not None:
        return _cached_root

    # ``Path.cwd()`` is the calfkit-tools process's working directory at
    # startup, which is the repo root when launched via ``uv run`` and
    # the container's ``WORKDIR`` (``/app``) when launched via the
    # provided Dockerfile.
    raw = os.getenv(_ENV_VAR)
    root = (
        Path(raw).expanduser().resolve()
        if raw
        else (Path.cwd() / _DEFAULT_RELATIVE).resolve()
    )

    root.mkdir(parents=True, exist_ok=True)
    logger.info("calfcord workspace root resolved path=%s env=%s", root, _ENV_VAR in os.environ)
    _cached_root = root
    return root


def _reset_cache_for_tests() -> None:
    """Clear the cached workspace root so the next ``get_workspace_root`` call
    re-resolves the env var. Test-only — production code never invalidates
    the cache because ``CALFCORD_WORKSPACE_DIR`` is treated as boot-time
    configuration."""
    global _cached_root
    _cached_root = None

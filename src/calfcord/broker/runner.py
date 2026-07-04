"""Launcher for the local Tansu broker (the ``calfcord-broker`` console script).

Resolves the Tansu binary bundled by ``calfkit-mesh`` and process-replaces this
launcher with ``tansu broker <args>``. calfcord's substrate defaults (ephemeral
memory storage, advertised on localhost:9092) are supplied via env so the
operator's shell env and passthrough CLI args still override them.

The ``disco broker`` shim arm runs this WITHOUT ``--env-file``, so — like the
native binary it replaces — the broker does not read ``config/.env``; its config
comes from the process environment and CLI args only.
"""

from __future__ import annotations

import os
import sys
from typing import NoReturn

from calfkit_mesh import ENV_VAR, TansuBinaryNotFound, resolve_broker_bin

_MEMORY_ENGINE = "memory://tansu/"
_DEFAULT_LISTENER = "tcp://localhost:9092"


def _default_env(name: str, value: str) -> None:
    """Set ``name`` to ``value`` unless it already holds a non-empty value.

    Matches the old ``${VAR:-default}`` shell semantics the install shim used:
    an empty string counts as unset (unlike ``dict.setdefault``).
    """
    if not os.environ.get(name):
        os.environ[name] = value


def _die(detail: str) -> NoReturn:
    """Print a single branded ``disco broker:`` line to stderr and exit non-zero.

    Callers pass ``str(exc) or type(exc).__name__`` so the operator never sees a
    bare prefix even if the dependency raised with an empty message.
    """
    print(f"disco broker: {detail}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    try:
        # Absolute path. Raises TansuBinaryNotFound if unresolved, and OSError if
        # the first-run extraction (mkdir/copy/chmod) faults.
        tansu = resolve_broker_bin()
    except (TansuBinaryNotFound, OSError) as exc:
        _die(str(exc) or type(exc).__name__)

    _default_env("STORAGE_ENGINE", _MEMORY_ENGINE)
    _default_env("ADVERTISED_LISTENER_URL", _DEFAULT_LISTENER)

    # The bundled binary is memory-only. If it was chosen (no operator override
    # via ENV_VAR) yet a non-memory engine was requested, refuse to boot into an
    # opaque crash-loop under Process Compose's ``restart: always`` — warn and
    # force memory, pointing persistence users at the override.
    storage = os.environ["STORAGE_ENGINE"]
    if not os.environ.get(ENV_VAR) and not storage.startswith("memory:"):
        print(
            f"disco broker: bundled broker is memory-only; ignoring STORAGE_ENGINE={storage!r}. "
            f"For persistence, set {ENV_VAR} to a full Tansu binary or use the Docker broker.",
            file=sys.stderr,
        )
        os.environ["STORAGE_ENGINE"] = _MEMORY_ENGINE

    # execv replaces this process and only returns by raising — e.g. a wrong-arch
    # / wrong-libc $CALF_TANSU_BIN (ENOEXEC) or a noexec mount (EACCES), which
    # resolve_broker_bin cannot detect. Give that the same branded message rather
    # than an opaque traceback that repeats under restart: always.
    try:
        os.execv(tansu, [tansu, "broker", *sys.argv[1:]])
    except OSError as exc:
        _die(f"cannot exec {tansu!r}: {str(exc) or type(exc).__name__}")


if __name__ == "__main__":
    main()

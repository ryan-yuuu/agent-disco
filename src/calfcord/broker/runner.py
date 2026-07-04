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

from calfkit_mesh import TansuBinaryNotFound, resolve_broker_bin

_MEMORY_ENGINE = "memory://tansu/"
_DEFAULT_LISTENER = "tcp://localhost:9092"


def _default_env(name: str, value: str) -> None:
    """Set ``name`` to ``value`` unless it already holds a non-empty value.

    Matches the old ``${VAR:-default}`` shell semantics the install shim used:
    an empty string counts as unset (unlike ``dict.setdefault``).
    """
    if not os.environ.get(name):
        os.environ[name] = value


def main() -> None:
    try:
        # Absolute path. Raises TansuBinaryNotFound if unresolved, and OSError if
        # the first-run extraction (mkdir/copy/chmod) faults.
        tansu = resolve_broker_bin()
    except (TansuBinaryNotFound, OSError) as exc:
        print(f"disco broker: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

    _default_env("STORAGE_ENGINE", _MEMORY_ENGINE)
    _default_env("ADVERTISED_LISTENER_URL", _DEFAULT_LISTENER)

    # The bundled binary is memory-only. If it was chosen (no $CALF_TANSU_BIN
    # override) yet a non-memory engine was requested, refuse to boot into an
    # opaque crash-loop under Process Compose's ``restart: always`` — warn and
    # force memory, pointing persistence users at $CALF_TANSU_BIN.
    if not os.environ.get("CALF_TANSU_BIN") and not os.environ["STORAGE_ENGINE"].startswith("memory:"):
        print(
            "disco broker: bundled broker is memory-only; ignoring "
            f"STORAGE_ENGINE={os.environ['STORAGE_ENGINE']!r}. For persistence, set "
            "CALF_TANSU_BIN to a full Tansu binary or use the Docker broker.",
            file=sys.stderr,
        )
        os.environ["STORAGE_ENGINE"] = _MEMORY_ENGINE

    os.execv(tansu, [tansu, "broker", *sys.argv[1:]])


if __name__ == "__main__":
    main()

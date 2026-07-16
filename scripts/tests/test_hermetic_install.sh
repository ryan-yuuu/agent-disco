#!/usr/bin/env bash
#
# Real-network guard for the installer's central promise: on a box with NO
# Python, `install.sh` must build a working env on an interpreter it OWNS, at the
# exact pinned patch.
#
# Nothing else covers this. `tests/test_install_sh.py` and
# `scripts/tests/test_installer.sh` stub `uv` and are network-free by design, and
# GitHub's runners ship Python — so the claim "you don't need Python installed
# first" (docs/installation.md) was asserted nowhere. It is also the only guard
# that catches the uv/CPython pin drifting apart: a PYTHON_VERSION the pinned uv
# does not know fails here (`No interpreter found ... in managed installations`)
# rather than in an operator's terminal. See docs/adr/0023.
#
# Run it in a container with no python3 (CI does; see .github/workflows/installer.yml):
#   docker run --rm -v "$PWD:/w" -w /w debian:bookworm-slim \
#     sh -c 'apt-get update && apt-get install -y bash curl ca-certificates tar &&
#            bash scripts/tests/test_hermetic_install.sh'
#
# The source download is stubbed to the CHECKOUT rather than GitHub's archive, so
# this guards the commit under test instead of whatever is already on main.
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

# The whole point is a box without Python; on one that has it, this proves nothing.
if command -v python3 >/dev/null 2>&1 || command -v python >/dev/null 2>&1; then
  fail "this guard must run where no python/python3 exists (found: $(command -v python3 || command -v python))"
fi

export CALFCORD_HOME="$SANDBOX/.agent-disco"
export UV_PYTHON_INSTALL_DIR="$SANDBOX/pythons"  # keep the interpreter download inside the sandbox

# `main` is guarded off when sourced, so the individual steps can be driven here.
# shellcheck source=../install.sh
. "$REPO_ROOT/scripts/install.sh"

# Build the commit under test, not the published one. `.git` and a developer's
# `.venv` are excluded: the former is huge and unused, the latter would drop a
# foreign env into the version dir and mask exactly what we are measuring.
extract_source() {
  mkdir -p "$2"
  ( cd "$REPO_ROOT" && tar -cf - --exclude=./.git --exclude=./.venv . ) | tar -xf - -C "$2"
}

SHA="0000000000000000000000000000000000000000"

ensure_uv
[ -x "$CALFCORD_HOME/bin/uv" ] || fail "installer did not bootstrap its own uv"

install_version "$SHA"
[ -f "$INSTALLED_DEST/.calfcord-ok" ] || fail "build produced no .calfcord-ok marker"

interpreter_is_owned "$INSTALLED_DEST" \
  || fail "built env is not on an owned interpreter (bound to: $(venv_interpreter_home "$INSTALLED_DEST"))"

# The env must actually RUN, on exactly the pinned patch — `pyvenv.cfg` says what
# uv intended, this says what the bridge and agents will really execute.
actual="$("$UV" run --frozen --no-sync --project "$INSTALLED_DEST" -- \
  python -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
[ "$actual" = "$PYTHON_VERSION" ] || fail "env runs Python $actual, expected the pinned $PYTHON_VERSION"

# A console script the supervisor actually launches must import and run.
"$UV" run --frozen --no-sync --project "$INSTALLED_DEST" -- calfkit-agent --help >/dev/null \
  || fail "calfkit-agent could not start in the built env"

printf 'ok: no Python on the box -> owned CPython %s, agent runner boots\n' "$actual"

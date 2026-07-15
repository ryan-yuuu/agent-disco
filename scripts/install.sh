#!/usr/bin/env bash
#
# Agent Disco installer — native, no-prerequisites, reproducible one-line install.
#
#   curl -fsSL https://raw.githubusercontent.com/ryan-yuuu/agent-disco/main/scripts/install.sh | bash
#
# What it does, making NO assumptions about the box (no git, no system Python):
#   1. bootstraps `uv` (a static binary) privately under ~/.agent-disco
#   2. pins + downloads the source for a single commit of `main` (tarball, no git)
#   3. builds an isolated, locked venv with `uv sync --locked --no-dev`
#   4. installs a `disco` command that thinly wraps `uv run` in that venv
#
# Each version is built in its own `versions/<sha>` dir (Python venvs are not
# relocatable, so they must be built in their final home); a `current` symlink
# is flipped only after a build succeeds, making activation atomic and rollback
# a symlink flip. The command surface is a pure passthrough — `disco <x>`
# forwards `<x>` to `uv run`, so new entry points need no installer changes.
#
# Env knobs:
#   CALFCORD_HOME   install root          (default: ~/.agent-disco)
#   CALFCORD_REF    branch or commit SHA  (default: main)
#   CALFCORD_REPO   owner/repo            (default: ryan-yuuu/agent-disco)
#   GITHUB_TOKEN    optional, for API rate limits / private mirrors
#   CALFCORD_VERBOSE  set to restore the step-by-step progress narration
#                     (default: quiet — only the final ACTIVATE hint prints)
#
set -Eeuo pipefail

# ------------------------------------------------------------------ config ---
REPO="${CALFCORD_REPO:-ryan-yuuu/agent-disco}"
REF="${CALFCORD_REF:-main}"
CALFCORD_HOME="${CALFCORD_HOME:-$HOME/.agent-disco}"

BIN_DIR="$CALFCORD_HOME/bin"          # private uv (NOT placed on PATH)
SHIM_DIR="$CALFCORD_HOME/shims"       # disco + disco-self (placed on PATH)
VERSIONS_DIR="$CALFCORD_HOME/versions"
CONFIG_DIR="$CALFCORD_HOME/config"
CONFIG_ENV="$CONFIG_DIR/.env"
CONFIG_SETTINGS="$CONFIG_DIR/settings.json"
AGENTS_DIR="$CALFCORD_HOME/agents"            # operator's agent .md files (stable across updates)
CURRENT_LINK="$CALFCORD_HOME/current"

# Process supervisor: pinned F1bonacc1/process-compose release, downloaded into
# BIN_DIR/process-compose. Pin matches the Phase-0 gate (docs §13.2): the REST
# update semantics and the disabled-slot start path are version-specific.
PROCESS_COMPOSE_VERSION="${CALFCORD_PROCESS_COMPOSE_VERSION:-v1.110.0}"
VERSION_FILE="$CALFCORD_HOME/version"

API_BASE="https://api.github.com/repos/$REPO"
DL_BASE="https://github.com/$REPO"

UV=""            # resolved by ensure_uv
INSTALLED_DEST=""   # set by install_version
PREVIOUS_SHA=""     # set by activate_version (for GC)
SEEDED_STARTER=0    # set by seed_agents when it drops in the starter agent
PROCESS_COMPOSE_OK=0  # set by ensure_process_compose when the supervisor binary is in place
PATH_WIRED=0        # set by ensure_path when it wires PATH (so activation is needed)
SYMLINK_CREATED=""  # set by link_onto_path to the dir where `disco` became reachable now

# ---------------------------------------------------------------------- ui ---
if [ -t 2 ]; then
  C_I=$'\033[1;36m'; C_W=$'\033[1;33m'; C_E=$'\033[1;31m'; C_0=$'\033[0m'
else
  C_I=''; C_W=''; C_E=''; C_0=''
fi
# Step-by-step progress is muted by default so a clean install ends on the single
# ACTIVATE hint; export CALFCORD_VERBOSE=1 to restore the full trace when debugging
# a failed install. It must return 0 even when muted: under `set -e` plus the ERR
# trap below, a non-zero return from a silenced call would abort the install.
log()  { [ -n "${CALFCORD_VERBOSE:-}" ] || return 0; printf '%sdisco%s %s\n' "$C_I" "$C_0" "$*" >&2; }
# note() always prints — it carries the one message the operator must act on.
note() { printf '%sdisco%s %s\n' "$C_I" "$C_0" "$*" >&2; }
warn() { printf '%sdisco%s %s\n' "$C_W" "$C_0" "$*" >&2; }
die()  { printf '%sdisco error%s %s\n' "$C_E" "$C_0" "$*" >&2; exit 1; }
trap 'die "install failed: $BASH_COMMAND"' ERR

have() { command -v "$1" >/dev/null 2>&1; }

# True if this uv supports the flags the disco shim relies on (notably
# `uv run --env-file`, a relatively recent addition).
uv_supported() { "$1" run --help 2>/dev/null | grep -q -- '--env-file'; }

# fetch URL [accept] -> response body on stdout. Single home for the
# curl/wget + optional-auth matrix. For curl, --location-trusted keeps the
# auth header across GitHub's github.com -> codeload redirect (private mirrors).
fetch() {
  local url="$1" accept="${2:-}"
  local acc=() auth=()
  if have curl; then
    if [ -n "$accept" ]; then acc=(-H "Accept: $accept"); fi
    if [ -n "${GITHUB_TOKEN:-}" ]; then auth=(--location-trusted -H "Authorization: Bearer $GITHUB_TOKEN"); fi
    curl -fsSL "${acc[@]+"${acc[@]}"}" "${auth[@]+"${auth[@]}"}" "$url"
  elif have wget; then
    if [ -n "$accept" ]; then acc=(--header="Accept: $accept"); fi
    if [ -n "${GITHUB_TOKEN:-}" ]; then auth=(--header="Authorization: Bearer $GITHUB_TOKEN"); fi
    wget -qO- "${acc[@]+"${acc[@]}"}" "${auth[@]+"${auth[@]}"}" "$url"
  else
    die "need curl or wget"
  fi
}

require_bash() {
  [ -n "${BASH_VERSION:-}" ] || die "this installer needs bash; run: curl -fsSL <url> | bash"
}

# ------------------------------------------------------------------- steps ---

# Echo the bare 40-char commit SHA for a ref (no git; GitHub returns it directly
# via the application/vnd.github.sha media type).
resolve_sha() {
  local ref="$1"
  local sha
  sha="$(fetch "$API_BASE/commits/$ref" 'application/vnd.github.sha')"
  case "$sha" in
    "" | *[!0-9a-f]*) die "could not resolve '$ref' to a commit (got: ${sha:0:60})" ;;
  esac
  [ "${#sha}" -eq 40 ] || die "resolved '$ref' to a non-commit value (${#sha} chars): ${sha:0:60}"
  printf '%s' "$sha"
}

# Stream the source tarball for a SHA into DEST, stripping the top-level dir.
extract_source() {
  local sha="$1" dest="$2"
  mkdir -p "$dest"
  fetch "$DL_BASE/archive/$sha.tar.gz" | tar -xz -C "$dest" --strip-components=1
}

# Bootstrap uv privately, or reuse an existing one.
ensure_uv() {
  if [ -x "$BIN_DIR/uv" ]; then
    UV="$BIN_DIR/uv"
  elif have uv && uv_supported "$(command -v uv)"; then
    UV="$(command -v uv)"
    log "using existing uv at $UV"
  else
    if have uv; then
      warn "system uv lacks 'uv run --env-file'; installing a private uv under $BIN_DIR"
    else
      log "installing uv (no system Python or git required)..."
    fi
    mkdir -p "$BIN_DIR"
    if have curl; then
      curl -LsSf https://astral.sh/uv/install.sh | env UV_UNMANAGED_INSTALL="$BIN_DIR" sh
    elif have wget; then
      wget -qO- https://astral.sh/uv/install.sh | env UV_UNMANAGED_INSTALL="$BIN_DIR" sh
    else
      die "need curl or wget to install uv"
    fi
    UV="$BIN_DIR/uv"
  fi
  [ -x "$UV" ] || die "uv unavailable after bootstrap"
}

# Download the pinned process-compose supervisor binary into
# $BIN_DIR/process-compose. Best-effort: an unsupported platform or any
# download/extract/placement failure WARNS and leaves PROCESS_COMPOSE_OK=0 rather
# than aborting the install — Agent Disco can still run its processes manually
# (`disco run …`) or under Docker without the native supervisor. This is the one
# native binary the installer still fetches by hand (the Tansu broker now ships as
# the calfkit-mesh wheel dependency); process-compose has no first-party wheel.
# Layout notes, verified against the real v1.110.0 release assets:
#   * os/arch use the Go-style names process-compose ships (darwin/linux,
#     arm64/amd64).
#   * the binary sits at the TARBALL ROOT (./process-compose), not under bin/.
# Windows ships a .zip and is intentionally not bootstrapped (use WSL/Docker).
ensure_process_compose() {
  if [ -x "$BIN_DIR/process-compose" ]; then
    PROCESS_COMPOSE_OK=1
    log "using existing process-compose at $BIN_DIR/process-compose"
    return 0
  fi
  local os arch
  case "$(uname -s)" in
    Darwin) os="darwin" ;;
    Linux) os="linux" ;;
    *) warn "no native process-compose for $(uname -s); run components manually (disco run …) or use Docker"; return 0 ;;
  esac
  case "$(uname -m)" in
    arm64 | aarch64) arch="arm64" ;;
    x86_64 | amd64) arch="amd64" ;;
    *) warn "no native process-compose for CPU $(uname -m); run components manually (disco run …) or use Docker"; return 0 ;;
  esac
  local asset="process-compose_${os}_${arch}.tar.gz"
  # Capital F1bonacc1 is the actual GitHub org name — not a typo.
  local url="https://github.com/F1bonacc1/process-compose/releases/download/$PROCESS_COMPOSE_VERSION/$asset"
  log "installing process-compose $PROCESS_COMPOSE_VERSION ($os/$arch) ..."
  mkdir -p "$BIN_DIR"
  local tmp
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/calfcord-pc.XXXXXX")"
  # Extract the whole tarball to a temp dir then move just the binary — most
  # portable across GNU/BSD tar (avoids strip-components + leading-./ quirks).
  if ! fetch "$url" | tar -xz -C "$tmp"; then
    rm -rf "$tmp"
    warn "failed to download process-compose from $url; native supervisor unavailable (run components manually or use Docker)"
    return 0
  fi
  if [ ! -f "$tmp/process-compose" ]; then
    rm -rf "$tmp"
    warn "process-compose tarball did not contain process-compose (release layout changed?); native supervisor unavailable"
    return 0
  fi
  # Guard the placement like every other step: a filesystem fault moving the
  # OPTIONAL supervisor binary must not trip the ERR trap and abort the install.
  if ! { mv "$tmp/process-compose" "$BIN_DIR/process-compose" && chmod +x "$BIN_DIR/process-compose"; }; then
    rm -rf "$tmp"
    warn "failed to install process-compose into $BIN_DIR (filesystem/permissions?); native supervisor unavailable (run components manually or use Docker)"
    return 0
  fi
  rm -rf "$tmp"
  # macOS quarantines downloaded binaries; clear it so first launch isn't blocked.
  if [ "$os" = "darwin" ] && have xattr; then
    xattr -d com.apple.quarantine "$BIN_DIR/process-compose" 2>/dev/null || true
  fi
  if [ -x "$BIN_DIR/process-compose" ]; then
    PROCESS_COMPOSE_OK=1
    log "process-compose installed at $BIN_DIR/process-compose"
  else
    warn "process-compose binary not executable after install; native supervisor unavailable"
  fi
  return 0
}

# Pre-extract the calfkit-mesh-bundled Tansu binary now (a one-time copy to
# ~/.calfkit/bin) so the supervised broker never does it inside Process Compose's
# restart-always loop on first `disco start`. Best-effort: a failure here
# (read-only/full disk, permissions) WARNS and proceeds — the wheel is already
# installed and `disco doctor` re-checks. The `if` guard keeps a non-zero result
# from tripping the ERR trap and aborting the install.
warm_broker_cache() {
  local dest="$1"
  if "$UV" run --frozen --no-sync --project "$dest" -- \
      python -c "import calfkit_mesh; calfkit_mesh.resolve_broker_bin()" >/dev/null 2>&1; then
    log "broker binary ready (calfkit-mesh)"
  else
    warn "could not pre-extract the Tansu broker binary; 'disco broker' will retry on first use (check: disco doctor)"
  fi
}

# Build versions/<sha> in place (idempotent). Sets INSTALLED_DEST.
install_version() {
  local sha="$1"
  local dest="$VERSIONS_DIR/$sha"
  INSTALLED_DEST="$dest"
  if [ -f "$dest/.calfcord-ok" ]; then
    log "version ${sha:0:12} already built — reusing"
    return 0
  fi
  log "downloading source @ ${sha:0:12} ..."
  rm -rf "$dest"
  extract_source "$sha" "$dest"
  [ -f "$dest/pyproject.toml" ] || die "extracted source looks wrong (no pyproject.toml)"
  log "building isolated environment (uv sync --locked --no-dev) ..."
  ( cd "$dest" && "$UV" sync --locked --no-dev )
  : > "$dest/.calfcord-ok"
}

# Copy .env.example -> config/.env once; never clobber an operator's edits.
# Also seed an empty mcp.json beside it (same once-only rule): the MCP CLI and
# the compose generator read it, and 0600 because entries may carry literal
# credentials.
seed_config() {
  local dest="$1"
  mkdir -p "$CONFIG_DIR"
  if [ ! -f "$CONFIG_DIR/mcp.json" ]; then
    printf '{\n  "mcpServers": {}\n}\n' > "$CONFIG_DIR/mcp.json"
    chmod 600 "$CONFIG_DIR/mcp.json"
    log "seeded MCP config at $CONFIG_DIR/mcp.json (add servers with: disco mcp add)"
  fi
  if [ ! -f "$CONFIG_SETTINGS" ]; then
    printf '{\n  "sticky_replies": {\n    "enabled": true\n  }\n}\n' > "$CONFIG_SETTINGS"
    chmod 600 "$CONFIG_SETTINGS"
    log "seeded bridge settings at $CONFIG_SETTINGS"
  fi
  if [ -f "$CONFIG_ENV" ]; then
    log "keeping existing config at $CONFIG_ENV"
    return 0
  fi
  if [ -f "$dest/.env.example" ]; then
    cp "$dest/.env.example" "$CONFIG_ENV"
  else
    : > "$CONFIG_ENV"
  fi
  chmod 600 "$CONFIG_ENV"
  log "seeded config at $CONFIG_ENV (fill in DISCORD_*, CALF_HOST_URL, API keys)"
}

# Give the native install a stable home for agent definitions, and drop in the
# bundled starter agent on first install. ``calfkit-agent`` resolves the agents
# dir from CALFKIT_AGENTS_DIR — the shim points it at $AGENTS_DIR
# ($CALFCORD_HOME/agents), so this pre-creates exactly the dir the runtime uses.
# It lives outside the GC'd ``versions/<sha>`` tree to survive ``disco self
# update``. Seeding only happens when the agents dir is empty, so an operator who
# removed the starter (or added their own agents) is never clobbered on re-install.
seed_agents() {
  local dest="$1"
  mkdir -p "$AGENTS_DIR"
  if [ -n "$(ls -A "$AGENTS_DIR" 2>/dev/null)" ]; then
    log "keeping existing agents in $AGENTS_DIR"
    return 0
  fi
  if [ -f "$dest/agents/assistant.md" ]; then
    cp "$dest/agents/assistant.md" "$AGENTS_DIR/assistant.md"
    SEEDED_STARTER=1
    log "seeded starter agent at $AGENTS_DIR/assistant.md"
  else
    warn "no starter agent in source; create one with: disco init"
  fi
}

# Read one field from the existing version marker by PARSING, never sourcing
# (a repo/ref value could contain shell metacharacters) — mirrors the shim's meta().
_version_field() {
  local key="$1" line
  [ -f "$VERSION_FILE" ] || return 0
  while IFS= read -r line; do
    case "$line" in "$key="*) printf '%s' "${line#*=}"; return 0 ;; esac
  done < "$VERSION_FILE"
  return 0
}

# Flip the current symlink atomically and record the version marker.
activate_version() {
  local dest="$1" sha now old_sha
  sha="$(basename "$dest")"
  old_sha=""
  if [ -L "$CURRENT_LINK" ]; then
    old_sha="$(basename "$(readlink "$CURRENT_LINK")")"
  fi
  # Re-activating the SAME sha — a no-op re-install, or `self update` when already
  # current (it has no up-to-date short-circuit) — must NOT make the version its
  # own predecessor: that records prev == current and then `gc_versions` deletes
  # the genuine rollback target. Keep the existing previous in that case; otherwise
  # the outgoing sha becomes the new previous.
  if [ "$old_sha" = "$sha" ]; then
    PREVIOUS_SHA="$(_version_field CALFCORD_PREVIOUS_COMMIT)"
  else
    PREVIOUS_SHA="$old_sha"
  fi
  ln -sfn "$dest" "$CURRENT_LINK"
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  cat > "$VERSION_FILE" <<EOF
CALFCORD_COMMIT=$sha
CALFCORD_INSTALLED_AT=$now
CALFCORD_REPO=$REPO
CALFCORD_REF=$REF
CALFCORD_PREVIOUS_COMMIT=$PREVIOUS_SHA
EOF
}

# Keep only current + previous version dirs.
gc_versions() {
  local cur="$1" prev="${2:-}" d b
  for d in "$VERSIONS_DIR"/*/; do
    [ -d "$d" ] || continue
    b="$(basename "$d")"
    [ "$b" = "$cur" ] && continue
    [ -n "$prev" ] && [ "$b" = "$prev" ] && continue
    log "pruning old version ${b:0:12}"
    rm -rf "$d"
  done
}

write_shims() {
  mkdir -p "$SHIM_DIR"

  cat > "$SHIM_DIR/disco" <<'CALF_SHIM'
#!/usr/bin/env bash
# disco — thin passthrough to `uv run` inside the pinned install.
# `disco <command> [args]` runs any console script in the locked env;
# `disco self ...` handles install management. New entry points need no
# changes here.
set -euo pipefail
# shellcheck disable=SC2154  # rc is assigned by rc=$? at the start of the trap body
trap 'rc=$?; printf "disco: failed (exit %s): %s\n" "$rc" "$BASH_COMMAND" >&2; exit "$rc"' ERR

H="${CALFCORD_HOME:-$HOME/.agent-disco}"
export CALFCORD_HOME="$H"  # so calfcord-cli can locate config/.env and the agents dir

if [ "${1:-}" = "self" ]; then
  shift
  exec "$H/shims/disco-self" "$@"
fi

usage() {
  cat <<'USAGE'
usage:
  disco init                  guided setup; ends with your first agent live in Discord
  disco doctor                check config, broker, Discord token/app id, and agents
  disco start                 open the workspace (broker + bridge — the always-on substrate)
  disco stop                  stop the local org
  disco status                show what's running locally
  disco logs [component] [-f] tail unified or per-component logs
  disco explain topology      explain how the pieces split, and why
  disco deploy <systemd|k8s|docker> [-o PATH]
                              generate deployment manifests (advanced)
  disco broker                run a local Tansu broker (ephemeral, localhost:9092)
  disco run <bridge|agent|tools|mcp>
                              run an Agent Disco process in the pinned env
  disco agent <create|list|show|edit|set|rename|delete|tools> [<name>]
                              manage agents (create/inspect/edit/rename/delete)
  disco agent <start|stop|restart> [<name>|--all]
                              clock an agent (or every agent on this host) in/out/reload
  disco tools <start|stop|restart> [--all]
                              bring the tools host online / offline / reload
  disco bridge restart        restart the bridge in place (recover a wedged reader / apply a new build)
  disco mcp <add|list|remove> [<server>]
                              manage MCP servers in mcp.json
  disco mcp <start|stop|restart> <server>|--all
                              bring MCP servers online / offline / reload
  disco auth [args]           Codex (ChatGPT subscription) login
  disco self <version|status|update|rollback|set-broker>
USAGE
}

# Explicit help -> stdout, exit 0; a bare invocation -> usage on stderr, exit 2.
# (stdout-for-help diverges from disco-self, which writes help to stderr; intentional.)
case "${1:-}" in
  -h|--help|help) usage; exit 0 ;;
  "") usage >&2; exit 2 ;;
esac

UV="$H/bin/uv"
if [ ! -x "$UV" ]; then
  UV="$(command -v uv || true)"
fi
{ [ -n "$UV" ] && [ -x "$UV" ]; } || { echo "disco: uv not found; re-run the installer" >&2; exit 1; }
[ -e "$H/current" ] || { echo "disco: no active install at $H/current; re-run the installer" >&2; exit 1; }

# `disco broker` runs the calfcord-broker console script in the pinned venv,
# which resolves the calfkit-mesh-bundled Tansu binary and runs it with calfcord
# defaults (ephemeral memory storage on localhost:9092). Deliberately NOT via
# --env-file: like the native binary it replaced, the broker does not read
# config/.env — its config comes from the process env and passthrough args only.
if [ "${1:-}" = "broker" ]; then
  shift
  exec "$UV" run --frozen --no-sync --project "$H/current" -- calfcord-broker "$@"
fi

ENVF="$H/config/.env"

# Default Agent Disco's runtime dirs into the install layout unless the operator
# already chose them (shell env OR config .env wins — checked here so we don't
# depend on `uv run --env-file` precedence). Agents and per-agent state live
# under the install home so they survive `self update` and are found from any
# directory; the tools workspace defaults to the *launch* directory so agents
# act where you ran the command (like Claude Code). Override any of these in
# config/.env.
#
# The `^$1=.` grep requires at least one char after the `=`: a bare `KEY=`
# (which `.env.example` ships for CALFCORD_WORKSPACE_DIR) counts as UNSET, so
# the workspace still defaults to $PWD. An operator must give a real value to
# override the default.
_default_env() {  # name default
  [ -n "${!1:-}" ] && return 0
  [ -f "$ENVF" ] && grep -q "^$1=." "$ENVF" && return 0
  export "$1=$2"
}
_default_env CALFKIT_AGENTS_DIR     "$H/agents"
_default_env CALFCORD_WORKSPACE_DIR "$PWD"

# Translate friendly verbs to the underlying console scripts. Management verbs go to the
# calfcord-cli argparse entry point; raw `disco calfkit-*` runner names aren't matched
# here and fall through to the `uv run` passthrough below, so they keep working unchanged.
case "${1:-}" in
  # Management + day-to-day lifecycle verbs all resolve to the calfcord-cli
  # argparse entry point. `start|stop|status` drive the process-compose
  # supervisor; `_healthcheck` is the readiness-probe command PC's exec probes
  # invoke (`disco _healthcheck <component>`). These are listed explicitly so
  # they don't fall through to the `uv run` passthrough (which would try to exec
  # a nonexistent `start`/`stop`/… console script). `tools` is a calfcord-cli
  # verb group (the singleton tools-host lifecycle: `tools start|stop|restart`);
  # `mcp` is too (per-server MCP lifecycle + mcp.json management).
  # The graduation-tier verbs (`explain` / `logs` /
  # `deploy`) are calfcord-cli subcommands too — listed here so their sub-args
  # forward verbatim to the argparse entry point instead of the `uv run` passthrough.
  # `bridge` (the substrate slot's `bridge restart`) is a management verb here — it
  # is distinct from `disco run bridge` (the raw `calfkit-bridge` runner) handled by
  # the `run` arm below, so both forms coexist.
  init|agent|tools|mcp|bridge|doctor|_healthcheck|start|stop|status|logs|explain|deploy) set -- calfcord-cli "$@" ;;
  run)
    shift
    case "${1:-}" in
      bridge|agent|tools|mcp) set -- "calfkit-$1" "${@:2}" ;;
      -h|--help) usage; exit 0 ;;
      *) usage >&2; exit 2 ;;
    esac ;;
  auth) shift; set -- calfkit-auth "$@" ;;
esac

if [ -f "$ENVF" ]; then
  exec "$UV" run --frozen --no-sync --project "$H/current" --env-file "$ENVF" -- "$@"
else
  exec "$UV" run --frozen --no-sync --project "$H/current" -- "$@"
fi
CALF_SHIM

  cat > "$SHIM_DIR/disco-self" <<'CALF_SELF'
#!/usr/bin/env bash
# disco self-management: version | status | update | rollback | set-broker
set -euo pipefail
trap 'rc=$?; printf "disco self: failed (exit %s): %s\n" "$rc" "$BASH_COMMAND" >&2; exit "$rc"' ERR

H="${CALFCORD_HOME:-$HOME/.agent-disco}"
VERSION_FILE="$H/version"
VERSIONS_DIR="$H/versions"
CURRENT_LINK="$H/current"
CONFIG_ENV="$H/config/.env"

# Read the install marker by PARSING, never sourcing: a ref/repo containing
# shell metacharacters must be treated as data, not executed.
meta() {
  local _line
  [ -f "$VERSION_FILE" ] || return 0
  while IFS= read -r _line; do
    case "$_line" in "$1="*) printf '%s' "${_line#*=}"; return 0 ;; esac
  done < "$VERSION_FILE"
  return 0
}
CALFCORD_COMMIT="$(meta CALFCORD_COMMIT)"
CALFCORD_INSTALLED_AT="$(meta CALFCORD_INSTALLED_AT)"
CALFCORD_REPO="$(meta CALFCORD_REPO)"
CALFCORD_REF="$(meta CALFCORD_REF)"
CALFCORD_PREVIOUS_COMMIT="$(meta CALFCORD_PREVIOUS_COMMIT)"
REPO="${CALFCORD_REPO:-ryan-yuuu/agent-disco}"

short() { printf '%s' "${1:0:12}"; }

remote_sha() {
  local ref="${1:-main}"
  local url="https://api.github.com/repos/$REPO/commits/$ref"
  if command -v curl >/dev/null 2>&1; then
    if [ -n "${GITHUB_TOKEN:-}" ]; then
      curl -fsSL -H 'Accept: application/vnd.github.sha' -H "Authorization: Bearer $GITHUB_TOKEN" "$url"
    else
      curl -fsSL -H 'Accept: application/vnd.github.sha' "$url"
    fi
  elif command -v wget >/dev/null 2>&1; then
    if [ -n "${GITHUB_TOKEN:-}" ]; then
      wget -qO- --header='Accept: application/vnd.github.sha' --header="Authorization: Bearer $GITHUB_TOKEN" "$url"
    else
      wget -qO- --header='Accept: application/vnd.github.sha' "$url"
    fi
  else
    echo "disco self: need curl or wget" >&2; return 1
  fi
}

cmd="${1:-}"; [ "$#" -gt 0 ] && shift || true
case "$cmd" in
  version)
    echo "commit:       ${CALFCORD_COMMIT:-unknown}"
    echo "installed_at: ${CALFCORD_INSTALLED_AT:-unknown}"
    echo "repo:         $REPO"
    echo "ref:          ${CALFCORD_REF:-main}"
    ;;
  status)
    have="${CALFCORD_COMMIT:-}"
    [ -n "$have" ] || { echo "no install metadata; re-run the installer" >&2; exit 1; }
    ref="${CALFCORD_REF:-main}"
    if ! latest="$(remote_sha "$ref")" || [ -z "$latest" ]; then
      echo "disco self: could not reach GitHub to check for updates (offline or rate-limited)" >&2
      exit 1
    fi
    if [ "$have" = "$latest" ]; then
      echo "up to date ($(short "$have") on $ref)"
    else
      echo "outdated: have $(short "$have"), latest $(short "$latest") on $ref"
      echo "run 'disco self update' to upgrade"
    fi
    ;;
  update)
    url="https://raw.githubusercontent.com/$REPO/main/scripts/install.sh"
    ref="${CALFCORD_REF:-main}"
    echo "disco: updating $REPO ($ref)..." >&2
    tmp="$(mktemp)"
    if command -v curl >/dev/null 2>&1; then
      curl -fsSL "$url" -o "$tmp" || { echo "disco self: update download failed" >&2; rm -f "$tmp"; exit 1; }
    else
      wget -qO- "$url" > "$tmp" || { echo "disco self: update download failed" >&2; rm -f "$tmp"; exit 1; }
    fi
    [ -s "$tmp" ] || { echo "disco self: downloaded installer is empty" >&2; rm -f "$tmp"; exit 1; }
    # Re-run for the SAME ref/repo/home this install used, not a hardcoded main.
    rc=0
    CALFCORD_REPO="$REPO" CALFCORD_REF="$ref" CALFCORD_HOME="$H" bash "$tmp" || rc=$?
    rm -f "$tmp"
    [ "$rc" -eq 0 ] || exit "$rc"
    ;;
  rollback)
    [ -L "$CURRENT_LINK" ] || { echo "no active install to roll back" >&2; exit 1; }
    cur_sha="$(basename "$(readlink "$CURRENT_LINK")")"
    prev="${CALFCORD_PREVIOUS_COMMIT:-}"
    if [ -z "$prev" ] || [ ! -f "$VERSIONS_DIR/$prev/.calfcord-ok" ]; then
      echo "disco self: no valid previous version to roll back to" >&2
      exit 1
    fi
    ln -sfn "$VERSIONS_DIR/$prev" "$CURRENT_LINK"
    now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    cat > "$VERSION_FILE" <<EOF
CALFCORD_COMMIT=$prev
CALFCORD_INSTALLED_AT=$now
CALFCORD_REPO=$REPO
CALFCORD_REF=${CALFCORD_REF:-main}
CALFCORD_PREVIOUS_COMMIT=$cur_sha
EOF
    echo "rolled back to $(short "$prev")"
    ;;
  set-broker)
    val="${1:-}"
    [ -n "$val" ] || { echo "usage: disco self set-broker <host:port>" >&2; exit 2; }
    mkdir -p "$(dirname "$CONFIG_ENV")"
    [ -f "$CONFIG_ENV" ] || { : > "$CONFIG_ENV"; chmod 600 "$CONFIG_ENV"; }
    tmp="$(mktemp)"
    rc=0
    grep -v '^CALF_HOST_URL=' "$CONFIG_ENV" > "$tmp" || rc=$?
    if [ "$rc" -gt 1 ]; then
      echo "disco self: failed to read $CONFIG_ENV (grep exit $rc)" >&2; rm -f "$tmp"; exit 1
    fi
    echo "CALF_HOST_URL=$val" >> "$tmp" || { echo "disco self: failed to write $CONFIG_ENV" >&2; rm -f "$tmp"; exit 1; }
    mv "$tmp" "$CONFIG_ENV"
    chmod 600 "$CONFIG_ENV"
    echo "set CALF_HOST_URL=$val in $CONFIG_ENV"
    ;;
  ""|-h|--help|help)
    cat >&2 <<'USAGE'
disco self <command>:
  version              show installed commit + timestamp
  status               compare installed commit to the latest on the branch
  update               re-run the installer to upgrade to the latest
  rollback             switch back to the previous installed version
  set-broker <host:port>  set CALF_HOST_URL (Kafka bootstrap) in the config .env
USAGE
    [ -z "$cmd" ] && exit 2
    exit 0
    ;;
  *)
    echo "disco self: unknown command '$cmd'" >&2
    exit 2
    ;;
esac
CALF_SELF

  chmod +x "$SHIM_DIR/disco" "$SHIM_DIR/disco-self"

  # Clean cutover: remove any command shims from a pre-rename install so no
  # stale command lingers on PATH (there is no compatibility alias). The glob
  # matches only the old-named shims (the command shim + its self sibling); the
  # fresh disco shims written above are untouched.
  rm -f "$SHIM_DIR"/calfcord* 2>/dev/null || true
}

# True when DIR is already a component of the caller's $PATH.
path_has_dir() {
  case ":$PATH:" in
    *":$1:"*) return 0 ;;
    *) return 1 ;;
  esac
}

# Symlink the `disco` shim into the first candidate dir (args, in preference
# order) that the caller's shell ALREADY searches and that we can write.
#
# This is the only way a `curl | bash` installer can make its command usable in
# the CURRENT terminal: the installer is a child process, so it can never mutate
# its parent's PATH — but PATH is resolved at *lookup* time, so a file appearing
# in an already-listed dir is found by the running shell with no env change and
# no `rehash`. When nothing qualifies (a stock macOS has no writable dir on
# PATH), this is a no-op and the profile hooks below carry the install instead.
#
# Best-effort throughout: every rejection just leaves SYMLINK_CREATED empty. We
# never escalate to sudo — a piped installer has no stdin to prompt on.
#
# The `-x` / `command -v` checks below assert "the shell that ran the installer
# can run this", so they are only as true as that shell's identity. Under a
# `curl | sudo bash` they run as root (whose $HOME sudo may reset to /root),
# which is not the operator's shell — an unsupported way to install this, and
# the reason nothing here tries to earn root in the first place.
link_onto_path() {
  local candidate link resolved
  for candidate in "$@"; do
    path_has_dir "$candidate" || continue
    # PATH says the shell searches here, so creating the dir honours that rather
    # than inventing policy: Fedora/RHEL put ~/.local/bin on PATH whether or not
    # it exists. A failure (a root-owned /usr/local) just tries the next.
    [ -d "$candidate" ] || mkdir -p "$candidate" 2>/dev/null || continue
    [ -w "$candidate" ] || continue
    link="$candidate/disco"
    # Only ever replace a link we already own. Anything else is another tool's
    # `disco` on the operator's PATH, and clobbering it would hijack an
    # unrelated command. `-e` is false for a dangling link, so `-L` is tested
    # too — otherwise a broken foreign link would look like an empty slot.
    if [ -e "$link" ] || [ -L "$link" ]; then
      [ "$(readlink "$link" 2>/dev/null || true)" = "$SHIM_DIR/disco" ] || continue
    fi
    ln -sfn "$SHIM_DIR/disco" "$link" 2>/dev/null || continue
    # Claiming READY means "typing `disco` runs THIS", which is a stronger fact
    # than "ln succeeded" — and the only one the operator cares about. A link
    # that resolves to nothing (a relative CALFCORD_HOME makes the target
    # relative to the LINK's dir, not ours) is our own garbage, so remove it.
    if [ ! -x "$link" ]; then
      rm -f "$link" 2>/dev/null || true
      continue
    fi
    # ...and the shell resolves in PATH order, not our preference order, so an
    # earlier PATH entry holding another `disco` shadows us.
    hash -r 2>/dev/null || true
    resolved="$(command -v disco 2>/dev/null || true)"
    if [ "$resolved" != "$link" ]; then
      # Say it out loud: this is the one case NEITHER message can fix. READY
      # would drive someone else's tool, and the ACTIVATE fallback is no better
      # — sourcing `env` prepends SHIM_DIR, but a macOS login shell then runs
      # /etc/zprofile's path_helper(8), which reorders PATH and demotes SHIM_DIR
      # to LAST, so the other `disco` keeps winning even after a restart. The
      # link stays (it is correct, and wins the moment the other one goes).
      warn "another 'disco' at $resolved takes precedence on your PATH — remove it, or run $link directly"
      continue
    fi
    SYMLINK_CREATED="$candidate"
    log "linked $link -> $SHIM_DIR/disco"
    return 0
  done
  return 0
}

# Where zsh actually reads .zshenv from. ZDOTDIR redirects it, and ZDOTDIR is
# typically not exported, so the installer's own (bash) env cannot answer this —
# ask zsh, as rustup does.
#
# `-f` is load-bearing. zsh locates .zshenv from /etc/zshenv or the inherited
# env ONLY, then reads it once; it never re-reads .zshenv from a ZDOTDIR that
# file set. `-f` (NO_RCS) skips the user's rc files while still reading
# /etc/zshenv, which is exactly zsh's own lookup context. A plain `zsh -c` would
# instead source ~/.zshenv and report the ZDOTDIR *it* sets — the widespread XDG
# idiom of a stub ~/.zshenv that only does `export ZDOTDIR=~/.config/zsh` — and
# we would write the hook to a file zsh has already finished looking for. Dead
# code, and a permanently broken ACTIVATE. `-f` also keeps the probe from
# executing (or hanging on) arbitrary rc code just to learn a directory name.
# `-f` cannot skip /etc/zshenv (nothing can — that is where a fleet-managed Mac
# sets ZDOTDIR, so losing it would defeat the probe). A site /etc/zshenv that
# also prints would prepend noise to the capture, so take the last line only:
# `printf %s` emits no trailing newline, making the value always the final line.
# `</dev/null` matters because under `curl | bash` stdin IS the install script —
# a child that reads it would eat the rest of the installer.
zsh_dotdir() {
  local d=""
  if have zsh; then
    d="$(zsh -f -c 'printf %s "${ZDOTDIR:-$HOME}"' </dev/null 2>/dev/null | tail -n 1 || true)"
  fi
  { [ -n "$d" ] && [ -d "$d" ]; } || d="$HOME"
  printf '%s' "$d"
}

# Make `disco` reachable, in two tiers:
#   1. link_onto_path — usable in the shell that ran the installer, no restart.
#   2. the rustup/uv env-file + profile hooks — every future shell, and the sole
#      mechanism on boxes where tier 1 found nothing to link into.
# Both always run: the symlink is one command in one dir and says nothing about
# future shells, so it does not replace the hooks. Idempotent by construction,
# so it's safe on every re-run (including `disco self update`, which re-execs
# this installer).
ensure_path() {
  # Already reachable — an active hook from a prior install, or a hand-wired
  # PATH. Skip everything (this also covers the migration case where an old
  # direct `export PATH=` line is already in effect).
  if path_has_dir "$SHIM_DIR"; then
    return 0
  fi

  link_onto_path "$HOME/.local/bin" "/usr/local/bin"

  # The canonical activation file. The installer owns it, so overwriting on every
  # run keeps it correct. The `case` guard makes it idempotent when sourced and
  # keeps it POSIX-sh / bash-3.2 / zsh compatible. The heredoc is unquoted so
  # SHIM_DIR interpolates, while `\$PATH` stays literal for profile-load-time
  # expansion (expanding it now would bake in the installer's PATH).
  mkdir -p "$CALFCORD_HOME"
  cat > "$CALFCORD_HOME/env" <<EOF
# $CALFCORD_HOME/env — added by the Agent Disco installer
case ":\$PATH:" in
  *":$SHIM_DIR:"*) ;;
  *) export PATH="$SHIM_DIR:\$PATH" ;;
esac
EOF

  # Source the env file from each shell's startup file, creating any that are
  # missing (>> creates). zsh gets .zshenv, NOT .zprofile: .zprofile is read only
  # by *login* shells, which left the hook invisible to a non-login interactive
  # zsh — VS Code's integrated terminal spawns `/bin/zsh -i`, where restarting
  # the terminal never helped. .zshenv is the only zsh startup file read
  # unconditionally, so it is the one target that always gets us on PATH.
  #
  # The trade: .zshenv runs BEFORE /etc/zprofile, whose path_helper(8) rewrites
  # PATH on every macOS login shell and moves SHIM_DIR to the end — so we win on
  # reachability and lose the prepend that .zprofile used to get. That costs
  # nothing unless something else on PATH is also called `disco`, in which case
  # it outranks us here AND after a restart; link_onto_path detects exactly that
  # and warns, because no message this installer prints can fix it.
  #
  # Guard on the exact hook line so re-runs never duplicate it; a pre-existing
  # legacy `export PATH=` line is left alone.
  # The append sits in an `if` BODY, where `set -e` and the ERR trap still bite:
  # a read-only rc file (nix home-manager, chezmoi and stow all produce 444
  # dotfiles, and .zshenv is far likelier to be tool-managed than .zprofile was)
  # would fail the redirect and abort an install that had ALREADY succeeded.
  # Guard it and warn instead — `source $CALFCORD_HOME/env` still activates.
  local rc hook
  hook='. "'"$CALFCORD_HOME"'/env"'
  for rc in "$HOME/.profile" "$HOME/.bashrc" "$(zsh_dotdir)/.zshenv"; do
    grep -qsF "$hook" "$rc" && continue
    # `2>/dev/null` precedes the append deliberately: redirections are applied
    # left to right, and a failing `>>` is reported by the SHELL, not by printf
    # — so putting it second would let bash's raw "Permission denied" through
    # ahead of the warning that actually tells the operator what to do.
    if printf '\n# Agent Disco\n%s\n' "$hook" 2>/dev/null >> "$rc"; then
      log "wired $SHIM_DIR onto PATH via $rc"
    else
      warn "could not write $rc (read-only?) — activate with: source $CALFCORD_HOME/env"
    fi
  done
  PATH_WIRED=1
}

# The one line the operator must act on — or not. Conditional by design: when
# `disco` already resolves in the shell that ran the installer there is nothing
# to activate, and demanding a restart anyway is both noise and untrue.
activation_hint() {
  if [ -n "$SYMLINK_CREATED" ]; then
    note "  READY: 'disco' is on your PATH now — run  disco init"
  elif [ "$PATH_WIRED" -eq 1 ]; then
    note "  ACTIVATE: run  source $CALFCORD_HOME/env   now, or open a new terminal — then 'disco' is on your PATH"
  fi
}

# -------------------------------------------------------------------- main ---
main() {
  require_bash
  log "installing Agent Disco from $REPO @ $REF"
  mkdir -p "$CALFCORD_HOME" "$VERSIONS_DIR"
  ensure_uv
  ensure_process_compose
  local sha
  sha="$(resolve_sha "$REF")"
  log "resolved $REF -> ${sha:0:12}"
  install_version "$sha"
  warm_broker_cache "$INSTALLED_DEST"
  seed_config "$INSTALLED_DEST"
  seed_agents "$INSTALLED_DEST"
  activate_version "$INSTALLED_DEST"
  gc_versions "$sha" "$PREVIOUS_SHA"
  write_shims
  ensure_path
  log "done."
  activation_hint
  log "  version:  disco self version"
  log "  config:   $CONFIG_ENV  (set CALF_HOST_URL, or: disco self set-broker <url>)"
  log "  broker:   disco broker   (Tansu via calfkit-mesh, ephemeral memory, localhost:9092)"
  if [ "$PROCESS_COMPOSE_OK" -eq 1 ]; then
    log "  supervisor: process-compose $PROCESS_COMPOSE_VERSION installed"
  else
    log "  supervisor: process-compose unavailable — run components manually (disco run …) or use Docker"
  fi
  if [ "$SEEDED_STARTER" -eq 1 ]; then
    log "  agents:   $AGENTS_DIR  (starter: assistant.md)"
  else
    log "  agents:   $AGENTS_DIR"
  fi
  log "  check:    disco doctor"
  log "  setup:    disco init      (guided; ends with your first agent live in Discord)"
  log "  status:   disco status    (the org board, once you're up)"
}

# Run main only when executed (``bash install.sh``) or piped (``curl | bash``),
# never when sourced — so tests can source this file to exercise individual
# functions. Piped execution leaves ``BASH_SOURCE[0]`` empty; a file execution
# makes it equal to ``$0``; sourcing makes it a non-empty path that differs from
# ``$0``. The ``:-`` guards keep this safe under ``set -u``.
if [ -z "${BASH_SOURCE[0]:-}" ] || [ "${BASH_SOURCE[0]:-}" = "$0" ]; then
  main "$@"
fi

# Native install (no Docker)

A one-line installer for running calfcord directly on a box — no Docker, and no
prerequisites on the box itself (it bootstraps everything it needs, including
Python). This is the native alternative to the
[Docker quick start](../README.md#quick-start) and the slim per-role images in
[`distributed-deployment.md`](distributed-deployment.md); the
[process model](architecture.md#running-modes) and the Kafka contract are
identical either way.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/ryan-yuuu/calfcord/main/scripts/install.sh | bash
```

The box needs only `curl` (or `wget`) and `bash` — no system Python and no
`git`. The installer:

1. bootstraps [`uv`](https://docs.astral.sh/uv/) privately under `~/.calfcord`;
2. pins `main` to an exact commit and downloads that source as a tarball (no
   `git clone`);
3. builds an isolated, locked environment with `uv sync --locked --no-dev`
   (`uv` provisions Python 3.12 automatically);
4. installs a `calfcord` command into `~/.calfcord/shims` and adds it to your
   `PATH`.

Restart your shell (or `source` your profile) afterwards so `calfcord` is on
your `PATH`. Everything lives under `~/.calfcord`; to uninstall, `rm -rf
~/.calfcord` and remove the `# calfcord` line from your shell profile.

## The `calfcord` command

`calfcord` is a thin wrapper around `uv run` inside the pinned environment, so
**any** calfcord process is just `calfcord <process> [args]`:

```bash
calfcord calfkit-bridge        # the Discord gateway
calfcord calfkit-agent         # run the agents
calfcord calfkit-router        # ambient routing
calfcord calfkit-tools         # tools + the agent-to-agent channel
```

New entry points work automatically — the wrapper forwards everything to `uv
run`, so it never needs updating as calfcord grows.

## Configure

The installer seeds `~/.calfcord/config/.env` from the project's
[`.env.example`](../.env.example) (mode `600`), and every command run through
`calfcord` loads it automatically. Fill in the values that box's role needs
(full reference: [`configuration.md`](configuration.md)) — at minimum the
broker URL that ties the swarm together:

```bash
calfcord self set-broker my-broker.internal:9092   # writes CALF_HOST_URL
# ...or edit ~/.calfcord/config/.env directly
```

> **Where config lives.** Secrets and connection settings (`DISCORD_*`,
> `CALF_HOST_URL`, API keys) belong in this `.env` — that is the 12-factor home
> for per-box, per-deploy values, and it is how calfcord's processes read
> config. Structured, versioned definitions (your agents and tools) stay in
> their Markdown / YAML-frontmatter files. Don't promote secrets into YAML.

## Deploy across hosts

calfcord's processes are location-transparent over Kafka (see
[`distributed-deployment.md`](distributed-deployment.md)). Install calfcord on
each box, point them all at the same broker, and run the role each box owns:

```bash
# box A — bridge + router + agents
calfcord self set-broker broker.tailnet:9092
calfcord calfkit-bridge &
calfcord calfkit-router &
calfcord calfkit-agent &

# box B — remote tools only, same broker
calfcord self set-broker broker.tailnet:9092
calfcord calfkit-tools
```

(For real deployments, run each role under a supervisor — `systemd`, `tmux`, or
similar — rather than `&`.) Because every box installs the same pinned commit,
they all run a schema/topic-compatible build, which matters since the processes
only agree via Kafka topics derived from committed schemas.

## Stay current

```bash
calfcord self version     # installed commit + timestamp
calfcord self status      # compare against the latest commit on main
calfcord self update      # upgrade to the latest main (keeps the previous build)
calfcord self rollback    # switch back to the previous build
```

`update` and `rollback` swap an internal `current` symlink between built
versions, so switching is atomic and the previous version stays on disk for an
instant rollback.

## Pinning a specific version

To install a specific branch or commit instead of the latest `main`:

```bash
curl -fsSL https://raw.githubusercontent.com/ryan-yuuu/calfcord/main/scripts/install.sh | CALFCORD_REF=<branch-or-sha> bash
```

Other knobs: `CALFCORD_HOME` (install root), `CALFCORD_REPO` (install a fork),
`GITHUB_TOKEN` (lift GitHub API rate limits / use a private mirror).

## This is for deploying, not developing

The installer gives you a frozen, pinned build. To hack on calfcord, use the
normal `uv` project workflow instead — `git clone`, `uv sync`, `uv run …` — so
your edits are live. See [`CONTRIBUTING.md`](../CONTRIBUTING.md).

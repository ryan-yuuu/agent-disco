# Install & run Agent Disco

Install Agent Disco on a machine with a single command and configure it with a
guided session that ends with your **first agent live in Discord** — no repo
clone, and no Python, Docker, or git to set up first. This is the path the
[README quick start](../README.md#quick-start) follows. (Want to hack on Agent Disco
itself instead? Don't use the installer — see
[Developing Agent Disco](#developing-agent-disco).)

## 1. Install

```bash
curl -fsSL https://raw.githubusercontent.com/ryan-yuuu/agent-disco/main/scripts/install.sh | bash
```

You don't need Python, Docker, or git installed first — the installer handles
everything, including a native **Tansu** broker and a process supervisor (more
on both below).

It finishes on one of two lines. `READY` means `disco` already works in this
terminal. `ACTIVATE` means it couldn't reach your current shell — open a new
terminal, or:

```bash
source ~/.agent-disco/env
```

## 2. Configure — `disco init`

```bash
disco init
```

`init` is one continuous, resumable session. It asks for:

- A **model provider** (Anthropic / OpenAI / Codex subscription) and its API
  key. If you pick **Codex**, it logs you into ChatGPT inline via a device code
  — it prints a URL + one-time code you open on any device, so it works the same
  on a local machine or over SSH — instead of pointing you at a separate command.
- Your **first agent**: a name (default `assistant`), a description, and a
  **model picked from a live list fetched from the provider** (you select one, so
  you can't enter a slug the provider would reject). It starts with **every
  built-in tool enabled** — `init` deliberately skips the tool picker so setup
  stays short; narrow the list later with `disco agent tools <name>`, or pick per
  agent up front when you add teammates with `disco agent create`.
- Your **Discord bot token** (verified the instant you paste it — you'll see
  `✓ Connected as <bot>`). It then shows the invite link, **waits while you
  authorize the bot, and auto-detects your server** (confirming the bot can post)
  — no numeric IDs to copy. See [`discord-setup.md`](discord-setup.md) for the
  bot-token prerequisite.

It writes `~/.agent-disco/config/.env`, seeds
`~/.agent-disco/config/settings.json`, and writes the agent at
`~/.agent-disco/agents/<name>.md` (provider, model, and tools baked in). Because
the tools step defaults to *every* tool, a freshly-configured agent has terminal,
file-write, code-execution, and web reach — see
[`security.md`](security.md#33-tools-native-broker--others-in-docker) before
exposing it.

**`init` ends live.** After config it opens your workspace, brings your agent
online, and waits until the agent registers on the mesh — so the session
finishes with a working agent, not a "now run these commands" wall. Saying
`!assistant hello` afterward is a confirmation, not the moment of truth.

It's idempotent — re-run it any time to change a setting (an existing agent of
the same name is updated in place, body preserved), and a crash or Ctrl-C
resumes where you left off instead of restarting. Prefer to edit by hand? Open
`~/.agent-disco/config/.env` directly for environment variables, or
`~/.agent-disco/config/settings.json` for bridge behavior such as sticky replies:

```bash
$EDITOR ~/.agent-disco/config/.env
```

See [`configuration.md`](configuration.md) for environment variables and
[`bridge-settings.md`](bridge-settings.md) for `settings.json`.

## How the workspace runs

`init` leaves you with a running **workspace**: a local Kafka broker plus the
Discord bridge, kept alive in the background by a process supervisor the
installer bootstraps ([Process Compose](https://f1bonacc1.github.io/process-compose),
a single static binary, downloaded to `~/.agent-disco/bin/process-compose`). You
never edit the supervisor's config; Agent Disco generates it from your config.

Two layers are worth keeping straight:

- **The substrate** — the broker and the bridge. This is the always-on office.
  `disco start` brings it up (detached, health-gated); `disco stop` closes
  the whole workspace (substrate **and** roster). `start` brings up **only** the
  substrate — nothing else runs that you didn't ask for.
- **The roster** — your agents, the tools host, and any MCP servers. These
  are teammates that clock into the running office on demand:
  `disco agent start <name>`, `disco tools start`, and so on.

> **Reboot non-survival.** The workspace is session-scoped — it does **not**
> come back automatically after a reboot. After restarting your machine, run
> `disco start` again (then bring your agents back online). For a workspace
> that survives reboots, register a launchd (macOS) or systemd (Linux) user unit
> — `disco deploy systemd` generates a starting point (see
> [Going to production](#going-to-production)).

### The workspace runs the broker for you

Agent Disco's processes talk to each other through a **Kafka broker**. You don't
have to run one yourself: the broker ships as the `calfkit-mesh` dependency (a
Tansu binary bundled in the locked environment), so there is no binary to fetch
by hand — `disco init` selects `CALF_HOST_URL=localhost:9092` and `disco start`
launches the broker as part of the substrate. There is no separate "start the
broker" step. On first use the bundled binary is cached under `~/.calfkit/bin/`.

The bundled broker uses **ephemeral memory** storage — topics and messages reset
when the broker restarts, and Agent Disco re-creates the topics it needs on
startup. The bundled build is **memory-only**; for a persistent store
(libsql/SQLite, postgres) point `CALF_TANSU_BIN` at a full Tansu binary, or run
the broker under Docker (`docker compose up tansu`), which uses the full image.
See [Tansu's docs](https://docs.tansu.io/).

**Bring your own / a shared broker.** Choose "I have a broker URL (advanced)" in
`disco init`, or point an existing install at one later:

```bash
disco self set-broker my-broker-host:9092
```

Running agents across machines uses **one shared broker URL** — install Agent Disco
on each host and point them all at the same broker. See
[`distributed-deployment.md`](distributed-deployment.md).

## 3. Day-to-day — start, status, logs

`init` already opened your workspace once. From then on, these are the commands
you live in:

```bash
disco start              # open the workspace (broker + bridge), detached
disco agent start <name> # bring an agent online (a teammate clocks in)
disco status             # the org board: substrate + roster health
disco logs -f            # tail the whole workspace (add a component to scope it)
disco stop               # close the workspace (stops everything it manages)
```

The minimum path to a live agent is two honest commands — open the office, then
bring a teammate in:

```bash
disco start
disco agent start assistant
```

`start` is substrate-only by design, so after a fresh `start` nothing replies
until you bring an agent online; the success banner names that next step for you.

Manage which agents exist and which are running with the `agent` group:

```bash
disco agent list          # agents DEFINED on disk (the .md files)
disco agent ps            # agents RUNNING right now
disco agent restart <name># reload a running agent after editing its .md
disco agent stop <name>   # take an agent offline
```

`logs` reads the supervisor's per-component log files (which also live on disk at
`~/.agent-disco/state/logs/<name>.log`); pass a component name to scope the tail,
e.g. `disco logs -f bridge`.

### Sanity-check with `doctor`

`disco doctor` is the deliberate, authoritative preflight. It runs the
**static config checks** — config file, broker reachability, the Discord bot
token + application id, and that your agents parse — and, **when the workspace is
running**, adds a **daemon-liveness check**: that the bridge heartbeat is fresh
(a live daemon, not a wedged zombie), which — because the bridge only beats once
it is connected to Discord — also confirms the gateway is up. The exit code is
non-zero on a hard failure (a ✗) and 0 on warnings, so it gates scripts cleanly.

```bash
disco doctor             # add --offline to skip the live Discord token check
```

For *who is actually online right now*, use `disco status` / `disco agent
ps` — the roster is read from calfkit's live agent mesh (the old end-to-end
control-plane probe was removed in the calfkit 0.12 migration). `status` is the
cheap, glanceable view; `doctor` is the thorough config + daemon check.

### Talking to an agent

An agent answers when you mention it (`!assistant hello`); a message with no
mention goes unanswered by design — there is no ambient router. A mentioned
agent can also consult or hand off to a peer, which the bridge projects to an
audit channel (see [`a2a-threads.md`](a2a-threads.md)).

## Where your agents live

The installer seeds a text-only starter agent at
`~/.agent-disco/agents/assistant.md`; `disco init` writes or updates the agent
there with the provider, model, and tools you chose. Your agents live in
`~/.agent-disco/agents/` and survive `disco self update`. To add or remove an
agent's tools interactively, run `disco agent tools [<name>]`, then
`disco agent restart <name>` (tools are loaded at agent boot). See
[`authoring-agents.md`](authoring-agents.md) for the full field reference.

The tools host's workspace defaults to **the directory you launch the workspace
(`disco start`) from** — agents read and write files there, the same way
Claude Code works. Mind the trust implications before pointing it at sensitive
files: [`security.md`](security.md#33-tools-native-broker--others-in-docker).

The installer also seeds an empty MCP server registry at
`~/.agent-disco/config/mcp.json` (mode `0600`, never clobbered on update). It stays
empty until you add a server with `disco mcp add` — that's how agents reach
external [MCP](https://modelcontextprotocol.io) tools. See
[`mcp-tools.md`](mcp-tools.md).

## Going to production

Running the same agents across machines is a deployment change, not a rewrite:
the same `.env`, the same `agents/*.md`, and the same commands work on one host
or twenty. Install Agent Disco on each host, point them all at the **same** broker
(`disco self set-broker`), and on each host `disco start` the substrate and
`disco agent start` only the agents that host should run.

When you're ready for managed deployment, `disco deploy` renders manifests you
can hand to an init system or orchestrator:

```bash
disco deploy systemd -o disco.service   # or: k8s, docker
```

For the full picture, run `disco explain topology` (one screen on how the
pieces split and why) and read
[`distributed-deployment.md`](distributed-deployment.md).

## 4. Keep it up to date

```bash
disco self version     # show what's installed
disco self status      # check whether a newer version is available
disco self update      # upgrade to the latest
disco self rollback    # undo the last update
```

`disco self update` does not stop a running workspace, so restart it before
or right after upgrading — the old processes keep running the old code until
you do:

```bash
disco stop && disco start && disco agent start --all
```

Upgrading from an older calfcord (one whose supervisor still ran the agents
itself)? The commands that *spawn* processes (`agent start`/`restart`, `tools
start`/`restart`, `mcp start`/`restart`) will refuse with "this workspace was
started by an older calfcord" until you run exactly that stop/start cycle —
`stop` and `status` keep working so you can wind the old workspace down.

### Existing `~/.calfcord` installs

New installs default to `~/.agent-disco`. Existing installs keep using their
current `$CALFCORD_HOME` when you run `disco self update`, because the old shim
passes that path into the installer. To move an existing install, stop the
workspace, move the directory, then update your shell profile hook to source the
new env file:

```bash
disco stop
mv ~/.calfcord ~/.agent-disco
. ~/.agent-disco/env
```

Edit the `# Agent Disco` block if it still points at `~/.calfcord/env` — in
`~/.profile`, `~/.bashrc`, `~/.zshenv`, or `~/.zprofile` (older installs).

## Uninstall

```bash
disco stop
rm -rf ~/.agent-disco ~/.calfkit/bin
for d in ~/.local/bin /usr/local/bin; do
  case "$(readlink "$d/disco" 2>/dev/null)" in */.agent-disco/shims/disco) rm -f "$d/disco" ;; esac
done
```

The loop removes the `disco` symlink the installer may have put on your `PATH`,
but only while it still points into `~/.agent-disco` — another tool's `disco` is
never touched.

Then remove the `# Agent Disco` block the installer added to `~/.profile`,
`~/.bashrc`, and `~/.zshenv` (or `$ZDOTDIR/.zshenv`).

---

**Pin a version:** set `CALFCORD_REF` to a branch or commit before installing,
e.g. `… | CALFCORD_REF=v1.2 bash`. `disco self update` then stays on that
ref.

## Developing Agent Disco

Don't use the installer — clone the repo and use the standard `uv` /
`docker compose` workflow so your edits are live. See
[`CONTRIBUTING.md`](../CONTRIBUTING.md) and the
[running modes](architecture.md#running-modes) in `architecture.md`.

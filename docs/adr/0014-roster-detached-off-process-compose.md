# Run the roster as detached processes off Process Compose

**Status:** accepted

Only the **substrate** (broker + bridge) stays on Process Compose. The **roster**
— every agent, the `tools` singleton, and each `mcp-<server>` — is now a
directly-supervised **detached** process: a double-fork-style spawn into its own
session (`src/calfcord/supervisor/procspawn.py`), an identity-checked pidfile
under `state/run/<slot>.pid`, and a per-slot log under `state/logs/` rotated at
spawn. `compose.py` renders the substrate only; `roster.py` /
`_workspace.py` own the spawn/scan/terminate glue.

## Why

An empirical spike proved Process Compose **cannot hot-add a process to a live
project**: `POST /project` (update_project) bounces *every* running PID — the same
on the pinned v1.110.0 and the latest v1.116.0 — and the REST API has no
add-process endpoint. (PC's first-update quirk, upstream bug #494, already forced
a priming-reconcile workaround.) On PC, adding one agent therefore meant a full
workspace reload (`disco stop && disco start`), dropping in-flight broker work —
unacceptable for "add a teammate any time". Off PC, a brand-new agent or MCP
server spawns directly into a live workspace and the reload contract is deleted.

Considered and rejected: **PC hot-add** (no endpoint; `POST /project` bounces all
PIDs); a **pre-declared pool of disabled PC slots** (agent names and commands
aren't known ahead of time); **calfkit-native worker lifecycle** — the eventual
direction, but not available today (see
`docs/design/calfkit-worker-lifecycle-gaps.md`).

## Decision mechanics

- **procspawn primitives** — detached spawn (own session, merged stdout/stderr
  appended to the slot log), an identity pidfile carrying pid + a re-queryable OS
  start-token (so a recycled pid is never mistaken for ours), group SIGTERM→SIGKILL
  terminate.
- **Locks** — a shared **lifecycle lock** (`slot_mutation` takes it `LOCK_SH`; the
  stop sweep takes it `LOCK_EX`) plus an **exclusive per-slot lock**: no
  double-spawn, no spawn interleaving with a stop sweep.
- **Boot-confirmation window** — `launch_slot` confirms the slot is still alive
  after ~1.5s; a crash-on-boot reports the log path instead of lying "started".
- **Broker gate** — `broker_gate` replaces PC's `depends_on: broker healthy`,
  probing reachability before a roster spawn.
- **Status truth** — `disco status` / `agent ps` reconcile broker-wide mesh
  presence against local pidfiles (the `tools` and `mcp-<server>` slots have no
  mesh presence, so their pidfile is the whole truth).

## Consequences

- **No auto-respawn** for roster processes — PC's `restart: always` is gone for
  them. A crash reads as offline/exited in `disco status`; the operator restarts.
- **Rotate-at-spawn only** — a long-lived, chatty slot grows its current log
  unbounded between restarts; there is no in-run rotation.
- **Bulk sweeps** (`agent start --all`, etc.) pay the ~1.5s boot-confirm serially
  per slot.
- The lifecycle-lock contention message can be imprecise about which command holds
  the lock.

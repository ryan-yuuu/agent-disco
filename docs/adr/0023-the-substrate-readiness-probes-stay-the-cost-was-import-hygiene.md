# The substrate readiness probes stay; the startup cost was import hygiene

**Status:** accepted

`disco start` took ~20-30s and printed nothing until its final banner, and the
obvious culprit looked architectural: the bridge gates on `depends_on:
{broker: process_healthy}` (`supervisor/compose.py`), so an exec probe costing
~1.4s per run sits on the critical path before the bridge may even start. The
tempting fix — drop the health gate, start broker and bridge concurrently, and
gate readiness in-process from the CLI — is **wrong**, and we are recording that
here because the code gives a future reader every reason to try it.

Measurement found the real cost. `disco _healthcheck bridge` — a probe whose
entire job is reading one JSON file and comparing a timestamp — took **1.44s**,
of which `uv run` was 0.035s, the interpreter 0.010s, and
`calfcord.health.check` 0.156s. The remainder was `import calfcord.cli.main`
(**1.83s**), which eagerly imported 11 subcommand modules including
`agent_create` → `calfcord.agents` → `calfkit` → `calfkit.nodes.agent` →
`calfkit.providers.pydantic_ai`. The probe was loading the entire agent
framework — 24 modules of it — every 3
seconds, forever — and so was every other `disco` command (`disco agent list`:
1.93s). The fix is import hygiene, an invariant this codebase already states
elsewhere (`cli/_supervisor.py`'s "import-light invariant"; calfkit's own
`cli/dev.py` "Import hygiene (load-bearing)").

So: **`depends_on` and both readiness probes stay. The import graph gets fixed.**

## Consequences

- The probes' standing CPU cost drops with the import graph rather than with
  their cadence, so `_PROBE_PERIOD_SECONDS` (`supervisor/compose.py`) keeps a
  responsive watchdog without re-creating the readiness-spiral incident that
  same file documents. Cheapening the probe *dissolves* the tension between
  watchdog responsiveness and CPU cost that forced the 3s period; it does not
  merely trade one for the other.
- `cli/main.py`'s subcommand imports are now load-bearing for latency. A future
  top-level `from calfcord.cli import <module>` that reaches the agent stack
  silently re-imposes the ~1.4s tax on every probe and every command. This is
  guarded by a test, not by convention.
- Startup remains **polled, not event-driven**, and that is deliberate: Process
  Compose's REST API exposes no state event stream (`supervisor/client.py`), so
  polling is the only option available. calfkit's own CLI polls too — at
  0.05-0.25s, affordable only because its probes are in-process. Ours are
  subprocesses; cheapening them is what buys back the granularity.

## Considered options

- **Drop `depends_on` + the broker's readiness probe; gate on the broker
  in-process from the CLI.** Rejected — it deletes the broker's only
  self-healing mechanism. Verified against the pinned process-compose v1.110.0:
  a live process (`sleep 300`) whose *readiness* probe fails is restarted
  repeatedly (`restarts=4` within 18s), while an identical probe-less process
  sits at `restarts=0` forever. The probe is not just a startup gate — it is the
  watchdog that recovers a Tansu that still holds :9092 but has stopped serving
  metadata. Today that self-heals in ~9s; without the probe it never does, and
  the board stays green because the bridge's readiness is heartbeat-only
  (`health/check.py`) and never consults the broker. That is §12.6's "green
  light that lies" in its most durable form.
- **Replace the gate with a CLI-side `default_broker_probe` check.** Rejected
  even as a partial mitigation: a metadata probe is decoupled from the *process*.
  If a foreign broker (another `$CALFCORD_HOME` install, a stale Tansu, a Docker
  Kafka) holds :9092 while our own broker crash-loops on bind failure, the probe
  connects to the foreigner and returns True. Today `process_healthy` only ever
  probes a Running process, so this is a narrow race; a decoupled probe makes it
  a deterministic pass.
- **Adopt calfkit's `ck dev` CLI to start the broker and workers.** Rejected —
  it is a dev-loop tool ("reload ON, idempotence OFF", ownership by argv
  process-table scan with no saved state) with no restart supervision, no log
  rotation, no roster, no per-home port isolation, and a SIGKILL teardown that
  would deny agents the ~2s they need to publish `AgentDepartureEvent`. Its
  *readiness architecture* is worth borrowing and its `ConsoleWaitReporter` is
  worth modelling; its process model is not. Notably its broker is the same
  bundled memory-engine Tansu ours is, so storage was never the differentiator.
- **Add a calfcord-side retry around the bridge's first `broker.start()`.**
  Rejected as specified. The symptom is real (a mention landing during a broker
  bounce dies as a generic "Something went wrong"), but `broker.start()` is
  documented **not self-idempotent** and is reached only through calfkit's
  private `_ensure_started`, whose `_start_lock` + double-check is precisely what
  makes two concurrent mentions safe today. A local retry would bypass that lock
  and *create* the race it purports to fix. The only safe placement is an eager
  pre-start, which re-couples bridge startup to the broker and undoes the
  deliberate D-11 decision (`bridge/gateway.py`). Left alone pending a public
  retry seam upstream.

## Reconciliation

`docs/design/calfkit-012-implementation-plan.md` (D-9) schedules deleting the
`healthcheck()` broker arm *and* `default_broker_probe`, citing the compose
broker gate as their only consumer. That plan predates ADR-0014's roster
`broker_gate` (`supervisor/_workspace.py`), which re-imposed the same gate off
Process Compose and is now one of three live callers (with `lifecycle.start`'s
external-broker fast-fail and `cli/main.py`'s hidden `_healthcheck` verb, itself
allowlisted in `scripts/install.sh`). D-9's "remove its consumers" list is stale
and must not be actioned as written.

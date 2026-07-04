# Bootstrap the Tansu broker via the calfkit-mesh wheel, not a bash download

**Status:** accepted

The installer no longer downloads a Tansu release tarball by hand (the
`ensure_tansu` step in `scripts/install.sh`, plus the `~/.calfcord/bin/tansu`
placement and the version pin). Instead the broker ships as a **required**
project dependency, [`calfkit-mesh`](https://github.com/calf-ai/calfkit-mesh),
which bundles a **memory-only** Tansu binary inside platform wheels. `disco
broker` now runs a small `calfcord-broker` console script that resolves the
bundled binary via `calfkit_mesh.resolve_broker_bin()` and launches it with
calfcord's substrate defaults (ephemeral memory storage on `localhost:9092`).
Design spec: [`docs/design/tansu-via-calfkit-mesh.md`](../design/tansu-via-calfkit-mesh.md).

## Why

The binary rides in the locked venv the installer already builds with `uv sync`,
so the bespoke ~55-line download function (OS/arch triple detection, tarball
fetch, quarantine strip, best-effort guards) is deleted and the version pin moves
into `uv.lock` — canonical and reproducible. It is a first-party calf-ai package
(same org as the `calfkit` SDK we already depend on) with **no runtime
dependencies** (zero interaction with our `calfkit>=0.12.5` pin), and its wheels
cover a **wider** surface than the old bash (macOS + Linux glibc **and musl**,
x86_64/aarch64). The bundled Tansu version (`v0.6.0`) is byte-identical to what
the bash installer pinned, so this is a pure mechanism swap, not a broker
upgrade.

## Considered options

- **Optional extra** (`calfkit-mesh` behind an extras group, tolerating a failed
  install). Rejected: "the project needs a broker to run", so broker provisioning
  should be a normal locked dependency that fails loudly if unmet, not degrade
  silently.
- **Keep the bash download** to preserve the full (S3-capable) binary for the
  Stage-2 roadmap. Rejected: Stage 2 is exploratory, the default config is already
  memory, and `$CALF_TANSU_BIN` keeps the door open (see Consequences).

## Consequences

- **Memory-only is a deliberate capability removal.** The old full binary
  supported `libsql`/`postgres`/`s3`; the bundled build is memory-only. Operators
  needing persistence set `$CALF_TANSU_BIN` to a full Tansu binary, or use the
  Docker broker (whose image retains those engines). Docs (`docs/installation.md`,
  `.env.example`, `docs/configuration.md`) were corrected accordingly.
- **The broker deliberately does not read `config/.env`.** The `disco broker`
  shim arm runs `calfcord-broker` **without** `--env-file` — preserving the old
  arm's behavior. Routing it through `--env-file` would newly feed a stale
  `STORAGE_ENGINE=libsql://…` into the memory-only binary and crash-loop it under
  Process Compose's `restart: always`. As defense-in-depth the launcher also
  forces memory storage for the bundled binary (honoring a non-memory engine only
  when `$CALF_TANSU_BIN` is set).
- **Hard dependency couples install to role.** A broker-less host (agents/tools
  pointed at a remote `CALF_HOST_URL`) now installs a broker binary it never runs,
  and on a platform with no wheel `uv sync --locked` fails the whole install
  (the old bash was best-effort). This is an install-time footprint cost, not a
  runtime-topology change (processes still share only the broker over Kafka).
  Windows is explicitly out of scope (the installer is bash/POSIX-only), so the
  absent Windows wheel is a non-issue.
- **Hard cutover, no rollback across the boundary.** `disco self rollback` only
  flips the `current` symlink; a post-migration shim over a pre-migration venv has
  no `calfcord-broker` entry point. We add no compatibility fallback (it would
  re-introduce the hardcoded path this migration removes); rolling back across the
  migration is unsupported.
- **Stage 2 (S3) direction.** The preferred path is for `calfkit-mesh` to grow a
  full/S3-capable wheel (Stage 2 rides the same dependency), not for calfcord to
  re-add a bash download; `$CALF_TANSU_BIN` is a runtime override, not a
  provisioning mechanism.
- **process-compose is still bash-bootstrapped** — it has no first-party wheel, so
  that one `ensure_process_compose` download remains (an accepted asymmetry).

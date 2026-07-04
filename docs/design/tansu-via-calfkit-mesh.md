# Bootstrap the Tansu broker via the `calfkit-mesh` wheel — migration spec

- **Status:** Draft — **revised after a 4-agent deep review** (codebase-fact,
  calfkit-mesh package, design/architecture, completeness). Findings folded in;
  see §10 for the review trail. (drafted 2026-07-03)
- **Owner:** Ryan
- **Scope:** Replace the bespoke `ensure_tansu` bash download in
  `scripts/install.sh` with **`calfkit-mesh`** as a *required* project
  dependency. The Tansu broker binary rides in the locked venv (installed by the
  existing `uv sync`); `disco broker` resolves it through
  `calfkit_mesh.resolve_broker_bin()` instead of a hardcoded
  `~/.calfcord/bin/tansu` path.
- **Touches:** `pyproject.toml` / `uv.lock` (one dep), one net-new module
  (`src/calfcord/broker/`), `scripts/install.sh` (mostly deletions + one arm
  rewrite), **`scripts/tests/test_installer.sh`** (rewrite the broker-arm block,
  delete the `ensure_tansu` block — this is CI-gated), a new
  `tests/broker/test_runner.py`, a `disco doctor` broker-resolve check
  (`cli/doctor.py`), and docs (`docs/installation.md`, `.env.example`,
  `docs/architecture.md`, `docs/configuration.md`, `roadmap/tansu-broker.md`),
  plus one ADR. **No** changes to the supervisor, `health/check.py`,
  `cli/deploy.py`, or the Docker/k8s manifests.
- **Relates to:** [`../../roadmap/tansu-broker.md`](../../roadmap/tansu-broker.md)
  (Stage 1 "Native bootstrap", which this supersedes; Stage 2 S3 direction, which
  this constrains — see §6 and §11).

> Design archive note (per [`README.md`](./README.md)): a plan for an unbuilt
> change, recording the decisions and their rationale.

---

## 1. Context & goal

Today the one-line installer downloads a standalone Tansu release tarball with a
~55-line bash function, `ensure_tansu` (`scripts/install.sh:162-217`, called from
`main` at `:749`), places the binary at `~/.calfcord/bin/tansu`, strips the macOS
quarantine xattr, and pins the version in a bash variable
(`TANSU_VERSION="${CALFCORD_TANSU_VERSION:-v0.6.0}"`, `:43`, described by the
inline comment at `:42`). The `disco broker` shim arm
(`scripts/install.sh:432-440`) then execs that path with two env defaults.

[`calfkit-mesh`](https://github.com/calf-ai/calfkit-mesh) is a first-party
(calf-ai, same org as `calfkit`) PyPI package that **bundles a static,
memory-only Tansu build inside platform wheels** and exposes a single locator,
`resolve_broker_bin()`. Because the installer already builds a locked venv via
`uv sync --locked --no-dev`, the broker binary can ride in as an ordinary
dependency — deleting the bespoke download path entirely.

**Goal:** the broker is provisioned by the dependency graph, not by hand-rolled
bash; `disco broker` resolves it via the package API; version pinning lives in
`uv.lock`, not a bash constant.

### 1.1 Continuity fact (de-risks the swap)

`calfkit-mesh 0.1.1` bundles Tansu **`v0.6.0`** (exported as
`calfkit_mesh.TANSU_VERSION`) — the *exact* version the current bash installer
pins. This migration is therefore a **pure mechanism swap, not a broker
upgrade**: the running broker binary is byte-identical in version.

---

## 2. Settled decisions (owner-confirmed)

| # | Decision | Rationale |
|---|---|---|
| D1 | **Memory-only build is acceptable.** | Stage 1 defaults to `memory://tansu/`; `disco broker` already defaults to it. **Zero regression for the *default* config.** Note this is nonetheless a *deliberate capability removal* for anyone who configured native persistence (the full binary supported `libsql`/`postgres`/`s3`); mitigated by `$CALF_TANSU_BIN` (§6.2). |
| D2 | **Required dependency, not an optional extra.** | "The project needs a broker to run." Broker provisioning should be a normal locked dependency, failing loudly if unmet rather than degrading silently. Tension with broker-less roles is acknowledged in §6.1 and the ADR (§7). |
| D3 | **Keep `$CALF_TANSU_BIN` as the escape hatch.** | calfkit-mesh's own override lets an operator point at a full/persistent/S3-capable binary without code changes — preserves the Stage 2 door (§11). |
| D4 | **Windows is out of scope.** | The installer and shims are bash/POSIX-only; there is no native-Windows `disco` path. The absence of a Windows wheel (§3) is therefore a non-issue by design. Alpine/musl hosts *are* a genuinely new supported surface. |

---

## 3. The `calfkit-mesh` contract (verified against the published 0.1.1 package)

- `resolve_broker_bin() -> str` — no arguments; returns an **absolute** path to a
  runnable Tansu binary. Raises **`TansuBinaryNotFound`** (a `RuntimeError`
  subclass, exported) if none resolve. It also raises `TansuBinaryNotFound` if
  `$CALF_TANSU_BIN` is set but not executable (a bad override fails loudly rather
  than silently falling back — worth a line in the §5.5 docs).
- Resolution order: `$CALF_TANSU_BIN` → bundled wheel binary → `shutil.which("tansu")`
  on `PATH`. All three yield absolute paths, so `os.execv` (no PATH search) is safe.
- **Lazy extraction:** importing `calfkit_mesh` has no side effects. The first
  `resolve_broker_bin()` call materializes the bundled binary to
  `~/.calfkit/bin/tansu-v0.6.0` (temp-file + atomic `os.replace`, then
  `os.chmod 0o755` on every call). This first-run copy (tens of MB) can raise
  `OSError`/`PermissionError` — the launcher must catch it (§5.2), and we
  cache-warm at install time to keep it out of the supervised broker's hot path
  (§5.3, §6.3).
- Public exports: `resolve_broker_bin`, `TansuBinaryNotFound`, `__version__`
  (`0.1.1`), `TANSU_VERSION` (`v0.6.0`).
- **No runtime dependencies** — `requires_dist` is empty, so **zero interaction**
  with agent-disco's `calfkit>=0.12.5`.
- **Wheels (exactly 6, all `py3-none`, no sdist):** macOS `x86_64` +
  `arm64`; Linux glibc (`manylinux2014`) `x86_64` + `aarch64`; Linux musl
  (`musllinux_1_1`) `x86_64` + `aarch64`. The **`py3-none` tag is
  version-independent**, so the CI matrix (3.12/3.13/3.14) and the
  `python:3.14-slim` Dockerfile all resolve the same wheels. **There is no
  Windows wheel** (the README's "Windows" prose is aspirational; no artifact was
  published) — a non-issue per D4. **No sdist ⇒ `uv sync` never compiles Rust**;
  an unsupported platform fails resolution hard (§6.1).

---

## 4. Current-state map (what actually references the binary)

Verified by grep across `src/`, `scripts/`, docs, CI, and deploy templates:

| Concern | Location today | Effect of migration |
|---|---|---|
| Binary download | `ensure_tansu`, `scripts/install.sh:162-217` (+ call `:749`) | **Deleted.** |
| Version pin | `TANSU_VERSION`, `scripts/install.sh:43` (comment `:42`) | **Deleted** (moves to `uv.lock`). |
| Binary path resolution + env defaults | shim `broker` arm, `scripts/install.sh:432-440` (hardcoded `$H/bin/tansu`; `STORAGE_ENGINE`/`ADVERTISED_LISTENER_URL`). **Short-circuits `exec` *before* the `--env-file` passthrough — so the broker never loads `config/.env` today.** | **Arm rewritten** to exec the `calfcord-broker` console script in the venv, still **without `--env-file`** to preserve the no-`.env` behavior (§5.3, §6.4). Defaults move into the Python launcher. |
| Broker env vars in `src/` | none — `STORAGE_ENGINE`/`ADVERTISED_LISTENER_URL` appear **only** in the shim | Move into the launcher. |
| Supervisor broker process | `supervisor/compose.py:266-267`, `command=f"{launcher} broker"` | **Command string unchanged.** Runtime tree gains a `uv run → python(execv→tansu)` layer — the **same shape the bridge already uses** (`compose.py:274-282`), safe because `shutdown.parent_only: false` (`compose.py:73`) signals the whole group. See §6.3. |
| Broker healthcheck | `cli/doctor.py:134-144` (`_check_broker`, TCP reachability) + `health/check.py` (metadata) | **Unchanged** — never touches the binary path. |
| Docker/k8s manifests | `cli/deploy.py:328,331` (image `ghcr.io/tansu-io/tansu:latest`, `--storage-engine=memory://tansu/`) | **Unchanged** — uses the Tansu *Docker image*, not the native binary. **Note:** the **systemd** target (`deploy.py:189`, `ExecStart={launcher} start`) *is* transitively coupled (→ process-compose → `disco broker` → launcher); nothing breaks, but §4's "independent" applies to Docker/k8s only. |
| **Installer test suite (bash, CI-gated)** | `scripts/tests/test_installer.sh:144-161` (broker-arm behavior) + `:163-168` (`ensure_tansu` degradation), run by `.github/workflows/installer.yml` | **Must be rewritten/deleted** (§5.4). This is the suite that actually tests the changed code. |

The takeaway: the supervisor and healthcheck reach the broker through
`disco broker`, so the binary-resolution change is contained to **one shim arm +
one new Python module** — but the **CI-gated bash installer suite** tests that arm
directly and must change with it.

---

## 5. The change — five areas

### 5.1 Dependency (`pyproject.toml` / `uv.lock`)

`uv add calfkit-mesh` (canonical — no hand-editing `pyproject.toml`). Lands in
`[project.dependencies]`; uv writes universal wheel entries to the lock for all 6
published targets. It then installs automatically in the installer's existing
`uv sync --locked --no-dev` step — **no new installer download logic**. (`uv.lock`
currently has zero `calfkit-mesh`/`tansu` entries — confirmed; the lock refresh is
genuinely new.)

### 5.2 New broker launcher (the only net-new code)

New `src/calfcord/broker/__init__.py` + `src/calfcord/broker/runner.py` (named
`runner.py` for consistency with `tools/runner.py`, `mcp/runner.py`):

> **As-built note:** the sketch below is the design intent; the shipped
> `runner.py` is the source of truth. It factored the env defaults into a
> `_default_env` helper + module constants, references `calfkit_mesh.ENV_VAR`
> (not a hardcoded `"CALF_TANSU_BIN"`), and wraps `os.execv` in the same branded
> error handling as `resolve_broker_bin` (a resolved-but-unrunnable binary exits
> cleanly instead of tracebacking under `restart: always`).

```python
import os
import sys

from calfkit_mesh import TansuBinaryNotFound, resolve_broker_bin


def main() -> None:
    """Resolve the Tansu binary and exec it as the local broker.

    Preserves the shim's prior behavior: memory storage on localhost:9092 by
    default, overridable via the operator's shell env / passthrough args.
    ``$CALF_TANSU_BIN`` (honored inside ``resolve_broker_bin``) substitutes a
    persistent/S3-capable binary.
    """
    try:
        tansu = resolve_broker_bin()  # absolute path; raises on failure
    except (TansuBinaryNotFound, OSError) as exc:  # incl. first-run extraction faults
        print(f"disco broker: {exc}", file=sys.stderr)
        raise SystemExit(1)

    # Match the old ``${VAR:-default}`` semantics exactly (empty string counts as
    # unset), not setdefault (which would preserve an explicit empty value).
    if not os.environ.get("STORAGE_ENGINE"):
        os.environ["STORAGE_ENGINE"] = "memory://tansu/"
    if not os.environ.get("ADVERTISED_LISTENER_URL"):
        os.environ["ADVERTISED_LISTENER_URL"] = "tcp://localhost:9092"

    # Defense-in-depth: the bundled binary is memory-only. If it was chosen (no
    # $CALF_TANSU_BIN override) yet a non-memory engine was requested, refuse to
    # boot into an opaque crash-loop under Process Compose's ``restart: always`` —
    # warn and force memory, directing persistence users to $CALF_TANSU_BIN.
    if not os.environ.get("CALF_TANSU_BIN") and not os.environ["STORAGE_ENGINE"].startswith("memory:"):
        print(
            f"disco broker: bundled broker is memory-only; ignoring "
            f"STORAGE_ENGINE={os.environ['STORAGE_ENGINE']!r}. For persistence, set "
            f"CALF_TANSU_BIN to a full Tansu binary or use the Docker broker.",
            file=sys.stderr,
        )
        os.environ["STORAGE_ENGINE"] = "memory://tansu/"

    os.execv(tansu, [tansu, "broker", *sys.argv[1:]])
```

New `[project.scripts]` entry:
`calfcord-broker = "calfcord.broker.runner:main"`.

### 5.3 `scripts/install.sh` — deletions + one arm rewrite

- Delete `ensure_tansu()` and its `main()` call.
- Delete the `TANSU_VERSION` / `TANSU_OK` vars, the `:42` inline comment, and the
  `main()` summary `TANSU_OK` branch (`:767-771`).
- **Rewrite the `broker` shim arm** (`:432-440`) to exec the console script in the
  venv **without `--env-file`**, preserving today's "broker ignores `config/.env`"
  behavior (see §6.4 for why this matters). It must run *after* the `$UV` / `$H/current`
  resolution (`:480-485`), so move it below that point:
  ```bash
  if [ "${1:-}" = "broker" ]; then
    shift
    exec "$UV" run --frozen --no-sync --project "$H/current" -- calfcord-broker "$@"
  fi
  ```
  (Deliberately *not* routed through the verb-translation `case` → `--env-file`
  passthrough; that alternative was rejected in §6.4.)
- **Cache-warm at install time.** After `install_version` (the `uv sync`), invoke
  `resolve_broker_bin()` once so the tens-of-MB extraction to
  `~/.calfkit/bin/tansu-v0.6.0` happens at install (loud, one-time) rather than
  inside the `restart: always` broker on first `disco start` (§6.3). Best-effort
  with a clear warning on failure; `disco doctor` (below) re-checks.
- **Leave `ensure_process_compose` untouched** — no first-party wheel exists for
  it, so that one bash bootstrap knowingly remains (an accepted asymmetry; note it
  means process-compose and Tansu now bootstrap by *different* mechanisms, which
  the `docs/architecture.md` prose must stop implying — §5.5).

### 5.4 Tests (TDD — written first)

- **`scripts/tests/test_installer.sh` (CI-gated — the primary test edit):**
  - Rewrite the broker block (`:144-161`): assert `disco broker --cluster-id demo`
    dispatches to `<stub-uv> run … -- calfcord-broker --cluster-id demo` (mirror
    the existing dispatch assertion at `:101-104`). Drop the direct-`$TANSU`-exec,
    `SE=`/`AL=`, and `"native tansu broker not installed"` assertions — that
    behavior now lives in the Python launcher unit test.
  - Delete the `ensure_tansu` degradation block (`:163-168`).
  - `scripts/tests/lint.sh` shellchecks this file, so keep it clean.
- **New `tests/broker/test_runner.py`** (monkeypatch `resolve_broker_bin` +
  `os.execv`): (a) env defaults applied when unset, preserved when explicitly set;
  (b) `os.execv` called with `[bin, "broker", *passthrough]`; (c)
  `TansuBinaryNotFound` **and** `OSError` → exit 1 + stderr; (d) bundled binary +
  non-memory `STORAGE_ENGINE` → warn + forced memory; (e) `$CALF_TANSU_BIN` set →
  non-memory engine honored.
- **`tests/test_install_sh.py`:** add a Python-level `disco broker` dispatch test
  mirroring `test_shim_auth_maps_to_calfkit_auth` (`:264-267`); confirm no
  collision with `test_shim_existing_verbs_still_route_after_lifecycle_verbs`
  (`:309-325`). (This file references no tansu symbols today, so deleting
  `ensure_tansu` needs no edits here.)
- Coverage: the `install.sh` summary-block deletion is `log()`-gated (untested by
  design); the new launcher branches are covered by (a)–(e).

### 5.5 Docs

- `docs/installation.md`: rewrite §"The workspace runs the broker for you"
  (`:91`, `:95`) — broker ships via the `calfkit-mesh` dependency, cached at
  `~/.calfkit/bin/tansu-v0.6.0`. **Correctness fix:** remove/qualify the
  persistence-via-`STORAGE_ENGINE=libsql/postgres` guidance (`:101-102`) — the
  bundled build is memory-only; persistence needs `$CALF_TANSU_BIN` or Docker.
  Also fix `:71` ("process-compose … the same way the Tansu broker binary is") —
  no longer the same way.
- **`.env.example:54-60`** — the native-broker block instructs
  `STORAGE_ENGINE=libsql/…` for persistence and is copied verbatim to
  `config/.env` on install (`seed_config`, `:321-322`). Repoint it at
  `$CALF_TANSU_BIN`/Docker. **(Highest-priority doc fix — this is the source of
  the stale-`STORAGE_ENGINE` hazard.)**
- **`docs/architecture.md:151`** — "bootstraps the same way it bootstraps the
  Tansu broker (a pinned binary under `$CALFCORD_HOME/bin`, kept out of the agent
  Python path)" is now false on both clauses (tansu is cached under `~/.calfkit`,
  and it now rides *in* the venv). Fix the prose; also review `:249,:263`
  ("native Tansu broker"). A literal `~/.calfcord/bin/tansu` grep misses this —
  grep `$CALFCORD_HOME/bin` and "pinned binary".
- `docs/configuration.md`: document `$CALF_TANSU_BIN` (incl. "set-but-not-executable
  fails loudly"); note the native broker is memory-only while Docker/k8s retain
  persistence knobs (§6.5).
- `roadmap/tansu-broker.md:82-99`: annotate Stage 1's "Native bootstrap" bullet
  (historical — annotate, don't rewrite). Also flag stale mirror references:
  `roadmap/onboarding-cli.md:115`, `src/calfcord/supervisor/lifecycle.py:221`
  ("mirroring `ensure_tansu`"), and `docs/design/onboarding-redesign.md`
  (`:115,:200,:272,:686`) — minor stale-reference cleanup.

### 5.6 `disco doctor` broker-binary check (in scope, owner-confirmed)

Add a check to `cli/doctor.py` that calls `resolve_broker_bin()` and reports
`ok`/`fail` with the resolved path (or the `TansuBinaryNotFound`/`OSError`
message). This surfaces a missing or unextractable binary as a diagnostic instead
of a `disco start` crash-loop, and doubles as a cache-warm (its call triggers the
same lazy extraction). It slots beside the existing static checks
(`cli/doctor.py:287,298`) and needs its own test (monkeypatched success + failure).

---

## 6. Behavior changes / trade-offs (stated, not hidden)

1. **Broker becomes a hard install dependency (role-coupling honesty).** On a
   platform with no `calfkit-mesh` wheel, `uv sync --locked` fails the whole
   install (no Rust sdist fallback). The *runtime* invariant is untouched
   (processes still share only the broker over Kafka) and the install is already
   monolithic (one venv per host, no per-role dep slicing), so this is an
   **install-time footprint** issue, not a topology violation — a broker-less
   host (tools/agents pointed at a remote `CALF_HOST_URL`) now installs a ~tens-of-MB
   broker binary it will never run. But the failure mode is **asymmetric**: the
   old bash was best-effort (unsupported platform still installed, ran against a
   remote broker); the wheel is **install-blocking**. So "the practical surface
   widens" is true only where wheels exist (adds musl + aarch64); on a wheel-less
   platform a broker-less host that used to work **can't install at all**. D2's
   "the project needs a broker to run" is a single-host framing; the ADR must own
   this tension for the distributed-deployment story.
2. **Memory-only = deliberate capability removal.** The current `ensure_tansu`
   pulls the *full* tansu-io binary (`memory`/`libsql`/`postgres`/`s3`);
   calfkit-mesh bundles a memory-only build. Zero regression for default config;
   a real loss for native-persistence users, recoverable via `$CALF_TANSU_BIN`.
   Drives the §5.5 doc fixes.
3. **Supervised process gains a `uv run → tansu` layer + first-run extraction.**
   Safe: identical to the bridge's supervised shape and correct under
   `shutdown.parent_only: false` (`compose.py:73`) — a future edit flipping that
   flag would orphan the exec'd binary, so §7 pins it as a verify item. The lazy
   first-run extraction is moved to install time (§5.3) so the `restart: always`
   broker never extracts in its hot path.
4. **The broker still does not read `config/.env`** — preserved by the
   no-`--env-file` arm (§5.3, §6.4). Cache path moves from
   `~/.calfcord/bin/tansu` to `~/.calfkit/bin/tansu-v0.6.0`. The old
   `~/.calfcord/bin/tansu` (~138 MB, the full binary) **lingers after update**;
   the installer does **not** remove it (owner-confirmed — no cleanup logic; it is
   harmless dead weight, and the new shim never looks there). Operators may delete
   it by hand to reclaim disk.

### 6.4 Why the broker arm must NOT route through `--env-file` (the sharpest finding)

Today's arm `exec`s **before** the `--env-file "$ENVF"` passthrough (`:432-440`
vs `:538`), so the broker's environment is shell-only. Routing `broker` through
the verb-case (the naïve simplification) would newly load `config/.env` into the
broker. Because `docs/installation.md:101-102` and `.env.example:54-60`
**actively instruct** operators to set `STORAGE_ENGINE=libsql://…`, that stale
value would reach the **memory-only** binary and **crash-loop it under
`restart: always`** — turning a working install into a broken one purely on
`disco self update`. The dedicated no-`--env-file` arm avoids this entirely; the
launcher's memory-enforcement (§5.2) is belt-and-suspenders for a value exported
in the operator's shell.

### 6.5 Migration & upgrade safety

- **`disco self update` on a wheel-less platform** now hard-fails (`uv sync`
  can't resolve the wheel), stranding a previously-working broker-less install.
  Document as a known limitation of D2.
- **Rollback across the migration boundary is unsupported (hard cutover,
  owner-confirmed).** `disco self rollback` (`install.sh:636-654`) only flips the
  `current` symlink; it does **not** re-run `write_shims`. After the update that
  crosses this migration, the retained (post-migration) shim routes
  `broker → calfcord-broker`, but a rolled-back (pre-migration) venv has no such
  entry point → `disco broker` breaks. We add **no** compatibility fallback: a
  `$H/bin/tansu` fallback would re-introduce the hardcoded path this migration
  removes. Operators who need the old behavior re-run the installer forward. The
  release notes / ADR state this plainly.
- **Stale `STORAGE_ENGINE` in an operator's `config/.env`** is rendered inert for
  the broker by the no-`--env-file` arm, and by the launcher's memory-enforcement
  if shell-exported. The `.env.example` fix (§5.5) stops seeding the false
  promise going forward.

---

## 7. Verification & rollout

- Follow `/test-driven-development` (red→green→refactor); `uv run pytest`; **plus
  `bash scripts/tests/test_installer.sh`** (pytest does not run the shell suite —
  the CI `installer.yml` job does, on ubuntu + macOS bash 3.2); ruff clean;
  `/pytest-coverage` on the new module.
- **Verify `uv run` propagates SIGTERM + exit status** to the exec'd tansu so the
  compose broker slot still stops on `disco stop` and restarts on crash (relies on
  `parent_only: false`).
- Manual smoke: `uv run calfcord-broker` serves memory Tansu on `:9092` →
  `disco start` brings the substrate up → `disco doctor` green → `disco stop`
  tears the broker down cleanly.
- Confirm `uv add` captures all 6 wheels in `uv.lock` (universal lock) so
  `uv sync --locked` works on macOS, glibc, and musl runners; confirm the
  `Dockerfile` (`python:3.14-slim`, glibc ≥ manylinux2014 baseline) and the
  `release.yml` multi-arch build (`linux/amd64,linux/arm64`) resolve the
  `manylinux2014` x86_64/aarch64 wheels — the calfcord image now carries an inert
  broker binary (accepted).
- Deep review via `/pr-review-toolkit:review-pr` + `/simplify`.
- **ADR:** reverses a shipped decision (native bash bootstrap → wheel) and carries
  the memory-only + hard-dep-role-coupling trade-offs (§6.1, §6.2), so it
  qualifies. Draft alongside the change; state the distributed-deployment caveat.

---

## 8. Resolved decisions (owner-confirmed 2026-07-03)

- **Rollback-across-boundary:** **hard cutover — unsupported.** No compatibility
  fallback; a `$H/bin/tansu` fallback would re-introduce the deleted path (§6.5).
- **Stale `~/.calfcord/bin/tansu`:** **no installer cleanup** — left in place;
  operators remove it themselves (§6 item 4). (On the owner's own machine, the
  ~138 MB binary is deleted as the **final step after implementation**, once the
  local install is on `calfkit-mesh` — not before, since it is still the live
  broker until then.)
- **Cache-warm failure (§5.3):** **warn + proceed** — the install-time
  `resolve_broker_bin()` call (which triggers the one-time binary extraction) may
  fail on a read-only/full disk or a permissions fault; that must not abort an
  otherwise-successful install. It prints a clear warning and points at
  `disco doctor`, which re-checks (§5.6). (`uv sync` already guaranteed the wheel
  is present, so this failure is rare/transient.)
- **`disco doctor` broker check:** **in scope** (§5.6).

---

## 9. Estimated footprint

~1 small new module + entry point; ~60 lines deleted + one arm rewritten in
`scripts/install.sh` (+ a cache-warm call); one added check in `cli/doctor.py`;
**rewrite of a CI-gated bash test block + one deleted block** in
`scripts/tests/test_installer.sh`; 1 new Python test file + 1 added shim-dispatch
case + 1 doctor-check test; **5 doc edits** (`installation.md`, `.env.example`,
`architecture.md`, `configuration.md`, `roadmap/tansu-broker.md`); 1 ADR.

---

## 10. Review trail (4-agent deep review, 2026-07-03)

- **calfkit-mesh package verifier:** confirmed the §3 contract; caught the
  **non-existent Windows wheel** (struck per D4), the **`OSError` on first-run
  extraction** (launcher now catches it), the `py3-none`/no-sdist/no-runtime-deps
  facts, and the `${VAR:-}`-vs-`setdefault` empty-string nuance (launcher now
  matches `${VAR:-}`).
- **Codebase-fact verifier:** confirmed every install.sh line anchor and the
  routing mechanics; caught the **wrong test file** (blocker — see below).
- **Completeness/gaps hunter:** found the **CI-gated `scripts/tests/test_installer.sh`**
  (blocker), the **rollback/update-skew** gaps (§6.5), the **`.env.example`**
  false promise (§5.5), the Docker/native persistence divergence (§6.5), and the
  Dockerfile/release wheel-ABI question (resolved: `py3-none`).
- **Design/architecture reviewer:** validated `os.execv`/module home/no-coupling;
  landed the **sharpest finding** — routing through `--env-file` would crash-loop
  memory-only installs (§6.4, resolved by the dedicated arm); the **hard-dep
  role-coupling** honesty (§6.1); the **`parent_only: false`** correctness pin
  (§6.3); and the Stage-2 provisioning caveat (§11).

Both blockers (wrong test file / missed CI-gated suite) converged from two
independent reviewers and are resolved in §4/§5.4.

---

## 11. Stage 2 (S3) note

`$CALF_TANSU_BIN` keeps the S3 roadmap open at runtime, but note the trap: Stage 2
needs a **full, S3-capable binary on every host**, so the bundled memory-only
wheel becomes inert on Stage-2 hosts, and Stage 2 would need to re-introduce a
binary-provisioning step (`$CALF_TANSU_BIN` is a runtime *override*, not a
*provisioning* mechanism). **Preferred Stage-2 direction:** calf-ai extends
`calfkit-mesh` to ship a full build (Stage 2 then rides the same dependency) —
**not** calfcord re-adding a bash download. A hard-dep carve-out for
broker-less/Stage-2 hosts may be revisited then.

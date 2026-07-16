# Installs own and pin their toolchain (uv and the interpreter)

**Status:** accepted

The installer built each `versions/<sha>` with whatever uv and Python the box
happened to provide: `ensure_uv` reused a system uv when it looked capable, and
`uv sync` inherited the caller's environment. So an active conda env silently
bound the "pinned" venv to `~/miniconda3` (observed on two independent installs,
whose `.venv` held no interpreter of its own — `base_prefix` and the stdlib both
resolved into miniconda), and `.python-version = 3.12` floated across patch
releases (3.12.12 on one box, 3.12.13 on a fresh one, from the same commit).

Agent Disco now owns both. A pinned uv is always bootstrapped into
`$CALFCORD_HOME/bin`; `[tool.uv] python-preference = "only-managed"` declares
that builds run on a uv-managed interpreter; and the installer pins the exact
CPython patch. One commit resolves to one interpreter on every box.

**The requirement is declared, the result is asserted.** The declaration lives in
`pyproject.toml`, so a single statement governs the install, a contributor's
`uv run`, and CI — and it outranks a user-level `~/.config/uv/uv.toml`, the
realistic threat. It does *not* outrank an exported `UV_PYTHON_*`, and we do not
try to win that fight: `interpreter_is_owned` checks the interpreter the build
actually landed on and refuses to mark the version good otherwise. Declaring
without verifying is a wish; verifying makes the declaration enough.

## Why

**The two pins are one decision.** uv ships a static registry of downloadable
interpreters: uv 0.9.22 knows CPython up to 3.12.12, and 3.12.13 only appears in
a later release. An exact interpreter pin therefore resolves only through a uv
new enough to know it — pinning `3.12.13` while a box's system uv is 0.9.22
fails the install outright (`No interpreter found for Python 3.12.13 in managed
installations`). Interpreter reproducibility is *conditional on owning uv*, which
is why the reuse branch goes. The two pins must move together.

**uv was the only runtime dependency borrowed from the box.**
`ensure_process_compose` never consults a system process-compose — it downloads a
pinned `v1.110.0` unconditionally — and ADR 0015 moved the broker into a locked
wheel on the principle that provisioning "should be a normal locked dependency
that fails loudly if unmet, not degrade silently". Owning uv removes an exception
rather than adding a policy.

It also closes a live defect. Because no private uv existed on a box that already
had one, the shim fell back to `command -v uv`; the systemd unit `deploy.py`
generates sets `Environment=CALFCORD_HOME` but no `Environment=PATH=`, so
`disco start` failed with `uv not found` on systemd's minimal PATH while the
interactive `disco` worked fine.

**`only-managed` declares the invariant; scrubbing enumerates threats.** Both
defeat the conda binding, but `env -u CONDA_PREFIX -u VIRTUAL_ENV` is a blocklist
that must be kept current as uv grows new ambient inputs. Declaring the
requirement closes the class.

**Why the CPython pin is not in `.python-version`.** That file drives every
contributor's `uv run` — and because the pin is only resolvable on a uv that
knows it, putting `3.12.13` there breaks the dev loop of anyone whose uv predates
the patch (it did, immediately: `No interpreter found for Python 3.12.13 in
managed installations`). The installer owns its uv, so the pin belongs where that
ownership is. `.python-version` stays the dev default, `requires-python` stays
the library's floor, and the 3.12-3.14 support matrix stays CI's, driven by
`setup-uv` rather than by either of them.

## Considered options

- **Scrub the offending variables before `uv sync`.** Verified to work today.
  Rejected: it fixes conda and waits for the next ambient input.
- **Keep reusing a system uv but record its absolute path** in the version marker,
  so the shim never needs PATH. Cheaper (no 50MB) and fixes systemd, but leaves
  the interpreter pin hostage to the box's uv version and dangles the moment the
  operator's uv is upgraded, relocated, or uninstalled.
- **Emit `Environment=PATH=` in the systemd unit.** Treats one symptom; cron,
  launchd and containers stay broken, and it does nothing for reproducibility.
- **Pin the interpreter to a minor series (`3.12`).** The status quo. Two installs
  of one commit get different patch releases — the interpreter is the only
  floating input in a build where `uv.lock` pins 164 packages exactly.
- **Relocate uv's interpreter cache under `$CALFCORD_HOME`** via
  `UV_PYTHON_INSTALL_DIR`, for a home that fully self-uninstalls. Rejected as
  inconsistent with two accepted decisions: ADR 0018 already conceded that
  `rm -rf ~/.agent-disco` is not a full uninstall, and ADR 0015's broker binary
  already lives in a shared `~/.calfkit/bin`. The interpreter stays in uv's shared
  cache and uninstall documents it.

## Consequences

- **The uv pin and the interpreter pin must be bumped together**, and the uv pin
  must be ≥ the release that first ships the pinned patch. Raising `PYTHON_VERSION`
  alone fails the install — a sharp edge, and the reason both live in this ADR and
  on adjacent lines in `scripts/install.sh`. `[tool.uv] required-version` was
  considered as a guard and rejected: the installer bootstraps its own pinned uv,
  so it always satisfies the coupling, and the setting would force every
  contributor to `uv self update` for no benefit under this design.
- **Bumping them is only safe because `ensure_uv` reuses the private uv solely at
  the pinned version.** `disco self update` re-runs the installer against an
  existing home, so an unconditional reuse would apply the new `PYTHON_VERSION`
  while keeping the old uv — leaving every *existing* install unable to update
  while fresh installs, and CI, stayed green. The version gate is what makes the
  bump discipline above actually work.
- **CPython patch updates become a maintenance commitment.** Security patches now
  arrive by editing `PYTHON_VERSION` in `scripts/install.sh` — *not*
  `.python-version`, which is only the dev default and does not touch the shipped
  interpreter — and no longer drift in for free.
- **~50MB of uv is downloaded even on boxes that already have it** — ~11% of an
  install already near 450MB, and the same order as the 40MB process-compose
  fetched unconditionally today.
- **`uv_supported()` is deleted.** It existed only to probe whether an ambient uv
  understood `uv run --env-file`; owning the binary answers that at the pin.
- **Docker is out of scope and keeps `python:3.14-slim`** — but it takes an
  explicit `ENV UV_PYTHON_PREFERENCE=only-system` in the builder stage to stay
  that way, *not* the dockerignored `.python-version`. The Dockerfile copies
  `pyproject.toml` (uv sync needs it), so `[tool.uv]` reaches the image: under
  `only-managed` uv would reject the base image's Python, download a managed
  CPython outside `/app`, and bind `/app/.venv/bin/python` to it — and since the
  runtime stage copies only `/app`, the image would ship a dangling interpreter
  while the build stayed green. CI now runs an entry point in the built image,
  because building it never caught this.
  "Reproducible" here still means reproducible for the *native* install: a
  container and a native install run different interpreter minor versions for the
  same commit, so a cp312-vs-cp314 wheel bug can reproduce in one and not the other.
- **The managed interpreter outlives `rm -rf ~/.agent-disco`** in
  `~/.local/share/uv/python`; uninstall lists it beside `~/.calfkit/bin`.
- **The provenance assertion gates reuse, not just marking.** `install_version`
  short-circuits on `.calfcord-ok`, so an install already built against conda would
  otherwise never be repaired. Checking provenance before honouring the marker lets
  existing broken installs self-heal on the next install or `disco self update`.
- `disco doctor` reports interpreter provenance, for native installs only (a
  container deliberately runs its base image's Python, so checking there would
  warn about an intentional choice). The conda binding survived unnoticed for over
  a day because nothing ever looked.
- **The declaration reaches the dev loop too**, which is a bonus rather than a
  cost: a contributor with an active conda env had their `.venv` bound to it in
  exactly the same way, and `uv sync` now rebuilds it on a managed interpreter.
- **`scripts/tests/test_hermetic_install.sh` is the only test of the headline
  promise.** The other suites stub `uv` and are network-free, and both GitHub
  runner images ship Python, so "you don't need Python installed first" was
  asserted nowhere. It runs in a `debian:bookworm-slim` container with no python3
  and builds the commit under test — which also makes it the guard that catches
  the two pins drifting apart.

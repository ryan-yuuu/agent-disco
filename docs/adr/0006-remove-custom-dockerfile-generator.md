# Remove the custom per-tool / per-agent Dockerfile generator

**Status:** accepted

We deleted the `src/calfcord/packaging/` package — the `calfcord-package-tools`
and `calfcord-package-agents` CLIs and the `dockerfile.py` templater that
rendered slim per-tool / per-agent Dockerfiles and ran `docker buildx build`
(plus the matching `[project.scripts]` entry points). calfcord no longer ships
a custom-image generator.

## Why

The generator's only job was to bake `CALFCORD_TOOLS_INCLUDE` /
`CALFCORD_TOOLS_ALIAS` into a slim image (narrowing/aliasing a host's tool
surface for multi-host splits). But those are **runtime env vars** read at boot
by `apply_deploy_filters` over the explicit `ALL_TOOLS` list — they work
identically on the canonical `calfcord:latest` image (set at `docker run`, in
compose/k8s, or bare-metal via `--env-file`). So the generator was a maintenance
surface (a Dockerfile templater that had to track the canonical `Dockerfile`,
an OS-dep map, two CLIs, and their tests) that bought only image *slimming* —
and even that was OS-deps-only (it did no Python-dep slimming, so the images
weren't materially smaller). The capability is fully retained without it; only
the build convenience is gone.

## Consequences

- **Multi-host narrowing/aliasing is env-driven on the canonical image.**
  `CALFCORD_TOOLS_INCLUDE` / `CALFCORD_TOOLS_ALIAS` on `calfcord:latest` (or on
  `calfkit-tools` bare metal) is the single mechanism. `docs/distributed-deployment.md`
  is the operator reference.
- **`calfcord deploy` is retained** — it generates *run manifests*
  (systemd / k8s reference YAML / a docker-compose override), not images. The
  k8s target already deploys one workload per process type on the shipped image
  dialing the shared broker; the docker target's `compose.override.yml` covers
  the per-agent crash isolation the deleted per-agent image builder used to.
- **The runtime alias/include logic stays** in `src/calfcord/tools/`
  (`deploy_filters.py`, `runner.py`, `__init__.py`); this removal does not touch
  the tool surface or multi-tenancy behavior from
  [ADR-0005](0005-adopt-calfkit-tools-explicit-composition.md).
- A future `calfcord tools alias` CLI (if built) manages `CALFCORD_TOOLS_ALIAS`
  in the install `.env` and is unaffected by — indeed simplified by — this
  removal (no packaging CLI to keep in sync).

"""xAI Grok model catalog — fetched live, with a pinned fallback.

xAI exposes an authenticated catalog: the rich ``GET /language-models`` (ids,
aliases, modalities, context) and the OpenAI-compatible ``GET /models``. Both
require a credential — anonymous requests 401 (verified empirically) — so the
fetch rides on the provider's own bearer / API key.

Because xAI gates OAuth API access behind a server-side allowlist (a valid login
can still 403), :meth:`GrokModelResolver.ensure_loaded` never raises: on any
failure it degrades to :data:`_FALLBACK_MODELS` and records ``source="fallback"``
so the provider keeps working and the operator sees why in the logs.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

from calfcord.providers.grok.oauth import DEFAULT_XAI_BASE_URL

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Model families that accept the ``reasoning.effort`` dial on ``/responses``.
# Conservative by design (Hermes' ``grok_supports_reasoning_effort``): an unlisted
# model gets no effort key rather than an xAI 400. Matched by prefix on the bare
# slug (aggregator prefixes like ``x-ai/`` are stripped first).
_EFFORT_CAPABLE_PREFIXES = (
    "grok-3-mini",
    "grok-4.20-multi-agent",
    "grok-4.3",
    "grok-4.5",
)

# Preferred general-purpose default, in order, when the live catalog is present.
_PREFERRED_DEFAULTS = ("grok-4.3", "grok-4.5", "grok-build-0.1")

# Keys xAI / models.dev have used for the context window, most-specific first.
_CONTEXT_LENGTH_KEYS = ("context_length", "context_window", "max_context_length", "max_input_tokens")


def fallback_model_slugs() -> list[str]:
    """The pinned fallback model ids (``grok-4.3`` first = default).

    Used by the setup wizard's curated fallback and available without a fetch.
    """
    return [model.id for model in _FALLBACK_MODELS]


def grok_supports_reasoning_effort(model: str) -> bool:
    """True when an xAI Grok model accepts a ``reasoning.effort`` value."""
    name = (model or "").strip().lower()
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return bool(name) and any(name.startswith(prefix) for prefix in _EFFORT_CAPABLE_PREFIXES)


@dataclass(frozen=True)
class GrokModel:
    """One selectable Grok model parsed from the live catalog."""

    id: str
    aliases: tuple[str, ...] = ()
    context_length: int | None = None
    input_modalities: tuple[str, ...] = ()

    @property
    def supports_reasoning_effort(self) -> bool:
        return grok_supports_reasoning_effort(self.id)

    def matches(self, name: str) -> bool:
        wanted = name.strip().lower()
        return wanted == self.id.lower() or wanted in {alias.lower() for alias in self.aliases}


# Pinned fallback (2026-07 snapshot; ``grok-4.3`` first = default). Only used when
# the live fetch is unavailable/denied — the API result always wins otherwise.
_FALLBACK_MODELS: tuple[GrokModel, ...] = (
    GrokModel(id="grok-4.3", context_length=1_000_000, input_modalities=("text", "image")),
    GrokModel(id="grok-4.5", context_length=500_000, input_modalities=("text", "image")),
    GrokModel(id="grok-build-0.1", context_length=256_000, input_modalities=("text",)),
    GrokModel(id="grok-4.20-0309-reasoning", context_length=1_000_000, input_modalities=("text",)),
    GrokModel(id="grok-4.20-0309-non-reasoning", context_length=1_000_000, input_modalities=("text",)),
    GrokModel(id="grok-4.20-multi-agent-0309", context_length=1_000_000, input_modalities=("text",)),
)
_FALLBACK_DEFAULT = "grok-4.3"


def _extract_context_length(entry: dict[str, object]) -> int | None:
    for key in _CONTEXT_LENGTH_KEYS:
        value = entry.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def _parse_catalog(payload: object) -> list[GrokModel]:
    """Parse a ``/language-models`` or ``/models`` body into models.

    Tolerant of the top-level shape (``{"models": [...]}``, OpenAI's
    ``{"data": [...]}``, or a bare list) since the two endpoints differ.
    """
    entries = payload.get("models") or payload.get("data") if isinstance(payload, dict) else payload
    # Every server-controlled value below is isinstance-guarded: a 200 body with a
    # truthy non-list at ``models``/``data`` (or a scalar ``aliases``) must yield an
    # empty catalog (→ pinned fallback), never a TypeError — ``ensure_loaded`` is
    # contracted to never raise, and the runner prewarm has no backstop.
    if not isinstance(entries, list):
        return []
    parsed: list[GrokModel] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = str(entry.get("id") or entry.get("name") or "").strip()
        if not model_id:
            continue
        raw_aliases = entry.get("aliases")
        aliases = tuple(str(a) for a in raw_aliases if a) if isinstance(raw_aliases, list) else ()
        raw_modalities = entry.get("input_modalities")
        modalities = tuple(str(m) for m in raw_modalities if m) if isinstance(raw_modalities, list) else ()
        parsed.append(
            GrokModel(
                id=model_id,
                aliases=aliases,
                context_length=_extract_context_length(entry),
                input_modalities=modalities,
            )
        )
    return parsed


@dataclass(frozen=True)
class _CatalogFetch:
    """Outcome of a catalog fetch: the models, or why the fetch failed.

    ``status`` is the last non-200 HTTP status seen (None for a network/parse
    failure or an empty catalog); ``reason`` is a human string for the log. The
    status lets callers tell an auth rejection (401/403) apart from a transient
    blip so a rejected key isn't silently masked as a benign fallback.
    """

    models: list[GrokModel] | None
    status: int | None = None
    reason: str = "no credential"


async def _fetch_catalog(client: httpx.AsyncClient, bearer: str, base_url: str) -> _CatalogFetch:
    """Fetch the catalog, trying the rich endpoint first."""
    headers = {"Authorization": f"Bearer {bearer}", "Accept": "application/json"}
    root = base_url.rstrip("/")
    last_status: int | None = None
    last_reason = "empty catalog"
    for path in ("/language-models", "/models"):
        try:
            # UnicodeError (a ValueError) fires here if a non-ASCII bearer can't be
            # header-encoded; treat it like any other fetch failure.
            response = await client.get(f"{root}{path}", headers=headers)
        except (httpx.HTTPError, UnicodeError) as exc:
            last_reason = f"network error ({exc.__class__.__name__})"
            logger.debug("xAI Grok catalog fetch %s failed: %s", path, exc)
            continue
        if response.status_code != 200:
            last_status = response.status_code
            last_reason = f"HTTP {response.status_code}"
            logger.debug("xAI Grok catalog %s returned HTTP %s", path, response.status_code)
            continue
        try:
            # ValueError = bad JSON; TypeError = a well-formed-but-wrong-shape body
            # (_parse_catalog is isinstance-guarded, but belt-and-suspenders here).
            models = _parse_catalog(response.json())
        except (ValueError, TypeError) as exc:
            last_reason = "malformed catalog body"
            logger.debug("xAI Grok catalog %s returned a malformed body: %s", path, exc)
            continue
        if models:
            return _CatalogFetch(models)
        last_reason = "empty catalog"
    return _CatalogFetch(None, status=last_status, reason=last_reason)


class GrokModelResolver:
    """In-memory Grok catalog. Load once per process via :meth:`ensure_loaded`."""

    def __init__(self) -> None:
        self._catalog: tuple[GrokModel, ...] = ()
        self._source: str = "unloaded"
        self._fallback_status: int | None = None
        self._loaded = False
        self._lock = asyncio.Lock()

    @property
    def source(self) -> str:
        """``"api"`` (live), ``"fallback"`` (pinned), or ``"unloaded"``."""
        return self._source

    @property
    def fallback_status(self) -> int | None:
        """When ``source == "fallback"``, the HTTP status that caused it (else None).

        Lets the setup wizard tell a rejected credential (401/403/400) apart from
        a transient/offline fallback and warn the operator loudly in the former.
        """
        return self._fallback_status

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    async def ensure_loaded(
        self,
        bearer: str,
        *,
        base_url: str = DEFAULT_XAI_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Populate the catalog once. Never raises — degrades to the pinned fallback."""
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            result = _CatalogFetch(None, reason="no credential")
            if bearer:
                owns_client = client is None
                active = client or httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
                try:
                    result = await _fetch_catalog(active, bearer, base_url)
                except Exception as exc:
                    # The "never raises" contract is load-bearing: the runner
                    # prewarm calls this at boot with no backstop, so an
                    # unexpected error must degrade to the fallback, not crash the
                    # Worker. (CancelledError, a BaseException, still propagates.)
                    logger.warning("xAI Grok catalog fetch raised unexpectedly (%s); using fallback.", exc)
                    result = _CatalogFetch(None, reason=f"fetch error ({exc.__class__.__name__})")
                finally:
                    if owns_client:
                        await active.aclose()
            if result.models:
                self._catalog = tuple(result.models)
                self._source = "api"
            else:
                self._catalog = _FALLBACK_MODELS
                self._source = "fallback"
                self._fallback_status = result.status
                self._warn_fallback(result)
            self._loaded = True

    @staticmethod
    def _warn_fallback(result: _CatalogFetch) -> None:
        if result.status == 403:
            logger.warning(
                "xAI Grok model catalog is 403-gated for this account (not allowlisted for "
                "OAuth API access); using the pinned fallback list (default %s). Re-login won't "
                "help — set XAI_API_KEY and use provider 'xai' instead.",
                _FALLBACK_DEFAULT,
            )
        else:
            logger.warning(
                "xAI Grok model catalog unavailable (%s); using the pinned fallback list "
                "(default %s). Configured models are not validated.",
                result.reason,
                _FALLBACK_DEFAULT,
            )

    def reset(self) -> None:
        self._catalog = ()
        self._source = "unloaded"
        self._fallback_status = None
        self._loaded = False

    def selectable_models(self) -> list[str]:
        catalog = self._catalog or _FALLBACK_MODELS
        return [model.id for model in catalog]

    def default_slug(self) -> str:
        """The default model when an agent leaves ``model:`` unset.

        Prefers a known general-purpose slug, else the first catalog entry, else
        the pinned fallback default (always available without a fetch).
        """
        # Both branches are non-empty tuples, so ``catalog`` is never empty here.
        catalog = self._catalog or _FALLBACK_MODELS
        ids = {model.id for model in catalog}
        for preferred in _PREFERRED_DEFAULTS:
            if preferred in ids:
                return preferred
        return catalog[0].id

    def is_known(self, model: str) -> bool:
        """True when ``model`` matches a catalog id or alias."""
        return any(entry.matches(model) for entry in (self._catalog or _FALLBACK_MODELS))


_default_resolver: GrokModelResolver = GrokModelResolver()


def get_default_resolver() -> GrokModelResolver:
    """The process-wide singleton, prewarmed by the runner."""
    return _default_resolver


def reset_default_resolver() -> None:
    """Reset the singleton (used by tests; ``grok models`` resets it directly)."""
    _default_resolver.reset()


async def prewarm_grok_models(
    bearer: str,
    *,
    base_url: str = DEFAULT_XAI_BASE_URL,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Load the default resolver's catalog (best-effort; never raises)."""
    await _default_resolver.ensure_loaded(bearer, base_url=base_url, client=client)

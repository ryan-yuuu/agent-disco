"""Runtime resolution of a usable xAI Grok access token.

Ties :mod:`oauth` (wire flow) to :mod:`token_store` (persistence): read the
stored session, refresh the access token when its JWT is expiring, persist the
rotated tokens, and quarantine a dead grant. Used by the per-request bearer auth
(:mod:`calfcord.providers.grok.model_client`) and the ``calfkit-auth grok``
status/refresh commands.

The refresh critical section runs under a cross-process file lock with a
double-checked expiry re-read, mirroring Hermes' ``resolve_xai_oauth_runtime_credentials``:
xAI rotates single-use refresh tokens, so two processes (or two async callers)
must not refresh the same token concurrently.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import replace

import httpx
from filelock import Timeout as LockTimeout

from calfcord.providers.grok import oauth, token_store
from calfcord.providers.grok.oauth import GrokAuthError
from calfcord.providers.grok.token_store import GrokCredentials

logger = logging.getLogger(__name__)

_REFRESH_TIMEOUT_SECONDS = 20.0
_LOGIN_HINT = "Run: uv run calfkit-auth grok login"


class GrokNotLoggedInError(GrokAuthError):
    """No usable xAI Grok credentials are cached (logged out or quarantined)."""

    def __init__(self, message: str = f"No xAI Grok credentials cached. {_LOGIN_HINT}") -> None:
        super().__init__(message, code="not_logged_in", relogin_required=True)


def _should_refresh(
    creds: GrokCredentials,
    *,
    force_refresh: bool,
    refresh_if_expiring: bool,
    refresh_skew_seconds: int | None,
) -> bool:
    if force_refresh:
        return True
    if not refresh_if_expiring:
        return False
    skew = (
        refresh_skew_seconds
        if refresh_skew_seconds is not None
        else oauth.proactive_refresh_skew_seconds(creds.access_token)
    )
    return oauth.access_token_is_expiring(creds.access_token, skew)


async def resolve_credentials(
    *,
    force_refresh: bool = False,
    refresh_if_expiring: bool = True,
    refresh_skew_seconds: int | None = None,
    client: httpx.AsyncClient | None = None,
) -> GrokCredentials:
    """Return current credentials, refreshing (once, under lock) if the token is expiring.

    Raises:
        GrokNotLoggedInError: when no usable session is stored.
        GrokAuthError: when a refresh fails terminally (relogin or tier-gate).
    """
    creds = token_store.load_credentials()
    if creds is None:
        raise GrokNotLoggedInError()
    if not _should_refresh(
        creds,
        force_refresh=force_refresh,
        refresh_if_expiring=refresh_if_expiring,
        refresh_skew_seconds=refresh_skew_seconds,
    ):
        return creds

    lock = token_store.credential_lock()
    try:
        try:
            await asyncio.to_thread(lock.acquire)
        except LockTimeout as exc:
            # Sustained contention (another process refreshing) — surface an
            # actionable auth error rather than a raw OSError to the per-request
            # bearer auth / CLI.
            raise GrokAuthError(
                "Timed out acquiring the xAI Grok refresh lock (another process may be "
                "refreshing). Retry shortly.",
                code="xai_refresh_lock_timeout",
            ) from exc
        # Re-read under the lock: another process/coroutine may have refreshed
        # (or quarantined) while we waited, so we don't burn a second refresh.
        creds = token_store.load_credentials()
        if creds is None:
            raise GrokNotLoggedInError()
        if not _should_refresh(
            creds,
            force_refresh=force_refresh,
            refresh_if_expiring=refresh_if_expiring,
            refresh_skew_seconds=refresh_skew_seconds,
        ):
            return creds
        return await _refresh(creds, client)
    finally:
        # ``is_locked`` guards the cancellation window: if the acquire was
        # cancelled (or timed out) we never held the lock, so we must not release.
        if lock.is_locked:
            lock.release()


async def resolve_access_token(
    *,
    force_refresh: bool = False,
    refresh_if_expiring: bool = True,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Convenience wrapper returning just the bearer string."""
    creds = await resolve_credentials(
        force_refresh=force_refresh, refresh_if_expiring=refresh_if_expiring, client=client
    )
    return creds.access_token


async def _refresh(creds: GrokCredentials, client: httpx.AsyncClient | None) -> GrokCredentials:
    owns_client = client is None
    active = client or httpx.AsyncClient(timeout=httpx.Timeout(_REFRESH_TIMEOUT_SECONDS))
    try:
        try:
            updated = await oauth.refresh_tokens(
                active, refresh_token=creds.refresh_token, token_endpoint=creds.token_endpoint
            )
        except GrokAuthError as exc:
            # A revoked/invalid grant (400/401) is dead — clear it so the next
            # call fails fast with a login hint. A 403 tier-gate leaves the
            # (valid) tokens in place; the operator switches to XAI_API_KEY.
            # Quarantine is a best-effort side effect: a disk failure here must
            # NOT replace the actionable GrokAuthError with an opaque OSError.
            if exc.relogin_required:
                with contextlib.suppress(OSError):
                    token_store.quarantine_credentials(code=exc.code or "xai_refresh_failed", message=str(exc))
            raise
        refreshed = replace(
            creds,
            access_token=updated["access_token"],
            refresh_token=updated["refresh_token"],
            id_token=updated["id_token"] or creds.id_token,
            expires_in=updated["expires_in"] if updated["expires_in"] is not None else creds.expires_in,
            token_type=updated["token_type"],
            last_refresh=updated["last_refresh"],
        )
        try:
            token_store.save_credentials(refreshed)
        except OSError as exc:
            # xAI already rotated (and invalidated) the old refresh token, so the
            # in-memory `refreshed` is the only valid grant — and we couldn't
            # persist it. The next process would replay the dead token. Surface
            # this as an actionable auth error rather than a bare OSError.
            raise GrokAuthError(
                f"Refreshed the xAI Grok token but could not persist it ({exc}); the "
                f"rotated refresh token is lost. Check the auth dir, then re-run: "
                f"uv run calfkit-auth grok login",
                code="xai_refresh_persist_failed",
                relogin_required=True,
            ) from exc
        logger.info("Refreshed xAI Grok access token (new expiry from JWT).")
        return refreshed
    finally:
        if owns_client:
            await active.aclose()

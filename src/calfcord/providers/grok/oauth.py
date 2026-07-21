"""xAI Grok OAuth 2.0 Device Authorization Grant (RFC 8628).

A faithful async port of NousResearch/hermes-agent's ``_xai_oauth_*`` functions
(MIT, © 2025 Nous Research). The wire contract is identical — same issuer,
public ``grok-cli`` client id, scopes, grant types, and error classification —
so a login here is indistinguishable from Hermes' on xAI's side.

Why device-code (not authorization-code + PKCE loopback): Agent Disco's bridge
and agents are server-deployed and an operator authenticates from a CLI that
may be running over SSH. The device grant needs no local callback server and
no graphical browser on the host, so it works headless.

Security invariants carried over from Hermes:

* The ``token_endpoint`` is read from OIDC discovery and **pinned to the xAI
  origin** (:func:`validate_oauth_endpoint`) before it is ever used or cached.
  It receives the refresh token on every future refresh; a single MITM at
  discovery time that substituted a foreign endpoint would otherwise be a
  permanent credential leak.
* The inference ``base_url`` is likewise pinned (:func:`validate_inference_base_url`)
  so a tampered ``XAI_BASE_URL`` env override cannot ship the bearer elsewhere.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import math
import time
import webbrowser
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# --- Constants (pinned to the public xAI "grok-cli" desktop OAuth client) -----
XAI_OAUTH_ISSUER = "https://auth.x.ai"
XAI_OAUTH_DISCOVERY_URL = f"{XAI_OAUTH_ISSUER}/.well-known/openid-configuration"
XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_OAUTH_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_OAUTH_DEVICE_CODE_URL = f"{XAI_OAUTH_ISSUER}/oauth2/device/code"
XAI_OAUTH_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

DEFAULT_XAI_BASE_URL = "https://api.x.ai/v1"

# How far before JWT ``exp`` to proactively refresh. SuperGrok sessions can ship
# multi-hour tokens where a wide window is fine; device-code logins often return
# ~15-minute JWTs where the full window would burn a single-use refresh token on
# every credential resolution — :func:`proactive_refresh_skew_seconds` shrinks it.
REFRESH_SKEW_SECONDS = 3600

_FORM_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json",
}

Sleep = Callable[[float], Awaitable[None]]


class GrokAuthError(RuntimeError):
    """A failure in the xAI Grok OAuth flow.

    ``code`` is a stable machine-readable tag; ``relogin_required`` distinguishes
    "your grant is dead, re-run login" (400/401) from "your account isn't
    allowlisted for OAuth API access, re-login won't help" (403 tier gate).
    """

    def __init__(self, message: str, *, code: str | None = None, relogin_required: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.relogin_required = relogin_required


# --- Host pinning -------------------------------------------------------------
def _is_x_ai_host(host: str) -> bool:
    host = host.lower()
    return host == "x.ai" or host.endswith(".x.ai")


def validate_oauth_endpoint(url: str, *, field: str) -> str:
    """Refuse any OIDC endpoint that isn't HTTPS on the xAI origin.

    The discovery response is cached and its ``token_endpoint`` receives every
    future refresh token; pinning scheme + host to ``x.ai`` / ``*.x.ai`` denies a
    one-time MITM the persistence it would otherwise gain.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise GrokAuthError(
            f"xAI OIDC discovery returned a non-HTTPS {field}: {url!r}.",
            code="xai_discovery_invalid",
        )
    host = (parsed.hostname or "").lower()
    if not host or not _is_x_ai_host(host):
        raise GrokAuthError(
            f"xAI OIDC discovery {field} host {host!r} is not on the xAI origin "
            f"(expected x.ai or a *.x.ai subdomain). Refusing a possibly "
            f"substituted endpoint; re-run: uv run calfkit-auth grok login",
            code="xai_discovery_invalid",
        )
    return url


def validate_inference_base_url(value: str, *, fallback: str) -> str:
    """Pin the OAuth inference origin to xAI; fall back (not raise) on a bad override.

    ``XAI_BASE_URL`` lets an operator repoint inference (staging/proxy), but it is
    also a bearer-leak vector: a hostile value would ship the OAuth access token
    to a third party on every request. A bad override should not deadlock auth, so
    warn and fall back rather than raise.
    """
    candidate = (value or "").strip().rstrip("/")
    if not candidate:
        return fallback
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host or not _is_x_ai_host(host):
        logger.warning(
            "Refusing xAI base_url override %r (not HTTPS on the xAI origin); the "
            "xai-grok bearer is only valid against xAI and must not be sent "
            "elsewhere. Falling back to %s.",
            candidate,
            fallback,
        )
        return fallback
    return candidate


# --- JWT expiry helpers -------------------------------------------------------
def _jwt_payload(access_token: str) -> dict[str, Any] | None:
    """Decode a JWT's payload dict without verifying the signature (xAI verifies)."""
    if not isinstance(access_token, str) or access_token.count(".") < 2:
        return None
    payload_b64 = access_token.split(".")[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("ascii")))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _jwt_number(access_token: str, claim: str) -> float | None:
    """Return a finite numeric JWT claim as a float, or ``None``.

    ``bool`` is rejected (not a real numeric claim), and — because JSON integers
    are arbitrary-precision — a pathological oversized/``Infinity``/``NaN`` value
    (only reachable via a corrupt/tampered credential file) is coerced to ``None``
    rather than raising ``OverflowError``, which would otherwise escape every
    caller and crash the runner at boot.
    """
    payload = _jwt_payload(access_token)
    value = payload.get(claim) if payload else None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def decode_jwt_exp(access_token: str) -> float | None:
    """Return the ``exp`` claim (Unix seconds) of a JWT, or ``None`` if unreadable.

    We only need ``exp`` to decide when to refresh.
    """
    return _jwt_number(access_token, "exp")


def access_token_is_expiring(access_token: str, skew_seconds: int = 0) -> bool:
    """True when the JWT ``exp`` is within ``skew_seconds`` of now (opaque -> False)."""
    exp = decode_jwt_exp(access_token)
    if exp is None:
        return False
    return exp <= (time.time() + max(0, int(skew_seconds)))


def proactive_refresh_skew_seconds(access_token: str) -> int:
    """How far before ``exp`` to proactively refresh, keyed off the token's lifetime.

    The window is a fraction of the token's *total* lifetime (from ``iat`` to
    ``exp``), capped at the gateway-oriented :data:`REFRESH_SKEW_SECONDS` and
    floored at 120s. Keying off lifetime rather than the current remaining time
    avoids two failure modes: a fixed hour-long window would make a ~15-minute
    device-code token read as "expiring" from birth (refresh on every call →
    single-use-refresh-token thrash), while shrinking to a flat 120s would leave a
    multi-hour SuperGrok token refreshing only in its final two minutes.
    """
    exp = decode_jwt_exp(access_token)
    if exp is None or exp - time.time() <= 0:
        # Opaque or already expired: use the wide window (a no-op for opaque, since
        # ``access_token_is_expiring`` returns False without an ``exp``).
        return REFRESH_SKEW_SECONDS
    iat = _jwt_number(access_token, "iat")
    if iat is not None and exp > iat:
        lifetime = exp - iat
        return int(min(REFRESH_SKEW_SECONDS, max(120, lifetime * 0.2)))
    # No ``iat`` to size the window: a modest fixed lead, safely below the
    # ~15-minute device-code tokens this provider issues.
    return min(300, REFRESH_SKEW_SECONDS)


# --- Wire calls ---------------------------------------------------------------
# Every wire call funnels transport + JSON-decode failures through these two
# helpers so they all raise ``GrokAuthError`` (never a bare ``httpx.HTTPError``
# or ``json.JSONDecodeError``). Downstream handlers — the CLI, the wizard, the
# runner prewarm, the per-request bearer auth — all catch ``GrokAuthError``, so
# an unnormalized transport error would escape them and, e.g., crash agent
# startup on a boot-time network blip.
async def _post(client: httpx.AsyncClient, url: str, *, data: dict[str, str]) -> httpx.Response:
    try:
        return await client.post(url, headers=_FORM_HEADERS, data=data)
    except httpx.HTTPError as exc:
        raise GrokAuthError(f"xAI request to {url} failed: {exc}", code="xai_network_error") from exc


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise GrokAuthError(
            f"xAI returned a non-JSON response (HTTP {response.status_code}): {exc}",
            code="xai_invalid_response",
        ) from exc


async def discover_endpoints(client: httpx.AsyncClient) -> dict[str, str]:
    """GET the OIDC discovery doc and return its host-pinned endpoints."""
    try:
        response = await client.get(XAI_OAUTH_DISCOVERY_URL, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise GrokAuthError(f"xAI OIDC discovery failed: {exc}", code="xai_discovery_failed") from exc
    if response.status_code != 200:
        raise GrokAuthError(
            f"xAI OIDC discovery returned status {response.status_code}.",
            code="xai_discovery_failed",
        )
    payload = _json(response)
    if not isinstance(payload, dict):
        raise GrokAuthError("xAI OIDC discovery response was not a JSON object.", code="xai_discovery_incomplete")
    authorization_endpoint = str(payload.get("authorization_endpoint", "") or "").strip()
    token_endpoint = str(payload.get("token_endpoint", "") or "").strip()
    if not authorization_endpoint or not token_endpoint:
        raise GrokAuthError(
            "xAI OIDC discovery response was missing required endpoints.",
            code="xai_discovery_incomplete",
        )
    validate_oauth_endpoint(authorization_endpoint, field="authorization_endpoint")
    validate_oauth_endpoint(token_endpoint, field="token_endpoint")
    return {"authorization_endpoint": authorization_endpoint, "token_endpoint": token_endpoint}


async def request_device_code(client: httpx.AsyncClient, *, scope: str = XAI_OAUTH_SCOPE) -> dict[str, Any]:
    """Start the device grant: returns ``device_code`` / ``user_code`` / URLs."""
    response = await _post(
        client,
        XAI_OAUTH_DEVICE_CODE_URL,
        data={"client_id": XAI_OAUTH_CLIENT_ID, "scope": scope},
    )
    if response.status_code != 200:
        raise GrokAuthError(
            f"xAI device-code request failed (HTTP {response.status_code})."
            + (f" Response: {response.text.strip()}" if response.text else ""),
            code="device_code_request_failed",
        )
    payload = _json(response)
    if not isinstance(payload, dict):
        raise GrokAuthError("xAI device-code response was not a JSON object.", code="device_code_invalid")
    required = (
        "device_code",
        "user_code",
        "verification_uri",
        "verification_uri_complete",
        "expires_in",
        "interval",
    )
    missing = [key for key in required if key not in payload]
    if missing:
        raise GrokAuthError(
            f"xAI device-code response missing fields: {', '.join(missing)}",
            code="device_code_invalid",
        )
    # Coerce + validate the numeric fields here so the poll loop's downstream
    # int() calls can't raise a bare ValueError/TypeError past the login handlers.
    for numeric in ("expires_in", "interval"):
        try:
            payload[numeric] = int(payload[numeric])
        except (ValueError, TypeError) as exc:
            raise GrokAuthError(
                f"xAI device-code field {numeric!r} is not an integer: {payload.get(numeric)!r}",
                code="device_code_invalid",
            ) from exc
    return payload


async def poll_device_token(
    client: httpx.AsyncClient,
    *,
    token_endpoint: str,
    device_code: str,
    expires_in: int,
    interval: int,
    sleep: Sleep = asyncio.sleep,
) -> dict[str, Any]:
    """Poll ``token_endpoint`` until the user approves, honoring RFC 8628 pacing."""
    deadline = time.monotonic() + max(0.0, float(expires_in))
    current_interval = max(1, int(interval))
    while time.monotonic() < deadline:
        response = await _post(
            client,
            token_endpoint,
            data={
                "grant_type": XAI_OAUTH_DEVICE_GRANT,
                "client_id": XAI_OAUTH_CLIENT_ID,
                "device_code": device_code,
            },
        )
        if response.status_code == 200:
            payload = _json(response)
            if not isinstance(payload, dict) or not payload.get("access_token") or not payload.get("refresh_token"):
                raise GrokAuthError(
                    "xAI device-code token response was missing access_token/refresh_token.",
                    code="xai_device_token_invalid",
                )
            return payload

        error_code = ""
        with contextlib.suppress(json.JSONDecodeError, ValueError, AttributeError):
            error_code = str((response.json() or {}).get("error") or "")
        if error_code == "authorization_pending":
            await sleep(current_interval)
            continue
        if error_code == "slow_down":
            current_interval = min(current_interval + 1, 30)
            await sleep(current_interval)
            continue
        raise GrokAuthError(
            f"xAI device-code authorization failed: {error_code or response.text.strip()}",
            code="xai_device_token_failed",
        )
    raise GrokAuthError(
        "Timed out waiting for xAI device authorization.",
        code="device_code_timeout",
    )


async def refresh_tokens(
    client: httpx.AsyncClient,
    *,
    refresh_token: str,
    token_endpoint: str,
) -> dict[str, Any]:
    """Exchange a refresh token for a fresh access token (rotating the refresh token).

    ``token_endpoint`` is re-validated on this hot path: an on-disk store written
    by an older/edited install could carry a foreign endpoint that would receive
    the refresh token in plaintext.
    """
    if not isinstance(refresh_token, str) or not refresh_token.strip():
        raise GrokAuthError(
            "xAI Grok credentials are missing a refresh_token. Re-run: "
            "uv run calfkit-auth grok login",
            code="xai_auth_missing_refresh_token",
            relogin_required=True,
        )
    validate_oauth_endpoint(token_endpoint, field="token_endpoint")
    response = await _post(
        client,
        token_endpoint,
        data={
            "grant_type": "refresh_token",
            "client_id": XAI_OAUTH_CLIENT_ID,
            "refresh_token": refresh_token,
        },
    )
    if response.status_code != 200:
        detail = response.text.strip()
        if response.status_code == 403:
            raise GrokAuthError(
                "xAI token refresh failed with HTTP 403."
                + (f" Response: {detail}" if detail else "")
                + " This account is not authorized for xAI OAuth API access — xAI "
                "restricts it to some SuperGrok / Premium+ tiers. Re-logging in "
                "won't change that; set XAI_API_KEY and use `provider: xai` instead.",
                code="xai_oauth_tier_denied",
                relogin_required=False,
            )
        raise GrokAuthError(
            "xAI token refresh failed." + (f" Response: {detail}" if detail else ""),
            code="xai_refresh_failed",
            relogin_required=response.status_code in {400, 401},
        )
    payload = _json(response)
    if not isinstance(payload, dict):
        raise GrokAuthError(
            "xAI token refresh response was not a JSON object.",
            code="xai_refresh_invalid_response",
            relogin_required=False,
        )
    access_token = str(payload.get("access_token", "") or "").strip()
    if not access_token:
        raise GrokAuthError(
            "xAI token refresh response was missing access_token.",
            code="xai_refresh_missing_access_token",
            relogin_required=True,
        )
    return {
        "access_token": access_token,
        # A missing refresh_token means "keep the old one" (RFC 6749 §5.1).
        "refresh_token": str(payload.get("refresh_token") or refresh_token).strip(),
        "id_token": str(payload.get("id_token") or "").strip(),
        "expires_in": payload.get("expires_in"),
        "token_type": str(payload.get("token_type") or "Bearer").strip() or "Bearer",
        "last_refresh": _utc_now_iso(),
    }


@contextlib.asynccontextmanager
async def _borrowed_or_owned_client(
    client: httpx.AsyncClient | None, timeout_seconds: float
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Yield ``client`` untouched, or a temporary one that is closed on exit."""
    if client is not None:
        yield client
        return
    timeout = httpx.Timeout(max(20.0, timeout_seconds))
    async with httpx.AsyncClient(timeout=timeout, headers={"Accept": "application/json"}) as owned:
        yield owned


async def device_code_login(
    *,
    open_browser: bool = True,
    timeout_seconds: float = 20.0,
    client: httpx.AsyncClient | None = None,
    printer: Callable[[str], None] = print,
    browser_opener: Callable[[str], bool] | None = None,
    sleep: Sleep = asyncio.sleep,
) -> dict[str, Any]:
    """Run the full device-code login and return a flat credentials dict.

    ``client`` is injectable for tests; when omitted a client is created and
    closed here. The returned dict is what :mod:`token_store` persists.
    """
    async with _borrowed_or_owned_client(client, timeout_seconds) as active:
        return await _device_code_login(
            active,
            open_browser=open_browser,
            printer=printer,
            browser_opener=browser_opener,
            sleep=sleep,
        )


async def _device_code_login(
    client: httpx.AsyncClient,
    *,
    open_browser: bool,
    printer: Callable[[str], None],
    browser_opener: Callable[[str], bool] | None,
    sleep: Sleep,
) -> dict[str, Any]:
    discovery = await discover_endpoints(client)
    device = await request_device_code(client)
    verification_url = str(device.get("verification_uri_complete") or device["verification_uri"])
    user_code = str(device["user_code"])

    printer("")
    printer("To authorize Agent Disco with your Grok subscription:")
    printer(f"  1. Open: {verification_url}")
    printer(f"  2. If prompted, enter code: {user_code}")
    if open_browser:
        opener = browser_opener or webbrowser.open
        with contextlib.suppress(Exception):
            opener(verification_url)
    printer(f"Waiting for approval (polling every {max(1, int(device['interval']))}s)...")

    tokens = await poll_device_token(
        client,
        token_endpoint=discovery["token_endpoint"],
        device_code=str(device["device_code"]),
        expires_in=int(device["expires_in"]),
        interval=int(device["interval"]),
        sleep=sleep,
    )
    return {
        "access_token": str(tokens["access_token"]).strip(),
        "refresh_token": str(tokens["refresh_token"]).strip(),
        "id_token": str(tokens.get("id_token") or "").strip(),
        "expires_in": tokens.get("expires_in"),
        "token_type": str(tokens.get("token_type") or "Bearer").strip() or "Bearer",
        "token_endpoint": discovery["token_endpoint"],
        "authorization_endpoint": discovery["authorization_endpoint"],
        "base_url": DEFAULT_XAI_BASE_URL,
        "last_refresh": _utc_now_iso(),
    }


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")

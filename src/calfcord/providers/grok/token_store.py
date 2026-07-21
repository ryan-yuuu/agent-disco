"""On-disk persistence for xAI Grok OAuth credentials.

A single atomic JSON file, ``$CALFCORD_HOME/auth/xai_oauth.json`` (mode 0600),
vendor-namespaced so it sits beside the codex store in the shared auth dir
(override with ``CALFCORD_AUTH_DIR``). Unlike codex — which delegates to
OpenHands' ``CredentialStore`` — we own this format because we own the xAI OAuth
flow, and OpenHands has no xAI vendor.

Concurrency: xAI rotates single-use refresh tokens, so two processes refreshing
at once would invalidate each other. :func:`credential_lock` returns a
cross-process ``filelock.FileLock`` for the read-refresh-write critical section
(see :mod:`calfcord.providers.grok.credentials`).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock

from calfcord.providers._paths import provider_auth_dir
from calfcord.providers.grok.oauth import DEFAULT_XAI_BASE_URL

logger = logging.getLogger(__name__)

_CREDENTIALS_FILENAME = "xai_oauth.json"
_LOCK_FILENAME = "xai_oauth.lock"
_AUTH_MODE = "oauth_device_code"


@dataclass(frozen=True)
class GrokCredentials:
    """A logged-in xAI Grok OAuth session.

    ``token_endpoint`` is persisted (not just discovered) so a routine refresh
    needs no network round-trip to the discovery doc; it is re-validated against
    the xAI origin on use.
    """

    access_token: str
    refresh_token: str
    token_endpoint: str
    id_token: str = ""
    token_type: str = "Bearer"
    expires_in: int | None = None
    base_url: str = DEFAULT_XAI_BASE_URL
    authorization_endpoint: str = ""
    last_refresh: str = ""
    auth_mode: str = _AUTH_MODE

    @classmethod
    def from_login(cls, payload: dict[str, Any]) -> GrokCredentials:
        """Build credentials from an :func:`oauth.device_code_login` result.

        Keys the dataclass doesn't declare (e.g. ``last_auth_error`` metadata)
        are ignored — the accepted set derives from the fields themselves, so it
        can't drift when a field is added.
        """
        accepted = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in payload.items() if key in accepted})


def credentials_path() -> Path:
    return provider_auth_dir() / _CREDENTIALS_FILENAME


def _lock_path() -> Path:
    return provider_auth_dir() / _LOCK_FILENAME


def credential_lock(timeout: float = 30.0) -> FileLock:
    """A cross-process lock guarding the refresh critical section.

    ``thread_local=False`` is required: :func:`credentials.resolve_credentials`
    acquires this lock inside an ``asyncio.to_thread`` worker (so the event loop
    isn't blocked while waiting) but releases it on the event-loop thread. With
    filelock's default thread-local state, the release would run on a thread that
    never acquired and silently no-op, leaking the OS lock and deadlocking the
    next waiter.
    """
    _lock_path().parent.mkdir(parents=True, exist_ok=True)
    return FileLock(str(_lock_path()), timeout=timeout, thread_local=False)


def _read_raw() -> dict[str, Any] | None:
    path = credentials_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except OSError as exc:
        # Permission denied, is-a-directory, etc. — an unreadable file reads as
        # "logged out" (loud, actionable) rather than crashing callers that only
        # expect a missing-file/None outcome.
        logger.warning("xAI Grok credential file at %s could not be read (%s); treating as logged out.", path, exc)
        return None
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        logger.warning("xAI Grok credential file at %s is unreadable (%s); treating as logged out.", path, exc)
        return None
    return data if isinstance(data, dict) else None


def load_credentials() -> GrokCredentials | None:
    """Load a usable session, or ``None`` if logged out / quarantined / malformed."""
    raw = _read_raw()
    if not raw:
        return None
    access_token = str(raw.get("access_token") or "").strip()
    refresh_token = str(raw.get("refresh_token") or "").strip()
    if not access_token or not refresh_token:
        # Cleared by quarantine, or a partial write: not a usable session.
        return None
    try:
        return GrokCredentials.from_login(raw)
    except TypeError as exc:
        logger.warning("xAI Grok credential file has an unexpected shape (%s); treating as logged out.", exc)
        return None


def _atomic_write(payload: dict[str, Any]) -> None:
    directory = credentials_path().parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".xai_oauth-", suffix=".tmp")
    try:
        # fdopen adopts the fd immediately, so fchmod (inside the context) can't
        # leak it even if it raises; the context manager always closes it.
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(payload, handle, indent=2, sort_keys=True)
        os.replace(tmp_name, credentials_path())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def save_credentials(credentials: GrokCredentials) -> None:
    """Persist ``credentials`` atomically with owner-only permissions."""
    payload = asdict(credentials)
    payload["last_auth_error"] = None
    _atomic_write(payload)


def delete_credentials() -> bool:
    """Remove the credential file; return whether it existed."""
    try:
        credentials_path().unlink()
        return True
    except FileNotFoundError:
        return False


def quarantine_credentials(*, code: str, message: str) -> None:
    """Strip the (now-dead) tokens and record why, so the next session fails fast.

    A no-op when nothing is stored — we never fabricate a phantom credential file.
    """
    raw = _read_raw()
    if not raw:
        return
    raw["access_token"] = ""
    raw["refresh_token"] = ""
    raw["last_auth_error"] = {
        "code": code,
        "message": message,
        "at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    _atomic_write(raw)


def load_auth_error() -> dict[str, Any] | None:
    """Return the recorded quarantine error, if any."""
    raw = _read_raw()
    error = raw.get("last_auth_error") if raw else None
    return error if isinstance(error, dict) else None

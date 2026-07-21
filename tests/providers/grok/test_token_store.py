"""Tests for the on-disk xAI Grok credential store.

The store is a small atomic JSON file under ``$CALFCORD_HOME/auth/`` (0600),
vendor-namespaced ``xai_oauth.json`` so it coexists with the codex store. A
cross-process file lock guards the refresh critical section, because xAI rotates
single-use refresh tokens and two processes refreshing at once would invalidate
each other.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from calfcord.providers.grok import token_store
from calfcord.providers.grok.token_store import GrokCredentials


@pytest.fixture(autouse=True)
def _auth_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("CALFCORD_AUTH_DIR", str(tmp_path / "auth"))
    return tmp_path / "auth"


def _creds(**overrides: object) -> GrokCredentials:
    base = dict(
        access_token="at-1",
        refresh_token="rt-1",
        token_endpoint="https://auth.x.ai/oauth2/token",
        id_token="id-1",
        base_url="https://api.x.ai/v1",
        last_refresh="2026-07-20T00:00:00Z",
    )
    base.update(overrides)
    return GrokCredentials(**base)  # type: ignore[arg-type]


class TestSaveLoadRoundTrip:
    def test_save_then_load_preserves_fields(self) -> None:
        token_store.save_credentials(_creds())
        loaded = token_store.load_credentials()
        assert loaded is not None
        assert loaded.access_token == "at-1"
        assert loaded.refresh_token == "rt-1"
        assert loaded.token_endpoint == "https://auth.x.ai/oauth2/token"
        assert loaded.base_url == "https://api.x.ai/v1"

    def test_load_returns_none_when_absent(self) -> None:
        assert token_store.load_credentials() is None

    def test_load_returns_none_for_malformed_file(self, _auth_dir: Path) -> None:
        _auth_dir.mkdir(parents=True, exist_ok=True)
        token_store.credentials_path().write_text("{ not json", encoding="utf-8")
        assert token_store.load_credentials() is None

    def test_credentials_path_under_auth_dir(self, _auth_dir: Path) -> None:
        assert token_store.credentials_path() == _auth_dir / "xai_oauth.json"

    def test_load_returns_none_when_path_is_unreadable(self, _auth_dir: Path) -> None:
        # An unreadable cred path (here a directory where the file should be, an
        # OSError on read) reads as logged out rather than crashing callers.
        token_store.credentials_path().mkdir(parents=True)
        assert token_store.load_credentials() is None


class TestPermissionsAndAtomicity:
    def test_saved_file_is_owner_only(self) -> None:
        token_store.save_credentials(_creds())
        mode = stat.S_IMODE(os.stat(token_store.credentials_path()).st_mode)
        assert mode == 0o600

    def test_save_leaves_no_temp_file_behind(self, _auth_dir: Path) -> None:
        token_store.save_credentials(_creds())
        leftovers = [p.name for p in _auth_dir.iterdir() if p.name != "xai_oauth.json"]
        assert leftovers == []

    def test_overwrite_replaces_content(self) -> None:
        token_store.save_credentials(_creds(access_token="old"))
        token_store.save_credentials(_creds(access_token="new"))
        loaded = token_store.load_credentials()
        assert loaded is not None and loaded.access_token == "new"


class TestDelete:
    def test_delete_removes_file_and_reports_true(self) -> None:
        token_store.save_credentials(_creds())
        assert token_store.delete_credentials() is True
        assert token_store.load_credentials() is None

    def test_delete_reports_false_when_absent(self) -> None:
        assert token_store.delete_credentials() is False


class TestQuarantine:
    def test_quarantine_clears_tokens_and_records_error(self) -> None:
        token_store.save_credentials(_creds())
        token_store.quarantine_credentials(code="xai_refresh_failed", message="token revoked")
        # A quarantined store reads as "not usable" so callers fail fast.
        assert token_store.load_credentials() is None
        raw = json.loads(token_store.credentials_path().read_text(encoding="utf-8"))
        assert raw["access_token"] == ""
        assert raw["refresh_token"] == ""
        assert raw["last_auth_error"]["code"] == "xai_refresh_failed"

    def test_quarantine_is_noop_when_not_logged_in(self) -> None:
        # Nothing to quarantine; must not create a phantom file.
        token_store.quarantine_credentials(code="x", message="y")
        assert not token_store.credentials_path().exists()


class TestLock:
    def test_credential_lock_targets_a_sibling_lockfile(self, _auth_dir: Path) -> None:
        lock = token_store.credential_lock()
        assert Path(lock.lock_file).parent == _auth_dir
        assert Path(lock.lock_file).name.startswith("xai_oauth")

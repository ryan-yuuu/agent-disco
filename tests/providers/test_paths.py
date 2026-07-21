"""Tests for the shared provider path resolution helpers.

These back both the codex and grok auth stores, which persist credentials
under the install root so they move with a relocated / per-host install.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from calfcord.providers._paths import calfcord_home, provider_auth_dir


class TestCalfcordHome:
    def test_honors_calfcord_home_when_set(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "opt" / "calfcord"))
        assert calfcord_home() == tmp_path / "opt" / "calfcord"

    def test_falls_back_to_dot_agent_disco_when_unset(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("CALFCORD_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert calfcord_home() == tmp_path / ".agent-disco"

    def test_empty_calfcord_home_counts_as_unset(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("CALFCORD_HOME", "")
        monkeypatch.setenv("HOME", str(tmp_path))
        assert calfcord_home() == tmp_path / ".agent-disco"


class TestProviderAuthDir:
    def test_honors_calfcord_auth_dir_override(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("CALFCORD_AUTH_DIR", str(tmp_path / "creds"))
        assert provider_auth_dir() == tmp_path / "creds"

    def test_falls_back_to_auth_under_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("CALFCORD_AUTH_DIR", raising=False)
        monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "install"))
        assert provider_auth_dir() == tmp_path / "install" / "auth"

    def test_empty_auth_dir_override_counts_as_unset(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # A stray ``CALFCORD_AUTH_DIR=`` must not root the credential dir at
        # ``/auth`` — fall through to ``$CALFCORD_HOME/auth`` like the override
        # were absent.
        monkeypatch.setenv("CALFCORD_AUTH_DIR", "")
        monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "install"))
        assert provider_auth_dir() == tmp_path / "install" / "auth"

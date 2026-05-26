"""Tests for the shared workspace-root resolver."""

from __future__ import annotations

from pathlib import Path

import pytest

from calfkit_organization.tools.builtin import workspace


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    """Clear the resolver cache before each test so env-var overrides take effect."""
    workspace._reset_cache_for_tests()
    yield
    workspace._reset_cache_for_tests()


class TestGetWorkspaceRoot:
    def test_env_var_wins(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        target = tmp_path / "ws"
        monkeypatch.setenv("CALFCORD_WORKSPACE_DIR", str(target))
        root = workspace.get_workspace_root()
        assert root == target.resolve()
        assert root.is_dir()

    def test_creates_directory_if_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        target = tmp_path / "deep" / "nested" / "ws"
        monkeypatch.setenv("CALFCORD_WORKSPACE_DIR", str(target))
        assert not target.exists()
        workspace.get_workspace_root()
        assert target.is_dir()

    def test_expands_user_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # Simulate ~/foo by pointing $HOME at tmp_path.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("CALFCORD_WORKSPACE_DIR", "~/myws")
        root = workspace.get_workspace_root()
        assert root == (tmp_path / "myws").resolve()

    def test_default_falls_back_to_cwd_state_workspace(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("CALFCORD_WORKSPACE_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        root = workspace.get_workspace_root()
        assert root == (tmp_path / "state" / "workspace").resolve()
        assert root.is_dir()

    def test_result_is_cached(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CALFCORD_WORKSPACE_DIR", str(tmp_path / "first"))
        first = workspace.get_workspace_root()
        # Mutating the env after the first call must NOT change the cached
        # value — workspace root is treated as boot-time config.
        monkeypatch.setenv("CALFCORD_WORKSPACE_DIR", str(tmp_path / "second"))
        second = workspace.get_workspace_root()
        assert first == second

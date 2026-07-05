from __future__ import annotations

import json
from pathlib import Path

import pytest

from calfcord.bridge.settings import SettingsConfigError, load_settings, resolve_settings_path


def test_resolve_settings_path_honors_explicit_override(tmp_path: Path) -> None:
    explicit = tmp_path / "custom.json"

    path = resolve_settings_path({"CALFCORD_SETTINGS": str(explicit), "CALFCORD_HOME": str(tmp_path / "home")})

    assert path == explicit


def test_resolve_settings_path_uses_install_config_when_home_is_set(tmp_path: Path) -> None:
    home = tmp_path / "home"

    path = resolve_settings_path({"CALFCORD_HOME": str(home)})

    assert path == home / "config" / "settings.json"


def test_resolve_settings_path_falls_back_to_dev_file() -> None:
    assert resolve_settings_path({}) == Path("settings.json")


def test_missing_settings_file_defaults_sticky_replies_enabled(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "missing.json")

    assert settings.sticky_replies.enabled is True


def test_load_settings_reads_sticky_replies_flag(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"sticky_replies": {"enabled": False}}), encoding="utf-8")

    settings = load_settings(path)

    assert settings.sticky_replies.enabled is False


def test_load_settings_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(SettingsConfigError, match="not valid JSON"):
        load_settings(path)


def test_load_settings_rejects_invalid_schema(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"sticky_replies": {"enabled": "yes"}}), encoding="utf-8")

    with pytest.raises(SettingsConfigError, match="invalid settings"):
        load_settings(path)

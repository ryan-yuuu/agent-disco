"""Bridge-local settings loaded from ``settings.json``."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError

_SETTINGS_ENV_VAR = "CALFCORD_SETTINGS"
_HOME_ENV_VAR = "CALFCORD_HOME"
_CONFIG_DIRNAME = "config"
_SETTINGS_FILENAME = "settings.json"


class SettingsConfigError(Exception):
    """A problem with bridge ``settings.json``."""


class StickyRepliesSettings(BaseModel):
    """Settings for sticky reply routing."""

    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool = True


class BridgeSettings(BaseModel):
    """Bridge-local runtime settings."""

    model_config = ConfigDict(extra="forbid")

    sticky_replies: StickyRepliesSettings = StickyRepliesSettings()


def resolve_settings_path(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the bridge ``settings.json`` path for this process."""
    values = env if env is not None else __import__("os").environ
    override = values.get(_SETTINGS_ENV_VAR)
    if override:
        return Path(override)
    home = values.get(_HOME_ENV_VAR)
    if home:
        return Path(home) / _CONFIG_DIRNAME / _SETTINGS_FILENAME
    return Path(_SETTINGS_FILENAME)


def load_settings(path: Path) -> BridgeSettings:
    """Load bridge settings, defaulting missing files to the built-in defaults."""
    if not path.exists():
        return BridgeSettings()
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SettingsConfigError(f"cannot read settings {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SettingsConfigError(f"settings {path} is not valid JSON: {exc}") from exc
    try:
        return BridgeSettings.model_validate(data)
    except ValidationError as exc:
        raise SettingsConfigError(f"invalid settings in {path}: {exc}") from exc

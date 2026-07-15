"""Bridge-local settings loaded from ``settings.json``."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError

DEFAULT_HISTORY_MAX_JSON_BYTES: Final[int] = 800_000
"""Default ceiling on the serialized size of an outgoing ``message_history``.

Sized against the envelope the history rides in: aiokafka's default
``max_request_size`` is 1 MiB, and the envelope also carries the current prompt,
``deps`` and headers — so this leaves ~250 KB of headroom for everything that is
not history. See ADR 0018 for what this does and does not guarantee."""

HISTORY_MIN_JSON_BYTES: Final[int] = 10_000
"""Floor for the configurable budget — a footgun guard, not a technical limit.

A budget too small to hold one message empties every history instead of trimming
it. See ADR 0018 for why that class of misconfiguration is rejected at startup
rather than warned about once per turn."""

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


class MessageHistorySettings(BaseModel):
    """Settings for the outgoing ``message_history`` payload."""

    model_config = ConfigDict(extra="forbid")

    max_json_bytes: StrictInt = Field(
        default=DEFAULT_HISTORY_MAX_JSON_BYTES, ge=HISTORY_MIN_JSON_BYTES
    )
    """Ceiling on the serialized history; oldest turns are dropped to fit. Lower
    it when the envelope's non-history terms are unusually large, raise it when
    the broker's ``max_request_size`` has been raised to match."""


class BridgeSettings(BaseModel):
    """Bridge-local runtime settings."""

    model_config = ConfigDict(extra="forbid")

    sticky_replies: StickyRepliesSettings = StickyRepliesSettings()
    message_history: MessageHistorySettings = MessageHistorySettings()


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

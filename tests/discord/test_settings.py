"""Env-loading behaviour of :class:`DiscordSettings`.

The install seeds ``config/.env`` from ``.env.example``, which ships every
``DISCORD_*`` as an *empty* placeholder (``DISCORD_GUILD_ID=``). An empty-string
env var is NOT the same as an absent one: pydantic reads ``""`` and tries to
coerce it, so without care even the ``int | None`` optionals blow up. These
tests pin that an empty ``DISCORD_*`` behaves like "unset".
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from calfcord.discord.settings import DiscordSettings


def _require(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the two always-required fields so a test can isolate the optionals."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "123")


def test_empty_optional_discord_env_vars_fall_back_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """The seeded placeholders ``DISCORD_GUILD_ID=`` / ``DISCORD_DEFAULT_CHANNEL_ID=``
    / ``DISCORD_OWNER_USER_ID=`` are empty strings; each optional int field must
    treat that as unset and use its ``None`` default, not fail to coerce ``""``."""
    _require(monkeypatch)
    monkeypatch.setenv("DISCORD_GUILD_ID", "")
    monkeypatch.setenv("DISCORD_DEFAULT_CHANNEL_ID", "")
    monkeypatch.setenv("DISCORD_OWNER_USER_ID", "")

    settings = DiscordSettings(_env_file=None)  # type: ignore[call-arg]

    assert settings.guild_id is None
    assert settings.default_channel_id is None
    assert settings.owner_user_id is None


def test_empty_required_application_id_is_a_missing_field_not_a_parse_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty required field should read as *unset* (a clean "field required"),
    not as an int-parse failure on ``""`` — so the diagnosis points the operator
    at the missing value rather than a confusing type error."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "")

    with pytest.raises(ValidationError) as exc:
        DiscordSettings(_env_file=None)  # type: ignore[call-arg]

    report = str(exc.value)
    assert "application_id" in report
    assert "Field required" in report


def test_populated_discord_env_vars_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-empty value is still parsed normally — ignoring empties must not also
    swallow real values."""
    _require(monkeypatch)
    monkeypatch.setenv("DISCORD_GUILD_ID", "42")
    monkeypatch.setenv("DISCORD_OWNER_USER_ID", "7")

    settings = DiscordSettings(_env_file=None)  # type: ignore[call-arg]

    assert settings.application_id == 123
    assert settings.guild_id == 42
    assert settings.owner_user_id == 7

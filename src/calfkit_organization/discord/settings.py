"""Discord credentials and runtime configuration.

Loaded from environment variables (and optionally a ``.env`` file) using
pydantic-settings. All env vars are prefixed with ``DISCORD_``.
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class DiscordSettings(BaseSettings):
    """Runtime configuration for the Discord layer.

    Environment variables (all prefixed with ``DISCORD_``):

    - ``DISCORD_BOT_TOKEN``         (required)  Bot token from the Developer Portal.
    - ``DISCORD_APPLICATION_ID``    (required)  Numeric application ID.
    - ``DISCORD_GUILD_ID``          (optional)  Default guild for guild-scoped
                                                slash command sync. ``None`` means
                                                global sync (~1h propagation).
    - ``DISCORD_DEFAULT_CHANNEL_ID`` (optional) Channel used by example scripts.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="DISCORD_",
        extra="ignore",
        case_sensitive=False,
    )

    bot_token: SecretStr = Field(
        ...,
        description="Bot token from https://discord.com/developers/applications → Bot → Reset Token.",
    )
    application_id: int = Field(
        ...,
        description="Application ID from the Developer Portal → General Information.",
    )
    guild_id: int | None = Field(
        default=None,
        description="Default guild ID for guild-scoped slash commands.",
    )
    default_channel_id: int | None = Field(
        default=None,
        description="Default channel ID used by example scripts.",
    )
    owner_user_id: int | None = Field(
        default=None,
        description="Discord user ID of the human owner. The bridge normalizer sets "
        "WireAuthor.is_human_owner when message.author.id matches this value.",
    )

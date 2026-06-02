"""Domain models for messages crossing the Discord layer boundary.

These are deliberately decoupled from ``discord.py`` types so handlers
don't import the underlying library and can be tested without it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """A message received from Discord, normalized for handler consumption."""

    id: int
    channel_id: int
    guild_id: int | None
    author_id: int
    author_name: str
    content: str
    created_at: datetime
    is_from_self: bool
    is_bot: bool


@dataclass(frozen=True, slots=True)
class SentMessage:
    """Identity of a message produced by ``DiscordSender.send``."""

    id: int
    channel_id: int

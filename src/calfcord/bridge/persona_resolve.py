"""Pure persona resolution for the bridge (C8/C9/D-7).

After the migration the bridge no longer reads a registry for an agent's
``display_name``/``avatar_url``: a persona is derived entirely from the agent's
name — the webhook username *is* the name (C8), and the avatar is a
deterministic DiceBear seeded by it (C9). So :func:`persona_for` is a pure
function with no roster dependency, and it is handoff-correct because it is
keyed on the actual reply ``emitter`` (the node that really replied), not the
mention target.
"""

from __future__ import annotations

import zlib
from typing import Final

import discord

from calfcord.discord.avatar import dicebear_avatar_url
from calfcord.discord.persona import Persona


def persona_for(name: str) -> Persona:
    """The Discord persona for the agent ``name`` — its name as the webhook
    username and a deterministic DiceBear avatar seeded by that name."""
    return Persona(name=name, avatar_url=dicebear_avatar_url(name))


_ACCENTS: Final[tuple[discord.Colour, ...]] = (
    discord.Colour(0x5865F2),  # blurple
    discord.Colour(0x0AA8A0),  # teal
    discord.Colour(0x3BA55D),  # green
    discord.Colour(0xE67E22),  # amber
    discord.Colour(0x9B59B6),  # violet
    discord.Colour(0x3498DB),  # blue
    discord.Colour(0xE91E63),  # pink
    discord.Colour(0x1ABC9C),  # mint
)
"""Hand-picked accent stripes, all legible on Discord's dark AND light themes.

A curated palette rather than a free hue derived from the name: an arbitrary hue
can land muddy, or vanish against one of the two backgrounds. Collisions between
agents are possible and harmless — two agents sharing a stripe costs nothing,
whereas an illegible stripe costs the signal.

Deliberately contains **no red**: the old renderer striped every message
``0xE74C3C``, successes included, so colour carried no information at all. Here
the stripe means *who*, and a fault is carried by the seal row.
"""


def accent_for(name: str) -> discord.Colour:
    """The trace stripe for the agent ``name`` — identity, never state.

    Kept beside :func:`persona_for` because it is the same idea: a display
    attribute derived purely from the agent's name, with no roster lookup. It
    stays OFF :class:`Persona`, which ADR-0012 deliberately reduced to a minimal
    webhook identity (name + avatar); the stripe is a trace-rendering concern
    with one caller.

    ``zlib.crc32``, NOT ``hash()``: Python randomises string hashing per process
    (``PYTHONHASHSEED``), so ``hash(name) % len`` would be perfectly stable
    within one run and hand the agent a *different* colour after every bridge
    restart.
    """
    return _ACCENTS[zlib.crc32(name.encode("utf-8")) % len(_ACCENTS)]

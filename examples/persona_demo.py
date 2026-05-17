"""Send messages under two different personas to the configured default channel.

Demonstrates how ``DiscordPersonaSender`` lets one bot project multiple
identities (name + avatar) through a single per-channel webhook.

Prerequisites:
    - The bot must have **Manage Webhooks** permission in the target
      channel (re-invite via OAuth2 URL Generator if you haven't already).

Run::

    uv run python examples/persona_demo.py
"""

from __future__ import annotations

import asyncio
import logging

from calfkit_organization.discord import (
    DiscordPersonaSender,
    DiscordSettings,
    Persona,
)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    settings = DiscordSettings()  # type: ignore[call-arg]
    if settings.default_channel_id is None:
        raise SystemExit("set DISCORD_DEFAULT_CHANNEL_ID in .env")

    # DiceBear "glass" style (https://www.dicebear.com): abstract
    # frosted-gradient blobs, deterministic per seed, no auth.
    def avatar(seed: str) -> str:
        return f"https://api.dicebear.com/9.x/glass/png?seed={seed}"

    aksel = Persona(name="Aksel (Scheduler)", avatar_url=avatar("Aksel"))
    finn = Persona(name="Finn (Finance)", avatar_url=avatar("Finn"))

    async with DiscordPersonaSender(settings) as personas:
        a = await personas.send(
            aksel,
            settings.default_channel_id,
            "Booked your dentist for Thursday at 2pm.",
        )
        print(f"sent persona='{aksel.name}' message id={a.id}")

        f = await personas.send(
            finn,
            settings.default_channel_id,
            "FYI — that'll be $180 on the FSA card.",
        )
        print(f"sent persona='{finn.name}' message id={f.id}")


if __name__ == "__main__":
    asyncio.run(main())

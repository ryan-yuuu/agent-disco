"""Send a single message to the configured default channel and exit.

Verifies that the bot token, application ID, and channel ID are correct
without spinning up the gateway.

Run::

    uv run python examples/send_once.py "hello from calfkit"
"""

from __future__ import annotations

import asyncio
import logging
import sys

from calfkit_organization.discord import DiscordSender, DiscordSettings


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if len(sys.argv) < 2:
        print('usage: python examples/send_once.py "<message>"', file=sys.stderr)
        sys.exit(2)

    content = " ".join(sys.argv[1:])
    settings = DiscordSettings()  # type: ignore[call-arg]

    if settings.default_channel_id is None:
        print("error: set DISCORD_DEFAULT_CHANNEL_ID in .env", file=sys.stderr)
        sys.exit(2)

    async with DiscordSender(settings) as sender:
        sent = await sender.send(settings.default_channel_id, content)
        print(f"sent message id={sent.id} channel={sent.channel_id}")


if __name__ == "__main__":
    asyncio.run(main())

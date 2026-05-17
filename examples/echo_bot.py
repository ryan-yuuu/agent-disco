"""Echo every non-self, non-bot message back to its channel.

Long-running smoke test: starts the gateway, registers a handler,
blocks until Ctrl+C. Demonstrates that the Sender (REST) and Receiver
(gateway) compose cleanly inside one process.

Run::

    uv run python examples/echo_bot.py
"""

from __future__ import annotations

import asyncio
import logging

from calfkit_organization.discord import (
    DiscordReceiver,
    DiscordSender,
    DiscordSettings,
    IncomingMessage,
)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    settings = DiscordSettings()  # type: ignore[call-arg]
    receiver = DiscordReceiver(settings)
    sender = DiscordSender(settings)

    @receiver.on_message
    async def echo(msg: IncomingMessage) -> None:
        if msg.is_from_self or msg.is_bot:
            return
        await sender.send(msg.channel_id, f"echo: {msg.content}", reply_to_message_id=msg.id)

    async with sender:
        try:
            await receiver.start()
        finally:
            await receiver.close()


if __name__ == "__main__":
    asyncio.run(main())

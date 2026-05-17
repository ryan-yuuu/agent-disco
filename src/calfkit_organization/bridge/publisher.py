"""Publish :class:`WireMessage`s to Kafka via the calfkit SDK.

The publisher delegates envelope construction to ``Client.invoke_node`` so
that the bridge does not depend on calfkit's internal envelope shape. Each
publish schedules a background cleanup task that lets the dispatcher's
pending future auto-cancel at the 15-minute mark (matching Discord's
interaction lifetime). ``asyncio.wait_for`` cancels the future on timeout,
which fires the dispatcher's ``done_callback`` and pops the entry from
``_pending``. No memory leak across the daemon's lifetime.
"""

from __future__ import annotations

import asyncio
import logging

from calfkit.client import Client, InvocationHandle

from calfkit_organization.bridge.wire import WireMessage

logger = logging.getLogger(__name__)

_DEFAULT_CLEANUP_TIMEOUT_SECONDS: float = 900.0
"""Matches Discord's slash-interaction lifetime."""


class KafkaPublisher:
    """Publishes wire messages to channel topics through ``Client.invoke_node``."""

    def __init__(
        self,
        client: Client,
        cleanup_timeout_seconds: float = _DEFAULT_CLEANUP_TIMEOUT_SECONDS,
    ) -> None:
        self._client = client
        self._cleanup_timeout = cleanup_timeout_seconds
        self._pending_cleanups: set[asyncio.Task[None]] = set()

    async def publish(self, wire: WireMessage) -> None:
        """Publish ``wire`` to ``discord.channel.{wire.channel_id}``.

        Returns once the underlying Kafka publish call completes (the
        invocation handle's cleanup is scheduled as a background task and
        does not block the caller).
        """
        topic = f"discord.channel.{wire.channel_id}"
        handle = await self._client.invoke_node(
            user_prompt=wire.content,
            topic=topic,
            correlation_id=wire.event_id,
            deps={"discord": wire.model_dump(mode="json")},
        )
        self._schedule_cleanup(handle)
        logger.info(
            "published wire kind=%s target=%s topic=%s event_id=%s",
            wire.kind,
            wire.slash_target,
            topic,
            wire.event_id,
        )

    def _schedule_cleanup(self, handle: InvocationHandle) -> None:
        task = asyncio.create_task(self._discard_after(handle))
        self._pending_cleanups.add(task)
        task.add_done_callback(self._pending_cleanups.discard)

    async def _discard_after(self, handle: InvocationHandle) -> None:
        """Wait the cleanup timeout, then let the future cancel and clean up."""
        try:
            await handle.result(timeout=self._cleanup_timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception:
            logger.debug(
                "publisher cleanup got unexpected reply or error for correlation_id=%s",
                handle.correlation_id,
                exc_info=True,
            )

    async def close(self) -> None:
        """Cancel all pending cleanup tasks. Idempotent."""
        pending = list(self._pending_cleanups)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

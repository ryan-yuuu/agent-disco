"""Unit tests for the raw-HTTP inline-reply shim in DiscordPersonaSender.

Spins up an in-process aiohttp test server pretending to be the Discord
webhook execute endpoint, then exercises ``_send_via_raw_http`` directly
to verify request shape and response parsing.
"""

from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from calfkit_organization.discord.persona import DiscordPersonaSender, Persona


async def _make_webhook_server(captured: list[dict], status: int = 200, response_body: dict | None = None) -> TestServer:
    """Start an in-process aiohttp server that mimics Discord's webhook execute endpoint."""

    async def handler(request: web.Request) -> web.Response:
        body = await request.json()
        captured.append({"body": body, "query": dict(request.query)})
        if status >= 400:
            return web.Response(status=status, text="bad request")
        return web.json_response(response_body or {"id": "999", "channel_id": "200"})

    app = web.Application()
    app.router.add_post("/{path:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    return server


@pytest.fixture
async def captured_requests() -> list[dict]:
    return []


async def test_sends_message_reference_in_body(captured_requests: list[dict]):
    server = await _make_webhook_server(captured_requests)
    try:
        webhook = SimpleNamespace(url=str(server.make_url("/webhooks/123/token-abc")))
        persona = Persona(name="Aksel (Scheduler)")

        sent = await DiscordPersonaSender._send_via_raw_http(
            webhook=webhook,
            persona=persona,
            content="reply body",
            channel_id=200,
            reply_to_message_id=42,
            thread_id=None,
        )

        assert sent.id == 999
        assert sent.channel_id == 200
        assert len(captured_requests) == 1
        body = captured_requests[0]["body"]
        assert body["content"] == "reply body"
        assert body["username"] == "Aksel (Scheduler)"
        # IDs must be JSON strings — Discord snowflakes can exceed 2^53
        # and lose precision if serialized as numbers.
        assert body["message_reference"] == {
            "message_id": "42",
            "channel_id": "200",
            "fail_if_not_exists": False,
        }
        assert isinstance(body["message_reference"]["message_id"], str)
        assert isinstance(body["message_reference"]["channel_id"], str)
        # The wait=true query param is what makes Discord return the created message.
        assert captured_requests[0]["query"]["wait"] == "true"
    finally:
        await server.close()


async def test_includes_thread_id_query_param_when_set(captured_requests: list[dict]):
    server = await _make_webhook_server(captured_requests)
    try:
        webhook = SimpleNamespace(url=str(server.make_url("/webhooks/123/token-abc")))
        persona = Persona(name="Finn (Finance)")

        sent = await DiscordPersonaSender._send_via_raw_http(
            webhook=webhook,
            persona=persona,
            content="x",
            channel_id=200,
            reply_to_message_id=42,
            thread_id=500,
        )

        assert sent.channel_id == 500, "SentMessage.channel_id is the thread when thread_id is set"
        assert captured_requests[0]["query"]["thread_id"] == "500"
    finally:
        await server.close()


async def test_includes_avatar_url_when_set(captured_requests: list[dict]):
    server = await _make_webhook_server(captured_requests)
    try:
        webhook = SimpleNamespace(url=str(server.make_url("/webhooks/123/token-abc")))
        persona = Persona(name="Aksel", avatar_url="https://example.com/avatar.png")

        await DiscordPersonaSender._send_via_raw_http(
            webhook=webhook,
            persona=persona,
            content="x",
            channel_id=200,
            reply_to_message_id=42,
            thread_id=None,
        )

        assert captured_requests[0]["body"]["avatar_url"] == "https://example.com/avatar.png"
    finally:
        await server.close()


async def test_omits_avatar_url_when_none(captured_requests: list[dict]):
    server = await _make_webhook_server(captured_requests)
    try:
        webhook = SimpleNamespace(url=str(server.make_url("/webhooks/123/token-abc")))
        persona = Persona(name="Aksel", avatar_url=None)

        await DiscordPersonaSender._send_via_raw_http(
            webhook=webhook,
            persona=persona,
            content="x",
            channel_id=200,
            reply_to_message_id=42,
            thread_id=None,
        )

        assert "avatar_url" not in captured_requests[0]["body"]
    finally:
        await server.close()


async def test_raises_http_exception_on_error_status(captured_requests: list[dict]):
    server = await _make_webhook_server(captured_requests, status=403)
    try:
        webhook = SimpleNamespace(url=str(server.make_url("/webhooks/123/token-abc")))
        persona = Persona(name="Aksel")

        with pytest.raises(discord.HTTPException):
            await DiscordPersonaSender._send_via_raw_http(
                webhook=webhook,
                persona=persona,
                content="x",
                channel_id=200,
                reply_to_message_id=42,
                thread_id=None,
            )
    finally:
        await server.close()

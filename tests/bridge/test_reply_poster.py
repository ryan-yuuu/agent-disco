"""Unit tests for the reply poster (unified chunked delivery).

Every reply is chunk-split (1 chunk for normal-length replies) and posted
chunk-by-chunk; there is no retry-with-feedback. ``post_reply`` returns
``"posted"`` (≥1 chunk delivered), ``"empty"`` (nothing to post), or
``"lost"`` (every chunk failed) so the handler can set the sticky owner or
surface an operator notice.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import discord
import pytest
from calfkit._vendor.pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from calfcord.bridge import reply_poster as rp
from calfcord.bridge.mention_handler import MentionRequest
from calfcord.bridge.reply_poster import ReplyPoster
from calfcord.bridge.wire import WireAuthor, WireMessage
from calfcord.discord.chunking import chunk_split
from calfcord.discord.messages import SentMessage
from calfcord.discord.persona import Persona


# --- helpers ---------------------------------------------------------------
class _Resp:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "err"


def _http(status: int, text: str = "rejected") -> discord.HTTPException:
    return discord.HTTPException(_Resp(status), text)


def _wire(*, source_channel_id: int | None = None, channel_id: int = 6789) -> WireMessage:
    return WireMessage(
        event_id="c1",
        kind="message",
        slash_target=None,
        message_id=12345,
        channel_id=channel_id,
        source_channel_id=source_channel_id,
        guild_id=4242,
        content="hello?",
        author=WireAuthor(discord_user_id=111, display_name="alice", is_bot=False, is_webhook=False),
        created_at=datetime.now(UTC),
    )


def _req(wire: WireMessage | None = None) -> MentionRequest:
    wire = wire if wire is not None else _wire()
    return MentionRequest(
        content="hello?",
        mention_ids=("scribe",),
        author_label="alice",
        message_id=12345,
        source_channel_id=wire.source_channel_id or wire.channel_id,
        channel_id=wire.channel_id,
        wire=wire,
        reply_target=_FakeReplyTarget(),
    )


def _result(output: str, *, message_history: list[Any] | None = None, emitter: str = "scribe") -> Any:
    return SimpleNamespace(
        output=output, message_history=message_history or [], emitter_node_id=emitter, correlation_id="c1"
    )


def _tool_history() -> list[Any]:
    """One turn's real cumulative ``message_history``, as calfkit builds it.

    The shape is load-bearing and was previously modelled wrong here (the
    committed prompt was omitted, which hid the duplication bug this fixture now
    pins). ``Client.start`` unconditionally stages the user prompt
    (``client/caller.py``) and the agent commits it BEFORE the model loop
    (``nodes/agent.py``), so it lands at exactly ``initial_len`` — i.e. the
    turn's own prompt is the first thing after the channel-history prefix, not
    the agent's first tool call.
    """
    return [
        # channel-history prefix (initial_len=1)
        ModelRequest(parts=[UserPromptPart(content="prefix", name="ryan")]),
        # the turn's OWN prompt, committed by calfkit at index initial_len
        ModelRequest(parts=[UserPromptPart(content="do a search", name="ryan")]),
        ModelResponse(parts=[ToolCallPart(tool_name="search", args={"q": "x"}, tool_call_id="t1")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="search", content="res", tool_call_id="t1")]),
        ModelResponse(parts=[TextPart(content="final")]),  # trailing final answer (dropped)
    ]


class TestTurnDelta:
    """The slice persisted for next-turn tool-call replay."""

    def test_excludes_the_turns_own_prompt(self) -> None:
        """The delta is the turn's STEPS. calfkit commits the staged prompt at
        ``initial_len``, so a naive ``[initial_len:-1]`` captures it — and replay
        then re-injects a prompt the channel history already supplies, so the
        model sees it twice and the envelope carries it twice.
        """
        delta = rp._turn_delta(_result("final", message_history=_tool_history()), 1)

        assert not any(
            isinstance(p, UserPromptPart) for m in delta for p in m.parts
        ), "delta must not carry the turn's own user prompt"
        assert [type(m).__name__ for m in delta] == ["ModelResponse", "ModelRequest"]
        assert isinstance(delta[0].parts[0], ToolCallPart)
        assert isinstance(delta[1].parts[0], ToolReturnPart)

    def test_pure_text_turn_has_an_empty_delta(self) -> None:
        """A turn with no tool calls has no steps to replay. The prompt alone is
        not a step — with it included the delta was length-1, which the docstring
        already (correctly) claimed should be empty."""
        history = [
            ModelRequest(parts=[UserPromptPart(content="prefix", name="ryan")]),
            ModelRequest(parts=[UserPromptPart(content="hi", name="ryan")]),
            ModelResponse(parts=[TextPart(content="hello")]),
        ]

        assert rp._turn_delta(_result("hello", message_history=history), 1) == []


class _FakePersonas:
    def __init__(self) -> None:
        self.sends: list[dict[str, Any]] = []
        self._next_id = 7000
        self.errors: list[Exception | None] = []  # scripted per-send; None = success

    async def send(
        self,
        persona: Persona,
        channel_id: int,
        content: str,
        *,
        reply_to: Any = None,
        thread_id: int | None = None,
    ) -> SentMessage:
        if self.errors:
            err = self.errors.pop(0)
            if err is not None:
                raise err
        self._next_id += 1
        self.sends.append(
            {
                "persona": persona,
                "channel_id": channel_id,
                "content": content,
                "reply_to": reply_to,
                "thread_id": thread_id,
            }
        )
        return SentMessage(id=self._next_id, channel_id=thread_id or channel_id)


class _FakeStore:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.rows: list[Any] = []

    async def write_turn(self, row: Any) -> None:
        self.rows.append(row)


class _FakeReplyTarget:
    def __init__(self) -> None:
        self.replies: list[str] = []
        self.fail = False

    async def reply(self, text: str) -> None:
        if self.fail:
            raise _http(403)
        self.replies.append(text)


def _poster(
    personas: _FakePersonas | None = None, store: _FakeStore | None = None
) -> tuple[ReplyPoster, _FakePersonas, _FakeStore]:
    p = personas or _FakePersonas()
    s = store or _FakeStore()
    return ReplyPoster(p, s), p, s  # type: ignore[arg-type]


# --- single-chunk replies ----------------------------------------------------
class TestPostReplySingleChunk:
    async def test_happy_path_posts_and_returns_posted(self) -> None:
        poster, personas, store = _poster()
        out = await poster.post_reply(
            _req(), Persona(name="scribe"), _result("done"), initial_len=0, correlation_id="c1"
        )
        assert out == "posted"
        assert len(personas.sends) == 1
        assert personas.sends[0]["persona"].name == "scribe"
        assert personas.sends[0]["content"] == "done"
        assert personas.sends[0]["reply_to"] is not None  # anchored to the trigger
        assert store.rows == []  # pure-text turn: no transcript row

    async def test_empty_output_returns_empty_without_sending(self) -> None:
        poster, personas, _ = _poster()
        out = await poster.post_reply(
            _req(), Persona(name="scribe"), _result("   "), initial_len=0, correlation_id="c1"
        )
        assert out == "empty"
        assert personas.sends == []

    async def test_tool_turn_writes_transcript(self) -> None:
        poster, _, store = _poster()
        out = await poster.post_reply(
            _req(),
            Persona(name="scribe"),
            _result("final", message_history=_tool_history()),
            initial_len=1,
            correlation_id="c1",
        )
        assert out == "posted"
        # A turn that used tools persists its transcript (for tool-call replay).
        assert len(store.rows) == 1
        assert store.rows[0].agent_id == "scribe" and store.rows[0].correlation_id == "c1"

    async def test_pure_text_turn_no_row(self) -> None:
        poster, personas, store = _poster()
        # message_history with no tool slice → empty delta
        hist = [ModelRequest(parts=[TextPart(content="p")]), ModelResponse(parts=[TextPart(content="final")])]
        await poster.post_reply(
            _req(), Persona(name="scribe"), _result("final", message_history=hist), initial_len=1, correlation_id="c1"
        )
        assert len(personas.sends) == 1  # the reply still posts
        assert store.rows == []  # no tools → no transcript row

    async def test_disabled_store_suppresses_write(self) -> None:
        poster, personas, store = _poster(store=_FakeStore(enabled=False))
        await poster.post_reply(
            _req(),
            Persona(name="scribe"),
            _result("final", message_history=_tool_history()),
            initial_len=1,
            correlation_id="c1",
        )
        assert len(personas.sends) == 1  # the reply still posts
        assert store.rows == []  # disabled store: no transcript row

    async def test_render_fault_degrades_to_no_transcript_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A step-render bug must not block delivery: the reply still posts, only
        # the transcript row (tool-call replay for this turn) is skipped.
        def _boom(delta: Any) -> list[str]:
            raise RuntimeError("render boom")

        monkeypatch.setattr(rp, "_render_tree_blocks", _boom)
        poster, personas, store = _poster()
        out = await poster.post_reply(
            _req(),
            Persona(name="scribe"),
            _result("final", message_history=_tool_history()),
            initial_len=1,
            correlation_id="c1",
        )
        assert out == "posted"
        assert len(personas.sends) == 1
        assert store.rows == []

    async def test_transcript_store_failure_does_not_fail_the_post(self) -> None:
        # The reply is already on Discord when the row write fails; the loss is
        # replay data for this turn, never the delivery result.
        class _BoomStore(_FakeStore):
            async def write_turn(self, row: Any) -> None:
                raise RuntimeError("store down")

        poster, personas, _ = _poster(store=_BoomStore())
        out = await poster.post_reply(
            _req(),
            Persona(name="scribe"),
            _result("final", message_history=_tool_history()),
            initial_len=1,
            correlation_id="c1",
        )
        assert out == "posted"
        assert len(personas.sends) == 1

    async def test_thread_routing(self) -> None:
        poster, personas, _ = _poster()
        await poster.post_reply(
            _req(_wire(source_channel_id=99999)),
            Persona(name="scribe"),
            _result("done"),
            initial_len=0,
            correlation_id="c1",
        )
        assert personas.sends[0]["thread_id"] == 99999


# --- multi-chunk replies -----------------------------------------------------
class TestPostReplyMultiChunk:
    async def test_long_reply_splits_first_chunk_anchored_and_writes_row(self) -> None:
        poster, personas, store = _poster()
        big = "x" * 4500
        out = await poster.post_reply(
            _req(),
            Persona(name="scribe"),
            _result(big, message_history=_tool_history()),
            initial_len=1,
            correlation_id="c1",
        )
        assert out == "posted"
        assert len(personas.sends) >= 3  # >2 chunks
        assert personas.sends[0]["reply_to"] is not None  # anchor on first chunk only
        assert all(s["reply_to"] is None for s in personas.sends[1:])
        assert len(store.rows) == 1  # the turn used tools → one transcript row

    async def test_chunks_post_in_original_order(self) -> None:
        poster, personas, _ = _poster()
        big = ("paragraph one " * 100 + "\n\n") * 3  # forces boundary-aware splits
        out = await poster.post_reply(
            _req(), Persona(name="scribe"), _result(big), initial_len=0, correlation_id="c1"
        )
        assert out == "posted"
        assert [s["content"] for s in personas.sends] == chunk_split(big.strip())

    async def test_partial_failure_still_posts_remaining_and_returns_posted(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        personas = _FakePersonas()
        personas.errors = [None, _http(400, "bad chunk"), None]  # middle chunk fails
        poster, _, _ = _poster(personas)
        with caplog.at_level("WARNING"):
            out = await poster.post_reply(
                _req(), Persona(name="scribe"), _result("x" * 4500), initial_len=0, correlation_id="c1"
            )
        assert out == "posted"
        assert len(personas.sends) == 2  # chunks 1 and 3 delivered despite the gap
        failed = [r for r in caplog.records if "chunk" in r.message and "failed" in r.message]
        assert len(failed) == 1

    async def test_first_chunk_failure_skips_transcript_row(self) -> None:
        personas = _FakePersonas()
        personas.errors = [_http(400), None, None]  # anchor chunk fails
        poster, _, store = _poster(personas)
        out = await poster.post_reply(
            _req(),
            Persona(name="scribe"),
            _result("x" * 4500, message_history=_tool_history()),
            initial_len=1,
            correlation_id="c1",
        )
        assert out == "posted"  # later chunks still delivered
        assert store.rows == []  # row is keyed to the anchor chunk's message id

    async def test_all_chunks_fail_returns_lost(self) -> None:
        personas = _FakePersonas()
        personas.errors = [_http(403)] * 10
        poster, _, store = _poster(personas)
        # must not raise even though every chunk fails, and must signal total loss
        # so the handler surfaces an operator notice.
        out = await poster.post_reply(
            _req(), Persona(name="scribe"), _result("x" * 4500), initial_len=0, correlation_id="c1"
        )
        assert out == "lost"
        assert store.rows == []


# --- failure logging and transport smoothing ----------------------------------
class TestFailureHandling:
    async def test_forbidden_chunk_logs_error(self, caplog: pytest.LogCaptureFixture) -> None:
        personas = _FakePersonas()
        personas.errors = [discord.Forbidden(_Resp(403), "no perms")]
        poster, _, _ = _poster(personas)
        with caplog.at_level("WARNING"):
            out = await poster.post_reply(
                _req(), Persona(name="scribe"), _result("x"), initial_len=0, correlation_id="c1"
            )
        assert out == "lost"
        # 403 (missing Manage Webhooks) is an operator-actionable misconfiguration → ERROR.
        failed = [r for r in caplog.records if "chunk" in r.message and "failed" in r.message]
        assert len(failed) == 1 and failed[0].levelname == "ERROR"

    async def test_rate_limited_chunk_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        personas = _FakePersonas()
        personas.errors = [discord.RateLimited(1.0)]
        poster, _, _ = _poster(personas)
        with caplog.at_level("WARNING"):
            out = await poster.post_reply(
                _req(), Persona(name="scribe"), _result("x"), initial_len=0, correlation_id="c1"
            )
        assert out == "lost"
        # Rate-limit is transient noise → WARNING, not ERROR.
        failed = [r for r in caplog.records if "chunk" in r.message and "failed" in r.message]
        assert len(failed) == 1 and failed[0].levelname == "WARNING"

    async def test_total_loss_logs_summary(self, caplog: pytest.LogCaptureFixture) -> None:
        personas = _FakePersonas()
        personas.errors = [_http(403)] * 10
        poster, _, _ = _poster(personas)
        with caplog.at_level("ERROR"):
            await poster.post_reply(
                _req(), Persona(name="scribe"), _result("x" * 4500), initial_len=0, correlation_id="c1"
            )
        summaries = [r for r in caplog.records if "fully lost" in r.message]
        assert len(summaries) == 1 and summaries[0].levelname == "ERROR"

    async def test_non_discord_sender_error_returns_lost(self) -> None:
        personas = _FakePersonas()
        personas.errors = [TypeError("not a text channel")]
        poster, _, _ = _poster(personas)
        out = await poster.post_reply(_req(), Persona(name="scribe"), _result("x"), initial_len=0, correlation_id="c1")
        assert out == "lost"

    async def test_5xx_once_then_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rp, "_SERVER_ERROR_RETRY_DELAY_SECONDS", 0)
        personas = _FakePersonas()
        personas.errors = [discord.DiscordServerError(_Resp(503), "down"), None]  # fail then succeed
        poster, _, _ = _poster(personas)
        out = await poster.post_reply(_req(), Persona(name="scribe"), _result("x"), initial_len=0, correlation_id="c1")
        assert out == "posted"
        assert len(personas.sends) == 1  # the successful re-send

    async def test_5xx_smoothing_applies_per_chunk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rp, "_SERVER_ERROR_RETRY_DELAY_SECONDS", 0)
        personas = _FakePersonas()
        # Chunk 1: 503 then success; chunk 2: 503 then success; chunk 3: clean.
        personas.errors = [
            discord.DiscordServerError(_Resp(503), "down"),
            None,
            discord.DiscordServerError(_Resp(503), "down"),
            None,
            None,
        ]
        poster, _, _ = _poster(personas)
        out = await poster.post_reply(
            _req(), Persona(name="scribe"), _result("x" * 4500), initial_len=0, correlation_id="c1"
        )
        assert out == "posted"
        assert len(personas.sends) == 3  # every chunk delivered despite the blips

    async def test_persistent_5xx_returns_lost(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rp, "_SERVER_ERROR_RETRY_DELAY_SECONDS", 0)
        personas = _FakePersonas()
        personas.errors = [
            discord.DiscordServerError(_Resp(503), "down"),
            discord.DiscordServerError(_Resp(503), "down"),
        ]
        poster, _, _ = _poster(personas)
        out = await poster.post_reply(_req(), Persona(name="scribe"), _result("x"), initial_len=0, correlation_id="c1")
        assert out == "lost"


class TestPostNotice:
    async def test_notice_replies_to_trigger(self) -> None:
        poster, _, _ = _poster()
        req = _req()
        await poster.post_notice(req, "no agent online")
        assert req.reply_target.replies == ["no agent online"]

    async def test_notice_swallows_failure(self) -> None:
        poster, _, _ = _poster()
        req = _req()
        req.reply_target.fail = True
        await poster.post_notice(req, "boom")  # must not raise
        assert req.reply_target.replies == []

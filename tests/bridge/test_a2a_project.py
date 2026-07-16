"""Unit tests for the A2A projector (spec §6.2 / D-1/D-2).

Drives :class:`A2AProjector` through a recording fake persona sender and a fake
channel resolver — no Discord. Asserts thread anchoring per ``correlation_id``,
persona attribution (caller for requests, peer for replies, a system persona for
rejects/handoffs/faults), and best-effort error swallowing.
"""

from __future__ import annotations

import logging
from typing import Any

import discord
import pytest

from calfcord.bridge.a2a_dispatch import A2ACall, A2AFailed, A2AReject, A2AReply, A2ARequest
from calfcord.bridge.a2a_project import _EMPTY_PLACEHOLDER, _SYSTEM_PERSONA, A2AProjector
from calfcord.discord.messages import SentMessage
from calfcord.discord.persona import Persona


class _FakePersonas:
    def __init__(self) -> None:
        self.sends: list[dict[str, Any]] = []
        self._next_id = 1000
        self.fail_on_send = False

    async def send(
        self,
        persona: Persona,
        channel_id: int,
        content: str,
        *,
        thread_id: int | None = None,
    ) -> SentMessage:
        if self.fail_on_send:
            raise discord.HTTPException(response=_FakeResponse(), message="boom")
        self._next_id += 1
        self.sends.append({"persona": persona, "channel_id": channel_id, "content": content, "thread_id": thread_id})
        return SentMessage(id=self._next_id, channel_id=thread_id or channel_id)


class _FakeResolver:
    guild_id = 42

    def __init__(self, *, channel_id: int = 500) -> None:
        self._channel_id = channel_id
        self.resolve_calls = 0
        self.created: list[dict[str, Any]] = []
        self._next_thread = 9000

    async def resolve_unified_channel(self) -> int:
        self.resolve_calls += 1
        return self._channel_id

    async def create_anchored_thread(self, channel_id: int, anchor_message_id: int, *, name: str) -> int:
        self._next_thread += 1
        self.created.append({"channel_id": channel_id, "anchor": anchor_message_id, "name": name})
        return self._next_thread


class _FakeResponse:
    status = 500
    reason = "err"


def _make() -> tuple[A2AProjector, _FakePersonas, _FakeResolver]:
    personas = _FakePersonas()
    resolver = _FakeResolver()
    return A2AProjector(resolver, personas), personas, resolver  # type: ignore[arg-type]


class TestRequest:
    async def test_first_request_anchors_thread_under_caller_persona(self) -> None:
        proj, personas, resolver = _make()
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t1", caller="scribe", peer="conan", message="summarize")
        )
        # one anchor send under the caller persona, into the unified channel (no thread yet)
        assert len(personas.sends) == 1
        send = personas.sends[0]
        assert send["persona"].name == "scribe"
        assert send["channel_id"] == 500 and send["thread_id"] is None
        assert send["content"] == "summarize"
        # a thread was anchored on the sent message, named caller→peer
        assert len(resolver.created) == 1
        assert resolver.created[0]["name"] == "scribe→conan: summarize"
        assert proj._threads["c1"] == 9001

    async def test_second_request_same_correlation_reuses_thread(self) -> None:
        proj, personas, resolver = _make()
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t1", caller="scribe", peer="conan", message="one")
        )
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t2", caller="scribe", peer="dot", message="two")
        )
        # only one thread ever created; the second request posts INTO it
        assert len(resolver.created) == 1
        assert personas.sends[1]["thread_id"] == 9001
        assert personas.sends[1]["content"] == "two"

    async def test_empty_message_uses_placeholder(self) -> None:
        proj, personas, _ = _make()
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t1", caller="scribe", peer="conan", message="")
        )
        assert personas.sends[0]["content"] == _EMPTY_PLACEHOLDER


class TestReply:
    async def test_reply_posts_under_peer_persona_in_thread(self) -> None:
        proj, personas, resolver = _make()
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t1", caller="scribe", peer="conan", message="q")
        )
        await proj.project(
            A2AReply(correlation_id="c1", tool_call_id="t1", caller="scribe", peer="conan", text="the answer")
        )
        # no second thread; reply posted into the existing thread under the PEER persona
        assert len(resolver.created) == 1
        reply_send = personas.sends[1]
        assert reply_send["persona"].name == "conan"
        assert reply_send["thread_id"] == 9001
        assert reply_send["content"] == "the answer"


class TestReject:
    async def test_reject_renders_system_note_not_peer_post(self) -> None:
        proj, personas, _ = _make()
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t1", caller="scribe", peer="ghost", message="q")
        )
        await proj.project(
            A2AReject(correlation_id="c1", tool_call_id="t1", caller="scribe", peer="ghost", text="agent offline")
        )
        note = personas.sends[1]
        assert note["persona"].name == _SYSTEM_PERSONA.name  # NOT "ghost"
        assert note["thread_id"] == 9001
        assert "ghost" in note["content"] and "agent offline" in note["content"]


class TestFailed:
    async def test_failed_renders_system_note_distinct_from_reject(self) -> None:
        proj, personas, _ = _make()
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t1", caller="scribe", peer="conan", message="q")
        )
        await proj.project(
            A2AFailed(correlation_id="c1", tool_call_id="t1", caller="scribe", peer="conan", text="boom")
        )
        note = personas.sends[1]
        assert note["persona"].name == _SYSTEM_PERSONA.name  # NOT "conan"
        assert note["thread_id"] == 9001
        content = note["content"]
        assert "conan" in content and "boom" in content and "failed" in content
        assert "rejected" not in content  # distinct from the A2AReject note


class TestFault:
    async def test_project_fault_notes_dangling_consult(self) -> None:
        proj, personas, _ = _make()
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t1", caller="scribe", peer="conan", message="q")
        )
        await proj.project_fault(
            A2ACall(tool_call_id="t1", correlation_id="c1", caller="scribe", peer="conan", message="q")
        )
        note = personas.sends[1]
        assert note["persona"].name == _SYSTEM_PERSONA.name
        assert "conan" in note["content"] and note["thread_id"] == 9001


class TestChunking:
    async def test_oversized_content_splits_anchor_then_thread(self) -> None:
        proj, personas, _ = _make()
        big = "x" * 4500  # > 2 chunks at CHUNK_SAFE_SIZE=1990
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t1", caller="scribe", peer="conan", message=big)
        )
        # first chunk is the anchor (no thread), remaining chunks go INTO the thread
        assert personas.sends[0]["thread_id"] is None
        assert all(s["thread_id"] == 9001 for s in personas.sends[1:])
        assert len(personas.sends) >= 3
        assert "".join(s["content"] for s in personas.sends) == big


class TestProjectReturnsAReceipt:
    """``project`` returns the jump link the bridge puts in the human's thread, so a
    consult is reachable from the conversation that caused it. It is a RECEIPT for
    the render just performed — never a lookup of turn state, which can outlive the
    render that created it."""

    def _req(self, corr: str = "c1", *, tcid: str = "t1", peer: str = "conan", msg: str = "q") -> A2ARequest:
        return A2ARequest(correlation_id=corr, tool_call_id=tcid, caller="scribe", peer=peer, message=msg)

    async def test_url_points_at_the_anchored_thread(self) -> None:
        proj, _personas, resolver = _make()
        url = await proj.project(self._req())
        assert url == f"https://discord.com/channels/42/{resolver._next_thread}"

    async def test_no_url_when_the_projection_failed(self) -> None:
        # Best-effort: the Discord failure is swallowed so it can't fault the human
        # turn. Nothing was written, so there is nothing to link to — the caller
        # renders the audit gap instead of a link to nowhere.
        proj, personas, _resolver = _make()
        personas.fail_on_send = True
        assert await proj.project(self._req()) is None

    async def test_a_failed_second_consult_does_not_inherit_the_first_s_link(self) -> None:
        # THE reason this is a receipt and not a lookup. A turn shares ONE
        # correlation_id, so after consult #1 anchors a thread, a lookup would hand
        # consult #2 that same thread even though #2 never reached Discord — a
        # confident link to an exchange that isn't there.
        proj, personas, _resolver = _make()
        assert await proj.project(self._req(tcid="t1", peer="conan", msg="q1")) is not None
        personas.fail_on_send = True
        assert await proj.project(self._req(tcid="t2", peer="dot", msg="q2")) is None


class TestFailureIsLoudButNotSpammy:
    """A swallowed audit gap must still be findable.

    The cause is almost always systemic (a missing permission), so it fails
    identically on EVERY consult: a bare WARN-per-projection buries the signal under
    duplicate tracebacks and names no fix, leaving the log readable only by hand. So
    the first failure is an ERROR naming the remedy, and repeats drop to DEBUG.
    """

    def _req(self, corr: str = "c1") -> A2ARequest:
        return A2ARequest(correlation_id=corr, tool_call_id="t1", caller="scribe", peer="conan", message="q")

    async def test_first_failure_logs_one_error_naming_the_remedy(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        proj, personas, _resolver = _make()
        personas.fail_on_send = True
        with caplog.at_level(logging.ERROR, logger="calfcord.bridge.a2a_project"):
            await proj.project(self._req())
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "Manage Channels" in errors[0].getMessage()

    async def test_repeat_failures_do_not_re_log_at_error(self, caplog: pytest.LogCaptureFixture) -> None:
        proj, personas, _resolver = _make()
        personas.fail_on_send = True
        with caplog.at_level(logging.ERROR, logger="calfcord.bridge.a2a_project"):
            for i in range(5):
                await proj.project(self._req(f"c{i}"))
        assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1

    async def test_a_successful_fault_note_also_re_arms(self, caplog: pytest.LogCaptureFixture) -> None:
        # Both render entry points must arm AND re-arm the latch. If project_fault
        # only ever armed it, a fault note landing after an outage would leave the
        # latch stuck and demote the next genuine outage to DEBUG — losing exactly
        # the loud line the latch exists to protect.
        proj, personas, _resolver = _make()
        personas.fail_on_send = True
        await proj.project(self._req("c1"))  # arms the latch
        personas.fail_on_send = False
        await proj.project_fault(
            A2ACall(tool_call_id="t1", correlation_id="c2", caller="scribe", peer="conan", message="q")
        )
        personas.fail_on_send = True
        with caplog.at_level(logging.ERROR, logger="calfcord.bridge.a2a_project"):
            await proj.project(self._req("c3"))
        assert [r for r in caplog.records if r.levelno == logging.ERROR]

    async def test_recovery_re_arms_the_error(self, caplog: pytest.LogCaptureFixture) -> None:
        # A channel fixed and then re-broken must announce itself again — the latch
        # suppresses noise from ONE ongoing outage, not every future one.
        proj, personas, _resolver = _make()
        personas.fail_on_send = True
        await proj.project(self._req("c1"))
        personas.fail_on_send = False
        await proj.project(self._req("c2"))  # recovered
        personas.fail_on_send = True
        with caplog.at_level(logging.ERROR, logger="calfcord.bridge.a2a_project"):
            await proj.project(self._req("c3"))
        assert [r for r in caplog.records if r.levelno == logging.ERROR]


class TestBestEffort:
    async def test_discord_failure_is_swallowed(self) -> None:
        proj, personas, _ = _make()
        personas.fail_on_send = True
        # must NOT raise — a failed render is an accepted audit gap
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t1", caller="scribe", peer="conan", message="q")
        )
        await proj.project_fault(
            A2ACall(tool_call_id="t1", correlation_id="c1", caller="scribe", peer="conan", message="q")
        )
        assert personas.sends == []


@pytest.mark.parametrize(
    ("content", "expected_tail"),
    [("hello\nworld", "hello world"), ("", "<empty>"), ("a" * 80, "a" * 40)],
)
def test_thread_name_shaping(content: str, expected_tail: str) -> None:
    from calfcord.bridge.a2a_project import _build_thread_name

    name = _build_thread_name("alice", "bob", content)
    assert name.startswith("alice→bob: ")
    assert name.endswith(expected_tail)
    assert len(name) <= 100

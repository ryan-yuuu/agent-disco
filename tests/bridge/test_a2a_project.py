"""Unit tests for the A2A projector (spec §6.2 / D-1/D-2).

Drives :class:`A2AProjector` through a recording fake persona sender and a fake
channel resolver — no Discord. Asserts thread anchoring per ``correlation_id``,
persona attribution (caller for requests, peer for replies, a system persona for
rejects/handoffs/faults), and best-effort error swallowing.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import discord
import pytest

from calfcord.bridge.a2a_dispatch import A2ACall, A2AFailed, A2AReject, A2AReply, A2ARequest
from calfcord.bridge.a2a_project import A2AProjector
from calfcord.bridge.step_events import StepEvent
from calfcord.bridge.trace import StepTraceRenderer
from calfcord.discord.messages import SentMessage
from calfcord.discord.persona import Persona


class _FakePersonas:
    def __init__(self) -> None:
        self.sends: list[dict[str, Any]] = []
        # Components-V2 traffic (the step renderer's aggregate), kept apart from
        # the plain projection posts so tests can assert each surface on its own.
        self.component_sends: list[dict[str, Any]] = []
        self.component_edits: list[dict[str, Any]] = []
        self.card_sends: list[dict[str, Any]] = []
        self.card_edits: list[dict[str, Any]] = []
        self._next_id = 1000
        self.fail_on_send = False
        self.fail_on_component_send = False
        self.fail_on_component_edit = False

    async def send(
        self,
        persona: Persona,
        channel_id: int,
        content: str,
        *,
        thread_id: int | None = None,
        suppress_mentions: bool = False,
    ) -> SentMessage:
        if self.fail_on_send:
            raise discord.HTTPException(response=_FakeResponse(), message="boom")
        self._next_id += 1
        self.sends.append(
            {
                "persona": persona,
                "channel_id": channel_id,
                "content": content,
                "thread_id": thread_id,
                "suppress_mentions": suppress_mentions,
            }
        )
        return SentMessage(id=self._next_id, channel_id=thread_id or channel_id)

    async def send_components(
        self,
        *,
        persona: Persona,
        channel_id: int,
        view: discord.ui.LayoutView,
        thread_id: int | None = None,
    ) -> SentMessage:
        if self.fail_on_component_send:
            raise discord.HTTPException(response=_FakeResponse(), message="boom")
        self._next_id += 1
        record = {"persona": persona, "channel_id": channel_id, "thread_id": thread_id, "body": _view_body(view)}
        (self.card_sends if record["body"].startswith("### ") else self.component_sends).append(record)
        return SentMessage(id=self._next_id, channel_id=thread_id or channel_id)

    async def edit_components(
        self,
        *,
        channel_id: int,
        message_id: int,
        view: discord.ui.LayoutView,
        thread_id: int | None = None,
    ) -> None:
        if self.fail_on_component_edit:
            raise discord.HTTPException(response=_FakeResponse(), message="boom")
        record = {"channel_id": channel_id, "message_id": message_id, "thread_id": thread_id, "body": _view_body(view)}
        (self.card_edits if record["body"].startswith("### ") else self.component_edits).append(record)


def _view_body(view: discord.ui.LayoutView) -> str:
    """All text displayed by a Components-V2 view, in visual order."""
    bodies = [item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)]
    assert bodies, "expected at least one TextDisplay"
    return "\n".join(bodies)


class _FakeResolver:
    guild_id = 42

    def __init__(self, *, channel_id: int = 500) -> None:
        self._channel_id = channel_id
        self.resolve_calls = 0
        self.created: list[dict[str, Any]] = []
        self._next_thread = 9000
        self.fail_on_create = False

    async def resolve_unified_channel(self) -> int:
        self.resolve_calls += 1
        return self._channel_id

    async def create_anchored_thread(self, channel_id: int, anchor_message_id: int, *, name: str) -> int:
        if self.fail_on_create:
            raise discord.HTTPException(response=_FakeResponse(), message="boom")
        self._next_thread += 1
        self.created.append({"channel_id": channel_id, "anchor": anchor_message_id, "name": name})
        return self._next_thread


class _FakeResponse:
    status = 500
    reason = "err"


def _make(*, interval: float = 0.0) -> tuple[A2AProjector, _FakePersonas, _FakeResolver]:
    personas = _FakePersonas()
    resolver = _FakeResolver()
    # A REAL step renderer (interval=0 → flushes as fast as the loop turns), so
    # these tests exercise the actual aggregation rather than a stand-in.
    steps = StepTraceRenderer(personas, min_edit_interval=interval)  # type: ignore[arg-type]
    return A2AProjector(resolver, personas, steps), personas, resolver  # type: ignore[arg-type]


async def _begin(proj: A2AProjector, *, subject: str = "Plan the Reddit launch") -> None:
    await proj.begin_turn(correlation_id="c1", root_agent="marketing", subject=subject)


class TestTurnScopedConsultCards:
    """The audit thread is one turn-level trace with one editable card per call."""

    async def test_first_request_anchors_a_components_card_with_a_turn_level_title(self) -> None:
        proj, personas, resolver = _make()
        await _begin(proj, subject="!marketing   Plan\n the Reddit launch")

        await proj.project(
            A2ARequest(
                correlation_id="c1",
                tool_call_id="t1",
                caller="marketing",
                peer="grok",
                message="Pressure-test the launch copy",
            )
        )

        assert personas.sends == []
        assert len(personas.card_sends) == 1
        card = personas.card_sends[0]
        assert card["persona"].name == "marketing"
        assert card["thread_id"] is None
        assert "↗ marketing → grok" in card["body"]
        assert "Consulting" in card["body"]
        assert "Pressure-test the launch copy" in card["body"]
        assert resolver.created == [
            {"channel_id": 500, "anchor": 1001, "name": "marketing · Plan the Reddit launch"}
        ]

    async def test_parallel_results_edit_only_their_own_cards_and_post_routed_responses(self) -> None:
        proj, personas, resolver = _make()
        await _begin(proj)
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t1", caller="marketing", peer="grok", message="copy")
        )
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t2", caller="marketing", peer="sol", message="facts")
        )

        await proj.project(
            A2AReply(correlation_id="c1", tool_call_id="t2", caller="marketing", peer="sol", text="fact check")
        )
        await proj.project(
            A2AReply(correlation_id="c1", tool_call_id="t1", caller="marketing", peer="grok", text="new hook")
        )

        assert len(resolver.created) == 1
        assert [send["thread_id"] for send in personas.card_sends] == [None, 9001]
        assert [edit["message_id"] for edit in personas.card_edits] == [1002, 1001]
        assert all("Consulted" in edit["body"] for edit in personas.card_edits)
        assert [(send["persona"].name, send["content"]) for send in personas.sends] == [
            ("sol", "-# **↩ sol → marketing · response**\n\nfact check"),
            ("grok", "-# **↩ grok → marketing · response**\n\nnew hook"),
        ]
        assert all("✓ marketing →" not in send["content"] for send in personas.sends)
        assert all(send["suppress_mentions"] is True for send in personas.sends)

    async def test_rejection_and_dangling_fault_resolve_the_original_card_without_extra_messages(self) -> None:
        proj, personas, _ = _make()
        await _begin(proj)
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t1", caller="marketing", peer="grok", message="copy")
        )
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t2", caller="marketing", peer="sol", message="facts")
        )

        await proj.project(
            A2AReject(correlation_id="c1", tool_call_id="t1", caller="marketing", peer="grok", text="cycle")
        )
        await proj.project_fault(
            A2ACall(tool_call_id="t2", correlation_id="c1", caller="marketing", peer="sol", message="facts")
        )

        assert personas.sends == []
        assert [edit["message_id"] for edit in personas.card_edits] == [1001, 1002]
        assert "Not dispatched" in personas.card_edits[0]["body"]
        assert "cycle" in personas.card_edits[0]["body"]
        assert "No response" in personas.card_edits[1]["body"]

    async def test_thread_create_failure_keeps_the_card_resolvable_without_a_false_link(self) -> None:
        proj, personas, resolver = _make()
        resolver.fail_on_create = True
        assert await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t1", caller="marketing", peer="grok", message="copy")
        ) is None
        await proj.project_fault(
            A2ACall(tool_call_id="t1", correlation_id="c1", caller="marketing", peer="grok", message="copy")
        )
        assert "No response" in personas.card_edits[-1]["body"]
        assert personas.card_edits[-1]["thread_id"] is None

    async def test_thread_create_failure_still_posts_a_successful_reply_beside_the_parent_card(self) -> None:
        proj, personas, resolver = _make()
        resolver.fail_on_create = True
        request = A2ARequest(
            correlation_id="c1", tool_call_id="t1", caller="marketing", peer="grok", message="copy"
        )
        assert await proj.project(request) is None

        assert await proj.project(
            A2AReply(correlation_id="c1", tool_call_id="t1", caller="marketing", peer="grok", text="answer")
        ) is None

        assert "Consulted" in personas.card_edits[-1]["body"]
        assert personas.card_edits[-1]["thread_id"] is None
        assert personas.sends[-1]["thread_id"] is None
        assert personas.sends[-1]["content"].endswith("answer")
        await proj.finish("c1")
        assert not any("No response" in edit["body"] for edit in personas.card_edits)

    async def test_a_failed_card_edit_does_not_suppress_the_peer_response(self) -> None:
        proj, personas, _ = _make()
        await _begin(proj)
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t1", caller="marketing", peer="grok", message="copy")
        )
        personas.fail_on_component_edit = True

        url = await proj.project(
            A2AReply(correlation_id="c1", tool_call_id="t1", caller="marketing", peer="grok", text="answer")
        )

        assert url == "https://discord.com/channels/42/9001"
        assert personas.sends[-1]["content"].endswith("answer")
        personas.fail_on_component_edit = False
        await proj.finish("c1")
        assert not any("No response" in edit["body"] for edit in personas.card_edits)


def _step(
    kind: str,
    *,
    emitter: str,
    correlation_id: str = "c1",
    text: str = "",
    name: str | None = None,
    outcome: str = "success",
    tool_call_id: str | None = None,
    args: dict[str, Any] | None = None,
) -> StepEvent:
    return StepEvent(
        kind=kind,  # type: ignore[arg-type]
        correlation_id=correlation_id,
        depth=1,
        emitter=emitter,
        text=text,
        name=name,
        outcome=outcome,  # type: ignore[arg-type]
        tool_call_id=tool_call_id,
        args=args,
    )


async def _settle(cycles: int = 20) -> None:
    """Give the writer task ample loop cycles WITHOUT asserting anything — used
    before asserting that something did NOT happen."""
    for _ in range(cycles):
        await asyncio.sleep(0.001)


async def _until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    """Yield to the event loop until ``predicate`` holds — the renderer's writer
    task posts between iterations. Fails the test on timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "timed out waiting for the writer task"
        await asyncio.sleep(0.001)


class TestResponseDeliveryFailure:
    async def test_failed_body_send_allows_finish_to_correct_the_card(self) -> None:
        proj, personas, _ = _make()
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t1", caller="scribe", peer="conan", message="q")
        )
        personas.fail_on_send = True
        assert await proj.project(
            A2AReply(correlation_id="c1", tool_call_id="t1", caller="scribe", peer="conan", text="answer")
        ) is None
        personas.fail_on_send = False
        await proj.finish("c1")
        assert "No response" in personas.card_edits[-1]["body"]


class TestResponseChunking:
    async def test_oversized_response_labels_only_the_first_chunk(self) -> None:
        proj, personas, _ = _make()
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t1", caller="scribe", peer="conan", message="q")
        )
        big = "x" * 4500
        await proj.project(
            A2AReply(correlation_id="c1", tool_call_id="t1", caller="scribe", peer="conan", text=big)
        )
        assert personas.sends[0]["content"].startswith("-# **↩ conan → scribe · response**")
        assert all("response**" not in send["content"] for send in personas.sends[1:])
        rendered = "".join(send["content"] for send in personas.sends)
        assert rendered.endswith(big)
        assert all(len(send["content"]) <= 2000 for send in personas.sends)


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
        personas.fail_on_component_send = True
        assert await proj.project(self._req()) is None

    async def test_a_failed_second_consult_does_not_inherit_the_first_s_link(self) -> None:
        # THE reason this is a receipt and not a lookup. A turn shares ONE
        # correlation_id, so after consult #1 anchors a thread, a lookup would hand
        # consult #2 that same thread even though #2 never reached Discord — a
        # confident link to an exchange that isn't there.
        proj, personas, _resolver = _make()
        assert await proj.project(self._req(tcid="t1", peer="conan", msg="q1")) is not None
        personas.fail_on_component_send = True
        assert await proj.project(self._req(tcid="t2", peer="dot", msg="q2")) is None

    @pytest.mark.parametrize("outcome", ["reject", "failed"])
    async def test_an_outcome_for_a_missing_card_never_returns_an_existing_thread(self, outcome: str) -> None:
        proj, personas, _resolver = _make()
        assert await proj.project(self._req(tcid="t1", peer="conan", msg="q1")) is not None
        personas.fail_on_component_send = True
        assert await proj.project(self._req(tcid="t2", peer="dot", msg="q2")) is None
        projection: A2AReject | A2AFailed
        if outcome == "reject":
            projection = A2AReject(
                correlation_id="c1", tool_call_id="t2", caller="scribe", peer="dot", text="offline"
            )
        else:
            projection = A2AFailed(
                correlation_id="c1", tool_call_id="t2", caller="scribe", peer="dot", text="boom"
            )
        assert await proj.project(projection) is None

    async def test_a_nested_reply_without_a_thread_returns_no_receipt(self) -> None:
        proj, _personas, _resolver = _make()
        assert await proj.project(
            A2AReply(correlation_id="c1", tool_call_id="nested", caller="conan", peer="dot", text="answer")
        ) is None

    async def test_begin_turn_is_idempotent_and_does_not_discard_open_cards(self) -> None:
        proj, personas, _resolver = _make()
        await _begin(proj)
        await proj.project(self._req())
        await proj.begin_turn(correlation_id="c1", root_agent="wrong", subject="wrong")
        await proj.project(
            A2AReject(correlation_id="c1", tool_call_id="t1", caller="scribe", peer="conan", text="offline")
        )
        assert "Not dispatched" in personas.card_edits[-1]["body"]


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
        personas.fail_on_component_send = True
        with caplog.at_level(logging.ERROR, logger="calfcord.bridge.a2a_project"):
            await proj.project(self._req())
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "Manage Channels" in errors[0].getMessage()

    async def test_repeat_failures_do_not_re_log_at_error(self, caplog: pytest.LogCaptureFixture) -> None:
        proj, personas, _resolver = _make()
        personas.fail_on_component_send = True
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
        personas.fail_on_component_send = True
        await proj.project(self._req("c1"))  # arms the latch
        personas.fail_on_component_send = False
        await proj.project(self._req("c2"))
        await proj.project_fault(
            A2ACall(tool_call_id="t1", correlation_id="c2", caller="scribe", peer="conan", message="q")
        )
        personas.fail_on_component_send = True
        caplog.clear()
        with caplog.at_level(logging.ERROR, logger="calfcord.bridge.a2a_project"):
            await proj.project(self._req("c3"))
        assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1

    async def test_recovery_re_arms_the_error(self, caplog: pytest.LogCaptureFixture) -> None:
        # A channel fixed and then re-broken must announce itself again — the latch
        # suppresses noise from ONE ongoing outage, not every future one.
        proj, personas, _resolver = _make()
        personas.fail_on_component_send = True
        await proj.project(self._req("c1"))
        personas.fail_on_component_send = False
        await proj.project(self._req("c2"))  # recovered
        personas.fail_on_component_send = True
        caplog.clear()
        with caplog.at_level(logging.ERROR, logger="calfcord.bridge.a2a_project"):
            await proj.project(self._req("c3"))
        assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1

    async def test_a_nested_dangling_fault_does_not_poison_the_gap_latch(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        proj, personas, _resolver = _make()
        await proj.project(self._req("c1"))
        await proj.project_fault(
            A2ACall(tool_call_id="nested", correlation_id="c1", caller="conan", peer="dot", message="q")
        )
        assert proj._degraded is False
        personas.fail_on_component_send = True
        caplog.clear()
        with caplog.at_level(logging.ERROR, logger="calfcord.bridge.a2a_project"):
            await proj.project(self._req("c2"))
        assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1


class TestBestEffort:
    async def test_discord_failure_is_swallowed(self) -> None:
        proj, personas, _ = _make()
        personas.fail_on_component_send = True
        # must NOT raise — a failed render is an accepted audit gap
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t1", caller="scribe", peer="conan", message="q")
        )
        await proj.project_fault(
            A2ACall(tool_call_id="t1", correlation_id="c1", caller="scribe", peer="conan", message="q")
        )
        assert personas.sends == []


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("hello\nworld", "alice · hello world"),
        ("", "alice · consultation"),
        ("!alice   plan the launch", "alice · plan the launch"),
    ],
)
def test_thread_name_shaping(content: str, expected: str) -> None:
    from calfcord.bridge.a2a_project import _build_thread_name

    assert _build_thread_name("alice", content) == expected
    assert len(_build_thread_name("alice", "a" * 200)) == 100


class TestProjectStep:
    """A consulted agent's own steps render into that turn's thread (ADR-0026).

    The projector owns the audit channel, so it owns this too: the drain loop
    hands it every step whose emitter is not the turn's owner.
    """

    async def test_consulted_agents_step_renders_into_the_turn_thread_under_its_own_persona(self) -> None:
        proj, personas, _ = _make()
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t1", caller="scribe", peer="conan", message="summarize")
        )
        await proj.project_step(_step("tool_call", name="read_file", emitter="conan"))
        await _until(lambda: len(personas.component_sends) == 1)
        sent = personas.component_sends[0]
        assert sent["channel_id"] == 500  # the webhook still hosts on the audit channel
        assert sent["thread_id"] == 9001  # posted INTO the turn's thread
        assert sent["persona"].name == "conan"  # the consulted agent's own identity

    async def test_step_without_a_thread_is_dropped_and_never_creates_one(self) -> None:
        # No consult has been projected, so there is no thread. A step must not
        # invent one: a thread is named from the consult it cannot supply.
        proj, personas, resolver = _make()
        await proj.project_step(_step("tool_call", name="read_file", emitter="conan"))
        await _settle()
        assert personas.component_sends == []
        assert resolver.created == []

    async def test_nested_consult_renders_each_agent_under_its_own_persona(self) -> None:
        # scribe→conan, then conan→dot: all in ONE thread (one turn), each agent's
        # work under its own identity. A persona change opens a fresh message,
        # because a webhook edit cannot change username/avatar.
        proj, personas, resolver = _make()
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t1", caller="scribe", peer="conan", message="q")
        )
        await proj.project_step(_step("tool_call", name="read_file", emitter="conan"))
        await _until(lambda: len(personas.component_sends) == 1)
        await proj.project_step(_step("tool_call", name="search", emitter="dot"))
        await _until(lambda: len(personas.component_sends) == 2)
        assert len(resolver.created) == 1  # still one thread for the turn
        assert [s["persona"].name for s in personas.component_sends] == ["conan", "dot"]
        assert {s["thread_id"] for s in personas.component_sends} == {9001}


class TestRequestPreview:
    """The consult-request preview budget (ADR-0027)."""

    def test_a_short_prompt_is_kept_whole_and_trimmed(self) -> None:
        from calfcord.bridge.a2a_project import _request_preview

        assert _request_preview("  review the auth  ") == "review the auth"
        assert _request_preview("") == ""  # a blank ask stays blank (renders a bare inline marker)

    def test_a_long_prompt_is_cut_to_the_budget_with_an_ellipsis(self) -> None:
        from calfcord.bridge.a2a_project import _REQUEST_PREVIEW_MAX, _request_preview

        out = _request_preview("x" * 200)
        assert out.endswith("…")
        # Pins the budget: a mutation moving _REQUEST_PREVIEW_MAX off 60 fails here.
        assert len(out) == _REQUEST_PREVIEW_MAX + 1


class TestNestedConsultRow:
    """A nested consult (a consulted agent consulting a peer) is announced by a
    RESOLVING row in the consulting agent's trace inside the audit thread
    (ADR-0026), so its peer's work never appears unannounced. The row reuses the
    ConsultRow grammar; the request text is folded onto it and the standalone
    prompt message is suppressed.
    """

    async def _open_turn(self, proj: A2AProjector) -> None:
        # marketing→sol creates the turn's thread; sol is a consulted agent.
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t1", caller="marketing", peer="sol", message="review")
        )

    async def test_renders_a_pending_row_under_the_callers_persona_with_a_preview(self) -> None:
        proj, personas, _ = _make()
        await self._open_turn(proj)
        await proj.project_consult(
            A2ARequest(
                correlation_id="c1",
                tool_call_id="t2",
                caller="sol",
                peer="terra",
                message="review the auth changes in src",
            )
        )
        await _until(lambda: len(personas.component_sends) == 1)
        sent = personas.component_sends[0]
        assert sent["body"] == '◐ sol → terra · "review the auth changes in src"'
        assert sent["persona"].name == "sol"  # the consulting agent's identity
        assert sent["thread_id"] == 9001  # the turn's one thread

    async def test_posts_no_standalone_prompt_message(self) -> None:
        # The whole point: the raw prompt no longer posts as a [sol] message —
        # only the top-level starter is a plain send.
        proj, personas, _ = _make()
        await self._open_turn(proj)
        await proj.project_consult(
            A2ARequest(correlation_id="c1", tool_call_id="t2", caller="sol", peer="terra", message="hi terra")
        )
        await _settle()
        assert personas.sends == []  # the top-level starter is a card; nested prompt is suppressed

    async def test_a_long_prompt_is_truncated_to_a_bounded_preview(self) -> None:
        proj, personas, _ = _make()
        await self._open_turn(proj)
        await proj.project_consult(
            A2ARequest(correlation_id="c1", tool_call_id="t2", caller="sol", peer="terra", message="x" * 200)
        )
        await _until(lambda: len(personas.component_sends) == 1)
        body = personas.component_sends[0]["body"]
        assert body.startswith('◐ sol → terra · "')
        assert body.endswith('…"')  # truncated
        assert len(body) < 100  # bounded, not the full 200 chars

    async def test_an_empty_prompt_does_not_masquerade_as_an_audit_gap(self) -> None:
        # A nested consult with a blank message is still an inline row — its
        # exchange is right below — so it must NOT render the top-level
        # "couldn't write the audit log" marker, which would send an operator
        # chasing a nonexistent permissions problem.
        proj, personas, _ = _make()
        await self._open_turn(proj)
        await proj.project_consult(
            A2ARequest(correlation_id="c1", tool_call_id="t2", caller="sol", peer="terra", message="   ")
        )
        await _until(lambda: len(personas.component_sends) == 1)
        body = personas.component_sends[0]["body"]
        assert body == "◐ sol → terra"  # bare marker, no tail
        assert "audit log" not in body

    async def test_no_thread_drops_the_row_and_never_creates_one(self) -> None:
        # No top-level consult was projected, so there is no thread; a nested row
        # must not invent one (same contract as project_step).
        proj, personas, resolver = _make()
        await proj.project_consult(
            A2ARequest(correlation_id="c1", tool_call_id="t2", caller="sol", peer="terra", message="q")
        )
        await _settle()
        assert personas.component_sends == []
        assert resolver.created == []

    async def test_a_reply_resolves_the_row_in_place(self) -> None:
        proj, personas, _ = _make()
        await self._open_turn(proj)
        await proj.project_consult(
            A2ARequest(correlation_id="c1", tool_call_id="t2", caller="sol", peer="terra", message="review the auth")
        )
        await _until(lambda: len(personas.component_sends) == 1)  # pending row posted
        await proj.project_consult_result(
            A2AReply(correlation_id="c1", tool_call_id="t2", caller="sol", peer="terra", text="done")
        )
        await _until(lambda: len(personas.component_edits) == 1)  # resolved in the SAME message
        assert personas.component_edits[-1]["body"] == '-# ● sol → terra · consulted · "review the auth"'

    async def test_a_rejected_consult_resolves_to_denied_with_its_reason(self) -> None:
        proj, personas, _ = _make()
        await self._open_turn(proj)
        await proj.project_consult(
            A2ARequest(correlation_id="c1", tool_call_id="t2", caller="sol", peer="terra", message="hi")
        )
        await _until(lambda: len(personas.component_sends) == 1)
        await proj.project_consult_result(
            A2AReject(correlation_id="c1", tool_call_id="t2", caller="sol", peer="terra", text="terra is offline")
        )
        await _until(lambda: len(personas.component_edits) == 1)
        assert personas.component_edits[-1]["body"] == '-# ~~⊘ terra~~ — terra is offline · "hi"'

    async def test_a_faulted_consult_resolves_to_the_bright_failed_row(self) -> None:
        proj, personas, _ = _make()
        await self._open_turn(proj)
        await proj.project_consult(
            A2ARequest(correlation_id="c1", tool_call_id="t2", caller="sol", peer="terra", message="hi")
        )
        await _until(lambda: len(personas.component_sends) == 1)
        await proj.project_consult_result(
            A2AFailed(correlation_id="c1", tool_call_id="t2", caller="sol", peer="terra", text="boom")
        )
        await _until(lambda: len(personas.component_edits) == 1)
        assert personas.component_edits[-1]["body"] == '❌ terra didn\'t answer · "hi"'

    async def test_a_dangling_nested_consult_seals_to_never_replied(self) -> None:
        # The peer faulted and never replied, so no project_consult_result ever
        # comes. The seal must resolve the still-pending row to "never replied"
        # (keeping its ask) — never a frozen ◐. This is the failure the resolving
        # row exists to prevent.
        proj, personas, _ = _make()
        await self._open_turn(proj)
        await proj.project_consult(
            A2ARequest(correlation_id="c1", tool_call_id="t2", caller="sol", peer="terra", message="review the auth")
        )
        await _until(lambda: len(personas.component_sends) == 1)
        await proj.seal("c1", faulted=True)
        await proj.finish("c1")
        body = _last_component_body(personas)
        assert '~~⊘ terra~~ — never replied · "review the auth"' in body
        assert "◐" not in body  # nothing left claiming to be in flight

    async def test_the_row_posts_before_the_peers_box(self) -> None:
        # The feature's payoff: the "consulting" row is announced FIRST, then the
        # peer's own work renders below it, under the peer's own persona, in a
        # separate message (persona change opens one), in that order.
        proj, personas, _ = _make()
        await self._open_turn(proj)
        await proj.project_consult(
            A2ARequest(correlation_id="c1", tool_call_id="t2", caller="sol", peer="terra", message="review")
        )
        await _until(lambda: len(personas.component_sends) == 1)
        await proj.project_step(_step("tool_call", emitter="terra", name="read_file", tool_call_id="x1", args={}))
        await _until(lambda: len(personas.component_sends) == 2)
        first, second = personas.component_sends[0], personas.component_sends[1]
        assert first["persona"].name == "sol"
        assert first["body"].startswith("◐ sol → terra")
        assert second["persona"].name == "terra"  # the peer's box, strictly after the row

    async def test_a_result_for_a_dropped_row_is_a_harmless_no_op(self) -> None:
        # When the top-level render failed there is no thread, so project_consult
        # opens no row. A subsequent outcome must be a silent no-op — not a crash,
        # not a conjured thread — matching the best-effort contract.
        proj, personas, resolver = _make()
        await proj.project_consult(
            A2ARequest(correlation_id="c1", tool_call_id="t2", caller="sol", peer="terra", message="q")
        )
        await proj.project_consult_result(
            A2AReply(correlation_id="c1", tool_call_id="t2", caller="sol", peer="terra", text="done")
        )
        await _settle()
        assert personas.component_sends == []
        assert personas.component_edits == []
        assert resolver.created == []


class TestResponseOrdering:
    async def test_reply_flushes_dirty_peer_trace_before_posting(self) -> None:
        proj, personas, _ = _make(interval=60.0)
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t1", caller="scribe", peer="conan", message="q")
        )
        await proj.project_step(
            _step("tool_call", emitter="conan", name="read_file", tool_call_id="x1", args={"path": "a.py"})
        )
        await _until(lambda: len(personas.component_sends) == 1)
        await proj.project_step(_step("tool_result", emitter="conan", name="read_file", tool_call_id="x1"))
        assert personas.component_edits == []  # parked behind the 60s throttle

        await asyncio.wait_for(
            proj.project(A2AReply(correlation_id="c1", tool_call_id="t1", caller="scribe", peer="conan", text="done")),
            timeout=2.0,
        )

        assert len(personas.component_edits) == 1
        assert personas.sends[-1]["content"].endswith("done")


class TestFinish:
    """The turn's per-correlation state is retired — the trace is flushed and the
    thread mapping evicted, so neither leaks for the bridge's lifetime."""

    async def test_finish_flushes_pending_trace(self) -> None:
        proj, personas, _ = _make(interval=60.0)  # writer parks: nothing flushes on its own
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t1", caller="scribe", peer="conan", message="q")
        )
        await proj.project_step(_step("tool_call", name="read_file", emitter="conan"))
        await _until(lambda: len(personas.component_sends) == 1)  # leading edge posts
        await proj.project_step(_step("tool_result", name="read_file", emitter="conan"))
        await _settle()
        assert personas.component_edits == []  # parked behind the interval
        await proj.finish("c1")
        assert len(personas.component_edits) == 1  # finish drained it

    async def test_finish_resolves_any_pending_top_level_card_as_interrupted(self) -> None:
        proj, personas, _ = _make()
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t1", caller="scribe", peer="conan", message="q")
        )
        await proj.finish("c1")
        assert "No response" in personas.card_edits[-1]["body"]

    async def test_finish_evicts_the_thread_mapping(self) -> None:
        proj, _personas, _ = _make()
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t1", caller="scribe", peer="conan", message="q")
        )
        assert proj._threads["c1"] == 9001
        await proj.finish("c1")
        assert "c1" not in proj._threads

    async def test_finish_evicts_the_thread_even_if_the_flush_does_not_complete(self) -> None:
        # The eviction IS the leak fix (nothing else removes an entry), so it is
        # unconditional. The stand-in raises to pin that; the REAL renderer contains
        # its own flush errors, so in production the reachable escape is a
        # CancelledError at shutdown. Either way the entry must not be stranded —
        # and the two have no dependency: the entry carries its own destination.
        personas, resolver = _FakePersonas(), _FakeResolver()

        class _RaisingSteps:
            async def on_step(self, step: Any, dest: Any, *, acting_agent: str) -> None: ...

            async def flush(self, correlation_id: str) -> None: ...

            async def finish(self, correlation_id: str) -> None:
                raise RuntimeError("flush boom")

        proj = A2AProjector(resolver, personas, _RaisingSteps())  # type: ignore[arg-type]
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t1", caller="scribe", peer="conan", message="q")
        )
        assert proj._threads["c1"] == 9001
        with pytest.raises(RuntimeError):
            await proj.finish("c1")
        assert "c1" not in proj._threads


def _last_component_body(personas: Any) -> str:
    """The most recent body the audit thread would have shown.

    Whether it lands as a send or an edit depends on whether the writer got a
    turn between appends — a throttle detail, not what these tests are about.
    """
    calls = personas.component_edits or personas.component_sends
    assert calls, "nothing was posted"
    return str(calls[-1]["body"])


class TestConsultedWorkUsesTheRowGrammar:
    """A consulted agent's trace is the SAME surface as the human's, so it gets
    the same grammar for free (ADR-0024): one row per tool that resolves in
    place, dim at rest, bright only when it needs you.

    That is the point of routing here rather than inventing a second renderer —
    23 tool calls are ONE edited message, not 23 (ADR-0017), and not 46.
    """

    async def test_a_consulted_agents_tool_is_one_row_that_resolves(self) -> None:
        proj, personas, _ = _make()
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t1", caller="scribe", peer="conan", message="q")
        )
        await proj.project_step(
            _step("tool_call", emitter="conan", name="read_file", tool_call_id="x1", args={"path": "a.py"})
        )
        await _until(lambda: len(personas.component_sends) == 1)
        assert personas.component_sends[0]["body"] == r"◐ read\_file a.py"

        await proj.project_step(_step("tool_result", emitter="conan", name="read_file", tool_call_id="x1"))
        await _until(lambda: len(personas.component_edits) == 1)
        body = personas.component_edits[-1]["body"]
        assert body.startswith(r"-# ● read\_file a.py · ")  # resolved in place, dim
        assert body.count("read") == 1  # ONE row, not a called/returned pair

    async def test_a_failed_tool_is_the_one_bright_row(self) -> None:
        proj, personas, _ = _make()
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t1", caller="scribe", peer="conan", message="q")
        )
        await proj.project_step(
            _step("tool_call", emitter="conan", name="fetch", tool_call_id="x1", args={"url": "u"})
        )
        await proj.project_step(
            _step("tool_result", emitter="conan", name="fetch", tool_call_id="x1", outcome="failed", text="418")
        )
        await _settle()
        assert _last_component_body(personas) == "❌ fetch u — 418"

    async def test_a_faulted_consult_seals_with_the_work_it_did(self) -> None:
        # ADR-0026's motivating case, end to end: an agent makes tool calls, then
        # faults, and never replies. Its thread used to hold the question and a
        # shrug. Now it holds the work AND says where it stopped.
        proj, personas, _ = _make()
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t1", caller="sol", peer="terra", message="research")
        )
        for i in range(3):
            await proj.project_step(
                _step("tool_call", emitter="terra", name=f"probe{i}", tool_call_id=f"x{i}", args={})
            )
            await proj.project_step(_step("tool_result", emitter="terra", name=f"probe{i}", tool_call_id=f"x{i}"))
        await proj.seal("c1", faulted=True)
        await proj.finish("c1")
        body = _last_component_body(personas)
        assert "⚠️ run failed after 3 tools · " in body
        assert "◐" not in body  # nothing left claiming to be in flight

    async def test_finish_seals_a_consulted_trace_that_never_terminated(self) -> None:
        proj, personas, _ = _make()
        await proj.project(
            A2ARequest(correlation_id="c1", tool_call_id="t1", caller="sol", peer="terra", message="q")
        )
        await proj.project_step(_step("tool_call", emitter="terra", name="probe", tool_call_id="x1", args={}))
        await proj.finish("c1")
        # Not "run failed": no terminal was seen, so the outcome is unknown.
        assert "⊘ interrupted after 1 tool · " in _last_component_body(personas)

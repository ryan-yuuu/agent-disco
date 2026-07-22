"""Unit tests for the per-!mention orchestration (spec §5.2).

Drives :class:`MentionHandler` through a ``FakeHandle`` (scripted ``stream()`` +
``result()``) and recording collaborator fakes — no Kafka, no Discord, no LLM.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from calfkit.client import AgentMessageEvent, HandoffEvent, RunCompleted, RunFailed, ToolCallEvent, ToolResultEvent
from calfkit.exceptions import NodeFaultError
from calfkit.models.error_report import ErrorReport, ExceptionInfo, FaultTypes
from calfkit.models.node_result import InvocationResult
from calfkit.models.payload import TextPart
from calfkit.models.state import State

from calfcord.agents.thinking import build_model_settings_union
from calfcord.bridge.mention_handler import MentionHandler, MentionRequest
from calfcord.bridge.wire import WireAuthor, WireMessage


# --- fakes -----------------------------------------------------------------
class _FakeHandle:
    def __init__(
        self,
        *,
        steps: tuple[Any, ...] = (),
        result: Any = None,
        fault: Exception | None = None,
        correlation_id: str = "c1",
    ) -> None:
        self._steps = list(steps)
        self._result = result
        self._fault = fault
        self.correlation_id = correlation_id

    async def stream(self) -> Any:
        for s in self._steps:
            yield s
        # The real calfkit stream() is terminal-bearing: it yields the terminal
        # (RunCompleted / RunFailed) as its LAST event. normalize_run_event maps
        # those to None, so the handler drain must skip them (`if step is None:
        # continue`). Yield one here so that skip is exercised at the integration
        # level — a refactor to direct `event.kind` access would crash the drain.
        if self._fault is not None:
            report = getattr(self._fault, "report", None) or ErrorReport(error_type="calf.test.fault")
            yield RunFailed(report=report, correlation_id=self.correlation_id)
        else:
            output = getattr(self._result, "output", "") if self._result is not None else ""
            yield RunCompleted(output=output, correlation_id=self.correlation_id, agent="scribe", _envelope=None)

    async def result(self, *, timeout: float | None = None) -> Any:
        if self._fault is not None:
            raise self._fault
        return self._result


class _FakeGateway:
    def __init__(self, handles: list[_FakeHandle]) -> None:
        self._handles = handles
        self.starts: list[dict[str, Any]] = []

    @property
    def started(self) -> dict[str, Any] | None:
        """The first ``start()`` call's kwargs (the original invocation)."""
        return self.starts[0] if self.starts else None

    async def start(self, prompt: str, **kwargs: Any) -> _FakeHandle:
        self.starts.append({"prompt": prompt, **kwargs})
        # Successive start()s consume successive handles; the last handle is
        # reused if start() is called more times than handles given.
        return self._handles[min(len(self.starts) - 1, len(self._handles) - 1)]


class _FakeClient:
    def __init__(self, handles: list[_FakeHandle]) -> None:
        self.gw = _FakeGateway(handles)
        self.requested_agent: str | None = None

    def agent(self, name: str) -> _FakeGateway:
        self.requested_agent = name
        return self.gw


class _FakeRoster:
    def __init__(self, online: frozenset[str] | None) -> None:
        self._online = online
        self.refreshes = 0

    async def refresh(self) -> None:
        self.refreshes += 1

    def online(self) -> frozenset[str] | None:
        return self._online


class _FakeHistory:
    def __init__(self) -> None:
        self.calls = 0

    async def message_history(self, req: MentionRequest) -> list[Any]:
        self.calls += 1
        return []


class _FakeOverrides:
    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self._m = mapping or {}

    def effort_for(self, agent_id: str) -> str | None:
        return self._m.get(agent_id)


class _FakeA2A:
    def __init__(self, *, url: str | None = "https://discord.com/channels/42/9001") -> None:
        self.projected: list[Any] = []
        self.faults: list[Any] = []
        # Consulted agents' own steps, routed here instead of dropped (ADR-0026).
        self.steps: list[Any] = []
        # Nested-consult rows (announce) and their resolutions — kept apart from
        # `projected` so the suppressed raw prompt message is assertable (ADR-0026).
        self.consult_rows: list[Any] = []
        self.consult_row_results: list[Any] = []
        self.seals: list[tuple[str, bool]] = []
        # Ordering matters and is asserted: `finish` MUST come after `fault`, or
        # the fault note lands in a second, freshly created thread.
        self.calls: list[str] = []
        # The audit thread's jump link; ``None`` models a swallowed projection
        # failure (no thread was anchored), which the marker must surface.
        self.url = url
        self.begun: list[tuple[str, str, str]] = []

    async def begin_turn(self, *, correlation_id: str, root_agent: str, subject: str) -> None:
        self.begun.append((correlation_id, root_agent, subject))

    async def project(self, projection: Any) -> str | None:
        self.projected.append(projection)
        self.calls.append("project")
        return self.url

    async def project_fault(self, call: Any) -> None:
        self.faults.append(call)
        self.calls.append("fault")

    async def project_step(self, step: Any) -> None:
        self.steps.append(step)

    async def project_consult(self, request: Any) -> None:
        self.consult_rows.append(request)
        self.calls.append("project_consult")

    async def project_consult_result(self, projection: Any) -> None:
        self.consult_row_results.append(projection)
        self.calls.append("project_consult_result")

    async def seal(self, correlation_id: str, *, faulted: bool) -> None:
        self.seals.append((correlation_id, faulted))

    async def finish(self, correlation_id: str) -> None:
        self.calls.append("finish")


class _FakeTrace:
    def __init__(self) -> None:
        self.steps: list[Any] = []
        self.finished: list[str] = []
        # Track the owning agent passed to each on_step call so handoff-tracking
        # tests can assert that tool steps after a handoff carry the new agent.
        self.acting_agents: list[str] = []
        # Where each step was told to render — the handler's own derivation.
        self.dests: list[Any] = []
        # Consult rows opened (key, peer, url, persona_name) and resolved
        # (key, state) — bridge annotations on the turn, not run steps.
        self.consults: list[tuple[str, str, str | None, str]] = []
        # (key, state, note) — `note` is recorded because ADR-0020's privacy rule
        # lives in WHAT the handler passes here. Dropping it made the rule
        # unobservable: a mutation leaking the peer's reply passed every test.
        self.consult_results: list[tuple[str, str, str]] = []
        # (correlation_id, faulted) per seal — driven by the stream's terminal.
        self.seals: list[tuple[str, bool]] = []

    async def on_step(self, step: Any, dest: Any, *, acting_agent: str) -> None:
        self.steps.append(step)
        self.dests.append(dest)
        self.acting_agents.append(acting_agent)

    async def on_consult(
        self,
        key: str,
        peer: str,
        thread_url: str | None,
        dest: Any,
        *,
        correlation_id: str,
        persona_name: str,
    ) -> None:
        self.consults.append((key, peer, thread_url, persona_name))

    async def on_consult_result(self, key: str, *, state: str, note: str, correlation_id: str) -> None:
        self.consult_results.append((key, state, note))

    async def seal(self, correlation_id: str, *, faulted: bool) -> None:
        self.seals.append((correlation_id, faulted))

    async def finish(self, correlation_id: str) -> None:
        self.finished.append(correlation_id)


class _FakeReply:
    def __init__(self, outcomes: list[str] | None = None) -> None:
        self.replies: list[tuple[Any, str]] = []
        self.notices: list[str] = []
        # Every correlation_id the handler passed — the post must be keyed on the
        # run's id so the transcript row matches the posted reply.
        self.correlation_ids: list[str] = []
        # Scripted per-post_reply outcomes; defaults to "posted" once exhausted.
        self._outcomes = list(outcomes or [])

    async def post_reply(
        self, req: MentionRequest, persona: Any, result: Any, *, initial_len: int, correlation_id: str
    ) -> str:
        self.replies.append((persona, result.output))
        self.correlation_ids.append(correlation_id)
        return self._outcomes.pop(0) if self._outcomes else "posted"

    async def post_notice(self, req: MentionRequest, text: str) -> None:
        self.notices.append(text)


class _FakeSticky:
    def __init__(self) -> None:
        self.sets: list[tuple[str, str]] = []

    async def set_sticky_owner(self, conversation_key: str, owner_agent_id: str) -> None:
        self.sets.append((conversation_key, owner_agent_id))


def _result(output: str, emitter: str) -> InvocationResult:
    return InvocationResult(
        output=output, state=State(message_history=[]), correlation_id="c1", emitter_node_id=emitter
    )


def _agent_msg(text: str, emitter: str = "scribe") -> AgentMessageEvent:
    return AgentMessageEvent(correlation_id="c1", depth=0, frame_id="f", emitter=emitter, parts=[TextPart(text=text)])


def _consult(tcid: str, peer: str, message: str, caller: str = "scribe") -> ToolCallEvent:
    return ToolCallEvent(
        correlation_id="c1",
        depth=1,
        frame_id="f",
        emitter=caller,
        tool_call_id=tcid,
        name="message_agent",
        args={"name": peer, "message": message},
    )


def _consult_outcome(tcid: str, text: str, outcome: str, caller: str = "scribe") -> ToolResultEvent:
    """A consult that was REJECTED (the caller refused to dispatch — offline,
    self, a cycle) or FAULTED (the peer engaged, then blew up).

    These are the branches this redesign exists for: today's marker is written at
    request time in the past tense, so both of these leave an optimistic
    "consulted" line in the human's thread while the warning goes only to the
    audit thread.
    """
    return ToolResultEvent(
        correlation_id="c1",
        depth=1,
        frame_id="f",
        emitter=caller,
        tool_call_id=tcid,
        name="message_agent",
        parts=[TextPart(text=text)],
        outcome=outcome,  # type: ignore[arg-type]
    )


def _consult_reply(tcid: str, peer: str, text: str, caller: str = "scribe") -> ToolResultEvent:
    """A consult's reply as calfkit actually emits it.

    NOT ``emitter=<peer>, name=<peer>``: the result is minted on the CALLER's fold
    hop (``nodes/_steps.py`` ``folded``) with ``name`` echoed from the call's
    marker, so the peer appears on neither field — which is exactly why the
    dispatcher pairs on ``tool_call_id`` and takes the peer from the request.
    """
    return ToolResultEvent(
        correlation_id="c1",
        depth=1,
        frame_id="f",
        emitter=caller,
        tool_call_id=tcid,
        name="message_agent",
        parts=[TextPart(text=text)],
        outcome="success",
    )


def _handoff(target: str, reason: str = "", emitter: str = "scribe") -> HandoffEvent:
    return HandoffEvent(correlation_id="c1", depth=0, frame_id="f", emitter=emitter, target=target, reason=reason)


def _tool_call(tcid: str, tool_name: str, emitter: str = "scribe") -> ToolCallEvent:
    """A non-message_agent tool call (the step-trace path, not A2A)."""
    return ToolCallEvent(
        correlation_id="c1",
        depth=1,
        frame_id="f",
        emitter=emitter,
        tool_call_id=tcid,
        name=tool_name,
        args={},
    )


def _wire(content: str = "hello") -> WireMessage:
    return WireMessage(
        event_id="e1",
        kind="message",
        message_id=1,
        channel_id=10,
        source_channel_id=10,
        guild_id=42,
        content=content,
        author=WireAuthor(discord_user_id=111, display_name="alice", is_bot=False, is_webhook=False),
        created_at=datetime.now(UTC),
    )


def _req(
    content: str = "hello",
    mentions: tuple[str, ...] = ("scribe",),
    *,
    route_kind: str = "explicit",
) -> MentionRequest:
    return MentionRequest(
        content=content,
        mention_ids=mentions,
        author_label="alice",
        message_id=1,
        source_channel_id=10,
        channel_id=10,
        wire=_wire(content),
        reply_target=object(),
        route_kind=route_kind,  # type: ignore[arg-type]
    )


def _make(
    *,
    online: frozenset[str] | None = frozenset({"scribe"}),
    handle: _FakeHandle | None = None,
    handles: list[_FakeHandle] | None = None,
    overrides: dict[str, str] | None = None,
    reply_outcomes: list[str] | None = None,
    trace: Any = None,
    sticky: _FakeSticky | None = None,
) -> tuple[MentionHandler, _FakeClient, dict[str, Any]]:
    if handles is None:
        handles = [handle if handle is not None else _FakeHandle(result=_result("done", "scribe"))]
    client = _FakeClient(handles)
    fakes = {
        "a2a": _FakeA2A(),
        "trace": trace if trace is not None else _FakeTrace(),
        "reply": _FakeReply(reply_outcomes),
        "history": _FakeHistory(),
        "sticky": sticky if sticky is not None else _FakeSticky(),
    }
    handler = MentionHandler(
        client=client,
        roster=_FakeRoster(online),
        history=fakes["history"],
        overrides=_FakeOverrides(overrides),
        a2a=fakes["a2a"],
        trace=fakes["trace"],
        reply=fakes["reply"],
        memory_deps=lambda: {"memory_prompt": "tmpl"},
        sticky=fakes["sticky"],
    )
    return handler, client, fakes


class TestRouting:
    async def test_happy_path_posts_reply_under_emitter_persona(self) -> None:
        handler, client, fakes = _make()
        await handler.handle(_req(mentions=("scribe",)))
        assert client.requested_agent == "scribe"
        # start carried history, author, the serialized discord wire, memory deps.
        started = client.gw.started
        assert started["author"] == "alice"
        discord_dep = started["deps"]["discord"]
        assert discord_dep["content"] == "hello" and discord_dep["channel_id"] == 10
        assert started["deps"]["memory_prompt"] == "tmpl"
        # one reply, persona named for the emitter, text == result.output.
        assert len(fakes["reply"].replies) == 1
        persona, text = fakes["reply"].replies[0]
        assert persona.name == "scribe" and text == "done"
        assert fakes["trace"].finished == ["c1"]
        assert fakes["reply"].notices == []

    async def test_reply_uses_handoff_emitter_persona_not_target(self) -> None:
        handler, _client, fakes = _make(handle=_FakeHandle(result=_result("handed off answer", "conan")))
        await handler.handle(_req(mentions=("scribe",)))
        persona, _ = fakes["reply"].replies[0]
        assert persona.name == "conan"  # the node that actually replied

    async def test_successful_reply_sets_sticky_owner_to_emitter(self) -> None:
        handler, _client, fakes = _make(handle=_FakeHandle(result=_result("handed off answer", "conan")))
        await handler.handle(_req(mentions=("scribe",)))
        assert fakes["sticky"].sets == [("10", "conan")]

    async def test_empty_reply_does_not_set_sticky_owner(self) -> None:
        handler, _client, fakes = _make(
            handle=_FakeHandle(result=_result("", "scribe")),
            reply_outcomes=["empty"],
        )
        await handler.handle(_req(mentions=("scribe",)))
        assert fakes["sticky"].sets == []

    async def test_lost_reply_does_not_set_sticky_owner(self) -> None:
        handler, _client, fakes = _make(reply_outcomes=["lost"])
        await handler.handle(_req(mentions=("scribe",)))
        assert fakes["sticky"].sets == []

    async def test_no_mention_is_unanswered(self) -> None:
        handler, client, fakes = _make()
        await handler.handle(_req(mentions=()))
        assert client.requested_agent is None
        assert fakes["reply"].replies == [] and fakes["reply"].notices == []

    async def test_mentioned_but_none_online_posts_notice(self) -> None:
        handler, client, fakes = _make(online=frozenset({"scribe"}))
        await handler.handle(_req(mentions=("ghost",)))
        assert client.requested_agent is None
        assert fakes["reply"].replies == []
        # The notice echoes the mention with the live trigger prefix (``!ghost``),
        # so a prefix change can't silently desync the user-facing wording.
        assert len(fakes["reply"].notices) == 1 and "`!ghost`" in fakes["reply"].notices[0]

    async def test_first_online_mention_wins(self) -> None:
        handler, client, _fakes = _make(online=frozenset({"conan"}))
        await handler.handle(_req(mentions=("ghost", "conan")))
        assert client.requested_agent == "conan"

    async def test_roster_unavailable_fails_fast_with_notice(self) -> None:
        handler, client, fakes = _make(online=None)
        await handler.handle(_req(mentions=("scribe",)))
        assert client.requested_agent is None
        assert len(fakes["reply"].notices) == 1 and "roster" in fakes["reply"].notices[0].lower()

    async def test_sticky_routed_offline_owner_posts_sticky_notice_without_clearing(self) -> None:
        handler, client, fakes = _make(online=frozenset())
        await handler.handle(_req(mentions=("scribe",), route_kind="sticky"))
        assert client.requested_agent is None
        assert fakes["reply"].replies == []
        assert fakes["sticky"].sets == []
        assert fakes["reply"].notices == [
            "This conversation is sticky to `!scribe`, but that agent is offline. "
            "Use `!unstick` or address another agent with `!name`."
        ]


class TestStreamDrain:
    async def test_a2a_consult_goes_to_projector_not_trace(self) -> None:
        steps = (_consult("t1", "conan", "summarize"), _consult_reply("t1", "conan", "the summary"))
        handler, _client, fakes = _make(handle=_FakeHandle(steps=steps, result=_result("done", "scribe")))
        await handler.handle(_req())
        assert len(fakes["a2a"].projected) == 2  # request + reply
        assert fakes["trace"].steps == []

    async def test_consulted_peer_steps_do_not_leak_into_the_human_thread(self) -> None:
        # calfkit flushes EVERY hop's steps to the ROOT caller (base.py's
        # ``_flush_steps`` → ``stack.root.callback_topic``), so a consulted peer's
        # own preamble and tool calls arrive on this run's stream stamped with the
        # PEER as emitter. That work is the A2A audit channel's business, not live
        # progress for the human — rendering it would spill a private exchange into
        # the mention's thread (and, for the peer's tool steps, under the CALLER's
        # persona, since ``acting_agent`` only transfers on handoff).
        steps = (
            _agent_msg("let me ask conan", emitter="scribe"),
            _consult("t1", "conan", "latency budget?"),
            _agent_msg("checking the ingest path…", emitter="conan"),  # the peer's preamble
            _tool_call("t2", "grep_codebase", emitter="conan"),  # the peer's own tool call
            _consult_reply("t1", "conan", "about 200ms"),
        )
        handler, _client, fakes = _make(handle=_FakeHandle(steps=steps, result=_result("done", "scribe")))
        await handler.handle(_req())
        # Only the caller's own step is the step trace; the consult itself is audited.
        assert [s.emitter for s in fakes["trace"].steps] == ["scribe"]
        assert len(fakes["a2a"].projected) == 2  # request + reply

    async def test_consult_leaves_a_cross_link_marker_in_the_human_thread(self) -> None:
        # The exchange is private (it projects to the audit channel), but the
        # consult itself must not be invisible from the conversation that caused
        # it: the trace gets ONE row per consult, under the CALLER's persona,
        # linking into the audit thread. The reply adds no second row — it
        # RESOLVES the one already there, which is what stops a rejected or
        # faulted consult from reading as though the peer answered.
        steps = (_consult("t1", "conan", "latency budget?"), _consult_reply("t1", "conan", "200ms"))
        handler, _client, fakes = _make(handle=_FakeHandle(steps=steps, result=_result("done", "scribe")))
        await handler.handle(_req())
        assert fakes["trace"].consults == [("t1", "conan", "https://discord.com/channels/42/9001", "scribe")]
        assert fakes["trace"].consult_results == [("t1", "ok", "")]

    async def test_consult_marker_surfaces_the_audit_gap_when_projection_failed(self) -> None:
        # The projection is best-effort and swallows Discord failures, so a broken
        # audit channel used to be invisible to everyone but a log reader. With no
        # thread to link, the marker states the gap in the human's own thread.
        steps = (_consult("t1", "conan", "latency budget?"),)
        handler, _client, fakes = _make(handle=_FakeHandle(steps=steps, result=_result("done", "scribe")))
        fakes["a2a"].url = None  # the projection failed; no thread was anchored
        await handler.handle(_req())
        assert fakes["trace"].consults == [("t1", "conan", None, "scribe")]

    async def test_peer_s_internal_handoff_does_not_hijack_the_owner(self) -> None:
        # A consulted peer may itself hand off (`handoff` defaults on), and that
        # handoff flushes to the ROOT caller like every other hop. It must not
        # advance `acting_agent`: control of the HUMAN's turn never left scribe.
        # Letting a peer's handoff transfer ownership silently blackholes every
        # later step the owner emits — the whole trace stops mid-turn.
        steps = (
            _consult("t1", "conan", "q"),
            _handoff("dot", "yours", emitter="conan"),  # conan's PRIVATE handoff
            _consult_reply("t1", "conan", "a"),
            _agent_msg("here's the answer", emitter="scribe"),  # the owner, still in control
        )
        handler, _client, fakes = _make(handle=_FakeHandle(steps=steps, result=_result("done", "scribe")))
        await handler.handle(_req())
        assert [s.emitter for s in fakes["trace"].steps] == ["scribe"]

    async def test_nested_consult_does_not_leak_a_marker_into_the_human_thread(self) -> None:
        # A consulted peer consulting ITS own peer (B→C inside A→B) is audited —
        # the dispatcher classifies it from any hop — but it is the peer's private
        # business. It is announced as a row in the peer's OWN trace inside the
        # audit thread (ADR-0026), never as a marker in the human's thread;
        # otherwise the cross-link becomes the very leak it replaced.
        steps = (
            _consult("t1", "conan", "q"),  # scribe → conan: the owner's consult
            _consult("t2", "dot", "z", caller="conan"),  # conan → dot: PRIVATE
        )
        handler, _client, fakes = _make(handle=_FakeHandle(steps=steps, result=_result("done", "scribe")))
        await handler.handle(_req())
        assert len(fakes["a2a"].projected) == 1  # only the owner's consult is a projected message...
        assert len(fakes["a2a"].consult_rows) == 1  # ...the nested one is audited as a row instead
        assert [persona for *_rest, persona in fakes["trace"].consults] == ["scribe"]

    async def test_plain_agent_message_goes_to_the_trace(self) -> None:
        handler, _client, fakes = _make(
            handle=_FakeHandle(steps=(_agent_msg("thinking…"),), result=_result("done", "scribe"))
        )
        await handler.handle(_req())
        assert len(fakes["trace"].steps) == 1
        assert fakes["a2a"].projected == []

    async def test_handoff_goes_to_progress_not_a2a(self) -> None:
        # A handoff transfers conversation control (ADR-0019) — distinct from a
        # message_agent consult — so it renders inline via the step-trace renderer
        # (the dispatcher no longer claims it), not the A2A audit channel.
        handler, _client, fakes = _make(
            handle=_FakeHandle(steps=(_handoff("dot", "prose is yours"),), result=_result("done", "scribe"))
        )
        await handler.handle(_req())
        assert [s.kind for s in fakes["trace"].steps] == ["handoff"]
        assert fakes["a2a"].projected == []

    async def test_acting_agent_transfers_on_handoff(self) -> None:
        # After a handoff from scribe → dot, subsequent tool steps carry dot as
        # the owning agent (their trace rows must appear under dot's persona,
        # not scribe's). The handoff step itself still carries the pre-transfer
        # owner (scribe announced it).
        steps = (
            _tool_call("t1", "terminal", emitter="scribe"),  # scribe's tool call
            _handoff("dot", "you handle this"),  # scribe hands off to dot
            # Post-handoff steps are emitted by DOT's node, so they carry dot as
            # emitter (calfkit stamps ``emitter=self.node_id`` per hop) — a handoff
            # transfers control, so these stay the step trace under the new owner.
            _tool_call("t2", "read_file", emitter="dot"),  # dot's tool call
            _agent_msg("done", emitter="dot"),  # dot's reply
        )
        handler, _client, fakes = _make(
            handle=_FakeHandle(steps=steps, result=_result("done", "dot"))
        )
        await handler.handle(_req(mentions=("scribe",)))
        # acting_agents aligns 1:1 with steps
        assert fakes["trace"].acting_agents == ["scribe", "scribe", "dot", "dot"]

    async def test_render_fault_in_drain_does_not_lose_terminal_reply(self) -> None:
        # I-3: a render/normalize/classify fault on a step must be swallowed (logged)
        # so it can't unwind the drain and cost the user the already-computed reply.
        class _RaisingTrace(_FakeTrace):
            async def on_step(self, step: Any, req: MentionRequest, *, acting_agent: str) -> None:
                raise RuntimeError("render boom")

        handler, _client, fakes = _make(
            handle=_FakeHandle(steps=(_agent_msg("thinking…"),), result=_result("done", "scribe")),
            trace=_RaisingTrace(),
        )
        await handler.handle(_req())
        # the terminal reply still posts, and finish() still ran (the drain didn't unwind).
        assert [t for _, t in fakes["reply"].replies] == ["done"]
        assert fakes["trace"].finished == ["c1"]


class TestTerminalErrors:
    async def test_fault_posts_error_and_synthesizes_dangling_a2a_notes(self) -> None:
        # A consult is opened (tool_call) but the peer faults — no reply step;
        # result() raises NodeFaultError, faulting the whole run (D-2).
        handle = _FakeHandle(steps=(_consult("t9", "conan", "x"),), fault=NodeFaultError("peer_fault", message="boom"))
        handler, _client, fakes = _make(handle=handle)
        await handler.handle(_req())
        assert len(fakes["a2a"].projected) == 1  # the request rendered before the fault
        assert len(fakes["a2a"].faults) == 1  # the dangling consult → failure note
        assert fakes["a2a"].faults[0].peer == "conan"
        assert len(fakes["reply"].notices) == 1 and fakes["reply"].replies == []


class TestOverrides:
    async def test_effort_override_applied_as_provider_blind_union(self) -> None:
        handler, client, _fakes = _make(overrides={"scribe": "high"})
        await handler.handle(_req(mentions=("scribe",)))
        assert client.gw.started["model_settings"] == build_model_settings_union("high")

    async def test_no_override_passes_none_model_settings(self) -> None:
        handler, client, _fakes = _make()
        await handler.handle(_req(mentions=("scribe",)))
        assert client.gw.started["model_settings"] is None


@pytest.mark.parametrize("output", ["", "a long reply"])
async def test_output_text_round_trips(output: str) -> None:
    handler, _client, fakes = _make(handle=_FakeHandle(result=_result(output, "scribe")))
    await handler.handle(_req())
    assert fakes["reply"].replies[0][1] == output


class TestDelivery:
    async def test_lost_reply_posts_notice_without_reinvoking(self) -> None:
        handler, client, fakes = _make(reply_outcomes=["lost"])
        await handler.handle(_req())
        # Delivery is single-pass: one invocation, one post attempt, no retry.
        assert len(client.gw.starts) == 1
        assert len(fakes["reply"].replies) == 1
        # I-2: a lost reply must surface an operator notice, not ghost the user.
        assert len(fakes["reply"].notices) == 1 and "couldn't post" in fakes["reply"].notices[0].lower()

    async def test_empty_reply_is_silent(self) -> None:
        handler, _client, fakes = _make(
            handle=_FakeHandle(result=_result("", "scribe")),
            reply_outcomes=["empty"],
        )
        await handler.handle(_req())
        # Nothing to deliver is a no-op, not a loss — no operator notice.
        assert fakes["reply"].notices == []

    async def test_posted_reply_without_sticky_store_is_fine(self) -> None:
        # The sticky store is an optional collaborator — a bridge configured
        # without one still delivers replies (it just tracks no owner).
        client = _FakeClient([_FakeHandle(result=_result("done", "scribe"))])
        reply = _FakeReply()
        handler = MentionHandler(
            client=client,
            roster=_FakeRoster(frozenset({"scribe"})),
            history=_FakeHistory(),
            overrides=_FakeOverrides(),
            a2a=_FakeA2A(),
            trace=_FakeTrace(),
            reply=reply,
            sticky=None,
        )
        await handler.handle(_req())  # must not raise on the sticky-owner step
        assert [t for _, t in reply.replies] == ["done"]

    async def test_post_keyed_on_run_correlation_id(self) -> None:
        # The post must carry the run's correlation_id so the transcript row
        # written by the poster matches the run that produced the reply.
        handler, _client, fakes = _make(handle=_FakeHandle(result=_result("x", "scribe"), correlation_id="c1"))
        await handler.handle(_req())
        assert fakes["reply"].correlation_ids == ["c1"]

    async def test_fault_logs_full_error_report_at_error(self, caplog: pytest.LogCaptureFixture) -> None:
        # I-1: the fault log must carry the ErrorReport calfkit shipped (error_type,
        # message), not just the origin, and at ERROR.
        handle = _FakeHandle(fault=NodeFaultError("billing.quota_exceeded", message="boom"))
        handler, _client, _fakes = _make(handle=handle)
        with caplog.at_level("ERROR"):
            await handler.handle(_req())
        faults = [r for r in caplog.records if "faulted" in r.message and r.levelname == "ERROR"]
        assert len(faults) == 1
        assert "error_type=billing.quota_exceeded" in faults[0].message
        assert "message=boom" in faults[0].message


# ---------------------------------------------------------------------------
# Root-cause surfacing: a fault_group carries its child faults in report.causes;
# the bridge walks them so the operator log and the Discord notice name the
# underlying exceptions (e.g. "4 x WebFetchError 403"), not just "agent X errored".
# ---------------------------------------------------------------------------


def _leaf_exception_fault(
    exc_type: str, message: str, *, origin: str = "web_fetch", attrs: dict[str, Any] | None = None
) -> ErrorReport:
    """A ``calf.exception`` report as ``ErrorReport.from_exception`` would build.

    Mirrors the shape the tools host synthesizes when a tool raises (e.g. a
    WebFetchError): ``error_type=calf.exception`` with the harvested ``exception``
    slot carrying the class name + sanitized attrs.
    """
    return ErrorReport.build_safe(
        error_type=FaultTypes.EXCEPTION,
        message=message,
        origin_node_id=origin,
        exception=ExceptionInfo(type=exc_type, attrs=attrs or {}),
    )


def _fault_group(target: str, causes: list[ErrorReport]) -> NodeFaultError:
    """A ``calf.fault_group`` wrapping child faults (a fan-out batch failure)."""
    return NodeFaultError(
        ErrorReport.build_safe(
            error_type=FaultTypes.FAULT_GROUP,
            message=f"fan-out batch closed with {len(causes)} unhandled fault(s)",
            origin_node_id=target,
            causes=causes,
        )
    )


class TestFaultRootCauses:
    """The bridge surfaces underlying root-cause exceptions from a fault — both in
    the operator log (full detail per leaf) and in the user-facing Discord notice
    (enough context to understand the failure). A fault_group carries its child
    faults in ``report.causes``; ``report.walk()`` yields them. Without this the
    operator/user sees only "fan-out batch closed with N fault(s)" / "agent X hit
    an error" — the actual 403s (or whatever) are invisible without a cross-host
    log dig.
    """

    async def test_fault_group_logs_each_root_cause_at_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """Each leaf exception in a fault_group is logged at ERROR with its origin,
        type, message, and attrs — full forensics without a cross-host log dig."""
        exc = _fault_group(
            "scribe",
            [
                _leaf_exception_fault(
                    "WebFetchError",
                    "Failed to fetch https://x.test/: 403 Forbidden",
                    attrs={"status_code": 403},
                ),
                _leaf_exception_fault(
                    "WebFetchError",
                    "Failed to fetch https://y.test/: 403 Forbidden",
                    attrs={"status_code": 403},
                ),
            ],
        )
        handler, _client, _fakes = _make(handle=_FakeHandle(fault=exc))
        with caplog.at_level("ERROR"):
            await handler.handle(_req())
        cause_logs = [r for r in caplog.records if "root cause" in r.message and r.levelname == "ERROR"]
        assert len(cause_logs) == 2  # one per leaf, not just the group summary
        for record in cause_logs:
            assert "WebFetchError" in record.message
            assert "403 Forbidden" in record.message
            assert "web_fetch" in record.message  # the origin node, not just the agent

    async def test_fault_group_notice_names_the_root_cause_exceptions(self) -> None:
        """The Discord notice names the underlying exception type(s) and message(s),
        not just the origin agent — so a user can tell 'fetches 403'd' from 'agent crashed'."""
        exc = _fault_group(
            "scribe",
            [
                _leaf_exception_fault("WebFetchError", "Failed to fetch https://x.test/: 403 Forbidden"),
                _leaf_exception_fault("WebFetchError", "Failed to fetch https://y.test/: 403 Forbidden"),
            ],
        )
        handler, _client, fakes = _make(handle=_FakeHandle(fault=exc))
        await handler.handle(_req())
        notice = fakes["reply"].notices[0]
        assert "scribe" in notice  # still names the agent
        assert "WebFetchError" in notice  # ...AND the root cause exception type
        assert "403 Forbidden" in notice  # ...AND enough message to understand it
        assert "x.test" in notice and "y.test" in notice  # both root causes, not just the first

    async def test_single_exception_fault_notice_surfaces_the_exception(self) -> None:
        """A top-level ``calf.exception`` (no group) also surfaces its exception
        type/message in the notice — the assistant's ``UnexpectedModelBehavior`` case."""
        report = ErrorReport.build_safe(
            error_type=FaultTypes.EXCEPTION,
            message="Exceeded maximum retries (1) for output validation",
            origin_node_id="scribe",
            exception=ExceptionInfo(type="UnexpectedModelBehavior", attrs={}),
        )
        handler, _client, fakes = _make(handle=_FakeHandle(fault=NodeFaultError(report)))
        await handler.handle(_req())
        notice = fakes["reply"].notices[0]
        assert "UnexpectedModelBehavior" in notice
        assert "Exceeded maximum retries" in notice

    async def test_minted_fault_with_no_exception_includes_report_message(self) -> None:
        """A framework/minted fault with no exception slot (e.g. ``billing.quota_exceeded``)
        has no leaf to surface — the notice stays the honest generic form but includes
        the report's ``message`` if it adds context the user can act on."""
        exc = NodeFaultError("billing.quota_exceeded", message="monthly quota exhausted")
        handler, _client, fakes = _make(handle=_FakeHandle(fault=exc))
        await handler.handle(_req())
        notice = fakes["reply"].notices[0]
        assert "hit an error" in notice  # the honest generic anchor
        assert "monthly quota exhausted" in notice  # the report message adds actionable context

    async def test_nested_fault_group_surfaces_leaf_causes(self) -> None:
        """A fault_group can nest another fault_group (a fan-out inside a fan-out);
        the walk recurses so the notice names the actual leaves, not the inner
        group's summary line."""
        leaf = _leaf_exception_fault("WebFetchError", "Failed to fetch https://x.test/: 403 Forbidden")
        inner_group = ErrorReport.build_safe(
            error_type=FaultTypes.FAULT_GROUP,
            message="fan-out batch closed with 1 unhandled fault(s)",
            origin_node_id="scribe",
            causes=[leaf],
        )
        exc = _fault_group("scribe", [inner_group])
        handler, _client, fakes = _make(handle=_FakeHandle(fault=exc))
        await handler.handle(_req())
        notice = fakes["reply"].notices[0]
        assert "WebFetchError" in notice and "403 Forbidden" in notice

    async def test_minted_fault_without_message_stays_generic(self) -> None:
        """A minted typed fault with an empty ``message`` has nothing to add —
        the notice is the honest generic form with no dangling detail clause."""
        handler, _client, fakes = _make(handle=_FakeHandle(fault=NodeFaultError("billing.quota_exceeded")))
        await handler.handle(_req())
        notice = fakes["reply"].notices[0]
        assert "hit an error" in notice
        assert notice.rstrip().endswith("Please try again.")

    async def test_many_leaves_are_bounded_under_discord_limit(self) -> None:
        """A pathological fault_group (many causes with long messages) must not blow
        past Discord's 2000-char notice limit — show as many root causes as fit and
        point to the logs for the rest."""
        causes = [
            _leaf_exception_fault(
                "WebFetchError",
                f"Failed to fetch https://reddit.com/r/LocalLLaMA/comments/xxxxx/long_path_{i}/: 403 Forbidden",
            )
            for i in range(30)
        ]
        exc = _fault_group("scribe", causes)
        handler, _client, fakes = _make(handle=_FakeHandle(fault=exc))
        await handler.handle(_req())
        notice = fakes["reply"].notices[0]
        assert len(notice) <= 2000
        assert "more" in notice.lower()  # names the elision honestly

    async def test_cause_chain_surfaces_only_outermost_not_cause_links(self) -> None:
        """A ``calf.exception`` whose ``causes`` hold a ``__cause__`` chain link
        surfaces ONLY the outermost failure — the chain is operator-log detail, not
        a separate user-facing failure (else one fetch wrapping httpx counts as two).

        Pins the deliberate narrowing of ``walk()`` documented on
        ``_root_cause_failures``: the framework's ``walk()`` yields both the
        outermost report and each ``__cause__`` link; this function yields only the
        outermost per failure path.
        """
        inner = ErrorReport.build_safe(
            error_type=FaultTypes.EXCEPTION,
            message="[Errno 61] Connection refused",
            exception=ExceptionInfo(type="ConnectError", attrs={}),
        )
        outer = ErrorReport.build_safe(
            error_type=FaultTypes.EXCEPTION,
            message="Failed to fetch https://x.test/: 403 Forbidden",
            origin_node_id="web_fetch",
            exception=ExceptionInfo(type="WebFetchError", attrs={"status_code": 403}),
            causes=[inner],  # the __cause__ chain — must NOT be surfaced as a separate failure
        )
        exc = _fault_group("scribe", [outer])
        handler, _client, fakes = _make(handle=_FakeHandle(fault=exc))
        await handler.handle(_req())
        notice = fakes["reply"].notices[0]
        assert "WebFetchError" in notice  # the outermost failure surfaced
        assert "403 Forbidden" in notice
        assert "ConnectError" not in notice  # the __cause__ chain link is NOT surfaced

    async def test_mixed_group_surfaces_typed_fault_children_too(self) -> None:
        """A fault_group mixing exception-bearing children and minted typed-fault
        children surfaces BOTH — the minted child's ``error_type`` stands in for
        the exception type (it has no harvested ``exception`` slot). Without this,
        a mixed group would silently drop its typed-fault children from the notice.
        """
        exception_child = _leaf_exception_fault(
            "WebFetchError", "Failed to fetch https://x.test/: 403 Forbidden"
        )
        minted_child = ErrorReport.build_safe(
            error_type="billing.quota_exceeded",
            message="monthly quota exhausted",
            origin_node_id="scribe",
        )
        exc = _fault_group("scribe", [exception_child, minted_child])
        handler, _client, fakes = _make(handle=_FakeHandle(fault=exc))
        await handler.handle(_req())
        notice = fakes["reply"].notices[0]
        assert "WebFetchError" in notice  # the exception child
        assert "403 Forbidden" in notice
        assert "billing.quota_exceeded" in notice  # the minted child — not silently dropped
        assert "monthly quota exhausted" in notice


class TestA2ATurnRegistration:
    async def test_registers_root_agent_and_human_subject_before_projection(self) -> None:
        steps = (_consult("t1", "conan", "q"), _consult_reply("t1", "conan", "answer"))
        handler, _client, fakes = _make(handle=_FakeHandle(steps=steps, result=_result("done", "scribe")))
        req = _req(content="!scribe plan the launch")
        await handler.handle(req)
        assert fakes["a2a"].begun == [("c1", "scribe", "!scribe plan the launch")]


class TestTerminalSeals:
    """The trace is sealed from the stream's terminal (ADR-0025).

    ``_FakeHandle.stream()`` yields the terminal as its last event, exactly as
    calfkit's does — which is the whole point: the outcome is available inside
    the drain, before the ``finally`` calls ``finish``.
    """

    async def test_a_completed_run_seals_the_trace_as_not_faulted(self) -> None:
        handler, _client, fakes = _make()
        await handler.handle(_req(mentions=("scribe",)))
        assert fakes["trace"].seals == [("c1", False)]
        # And the seal lands BEFORE finish — finish must find it already sealed.
        assert fakes["trace"].finished == ["c1"]

    async def test_a_faulted_run_seals_the_trace_as_faulted(self) -> None:
        fault = NodeFaultError(ErrorReport(error_type="calf.test.fault", origin_node_id="scribe"))
        handler, _client, fakes = _make(handle=_FakeHandle(fault=fault))
        await handler.handle(_req(mentions=("scribe",)))
        assert fakes["trace"].seals == [("c1", True)]
        # The notice still rides the native-reply path — the trace seal does NOT
        # replace it, because the trace's webhook is exactly what may be broken.
        assert len(fakes["reply"].notices) == 1

    async def test_a_seal_that_raises_never_faults_the_turn(self) -> None:
        # The drain's contract: the render path can never cost the user the
        # already-computed reply.
        class _RaisingSeal(_FakeTrace):
            async def seal(self, correlation_id: str, *, faulted: bool) -> None:
                raise RuntimeError("boom")

        handler, _client, fakes = _make(trace=_RaisingSeal())
        await handler.handle(_req(mentions=("scribe",)))
        assert len(fakes["reply"].replies) == 1


class TestConsultOutcomesReachTheTrace:
    """The redesign's stated reason for existing: a consult's OUTCOME must reach
    the human's thread, not just its request.

    Round-1 review: neither `A2AReject` nor `A2AFailed` appeared anywhere in this
    file, so the two branches carrying that fix were untested at the seam.
    """

    async def test_a_rejected_consult_resolves_the_row_with_its_reason(self) -> None:
        # The caller refused to dispatch (peer offline / self / a cycle). The
        # reason is the DISPATCHER's own note about why the call never left — not
        # part of any exchange — so ADR-0020 permits surfacing it.
        steps = (_consult("t1", "conan", "q"), _consult_outcome("t1", "conan is offline", "denied"))
        handler, _client, fakes = _make(handle=_FakeHandle(steps=steps, result=_result("done", "scribe")))
        await handler.handle(_req())
        assert fakes["trace"].consult_results == [("t1", "denied", "conan is offline")]

    async def test_a_faulted_consult_resolves_the_row_without_the_peer_s_words(self) -> None:
        # The peer engaged and then faulted. Its text is exchange content, so the
        # row must resolve WITHOUT it — the audit thread is where that lives.
        steps = (_consult("t1", "conan", "q"), _consult_outcome("t1", "conan exploded: SECRET", "failed"))
        handler, _client, fakes = _make(handle=_FakeHandle(steps=steps, result=_result("done", "scribe")))
        await handler.handle(_req())
        assert fakes["trace"].consult_results == [("t1", "failed", "")]

    async def test_a_successful_consult_never_forwards_the_peer_s_reply(self) -> None:
        # THE privacy invariant (ADR-0020), asserted at the point that enforces
        # it. It previously held only by accident — the `ok` render happens to
        # ignore `note` — so a mutation forwarding the reply passed every test.
        steps = (_consult("t1", "conan", "q"), _consult_reply("t1", "conan", "the SECRET is 42"))
        handler, _client, fakes = _make(handle=_FakeHandle(steps=steps, result=_result("done", "scribe")))
        await handler.handle(_req())
        assert fakes["trace"].consult_results == [("t1", "ok", "")]
        assert all("SECRET" not in note for _k, _s, note in fakes["trace"].consult_results)


class TestNestedConsultIsAnnounced:
    """A nested consult (a consulted agent consulting a peer) is announced by a
    resolving row in the CALLER's trace inside the audit thread, so the peer's
    work never appears unannounced (ADR-0026). The standalone prompt message is
    suppressed — the row is the single signal — while the peer's reply is kept.
    """

    @staticmethod
    def _nested_run() -> tuple[Any, ...]:
        # scribe→conan (top-level, acting), then conan→dot (nested), both replied.
        return (
            _consult("t1", "conan", "q"),
            _consult("t2", "dot", "review the auth changes", caller="conan"),
            _consult_reply("t2", "dot", "sub-answer", caller="conan"),
            _consult_reply("t1", "conan", "answer"),
        )

    async def test_a_nested_request_becomes_a_row_not_a_projected_message(self) -> None:
        handler, _client, fakes = _make(handle=_FakeHandle(steps=self._nested_run(), result=_result("done", "scribe")))
        await handler.handle(_req())
        # the nested conan→dot request is announced as a row...
        assert [(r.caller, r.peer) for r in fakes["a2a"].consult_rows] == [("conan", "dot")]
        # ...and never projected as a standalone [conan] prompt message; only the
        # top-level scribe→conan request is a projected message.
        projected_requests = [p for p in fakes["a2a"].projected if hasattr(p, "message")]
        assert [p.caller for p in projected_requests] == ["scribe"]

    async def test_a_nested_reply_resolves_the_row_and_still_posts_the_answer(self) -> None:
        handler, _client, fakes = _make(handle=_FakeHandle(steps=self._nested_run(), result=_result("done", "scribe")))
        await handler.handle(_req())
        # the row resolves from the nested reply...
        assert [(r.caller, r.peer) for r in fakes["a2a"].consult_row_results] == [("conan", "dot")]
        # ...and the peer's answer is STILL projected for the audit (kept, not suppressed).
        projected_replies = [(p.caller, p.peer) for p in fakes["a2a"].projected if hasattr(p, "text")]
        assert ("conan", "dot") in projected_replies

    async def test_a_nested_consult_never_touches_the_humans_thread(self) -> None:
        handler, _client, fakes = _make(handle=_FakeHandle(steps=self._nested_run(), result=_result("done", "scribe")))
        await handler.handle(_req())
        # only the top-level (acting) consult earns a human-thread row + resolution.
        assert [(key, peer, persona) for key, peer, _url, persona in fakes["trace"].consults] == [
            ("t1", "conan", "scribe")
        ]
        assert fakes["trace"].consult_results == [("t1", "ok", "")]

    async def test_the_acting_agents_consult_never_uses_the_nested_row_path(self) -> None:
        steps = (_consult("t1", "conan", "q"), _consult_reply("t1", "conan", "answer"))
        handler, _client, fakes = _make(handle=_FakeHandle(steps=steps, result=_result("done", "scribe")))
        await handler.handle(_req())
        assert fakes["a2a"].consult_rows == []
        assert fakes["a2a"].consult_row_results == []
        # the top-level consult still projects its message + opens the human row.
        assert fakes["trace"].consults == [("t1", "conan", fakes["a2a"].url, "scribe")]

    async def test_a_nested_reject_resolves_the_row_and_keeps_the_system_note(self) -> None:
        # A nested consult the caller refused to dispatch takes branch 3 too: the
        # system note still projects (kept for the audit) AND the row resolves,
        # and neither touches the human trace.
        steps = (
            _consult("t1", "conan", "q"),  # scribe→conan (acting)
            _consult("t2", "dot", "z", caller="conan"),  # conan→dot nested request
            _consult_outcome("t2", "dot is offline", "denied", caller="conan"),  # nested reject
            _consult_reply("t1", "conan", "answer"),
        )
        handler, _client, fakes = _make(handle=_FakeHandle(steps=steps, result=_result("done", "scribe")))
        await handler.handle(_req())
        assert [(r.caller, r.peer) for r in fakes["a2a"].consult_row_results] == [("conan", "dot")]
        # the reject's system note is still projected for the audit record...
        projected_notes = [(p.caller, p.peer) for p in fakes["a2a"].projected if hasattr(p, "text")]
        assert ("conan", "dot") in projected_notes
        # ...and only the top-level consult ever reached the human trace.
        assert [key for key, *_rest in fakes["trace"].consults] == ["t1"]


class TestConsultedAgentsWorkIsRouted:
    """A consulted agent's own steps reach the A2A thread instead of nowhere.

    The drain's `else` arm used to drop them at DEBUG while its own comment
    called them "the audit channel's business" — a promise nothing kept. That is
    invisible until a consult fails: an agent can make 23 tool calls, fault, and
    leave only a shrug in its thread (ADR-0026).
    """

    async def test_a_consulted_agents_step_goes_to_the_a2a_projector(self) -> None:
        steps = (_consult("t1", "conan", "q"), _agent_msg("thinking…", emitter="conan"))
        handler, _client, fakes = _make(handle=_FakeHandle(steps=steps, result=_result("done", "scribe")))
        await handler.handle(_req())
        assert [s.emitter for s in fakes["a2a"].steps] == ["conan"]
        # …and NOT into the human's thread, which is what is_acting guards.
        assert fakes["trace"].steps == []

    async def test_the_acting_agents_own_steps_never_go_to_the_projector(self) -> None:
        handler, _client, fakes = _make(
            handle=_FakeHandle(steps=(_agent_msg("mine", emitter="scribe"),), result=_result("done", "scribe"))
        )
        await handler.handle(_req())
        assert fakes["a2a"].steps == []
        assert [s.emitter for s in fakes["trace"].steps] == ["scribe"]

    async def test_the_human_trace_is_told_where_the_mention_landed(self) -> None:
        # The handler owns this derivation now; the renderer is told the answer.
        handler, _client, fakes = _make(
            handle=_FakeHandle(steps=(_agent_msg("hi"),), result=_result("done", "scribe"))
        )
        await handler.handle(_req())
        dest = fakes["trace"].dests[0]
        assert (dest.channel_id, dest.thread_id) == (10, None)

    async def test_both_surfaces_seal_from_the_stream_terminal(self) -> None:
        # A consulted agent's trace ends with the same run. Without this the
        # projector's finish would seal it defensively as "interrupted".
        handler, _client, fakes = _make()
        await handler.handle(_req(mentions=("scribe",)))
        assert fakes["trace"].seals == [("c1", False)]
        assert fakes["a2a"].seals == [("c1", False)]

    async def test_a_faulted_run_seals_both_surfaces_faulted(self) -> None:
        fault = NodeFaultError(ErrorReport(error_type="calf.test.fault", origin_node_id="conan"))
        handler, _client, fakes = _make(handle=_FakeHandle(fault=fault))
        await handler.handle(_req())
        assert fakes["a2a"].seals == [("c1", True)]

    async def test_the_projector_is_retired_after_the_fault_notes_not_before(self) -> None:
        # THE ordering trap. `_deliver`'s fault path writes the "did not reply"
        # notes and needs the turn's thread STILL mapped — retiring first evicts
        # the mapping, so each note creates a second thread and is orphaned in it.
        # Pins the sequence, not just the calls.
        fault = NodeFaultError(ErrorReport(error_type="calf.test.fault", origin_node_id="conan"))
        handler, _client, fakes = _make(
            handle=_FakeHandle(steps=(_consult("t1", "conan", "q"),), fault=fault)
        )
        await handler.handle(_req())
        assert fakes["a2a"].calls == ["project", "fault", "finish"]

    async def test_the_projector_is_retired_on_a_clean_run_too(self) -> None:
        # Unconditional: `_threads` was never evicted at all before, which now
        # strands a live writer task per turn.
        handler, _client, fakes = _make()
        await handler.handle(_req())
        assert fakes["a2a"].calls[-1] == "finish"

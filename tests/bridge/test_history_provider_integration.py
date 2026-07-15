"""Provider-integration sanity tests for the conversation-history feature.

Gated on real API keys. These tests do not run in normal CI; they exist
as a tripwire so a future pydantic-ai release that removes
:func:`_clean_message_history` (the auto-merge of adjacent same-role
messages, see ``calfkit/_vendor/pydantic_ai/_agent_graph.py:1386``)
surfaces as a clear test failure rather than a silent production bug.

Why this matters: the design intentionally does NOT merge consecutive
:class:`ModelRequest`s inside :func:`build_message_history` (see
``bridge/history.py`` module + function docstrings). The correctness of the
"boundary between history and staged user_prompt is well-formed" invariant
rests entirely on pydantic-ai's auto-merge running at one of the two call
sites (``_agent_graph.py`` lines 213 and 526).

If `_clean_message_history` is ever removed / changed:
    - Anthropic rejects with HTTP 400 ("messages must alternate")
    - OpenAI silently tolerates (no error, but the assistant may produce
      lower-quality output)

Either way, our unit tests in `test_history.py` would still pass (they're
unit-scoped). This file is the live alarm.

The tests build a canonical history via :func:`build_message_history` whose
tail ENDS in multiple consecutive ``ModelRequest``s — a shape that
pydantic-ai's auto-merge must consolidate before sending to the provider —
and feed it to a real ``pydantic_ai.Agent.run``. If the auto-merge silently
disappears in an upstream release, the Anthropic test fails with a
``400 messages must alternate`` (or equivalent); the OpenAI test still passes
but the regression is half-detected (model output quality may degrade).

Run manually::

    OPENAI_API_KEY=... ANTHROPIC_API_KEY=... \
        uv run pytest tests/bridge/test_history_provider_integration.py -v -m integration
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from calfkit._vendor.pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from calfcord.bridge.history import (
    HistoryRecord,
    _trim_history_to_budget,
    build_message_history,
)

pytestmark = pytest.mark.integration


def _record(content: str, author: str, *, is_agent: bool = False) -> HistoryRecord:
    return HistoryRecord(
        message_id=1,
        created_at=datetime.now(UTC),
        content=content,
        author_display_name=author,
        is_agent=is_agent,
    )


def _history_with_adjacent_users() -> list:
    """Build a canonical history whose tail is multiple consecutive
    ``ModelRequest`` entries (the case pydantic-ai's auto-merge must
    consolidate before the provider mapper sees it).
    """
    records = [
        _record("can you help me?", "ryan"),
        _record("sure, what do you need?", "Scribe", is_agent=True),
        _record("planning a meeting", "ryan"),
        _record("tuesday afternoon", "ryan"),  # consecutive user
        _record("also need help with the prep", "ryan"),  # consecutive user
    ]
    return build_message_history(records)


def _has_anthropic() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def _has_openai() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


def _assert_history_has_adjacent_user_requests(history: list) -> None:
    """Sanity check: confirm the built history actually has the
    adjacent-same-role shape this test is supposed to exercise.
    Without this, a subtle change to ``build_message_history`` could make
    the test vacuously pass.
    """
    request_runs = 0
    max_run = 0
    for m in history:
        if isinstance(m, ModelRequest):
            request_runs += 1
            max_run = max(max_run, request_runs)
        else:
            request_runs = 0
    assert max_run >= 2, (
        "test invariant: built history must contain >=2 consecutive "
        "ModelRequest entries to exercise pydantic-ai's auto-merge path; "
        f"got max consecutive-request run of {max_run}"
    )


@pytest.mark.skipif(not _has_anthropic(), reason="ANTHROPIC_API_KEY not set")
async def test_pydantic_ai_anthropic_auto_merges_adjacent_user_messages() -> None:
    """Construct a pydantic-ai Agent with an Anthropic model and send
    it a ``message_history`` whose tail has multiple consecutive
    ``ModelRequest`` entries. If pydantic-ai's ``_clean_message_history``
    auto-merge still runs, the call succeeds. If it's removed, Anthropic
    rejects with a 400 ``messages must alternate`` error.

    This is the real alarm — exercising the actual production code path.
    """
    from calfkit._vendor.pydantic_ai import Agent
    from calfkit._vendor.pydantic_ai.models.anthropic import AnthropicModel

    history = _history_with_adjacent_users()
    _assert_history_has_adjacent_user_requests(history)

    model = AnthropicModel("claude-haiku-4-5")
    agent: Agent = Agent(
        model=model,
        system_prompt="You are Scribe. Be concise.",
    )

    # If pydantic-ai stops auto-merging, the underlying anthropic API
    # call raises a ``BadRequestError`` (``messages must alternate``).
    # We assert no exception — the run succeeds and we get text out.
    result = await agent.run(
        "what time should we meet?",
        message_history=history,
    )
    assert result.output, "Anthropic returned an empty response"


def _rec(message_id: int, content: str, author: str, *, is_agent: bool = False) -> HistoryRecord:
    return HistoryRecord(
        message_id=message_id,
        created_at=datetime.now(UTC),
        content=content,
        author_display_name=author,
        is_agent=is_agent,
    )


def _replay_delta() -> list:
    """A persisted turn delta: the agent's tool call and its return."""
    return [
        ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="get_weather", args={"city": "Tokyo"}, tool_call_id="t1"
                )
            ],
            name="Scribe",
        ),
        ModelRequest(
            parts=[ToolReturnPart(tool_name="get_weather", content="18C", tool_call_id="t1")]
        ),
    ]


def _history_with_replay_delta() -> list:
    """A canonical history whose middle holds a spliced replay delta — the
    production shape the byte trim actually has to cut through."""
    records = [
        _rec(1, "hey, got a second?", "ryan"),
        _rec(2, "sure, what's up?", "Scribe", is_agent=True),
        _rec(3, "what's the weather in Tokyo?", "ryan"),
        _rec(4, "It's 18C.", "Scribe", is_agent=True),
        _rec(5, "thanks!", "ryan"),
    ]
    return build_message_history(records, hydration={4: _replay_delta()})


def _agent(provider: str):
    """A pydantic-ai Agent on ``provider``'s cheapest current model."""
    from calfkit._vendor.pydantic_ai import Agent

    if provider == "anthropic":
        from calfkit._vendor.pydantic_ai.models.anthropic import AnthropicModel

        model = AnthropicModel("claude-haiku-4-5")
    else:
        from calfkit._vendor.pydantic_ai.models.openai import OpenAIChatModel

        model = OpenAIChatModel("gpt-4o-mini")
    return Agent(model=model, system_prompt="You are Scribe. Be concise.")


_PROVIDERS = [
    pytest.param(
        "anthropic",
        marks=pytest.mark.skipif(not _has_anthropic(), reason="ANTHROPIC_API_KEY not set"),
    ),
    pytest.param(
        "openai",
        marks=pytest.mark.skipif(not _has_openai(), reason="OPENAI_API_KEY not set"),
    ),
]


@pytest.mark.parametrize("provider", _PROVIDERS)
async def test_provider_accepts_a_byte_trimmed_history(provider: str) -> None:
    """The positive half: a history trimmed to a byte budget is still a shape
    Anthropic accepts.

    The budget drops the opening turns and strands the agent reply that follows
    them at the head; repair removes it, leaving a user turn with the replay
    delta intact behind it. Every unit test asserts this shape is *legal* by
    reading the provider's rules — this asserts the provider agrees.
    """
    history = _history_with_replay_delta()
    # Drop the first two turns: the cut lands on a ModelResponse, so head repair
    # must run for the result to be sendable at all.
    budget = len(ModelMessagesTypeAdapter.dump_json(history[1:]))
    trimmed = _trim_history_to_budget(history, max_json_bytes=budget)

    # Test invariant: this must actually exercise a trim + a repair, or it
    # vacuously proves nothing.
    assert 0 < len(trimmed) < len(history), "expected a real trim"
    assert isinstance(trimmed[0], ModelRequest)
    assert any(isinstance(p, UserPromptPart) for p in trimmed[0].parts)

    result = await _agent(provider).run("what did I just ask about?", message_history=trimmed)
    assert result.output, f"{provider} returned an empty response"


@pytest.mark.parametrize("provider", _PROVIDERS)
async def test_provider_rejects_an_orphaned_tool_return_head(provider: str) -> None:
    """The negative control — the reason head repair exists at all.

    Cut a history mid-replay-delta and DON'T repair: the head is a
    ``ModelRequest`` of tool returns whose ``tool_call`` was just dropped. The
    whole design rests on the claim that a provider rejects this. If a provider
    ever accepts it, ``_drop_until_user_request``'s tool-return rule is
    unnecessary complexity and this test is the alarm that says so.

    Observed from OpenAI (gpt-4o-mini), naming the head explicitly::

        400 invalid_request_error, param 'messages.[0].role':
        "messages with role 'tool' must be a response to a preceeding
         message with 'tool_calls'."
    """
    from calfkit._vendor.pydantic_ai.exceptions import ModelHTTPError
    history = _history_with_replay_delta()
    # [MR(q1), MResp(a1), MR(q2), MResp(ToolCall), MR(ToolReturn), MResp(18C), MR(thanks)]
    # Cut at 4: the ToolReturn survives, its ToolCall at 3 does not.
    orphaned = list(history[4:])

    # Test invariant: the head really is an orphaned tool return.
    assert isinstance(orphaned[0], ModelRequest)
    assert any(isinstance(p, ToolReturnPart) for p in orphaned[0].parts)
    assert not any(isinstance(p, UserPromptPart) for p in orphaned[0].parts)

    with pytest.raises(ModelHTTPError) as exc:
        await _agent(provider).run("and tomorrow?", message_history=orphaned)

    assert exc.value.status_code == 400
    # Pin the REASON, not just any 400 — a bad model name is also a 400, and
    # would let this pass while proving nothing. Both providers name the tool
    # pairing in the body ("tool_calls" / "tool_use_id").
    assert "tool" in str(exc.value.body).lower()


@pytest.mark.skipif(not _has_openai(), reason="OPENAI_API_KEY not set")
async def test_pydantic_ai_openai_handles_adjacent_user_messages() -> None:
    """OpenAI tolerates adjacent same-role messages at the API layer,
    but pydantic-ai's auto-merge still runs uniformly. This test pins
    the success path against OpenAI; a failure here is unlikely to be
    pydantic-ai (OpenAI accepts the raw shape anyway) — more likely
    a misconfiguration."""
    from calfkit._vendor.pydantic_ai import Agent
    from calfkit._vendor.pydantic_ai.models.openai import OpenAIChatModel

    history = _history_with_adjacent_users()
    _assert_history_has_adjacent_user_requests(history)

    model = OpenAIChatModel("gpt-4o-mini")
    agent: Agent = Agent(
        model=model,
        system_prompt="You are Scribe. Be concise.",
    )

    result = await agent.run(
        "what time should we meet?",
        message_history=history,
    )
    assert result.output, "OpenAI returned an empty response"

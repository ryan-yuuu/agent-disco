"""Build the built-in :class:`AgentDefinition` for the routing agent.

The router definition is constructed in code rather than parsed from an
``agents/*.md`` file because:

* It is project infrastructure, not a user-customizable persona. A
  user-editable ``agents/_router.md`` would invite operators to remove
  it or tweak its agent_id/display_name, both of which would break the
  registry's "exactly one router" invariant.
* Its ``agent_id``/``display_name`` are reserved (``_router`` /
  ``Router``) and not user-overridable. Embedding the constants here
  pins the contract. The Discord slash command is always
  ``/<agent_id>``, so reserving the agent_id implicitly reserves the
  slash.

The router's prompt and its tunable runtime config (``provider``, ``model``,
``thinking_effort``, ``history_turns``) are not embedded here: they live in the
bundled :file:`router.md` (front matter for config, body for the prompt) and
are read via :func:`calfkit_organization.router.prompt.load_router_md`. A field
omitted from the front matter falls through to the ``_DEFAULT_*`` constant
below, so the file can stay minimal.

The bridge registry (:class:`AgentRegistry`) appends this definition
automatically in :meth:`AgentRegistry.from_agents_dir` so user-defined
agents and the router co-exist in a single roster.
"""

from __future__ import annotations

from calfkit_organization.agents.definition import AgentDefinition, Provider, ThinkingEffort
from calfkit_organization.router.prompt import load_router_md

ROUTER_AGENT_ID = "_router"
"""Canonical agent id for the singleton built-in router. Imported by
other modules that need to defensively self-filter (the fan-out
consumer skips this id when republishing) or to look the router up in
the registry (:meth:`AgentRegistry.router`)."""

_ROUTER_DISPLAY_NAME = "Router"
"""Reserved display_name for the singleton router. User-defined agents
that try to use the same name fail at :meth:`AgentRegistry._index`
duplicate-detection time — the operator must rename their agent."""

_ROUTER_DESCRIPTION = "Internal routing agent (not user-invocable)"

_ROUTER_PUBLISH_TOPIC = "routing.decisions"
"""Kafka topic the router publishes :class:`RoutingDecision`s to. The
fan-out consumer subscribes here. Hardcoded rather than env-driven
because the project's topology contract is fixed; an operator changing
this topic would also need to coordinate the fan-out consumer's
subscription, which is also constant."""

_DEFAULT_PROVIDER: Provider = "openai"
_DEFAULT_MODEL = "gpt-5-nano"
_DEFAULT_THINKING_EFFORT: ThinkingEffort = "none"
"""Fallback when ``router.md`` omits a field. The router runs a tightly-bounded
structured-output task (pick one agent from a small roster, weigh conversation
continuity against topic match), so the defaults are tuned for fast/cheap
classification — the router fires on every ambient message in every channel."""
_DEFAULT_HISTORY_TURNS = 10
"""Default number of recent channel messages projected into the router's
``message_history`` when ``router.md`` omits ``history_turns``. Smaller than the
assistant default (30) because:

* The router runs on every ambient message in every channel — the
  per-invocation cost adds up, and the routing decision is bounded
  by ``RoutingDecision``'s small structured output, so the marginal
  value of more context is modest.
* The router doesn't need to *carry* the conversation; it only
  needs enough context to recognize follow-ups vs. fresh topics.
* The router never appears as ``self`` in the projection (it has
  no Discord persona / no prior turns), so every record contributes
  one ``ModelRequest`` — no merging benefit from a larger window."""


def build_router_definition() -> AgentDefinition:
    """Construct the singleton :class:`AgentDefinition` for the router.

    Configuration source: the bundled :file:`router.md` (see
    :func:`calfkit_organization.router.prompt.load_router_md`). Its YAML front
    matter supplies ``provider`` / ``model`` / ``thinking_effort`` /
    ``history_turns``; any field omitted from the front matter falls through to
    the ``_DEFAULT_*`` constant above. The Markdown body supplies the system
    prompt. Operators override the whole file (config + prompt) by pointing
    ``CALFKIT_ROUTER_PROMPT_PATH`` at a mounted file.

    The returned definition satisfies the router invariants enforced by
    :class:`AgentDefinition`'s validators: ``role="router"``, empty
    ``tools``, non-empty ``publish_topic``, and the strict
    name/display_name format constraints.

    Returns:
        A frozen :class:`AgentDefinition` ready to be appended to the
        registry alongside user-defined agents. ``source_path`` is ``None``:
        ``router.md`` is bundled infrastructure, not a user-managed persona
        file, so it is intentionally not exposed to the ``/thinking-effort``
        frontmatter rewriter (which only edits ``agents/*.md``).
    """
    config, system_prompt = load_router_md()

    # We let pydantic raise on an invalid Provider / ThinkingEffort tag
    # rather than second-guessing here — same surface area as user-
    # defined ``.md`` parsing.
    provider_raw = config.provider or _DEFAULT_PROVIDER
    model = config.model or _DEFAULT_MODEL
    thinking_effort_raw = config.thinking_effort or _DEFAULT_THINKING_EFFORT
    history_turns = (
        config.history_turns if config.history_turns is not None else _DEFAULT_HISTORY_TURNS
    )

    return AgentDefinition(
        agent_id=ROUTER_AGENT_ID,
        display_name=_ROUTER_DISPLAY_NAME,
        description=_ROUTER_DESCRIPTION,
        avatar_url=None,
        provider=provider_raw,  # type: ignore[arg-type]
        model=model,
        tools=(),
        thinking_effort=thinking_effort_raw,  # type: ignore[arg-type]
        role="router",
        publish_topic=_ROUTER_PUBLISH_TOPIC,
        history_turns=history_turns,
        system_prompt=system_prompt,
        source_path=None,
    )

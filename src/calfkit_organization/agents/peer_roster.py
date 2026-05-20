"""Build the per-invocation peer roster injected as ``temp_instructions``.

A2A-enabled agents (those whose ``.md`` declares ``tools: [private_chat]``)
need to know which peers exist before they can sensibly call the
``private_chat`` tool. We surface that roster by injecting it as
``temp_instructions`` on each invocation rather than baking it into the
system prompt at agent build time — that way a new agent added to the
registry becomes visible to existing peers on the very next invocation,
no agent restarts needed.

Operates on a :class:`PhonebookEntry` list rather than an
:class:`AgentRegistry` directly so the same helper works in both the
bridge (which converts its registry to a phonebook) and in any
decoupled deployment that received the phonebook via ``deps``.

Cost model: the roster is only attached when the target agent has the
``private_chat`` tool, so agents that don't use A2A pay zero token cost
for the feature.
"""

from __future__ import annotations

from collections.abc import Sequence

from calfkit_organization.agents.phonebook import PhonebookEntry

_PRIVATE_CHAT_TOOL_NAME = "private_chat"


def build_temp_instructions(
    phonebook: Sequence[PhonebookEntry],
    target_agent_id: str,
) -> str | None:
    """Return the ``temp_instructions`` to inject for an invocation of ``target_agent_id``.

    Returns ``None`` when no instructions are needed, i.e. when the
    target agent does not declare ``private_chat`` in its tools or when
    the phonebook has no peers to advertise. Callers can pass the result
    straight through to :meth:`calfkit.client.Client.invoke_node`
    (``temp_instructions=None`` is a no-op there).

    Args:
        phonebook: The full set of known agents (the target included).
            Either freshly built from the registry by the bridge or
            received as ``deps["phonebook"]`` by a downstream deployment.
        target_agent_id: The agent the invocation will be delivered to.
            Excluded from the roster — an agent never needs to be told
            it can talk to itself, and ``private_chat`` rejects
            self-targets anyway.

    Returns:
        A short multi-line instructions block listing each peer's id
        and description, or ``None`` if the target doesn't use A2A
        tools or has no peers to call.
    """
    target = next((e for e in phonebook if e.agent_id == target_agent_id), None)
    if target is None:
        return None
    if _PRIVATE_CHAT_TOOL_NAME not in target.tools:
        return None
    peers = [e for e in phonebook if e.agent_id != target_agent_id]
    if not peers:
        return None
    lines = [f"- {e.agent_id}: {e.description}" for e in peers]
    return (
        "Peer agents you can reach via the private_chat tool:\n"
        + "\n".join(lines)
    )

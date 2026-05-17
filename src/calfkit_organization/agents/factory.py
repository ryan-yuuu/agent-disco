"""Construct a runnable calfkit :class:`Worker` from an :class:`AgentDefinition`.

**Currently a stub.** :meth:`AgentFactory.build` raises
:class:`NotImplementedError`. Everything around it — parsing, validation,
state loading/bootstrapping, persona-sender construction, calfkit client
connection, and SIGINT/SIGTERM shutdown — is real, so when this method is
implemented the runner's flow is complete end-to-end.
"""

from __future__ import annotations

from calfkit.client import Client
from calfkit.worker import Worker

from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.agents.state import AgentRuntimeState, AgentStateStore
from calfkit_organization.discord.persona import DiscordPersonaSender


class AgentFactory:
    """Builds a :class:`Worker` from a parsed :class:`AgentDefinition`.

    Stub: :meth:`build` raises :class:`NotImplementedError` until the
    LLM-backed agent runtime lands. The runner constructs the factory,
    the state store, and the persona sender; once :meth:`build` is real,
    the runner's flow is complete.
    """

    def __init__(
        self,
        persona_sender: DiscordPersonaSender,
        calfkit_client: Client,
    ) -> None:
        self._persona_sender = persona_sender
        self._calfkit_client = calfkit_client

    def build(
        self,
        definition: AgentDefinition,
        state: AgentRuntimeState,
        store: AgentStateStore,
    ) -> Worker:
        """Build a :class:`Worker` configured for ``definition`` and ``state``.

        Once implemented, the worker will subscribe to
        ``discord.channel.{cid}`` for each channel in ``state.channels`` and
        use ``store`` for in-runtime mutation (e.g. when the agent joins a
        new thread via ``/agent`` slash, ``store.add_channel`` is called).

        Raises:
            NotImplementedError: always — see module docstring.
        """
        raise NotImplementedError(
            f"AgentFactory.build is not yet implemented (agent {definition.agent_id!r}). "
            "Until the LLM-backed runtime lands, only agents with a hand-coded "
            "`agents/<name>.py` runtime (currently just `echo`) can be run; see "
            "agents/echo.py for the pattern."
        )

"""Discord slash command registration and dispatch.

Owns the ``app_commands.CommandTree`` for the bot. Two kinds of commands
can be registered:

* ``/thinking-effort agent:<name> effort:<tier>`` — the operator slash
  registered by :meth:`register_thinking_effort`. Writes the persisted
  effort tier into ``state/agents/<name>.json`` via a per-agent
  :class:`AgentStateStore` cached at registration time. Authorization is
  restricted to ``DiscordSettings.owner_user_id``.
* Per-agent invocation slashes (``/echo``, ``/scribe``, …) built by
  :meth:`register_all`. Currently disabled in the bridge in favour of
  ``@<agent_id>`` text-prefix invocation, but the builder is preserved
  here for future use. When enabled, dispatch defers the interaction,
  posts a followup as the reply anchor, normalizes to a
  :class:`WireMessage`, and hands off to :class:`BridgeRoundTrip` —
  whose calfkit reply dispatcher resolves the response within the 15
  minute followup window.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import cast, get_args

import discord
from discord import app_commands

from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.agents.state import AgentStateStore, ThinkingEffort
from calfkit_organization.bridge.normalizer import SlashNormalizer
from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.bridge.roundtrip import BridgeRoundTrip

logger = logging.getLogger(__name__)

_THINKING_EFFORT_VALUES: tuple[ThinkingEffort, ...] = get_args(ThinkingEffort)
_THINKING_EFFORT_COMMAND_NAME = "thinking-effort"


class SlashCommandManager:
    """Builds, syncs, and dispatches per-agent slash commands."""

    def __init__(
        self,
        client: discord.Client,
        registry: AgentRegistry,
        roundtrip: BridgeRoundTrip,
        slash_normalizer: SlashNormalizer,
        *,
        state_dir: Path | None = None,
        owner_user_id: int | None = None,
    ) -> None:
        self._client = client
        self._registry = registry
        self._roundtrip = roundtrip
        self._normalizer = slash_normalizer
        self._state_dir = state_dir
        self._owner_user_id = owner_user_id
        self._tree = app_commands.CommandTree(client)
        # One AgentStateStore per agent. Built eagerly when state_dir is
        # known so the orphan-tmp sweep runs once at boot — not on every
        # slash invocation — and concurrent invocations share the per-agent
        # asyncio.Lock. Empty dict when state_dir is None (test/legacy).
        self._stores: dict[str, AgentStateStore] = (
            {
                spec.agent_id: AgentStateStore(state_dir / f"{spec.agent_id}.json")
                for spec in registry.all()
            }
            if state_dir is not None
            else {}
        )

    def register_all(self) -> None:
        """Add one :class:`app_commands.Command` per agent. Call once at startup."""
        for spec in self._registry.all():
            self._tree.add_command(self._build_command(spec))

    def register_thinking_effort(self) -> None:
        """Register ``/thinking-effort`` on the command tree.

        Requires ``state_dir`` to have been supplied at construction; raises
        :class:`RuntimeError` otherwise so misconfiguration fails at boot
        rather than at first invocation.

        The per-agent :class:`AgentStateStore` cache is built at
        construction time (see ``__init__``).

        Ambient-message limitation: the persisted tier only applies when
        the bridge can identify the target agent (slash invocations and
        ``@<agent_id>`` mentions). Plain channel messages fall back to the
        agent's constructor defaults — see :mod:`bridge.thinking`.
        """
        if self._state_dir is None:
            raise RuntimeError(
                "SlashCommandManager.register_thinking_effort requires state_dir at construction"
            )
        self._tree.add_command(self._build_thinking_effort_command())

    def _build_thinking_effort_command(self) -> app_commands.Command:
        agent_choices = [
            app_commands.Choice(name=spec.agent_id, value=spec.agent_id)
            for spec in self._registry.all()
        ]
        effort_choices = [
            app_commands.Choice(name=value, value=value) for value in _THINKING_EFFORT_VALUES
        ]

        @app_commands.describe(
            agent="Which agent to configure",
            effort="Thinking-effort tier; applies to the next message (mentions/slashes only — ambient messages use the agent's default)",
        )
        @app_commands.choices(agent=agent_choices, effort=effort_choices)
        async def callback(
            interaction: discord.Interaction,
            agent: app_commands.Choice[str],
            effort: app_commands.Choice[str],
        ) -> None:
            await self._on_thinking_effort(interaction, agent.value, effort.value)

        return app_commands.Command(
            name=_THINKING_EFFORT_COMMAND_NAME,
            description="Configure an agent's per-call thinking effort tier",
            callback=callback,
        )

    async def _on_thinking_effort(
        self,
        interaction: discord.Interaction,
        agent_id: str,
        effort: str,
    ) -> None:
        assert self._state_dir is not None  # enforced by register_thinking_effort
        logger.info(
            "thinking-effort slash invoked agent=%s effort=%s user_id=%s",
            agent_id,
            effort,
            interaction.user.id,
        )

        async def reply(text: str) -> None:
            await interaction.response.send_message(text, ephemeral=True)

        if self._owner_user_id is not None and interaction.user.id != self._owner_user_id:
            await reply("Only the configured owner can change agent effort.")
            return

        spec = self._registry.by_id(agent_id)
        if spec is None:
            known = ", ".join(f"`{s.agent_id}`" for s in self._registry.all()) or "<none>"
            await reply(f"No agent named `{agent_id}`. Known: {known}.")
            return

        if effort not in _THINKING_EFFORT_VALUES:
            choices = ", ".join(f"`{v}`" for v in _THINKING_EFFORT_VALUES)
            await reply(f"Unknown effort `{effort}`. Choose one of: {choices}")
            return

        # Stores are pre-built at __init__. A miss here means post-boot
        # drift from the registry; should be impossible — treat defensively.
        store = self._stores.get(agent_id)
        if store is None:
            logger.error(
                "no AgentStateStore for known agent_id=%s (registered=%s)",
                agent_id,
                sorted(self._stores),
            )
            await reply(
                f"Internal error: no state store for `{agent_id}` "
                f"(interaction_id={interaction.id}). Check bridge logs."
            )
            return
        try:
            await store.set_thinking_effort(cast(ThinkingEffort, effort))
        except FileNotFoundError:
            await reply(
                f"Agent `{agent_id}` has no state file yet "
                "(start the agent at least once to bootstrap it)."
            )
            return
        except Exception:
            logger.exception(
                "failed to persist thinking_effort agent=%s interaction_id=%s",
                agent_id,
                interaction.id,
            )
            await reply(
                f"Sorry — failed to persist the new effort tier "
                f"(interaction_id={interaction.id}). Check the bridge logs."
            )
            return

        if effort == "none":
            await reply(
                f"Saved `effort=none` for `{agent_id}`. Thinking is disabled; "
                "applies to the next slash or @-mention message."
            )
        else:
            await reply(
                f"Saved `effort={effort}` for `{agent_id}`. "
                "Applies to the next slash or @-mention message."
            )

    async def sync(self, guild_id: int | None) -> None:
        """Push the command tree to Discord. Idempotent; safe to call on every boot."""
        guild = discord.Object(id=guild_id) if guild_id is not None else None
        if guild is not None:
            self._tree.copy_global_to(guild=guild)
        synced = await self._tree.sync(guild=guild)
        logger.info("synced %d slash command(s) guild=%s", len(synced), guild_id)

    def _build_command(self, spec: AgentDefinition) -> app_commands.Command:
        # A factory function gives each callback its own scope so ``spec``
        # closes over its own loop iteration, not the last one.
        def _make_callback(spec: AgentDefinition):
            @app_commands.describe(message="What you want this agent to do")
            async def callback(interaction: discord.Interaction, message: str) -> None:
                await self._on_invocation(interaction, spec, message)

            return callback

        return app_commands.Command(
            name=spec.slash.lstrip("/"),
            description=spec.description[:100],
            callback=_make_callback(spec),
        )

    async def _on_invocation(
        self,
        interaction: discord.Interaction,
        spec: AgentDefinition,
        message: str,
    ) -> None:
        logger.info(
            "slash invocation agent=%s interaction_id=%s user_id=%s",
            spec.agent_id,
            interaction.id,
            interaction.user.id,
        )
        try:
            await interaction.response.defer(ephemeral=False)
            followup = await interaction.followup.send(
                f"**/{spec.slash[1:]}** {message}",
                wait=True,
            )
            assert followup is not None, "followup.send with wait=True must return a Message"

            wire = self._normalizer.normalize(
                interaction=interaction,
                slash_target=spec,
                message_arg=message,
                followup_message_id=followup.id,
            )
            await self._roundtrip.handle(wire)
            logger.info(
                "slash dispatched agent=%s interaction_id=%s followup_id=%s event_id=%s",
                spec.agent_id,
                interaction.id,
                followup.id,
                wire.event_id,
            )
        except Exception:
            logger.exception(
                "slash invocation failed agent=%s interaction_id=%s",
                spec.agent_id,
                interaction.id,
            )
            try:
                await interaction.followup.send(
                    "Sorry — something went wrong handling that slash. Please try again.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass

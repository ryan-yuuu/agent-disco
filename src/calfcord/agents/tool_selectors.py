"""Calfcord-specific runtime selectors for the function-tool capability plane."""

from __future__ import annotations

from dataclasses import dataclass

from calfkit.models.capability import EnumerableCapabilityView, resolve_all_capabilities
from calfkit.models.tool_dispatch import SelectorResult

from calfcord.tools.discord import DISCORD_TOOL_NAMES


@dataclass(frozen=True)
class DiscoverDefaultTools:
    """Discover live function tools except security-sensitive opt-in tools.

    Discord history is qualitatively different from the generic workspace/web
    surface: it exposes every channel visible to the bot.  Omitted ``tools:``
    therefore retains calfcord's convenient runtime discovery while excluding
    the bridge-hosted Discord tools. Operators grant those names explicitly.
    """

    excluded: frozenset[str] = DISCORD_TOOL_NAMES

    def resolve_tools(self, view: EnumerableCapabilityView) -> SelectorResult:
        result = resolve_all_capabilities(view, node_kind="tool")
        return SelectorResult(
            bindings=tuple(binding for binding in result.bindings if binding.name not in self.excluded),
            missing_targets=result.missing_targets,
            missing_tools=result.missing_tools,
            invalid_targets=result.invalid_targets,
            wrong_kind_targets=result.wrong_kind_targets,
        )

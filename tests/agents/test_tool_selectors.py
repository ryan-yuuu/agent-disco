"""Security policy for calfcord's default function-tool discovery."""

from __future__ import annotations

from unittest.mock import patch

from calfkit.models.tool_dispatch import SelectorResult

from calfcord.agents.tool_selectors import DiscoverDefaultTools


def test_default_discovery_filters_discord_tools() -> None:
    allowed = type("Binding", (), {"name": "read_file"})()
    discord_list = type("Binding", (), {"name": "discord_list_channels"})()
    discord_read = type("Binding", (), {"name": "discord_read_messages"})()
    resolved = SelectorResult(bindings=(allowed, discord_list, discord_read))  # type: ignore[arg-type]

    with patch(
        "calfcord.agents.tool_selectors.resolve_all_capabilities",
        return_value=resolved,
    ):
        result = DiscoverDefaultTools().resolve_tools(object())  # type: ignore[arg-type]

    assert [binding.name for binding in result.bindings] == ["read_file"]

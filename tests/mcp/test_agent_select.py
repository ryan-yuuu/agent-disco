"""Unit tests for :mod:`calfcord.mcp.agent_select` — the agent-path toolbox selector.

Agents resolve their tri-state ``mcp:`` frontmatter field through calfkit's public
:class:`~calfkit.nodes.toolbox.Toolboxes` selector (0.13): discover mode binds
every live MCP server on the network; named mode pins per-server
:class:`~calfkit.mcp.MCPToolbox` entries — identity-only handles constructible
with just the server name, so distributed agent hosts never need ``mcp.json``.

The contract pins below are deliberate: calfcord's secrets boundary and
degradation policy ride on this upstream behavior, so drift in any of it must
fail loudly here rather than silently in production.

Pinned:

* tri-state mapping — ``True`` → discover, ``False``/``()`` → no selector,
  a non-empty tuple → a named ``Toolboxes`` (this part is calfcord semantics);
* named-entry merge — bare form subsumes explicit; dedup; servers sorted;
* protocol compliance — calfkit's ``split_tool_declarations`` must classify the
  selector as deferred (that classification is what makes ``Worker``
  auto-register the capability view);
* view resolution — discover binds every live toolbox, ``include`` scopes to
  named tools, and a missing server degrades structurally (never a raise).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from calfkit import Toolboxes
from calfkit.mcp import MCPToolbox
from calfkit.models.capability import CapabilityRecord, CapabilityToolDef
from calfkit.models.tool_dispatch import ToolSelector, split_tool_declarations

from calfcord.mcp.agent_select import toolbox_selector


def _record(server: str = "gmail", tools: tuple[str, ...] = ("search", "send")) -> CapabilityRecord:
    # The capability view keys by the dict entry (the server name), so the record
    # itself carries no id; ``node_kind`` must be "toolbox" for the selector's
    # ``expected_kind`` over-pull guard to admit it, and the liveness stamp fields
    # are required by the model (staleness filtering lives in the real
    # ControlPlaneView, not the plain-dict view these unit tests pass).
    now = datetime.now(tz=UTC)
    return CapabilityRecord(
        started_at=now,
        last_heartbeat_at=now,
        heartbeat_interval=5.0,
        node_kind="toolbox",
        dispatch_topic=f"mcp_server.{server}",
        tools=[CapabilityToolDef(name=t, description=None, parameters_json_schema={"type": "object"}) for t in tools],
        content_updated_at=now,
    )


class _EnumerableView(dict):
    """A plain-dict capability view that also satisfies ``EnumerableCapabilityView``
    (adds the ``snapshot()`` bulk-enumeration hook discover mode needs)."""

    def snapshot(self) -> dict[str, CapabilityRecord]:
        return dict(self)


class TestTristateMapping:
    def test_true_yields_discover(self) -> None:
        """``mcp: true`` (the default) → a single discover-mode ``Toolboxes`` so the
        agent binds every live MCP server at runtime, not a build-time snapshot."""
        assert toolbox_selector(True) == Toolboxes(discover=True)

    def test_false_yields_none(self) -> None:
        """``mcp: false`` opts out entirely — no toolbox selector is created."""
        assert toolbox_selector(False) is None

    def test_empty_tuple_yields_none(self) -> None:
        """``()`` (the normalizer's canonical off-state) also yields no selector —
        defense-in-depth for a ``model_construct`` that skips field validation."""
        assert toolbox_selector(()) is None

    def test_named_entries_become_one_selector(self) -> None:
        """A non-empty grant list → exactly one named ``Toolboxes`` (never discover)."""
        selector = toolbox_selector(("gmail",))
        assert selector == Toolboxes(MCPToolbox("gmail"))
        assert selector.discover is False


class TestNamedEntryMerge:
    def test_explicit_tools_merge_per_server_sorted_deduped(self) -> None:
        selector = toolbox_selector(("gmail/send", "gmail/search", "gmail/send"))
        assert selector == Toolboxes(MCPToolbox("gmail", include=("search", "send")))

    def test_bare_server_selects_all(self) -> None:
        assert toolbox_selector(("gmail",)) == Toolboxes(MCPToolbox("gmail", include=None))

    def test_bare_subsumes_explicit(self) -> None:
        """``gmail`` + ``gmail/search`` collapses to the wildcard — the old
        schema-build dedup semantics."""
        assert toolbox_selector(("gmail/search", "gmail")) == Toolboxes(MCPToolbox("gmail", include=None))

    def test_servers_sorted_for_determinism(self) -> None:
        selector = toolbox_selector(("zeta", "alpha"))
        assert [entry.name for entry in selector.entries] == ["alpha", "zeta"]

    def test_malformed_grant_raises_naming_entry(self) -> None:
        with pytest.raises(ValueError, match="a/b/c"):
            toolbox_selector(("a/b/c",))

    def test_bare_name_is_server_grant(self) -> None:
        """In the canonical field, a bare name is a server wildcard grant."""
        assert toolbox_selector(("shell",)) == Toolboxes(MCPToolbox("shell"))


class TestProtocolCompliance:
    def test_discover_selector_satisfies_tool_selector_protocol(self) -> None:
        assert isinstance(toolbox_selector(True), ToolSelector)

    def test_named_selector_satisfies_tool_selector_protocol(self) -> None:
        assert isinstance(toolbox_selector(("gmail",)), ToolSelector)

    @pytest.mark.parametrize("mcp", [True, ("gmail", "docs/search")])
    def test_split_tool_declarations_classifies_as_deferred_selector(self, mcp: bool | tuple[str, ...]) -> None:
        """``Worker._maybe_register_capability_view`` keys off the agent's
        ``_tool_selectors`` — which exist only if calfkit's partitioner routes the
        selector to the deferred side. This is the wire that makes per-turn
        discovery work end-to-end."""
        bindings, selectors = split_tool_declarations([toolbox_selector(mcp)])
        assert bindings == []
        assert len(selectors) == 1


class TestResolveTools:
    def test_discover_resolves_all_advertised_tools(self) -> None:
        """Discover mode binds every live toolbox on the network — the behavior the
        default ``mcp: true`` rides on."""
        view = _EnumerableView({"gmail": _record("gmail", ("search",)), "docs": _record("docs", ("read",))})
        result = toolbox_selector(True).resolve_tools(view)
        assert sorted(b.name for b in result.bindings) == ["docs__read", "gmail__search"]
        assert not result.unresolved

    def test_named_include_scopes_to_named_tools(self) -> None:
        """``include`` pins the BARE server-side name (the trust boundary); the
        resolved binding keeps the namespaced form."""
        result = toolbox_selector(("gmail/search",)).resolve_tools({"gmail": _record()})
        assert [b.name for b in result.bindings] == ["gmail__search"]

    def test_named_missing_server_degrades_not_raises(self) -> None:
        """calfcord policy: agents boot and run when their MCP servers are down; the
        turn degrades (``missing_targets`` / ``unresolved``) rather than raising."""
        result = toolbox_selector(("gmail",)).resolve_tools({})
        assert result.missing_targets == ("gmail",)
        assert result.bindings == ()

    def test_named_missing_included_tool_reported(self) -> None:
        result = toolbox_selector(("gmail/nope",)).resolve_tools({"gmail": _record()})
        assert result.missing_tools == ("nope",)

"""Builtin tools shipped with calfcord.

Each submodule defines one or more ``@agent_tool``-decorated async functions
that adapt an upstream library (``openhands-tools`` or ``smolagents``) into a
calfkit :class:`ToolNodeDef`. The tools live in :data:`TOOL_REGISTRY` and are
selectable by name from any agent's ``.md`` frontmatter ``tools:`` list.

These tools operate on the host where ``calfkit-tools`` runs — there is no
per-agent sandbox. See :func:`~calfkit_organization.tools.builtin.workspace.get_workspace_root`
and the project README's security model for what that implies.
"""

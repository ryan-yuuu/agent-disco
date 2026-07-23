"""Default function-tool discovery no longer needs a calfcord filter layer.

Omitted ``tools:`` maps to calfkit's native ``Tools(discover=True)``. Bridge-
hosted Discord reads are ordinary live tool nodes on that plane, so there is no
security filter selector to unit-test here anymore. This module stays only to
document that the old ``DiscoverDefaultTools`` seam is gone.
"""

from __future__ import annotations

import importlib

import pytest


def test_discover_default_tools_filter_module_is_removed() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("calfcord.agents.tool_selectors")

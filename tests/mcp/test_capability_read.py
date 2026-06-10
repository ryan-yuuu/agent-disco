"""Tests for the CLI's best-effort capability-view snapshot.

Only the degrade contract is unit-testable without a broker: any failure
(unreachable bootstrap, missing topic, replay timeout) must yield ``{}``
quickly — the editor then falls back to mcp.json server rows. The happy
path is covered by the broker-gated integration suite conventions.
"""

from __future__ import annotations

import time

from calfcord.mcp.capability_read import snapshot_capability_tools


def test_unreachable_broker_degrades_to_empty_quickly() -> None:
    started = time.monotonic()
    # An unroutable port on localhost: connection refused, not a hang.
    result = snapshot_capability_tools("localhost:1", timeout=0.5)
    elapsed = time.monotonic() - started
    assert result == {}
    assert elapsed < 10  # bounded — never the editor hanging on a dead broker

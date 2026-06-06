"""Gated REAL-broker test for the bridge's MANAGED (embedded) Worker lifecycle.

After the Tier-3 migration the bridge folds onto calfkit 0.6.0's managed Worker
as the single deliberate *embedded* variant: ``worker.start()`` /
``worker.stop()`` (signals opted OUT — the bridge owns SIGINT/SIGTERM for its
Discord foreground), with the blind-spot topics declared into the client's
startup ensurer from a pre-broker-start ``on_startup`` hook
(:func:`calfcord.bridge.gateway._register_blind_spot_topics`) and the raw
state-consumer subscriber registered BEFORE the start (register-before-serve).

This verifies the half of the bridge boot that does NOT need Discord, against a
broker that does NOT auto-create topics (Tansu): the embedded ``worker.start()``

* does NOT hang (calfkit's pre-start pass provisions the declared node +
  blind-spot topics, so neither the node groups nor the raw state-consumer group
  block on a missing topic), and
* leaves the bridge's blind-spot topics (``agent.state``, ``bridge.discovery``)
  actually created on the broker.

The Discord gateway co-run, the on_ready discovery ping, the heartbeat refresher
and the ordered shutdown are exercised offline in
``tests/bridge/test_gateway_worker_lifecycle.py`` (they cannot connect to Discord
in-sandbox); this fills the broker-side gap.

Gated behind ``CALF_TEST_KAFKA`` (mirrors calfkit's integration lane). Point it
at native Tansu to run for real::

    CALF_TEST_KAFKA=1 CALF_TEST_KAFKA_BOOTSTRAP=localhost:9092 uv run pytest \\
        tests/integration/test_bridge_embedded_lifecycle.py
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from typing import Any

import pytest
from calfkit import Client, ProvisioningConfig, Worker
from calfkit.nodes import consumer
from calfkit.provisioning import TopicProvisioner

from calfcord.bridge.gateway import _register_blind_spot_topics
from calfcord.control_plane.topics import AGENT_STATE_TOPIC, BRIDGE_DISCOVERY_TOPIC

pytestmark = pytest.mark.skipif(
    not os.getenv("CALF_TEST_KAFKA"),
    reason="set CALF_TEST_KAFKA=1 (+ CALF_TEST_KAFKA_BOOTSTRAP) against a NO-auto-create broker (e.g. Tansu)",
)

BOOTSTRAP = os.getenv("CALF_TEST_KAFKA_BOOTSTRAP", "localhost:9092")
_START_OK_TIMEOUT = 15.0  # a healthy start returns in well under a second
_STOP_OK_TIMEOUT = 15.0  # drain + disconnect


async def test_embedded_worker_start_provisions_blind_spots_and_does_not_hang() -> None:
    """The bridge's embedded ``worker.start()`` — with the blind-spot ``on_startup``
    hook and a raw ``agent.state`` subscriber registered before start — completes
    cleanly on a no-auto-create broker and creates the blind-spot topics.

    Stands in for the bridge boot's broker half: a representative consumer node
    (the outbox/synthesized/steps nodes are structurally identical for
    provisioning purposes) plus the raw state-consumer subscriber the bridge
    registers before serving. No Discord involvement.
    """
    inbox = f"itest.bridge-outbox-{uuid.uuid4().hex[:8]}"
    saw_state = asyncio.Event()

    @consumer(subscribe_topics=inbox)
    async def outbox_like(_message: Any) -> None:  # pragma: no cover - not exercised
        pass

    client = Client.connect(
        BOOTSTRAP,
        reply_topic=f"itest-discord-outbox-{uuid.uuid4().hex[:8]}",
        provisioning=ProvisioningConfig(enabled=True),
    )
    worker = Worker(client, [outbox_like])

    # Declare the bridge's blind-spot topics into the startup ensurer (the
    # production wiring) so the managed pre-start pass creates them.
    _register_blind_spot_topics(worker, client)

    # Raw state-consumer subscriber registered BEFORE start (register-before-serve).
    # Its group must join without hanging — which requires agent.state to have been
    # provisioned by the blind-spot hook above.
    async def _on_state(_message: Any) -> None:
        saw_state.set()

    client._connection.subscriber(
        AGENT_STATE_TOPIC,
        group_id=f"itest-bridge-state-{uuid.uuid4().hex[:8]}",
    )(_on_state)

    try:
        # Embedded surface: start() must NOT hang despite the no-auto-create
        # broker, because the declared node + blind-spot topics are provisioned
        # in calfkit's single pre-start pass before any group joins.
        await asyncio.wait_for(worker.start(), timeout=_START_OK_TIMEOUT)
        assert client.broker.running

        # The blind-spot topics now exist: a fresh provisioner reports them
        # "existing", not "created" — direct evidence the on_startup hook's
        # declaration was honoured in the pre-start pass.
        report = await TopicProvisioner.from_connection(
            server_urls=BOOTSTRAP, config=ProvisioningConfig(enabled=True)
        ).provision(
            [AGENT_STATE_TOPIC, BRIDGE_DISCOVERY_TOPIC],
            framework_topics=set(),
        )
        assert AGENT_STATE_TOPIC in report.existing
        assert BRIDGE_DISCOVERY_TOPIC in report.existing
        assert AGENT_STATE_TOPIC not in report.created
        assert BRIDGE_DISCOVERY_TOPIC not in report.created
    finally:
        # The embedded shutdown surface: stop() drains + disconnects without hanging.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(worker.stop(), timeout=_STOP_OK_TIMEOUT)
        with contextlib.suppress(Exception):
            await client.close()

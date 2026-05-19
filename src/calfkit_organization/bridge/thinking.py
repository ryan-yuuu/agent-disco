"""Map operator-facing thinking effort tiers to provider-specific model settings.

The bridge attaches the result of :func:`build_model_settings` to every
:meth:`calfkit.client.Client.execute_node` call destined for a targeted
agent (slash invocations and ``@<agent_id>`` mentions). Calfkit's
``OverridesState.model_settings`` carries the dict over Kafka, and
pydantic_ai merges it over the agent's constructor defaults and the model
client's own defaults on every LLM call — so changes take effect on the
next message without restarting the agent process.

The Anthropic ``budget_tokens`` ramp anchors its ``low`` / ``medium`` /
``high`` values (4000 / 10000 / 31999) to the same budgets Claude Code's
``think`` / ``megathink`` / ``ultrathink`` keywords trigger; see the PR
plan for sources. OpenAI's ``reasoning_effort`` currently tops out at
``high``, so ``xhigh`` and ``max`` saturate there until the API exposes
finer-grained tiers. See the per-provider tables below for exact values.

Ambient-message limitation (v1)
-------------------------------
Effort overrides only apply when the bridge can identify the target agent
ahead of time. That's true for slash invocations and ``@<agent_id>``
mentions (both produce ``WireMessage.slash_target``). Plain ambient
channel messages flow without ``model_settings``, so the agent's
constructor defaults take over for those.
"""

from __future__ import annotations

import logging
from typing import Any

from calfkit_organization.agents.definition import Provider
from calfkit_organization.agents.state import ThinkingEffort

logger = logging.getLogger(__name__)

_ANTHROPIC_BUDGET_TOKENS: dict[ThinkingEffort, int] = {
    "low": 4000,
    "medium": 10000,
    "high": 31999,
    "xhigh": 48000,
    "max": 63999,
}

_OPENAI_REASONING_EFFORT: dict[ThinkingEffort, str] = {
    "low": "minimal",
    "medium": "low",
    "high": "medium",
    "xhigh": "high",
    "max": "high",
}


def build_model_settings(
    provider: Provider,
    effort: ThinkingEffort | None,
) -> dict[str, Any] | None:
    """Build a calfkit per-call ``model_settings`` dict for the given tier.

    Returns:
        - ``None`` when ``effort is None`` (no operator override configured;
          calfkit treats this as "use whatever the agent constructor set").
        - ``{}`` when ``effort == "none"`` (operator explicitly asked for
          no extra overrides; calfkit treats an empty dict the same as
          ``None`` on the merge path).
        - A provider-specific dict for all other tiers.

    Defensive: a typed-input effort that doesn't appear in the per-provider
    mapping table (e.g. a future tier name that slipped past pydantic via a
    hand-edited state file) is logged and degrades to ``{}`` rather than
    raising. Unknown ``provider`` values raise :class:`ValueError` — those
    are a config-level bug that should fail fast.
    """
    if effort is None:
        return None
    if effort == "none":
        return {}

    if provider == "anthropic":
        budget = _ANTHROPIC_BUDGET_TOKENS.get(effort)
        if budget is None:
            logger.warning(
                "unknown anthropic effort tier %r; degrading to no override",
                effort,
            )
            return {}
        return {"anthropic_thinking": {"type": "enabled", "budget_tokens": budget}}

    if provider == "openai":
        value = _OPENAI_REASONING_EFFORT.get(effort)
        if value is None:
            logger.warning(
                "unknown openai effort tier %r; degrading to no override",
                effort,
            )
            return {}
        return {"openai_reasoning_effort": value}

    raise ValueError(f"unknown provider {provider!r}; expected 'anthropic' or 'openai'")

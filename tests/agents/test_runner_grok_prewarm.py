"""Tests for the runner's best-effort xAI Grok catalog prewarm.

Prewarm loads the model catalog once at startup for xai / xai-grok agents. It
never hard-fails: a not-logged-in / tier-gated / keyless deployment falls back to
the pinned catalog, and the actionable "not logged in" error surfaces later from
the per-agent factory build instead.
"""

from __future__ import annotations

import pytest

from calfcord.agents.definition import AgentDefinition
from calfcord.agents.runner import _prewarm_grok_if_needed
from calfcord.providers.grok.credentials import GrokNotLoggedInError


def _def(provider: str) -> AgentDefinition:
    return AgentDefinition(name="grokbot", description="d", provider=provider, system_prompt="p")


@pytest.fixture
def spy_prewarm(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    calls: dict[str, object] = {"count": 0}

    async def fake_prewarm(bearer: str, **kw: object) -> None:
        calls["count"] = int(calls["count"]) + 1  # type: ignore[arg-type]
        calls["bearer"] = bearer
        calls["base_url"] = kw.get("base_url")

    monkeypatch.setattr("calfcord.providers.grok.prewarm_grok_models", fake_prewarm)
    return calls


class TestPrewarm:
    async def test_noop_without_grok_agents(self, spy_prewarm: dict[str, object]) -> None:
        await _prewarm_grok_if_needed([_def("anthropic"), _def("openai")])
        assert spy_prewarm["count"] == 0

    async def test_oauth_bearer_used_when_logged_in(
        self, spy_prewarm: dict[str, object], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_resolve(**_kw: object) -> str:
            return "bearer-xyz"

        monkeypatch.setattr("calfcord.providers.grok.credentials.resolve_access_token", fake_resolve)
        await _prewarm_grok_if_needed([_def("xai-grok")])
        assert spy_prewarm["bearer"] == "bearer-xyz"

    async def test_not_logged_in_falls_back_without_raising(
        self, spy_prewarm: dict[str, object], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_resolve(**_kw: object) -> str:
            raise GrokNotLoggedInError()

        monkeypatch.setattr("calfcord.providers.grok.credentials.resolve_access_token", fake_resolve)
        # Must not raise — the factory build surfaces the login error per-agent.
        await _prewarm_grok_if_needed([_def("xai-grok")])
        assert spy_prewarm["bearer"] == ""

    async def test_api_key_used_for_xai_provider(
        self, spy_prewarm: dict[str, object], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XAI_API_KEY", "xai-secret")
        await _prewarm_grok_if_needed([_def("xai")])
        assert spy_prewarm["bearer"] == "xai-secret"

    async def test_prewarm_honors_valid_xai_base_url_override(
        self, spy_prewarm: dict[str, object], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XAI_API_KEY", "k")
        monkeypatch.setenv("XAI_BASE_URL", "https://staging.x.ai/v1")
        await _prewarm_grok_if_needed([_def("xai")])
        # Catalog is fetched from the same host-pinned endpoint the client uses.
        assert spy_prewarm["base_url"] == "https://staging.x.ai/v1"

    async def test_prewarm_ignores_foreign_base_url_override(
        self, spy_prewarm: dict[str, object], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XAI_API_KEY", "k")
        monkeypatch.setenv("XAI_BASE_URL", "https://attacker.example/v1")
        await _prewarm_grok_if_needed([_def("xai")])
        assert spy_prewarm["base_url"] == "https://api.x.ai/v1"  # rejected -> default

    async def test_transport_or_lock_error_degrades_without_crashing(
        self, spy_prewarm: dict[str, object], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A file-lock timeout (OSError) during refresh must not brick startup —
        # the docstring promises degradation to the pinned catalog on any failure.
        async def fake_resolve(**_kw: object) -> str:
            raise OSError("lock timeout")

        monkeypatch.setattr("calfcord.providers.grok.credentials.resolve_access_token", fake_resolve)
        await _prewarm_grok_if_needed([_def("xai-grok")])
        assert spy_prewarm["bearer"] == ""  # fell back to empty -> pinned catalog

"""Wizard wiring for the xai / xai-grok providers (network-free)."""

from __future__ import annotations

from pathlib import Path

import pytest

from calfcord.cli import _providers


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CALFCORD_AUTH_DIR", str(tmp_path / "auth"))
    monkeypatch.delenv("XAI_API_KEY", raising=False)


class TestMenu:
    def test_providers_menu_includes_both_xai_variants(self) -> None:
        values = {choice.value for choice in _providers.PROVIDERS}
        assert {"xai", "xai-grok"} <= values

    def test_api_key_provider_maps_to_xai_api_key(self) -> None:
        assert _providers._PROVIDER_KEY_VAR["xai"] == "XAI_API_KEY"

    def test_oauth_provider_has_no_key_var(self) -> None:
        # xai-grok authenticates via device-code OAuth, not a .env key.
        assert "xai-grok" not in _providers._PROVIDER_KEY_VAR


class TestListModels:
    def test_xai_grok_without_credentials_lists_fallback(self) -> None:
        # No creds -> empty bearer -> no network -> pinned fallback, default first.
        choices = _providers.list_models("xai-grok", api_key=None)
        ids = [c.value for c in choices]
        assert ids[0] == "grok-4.3"
        assert "grok-4.5" in ids


class TestRejectedKey:
    def test_xai_rejected_key_raises_model_auth_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A rejected XAI_API_KEY (xAI answers 401/403/400) must surface loudly,
        # like openai/anthropic — not be silently masked as a fallback list.
        from calfcord.providers.grok import models as grok_models

        async def fake_fetch(client: object, bearer: str, base_url: str) -> object:
            return grok_models._CatalogFetch(None, status=401, reason="HTTP 401")

        monkeypatch.setattr("calfcord.providers.grok.models._fetch_catalog", fake_fetch)
        with pytest.raises(_providers.ModelAuthError):
            _providers.list_models("xai", api_key="bad-key")

    def test_xai_grok_403_tier_gate_stays_quiet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # For the OAuth path a 403 is an expected tier-gate, not a bad key —
        # return the fallback list rather than raising.
        from calfcord.providers.grok import models as grok_models

        async def fake_fetch(client: object, bearer: str, base_url: str) -> object:
            return grok_models._CatalogFetch(None, status=403, reason="HTTP 403")

        monkeypatch.setattr("calfcord.providers.grok.models._fetch_catalog", fake_fetch)
        # bearer must be non-empty for the fetch to run; the OAuth path reads the
        # stored token, so seed one.
        from calfcord.providers.grok import token_store
        from calfcord.providers.grok.token_store import GrokCredentials

        token_store.save_credentials(
            GrokCredentials(access_token="tok", refresh_token="rt", token_endpoint="https://auth.x.ai/oauth2/token")
        )
        choices = _providers.list_models("xai-grok", api_key=None)
        assert choices[0].value == "grok-4.3"


class TestInlineLogin:
    def test_grok_login_never_aborts_on_oserror_in_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The wizard contract: an auth/lock hiccup during the "already logged in?"
        # probe must not tear down setup — fall through to a fresh login.
        async def boom(**_kw: object) -> object:
            raise OSError("lock timeout")

        async def fake_login(**_kw: object) -> dict[str, object]:
            return {"access_token": "at", "refresh_token": "rt", "token_endpoint": "https://auth.x.ai/oauth2/token"}

        monkeypatch.setattr("calfcord.providers.grok.credentials.resolve_credentials", boom)
        monkeypatch.setattr("calfcord.providers.grok.oauth.device_code_login", fake_login)
        _providers._grok_login()  # must not raise
        from calfcord.providers.grok import token_store

        assert token_store.load_credentials() is not None


class TestFallback:
    def test_fallback_models_cover_both_xai_providers(self) -> None:
        fallback = _providers.fallback_models()
        assert fallback["xai"] == fallback["xai-grok"]
        assert "grok-4.3" in fallback["xai"]

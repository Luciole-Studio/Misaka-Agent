"""Translated from pi's ``coding-agent/test/oauth-selector.test.ts`` (v1.1.0), plus the MISAKA account/subscription
label case."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from misaka.ai.auth.types import AuthCheck
from misaka.core.keybindings import KeybindingsManager
from misaka.modes.interactive.components.oauth_selector import (
    AuthSelectorProvider,
    OAuthSelectorComponent,
    formatAuthSelectorProviderType,
)
from misaka.modes.interactive.interactive_mode import InteractiveMode
from misaka.ui.tui import keybindings as tui_keybindings
from misaka.utils.ansi import stripAnsi


@pytest.fixture(autouse=True)
def _keybindings(monkeypatch):
    monkeypatch.setattr(tui_keybindings, "globalKeybindings", KeybindingsManager())


def _render(*providers: AuthSelectorProvider) -> str:
    selector = OAuthSelectorComponent("login", list(providers), lambda *_: None, lambda: None)
    return stripAnsi("\n".join(selector.render(120)))


def test_renders_an_option_without_compiled_auth_status_as_not_configured():
    output = _render(AuthSelectorProvider(id="google", name="Google", authType="api_key", status=None))

    assert "not configured" in output
    assert "✓ configured" not in output


def test_shows_oauth_auth_distinctly_in_the_api_key_selector():
    output = _render(
        AuthSelectorProvider(
            id="anthropic", name="Anthropic", authType="api_key", status=AuthCheck(type="oauth", source="OAuth")
        )
    )

    assert "subscription configured" in output


def test_shows_environment_api_key_auth_as_configured():
    output = _render(
        AuthSelectorProvider(
            id="openai", name="OpenAI", authType="api_key", status=AuthCheck(type="api_key", source="OPENAI_API_KEY")
        )
    )

    assert "✓ env: OPENAI_API_KEY" in output
    assert "not configured" not in output


def test_an_oauth_sign_in_without_a_subscription_is_an_account():
    assert formatAuthSelectorProviderType("oauth") == "subscription"
    assert formatAuthSelectorProviderType("oauth", True) == "subscription"
    assert formatAuthSelectorProviderType("oauth", False) == "account"
    assert formatAuthSelectorProviderType("api_key", False) == "API key"

    output = _render(
        AuthSelectorProvider(id="radius", name="Radius", authType="oauth", subscription=False),
        AuthSelectorProvider(id="openai", name="OpenAI", authType="api_key"),
    )
    assert "[account]" in output and "[API key]" in output


def test_projects_provider_owned_auth_options_without_provider_specific_filtering():
    async def login(*_args):
        return {}

    providers = [
        SimpleNamespace(
            id="anthropic",
            name="Anthropic",
            auth=SimpleNamespace(
                oauth=SimpleNamespace(name="Anthropic (Claude Pro/Max)", login=login),
                apiKey=SimpleNamespace(name="Anthropic API key", login=login),
            ),
        ),
        SimpleNamespace(
            id="google-vertex",
            name="Google Vertex AI",
            auth=SimpleNamespace(oauth=None, apiKey=SimpleNamespace(name="Google Cloud credentials")),
        ),
    ]
    fakeThis = SimpleNamespace(
        session=SimpleNamespace(
            modelRuntime=SimpleNamespace(
                getProviders=lambda: providers,
                getProviderAuthStatus=lambda _id: SimpleNamespace(configured=False, label=None, source=None),
                isUsingOAuth=lambda _id: False,
            )
        )
    )

    apiKeyOptions = InteractiveMode.getLoginProviderOptions(fakeThis, "api_key")  # type: ignore[arg-type]
    assert [(option.id, option.name, option.authType, option.method.name) for option in apiKeyOptions] == [
        ("anthropic", "Anthropic", "api_key", "Anthropic API key"),
        ("google-vertex", "Google Vertex AI", "api_key", "Google Cloud credentials"),
    ]
    oauthOptions = InteractiveMode.getLoginProviderOptions(fakeThis, "oauth")  # type: ignore[arg-type]
    assert [(option.id, option.name, option.authType) for option in oauthOptions] == [
        ("anthropic", "Anthropic", "oauth")
    ]


def test_shows_models_json_api_key_auth_as_configured():
    output = _render(
        AuthSelectorProvider(
            id="local-proxy",
            name="local-proxy",
            authType="api_key",
            status=AuthCheck(type="api_key", source="key in models.json"),
        )
    )

    assert "✓ key in models.json" in output


def test_shows_models_json_command_auth_as_configured():
    output = _render(
        AuthSelectorProvider(
            id="op-proxy",
            name="op-proxy",
            authType="api_key",
            status=AuthCheck(type="api_key", source="command in models.json"),
        )
    )

    assert "✓ command in models.json" in output

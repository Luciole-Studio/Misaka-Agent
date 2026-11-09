"""Translated from pi's ``packages/ai/test/env-api-keys.test.ts`` (v1.1.0)."""

from __future__ import annotations

import pytest

from misaka.ai.env_api_keys import findEnvKeys, getEnvApiKey


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    for name in (
        "COPILOT_GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "ZAI_CODING_CN_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_does_not_treat_generic_github_tokens_as_github_copilot_credentials(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "gh-token")
    monkeypatch.setenv("GITHUB_TOKEN", "github-token")

    assert findEnvKeys("github-copilot") is None
    assert getEnvApiKey("github-copilot") is None


def test_resolves_github_copilot_credentials_from_copilot_github_token(monkeypatch):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "copilot-token")
    monkeypatch.setenv("GH_TOKEN", "gh-token")
    monkeypatch.setenv("GITHUB_TOKEN", "github-token")

    assert findEnvKeys("github-copilot") == ["COPILOT_GITHUB_TOKEN"]
    assert getEnvApiKey("github-copilot") == "copilot-token"


def test_resolves_zai_china_coding_plan_credentials_from_zai_coding_cn_api_key(monkeypatch):
    monkeypatch.setenv("ZAI_CODING_CN_API_KEY", "zai-coding-cn-token")

    assert findEnvKeys("zai-coding-cn") == ["ZAI_CODING_CN_API_KEY"]
    assert getEnvApiKey("zai-coding-cn") == "zai-coding-cn-token"


def test_reports_anthropic_auth_token_but_preserves_oauth_token_api_key_lookup(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "auth-token")
    monkeypatch.setenv("ANTHROPIC_OAUTH_TOKEN", "oauth-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "api-key")

    assert findEnvKeys("anthropic") == ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_OAUTH_TOKEN", "ANTHROPIC_API_KEY"]
    assert getEnvApiKey("anthropic") == "oauth-token"


def test_does_not_return_anthropic_auth_token_as_an_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "auth-token")

    assert findEnvKeys("anthropic") == ["ANTHROPIC_AUTH_TOKEN"]
    assert getEnvApiKey("anthropic") is None


def test_preserves_anthropic_oauth_token_as_an_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_OAUTH_TOKEN", "oauth-token")

    assert findEnvKeys("anthropic") == ["ANTHROPIC_OAUTH_TOKEN"]
    assert getEnvApiKey("anthropic") == "oauth-token"


def test_falls_back_to_anthropic_api_key_for_api_key_lookup(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "api-key")

    assert getEnvApiKey("anthropic") == "api-key"

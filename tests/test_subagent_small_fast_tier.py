"""A prompt or agent hook that names no model runs on the provider's small-fast tier: the newest of its
family (``haiku``, ``mini``, ``flash``...) in the catalog, by the rule a sub-agent's ``haiku``
resolves with (R19). A pinned list chose claude-haiku-4-5 and gemini-3-flash-preview where newer
models were configured, and ranking any ``*-latest`` ID first chose old aliases (2026-10-28)."""
from types import SimpleNamespace

import pytest

from misaka.core.subagent.child import _small_fast_hook_model


@pytest.fixture(autouse=True)
def _no_overrides(monkeypatch):
    for name in ("ANTHROPIC_SMALL_FAST_MODEL", "OPENAI_SMALL_FAST_MODEL", "GEMINI_SMALL_FAST_MODEL"):
        monkeypatch.delenv(name, raising=False)


def _pick(provider, parent, ids):
    catalog = [SimpleNamespace(provider=provider, id=model_id) for model_id in ids]
    catalog.append(SimpleNamespace(provider="elsewhere", id="claude-haiku-9-9"))
    return _small_fast_hook_model(SimpleNamespace(provider=provider, id=parent), catalog).id


@pytest.mark.parametrize("provider, parent, ids, expected", [
    ("anthropic", "claude-opus-5-5",
     ["claude-haiku-4-5", "claude-haiku-4-5-20251001", "claude-3-5-haiku-latest", "claude-haiku-5-5"], "claude-haiku-5-5"),
    ("google-vertex", "gemini-3.8-pro",
     ["gemini-3-flash-preview", "gemini-2.5-flash", "gemini-3.8-flash-lite", "gemini-3.8-flash"], "gemini-3.8-flash"),
    ("google", "gemini-3.8-pro", ["gemini-flash-latest", "gemini-3.5-flash", "gemini-3.8-flash"], "gemini-3.8-flash"),
    ("openai", "gpt-5.5", ["gpt-4.1-mini", "gpt-5-mini", "gpt-5.4-mini", "gpt-5.5-mini", "gpt-5.5"], "gpt-5.5-mini"),
    ("gateway", "claude-opus-5-5", ["claude-haiku-5-5", "claude-haiku-4-5-20251001", "claude-opus-5-5"], "claude-haiku-5-5"),
])
def test_the_tier_is_the_newest_of_its_family(provider, parent, ids, expected):
    assert _pick(provider, parent, ids) == expected


def test_a_keyword_is_a_whole_word_of_the_id():
    assert _pick("proxy", "gpt-5.5", ["gemini-5-pro", "gpt-5.5", "gpt-4.1-mini"]) == "gpt-4.1-mini"


@pytest.mark.parametrize("parent, expected", [
    ("eu.anthropic.claude-sonnet-5-5", "eu.anthropic.claude-haiku-5-5"),
    ("anthropic.claude-sonnet-4-6-v1", "anthropic.claude-haiku-5-5"),
    ("apac.anthropic.claude-sonnet-5-5", "anthropic.claude-haiku-5-5"),      # no apac profile listed
])
def test_bedrock_keeps_the_parent_region_when_the_catalog_has_it(parent, expected):
    ids = ["anthropic.claude-haiku-4-5-20251001-v1:0", "us.anthropic.claude-haiku-5-5",
           "eu.anthropic.claude-haiku-5-5", "anthropic.claude-haiku-5-5", "eu.anthropic.claude-haiku-4-5-20251001-v1:0"]
    assert _pick("amazon-bedrock", parent, ids) == expected


def test_a_configured_small_fast_model_still_wins(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_SMALL_FAST_MODEL", "claude-haiku-4-5")
    assert _pick("anthropic", "claude-opus-5-5", ["claude-haiku-4-5", "claude-haiku-5-5"]) == "claude-haiku-4-5"


def test_a_provider_without_the_tier_keeps_the_parent():
    assert _pick("xai", "grok-5", ["grok-5", "grok-5-heavy"]) == "grok-5"

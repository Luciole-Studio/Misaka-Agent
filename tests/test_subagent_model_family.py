"""A sub-agent that names a model family (``opus``, ``sonnet``, ``haiku``) gets the newest of it in
its parent provider's configured catalog. CCB's pinned tier versions sent them to claude-opus-4-7
and dated 4-5 Sonnet and Haiku on a provider that also listed 5.5 (2026-10-25, R19): a blind
drafting test ran twice on the old models. The catalog is MISAKA's: models.json, refreshed
provider catalogs and the built-ins a release brings; a newer model there is picked up as is."""
from types import SimpleNamespace

import pytest

from misaka.core.subagent.model import resolve_model_spec

# The provider of the 2026-10-25 run, as its models.json listed it.
GATEWAY = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5", "claude-sonnet-5", "claude-haiku-5-5",
           "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5-20251101",
           "claude-sonnet-4-6", "claude-sonnet-4-5-20250929", "claude-haiku-4-5-20251001"]


def _catalog(provider, ids):
    return [SimpleNamespace(provider=provider, id=model_id) for model_id in ids]


def _resolve(spec, parent, ids, provider="gateway"):
    return resolve_model_spec({}, spec, None, SimpleNamespace(provider=provider, id=parent),
                              _catalog(provider, ids) + _catalog("elsewhere", ["claude-opus-9-9"]))


@pytest.mark.parametrize("family, parent, expected", [
    ("opus", "claude-sonnet-5-5", "claude-opus-5-5"),
    ("sonnet", "claude-opus-5-5", "claude-sonnet-5-5"),
    ("haiku", "claude-opus-5-5", "claude-haiku-5-5"),
    ("best", "claude-haiku-5-5", "claude-opus-5-5"),
])
def test_a_family_resolves_to_its_newest_model_in_the_parent_provider(family, parent, expected):
    assert _resolve(family, parent, GATEWAY) == ("gateway", expected)


def test_the_parent_family_keeps_the_parent_model():
    assert _resolve("opus", "claude-opus-4-7", GATEWAY) == ("gateway", "claude-opus-4-7")


@pytest.mark.parametrize("ids, expected", [
    (["claude-opus-4-9", "claude-opus-4-10"], "claude-opus-4-10"),                       # numbers, not text
    (["claude-opus-4-5-20251101", "claude-opus-4-5"], "claude-opus-4-5"),               # undated first
    (["claude-opus-5", "claude-opus-5-5", "claude-opus-4-8"], "claude-opus-5-5"),
    (["claude-3-opus-20240229", "claude-opus-4-1-20250805"], "claude-opus-4-1-20250805"),  # older naming
    (["anthropic/claude-opus-5.5:batch", "anthropic/claude-opus-5.5", "anthropic/claude-opus-4.8"],
     "anthropic/claude-opus-5.5"),                                                      # not a variant
])
def test_newest_is_read_off_the_version(ids, expected):
    assert _resolve("opus", "claude-sonnet-5-5", ids) == ("gateway", expected)


def test_a_date_is_not_a_version():
    ids = ["claude-3-7-sonnet-20250219", "claude-sonnet-5-5", "claude-sonnet-4-5-20250929"]
    assert _resolve("sonnet", "claude-opus-5-5", ids) == ("gateway", "claude-sonnet-5-5")
    older = ["claude-3-5-sonnet-20240620", "claude-3-7-sonnet-20250219"]
    assert _resolve("sonnet", "claude-opus-5-5", older) == ("gateway", "claude-3-7-sonnet-20250219")


@pytest.mark.parametrize("parent, expected", [
    ("eu.anthropic.claude-sonnet-5-5", "eu.anthropic.claude-haiku-5-5"),
    ("global.anthropic.claude-sonnet-5-5", "global.anthropic.claude-haiku-5-5"),
    ("anthropic.claude-sonnet-4-6-v1", "anthropic.claude-haiku-5-5"),
])
def test_a_bedrock_family_keeps_the_parent_region(parent, expected):
    ids = ["anthropic.claude-haiku-4-5-20251001-v1:0", "anthropic.claude-haiku-5-5", "us.anthropic.claude-haiku-5-5",
           "eu.anthropic.claude-haiku-5-5", "global.anthropic.claude-haiku-5-5", "au.anthropic.claude-haiku-5-5"]
    assert _resolve("haiku", parent, ids, provider="amazon-bedrock") == ("amazon-bedrock", expected)


def test_a_one_million_context_tag_stays_on_the_newest_model():
    assert _resolve("opus[1m]", "claude-sonnet-5-5", GATEWAY) == ("gateway", "claude-opus-5-5[1m]")


def test_a_provider_without_the_family_keeps_its_primary_model():
    assert _resolve("haiku", "gpt-5.5", ["gpt-5.5", "gpt-5.5-mini"], provider="openai-proxy") == ("openai-proxy", "gpt-5.5")

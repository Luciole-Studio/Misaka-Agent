"""The one gate every URL-taking tool runs before it dials.

Nothing here touches the network or DNS: every branch of :func:`screen_url` is a decision
about a string, which is exactly why it can run before anything resolves a name.
"""

from __future__ import annotations

import json

import pytest

from misaka.config.product import CFG
from misaka.core.tools._web import website_policy
from misaka.core.tools._web.screening import screen_url


@pytest.fixture(autouse=True)
def web_home(tmp_path, monkeypatch):
    """A throwaway web.json, with the policy cache cleared either side of the test."""
    path = tmp_path / "web.json"
    monkeypatch.setitem(CFG, "web_config", str(path))
    website_policy.invalidate_cache()
    yield path
    website_policy.invalidate_cache()


def write_policy(path, **blocklist):
    path.write_text(json.dumps({"website_blocklist": blocklist}), encoding="utf-8")
    website_policy.invalidate_cache()


def test_an_ordinary_url_passes_and_comes_back_normalised():
    screened = screen_url("https://example.com/Köln?q=weather")
    assert screened.allowed is True
    assert screened.refusal is None
    assert screened.policy is None
    assert screened.url.startswith("https://example.com/K")
    assert "Köln" not in screened.url  # percent-encoded on the way through


def test_a_bare_api_key_in_the_path_is_refused():
    screened = screen_url("https://example.com/v1/sk-abcdefghijklmnop/items")
    assert screened.allowed is False
    assert "API key or token" in screened.refusal


def test_a_percent_encoded_key_is_refused_too():
    """The raw string hides it; the check has to look at the decoded forms as well."""
    screened = screen_url("https://example.com/%73k-abcdefghijklmnop")
    assert screened.allowed is False
    assert "API key or token" in screened.refusal


def test_the_raw_string_is_screened_not_only_the_normalised_one():
    """secret_in_url covers four forms; handing it the normalised URL drops one of them."""
    assert screen_url("https://evil.test/ sk-abcdefghij0123456789").allowed is False
    assert screen_url("https://evil.test/%67hp_abcdefghij0123456789").allowed is False


def test_a_credential_named_query_parameter_is_refused_for_a_third_party_reader():
    screened = screen_url("https://example.com/data?access_token=opaque123", third_party=True)
    assert screened.allowed is False
    assert "access_token" in screened.refusal


def test_a_presigned_url_is_allowed_for_our_own_fetch():
    """download_file is built to take these; refusing them would cost a real capability."""
    url = "https://files.example/paper.pdf?X-Amz-Signature=deadbeefcafe"
    assert screen_url(url).allowed is True
    assert screen_url(url, third_party=True).allowed is False


def test_an_ordinary_query_parameter_is_not_mistaken_for_one():
    url = "https://example.com/search?q=how+to+key+a+lock"
    assert screen_url(url).allowed is True
    assert screen_url(url, third_party=True).allowed is True


def test_a_vendor_shaped_token_is_refused_on_every_path():
    """The exfiltration check does not care who dials: no real URL carries one of these."""
    url = "https://attacker.example/?stolen=ghp_abcdefghijklmnopqrst"
    assert screen_url(url).allowed is False
    assert screen_url(url, third_party=True).allowed is False


def test_a_blocked_domain_is_refused_and_carries_the_policy_record(web_home):
    write_policy(web_home, enabled=True, domains=["ads.example"])
    screened = screen_url("https://tracker.ads.example/pixel")
    assert screened.allowed is False
    assert "website policy" in screened.refusal
    assert screened.policy["host"] == "tracker.ads.example"
    assert screened.policy["rule"] == "ads.example"


def test_a_lookalike_domain_is_not_blocked(web_home):
    write_policy(web_home, enabled=True, domains=["example.com"])
    assert screen_url("https://notexample.com/page").allowed is True


def test_a_disabled_blocklist_allows_everything(web_home):
    write_policy(web_home, enabled=False, domains=["example.com"])
    assert screen_url("https://example.com/page").allowed is True


def test_a_broken_policy_config_fails_open(web_home):
    """A typo in a blocklist must not take every web tool down with it."""
    web_home.write_text('{"website_blocklist": {"domains": "not-a-list"}}', encoding="utf-8")
    website_policy.invalidate_cache()
    assert screen_url("https://example.com/page").allowed is True


def test_a_credential_is_refused_even_when_the_policy_layer_is_broken(web_home):
    """The token check is the one that must never fail open."""
    web_home.write_text("{ not json at all", encoding="utf-8")
    website_policy.invalidate_cache()
    assert screen_url("https://example.com/?stolen=sk-abcdefghijklmnop").allowed is False

"""The website blocklist: what the user wrote has to be what actually gets refused.

Hermes ships no test for ``tools/website_policy.py``, so this file is written from the
behaviour its docstrings promise. Every assertion here is about a rule the user typed by
hand -- which is why the lookalike case (``notexample.com`` must survive a rule for
``example.com``) and the wildcard case matter as much as the blocks do: a blocklist that
over-matches is a blocklist the user turns off.

Nothing here touches the network. The policy layer never opens a socket; it decides
whether the layers that do are allowed to.
"""

from __future__ import annotations

import json
import logging

import pytest
from webconf import write_web

from misaka.config import home
from misaka.core.web import website_policy
from misaka.core.web.website_policy import (
    WebsitePolicyError,
    check_website_access,
    invalidate_cache,
    load_website_blocklist,
)


@pytest.fixture(autouse=True)
def web_home(monkeypatch, tmp_path):
    """A throwaway web.json and an empty policy cache on both sides of every test.

    The cache is process-global, and while its fast path is scoped to the path and TTL it
    was read under, ``tmp_path`` is not the only thing that differs between tests --
    clearing it on both sides keeps one test's policy from deciding the next one's fetches
    however the fast path is later rearranged.
    """
    path = tmp_path / "policy.json"        # a standalone document, read by explicit path
    invalidate_cache()
    yield path
    invalidate_cache()


def write_policy(path, **blocklist) -> None:
    """The session's policy when ``path`` is the settings file; a standalone document otherwise."""
    if path == home.path("settings"):
        write_web({"website_blocklist": blocklist})
    else:
        path.write_text(json.dumps({"website_blocklist": blocklist}), encoding="utf-8")


@pytest.fixture
def session_policy(web_home):
    """The policy a real session reads: the ``web`` section of the home's settings."""
    return home.path("settings")


# --- enabled / disabled -------------------------------------------------------------


def test_disabled_policy_blocks_nothing(web_home):
    write_policy(web_home, enabled=False, domains=["ads.example"])
    assert check_website_access("https://ads.example/x", web_home) is None


def test_a_domain_list_without_enabled_blocks_nothing(web_home):
    """Hermes' opt-in default, kept: a half-written config is not a wall."""
    write_policy(web_home, domains=["ads.example"])
    assert load_website_blocklist(web_home)["enabled"] is False
    assert check_website_access("https://ads.example/x", web_home) is None


def test_absent_config_blocks_nothing(web_home):
    assert not web_home.exists()
    assert check_website_access("https://ads.example/x", web_home) is None


def test_enabled_policy_allows_an_unlisted_host(web_home):
    write_policy(web_home, enabled=True, domains=["ads.example"])
    assert check_website_access("https://docs.python.org/3/", web_home) is None


# --- matching -----------------------------------------------------------------------


def test_exact_host_block_names_the_rule_and_its_source(web_home):
    write_policy(web_home, enabled=True, domains=["ads.example"])
    blocked = check_website_access("https://ads.example/tracker?id=1", web_home)
    assert blocked == {
        "url": "https://ads.example/tracker?id=1",
        "host": "ads.example",
        "rule": "ads.example",
        "source": "config",
        "message": "Blocked by website policy: 'ads.example' matched rule 'ads.example' from config",
    }


def test_a_bare_rule_covers_its_subdomains(web_home):
    write_policy(web_home, enabled=True, domains=["mysite.dev"])
    blocked = check_website_access("https://preview.mysite.dev/page", web_home)
    assert blocked is not None
    assert blocked["host"] == "preview.mysite.dev"
    assert blocked["rule"] == "mysite.dev"


def test_a_lookalike_domain_is_not_blocked(web_home):
    """The dot in the suffix test is the whole defence against over-matching."""
    write_policy(web_home, enabled=True, domains=["example.com"])
    assert check_website_access("https://notexample.com/", web_home) is None


def test_www_is_stripped_from_a_rule_and_matched_back_on_the_host(web_home):
    write_policy(web_home, enabled=True, domains=["www.tracker.example"])
    assert load_website_blocklist(web_home)["rules"] == [
        {"pattern": "tracker.example", "source": "config"}
    ]
    assert check_website_access("https://tracker.example/", web_home) is not None
    assert check_website_access("https://www.tracker.example/", web_home) is not None


def test_a_wildcard_rule_covers_subdomains_only(web_home):
    write_policy(web_home, enabled=True, domains=["*.cdn.example"])
    assert check_website_access("https://assets.cdn.example/lib.js", web_home) is not None
    assert check_website_access("https://cdn.example/lib.js", web_home) is None


def test_a_full_url_is_reduced_to_its_host(web_home):
    write_policy(web_home, enabled=True, domains=["HTTPS://Ads.Example.COM/tracker?x=1"])
    assert load_website_blocklist(web_home)["rules"] == [
        {"pattern": "ads.example.com", "source": "config"}
    ]
    assert check_website_access("https://ads.example.com/anything", web_home) is not None


def test_a_schemeless_url_still_yields_a_host(web_home):
    write_policy(web_home, enabled=True, domains=["ads.example"])
    blocked = check_website_access("ads.example/path", web_home)
    assert blocked is not None
    assert blocked["host"] == "ads.example"


def test_a_string_with_no_host_is_allowed(web_home):
    write_policy(web_home, enabled=True, domains=["ads.example"])
    assert check_website_access("not a url at all", web_home) is None


@pytest.mark.parametrize("url", ["https://[::1", "http://exa[mple.com", "[::1"])
def test_an_unparseable_authority_is_allowed_rather_than_raising(web_home, url):
    """``urlparse`` raises on an unbalanced bracket; a blocklist has no verdict for that.

    Reachable, not theoretical: ``web_fetch`` turns any schemeless string into
    ``https://<string>``, so a model typing ``[::1`` arrives here as ``https://[::1``, and
    ``screen_url`` above this promises never to raise.
    """
    write_policy(web_home, enabled=True, domains=["ads.example"])
    assert check_website_access(url, web_home) is None


def test_an_unparseable_rule_is_dropped_rather_than_raising(web_home):
    """A rule the user wrote as a URL that does not parse means nothing, and says so."""
    write_policy(web_home, enabled=True, domains=["http://[::1", "ads.example"])
    assert load_website_blocklist(web_home)["rules"] == [
        {"pattern": "ads.example", "source": "config"}
    ]
    assert check_website_access("https://ads.example/", web_home) is not None


# --- shared files -------------------------------------------------------------------


def test_a_shared_file_contributes_rules_and_skips_comments_and_blanks(web_home, tmp_path):
    (tmp_path / "blocked.txt").write_text(
        "# vendor trackers\n\nads.example\n  www.metrics.example  \n#ads.notreally\n",
        encoding="utf-8",
    )
    write_policy(web_home, enabled=True, shared_files=["blocked.txt"])

    policy = load_website_blocklist(web_home)
    assert [rule["pattern"] for rule in policy["rules"]] == ["ads.example", "metrics.example"]
    assert all(rule["source"] == str(tmp_path / "blocked.txt") for rule in policy["rules"])

    blocked = check_website_access("https://ads.example/", web_home)
    assert blocked is not None
    assert blocked["source"] == str(tmp_path / "blocked.txt")
    assert str(tmp_path / "blocked.txt") in blocked["message"]


def test_a_missing_shared_file_warns_and_contributes_nothing(web_home, caplog):
    write_policy(web_home, enabled=True, domains=["ads.example"], shared_files=["gone.txt"])

    with caplog.at_level(logging.WARNING, logger=website_policy.__name__):
        policy = load_website_blocklist(web_home)

    assert [rule["pattern"] for rule in policy["rules"]] == ["ads.example"]
    assert "gone.txt" in caplog.text
    assert "not found" in caplog.text


# --- malformed configuration --------------------------------------------------------


@pytest.mark.parametrize(
    ("blocklist", "expected"),
    [
        ({"enabled": True, "domains": "ads.example"}, "domains must be a list"),
        ({"enabled": True, "shared_files": "blocked.txt"}, "shared_files must be a list"),
        ({"enabled": "yes"}, "enabled must be a boolean"),
    ],
)
def test_an_explicit_path_propagates_validation_errors(web_home, blocklist, expected):
    write_policy(web_home, **blocklist)
    with pytest.raises(WebsitePolicyError, match=expected):
        check_website_access("https://ads.example/", web_home)


def test_an_explicit_path_propagates_a_parse_error(web_home):
    web_home.write_text("{not json", encoding="utf-8")
    with pytest.raises(WebsitePolicyError, match="Invalid config JSON"):
        check_website_access("https://ads.example/", web_home)


def test_a_session_fails_open_on_a_malformed_config(session_policy, caplog):
    """No explicit path means a real session: a config typo must not ground web tools."""
    session_policy.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=website_policy.__name__):
        assert check_website_access("https://ads.example/") is None
    assert "failing open" in caplog.text


# --- the cache ----------------------------------------------------------------------


def test_an_edit_is_invisible_until_the_cache_is_invalidated(session_policy):
    write_policy(session_policy, enabled=True, domains=["ads.example"])
    assert check_website_access("https://metrics.example/") is None

    write_policy(session_policy, enabled=True, domains=["ads.example", "metrics.example"])
    assert check_website_access("https://metrics.example/") is None

    invalidate_cache()
    assert check_website_access("https://metrics.example/") is not None


def test_a_cached_disabled_policy_short_circuits_before_any_read(session_policy, monkeypatch):
    """The fast path in front of every fetch: disabled means no file read, no host parse."""
    write_policy(session_policy, enabled=False)
    assert check_website_access("https://ads.example/") is None

    def explode(*_args, **_kwargs):
        raise AssertionError("the disabled fast path must not reach the config")

    monkeypatch.setattr(website_policy, "_load_policy_config", explode)
    assert check_website_access("https://ads.example/") is None


def test_the_disabled_fast_path_expires_with_the_ttl(session_policy, monkeypatch):
    """Turning the blocklist on is the one transition every installation makes.

    ``enabled`` is opt-in, so every session starts in the cached-disabled state this fast
    path short-circuits. If it never expired, the user's first edit would never take
    effect -- and ``invalidate_cache()`` has no production caller to rescue them.
    """
    write_policy(session_policy, enabled=False)
    assert check_website_access("https://ads.example/") is None

    write_policy(session_policy, enabled=True, domains=["ads.example"])
    monkeypatch.setattr(
        website_policy,
        "_cached_policy_time",
        website_policy._cached_policy_time - website_policy._CACHE_TTL_SECONDS - 1,
    )
    assert check_website_access("https://ads.example/") is not None


def test_the_disabled_fast_path_is_scoped_to_the_path_it_read(session_policy, tmp_path, monkeypatch):
    """The home is redirectable (``MISAKA_HOME``), so a cached verdict belongs to one path."""
    write_policy(session_policy, enabled=False)
    assert check_website_access("https://ads.example/") is None

    monkeypatch.setenv(home.ENV_HOME, str(tmp_path / "other-home"))
    write_policy(home.path("settings"), enabled=True, domains=["ads.example"])
    assert check_website_access("https://ads.example/") is not None

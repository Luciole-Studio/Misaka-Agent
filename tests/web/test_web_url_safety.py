"""URL-text safety: normalisation, credentials in URLs, the metadata floor, the opt-out.

Nothing here resolves a name or opens a socket -- every question this module answers is
about the characters of a URL, which is exactly what makes it testable without a network.
The address *reachability* half lives next door in tests/test_web_bounded.py.
"""
from __future__ import annotations

import ipaddress
import json

import pytest

from misaka.config.product import CFG
from misaka.core.tools._web import url_safety
from misaka.core.tools._web.url_safety import (
    CGNAT_NETWORK,
    allow_private_urls,
    always_blocked_address,
    always_blocked_host,
    has_sensitive_query_params,
    normalize_url_for_request,
    secret_in_url,
    sensitive_query_param_name,
)

# Long enough to clear every ``{10,}`` in the prefix table, short enough to read.
_BODY = "abcdefghij0123456789"


# --- normalisation ------------------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    # Non-ASCII path is percent-encoded: an IRI is not something httpx will dial.
    ("https://wttr.in/Köln", "https://wttr.in/K%C3%B6ln"),
    # Existing escapes survive -- ``%`` is in the safe set, so no double-encoding.
    ("https://wttr.in/K%C3%B6ln", "https://wttr.in/K%C3%B6ln"),
    # The host goes through IDNA, not percent-encoding: DNS has no way to read %C3.
    ("https://münich.example/Köln", "https://xn--mnich-kva.example/K%C3%B6ln"),
    # Whitespace in path and fragment is encoded rather than repaired.
    ("https://example.com/a b#frag ment", "https://example.com/a%20b#frag%20ment"),
    # Reserved sub-delims stay literal: re-encoding ``&`` would rewrite the query.
    ("https://example.com/s?a=1&b=2", "https://example.com/s?a=1&b=2"),
])
def test_encodes_url_parts(raw, expected):
    assert normalize_url_for_request(raw) == expected


def test_repairs_whitespace_after_the_scheme_separator():
    """``https:// host`` is a model formatting artifact, not a URL with a space in it.

    That position is never meaningful in an HTTP(S) URL, so repairing it costs nothing
    and saves a fetch that would otherwise fail on a stray keystroke.
    """
    assert normalize_url_for_request("https:// docs.example/a b") == "https://docs.example/a%20b"


def test_does_not_collapse_an_embedded_scheme_separator_in_the_query():
    """The repair is anchored at the start: a ``://`` inside a query is data, not syntax."""
    assert (
        normalize_url_for_request("https://example.com/r?next=https:// evil.example")
        == "https://example.com/r?next=https://%20evil.example"
    )


@pytest.mark.parametrize("raw", [
    "https://wttr.in/Köln",
    "https://münich.example/Köln?q=Köln#Köln",
    "https://example.com/a b",
    "https://example.com/already%20encoded",
])
def test_normalisation_is_idempotent(raw):
    once = normalize_url_for_request(raw)
    assert normalize_url_for_request(once) == once


def test_non_http_schemes_are_left_alone():
    """Rewriting these would corrupt strings that merely look URL-shaped."""
    assert normalize_url_for_request("mailto:a@b.example") == "mailto:a@b.example"
    assert normalize_url_for_request("file:///tmp/a b") == "file:///tmp/a b"


@pytest.mark.parametrize("value", [None, 42, b"https://example.com/"])
def test_non_str_input_comes_back_unchanged(value):
    assert normalize_url_for_request(value) is value


# --- credentials in the URL text ----------------------------------------------------


@pytest.mark.parametrize("url", [
    f"https://example.com/?k=sk-{_BODY}",                 # OpenAI / Anthropic
    f"https://api.example.com/x?t=ghp_{_BODY}",           # GitHub PAT
    f"https://example.com/search?key=tvly-{_BODY}",       # Tavily
    "https://example.com/?k=xai-" + "a" * 30,             # xAI (30-char minimum)
    "https://example.com/?id=AKIAIOSFODNN7EXAMPLE",       # AWS access key id
    f"https://example.com/crawl/fc-{_BODY}",              # Firecrawl
])
def test_credential_families_are_detected(url):
    assert secret_in_url(url) is True


@pytest.mark.parametrize("url", [
    "https://example.com/ask-abcdefghijklmnop",  # lookbehind: 'a' precedes 'sk-'
    "https://example.com/docs/basket-report",
    "https://example.com/?q=sk-short",           # below the 10-char body minimum
    "https://example.com/plain/page",
    "",
])
def test_ordinary_urls_are_not_credentials(url):
    assert secret_in_url(url) is False


@pytest.mark.parametrize("url", [
    "https://www.digmandarin.com/hsk-vocabulary",       # hsk- + a 10-letter word
    "https://example.com/fc-bayernmunich/news",         # fc- (Firecrawl) + a football club
    "https://docs.example.com/guide/npm_dependencies",  # npm_ + the word every repo uses
    "https://example.com/pypi-package-listing",
    "https://example.com/rk_live_documentation",
    "https://example.com/sk_test_documentation",
    "https://example.com/blog/am_i_doing_this_right",   # am_ + an English sentence-slug
    "https://shop.example.com/p?am_campaign_id=summer",
    "https://example.com/sk_configuration_guide",
    "https://example.com/fal_something_here",
    "https://example.com/gsk_documentation",
])
def test_a_word_shaped_path_slug_is_not_a_credential(url):
    """Why ``_URL_BODY_MINIMUM`` exists, in the URLs that made the case for it.

    Every one of these is a page somebody would actually ask for, and at the table's own
    ``{10,}`` floor every one of them reads as a vendor key: a two-to-four character
    prefix plus ten word characters is the shape of an ordinary slug. Upstream tolerates
    that because it is *redacting* -- a false positive stars out a substring of a log.
    Here a false positive refuses the fetch, on MISAKA's own ``web_fetch`` and
    ``download_file`` as well as on the third-party readers Hermes confines the check to.
    """
    assert secret_in_url(url) is False


@pytest.mark.parametrize("key", [
    "fc-" + "0a" * 16,                 # Firecrawl, 32 hex
    "npm_" + "a" * 36,                 # npm access token
    "hf_" + "a" * 34,                  # HuggingFace
    "r8_" + "a" * 40,                  # Replicate
    "sk_" + "a" * 32,                  # ElevenLabs
    "rk_live_" + "a" * 24,             # Stripe restricted key
    "sk_test_" + "a" * 24,             # Stripe secret key (test)
    "glpat-" + "a" * 20,               # GitLab PAT -- the shortest body in the table
    "pk-lf-" + "a" * 36,               # Langfuse, whose own floor is 8
    "SG." + "a" * 22 + "." + "b" * 43,  # SendGrid: the body stops at the dot
])
def test_the_raised_floor_still_clears_every_real_key_length(key):
    """The floor is only worth raising if it costs no detection.

    Twenty is chosen against the shortest body any vendor in the table issues, so a real
    key of each family stays caught -- including the two that sit closest to the line,
    GitLab's 20-character PAT and Stripe's 24.
    """
    assert secret_in_url(f"https://attacker.example/?stolen={key}") is True


def test_sk_dash_keeps_the_original_floor():
    """The one prefix deliberately left at Hermes' ``{10,}``.

    ``sk-`` names the keys an agent is actually holding, so it is the prefix an
    exfiltration attempt most likely carries, and it gains least from the raise: the
    lookbehind already excludes the English words ending in it, and no ordinary path
    segment starts with a bare ``sk-``. ``screening.screen_url`` is built on this
    remaining true for a 16-character body.
    """
    assert "sk-" in url_safety._UNRAISED_PREFIXES
    assert secret_in_url("https://example.com/v1/sk-abcdefghijklmnop/items") is True


def test_every_other_body_floor_is_raised_to_the_url_minimum():
    """The table stays verbatim; the compiled pattern is where MISAKA diverges.

    Asserted over the derivation rather than a spelled-out list so that a table refreshed
    from upstream cannot quietly reintroduce a ``{10,}`` floor.
    """
    raised = {
        pattern.split("[", 1)[0]: url_safety._url_pattern(pattern)
        for pattern in url_safety._PREFIX_PATTERNS
    }
    assert raised["sk-"] == "sk-[A-Za-z0-9_-]{10,}"          # untouched
    assert raised["AKIA"] == "AKIA[A-Z0-9]{16}"              # a fixed count is not a floor
    assert raised["AIza"].endswith("{30,}")                  # already above the minimum
    for prefix, pattern in raised.items():
        floor = url_safety._BODY_FLOOR_RE.search(pattern)
        if floor is not None and prefix not in url_safety._UNRAISED_PREFIXES:
            assert int(floor.group(1)) >= url_safety._URL_BODY_MINIMUM, prefix


def test_non_str_is_not_a_credential():
    assert secret_in_url(None) is False


def test_percent_encoded_key_is_caught_by_the_decoded_form():
    """The reason there are four forms and not one.

    ``%73`` is ``s``: the raw string contains no ``sk-`` for the pattern to anchor on, so
    a single-pass check hands the key to the backend. Decoding is what closes it.
    """
    hidden = f"https://example.com/?k=%73k-{_BODY}"
    assert url_safety._PREFIX_RE.search(hidden) is None
    assert secret_in_url(hidden) is True


# --- credential-named query parameters ----------------------------------------------


@pytest.mark.parametrize("name", sorted(url_safety._SENSITIVE_QUERY_PARAM_NAMES))
def test_every_sensitive_param_name_is_reported(name):
    assert sensitive_query_param_name(f"https://example.com/p?{name}=opaque-value") == name
    assert has_sensitive_query_params(f"https://example.com/p?{name}=opaque-value") is True


@pytest.mark.parametrize("name", ["key", "code", "auth", "sig", "session", "id", "q"])
def test_deliberately_excluded_bare_words_do_not_trip(name):
    """These are the load-bearing omissions, not oversights.

    ``?code=`` is how every promo and challenge page on the web addresses a page, and
    ``key``/``auth``/``sig``/``session`` are routing and search facets at least as often
    as they are secrets. Adding them would block ordinary browsing; the vendor-prefix
    check in :func:`secret_in_url` still catches anything key-shaped that lands in one.
    """
    assert sensitive_query_param_name(f"https://example.com/p?{name}=whatever") is None
    assert has_sensitive_query_params(f"https://example.com/p?{name}=whatever") is False


def test_param_name_matching_ignores_case_and_encoding():
    """The reported name is what the URL spelled; the *match* is made on a decoded copy.

    ``parse_qsl`` decodes once and Hermes' ``unquote(key)`` decodes again, so a
    double-encoded ``%2574oken`` is caught too -- one more layer than an attacker gets
    for free, and the returned string stays the one a user can find in their URL.
    """
    assert sensitive_query_param_name("https://example.com/p?Access_Token=abc") == "Access_Token"
    assert sensitive_query_param_name("https://example.com/p?%74oken=abc") == "token"
    assert sensitive_query_param_name("https://example.com/p?%2574oken=abc") == "%74oken"


@pytest.mark.parametrize("url", [
    "https://example.com/p?token=",       # an unfilled form is not a credential
    "https://example.com/p",              # no query at all
    "ftp://example.com/p?token=abc",      # not a scheme these tools fetch
    "not a url at all",
])
def test_no_sensitive_param(url):
    assert sensitive_query_param_name(url) is None


def test_non_str_has_no_sensitive_params():
    assert sensitive_query_param_name(None) is None


# --- the always-blocked floor -------------------------------------------------------


@pytest.mark.parametrize("host", sorted(url_safety._BLOCKED_HOSTNAMES))
def test_metadata_hostnames_are_blocked(host):
    assert always_blocked_host(host) is True


def test_hostname_matching_normalises_case_and_the_root_dot():
    assert always_blocked_host("Metadata.Google.Internal.") is True
    assert always_blocked_host("  metadata.goog  ") is True


@pytest.mark.parametrize("host", ["example.com", "metadata.google.internal.evil.com", "", None])
def test_ordinary_hostnames_are_not_in_the_floor(host):
    assert always_blocked_host(host) is False


@pytest.mark.parametrize("value", [
    "169.254.169.254",              # AWS/GCP/Azure/DO/Oracle IMDS
    "169.254.170.2",                # ECS task metadata (task IAM credentials)
    "169.254.169.253",              # Azure IMDS wire server
    "100.100.100.200",              # Alibaba Cloud
    "fd00:ec2::254",                # AWS metadata over IPv6
    "::ffff:169.254.169.254",       # ... and the IPv4-mapped forms of each,
    "::ffff:169.254.170.2",         # which ipaddress treats as distinct values,
    "::ffff:169.254.169.253",       # so a resolver answering ::ffff: would
    "::ffff:100.100.100.200",       # otherwise walk straight past the set.
    "169.254.42.1",                 # any link-local address, not just the sentinels
    "::ffff:169.254.42.1",
    "::ffff:169.254.169.254%eth0",  # a scope id must not make it unparseable
])
def test_always_blocked_addresses(value):
    assert always_blocked_address(value) is True
    assert always_blocked_address(ipaddress.ip_address(value.split("%", 1)[0])) is True


@pytest.mark.parametrize("value", [
    "127.0.0.1",        # private, but the floor is narrower than the SSRF gate:
    "10.0.0.7",         # these are the operator's to allow.
    "100.64.0.1",       # CGNAT
    "93.184.216.34",    # plainly public
    "2606:4700::1",
    "example.com",      # a name is the resolver's question, not this one's
    "",
])
def test_addresses_outside_the_floor(value):
    assert always_blocked_address(value) is False


def test_the_floor_ignores_the_operator_opt_out(monkeypatch):
    """``allow_private_urls`` can widen what is reachable; it can never open this."""
    monkeypatch.setenv("MISAKA_ALLOW_PRIVATE_URLS", "true")
    assert allow_private_urls() is True
    assert always_blocked_address("169.254.169.254") is True
    assert always_blocked_host("metadata.google.internal") is True


def test_every_listed_metadata_ip_is_covered_by_the_public_helper():
    """The table and the function must not drift apart."""
    assert all(always_blocked_address(ip) for ip in url_safety._ALWAYS_BLOCKED_IPS)


# --- CGNAT ---------------------------------------------------------------------------


def test_cgnat_is_the_range_ipaddress_will_not_flag():
    """Why CGNAT_NETWORK is exported instead of left to ``is_private``.

    100.64.0.0/10 (RFC 6598) reports False for *both* ``is_private`` and ``is_global``,
    so every natural spelling of "block the non-public ranges" lets it through -- into a
    carrier NAT, a Tailscale/WireGuard mesh, or a cloud internal network.
    """
    inside = ipaddress.ip_address("100.64.0.1")
    assert inside.is_private is False
    assert inside.is_global is False
    assert inside in CGNAT_NETWORK
    assert ipaddress.ip_address("100.127.255.255") in CGNAT_NETWORK
    assert ipaddress.ip_address("100.63.255.255") not in CGNAT_NETWORK
    assert ipaddress.ip_address("100.128.0.0") not in CGNAT_NETWORK


# --- the operator opt-out ------------------------------------------------------------


@pytest.fixture
def web_json(monkeypatch, tmp_path):
    """A throwaway ``web.json`` and no ``MISAKA_ALLOW_PRIVATE_URLS`` in the environment."""
    monkeypatch.delenv("MISAKA_ALLOW_PRIVATE_URLS", raising=False)
    path = tmp_path / "web.json"
    monkeypatch.setitem(CFG, "web_config", str(path))
    return path


def test_nothing_set_means_blocked(web_json):
    assert not web_json.exists()
    assert allow_private_urls() is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", "TRUE", " True "])
def test_env_opts_out(web_json, monkeypatch, value):
    monkeypatch.setenv("MISAKA_ALLOW_PRIVATE_URLS", value)
    assert allow_private_urls() is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off"])
def test_env_false_wins_over_a_config_that_says_true(web_json, monkeypatch, value):
    """An explicit false must not fall through to the file.

    Someone exporting this for one process is answering the question, not declining to
    answer it -- and the process they are protecting is usually the one running untrusted
    output.
    """
    web_json.write_text(json.dumps({"allow_private_urls": True}), encoding="utf-8")
    monkeypatch.setenv("MISAKA_ALLOW_PRIVATE_URLS", value)
    assert allow_private_urls() is False


@pytest.mark.parametrize("stored, expected", [
    (True, True),
    ("true", True),
    ("yes", True),
    (False, False),
    ("false", False),   # bool("false") is True; reading it that way would open the gate
    ("off", False),
    (None, False),
    ("", False),
])
def test_config_value(web_json, stored, expected):
    web_json.write_text(json.dumps({"allow_private_urls": stored}), encoding="utf-8")
    assert allow_private_urls() is expected


def test_unset_key_in_an_existing_config_means_blocked(web_json):
    web_json.write_text(json.dumps({"backend": "tavily"}), encoding="utf-8")
    assert allow_private_urls() is False


@pytest.mark.parametrize("body", ["{not json", "[]", '"a string"'])
def test_a_malformed_config_never_raises_and_stays_closed(web_json, body):
    web_json.write_text(body, encoding="utf-8")
    assert allow_private_urls() is False


def test_the_toggle_is_not_cached(web_json):
    """The file the user just edited must take effect on the next call.

    Hermes memoises this for the process lifetime; MISAKA does not, because every other
    read of ``web.json`` here is uncached and a security toggle that alone needs a
    restart -- with nothing on screen saying so -- is unsupportable.
    """
    web_json.write_text(json.dumps({"allow_private_urls": True}), encoding="utf-8")
    assert allow_private_urls() is True
    web_json.write_text(json.dumps({"allow_private_urls": False}), encoding="utf-8")
    assert allow_private_urls() is False

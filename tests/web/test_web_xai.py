"""The xAI (Grok) web-search backend: three parse tiers, two credential ladders, one retry.

Ported from Hermes' ``tests/tools/test_web_providers_xai.py``. Nothing here touches the
network: every request is answered by an ``httpx.MockTransport`` mounted on the module's
session-owned HTTP transport seam, and the credential store is a JSON file in ``tmp_path`` reached
through the ``_auth_path`` seam, so no test can see -- or write -- a real login.

What is worth testing at all in a backend whose upstream is a language model: not that
Grok answers well, but that every shape it is known to answer *with* becomes the same
four-key row, and that a rejected token is retried exactly when retrying it can help.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest
from webconf import write_web

from misaka.ai.utils.oauth import OAuthCredentials
from misaka.ai.utils.oauth import xai as xai_oauth
from misaka.core.web.backends import xai


@pytest.fixture(autouse=True)
def xai_home(monkeypatch, tmp_path):
    """A throwaway web config, an empty xAI environment, and a throwaway credential store."""
    for name in ("XAI_API_KEY", "XAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(xai, "_auth_path", lambda: str(tmp_path / "auth.json"))
    return tmp_path


def write_web_config(_home, **section) -> None:
    """Store the given keys under ``xai`` in the web settings."""
    write_web({"xai": section})


def far_future_ms() -> int:
    """An expiry well outside the store's five-minute refresh window."""
    return int((time.time() + 3600) * 1000)


def write_oauth_login(home, access: str = "stale-token", refresh: str = "r1") -> None:
    """Put a stored xAI OAuth grant in the credential store, the shape ``/login xai`` writes."""
    (home / "auth.json").write_text(
        json.dumps(
            {"xai": {"type": "oauth", "access": access, "refresh": refresh, "expires": far_future_ms()}}
        ),
        encoding="utf-8",
    )


def stub_net(monkeypatch, handler):
    """Mount *handler* as the module's transport; returns the list of requests it received."""
    sent: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return handler(request)

    real = httpx.AsyncClient

    def client(**kwargs):
        return real(transport=httpx.MockTransport(record), **(kwargs | {"proxy": None}))

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return sent


def responses_payload(text: str, annotations=None, citations=None) -> dict:
    """A minimal Responses-API reply: one message carrying one ``output_text`` block."""
    chunk: dict = {"type": "output_text", "text": text}
    if annotations is not None:
        chunk["annotations"] = annotations
    payload: dict = {"output": [{"type": "message", "content": [chunk]}]}
    if citations is not None:
        payload["citations"] = citations
    return payload


def replies(payload, status: int = 200):
    """A handler answering every request with *payload* as JSON."""
    return lambda request: httpx.Response(status, json=payload, request=request)


def sent_body(request: httpx.Request) -> dict:
    return json.loads(request.content)


GROK_JSON = json.dumps(
    {
        "results": [
            {"title": "xAI", "url": "https://x.ai", "description": "The company."},
            {"title": "Grok docs", "url": "https://docs.x.ai", "description": "API reference."},
            {"title": "Grokipedia", "url": "https://grokipedia.com", "description": "Wiki."},
        ]
    }
)


# ---------------------------------------------------------------------------
# Identity and availability
# ---------------------------------------------------------------------------


def test_the_provider_is_named_for_its_config_key():
    provider = xai.XAIWebSearchProvider()
    assert provider.name == "xai"
    assert "Grok" in provider.display_name


def test_search_only_and_never_keyless():
    provider = xai.XAIWebSearchProvider()
    assert provider.supports_extract() is False
    assert provider.is_keyless_available() is False


def test_an_api_key_makes_the_backend_available(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "sk-xai-test")
    assert xai.XAIWebSearchProvider().is_available() is True


def test_a_stored_login_makes_the_backend_available(xai_home):
    write_oauth_login(xai_home)
    assert xai.XAIWebSearchProvider().is_available() is True


def test_no_credentials_anywhere_is_unavailable():
    assert xai.XAIWebSearchProvider().is_available() is False


def test_a_corrupted_credential_store_is_unavailable_not_an_exception(xai_home):
    (xai_home / "auth.json").write_text("not json at all }{", encoding="utf-8")
    assert xai.XAIWebSearchProvider().is_available() is False


def test_availability_never_opens_the_credential_store(monkeypatch):
    """The ABC's contract: this runs while a session is assembled, so no lock and no refresh."""
    monkeypatch.setenv("XAI_API_KEY", "sk-xai-test")

    class Forbidden:
        @staticmethod
        def create(*_args, **_kwargs):
            raise AssertionError("is_available must not open the credential store")

    monkeypatch.setattr(xai, "AuthStorage", Forbidden)
    assert xai.XAIWebSearchProvider().is_available() is True


# ---------------------------------------------------------------------------
# Parse tier 1: the JSON object Grok was asked for
# ---------------------------------------------------------------------------


async def test_a_clean_json_block_becomes_the_row_contract(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "k")
    stub_net(monkeypatch, replies(responses_payload(GROK_JSON)))

    result = await xai.XAIWebSearchProvider().search("what is xai", limit=5)

    assert result == {
        "success": True,
        "data": {
            "web": [
                {
                    "title": "xAI",
                    "url": "https://x.ai",
                    "description": "The company.",
                    "position": 1,
                },
                {
                    "title": "Grok docs",
                    "url": "https://docs.x.ai",
                    "description": "API reference.",
                    "position": 2,
                },
                {
                    "title": "Grokipedia",
                    "url": "https://grokipedia.com",
                    "description": "Wiki.",
                    "position": 3,
                },
            ]
        },
    }


async def test_json_wrapped_in_prose_is_still_parsed(monkeypatch):
    """Reasoning models narrate before the block even when told not to."""
    monkeypatch.setenv("XAI_API_KEY", "k")
    stub_net(monkeypatch, replies(responses_payload(f"Here are the results:\n{GROK_JSON}\nHope that helps!")))

    result = await xai.XAIWebSearchProvider().search("q", limit=5)

    assert [row["url"] for row in result["data"]["web"]] == [
        "https://x.ai",
        "https://docs.x.ai",
        "https://grokipedia.com",
    ]


async def test_a_row_without_a_url_is_dropped_and_positions_close_up(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "k")
    text = json.dumps(
        {
            "results": [
                {"title": "no url", "description": "skip me"},
                {"title": "good", "url": "https://ok.com", "description": "keep"},
            ]
        }
    )
    stub_net(monkeypatch, replies(responses_payload(text)))

    result = await xai.XAIWebSearchProvider().search("q", limit=5)

    assert result["data"]["web"] == [
        {"title": "good", "url": "https://ok.com", "description": "keep", "position": 1}
    ]


# ---------------------------------------------------------------------------
# Parse tiers 2 and 3: annotations, then bare citations
# ---------------------------------------------------------------------------


async def test_annotations_carry_the_answer_when_there_is_no_json(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "k")
    body = "xAI is an AI company founded in 2023. They make Grok."
    annotations = [
        {"type": "url_citation", "url": "https://x.ai/about", "title": "1", "start_index": 4, "end_index": 9},
        {"type": "url_citation", "url": "https://docs.x.ai", "title": "2", "start_index": 47, "end_index": 52},
        # A repeat of the first URL: Grok cites the same page several times per answer.
        {"type": "url_citation", "url": "https://x.ai/about", "title": "3", "start_index": 50, "end_index": 53},
    ]
    stub_net(monkeypatch, replies(responses_payload(body, annotations=annotations)))

    result = await xai.XAIWebSearchProvider().search("xai", limit=5)

    web = result["data"]["web"]
    assert [row["url"] for row in web] == ["https://x.ai/about", "https://docs.x.ai"]
    assert [row["position"] for row in web] == [1, 2]
    # The description is the prose immediately before the citation marker.
    assert web[0]["description"] == "xAI"
    assert web[0]["title"] == ""


async def test_bare_citations_are_the_last_resort(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "k")
    payload = responses_payload(
        "No structured answer here.",
        citations=["https://a.example", "https://b.example", ""],
    )
    stub_net(monkeypatch, replies(payload))

    result = await xai.XAIWebSearchProvider().search("q", limit=5)

    assert result["data"]["web"] == [
        {"title": "", "url": "https://a.example", "description": "", "position": 1},
        {"title": "", "url": "https://b.example", "description": "", "position": 2},
    ]


async def test_an_empty_but_valid_reply_is_a_success_with_no_rows(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "k")
    stub_net(monkeypatch, replies(responses_payload("", citations=[])))

    result = await xai.XAIWebSearchProvider().search("q", limit=5)

    assert result == {"success": True, "data": {"web": []}}


# ---------------------------------------------------------------------------
# What goes on the wire
# ---------------------------------------------------------------------------


async def test_the_request_is_a_bearer_post_to_the_responses_endpoint(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "secret-key")
    sent = stub_net(monkeypatch, replies(responses_payload(GROK_JSON)))

    await xai.XAIWebSearchProvider().search("q", limit=5)

    request = sent[0]
    assert str(request.url) == "https://api.x.ai/v1/responses"
    assert request.headers["authorization"] == "Bearer secret-key"
    assert request.headers["user-agent"] == "misaka-agent"
    body = sent_body(request)
    assert body["model"] == xai.DEFAULT_MODEL
    assert body["tools"] == [{"type": "web_search"}]
    assert body["input"][0]["role"] == "user"
    assert "no_inline_citations" in body["include"]


async def test_the_base_url_is_overridable(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "k")
    monkeypatch.setenv("XAI_BASE_URL", "https://proxy.x.ai/v1/")
    sent = stub_net(monkeypatch, replies(responses_payload(GROK_JSON)))

    await xai.XAIWebSearchProvider().search("q", limit=5)

    assert str(sent[0].url) == "https://proxy.x.ai/v1/responses"


# ---------------------------------------------------------------------------
# The origin pin: an OAuth bearer only ever leaves for xAI
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "override",
    [
        "http://api.x.ai/v1",  # right host, no TLS -- the bearer would go in cleartext
        "https://attacker.example/v1",  # wrong host entirely
        "https://x.ai.attacker.example/v1",  # suffix that only looks like the origin
        "https://notx.ai/v1",  # `.x.ai` must be a label boundary, not a substring
        "http://[oops/v1",  # malformed authority: urlparse raises on .hostname
        "/v1",  # no scheme and no hostname at all
    ],
)
async def test_an_off_origin_base_url_cannot_redirect_an_oauth_bearer(
    monkeypatch, xai_home, override
):
    """The whole point of the pin: a tampered env or web.json must not exfiltrate the token."""
    write_oauth_login(xai_home, access="oauth-token")
    monkeypatch.setenv("XAI_BASE_URL", override)
    sent = stub_net(monkeypatch, replies(responses_payload(GROK_JSON)))

    result = await xai.XAIWebSearchProvider().search("q", limit=5)

    assert result["success"] is True
    assert str(sent[0].url) == "https://api.x.ai/v1/responses"
    assert sent[0].headers["authorization"] == "Bearer oauth-token"


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ("https://api.x.ai/v2/", "https://api.x.ai/v2/responses"),
        ("https://staging.api.x.ai/v1", "https://staging.api.x.ai/v1/responses"),
        # Accepted on the strength of the host alone; httpx lowercases it on the way out.
        ("https://X.AI/v1", "https://x.ai/v1/responses"),
    ],
)
async def test_an_on_origin_base_url_still_serves_an_oauth_bearer(
    monkeypatch, xai_home, override, expected
):
    """The pin is an origin check, not a ban: xAI's own hosts stay reachable, case included."""
    write_oauth_login(xai_home, access="oauth-token")
    monkeypatch.setenv("XAI_BASE_URL", override)
    sent = stub_net(monkeypatch, replies(responses_payload(GROK_JSON)))

    await xai.XAIWebSearchProvider().search("q", limit=5)

    assert str(sent[0].url) == expected


async def test_an_api_key_may_still_point_anywhere(monkeypatch):
    """Hermes pins the OAuth branch only: a revocable API key against a local proxy is fine."""
    monkeypatch.setenv("XAI_API_KEY", "k")
    monkeypatch.setenv("XAI_BASE_URL", "http://localhost:8080/v1")
    sent = stub_net(monkeypatch, replies(responses_payload(GROK_JSON)))

    await xai.XAIWebSearchProvider().search("q", limit=5)

    assert str(sent[0].url) == "http://localhost:8080/v1/responses"


async def test_the_pin_covers_web_json_not_just_the_process_environment(monkeypatch, xai_home):
    """`provider_env` is wider than Hermes' env reader, so the pin has to be too."""
    write_oauth_login(xai_home, access="oauth-token")
    write_web({"env": {"XAI_BASE_URL": "https://attacker.example/v1"}})
    sent = stub_net(monkeypatch, replies(responses_payload(GROK_JSON)))

    await xai.XAIWebSearchProvider().search("q", limit=5)

    assert str(sent[0].url) == "https://api.x.ai/v1/responses"


async def test_a_rejected_override_is_logged_loudly_enough_to_debug(monkeypatch, xai_home, caplog):
    """Silent fallback is how a misconfigured proxy becomes an afternoon of confusion."""
    write_oauth_login(xai_home)
    monkeypatch.setenv("XAI_BASE_URL", "https://attacker.example/v1")
    stub_net(monkeypatch, replies(responses_payload(GROK_JSON)))

    with caplog.at_level("WARNING", logger=xai.logger.name):
        await xai.XAIWebSearchProvider().search("q", limit=5)

    assert "attacker.example" in caplog.text


@pytest.mark.parametrize(("asked", "sent_limit"), [(500, 100), (0, 1), (-3, 1), ("7", 7)])
async def test_the_limit_is_clamped_to_the_range_the_tool_accepts(monkeypatch, asked, sent_limit):
    monkeypatch.setenv("XAI_API_KEY", "k")
    sent = stub_net(monkeypatch, replies(responses_payload(GROK_JSON)))

    await xai.XAIWebSearchProvider().search("q", limit=asked)

    assert f"Return at most {sent_limit} results" in sent_body(sent[0])["input"][0]["content"]


async def test_a_domain_filter_is_capped_at_five_entries(monkeypatch, xai_home):
    monkeypatch.setenv("XAI_API_KEY", "k")
    write_web_config(xai_home, allowed_domains=[f"d{i}.com" for i in range(10)])
    sent = stub_net(monkeypatch, replies(responses_payload(GROK_JSON)))

    await xai.XAIWebSearchProvider().search("q", limit=5)

    filters = sent_body(sent[0])["tools"][0]["filters"]
    assert filters == {"allowed_domains": [f"d{i}.com" for i in range(5)]}


async def test_excluded_domains_travel_under_their_own_key(monkeypatch, xai_home):
    monkeypatch.setenv("XAI_API_KEY", "k")
    write_web_config(xai_home, excluded_domains=["bad.com", "", 7])
    sent = stub_net(monkeypatch, replies(responses_payload(GROK_JSON)))

    await xai.XAIWebSearchProvider().search("q", limit=5)

    assert sent_body(sent[0])["tools"][0]["filters"] == {"excluded_domains": ["bad.com"]}


async def test_the_configured_model_wins(monkeypatch, xai_home):
    monkeypatch.setenv("XAI_API_KEY", "k")
    write_web_config(xai_home, model="grok-9-fictional")
    sent = stub_net(monkeypatch, replies(responses_payload(GROK_JSON)))

    await xai.XAIWebSearchProvider().search("q", limit=5)

    assert sent_body(sent[0])["model"] == "grok-9-fictional"


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


async def test_no_credentials_fails_before_any_request(monkeypatch):
    sent = stub_net(monkeypatch, replies(responses_payload(GROK_JSON)))

    result = await xai.XAIWebSearchProvider().search("q", limit=5)

    assert result["success"] is False
    assert "XAI_API_KEY" in result["error"]
    assert sent == []


async def test_both_domain_filters_is_refused_without_a_round_trip(monkeypatch, xai_home):
    monkeypatch.setenv("XAI_API_KEY", "k")
    write_web_config(xai_home, allowed_domains=["a.com"], excluded_domains=["b.com"])
    sent = stub_net(monkeypatch, replies(responses_payload(GROK_JSON)))

    result = await xai.XAIWebSearchProvider().search("q", limit=5)

    assert result["success"] is False
    assert "cannot both be set" in result["error"]
    assert sent == []


async def test_an_http_error_body_is_truncated_to_three_hundred_characters(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "k")
    body = "x" * 500
    stub_net(monkeypatch, lambda request: httpx.Response(500, text=body, request=request))

    result = await xai.XAIWebSearchProvider().search("q", limit=5)

    assert result == {
        "success": False,
        "error": f"xAI web search returned HTTP 500: {'x' * 300}",
    }


async def test_a_two_hundred_with_an_error_envelope_is_a_failure_not_an_empty_result(monkeypatch):
    """An empty row list would report success and hide an overloaded or refusing model."""
    monkeypatch.setenv("XAI_API_KEY", "k")
    stub_net(monkeypatch, replies({"error": {"message": "model overloaded", "type": "server_error"}}))

    result = await xai.XAIWebSearchProvider().search("q", limit=5)

    assert result["success"] is False
    assert "model overloaded" in result["error"]


async def test_an_unreachable_endpoint_is_reported_not_raised(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "k")

    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    stub_net(monkeypatch, refuse)

    result = await xai.XAIWebSearchProvider().search("q", limit=5)

    assert result["success"] is False
    assert result["error"].startswith("Could not reach xAI:")


# ---------------------------------------------------------------------------
# The 401 retry: OAuth refreshes once, an API key never does
# ---------------------------------------------------------------------------


async def test_a_401_on_an_oauth_token_refreshes_once_and_retries(monkeypatch, xai_home):
    write_oauth_login(xai_home, access="stale-token", refresh="r1")
    refreshed_from: list[str] = []

    async def fake_refresh(refresh_token, signal=None, *, post_form=None):
        refreshed_from.append(refresh_token)
        return OAuthCredentials(access="fresh-token", refresh="r2", expires=far_future_ms())

    monkeypatch.setattr(xai_oauth, "refresh_xai_token", fake_refresh)

    def handler(request):
        if len(sent) == 1:
            return httpx.Response(401, text="Unauthorized", request=request)
        return httpx.Response(200, json=responses_payload(GROK_JSON), request=request)

    sent = stub_net(monkeypatch, handler)

    result = await xai.XAIWebSearchProvider().search("q", limit=5)

    assert result["success"] is True
    assert [request.headers["authorization"] for request in sent] == [
        "Bearer stale-token",
        "Bearer fresh-token",
    ]
    assert refreshed_from == ["r1"]
    # The rotated pair is persisted, or the next session signs in again for nothing.
    stored = json.loads((xai_home / "auth.json").read_text(encoding="utf-8"))["xai"]
    assert (stored["access"], stored["refresh"]) == ("fresh-token", "r2")


async def test_a_refresh_that_returns_the_same_token_does_not_retry(monkeypatch, xai_home):
    write_oauth_login(xai_home, access="stale-token", refresh="r1")

    async def fake_refresh(refresh_token, signal=None, *, post_form=None):
        return OAuthCredentials(access="stale-token", refresh="r1", expires=far_future_ms())

    monkeypatch.setattr(xai_oauth, "refresh_xai_token", fake_refresh)
    sent = stub_net(monkeypatch, lambda request: httpx.Response(401, text="Unauthorized", request=request))

    result = await xai.XAIWebSearchProvider().search("q", limit=5)

    assert result["success"] is False
    assert "401" in result["error"]
    assert len(sent) == 1


async def test_a_failed_refresh_reports_the_401_rather_than_itself(monkeypatch, xai_home):
    write_oauth_login(xai_home)

    async def fake_refresh(refresh_token, signal=None, *, post_form=None):
        raise RuntimeError("refresh token rejected")

    monkeypatch.setattr(xai_oauth, "refresh_xai_token", fake_refresh)
    sent = stub_net(monkeypatch, lambda request: httpx.Response(401, text="Unauthorized", request=request))

    result = await xai.XAIWebSearchProvider().search("q", limit=5)

    assert result["success"] is False
    assert "401" in result["error"]
    assert len(sent) == 1


async def test_a_401_on_an_api_key_never_retries(monkeypatch):
    """An API key cannot be refreshed; a second identical request only burns quota."""
    monkeypatch.setenv("XAI_API_KEY", "sk-dead")
    refreshed: list[str] = []

    async def fake_refresh(refresh_token, signal=None, *, post_form=None):
        refreshed.append(refresh_token)
        raise AssertionError("an API-key credential must never be refreshed")

    monkeypatch.setattr(xai_oauth, "refresh_xai_token", fake_refresh)
    sent = stub_net(monkeypatch, lambda request: httpx.Response(401, text="Unauthorized", request=request))

    result = await xai.XAIWebSearchProvider().search("q", limit=5)

    assert result == {"success": False, "error": "xAI web search returned HTTP 401: Unauthorized"}
    assert len(sent) == 1
    assert refreshed == []


async def test_a_stored_login_beats_the_environment_key(monkeypatch, xai_home):
    """Hermes' resolution order: an OAuth grant first, XAI_API_KEY only as the fallback."""
    monkeypatch.setenv("XAI_API_KEY", "sk-should-not-be-used")
    write_oauth_login(xai_home, access="oauth-token")
    sent = stub_net(monkeypatch, replies(responses_payload(GROK_JSON)))

    await xai.XAIWebSearchProvider().search("q", limit=5)

    assert sent[0].headers["authorization"] == "Bearer oauth-token"


async def test_a_key_in_web_json_is_honoured_when_nothing_is_exported(monkeypatch, xai_home):
    write_web({"env": {"XAI_API_KEY": "from-file"}})
    sent = stub_net(monkeypatch, replies(responses_payload(GROK_JSON)))

    await xai.XAIWebSearchProvider().search("q", limit=5)

    assert sent[0].headers["authorization"] == "Bearer from-file"

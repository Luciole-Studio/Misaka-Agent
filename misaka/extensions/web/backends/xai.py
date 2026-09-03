"""xAI Web Search (Grok): an LLM in a trench coat, wired to the provider contract.

Ported from Hermes' ``plugins/web/xai/provider.py``, with the credential half of
``tools/xai_http.py`` (``has_xai_credentials`` / ``resolve_xai_http_credentials``)
rebuilt on MISAKA's own auth store instead of Hermes' ``auth.json`` + credential pool.

Grok does the searching server-side through xAI's agentic ``web_search`` tool on the
Responses API; we ask it to hand back the top rows as JSON so this backend can produce
the same ``{title, url, description, position}`` shape every other backend produces.
Reference: https://docs.x.ai/developers/tools/web-search

Search only. Hermes pairs it with Firecrawl / Tavily for ``web_extract``; MISAKA reaches
pages through ``web_fetch``, so there is nothing to pair with.

Config keys this provider responds to (``~/.misaka/web.json``)::

    "search_backend": "xai"      # explicit per-capability
    "backend": "xai"             # shared fallback
    "xai": {
      "model": "grok-build-0.1",         # the reasoning model web_search requires
      "timeout": 90,                     # seconds
      "allowed_domains": ["x.ai"],       # max 5 -- mutually exclusive with the next key
      "excluded_domains": ["bad.com"]    # max 5 -- mutually exclusive with the previous
    }

Env vars::

    XAI_API_KEY=...     # https://console.x.ai -- the fallback when no OAuth login exists
    XAI_BASE_URL=...    # optional override of https://api.x.ai/v1; pinned to the xAI
                        # origin whenever the bearer is an OAuth token -- see
                        # :func:`_inference_base_url`

**Credentials, and where this diverges from Hermes.** Hermes resolves through its own
credential pool, which supports an unconditional ``force_refresh=True``. MISAKA's
:class:`~misaka.core.auth_storage.AuthStorage` has no such flag: every refresh it owns --
``getApiKey``, ``refreshOAuthTokenWithLock`` -- is gated on
``oauthCredentialsExpireSoon``, so asking it to refresh a token the store still believes
in is a no-op. That gate is exactly the case a 401 exists to report, so
:func:`_force_refresh_oauth_token` drives the registered xAI OAuth provider directly and
persists the rotated pair through ``AuthStorage.set`` -- the same ``{"type": "oauth", ...}``
envelope ``refreshOAuthTokenWithLock`` writes, under the same file lock. What it does not
inherit is that method's read-under-lock: a second session rotating the refresh token in
the same instant wins, and this call degrades to the 401 it already had.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from misaka.ai.utils.oauth import OAuthCredentials, getOAuthProvider
from misaka.config import get_auth_path
from misaka.core.auth_storage import AuthStorage
from misaka.extensions.web.config import provider_env, web_config
from misaka.extensions.web.keyless import CLIENT_NAME
from misaka.extensions.web.provider import WebSearchProvider

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "grok-build-0.1"
DEFAULT_TIMEOUT = 90
DEFAULT_BASE_URL = "https://api.x.ai/v1"

# The provider id the OAuth device-code flow in `misaka.ai.utils.oauth.xai` stores under,
# which is also the id `/login xai` writes. One name, so a chat login and a web search
# read the same credential.
_AUTH_PROVIDER_ID = "xai"

# xAI's hard cap on allowed_domains / excluded_domains. Trimmed silently rather than
# passed through, because the API answers an over-long list with a 400 and the user's
# sixth domain is not worth failing a search over.
_MAX_DOMAIN_FILTERS = 5

# How much text preceding a citation marker becomes that row's description in the
# annotations fallback. Grok writes prose there, not summaries, so this is a sentence or
# two of context rather than anything that deserves the whole message.
_ANNOTATION_CONTEXT_CHARS = 200

# Match the JSON object Grok is asked to emit. Greedy and tolerant of leading/trailing
# prose, because reasoning models occasionally narrate before the JSON block even when
# explicitly asked not to.
_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}", re.MULTILINE)

_NO_CREDENTIALS_ERROR = (
    "No xAI credentials found. Run `/login xai` to sign in with SuperGrok or "
    "X Premium, or set XAI_API_KEY."
)

_BOTH_DOMAIN_FILTERS_ERROR = (
    "web.xai.allowed_domains and web.xai.excluded_domains cannot both be set "
    "(xAI restriction)."
)


def _auth_path() -> str:
    """Where the credential store lives.

    Its own function rather than a call to ``get_auth_path()`` at each of the two use
    sites, because those two sites must never disagree: :meth:`is_available` reads the
    file raw and :func:`_resolve_credentials` opens it through ``AuthStorage``, and a
    probe that says "configured" about a different file than the search then opens is
    worse than either answer alone. It is also the seam the tests move.
    """
    return get_auth_path()


def _client(timeout: float) -> httpx.AsyncClient:
    """Build the HTTP client for one search.

    A fresh client per call, no shared pool -- the house rule for every backend here. It
    is a function so the tests can mount an ``httpx.MockTransport`` without reaching into
    ``httpx`` itself and disturbing whatever else the session has in flight.
    """
    return httpx.AsyncClient(timeout=timeout)


def _xai_config() -> dict[str, Any]:
    """Read the ``xai`` section of ``web.json`` (``{}`` on miss).

    Hermes' ``_load_xai_web_config``, reading ``web.xai`` from ``config.yaml``.
    """
    section = web_config().get("xai")
    return section if isinstance(section, dict) else {}


def _coerce_domain_list(value: Any) -> list[str]:
    """Coerce a config value to a clean list of at most :data:`_MAX_DOMAIN_FILTERS` domains."""
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            cleaned.append(item.strip())
        if len(cleaned) >= _MAX_DOMAIN_FILTERS:
            break
    return cleaned


async def _resolve_credentials() -> tuple[str, AuthStorage | None]:
    """Resolve the bearer token, and the store to refresh it through if it came from OAuth.

    Hermes' ``resolve_xai_http_credentials`` order: an xAI OAuth login first, the API key
    second. ``AuthStorage.getApiKey`` covers both stored shapes (an OAuth grant from
    ``/login xai``, an API key saved through the same flow) and refreshes an OAuth token
    that is already inside the five-minute expiry window, which is why the 401 path below
    is about revocation rather than ordinary expiry.

    The second element is the OAuth marker: a store handle when the token came from a
    stored OAuth grant, ``None`` when it is an API key. An API key cannot be refreshed,
    so it must never trigger the retry -- an immediate second request with the same
    rejected key only burns quota.

    Every failure inside the store degrades to ``XAI_API_KEY`` rather than raising: a
    corrupted or half-written ``auth.json`` must not cost a user their working key.
    """
    try:
        storage = await asyncio.to_thread(AuthStorage.create, _auth_path())
        credential = storage.get(_AUTH_PROVIDER_ID)
        is_oauth = isinstance(credential, dict) and credential.get("type") == "oauth"
        token = str(await storage.getApiKey(_AUTH_PROVIDER_ID) or "").strip()
        if token:
            return token, storage if is_oauth else None
    except Exception as exc:  # noqa: BLE001 - a broken store must not cost the API-key path
        logger.debug("xAI auth store unusable, falling back to XAI_API_KEY: %s", exc)
    # `provider_env` is wider than the store's own env fallback: it also reads the `env`
    # section of web.json, which is where a user who never exported anything puts a key.
    return provider_env("XAI_API_KEY"), None


def _inference_base_url(*, pin_origin: bool) -> str:
    """Resolve the inference origin from ``XAI_BASE_URL``, pinned to xAI for an OAuth bearer.

    Hermes' ``_xai_validate_inference_base_url`` (``hermes_cli/auth.py``), reached from
    ``tools/xai_http.py`` on the ``xai-oauth`` branch only. The threat it names is this
    one: the xAI OAuth bearer is a long-lived credential tied to a SuperGrok / X Premium
    subscription, and a tampered ``.env``, a hostile shell init, or -- here, where the
    override is wider than Hermes' -- a ``web.json`` written by other tooling could set
    ``XAI_BASE_URL=https://attacker.example/v1`` and ship that bearer to a third party on
    every search, silently. ``http://`` would ship it in cleartext as well.

    So when the token came from a stored OAuth grant, the override has to be HTTPS on
    ``x.ai`` or a ``*.x.ai`` subdomain (staging hosts and xAI-side proxies still work); a
    rejected override warns and falls back to :data:`DEFAULT_BASE_URL` rather than raising,
    because a bad env var should not be able to take the backend down.

    *pin_origin* is false for the ``XAI_API_KEY`` path, which Hermes deliberately leaves
    unpinned: an API key is a lower-value, per-user-revocable credential, and pointing one
    at a local proxy or a gateway is an ordinary thing to want.
    """
    candidate = provider_env("XAI_BASE_URL").rstrip("/")
    if not candidate:
        return DEFAULT_BASE_URL
    if not pin_origin:
        return candidate
    try:
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower()
    except ValueError as exc:  # a malformed authority, e.g. an unclosed IPv6 bracket
        logger.warning(
            "Ignoring malformed XAI_BASE_URL %r (%s); using %s instead.",
            candidate,
            exc,
            DEFAULT_BASE_URL,
        )
        return DEFAULT_BASE_URL
    if parsed.scheme != "https" or not host or (host != "x.ai" and not host.endswith(".x.ai")):
        logger.warning(
            "Refusing XAI_BASE_URL %r for an xAI OAuth login -- the bearer is only valid "
            "against xAI's own API and sending it elsewhere would leak it. Falling back "
            "to %s. (Set XAI_API_KEY instead to use a custom endpoint.)",
            candidate,
            DEFAULT_BASE_URL,
        )
        return DEFAULT_BASE_URL
    return candidate


async def _force_refresh_oauth_token(storage: AuthStorage, rejected: str) -> str:
    """Rotate the stored xAI OAuth token unconditionally; ``""`` when that changes nothing.

    Hermes forces a refresh here because a 401 closes two gaps its proactive expiry check
    cannot: an opaque (non-JWT) access token whose expiry cannot be read at all, and
    mid-window revocation -- an admin revoke, a refresh-token rotation elsewhere, or clock
    skew -- on a token whose recorded expiry is still comfortably in the future. MISAKA
    records an explicit ``expires``, so the first gap is closed already; the second is the
    whole reason this exists, and it is precisely the case ``AuthStorage``'s own
    expiry-gated refresh declines to serve (see the module docstring).

    Returns ``""`` on every failure and on a refresh that handed back the same token,
    so the caller reports the original 401 instead of replaying the request.
    """
    try:
        credential = storage.get(_AUTH_PROVIDER_ID)
        if not isinstance(credential, dict) or credential.get("type") != "oauth":
            return ""
        provider = getOAuthProvider(_AUTH_PROVIDER_ID)
        if provider is None:
            return ""
        stored = OAuthCredentials.model_validate(
            {key: value for key, value in credential.items() if key != "type"}
        )
        refreshed = await provider.refreshToken(stored)
        await asyncio.to_thread(
            storage.set,
            _AUTH_PROVIDER_ID,
            {"type": "oauth", **refreshed.model_dump(exclude_none=False)},
        )
        token = str(provider.getApiKey(refreshed) or "").strip()
    except Exception as exc:  # noqa: BLE001 - a failed refresh reports the 401, not itself
        logger.warning("xAI web search OAuth refresh after 401 failed: %s", exc)
        return ""
    return token if token and token != rejected else ""


class XAIWebSearchProvider(WebSearchProvider):
    """Search-only provider backed by xAI's agentic Web Search tool.

    Sends a structured prompt to Grok with ``tools=[{"type": "web_search"}]`` enabled and
    asks it to return the top *limit* results as JSON. Falls back to the Responses API
    annotations and then its ``citations`` list if Grok ignores the JSON schema
    instruction (rare, but cheap insurance).

    Trust model
    -----------
    Unlike index-backed providers (Brave / Tavily / Exa) which return verbatim
    search-engine results, this backend is an LLM in a trench coat: Grok decides which
    URLs to surface, generates the titles and descriptions itself, and is influenced by
    the *content of the query*. A maliciously crafted query (e.g. injected via untrusted
    upstream input the agent picked up) can in principle steer Grok into emitting
    attacker-chosen URLs. Callers that pipe untrusted text directly into ``web_search``
    should treat returned URLs the same way they would treat any model-generated link --
    validate before fetching.
    """

    @property
    def name(self) -> str:
        return "xai"

    @property
    def display_name(self) -> str:
        return "xAI Web Search (Grok)"

    def supports_extract(self) -> bool:
        """No extract capability -- Grok searches, it does not hand back page content.

        Grok answers with an index's results, not with a page it rendered; there is
        nothing here to extract a URL's text with. Hermes' xai provider says the same
        (``plugins/web/xai/provider.py:213``).
        """
        return False

    def is_available(self) -> bool:
        """Cheap probe: ``XAI_API_KEY`` is set, or the auth store already holds an ``xai`` entry.

        Deliberately not :func:`_resolve_credentials`. This runs while a session is being
        assembled and on every availability scan, so it must not open the auth store's
        file lock and must never reach the network -- and ``AuthStorage`` does both on
        construction (``reload`` takes the lock) and on OAuth resolution (an expiring
        token is refreshed over the network). A raw read of the JSON document answers the
        only question this needs to answer. Token freshness is :meth:`search`'s problem.

        Every failure is False: a corrupted ``auth.json`` must not be able to abort an
        availability walk that is only trying to find out which backends exist.
        """
        if provider_env("XAI_API_KEY"):
            return True
        try:
            with open(_auth_path(), encoding="utf-8-sig") as handle:
                store = json.load(handle)
        except Exception as exc:  # noqa: BLE001 - a missing or broken store means "no"
            logger.debug("xAI availability probe could not read the auth store: %s", exc)
            return False
        return bool(isinstance(store, dict) and store.get(_AUTH_PROVIDER_ID))

    def is_keyless_available(self) -> bool:
        """Never. xAI has no free tier and is not a member of the keyless ring."""
        return False

    async def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Execute a Grok-backed web search."""
        token, oauth_storage = await _resolve_credentials()
        if not token:
            return {"success": False, "error": _NO_CREDENTIALS_ERROR}

        # Clamp to the range the tool above accepts rather than to something smaller, so
        # an explicit limit is never silently downgraded. Grok happily produces longer
        # lists; cost scales with the requested count through reasoning tokens, but that
        # is the caller's call to make.
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 5
        limit = max(1, min(limit, 100))

        cfg = _xai_config()
        model = cfg.get("model") if isinstance(cfg.get("model"), str) else DEFAULT_MODEL
        model = model.strip() or DEFAULT_MODEL
        try:
            timeout = float(cfg.get("timeout", DEFAULT_TIMEOUT))
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT

        allowed = _coerce_domain_list(cfg.get("allowed_domains"))
        excluded = _coerce_domain_list(cfg.get("excluded_domains"))
        if allowed and excluded:
            # xAI rejects this combination outright, so it is answered here rather than
            # spent on a round trip that comes back a 400.
            return {"success": False, "error": _BOTH_DOMAIN_FILTERS_ERROR}

        web_search_tool: dict[str, Any] = {"type": "web_search"}
        if allowed:
            web_search_tool["filters"] = {"allowed_domains": allowed}
        elif excluded:
            web_search_tool["filters"] = {"excluded_domains": excluded}

        payload = {
            "model": model,
            "input": [{"role": "user", "content": _build_prompt(query, limit)}],
            "tools": [web_search_tool],
            # Drop inline citation markdown: the JSON block has to stay clean, and URLs
            # are read from annotations / citations separately anyway.
            "include": ["no_inline_citations"],
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": CLIENT_NAME,
        }

        # `oauth_storage is not None` is exactly Hermes' `provider == "xai-oauth"`: the
        # bearer about to go on the wire is a subscription OAuth token, so its destination
        # is pinned to the xAI origin.
        base_url = _inference_base_url(pin_origin=oauth_storage is not None)
        url = f"{base_url}/responses"
        logger.info(
            "xAI web search via %s: '%s' (limit=%d, model=%s)", base_url, query, limit, model
        )

        response: httpx.Response | None = None
        async with _client(timeout) as client:
            for attempt in range(2):
                try:
                    response = await client.post(url, headers=headers, json=payload)
                except httpx.RequestError as exc:
                    logger.warning("xAI web search request error: %s", exc)
                    return {"success": False, "error": f"Could not reach xAI: {exc}"}
                if response.status_code < 400:
                    break
                if response.status_code == 401 and attempt == 0 and oauth_storage is not None:
                    logger.info(
                        "xAI web search got 401 on first attempt; forcing OAuth "
                        "refresh and retrying once.",
                    )
                    refreshed = await _force_refresh_oauth_token(oauth_storage, token)
                    if refreshed:
                        token = refreshed
                        headers["Authorization"] = f"Bearer {token}"
                        continue
                    # The refresh failed or handed back the same token; there is nothing
                    # a second request would do differently. Fall through to the error.
                body = (response.text or "")[:300]
                logger.warning("xAI web search HTTP %d: %s", response.status_code, body)
                return {
                    "success": False,
                    "error": (
                        f"xAI web search returned HTTP {response.status_code}: {body}"
                    ).rstrip(),
                }

        if response is None:
            # Defensive: both attempts would have to leave the loop without a response.
            return {"success": False, "error": "xAI web search produced no response"}

        try:
            data = response.json()
        except ValueError as exc:
            logger.warning("xAI web search bad JSON: %s", exc)
            return {
                "success": False,
                "error": "Could not parse xAI Responses API reply as JSON",
            }

        # xAI's Responses surface sometimes answers HTTP 200 with an error envelope
        # (model overloaded, content-policy refusal). Without this check the extractor
        # below produces an empty list and the call reports success-with-no-rows, masking
        # a real failure the agent should see and decide whether to retry.
        api_error = data.get("error") if isinstance(data, dict) else None
        if isinstance(api_error, dict):
            message = api_error.get("message") or api_error.get("code") or "unknown error"
            logger.warning("xAI web search returned error envelope: %s", message)
            return {"success": False, "error": f"xAI returned an error: {message}"}

        # A successful call with no usable rows stays a success with an empty list, the
        # way every other backend reports zero hits: the model decides whether to retry.
        return {"success": True, "data": {"web": _extract_results(data, limit=limit)}}

    def setup_hint(self) -> dict[str, Any]:
        """Hermes' ``get_setup_schema``, minus its ``post_setup`` hook.

        Hermes delegates auth to a shared ``xai_grok`` post-setup prompt that every xAI
        service (image, TTS, search) reuses. MISAKA has no picker and no post-setup hooks,
        so the OAuth half is named in the tag and the key half is an ordinary env var.
        """
        return {
            "name": "xAI Web Search (Grok)",
            "badge": "paid",
            "tag": (
                "Agentic web search through Grok's web_search tool. Signs in with "
                "`/login xai` (SuperGrok / X Premium) or uses XAI_API_KEY."
            ),
            "env_vars": [
                {
                    "key": "XAI_API_KEY",
                    "prompt": "xAI API key (or sign in with `/login xai` instead)",
                    "url": "https://console.x.ai",
                },
            ],
        }


def _build_prompt(query: str, limit: int) -> str:
    """Compose the prompt that asks Grok to act as a search engine.

    A JSON object rather than a bare array, so :data:`_JSON_BLOCK_RE` can match it with
    one cheap pattern; prose, markdown fences and inline-citation links are all forbidden
    explicitly, because each of them is a way the payload stops being parseable.
    """
    return (
        "Use the web_search tool to find current information for the query below, "
        "then respond with ONLY a single JSON object — no prose, no markdown "
        "fences, no inline citation links — matching this exact schema:\n\n"
        '{"results": [{"title": "string", "url": "string", '
        '"description": "1-2 sentence summary"}]}\n\n'
        f"Return at most {limit} results, ordered by relevance, with absolute "
        "https:// URLs. If no usable results exist, return "
        '{"results": []}.\n\n'
        f"Query: {query}"
    )


def _extract_results(response_data: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
    """Pull ``[{title, url, description, position}, ...]`` out of a Responses-API reply.

    Three tiers, cheapest and most faithful first:

    1. the JSON object Grok was asked for, inside an ``output_text`` block;
    2. the ``url_citation`` annotations on those blocks, when Grok answered in prose;
    3. the top-level ``citations`` list of bare URLs, which carries no titles at all.

    Tier 2 only wins when it actually produced rows. xAI emits nothing but
    ``url_citation`` today, but an annotation type nobody here recognises would otherwise
    short-circuit to an empty list and hide real data sitting in ``citations``.
    """
    text_blocks, annotations = _collect_output_text(response_data)

    for block in text_blocks:
        parsed = _try_parse_json_results(block, limit=limit)
        if parsed:
            return parsed

    if annotations:
        from_annotations = _results_from_annotations(
            annotations, "\n".join(text_blocks), limit=limit
        )
        if from_annotations:
            return from_annotations

    citations = response_data.get("citations") or []
    if isinstance(citations, list):
        return [
            {"title": "", "url": str(url), "description": "", "position": index + 1}
            for index, url in enumerate(citations[:limit])
            if isinstance(url, str) and url.strip()
        ]
    return []


def _collect_output_text(
    response_data: dict[str, Any],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Return ``(text_blocks, annotations)`` from the reply's ``output`` messages."""
    text_blocks: list[str] = []
    annotations: list[dict[str, Any]] = []
    output = response_data.get("output")
    if not isinstance(output, list):
        return text_blocks, annotations

    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for chunk in content:
            if not isinstance(chunk, dict) or chunk.get("type") != "output_text":
                continue
            text = chunk.get("text")
            if isinstance(text, str) and text.strip():
                text_blocks.append(text)
            chunk_annotations = chunk.get("annotations")
            if isinstance(chunk_annotations, list):
                annotations.extend(a for a in chunk_annotations if isinstance(a, dict))
    return text_blocks, annotations


def _try_parse_json_results(text: str, *, limit: int) -> list[dict[str, Any]] | None:
    """Parse a JSON object with a ``results`` array out of *text*.

    Returns the normalised rows, or ``None`` when the block holds no JSON object with a
    ``results`` list. The whole string is tried first -- the cheap path, and the one that
    hits whenever Grok obeyed -- before falling back to the greedy block match.
    """
    candidates = [text]
    match = _JSON_BLOCK_RE.search(text)
    if match and match.group(0) != text:
        candidates.append(match.group(0))

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(parsed, dict):
            continue
        results = parsed.get("results")
        if not isinstance(results, list):
            continue
        normalized: list[dict[str, Any]] = []
        for row in results[:limit]:
            if not isinstance(row, dict):
                continue
            url = str(row.get("url", "")).strip()
            if not url:
                continue
            normalized.append(
                {
                    "title": str(row.get("title", "")).strip(),
                    "url": url,
                    "description": str(row.get("description", "")).strip(),
                    # Numbered over the kept rows, not the raw index, so a row dropped
                    # for having no URL does not leave a hole in what the agent reads.
                    "position": len(normalized) + 1,
                }
            )
        if normalized:
            return normalized
    return None


def _results_from_annotations(
    annotations: list[dict[str, Any]], joined_text: str, *, limit: int
) -> list[dict[str, Any]]:
    """Best-effort rows from ``url_citation`` annotations when the JSON tier found nothing.

    A citation's ``title`` is just its integer label, so it is dropped rather than
    surfaced; the description is the text immediately preceding the marker, which is the
    sentence the citation was attached to. Deduped by URL, because Grok cites the same
    page several times in one answer.
    """
    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    for annotation in annotations:
        if annotation.get("type") != "url_citation":
            continue
        url = str(annotation.get("url", "")).strip()
        if not url or url in seen:
            continue
        seen.add(url)

        description = ""
        start = annotation.get("start_index")
        end = annotation.get("end_index")
        if (
            isinstance(start, int)
            and isinstance(end, int)
            and 0 <= start < end <= len(joined_text)
        ):
            # Hermes re-trims this to 200 characters afterwards; a 200-character slice
            # that has only been stripped cannot exceed 200, so that step is dropped.
            description = joined_text[max(0, start - _ANNOTATION_CONTEXT_CHARS) : start].strip()

        results.append(
            {"title": "", "url": url, "description": description, "position": len(results) + 1}
        )
        if len(results) >= limit:
            break
    return results

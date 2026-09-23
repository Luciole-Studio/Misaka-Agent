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

OAuth expiry and rejected-token refresh both use AuthStorage's authoritative
read/refresh/write lock. A newer login is adopted; a deleted grant stays deleted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from misaka.ai.utils.oauth.xai import with_http_options
from misaka.config import home
from misaka.core.auth_storage import AuthStorage
from misaka.core.web.accounting import account_call
from misaka.core.web.config import provider_env, web_config
from misaka.core.web.keyless import CLIENT_NAME
from misaka.core.web.network import api_network_options
from misaka.core.web.provider import WebSearchProvider
from misaka.core.web.runtime import api_client
from misaka.core.web.scope import current_scope

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "grok-build-0.1"
DEFAULT_TIMEOUT = 90
DEFAULT_BASE_URL = "https://api.x.ai/v1"

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

    Its own function rather than a lookup at each of the two use
    sites, because those two sites must never disagree: :meth:`is_available` reads the
    file raw and :func:`_resolve_credentials` opens it through ``AuthStorage``, and a
    probe that says "configured" about a different file than the search then opens is
    worse than either answer alone. It is also the seam the tests move.
    """
    profile = current_scope().profile_dir
    return str(Path(profile) / "auth.json" if profile is not None else home.path("auth"))


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


@dataclass
class OAuthAccount:
    storage: AuthStorage
    name: str | None


def _identity(token):
    return hashlib.sha256(repr((_auth_path(), token)).encode()).hexdigest()


def _quarantine(token):
    scope = current_scope()
    now = time.monotonic()
    with scope.lock:
        for key, expiry in tuple(scope.rejected_credentials.items()):
            if expiry <= now:
                del scope.rejected_credentials[key]
        if len(scope.rejected_credentials) >= 128:
            scope.rejected_credentials.pop(next(iter(scope.rejected_credentials)))
        scope.rejected_credentials[_identity(token)] = now + 300


async def _resolve_credentials(*, prefer_api_key=False, excluded=()) -> tuple[str, OAuthAccount | None]:
    """Key-first for X, OAuth-first for Web; only the rejected account is refreshed."""
    explicit = provider_env("XAI_API_KEY")
    if prefer_api_key and explicit:
        return explicit, None
    try:
        from misaka.utils.async_lifecycle import run_in_thread

        storage = await run_in_thread(AuthStorage.create, _auth_path())
        for key, credential in storage.getAll().items():
            if key != "xai" and not key.startswith("xai:"):
                continue
            if key in excluded or not isinstance(credential, dict):
                continue
            if credential.get("type") != "oauth":
                if key == "xai":
                    token = await storage.getApiKey("xai")
                    if token:
                        return token, None
                continue
            from misaka.core.web.config import remember_secret
            remember_secret(credential.get("access"))
            remember_secret(credential.get("refresh"))
            rejected = current_scope().rejected_credentials.get(_identity(str(credential.get("access", ""))), 0)
            if rejected > time.monotonic():
                continue
            account = OAuthAccount(storage, key.partition(":")[2] or None)
            try:
                with with_http_options(api_network_options):
                    refreshed = await storage.refreshOAuthTokenWithLock("xai", account=account.name)
                token = str(refreshed["apiKey"] or "").strip() if refreshed else ""
            except Exception:  # noqa: BLE001 - tool or transport boundary reports the failure
                _quarantine(str(credential.get("access", "")))
                continue
            if token:
                return token, account
    except Exception:  # noqa: BLE001, S110 - tool or transport boundary reports the failure
        pass
    return explicit, None


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


async def _force_refresh_oauth_token(account: OAuthAccount, rejected: str) -> str:
    try:
        with with_http_options(api_network_options):
            refreshed = await account.storage.refreshOAuthTokenWithLock(
                "xai", rejected_api_key=rejected, account=account.name
            )
        token = str(refreshed["apiKey"] or "").strip() if refreshed else ""
    except Exception as error:  # noqa: BLE001 - tool or transport boundary reports the failure
        logger.warning("xAI OAuth rejected-token refresh failed (%s)", type(error).__name__)
        return ""
    return token if token and token != rejected else ""


async def post_responses(payload, query, *, operation="web_search", timeout=90, retries=0,
                         prefer_api_key=False):
    """One HTTP attempt per accounting event; account rotation is separate from 5xx retry."""
    token, account = await _resolve_credentials(prefer_api_key=prefer_api_key)
    if not token:
        raise ValueError(_NO_CREDENTIALS_ERROR)
    excluded, tried, refreshed_accounts = set(), set(), set()
    network_attempt = 0
    while True:
        from misaka.core.web.config import remember_secret
        remember_secret(token)
        base = _inference_base_url(pin_origin=account is not None or prefer_api_key)
        url = f"{base}/responses"
        identity = (account.name if account is not None else "api_key", _identity(token))
        tried.add(identity)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": CLIENT_NAME}
        try:
            # Pool identity includes account and bearer. Refresh never inherits cookies
            # from another account, and the old pool drains its current borrowers.
            async with api_client("xai", url, identity, timeout=timeout) as client, account_call(operation, "xai", query):
                response = await client.post(url, headers=headers, json=payload)
        except (httpx.ReadTimeout, httpx.ConnectError) as error:
            if network_attempt >= retries:
                raise ValueError(f"Could not reach xAI: {type(error).__name__}") from error
        else:
            if response.status_code == 401 and account is not None:
                key = "xai" + (":" + account.name if account.name else "")
                refreshed = ""
                if key not in refreshed_accounts:
                    refreshed_accounts.add(key)
                    refreshed = await _force_refresh_oauth_token(account, token)
                if refreshed and (account.name, _identity(refreshed)) not in tried:
                    token = refreshed
                    continue
                _quarantine(token)
                excluded.add(key)
                new_token, new_account = await _resolve_credentials(excluded=excluded)
                new_identity = (new_account.name if new_account is not None else "api_key", _identity(new_token))
                if new_token and new_identity not in tried and len(tried) < 64:
                    token, account = new_token, new_account
                    continue
            if response.status_code < 400:
                return response, "oauth" if account is not None else "api_key"
            if response.status_code < 500 or network_attempt >= retries:
                # Never reflect a rejected bearer from a vendor/proxy error response.
                body = response.text[:300 if operation == "web_search" else 500].replace(token, "<redacted>")
                raise ValueError(f"xAI {operation.replace('_', ' ')} returned HTTP {response.status_code}: {body}")
        network_attempt += 1
        await asyncio.sleep(min(5.0, 1.5 * network_attempt))


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
        except Exception:  # noqa: BLE001 - a missing or broken store means "no"
            return False
        return bool(isinstance(store, dict) and any(
            (key == "xai" or key.startswith("xai:")) and isinstance(value, dict)
            and (value.get("access") or value.get("key")) for key, value in store.items()))

    def is_keyless_available(self) -> bool:
        """Never. xAI has no free tier and is not a member of the keyless ring."""
        return False

    async def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Execute a Grok-backed web search."""
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
        try:
            response, _source = await post_responses(payload, query, timeout=timeout)
        except (ValueError, httpx.RequestError) as error:
            return {"success": False, "error": str(error)}

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

    def get_setup_schema(self) -> dict[str, Any]:
        """CLI setup uses the existing AuthStorage/device-code login, just like /login."""
        return {
            "name": "xAI Web Search (Grok)",
            "badge": "paid",
            "post_setup": "xai_grok",
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

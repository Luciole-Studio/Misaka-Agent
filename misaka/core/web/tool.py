"""The ``web_search`` tool: schema, dispatch, memo, and the untrusted fence.

Ported from Hermes' ``tools/web_tools.py`` -- ``web_search_tool`` (838-1048) and the
``WEB_SEARCH_SCHEMA`` + ``registry.register`` block (1655-1676). The provider layer below
this file answers "which backend, and what did it say"; this file answers "what does the
model see".

The schema dict is a verbatim copy of Hermes'. It is kept as a literal rather than
generated from a pydantic model on purpose: a model would rewrite the wording, reorder
the keys, and add ``title`` fields, and that description -- the operator sentence, the
1-100 range, the default of 5 -- is the part of the port a model actually reads.

``_truncate_with_footer`` and ``_store_full_text`` (web_tools.py 635-810) are not here
because they are not this tool's: they are ``web_extract``'s per-page character budget,
and they live in :mod:`misaka.core.web.extract` beside the tool that needs them. A
search result is a title, a URL and a two-line description; nothing on this path ever
holds page text. What web_search does have in Hermes is the registry's
``max_result_size_chars=100_000``, and that is ported below.
"""

from __future__ import annotations

import json
from typing import Any

from misaka.core.extensions.types import ToolDefinition
from misaka.core.platform.prompt_guard import untrusted
from misaka.core.tools._common import run_with_abort
from misaka.core.tools._web.single_flight import single_flight
from misaka.core.web import cache, debug
from misaka.core.web.config import redact_secrets, redact_values
from misaka.core.web.dispatch import memo_identity, resolve_provider
from misaka.core.web.dispatch import web_search as dispatch_search
from misaka.utils.values import signal_aborted

# Verbatim from Hermes tools/web_tools.py:1655-1676. Do not reword: the operator sentence
# is what makes a model try `site:` instead of giving up on a domain-scoped question.
WEB_SEARCH_SCHEMA = {
    "name": "web_search",
    "description": "Search the web for information. Returns up to 5 results by default with titles, URLs, and descriptions. The query is passed through to the configured backend, so operators such as site:domain, filetype:pdf, intitle:word, -term, and \"exact phrase\" may work when the backend supports them.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query to look up on the web. You may include backend-supported operators such as site:example.com, filetype:pdf, intitle:word, -term, or \"exact phrase\"."
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return. Defaults to 5.",
                "minimum": 1,
                "maximum": 100,
                "default": 5
            }
        },
        "required": ["query"]
    }
}

# Hermes' registry entry carries ``max_result_size_chars=100_000`` for this tool, and its
# ``tool_result_storage`` layer spills anything larger to a file, handing the model a
# pointer instead of the body. MISAKA has no such layer and no per-tool ceiling at all, so
# the number is enforced here -- see :func:`_bound_result_size` for what it does instead.
MAX_RESULT_SIZE_CHARS = 100_000

# Hermes' tools/registry.py bounds every tool error body before it reaches the model, so
# a raw interpolated exception cannot bloat history across retries. Same numbers.
_MAX_TOOL_ERROR_CHARS = 2048
_TOOL_ERROR_TRUNCATION_MARKER = "… [truncated]"


def tool_error(message: object, **extra: Any) -> str:
    """Return a JSON error string, the way Hermes' ``tools.registry.tool_error`` does."""
    text = str(message)
    if len(text) > _MAX_TOOL_ERROR_CHARS:
        text = text[:_MAX_TOOL_ERROR_CHARS] + _TOOL_ERROR_TRUNCATION_MARKER
    result: dict[str, Any] = {"error": text}
    if extra:
        result.update(extra)
    return json.dumps(result, ensure_ascii=False)


def _bound_error_field(response: dict[str, Any]) -> dict[str, Any]:
    """Trim an oversized ``error`` string a provider put in its own failure response.

    :func:`tool_error` caps the errors this module raises, but a backend that returns
    ``{"success": False, "error": <vendor body>}`` bypasses it -- and the vendor body is
    whatever the endpoint felt like sending, which for a proxy error page or an HTML
    rate-limit notice can be megabytes. Hermes bounds this at its dispatch boundary
    (``tools/registry.py:_bound_json_error_result``); MISAKA has no such boundary, so the
    cap lands here, on the same number, before the response is rendered.
    """
    error = response.get("error")
    if isinstance(error, str) and len(error) > _MAX_TOOL_ERROR_CHARS:
        response = dict(response)
        response["error"] = error[:_MAX_TOOL_ERROR_CHARS] + _TOOL_ERROR_TRUNCATION_MARKER
    return response


def _oversized_error(size: int) -> str:
    """A parseable refusal for a response too large to trim into shape.

    The hard cut this replaces spliced a JSON document mid-string and appended a marker,
    handing the model bytes it could not parse -- the one outcome worse than losing the
    results, because a model that cannot read a tool result retries the same call.
    """
    return tool_error(
        f"The web search response was {size:,} characters, over the "
        f"{MAX_RESULT_SIZE_CHARS:,}-character tool result limit, and had no result list "
        "to trim. Retry with a smaller limit or a narrower query.",
        success=False,
    )


def _bound_result_size(response: dict[str, Any], result_json: str) -> str:
    """Keep the rendered response under :data:`MAX_RESULT_SIZE_CHARS`.

    Hermes spills the oversized body to a file and points the model at it. MISAKA has no
    tool-result store to spill into, and cutting the JSON mid-string would leave the model
    something it cannot parse, so entries are dropped from the tail until the document
    fits and a note says how many went. A search response only gets near 100k when a
    backend returns descriptions the size of whole pages, so this is a guard, not a
    routine path.
    """
    if len(result_json) <= MAX_RESULT_SIZE_CHARS:
        return result_json
    web = (response.get("data") or {}).get("web")
    if not isinstance(web, list) or not web:
        return _oversized_error(len(result_json))
    kept = list(web)
    while kept:
        kept.pop()
        trimmed = json.loads(json.dumps(response))
        trimmed["data"]["web"] = kept
        trimmed["data"]["truncated"] = (
            f"{len(web) - len(kept)} of {len(web)} results were dropped to stay under "
            f"the {MAX_RESULT_SIZE_CHARS:,}-character tool result limit; ask for a "
            "smaller limit or a narrower query."
        )
        candidate = json.dumps(trimmed, indent=2, ensure_ascii=False)
        if len(candidate) <= MAX_RESULT_SIZE_CHARS:
            return candidate
    return _oversized_error(len(result_json))


async def web_search_tool(query: str, limit: int = 5, *, signal: Any = None) -> str:
    """Search the web through the configured backend and return Hermes' JSON string.

    The return value is the contract response shape rendered with ``indent=2`` -- the
    same bytes Hermes hands its model::

        {"success": true, "data": {"web": [{"title", "url", "description", "position"}]}}

    or ``{"success": false, "error": str}``. Never raises: a failure that reaches the
    outer handler comes back as ``{"error": ...}``, because a model that gets an
    exception instead of a result retries the same call.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 5
    limit = min(max(limit, 1), 100)

    try:
        # Hermes checks its interrupt flag here, at the top of the tool, before any
        # config or network work. MISAKA's equivalent is the caller's abort signal. The
        # check is not enough on its own -- the agent loop awaits this call rather than
        # running it as a task it can cancel -- so the dispatch below is raced against
        # the same signal.
        if signal_aborted(signal):
            return tool_error("Interrupted", success=False)

        # Resolving here rather than only inside dispatch buys the cache key: the memo is
        # per-backend, because one backend's ranking of a query is not another's. The
        # five keyless vendors count as one backend -- see ``memo_identity``.
        provider, _backend, config_error = resolve_provider()
        if provider is None:
            response_data = {"success": False, "error": config_error}
        else:
            name = memo_identity(provider)
            # The provider is asked for the BUCKETED count so near-identical limits share
            # an entry; the caller's own count is sliced out below.
            fetch_limit = cache.bucket_limit(limit)

            async def _paid_search() -> dict[str, Any]:
                # Re-check inside the flight: a concurrent identical call may have
                # stored while this one waited, and a follower that fell through
                # single_flight's timeout would otherwise pay again.
                hit = cache.search_memo.lookup(name, query, limit)
                if hit is not None:
                    debug.event("cache_hit", cache="search", backend=provider.name, subject=query)
                    return hit
                response = await dispatch_search(query, fetch_limit)
                # Never cache a rescue-served response: it came from a ring vendor, not
                # the chosen backend, and caching it would make the one-shot rescue
                # sticky for this query for a whole TTL -- the next call must attempt the
                # chosen backend again.
                if not (response.get("data") or {}).get("rescued_from"):
                    cache.search_memo.store(name, query, limit, response)
                return response

            response_data = cache.search_memo.lookup(name, query, limit)
            if response_data is None:
                response_data, aborted = await run_with_abort(
                    single_flight(cache.flight_key(name, query, limit), _paid_search),
                    signal,
                )
                if aborted:
                    return tool_error("Interrupted", success=False)
            else:
                debug.event("cache_hit", cache="search", backend=provider.name, subject=query)
        response_data = _bound_error_field(
            redact_values(cache.slice_search_response(response_data, limit))
        )
        rendered = json.dumps(response_data, indent=2, ensure_ascii=False)
        debug.metrics(original_response_chars=len(rendered))
        return _bound_result_size(response_data, rendered)

    except Exception as exc:  # noqa: BLE001 - a search failure is a result, not a crash
        error_msg = f"Error searching web: {exc!s}"
        return tool_error(redact_secrets(error_msg))


def register(harn) -> None:
    """Install ``web_search`` into one session's harness."""

    async def execute(tool_call_id, raw, signal, on_update, ctx):
        args = raw if isinstance(raw, dict) else {}
        # Hermes' handler lambda, argument for argument.
        result_json = await web_search_tool(
            args.get("query", ""), limit=args.get("limit", 5), signal=signal
        )
        debug.result_json(result_json)
        envelope = json.loads(result_json)
        # Titles, URLs and descriptions are written by whoever got ranked, and a backend's
        # own error body is echoed through verbatim -- all of it third-party text landing
        # in the model's context. Fencing the whole rendered document rather than each
        # field keeps the provider contract untouched (nothing below this line may
        # reshape what a provider returned) and leaves no unfenced seam between fields.
        return {"content": [{"type": "text", "text": untrusted("web-search", result_json)}],
                "details": {}, "isError": envelope.get("success") is False or bool(envelope.get("error"))}

    harn.registerTool(ToolDefinition(
        name=WEB_SEARCH_SCHEMA["name"],
        label="Search the web",
        description=WEB_SEARCH_SCHEMA["description"],
        parameters=WEB_SEARCH_SCHEMA["parameters"],
        execute=execute,
        promptSnippet="Search the web for current information",
        promptGuidelines=[
            ("Results are snippets and links, not pages: read a page with web_fetch or web_extract before citing "
             "it. Identical queries are served from a short-lived memo inside this process, so re-asking the same "
             "thing costs nothing; a different Sister's identical search is paid again."),
        ],
    ))

"""Provider-agnostic web search, backed by the Serper Google API."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from misaka.agent.types import AgentToolResult
from misaka.ai.types import TextContent
from misaka.core.extensions.types import ToolDefinition
from misaka.platform.prompt_guard import untrusted
from misaka.utils.values import signal_aborted

SERPER_SEARCH_URL = "https://google.serper.dev/search"
DEFAULT_NUM = 10
MAX_NUM = 20
TIMEOUT_SECONDS = 20.0

# Serper error bodies are short; anything longer is an HTML error page we do not
# want to paste into the model's context.
_MAX_ERROR_BODY = 200

# A title, snippet, or URL is written by whoever got ranked, and nothing upstream bounds
# it -- one spam page would otherwise decide how much of the context window this call
# eats. Free text gets the tighter cap; a URL keeps enough room to stay usable, since
# browsers stop honouring one past roughly this length anyway.
_MAX_TEXT_CHARS = 500
_MAX_URL_CHARS = 2000


class WebSearchToolInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    query: str = Field(description="Search query, as you would type it into Google")
    num: int = Field(
        default=DEFAULT_NUM,
        description=f"Number of results to return (default {DEFAULT_NUM}, maximum {MAX_NUM})",
    )


def _result(text: str, details: dict[str, Any] | None = None) -> AgentToolResult:
    return AgentToolResult(content=[TextContent(text=text)], details=details)


def _one_line(value: object, limit: int = _MAX_TEXT_CHARS) -> str:
    """Collapse a result field to one bounded line.

    Collapsing is what stops a result from forging rows: a snippet carrying newlines would
    otherwise print as further numbered entries the search never returned. The ellipsis on
    a cut is deliberate -- a silently shortened URL looks usable and is not.
    """
    flat = " ".join(str(value or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _format_results(query: str, organic: list[Any]) -> AgentToolResult:
    """Render a stably numbered result list; index N always means organic[N-1]."""
    lines = [f'Web search results for "{query}" ({len(organic)}):', ""]
    cited: list[dict[str, str]] = []
    for index, item in enumerate(organic, 1):
        entry = item if isinstance(item, dict) else {}
        title = _one_line(entry.get("title")) or "(untitled)"
        url = _one_line(entry.get("link"), _MAX_URL_CHARS)
        snippet = _one_line(entry.get("snippet"))
        date = _one_line(entry.get("date"))
        lines.append(f"{index}. {title}")
        lines.append(f"   {url}" if url else "   (no URL)")
        if snippet:
            lines.append(f"   {snippet}")
        if date:
            lines.append(f"   Date: {date}")
        lines.append("")
        cited.append({"index": str(index), "title": title, "url": url, "date": date})
    # Titles and snippets are third-party text arriving in the model's context: fenced the
    # same way every other MISAKA tool fences what it did not write.
    return _result(untrusted("web-search", "\n".join(lines).rstrip()),
                   {"query": query, "results": cited})


def _http_failure(status: int, body: str, key: str) -> AgentToolResult:
    if status in (401, 403):
        return _result(
            f"Web search rejected the API key (HTTP {status}). The key in MISAKA_SERPER_KEY is "
            "missing or invalid, so searching will keep failing this session — do not retry; "
            "answer from what you already know and say the search was unavailable."
        )
    if status == 429:
        return _result(
            "Web search quota is exhausted (HTTP 429). Do not retry in a loop; continue without "
            "search results, or try a single retry much later."
        )
    # The body is whatever answered -- Serper, or any proxy httpx picked up from the
    # environment. Anything that reflects the request would otherwise write the key into
    # the model's context and from there into the saved transcript.
    # Redaction runs before the trim so a key straddling the cut cannot leave a fragment.
    detail = _one_line(body.replace(key, "<redacted>"), _MAX_ERROR_BODY)
    return _result(
        f"Web search failed (HTTP {status}){f': {detail}' if detail else ''}. "
        "Retry once with a simpler query; if it fails again, continue without search results."
    )


def create_web_search_tool_definition() -> (
    ToolDefinition[WebSearchToolInput | dict[str, Any], dict[str, Any] | None] | None
):
    """Build the web_search tool, or return None when no Serper key is configured.

    Returning None is the registration signal: a search tool that can only report
    "no key" is worse than no search tool, because the model still spends turns on it.
    """
    key = os.environ.get("MISAKA_SERPER_KEY", "").strip()
    if not key:
        return None

    async def execute(
        _tool_call_id: str,
        params: WebSearchToolInput | dict[str, Any],
        signal: Any | None = None,
        _on_update: Callable[[AgentToolResult], None] | None = None,
        _ctx: Any = None,
    ) -> AgentToolResult:
        parsed = params if isinstance(params, WebSearchToolInput) else WebSearchToolInput.model_validate(params or {})
        query = parsed.query.strip()
        if not query:
            return _result("Web search needs a non-empty query. Call it again with the terms to search for.")
        num = max(1, min(parsed.num, MAX_NUM))

        # ponytail: aborts are checked before the request rather than raced against it. An
        # abort arriving mid-flight is therefore not noticed until the response lands and
        # the caller gets a result it no longer wants -- nothing leaks, the request is
        # awaited inside the client's own `async with`. Ceiling: one Serper round-trip of
        # wasted latency. Upgrade path: `abort_race`, the way read.py does it.
        if signal_aborted(signal):
            raise RuntimeError("Operation aborted")

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                response = await client.post(
                    SERPER_SEARCH_URL,
                    headers={"X-API-KEY": key, "Content-Type": "application/json"},
                    json={"q": query, "num": num},
                )
        except httpx.TimeoutException:
            return _result(
                f"Web search timed out after {TIMEOUT_SECONDS:g}s. Retry once with a shorter query, "
                "or continue without search results."
            )
        except httpx.RequestError as error:
            return _result(
                f"Web search could not reach the search backend ({type(error).__name__}). "
                "The network may be down; retry once, or continue without search results."
            )

        if response.status_code != 200:
            return _http_failure(response.status_code, response.text, key)

        try:
            payload = response.json()
        except ValueError:
            return _result(
                "Web search returned a response that was not JSON. Retry once; if it fails again, "
                "continue without search results."
            )

        organic = payload.get("organic") if isinstance(payload, dict) else None
        organic = organic[:num] if isinstance(organic, list) else []
        if not organic:
            return _result(
                f'No web results for "{query}". Try broader or differently worded search terms.',
                {"query": query, "results": []},
            )
        return _format_results(query, organic)

    return ToolDefinition(
        name="web_search",
        label="web search",
        description=(
            "Search the web via Google and return a numbered list of results (title, URL, snippet, "
            "date). Use it for facts that are recent, external, or that you are unsure of. Result "
            "numbers are stable for a given call — refer back to a result by its number."
        ),
        promptSnippet="Search the web for current information.",
        parameters=WebSearchToolInput,
        execute=execute,
    )


__all__ = ["WebSearchToolInput", "create_web_search_tool_definition"]

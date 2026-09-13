"""The ``web_extract`` tool: screening, cache, char budget, and the untrusted fence.

Ported from Hermes' ``tools/web_tools.py`` -- ``web_extract_tool`` (1048-1490), the
char-budget pipeline (``_get_extract_char_limit`` 649-660,
``convert_base64_images_to_links`` 663-690, ``_store_full_text`` 693-733,
``_truncate_with_footer`` 736-800) and the ``WEB_EXTRACT_SCHEMA`` + ``registry.register``
block (1678-1723). The provider layer below answers "which backend, and what did it say";
this file answers "what does the model see".

The schema dict is a verbatim copy of Hermes', with host-specific tool names and a material-kind note: ``read_file`` is ``read`` here, and the closing advice points
at ``web_fetch`` where Hermes points at its browser tool. Kept as a literal rather than
generated from a pydantic model for the same reason ``web_search``'s is: a model would
reword the description, and that wording -- "no LLM summarization", the PDF sentence, the
head+tail explanation -- is the part a model actually reads.

**Two deliberate divergences from Hermes, both in the same direction.**

*Every extracted page is written to disk, not only a truncated one.* Hermes calls
``_store_full_text`` inside the truncation branch, because the file exists to let the
model page through a middle it was not shown. MISAKA writes every page through the same
evidence writer ``web_fetch`` uses, because here the file has a second job:
readers and red teams need the original material even when the preview fits the budget. The path comes
back on each entry as ``saved_path``, which is the addition to Hermes' result shape.

*The per-page budget is squeezed to fit the whole call.* Hermes clamps ``char_limit`` to
2000-500k and lets its registry spill an oversized result to a file, handing the model a
pointer. MISAKA has no tool-result store to spill into (see
:mod:`misaka.core.web.tool`), so the budget is divided across the pages in the call
and shrunk until the rendered document fits. The outcome is the one Hermes engineers --
the model sees a bounded document and a pointer to the rest -- reached with the mechanism
MISAKA has, and no page is dropped to get there.

``WEB_TOOLS_DEBUG`` records are owned by WebRuntime rather than a module-global log.
MISAKA's loader supplies provider registration; ``requires_env`` / ``emoji`` /
``toolset`` metadata is not copied into unrelated ``ToolDefinition`` fields.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from misaka.core.extensions.types import ToolDefinition
from misaka.core.platform.prompt_guard import untrusted
from misaka.core.tools._common import run_with_abort
from misaka.core.tools._web.bounded import UnsafeUrlError, vet_public_url
from misaka.core.tools._web.evidence import (
    citable_url,
    frontmatter_line_count,
    save_page,
)
from misaka.core.tools._web.screening import screen_url
from misaka.core.tools._web.website_policy import check_website_access
from misaka.core.web import cache, debug
from misaka.core.web.config import redact_secrets, redact_values, web_config
from misaka.core.web.dispatch import resolve_extractor
from misaka.core.web.dispatch import web_extract as dispatch_extract
from misaka.core.web.network import proxy_for_url
from misaka.core.web.tool import tool_error
from misaka.utils.async_lifecycle import run_in_thread
from misaka.utils.values import signal_aborted

logger = logging.getLogger(__name__)

# Hermes tools/web_tools.py:1678-1697: host tool names and returned-material scope
# are adapted; input parameters and truncation behavior keep the upstream contract.
WEB_EXTRACT_SCHEMA = {
    "name": "web_extract",
    "description": "Extract content from web page URLs. Returns clean page content in markdown/text (no LLM summarization — fast). Also works with PDF URLs (arxiv papers, documents) — pass the PDF link directly. Pages within the char budget (default 15000) return whole; larger pages return a head+tail window with a footer telling you the full text's saved file path and the read call to page through the omitted middle. Inline images appear as [IMAGE: alt] placeholders; real image URLs are kept as links. Check content_kind: some providers return excerpts rather than full-page text. Saved files contain the returned material, not a guarantee of the site's complete text. If a URL fails or times out, use web_fetch instead.",
    "parameters": {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of URLs to extract content from (max 5 URLs per call)",
                "maxItems": 5,
            },
            "char_limit": {
                "type": "integer",
                "description": "Optional per-page character budget sent back (default 15000). Pages larger than this are head+tail truncated with the full text stored to disk. Raise it when you need more of a long page inline.",
                "minimum": 2000,
            },
        },
        "required": ["urls"],
    },
}

# Hermes' DEFAULT_EXTRACT_CHAR_LIMIT and its clamp. Spending context, not API dollars,
# which is why it is generous next to a search result's two-line description.
DEFAULT_EXTRACT_CHAR_LIMIT = 15000
_MIN_CHAR_LIMIT = 2000
_MAX_CHAR_LIMIT = 500_000

# Hermes slices the list at five in its registry handler; the schema says the same thing
# to the model, and a model that ignores it still gets five.
MAX_URLS = 5

# The ceiling the whole rendered document has to fit under, matching the number Hermes
# carries as ``max_result_size_chars`` on both web tools.
MAX_RESULT_SIZE_CHARS = 100_000

# Bounds on the two fields a vendor writes that the per-page budget does not govern.
# An ``error`` is whatever the endpoint felt like sending -- an HTML rate-limit page,
# a proxy's error document -- and a ``title`` is page-written; five unbounded ones
# would carry the document past the ceiling that the content budget alone cannot pull
# it back under. Same number Hermes bounds a tool error at.
_MAX_ENTRY_ERROR_CHARS = 2048
_MAX_ENTRY_TITLE_CHARS = 500
_ELLIPSIS = "… [truncated]"


def extract_url(value: Any) -> str | None:
    """A usable URL out of one model-supplied item, or None.

    Models forward a whole search result where a URL was asked for, so the two keys a
    search result actually uses are accepted. Anything else is rejected rather than
    stringified: ``str({...})`` produces a plausible-looking fetch target that is not a
    URL at all. Hermes' ``_web_extract_url``, unchanged.
    """
    if isinstance(value, dict):
        value = value.get("url") or value.get("href")
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def extract_char_limit() -> int:
    """The per-page budget from ``extract_char_limit``, clamped to Hermes' range.

    Floored at 2000 because below that the truncation footer dominates what the model
    sees; ceilinged at 500k so a typo cannot spend a whole context window.
    """
    configured = web_config().get("extract_char_limit")
    if configured is None:
        return DEFAULT_EXTRACT_CHAR_LIMIT
    try:
        return max(_MIN_CHAR_LIMIT, min(int(configured), _MAX_CHAR_LIMIT))
    except (TypeError, ValueError):
        return DEFAULT_EXTRACT_CHAR_LIMIT


# Hermes' three patterns, in order: a markdown image whose source is a blob, a
# parenthesised blob, and a bare one.
_MD_BASE64_IMAGE_RE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\(\s*data:image/[^;]+;base64,[A-Za-z0-9+/=\s]+\)"
)
_PAREN_BASE64_IMAGE_RE = re.compile(r"\(\s*data:image/[^;]+;base64,[A-Za-z0-9+/=\s]+\)")
_BARE_BASE64_IMAGE_RE = re.compile(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+")


def convert_base64_images_to_links(text: str) -> str:
    """Replace inline base64 image blobs with labelled placeholders.

    A single inline PNG is tens of thousands of characters of no use to a model, and the
    budget below would spend a whole page's allowance on one. The alt text survives, so
    the model still knows an image was there and what it was captioned; ``http`` image
    links are left alone, because those are addresses something else can fetch.
    Hermes' ``convert_base64_images_to_links``, pattern for pattern.
    """

    def _replace_markdown(match: re.Match[str]) -> str:
        alt = (match.group("alt") or "").strip()
        return f"[IMAGE: {alt}]" if alt else "[IMAGE]"

    out = _MD_BASE64_IMAGE_RE.sub(_replace_markdown, text)
    out = _PAREN_BASE64_IMAGE_RE.sub("[IMAGE]", out)
    return _BARE_BASE64_IMAGE_RE.sub("[IMAGE]", out)


def truncate_with_footer(
    content: str, char_limit: int, saved_path: str | None, frontmatter_lines: int
) -> tuple[str, bool]:
    """``(model_text, was_truncated)`` for one page.

    A page at or under the budget comes back whole. A larger one gets a 75/25 head+tail
    window cut back to a line boundary, plus a footer that says how much of the page it
    is looking at, where the whole thing is, and the exact ``read`` call that lands in the
    omitted middle. Deterministic; no model is involved in deciding what to drop.

    *frontmatter_lines* is how many lines the saved file's provenance block occupies.
    Hermes computes this offset over a file that *is* the page text; here the text starts
    below that block and ``read`` is 1-indexed over the whole file, so the same arithmetic
    would point the model at the frontmatter and it would read yaml where it expected the
    page. The count comes from the writer
    (:func:`~misaka.core.tools._web.evidence.frontmatter_line_count`) rather than from
    anyone's arithmetic here.
    """
    if len(content) <= char_limit:
        return content, False

    head_budget = int(char_limit * 0.75)
    tail_budget = char_limit - head_budget
    head = content[:head_budget]
    tail = content[-tail_budget:] if tail_budget else ""
    # Snap both cuts to a line boundary, but only when the boundary is near enough that
    # snapping costs a line rather than half the window.
    newline = head.rfind("\n")
    if newline > head_budget * 0.5:
        head = head[:newline]
    newline = tail.find("\n")
    if 0 <= newline < tail_budget * 0.5:
        tail = tail[newline + 1 :]

    footer = [
        "",
        "─" * 8 + " [TRUNCATED] " + "─" * 8,
        (
            f"Showing {len(head):,} chars (head) + {len(tail):,} chars (tail) "
            f"of {len(content):,} total clean characters."
        ),
    ]
    if saved_path:
        # Hermes' ``head.count("\n") + 2``, shifted down past the provenance block.
        middle_start = frontmatter_lines + head.count("\n") + 2
        footer.append(f"Full text saved to: {saved_path}")
        footer.append(
            f'To read the omitted middle: read path="{saved_path}" '
            f"offset={middle_start} limit=200  (the file contains the returned material; "
            "raise or lower offset to page through it)."
        )
    else:
        footer.append(
            "Full text could not be stored; re-run web_extract on a more specific URL, "
            "or use web_fetch for the complete page."
        )
    footer.append("─" * 29)

    model_text = head + "\n\n[... middle omitted — see footer ...]\n\n" + tail
    return model_text + "\n" + "\n".join(footer), True


def _invalid_entry(index: int) -> dict[str, Any]:
    """Hermes' wording for an item that is not a URL and cannot be made into one."""
    return {
        "url": "",
        "title": "",
        "content": "",
        "error": (
            f"Invalid URL item at index {index}: expected a URL string or an object "
            "with a string 'url' or 'href' field"
        ),
    }


def _final_url(entry: dict[str, Any]) -> str:
    metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
    try:
        return citable_url(str(metadata.get("sourceURL") or entry.get("url") or ""))
    except ValueError:
        # A malformed reported address must not erase an otherwise returned document.
        return ""


def _store_page(
    cwd: str | None, entry: dict[str, Any], clean: str, backend: str
) -> tuple[str | None, int]:
    """Write one extracted page into the workspace; return ``(path, frontmatter_lines)``.

    Full text and source metadata remain available for readers and red teams.
    Storage is best-effort and file digests express identity, not citation validity.
    """
    try:
        body = clean.encode()
    except UnicodeError:
        # Same failure the writer guards against, one step earlier: the digest and the
        # filename both encode. Best-effort means best-effort on every line of the path.
        return None, 0
    url = entry.get("requested_url", entry.get("url", ""))
    metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
    # Query-stripped, for the reason web_fetch and download_file strip it: the address a
    # vendor says it ended on is the server's choice and is commonly presigned, and this
    # header is written to the workspace and registered as an artifact.
    final = _final_url(entry)
    # The vendor that actually answered, not the one that was chosen. A rescued batch was
    # served by a ring member, and the ring says which in ``served_by``; recording the
    # chosen backend there would put a name on this page that never fetched it.
    served_by = metadata.get("served_by") or backend
    provenance = {
        "source_url": url,
        "final_url": final,
        "provider": served_by,
        "text_sha256": hashlib.sha256(body).hexdigest(),
        "title": entry.get("title", ""),
        "content_kind": metadata.get("content_kind") or "page_text",
    }
    if url is None:
        provenance["association"] = "unresolved"
    saved = save_page(cwd, provenance, clean)
    return saved, frontmatter_line_count(provenance)


async def web_extract_tool(
    urls: Any,
    format: str | None = None,
    char_limit: int | None = None,
    *,
    signal: Any = None,
    cwd: str | None = None,
) -> str:
    """Extract the clean text of up to five pages and return Hermes' JSON string.

    The return value is ``{"results": [{"url", "title", "content", "error"}]}`` rendered
    with ``indent=2``, plus ``blocked_by_policy`` on an entry the operator's blocklist
    refused and ``saved_path`` on one whose text reached the workspace. Never raises: a
    failure that reaches the outer handler comes back as ``{"error": ...}``, because a
    model that gets an exception instead of a result retries the same call.

    The gates run in Hermes' order, and the order is the point. Screening first, before
    anything resolves a name, so a refusal costs no DNS lookup. Then the SSRF vet, so an
    internal hostname is never spoken aloud to a vendor. Then the backend, so a
    configuration error is reported as one rather than as five failed pages. Only then the
    cache, which therefore sits after every control and before the only paid call.
    """
    try:
        result, aborted = await run_with_abort(
            _extract_pages(urls, format, char_limit, signal=signal, cwd=cwd), signal
        )
        return tool_error("Interrupted", success=False) if aborted else result
    except Exception as exc:  # noqa: BLE001 - a tool failure is a result
        return tool_error(redact_secrets(f"Error extracting content: {exc!s}"))


async def _extract_pages(urls, format, char_limit, *, signal, cwd) -> str:
    items = list(urls)[:MAX_URLS] if isinstance(urls, list) else []
    if signal_aborted(signal):
        return tool_error("Interrupted", success=False)

    entries: dict[int, dict[str, Any]] = {}
    unassociated: list[dict[str, Any]] = []
    screened_urls: list[tuple[int, str]] = []
    for index, item in enumerate(items):
        raw = extract_url(item)
        if raw is None:
            entries[index] = _invalid_entry(index)
            continue
        screening = screen_url(raw, third_party=True)
        if screening.policy is not None:
            # A policy refusal is one page's answer, not the call's: the operator
            # blocked a host, not the request. Flagged so the keyless rescue below
            # knows never to re-fetch it through a vendor they did not configure.
            entries[index] = {
                "url": screening.url,
                "title": "",
                "content": "",
                "error": screening.refusal,
                "blocked_by_policy": True,
            }
            continue
        if screening.refusal:
            # A credential in the URL fails the whole call, as in Hermes: the model
            # is about to hand a secret to a third party, and answering four of five
            # pages would bury that.
            return json.dumps(
                {"success": False, "error": screening.refusal}, ensure_ascii=False
            )
        screened_urls.append((index, screening.url))

    vetted: list[tuple[int, str]] = []
    for index, url in screened_urls:
        try:
            await vet_public_url(url, proxy=proxy_for_url(url))
        except UnsafeUrlError as error:
            entries[index] = {
                "url": url,
                "title": "",
                "content": "",
                "error": f"Blocked: {error}.",
            }
        else:
            vetted.append((index, url))

    backend_name = ""
    if vetted:
        provider, backend_name, config_error = resolve_extractor()
        if provider is None:
            return json.dumps({"success": False, "error": config_error}, ensure_ascii=False)

        to_fetch: list[tuple[int, str]] = []
        for index, url in vetted:
            hit = await run_in_thread(
                cache.extract_cache_get, url, format=format, provider=provider.name
            )
            if hit is not None:
                debug.event("cache_hit", cache="extract", backend=provider.name, subject=url, input_index=index)
                # A cached redirect still belongs to its final source. Apply today's
                # website rule without fetching or saving that blocked page again.
                source_url = hit["metadata"].get("sourceURL") or url
                blocked = check_website_access(source_url)
                if blocked:
                    hit.update(content="", error=blocked["message"], blocked_by_policy=blocked)
                elif source_url != url:
                    try:
                        await vet_public_url(source_url, proxy=proxy_for_url(source_url))
                    except UnsafeUrlError as error:
                        hit.update(content="", error=f"Blocked cached source: {error}.")
                entries[index] = hit
            else:
                to_fetch.append((index, url))

        if to_fetch:
            logger.info(
                "Web extract via %s: %d URL(s)", provider.name, len(to_fetch)
            )
            fetch_urls = [url for _, url in to_fetch]
            results, rescued = await dispatch_extract(provider, fetch_urls, format=format)
            for entry in results:
                blocked = check_website_access(_final_url(entry))
                if blocked:
                    entry.update(content="", raw_content="", error=blocked["message"], blocked_by_policy=blocked)
            # A batch provider can preserve material whose canonical URL no longer
            # identifies an input. Keep it, without an invented input or cache key.
            extra = results[len(to_fetch):]
            if len(extra) > MAX_URLS:
                # Bound the number of previews/files even for an over-producing API.
                # Preserve the extra records together, rather than dropping them.
                extra = [{"url": "", "title": "Unassociated provider records (JSON)",
                          "content": json.dumps(extra, ensure_ascii=False),
                          "metadata": {"content_kind": "provider_records"}}]
            unassociated.extend({**entry, "requested_url": None, "input_index": None} for entry in extra)
            for position, (index, url) in enumerate(to_fetch):
                entries[index] = (
                    results[position]
                    if position < len(results)
                    else {
                        "url": url,
                        "title": "",
                        "content": "",
                        "error": "Extract backend returned no result for this URL",
                    }
                )
            if not rescued:
                # Never cache a rescued batch: it came from a ring vendor rather than
                # the chosen backend, and caching it would make one bad minute stick
                # to these pages for a whole TTL.
                for index, url in to_fetch:
                    entry = entries[index]
                    if entry.get("error"):
                        continue
                    content = entry.get("raw_content") or entry.get("content") or ""
                    if content:
                        await run_in_thread(
                            cache.extract_cache_put,
                            url,
                            content,
                            title=entry.get("title", ""),
                            format=format,
                            provider=provider.name,
                            metadata=entry.get("metadata"),
                        )

    results_in_order = []
    for index in range(len(items)):
        entry = entries.get(index, _invalid_entry(index))
        results_in_order.append({**entry, "requested_url": entry.get("url"), "input_index": index})
    return await _render([*results_in_order, *unassociated], char_limit, cwd, backend_name)


def _capped(clean: str) -> str:
    """One page's text, bounded before anything stores or truncates it.

    Hermes' ``MAX_STORED_TEXT_CHARS`` (``tools/web_tools.py:637-644``), with its reason
    intact: some backends answer a long page with multiple megabytes of markdown, and
    without a ceiling every extract writes all of it to the workspace. The model never
    sees more than its per-page budget either way, so the cap costs it nothing; the marker
    is there so a reader of the file knows it is not the literal complete page.

    Capped before the digest rather than after, so ``text_sha256`` describes the bytes
    that are actually on disk -- which is what ``research/ledger.py`` re-hashes.
    """
    if len(clean) <= cache.MAX_STORED_TEXT_CHARS:
        return clean
    return clean[: cache.MAX_STORED_TEXT_CHARS] + (
        f"\n\n[... stored copy truncated at {cache.MAX_STORED_TEXT_CHARS:,} chars "
        f"of {len(clean):,}; re-extract a more specific URL for the rest ...]"
    )


def _bounded(value: object, limit: int) -> str:
    """A vendor-written string cut to *limit*, marked when it was."""
    text = str(value or "")
    if len(json.dumps(text, ensure_ascii=False)) - 2 <= limit:
        return text
    text = text[:limit]
    while len(json.dumps(text, ensure_ascii=False)) - 2 > limit:
        text = text[:len(text) // 2]
    return text + _ELLIPSIS


def _prepare(
    results: list[dict[str, Any]], cwd: str | None, backend: str
) -> dict[int, tuple[str, str | None, int]]:
    """Clean and store each page once, before any budget decision.

    Storing keeps the original available, so it happens for every page and at full
    length -- independently of how much of it the model is shown, which the budget below
    may revise more than once.
    """
    prepared: dict[int, tuple[str, str | None, int]] = {}
    for index, entry in enumerate(results):
        if entry.get("error"):
            continue
        raw = entry.get("raw_content") or entry.get("content") or ""
        if not raw:
            continue
        clean = _capped(convert_base64_images_to_links(raw))
        saved, frontmatter_lines = _store_page(cwd, entry, clean, backend)
        prepared[index] = (clean, saved, frontmatter_lines)
    return prepared


def _trim(
    results: list[dict[str, Any]],
    prepared: dict[int, tuple[str, str | None, int]],
    per_page: int,
) -> list[dict[str, Any]]:
    """Hermes' minimal per-entry shape, with the page cut to *per_page* characters."""
    trimmed: list[dict[str, Any]] = []
    for index, entry in enumerate(results):
        error = entry.get("error")
        requested = entry.get("requested_url", entry.get("url"))
        metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
        out: dict[str, Any] = {
            "url": _bounded(entry.get("url"), 2048),
            "requested_url": _bounded(requested, 2048) if requested is not None else None,
            "final_url": _bounded(_final_url(entry), 2048),
            "input_index": entry.get("input_index"),
            "provider": _bounded(metadata.get("served_by"), 100),
            "content_kind": _bounded(metadata.get("content_kind") or "page_text", 100),
            "title": _bounded(entry.get("title"), _MAX_ENTRY_TITLE_CHARS),
            "content": "",
            "error": _bounded(error, _MAX_ENTRY_ERROR_CHARS) if error else error,
        }
        if requested is None:
            out["association"] = "unresolved"
        if "blocked_by_policy" in entry:
            block = entry["blocked_by_policy"]
            out["blocked_by_policy"] = (
                {key: _bounded(block.get(key), 500) for key in ("host", "rule", "source")}
                if isinstance(block, dict) else bool(block)
            )
        page = prepared.get(index)
        if page is not None:
            clean, saved, frontmatter_lines = page
            out["content"], _ = truncate_with_footer(clean, per_page, saved, frontmatter_lines)
            if saved:
                # MISAKA's addition to Hermes' shape: the card records this path to make
                # the page quotable, and a page that fit under the budget has no footer
                # to name it.
                out["saved_path"] = saved
        else:
            out["content"] = _bounded(entry.get("content"), per_page)
        trimmed.append(out)
    return trimmed


async def _render(
    results: list[dict[str, Any]], char_limit: int | None, cwd: str | None, backend: str
) -> str:
    """Store, truncate and serialise, shrinking the per-page budget until it all fits.

    Halving rather than dividing by the page count: the pages in one call are rarely the
    same size, and a flat share starves a long page to leave room a short one never uses.
    Requested pages and unassociated material share the same bounded preview budget.
    """
    # Off the loop: storing five pages is five file writes plus their digests, and the
    # cache's own index lock (cache.py) exists precisely because this runs on a thread.
    stamped = []
    for entry in results:
        metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
        stamped.append({**entry, "metadata": {**metadata, "served_by": metadata.get("served_by") or backend}})
    results = stamped
    debug.original_json({"results": results})
    prepared = await run_in_thread(_prepare, results, cwd, backend)
    results = redact_values(results)
    prepared = {i: (redact_secrets(text), path, lines) for i, (text, path, lines) in prepared.items()}
    limit = char_limit if char_limit is not None else extract_char_limit()
    try:
        limit = max(_MIN_CHAR_LIMIT, min(int(limit), _MAX_CHAR_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_EXTRACT_CHAR_LIMIT

    trimmed = _trim(results, prepared, limit)
    rendered = json.dumps({"results": trimmed}, indent=2, ensure_ascii=False)
    while len(rendered) > MAX_RESULT_SIZE_CHARS and limit > 0:
        limit //= 2
        trimmed = _trim(results, prepared, limit)
        rendered = json.dumps({"results": trimmed}, indent=2, ensure_ascii=False)

    if not trimmed:
        return tool_error("Content was inaccessible or not found")
    if debug.active():
        debug.metrics(pages_truncated=sum(len(text) > limit for text, _path, _lines in prepared.values()))
        for index, (text, path, _lines) in prepared.items():
            debug.event("page_processed", input_index=results[index].get("input_index"),
                        original_chars=len(text), final_chars=len(trimmed[index]["content"]),
                        truncated=len(text) > limit, stored=path is not None)
    return rendered


def register(harn, workspace: str | None = None) -> None:
    """Install ``web_extract`` into one session's harness."""

    async def execute(tool_call_id, raw, signal, on_update, ctx):
        args = raw if isinstance(raw, dict) else {}
        requested = args.get("urls")
        # Hermes' handler lambda, argument for argument: the list is sliced at five here
        # rather than trusted from the schema, and format is fixed to markdown -- the
        # parameter exists all the way down but the model is not offered it.
        result_json = await web_extract_tool(
            requested[:MAX_URLS] if isinstance(requested, list) else [],
            "markdown",
            char_limit=args.get("char_limit"),
            signal=signal,
            cwd=workspace,
        )
        debug.result_json(result_json)
        # Whole pages of somebody else's prose, which is the most injection-prone thing
        # any tool in this project hands the model. Fencing the rendered document rather
        # than each field leaves no unfenced seam between entries and keeps the provider
        # contract untouched.
        try:
            rendered = json.loads(result_json)
            is_error = rendered.get("success") is False or bool(rendered.get("error"))
            saved_paths = list(dict.fromkeys(
                entry["saved_path"]
                for entry in rendered.get("results", [])
                if isinstance(entry, dict) and entry.get("saved_path")
            ))
        except (AttributeError, TypeError, ValueError):
            saved_paths = []
            is_error = True
        return {
            "content": [{"type": "text", "text": untrusted("web-extract", result_json)}],
            "details": {"saved_paths": saved_paths},
            "isError": is_error,
        }

    harn.registerTool(
        ToolDefinition(
            name=WEB_EXTRACT_SCHEMA["name"],
            label="Read web pages",
            description=WEB_EXTRACT_SCHEMA["description"],
            parameters=WEB_EXTRACT_SCHEMA["parameters"],
            execute=execute,
            promptSnippet="Extract the clean text of up to five web pages at once",
            promptGuidelines=[
                ("Up to five URLs per call. JavaScript rendering and whole-page extraction depend on the selected "
                 "provider; Perplexity returns snippets, not full pages. Each page's text is saved under "
                 "downloads/pages/ (saved_paths in the result): read the saved file before citing it, and check "
                 "content_kind and final_url. No provider gets past a paywall."),
            ],
        )
    )

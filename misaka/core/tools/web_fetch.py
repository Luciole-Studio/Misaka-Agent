"""web_fetch: read one public web page as text, safely and at most once per moment.

The alternative this replaces is `bash curl`, which has no SSRF vetting, no byte
ceiling, and no memory of the
host that refused us thirty seconds ago. Every one of those is supplied by the
`_web/` modules; this tool is the thing that puts them in a row:

    negative cache -> single flight -> per-hop vetted stream -> bounded read
    -> decode -> extraction -> untrusted fence

Nothing here raises at the model: a fetch that fails comes back as one sentence the
model can act on, because a traceback in a tool result only ever produces a retry.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from misaka.agent.types import AgentToolResult
from misaka.ai.types import TextContent
from misaka.core.documents.htmltext import clip as _clip
from misaka.core.documents.htmltext import readable as _readable
from misaka.core.documents.prompt import WEB_EVIDENCE_GUIDELINE
from misaka.core.extensions.types import ToolDefinition
from misaka.core.platform.prompt_guard import untrusted
from misaka.core.tools._common import run_with_abort
from misaka.core.tools._web.academic import route_academic
from misaka.core.tools._web.bounded import (
    DEFAULT_MAX_FETCH_BYTES,
    MAX_REDIRECT_HOPS,
    UnsafeUrlError,
    decode_body,
    is_text_content_type,
    open_checked_stream,
    read_bounded,
)

# The evidence writer is shared with web_extract; parsing and saving run off-loop.
from misaka.core.tools._web.evidence import citable_url
from misaka.core.tools._web.evidence import save_page as _save_page
from misaka.core.tools._web.negative_cache import (
    record_failure,
    record_success,
    skip_reason,
)
from misaka.core.tools._web.screening import screen_url
from misaka.core.tools._web.single_flight import single_flight
from misaka.core.web import debug
from misaka.core.web.config import web_config
from misaka.core.web.network import policy_key
from misaka.core.web.scope import cache_namespace
from misaka.core.web.timeouts import operation_seconds
from misaka.utils.async_lifecycle import run_in_thread
from misaka.utils.values import signal_aborted

TIMEOUT_SECONDS = 30.0

# Whole-operation defaults live in core.web.timeouts. WebPart's deadline covers
# DNS, redirects, body reading and material work, not just gaps between chunks.

# Characters of extracted page text that enter the model's context. The byte cap
# upstream bounds the *transfer*; this bounds the *context*, and 2 MiB of HTML can
# still render to far more prose than a turn should carry. Roughly 10k tokens.
# Upgrade path: W4's auxiliary-LLM condenser replaces the tail-drop with a summary.
_MAX_TEXT_CHARS = 40_000

# A Content-Type is a remote header quoted back at the model in this tool's own voice,
# outside any fence. Same reasoning as the title, tighter bound: no real media type is
# anywhere near this long.
_MAX_TYPE_CHARS = 100

# The URL a redirect chain landed on is chosen by the remote, and it is quoted back in
# this tool's own voice outside the untrusted() fence. Without a bound,
# `Location: https://<15KB of a>.example/` buys 15KB of the model's context in a
# position the model reads as ours -- the same reasoning as _MAX_TYPE_CHARS above and as
# _web/academic.py's _MAX_HOST_CHARS. Generous enough for a real redirect target
# (253-byte host plus a long path); `details["final_url"]` keeps the full value.
_MAX_URL_CHARS = 300

# Sent to every site fetched. The compatible-token form is what unblocks origins that
# reject an unfamiliar agent outright; the name is the project, never an account.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MISAKA/1.0; research agent)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# A page that came back without its content is worth remembering as 422 -- the negative
# cache's own name for "paywall, empty, or unparseable" -- and it bans only on the
# second consecutive one, which is what keeps a late-hydrating page from being written
# off on a single unlucky fetch.
_NO_CONTENT_STATUS = 422


class WebFetchToolInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str = Field(description="Full http(s) URL of the page to read")


# --- HTML -> text the model can navigate -------------------------------------------
#
# The extractor itself lives in misaka/core/documents/htmltext.py: the corpus reads .html files
# and EPUB chapters with the same parser, and a page fetched here has to render the same way
# as the same page saved to disk and indexed.


def _is_markup(content_type: str | None, text: str) -> bool:
    bare = (content_type or "").split(";", 1)[0].strip().lower()
    if bare:
        return bare in {"text/html", "application/xhtml+xml"} or bare.endswith("+html")
    # No declared type: is_text_content_type already let this through as text, so the
    # only question left is whether it is markup.
    return text.lstrip().lstrip("\ufeff").lstrip().startswith("<")


# --- what to fetch -------------------------------------------------------------------
#
# Where the evidence lands is _web/evidence.py: web_extract writes its pages through the
# same writer, so one saved page is one workspace file whatever tool reached it.


@dataclass(frozen=True, slots=True)
class _Target:
    """One fetch's address, after academic routing has had its say.

    ``requested`` is the URL the model asked for and the one provenance is reported
    against; ``url`` is the address actually dialled, which the academic table may have
    rewritten to somewhere that serves the text. ``note`` is this tool's own paragraph
    about the swap, so it belongs outside the untrusted fence with the rest of our voice.
    """

    requested: str
    url: str
    kind: str
    note: str

    def provenance(self) -> dict[str, Any]:
        """The URL fields every result of this fetch carries, routed or not."""
        out: dict[str, Any] = {"url": self.requested}
        if self.kind != "none":
            out["route"] = self.kind
            if self.url != self.requested:
                out["routed_url"] = self.url
        return out


# --- fetch -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Extracted:
    """Extracted page text and its saved-file provenance; no content-quality verdict."""

    has_text: bool = False
    title: str = ""
    full: str = ""
    body_sha: str = ""
    text_sha: str = ""
    saved: str | None = None


def _extract(
    target: _Target,
    final_url: str,
    content_type: str | None,
    body: bytes,
    text: str,
    cwd: str | None,
) -> _Extracted:
    """The whole derive-and-save stage of one fetch, meant to be run off the event loop.

    Parsing and saving can be CPU/disk intensive; keep them off the event loop.

    Nothing here touches loop-owned state: the negative cache stays with the caller.
    The one side effect is the evidence
    file, whose name is a digest of its own contents, so writing it from a worker thread
    is no different from writing it from any other caller (see :func:`_save_page`).
    """
    if _is_markup(content_type, text):
        content, title = _readable(text, final_url)
    else:
        content, title = text, ""
    if not content.strip():
        return _Extracted()

    # The title is page-written, so it goes inside the fence with the rest of the page,
    # never into this tool's own sentence: a <title> carrying the fence's own closing
    # marker would otherwise end the block early and let the page speak as the tool.
    full = f"Title: {title}\n\n{content}" if title else content

    # Record response and full extracted-text identities, not just the rendered prefix.
    body_sha = hashlib.sha256(body).hexdigest()
    text_sha = hashlib.sha256(full.encode()).hexdigest()
    # fetched_at is reported by the caller and deliberately not written: it is the one
    # value here that changes between two fetches of the same page, and the file's bytes
    # are what git and research_artifacts.sha256 have to agree on.
    saved = _save_page(
        cwd,
        {
            "source_url": target.requested,
            # Query-stripped, for the reason download_file has always stripped it: the
            # end of a redirect chain is chosen by the server and commonly presigned, and
            # this header is written to disk and registered as an artifact.
            "final_url": citable_url(final_url),
            "sha256": body_sha,
            "text_sha256": text_sha,
            "title": title,
        },
        full,
    )
    return _Extracted(
        has_text=True,
        title=title,
        full=full,
        body_sha=body_sha,
        text_sha=text_sha,
        saved=saved,
    )


@dataclass(slots=True)
class _Outcome:
    """What one fetch attempt has to say. ``text`` is already model-facing prose;
    page content inside it is already fenced."""

    text: str
    details: dict[str, Any] = field(default_factory=dict)


def _result(outcome: _Outcome) -> AgentToolResult:
    return AgentToolResult(content=[TextContent(text=outcome.text)], details=outcome.details)


def _status_advice(status: int) -> str:
    if status == 404:
        return "The page does not exist. Check the URL, or find the content at another source."
    if status == 401:
        return "The page needs credentials this fetcher does not have. Find an open version."
    if status == 403:
        # Only the statuses the negative cache actually tracks may promise a skip.
        return (
            "The site refused this fetcher. Further fetches of this URL are skipped for a "
            "while — find an open version of the content instead of retrying."
        )
    if status == 429:
        return (
            "The site is rate-limiting us. Further fetches of this URL are skipped for a "
            "few minutes — use another source rather than retrying now."
        )
    if status >= 500:
        return "The failure is on the site's side. Try once more later, or use another source."
    return "Use a different URL or another source."


async def _fetch(target: _Target, cwd: str | None = None, signal: Any | None = None) -> _Outcome:
    """One full fetch of *target*, reporting every failure as prose rather than raising.

    Two exceptions to "rather than raising": an abort and the total deadline. The first
    is re-raised as ``RuntimeError("Operation aborted")`` because a cancelled call has no
    result to report, and the second lands in the timeout branch below.

    Runs under :func:`single_flight`, so this must stay side-effect-safe to share: the
    state it writes is the negative cache, which is idempotent per verdict, and the
    evidence file, whose name is a digest of its own contents (see :func:`_save_page`)
    and so identical for every caller that saw the same page at the same address.
    """
    url = target.url
    base = target.provenance()
    # Keep the low-level reader's bound for standalone helper callers. In sessions,
    # WebPart's earlier deadline also interrupts DNS/headers and owns all cleanup.
    deadline = time.monotonic() + operation_seconds("web_fetch")

    def failed(text: str, **details: Any) -> _Outcome:
        # The routing note rides on failures, not only on the header the blueprint names:
        # several of those notes exist precisely to say what to try when the *rewritten*
        # URL is the thing that 404s, and a model that never asked for that URL cannot
        # work that out alone.
        return _Outcome(f"{text} {target.note}" if target.note else text, {**base, **details})

    try:
        async with open_checked_stream(
            url, headers=_HEADERS, timeout=TIMEOUT_SECONDS
        ) as response:
            status = response.status_code
            content_type = response.headers.get("content-type")
            final_url = str(response.url)
            if status >= 400:
                record_failure(url, status)
                return failed(
                    f"Could not read {url}: the server answered HTTP {status}. {_status_advice(status)}",
                    status=status,
                )
            if not is_text_content_type(content_type):
                # Deliberately before read_bounded: leaving the body unread closes the
                # connection instead of buffering a megabyte of a file nobody can read.
                return failed(
                    f"{url} is not a web page: the server returned binary content "
                    f"({_clip(' '.join((content_type or 'no type').split()), _MAX_TYPE_CHARS)}). "
                    "Use download_file to save it, or find an HTML "
                    "version of the same material.",
                    status=status,
                    content_type=content_type,
                )
            body, truncated = await read_bounded(
                response, DEFAULT_MAX_FETCH_BYTES, deadline=deadline, signal=signal
            )
            text = decode_body(response, body)
    except UnsafeUrlError as error:
        return failed(
            f"Refused to fetch {url} under the configured Web network policy: {error}.",
            refused=str(error),
        )
    except httpx.TooManyRedirects:
        return failed(
            f"Could not read {url}: it redirected more than {MAX_REDIRECT_HOPS} times. "
            "Follow the destination link yourself, or use another source.",
            refused="too many redirects",
        )
    except httpx.TimeoutException:
        return failed(
            f"Fetching {url} timed out (configured HTTP phase or whole-operation deadline). "
            "Retry once, or use another source.",
            refused="timeout",
        )
    except httpx.HTTPError as error:
        return failed(
            f"Could not reach {url} ({type(error).__name__}). The site or the network may "
            "be down; retry once, or use another source.",
            refused=type(error).__name__,
        )

    # One hop off the loop for the whole CPU-and-disk stage; the decisions it feeds are
    # taken back here, because every one of them writes state the loop owns alone.
    got = await run_in_thread(_extract, target, final_url, content_type, body, text, cwd)
    if not got.has_text:
        # Actual empty extraction, not a keyword/length guess about the material.
        record_failure(url, _NO_CONTENT_STATUS)
        return failed(
            f"{url} has no extractable text — its content is probably in a frame, an "
            "image, or an embedded object. Use another source.",
            status=status,
            render="empty",
        )
    # Only now is the fetch known to have produced something: recording success any
    # earlier clears the failure the branch above is about to record, so that ban could
    # never reach its second strike no matter how often the page came back unreadable.
    record_success(url)

    saved = got.saved
    fetched_at = int(time.time())

    notes = [f"Fetched {url}"]
    if final_url != url:
        notes.append(f"redirected to {_clip(final_url, _MAX_URL_CHARS)}")
    notes.append(f"{len(body)} bytes")
    header = "; ".join(notes) + "."
    page = got.full
    extra = []
    if saved:
        extra.append(
            f"The captured text is saved as {saved}; the card records that path automatically. "
            "Inspect its content and provenance before using it as evidence."
        )
    if truncated:
        extra.append(
            f"The download was cut at {DEFAULT_MAX_FETCH_BYTES} bytes, so the end of the "
            "page is missing."
        )
    if len(page) > _MAX_TEXT_CHARS:
        rest = f"the rest is in {saved}" if saved else "the rest was dropped"
        extra.append(
            f"Only the first {_MAX_TEXT_CHARS} characters of the extracted text are shown "
            f"(of {len(page)}); {rest}."
        )
        page = page[:_MAX_TEXT_CHARS]
    if target.note:
        extra.append(target.note)
    if extra:
        header = header + " " + " ".join(extra)

    return _Outcome(
        header + "\n\n" + untrusted(url, page),
        {
            **base,
            "final_url": final_url,
            "status": status,
            "content_type": content_type,
            "sha256": got.body_sha,
            "text_sha256": got.text_sha,
            "fetched_at": fetched_at,
            "bytes": len(body),
            "chars": len(page),
            "truncated": truncated,
            "title": got.title,
            "saved_path": saved,
        },
    )


def create_web_fetch_tool_definition(
    cwd: str | None = None,
) -> ToolDefinition[WebFetchToolInput | dict[str, Any], dict[str, Any] | None]:
    """Build the web_fetch tool. Always available: it needs no key and no configuration.

    *cwd* is the workspace fetched pages are saved into, the way download_file is rooted.
    A session assembled without one still fetches: it simply has nowhere to leave the
    evidence, so nothing is written and nothing is promised about a saved file.
    """

    async def execute(
        _tool_call_id: str,
        params: WebFetchToolInput | dict[str, Any],
        signal: Any | None = None,
        _on_update: Callable[[AgentToolResult], None] | None = None,
        _ctx: Any = None,
    ) -> AgentToolResult:
        parsed = (
            params
            if isinstance(params, WebFetchToolInput)
            else WebFetchToolInput.model_validate(params or {})
        )
        url = parsed.url.strip()
        if not url:
            return _result(_Outcome("web_fetch needs a URL. Call it again with the page to read."))
        if "://" not in url:
            # A bare host from a search snippet is the common shape; assuming https is
            # what the browser does, and vet_public_url still judges the result.
            url = f"https://{url}"

        # Credential and policy screening comes before everything else, routing included:
        # a refusal must not cost a DNS lookup, and the academic table must rewrite the
        # normalised address rather than whatever IRI the model typed. `screening.url` is
        # the one dialled from here on.
        screened = screen_url(url)
        if not screened.allowed:
            return _result(_Outcome(screened.refusal, {"url": url, "refused": True}))
        url = screened.url

        # Routing comes first, before the negative cache and before single flight, so
        # that all three agree on one address. A publisher landing page, a PubMed record
        # and an arXiv /pdf/ URL each have a better entry point than the one the model
        # typed; keying the cache or the flight on the URL that is never dialled would
        # ban a URL nothing requests and coalesce fetches that are not the same fetch.
        route = route_academic(url)
        target = _Target(requested=url, url=route.url, kind=route.kind, note=route.note)

        if signal_aborted(signal):
            raise RuntimeError("Operation aborted")

        skip = skip_reason(target.url)
        if skip:
            debug.event("cache_hit", cache="negative_fetch", subject=target.url)
            # Same shape as `failed` inside _fetch, and for the same reason: the ban is
            # against the address the routing table dialled, which the model never typed.
            # Naming only that one hands a Sister who asked for /pdf/ a sentence about a
            # /abs/ URL she has never seen, with nothing to explain the swap.
            if target.url != target.requested:
                skip = f"{target.requested} is fetched as {target.url}. {skip}"
            if target.note:
                skip = f"{skip} {target.note}"
            return _result(_Outcome(skip, {**target.provenance(), "skipped": True}))

        # The shared outcome contains a caller URL and a workspace-relative artifact.
        # Only callers with the same inputs may share that complete outcome.
        # The complete outcome includes policy-checked redirects. A caller with different
        # rules must run its own hop checks, not inherit another profile's accepted page.
        key = json.dumps(("web-fetch", cache_namespace(), web_config().get("website_blocklist"),
                          policy_key(), os.path.realpath(cwd) if cwd else None,
                          target.requested, target.url), sort_keys=True)
        outcome, aborted = await run_with_abort(
            single_flight(key, lambda: _fetch(target, cwd, signal)), signal
        )
        if aborted:
            raise RuntimeError("Operation aborted")
        return _result(outcome)

    return ToolDefinition(
        name="web_fetch",
        label="web fetch",
        description=(
            "Read one public web page and return its text. Prefer it over curl for any http(s) "
            "page: it strips markup down to headings, lists and links, refuses private addresses, "
            "caps the download, and reports HTTP or extraction failures. "
            "Binary files (PDF, archives, datasets) are not fetched — download them instead."
        ),
        promptSnippet="Read a web page's text by URL.",
        promptGuidelines=[
            WEB_EVIDENCE_GUIDELINE,
            ("Prefer available web and download tools that preserve source metadata and workspace artifacts. "
             "When using another suitable reader or a raw JSON/CSV endpoint, retain equivalent source "
             "references and the relevant material needed to check the result."),
        ],
        parameters=WebFetchToolInput,
        execute=execute,
    )


__all__ = ["WebFetchToolInput", "create_web_fetch_tool_definition"]

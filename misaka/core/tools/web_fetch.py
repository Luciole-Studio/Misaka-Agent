"""web_fetch: read one public web page as text, safely and at most once per moment.

The alternative this replaces is `bash curl`, which has no SSRF vetting, no byte
ceiling, no way to tell a JavaScript shell from an empty page, and no memory of the
host that refused us thirty seconds ago. Every one of those is supplied by the
`_web/` modules; this tool is the thing that puts them in a row:

    negative cache -> single flight -> per-hop vetted stream -> bounded read
    -> decode -> render check -> extraction -> untrusted fence

Nothing here raises at the model: a fetch that fails comes back as one sentence the
model can act on, because a traceback in a tool result only ever produces a retry.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from misaka.agent.types import AgentToolResult
from misaka.ai.types import TextContent
from misaka.core.extensions.types import ToolDefinition
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

# Aliased to the private names this module has always called them by: the evidence
# writer moved out to be shared with web_extract, and nothing about the call sites --
# including the tests that patch them here to watch which thread they run on -- changed.
from misaka.core.tools._web.evidence import citable_url
from misaka.core.tools._web.evidence import page_stem as _page_stem
from misaka.core.tools._web.evidence import save_page as _save_page
from misaka.core.tools._web.negative_cache import (
    record_failure,
    record_success,
    skip_reason,
)
from misaka.core.tools._web.render_check import check_render
from misaka.core.tools._web.screening import screen_url
from misaka.core.tools._web.single_flight import single_flight
from misaka.documents.htmltext import clip as _clip
from misaka.documents.htmltext import readable as _readable
from misaka.platform import budget
from misaka.platform.prompt_guard import untrusted
from misaka.utils.values import signal_aborted

TIMEOUT_SECONDS = 30.0

# Wall-clock ceiling for one fetch, counted from just before the request. The constant
# above is httpx's *per-operation* timeout: it bounds a stall between two chunks, so a
# server that sends one byte every 29 seconds satisfies it until the 2 MiB cap is
# reached -- an interval measured in years -- while the tool call is not a cancellable
# task, so Esc cannot free the session either. Same failure and same fix as
# ``download_file._TOTAL_TIMEOUT``; smaller value because a readable web page is three
# orders of magnitude smaller than a downloadable dataset.
TOTAL_TIMEOUT_SECONDS = 120.0

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
# The extractor itself lives in misaka/documents/htmltext.py: the corpus reads .html files
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
    """One fetched body after everything that can be derived from it has been.

    ``render`` is :func:`check_render`'s verdict; anything but ``"ok"`` means the
    response carried no page, and ``has_text`` distinguishes the other empty outcome --
    markup whose visible text survived the render check but not extraction.
    """

    render: str
    advice: str
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

    It is one function rather than three because that is what makes a single
    ``asyncio.to_thread`` hop enough. The render check parses the entire document, the
    extractor parses it again, and both are stdlib ``HTMLParser`` -- Python bytecode
    holding the GIL for as long as the page is large. A 2.00 MiB page (the fetch cap)
    measured 132ms in ``check_render`` and 335ms in ``_readable`` here, and the loop
    paying that is also running the other tools of a parallel call, the guard callbacks,
    and a Sister's lease heartbeat.

    Nothing here touches process-local state: the negative cache and the spend record
    stay with the caller, on the loop that owns them. The one side effect is the evidence
    file, whose name is a digest of its own contents, so writing it from a worker thread
    is no different from writing it from any other caller (see :func:`_page_stem`).
    """
    verdict = check_render(text, content_type)
    if verdict.kind != "ok":
        return _Extracted(render=verdict.kind, advice=verdict.advice)
    if _is_markup(content_type, text):
        content, title = _readable(text, final_url)
    else:
        content, title = text, ""
    if not content.strip():
        # Markup whose visible text survived check_render but not extraction (a frameset,
        # a document that is one big <svg>). Nothing is saved for it, and nothing is shown.
        return _Extracted(render="ok", advice="")

    # The title is page-written, so it goes inside the fence with the rest of the page,
    # never into this tool's own sentence: a <title> carrying the fence's own closing
    # marker would otherwise end the block early and let the page speak as the tool.
    full = f"Title: {title}\n\n{content}" if title else content

    # The provenance stamp research/ledger.py evidence is anchored to: sha256 of the
    # exact response bytes read, alongside the sha of the complete extracted text --
    # which is the text saved below, and therefore the text a quote is checked in. It is
    # deliberately not the sha of the truncated rendering: that prefix is cut at a
    # private constant nobody outside this module can re-derive.
    body_sha = hashlib.sha256(body).hexdigest()
    text_sha = hashlib.sha256(full.encode()).hexdigest()
    # fetched_at is reported by the caller and deliberately not written: it is the one
    # value here that changes between two fetches of the same page, and the file's bytes
    # are what git and research_artifacts.sha256 have to agree on.
    saved = _save_page(
        cwd,
        _page_stem(target.requested, final_url, body),
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
        render="ok",
        advice="",
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
    evidence file, whose name is a digest of its own contents (see :func:`_page_stem`)
    and so identical for every caller that saw the same page at the same address.
    """
    url = target.url
    base = target.provenance()
    # Started before the connection so connect, TLS and the redirect chain are all
    # inside the budget. Only the body read polls it (the hop loop is bounded by
    # MAX_REDIRECT_HOPS x TIMEOUT_SECONDS instead), so the effective worst case is the
    # deadline plus one hop's worth of stall, not the deadline exactly.
    deadline = time.monotonic() + TOTAL_TIMEOUT_SECONDS

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
            f"Refused to fetch {url}: {error}. Only public http(s) pages can be read — "
            "an internal or private address is never fetched, including via a redirect.",
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
            f"Fetching {url} timed out ({TIMEOUT_SECONDS:g}s with no data, or "
            f"{TOTAL_TIMEOUT_SECONDS:g}s in total). Retry once, or use another source.",
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
    got = await asyncio.to_thread(_extract, target, final_url, content_type, body, text, cwd)
    if got.render != "ok":
        # Not a transport failure, so nothing above recorded it: this is the one place
        # that knows a 200 carried no content.
        record_failure(url, _NO_CONTENT_STATUS)
        # Second-pass routing. A doi.org link only names its publisher after the hop, so
        # the fetcher cannot know it was aimed at a paywall until the redirect chain has
        # landed -- and "no readable content" plus "this host is subscriber-only" is a
        # different instruction ("find an open copy") from "no readable content" alone.
        landed = route_academic(final_url)
        advice = f"{got.advice} {landed.note}" if landed.kind == "paywall" else got.advice
        return failed(
            f"{url} returned no readable content. {advice}",
            status=status,
            render=got.render,
        )
    if not got.has_text:
        # Markup whose visible text survived check_render but not extraction (a frameset,
        # a document that is one big <svg>). Reported, never returned as a blank page.
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
    # One page pulled off the network is one entry on the run's spend record, next to
    # the model tokens. Recorded here rather than per attempt: this runs inside
    # single_flight, so coalesced callers share the one transfer that was paid for.
    budget.record_external_call("web_fetch", subject=url, bytes=len(body))

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
            f"The complete text is saved as {saved} — list that path in report.json to "
            "make it citable evidence."
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

        # ponytail: aborts are checked before the request and then once per body chunk
        # (`read_bounded`), rather than raced against the whole call. The tool call is
        # awaited directly by the agent loop, not run as a cancellable task, so polling
        # is the only thing that can stop it; the residual ceiling is one hop's stall,
        # bounded by TIMEOUT_SECONDS. Upgrade path is read.py's `abort_race`.
        if signal_aborted(signal):
            raise RuntimeError("Operation aborted")

        skip = skip_reason(target.url)
        if skip:
            # Same shape as `failed` inside _fetch, and for the same reason: the ban is
            # against the address the routing table dialled, which the model never typed.
            # Naming only that one hands a Sister who asked for /pdf/ a sentence about a
            # /abs/ URL she has never seen, with nothing to explain the swap.
            if target.url != target.requested:
                skip = f"{target.requested} is fetched as {target.url}. {skip}"
            if target.note:
                skip = f"{skip} {target.note}"
            return _result(_Outcome(skip, {**target.provenance(), "skipped": True}))

        # Keyed on the URL actually requested, matching the negative cache: two sisters
        # that pick the same link out of one search result page share a single round-trip.
        # The leader's signal is the one the shared transfer watches. A follower whose
        # own caller aborts is not stuck with it: single_flight treats the leader's
        # raise as "make your own call", and that call checks the follower's signal
        # before it dials.
        return _result(await single_flight(target.url, lambda: _fetch(target, cwd, signal)))

    return ToolDefinition(
        name="web_fetch",
        label="web fetch",
        description=(
            "Read one public web page and return its text. Prefer it over curl for any http(s) "
            "page: it strips markup down to headings, lists and links, refuses private addresses, "
            "caps the download, and tells you when a page needs JavaScript or is paywalled. "
            "Binary files (PDF, archives, datasets) are not fetched — download them instead."
        ),
        promptSnippet="Read a web page's text by URL.",
        parameters=WebFetchToolInput,
        execute=execute,
    )


__all__ = ["WebFetchToolInput", "create_web_fetch_tool_definition"]

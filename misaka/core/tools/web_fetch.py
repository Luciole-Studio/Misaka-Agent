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

import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from misaka.agent.types import AgentToolResult
from misaka.ai.types import TextContent
from misaka.core.extensions.types import ToolDefinition
from misaka.core.tools._web.bounded import (
    DEFAULT_MAX_FETCH_BYTES,
    MAX_REDIRECT_HOPS,
    UnsafeUrlError,
    decode_body,
    is_text_content_type,
    open_checked_stream,
    read_bounded,
)
from misaka.core.tools._web.negative_cache import (
    record_failure,
    record_success,
    skip_reason,
)
from misaka.core.tools._web.render_check import check_render
from misaka.core.tools._web.single_flight import single_flight
from misaka.platform.prompt_guard import untrusted
from misaka.utils.values import signal_aborted

TIMEOUT_SECONDS = 30.0

# Characters of extracted page text that enter the model's context. The byte cap
# upstream bounds the *transfer*; this bounds the *context*, and 2 MiB of HTML can
# still render to far more prose than a turn should carry. Roughly 10k tokens.
# Upgrade path: W4's auxiliary-LLM condenser replaces the tail-drop with a summary.
_MAX_TEXT_CHARS = 40_000

# A link target longer than this is not a link a model will usefully follow, and the
# href is attacker-chosen text; the anchor still renders, without the URL.
_MAX_LINK_CHARS = 500

# A <title> is page-written text that this tool repeats as metadata: into the fenced
# block, into `details`, and from there into whatever ledger row a caller builds from
# it. Bounded so one page cannot decide how much of a turn -- or of a database column
# -- it occupies; a 2 MiB <title> is as easy to serve as a 2 MiB body.
_MAX_TITLE_CHARS = 200

# A Content-Type is a remote header quoted back at the model in this tool's own voice,
# outside any fence. Same reasoning as the title, tighter bound: no real media type is
# anywhere near this long.
_MAX_TYPE_CHARS = 100

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

_BLANK_RUN = re.compile(r"\n{3,}")


class WebFetchToolInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str = Field(description="Full http(s) URL of the page to read")


# --- HTML -> text the model can navigate -------------------------------------------

_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_HIDDEN = frozenset({"script", "style", "noscript", "template", "svg"})
_BLOCKS = frozenset({
    "article", "aside", "blockquote", "dd", "div", "dl", "dt", "figcaption", "figure",
    "footer", "form", "header", "hr", "main", "nav", "ol", "p", "pre", "section",
    "table", "td", "th", "tr", "ul",
})


@dataclass(slots=True)
class _Anchor:
    start: int
    href: str


class _Readable(HTMLParser):
    """Visible page text with the structure a reader navigates by kept.

    Headings, list items, paragraph breaks and link targets survive, because those are
    what the next tool call is chosen from: a model that cannot see a page's links has
    to guess the URL of whatever it wants to read next.
    """

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        try:
            self._base: httpx.URL | None = httpx.URL(base_url)
        except httpx.InvalidURL:
            self._base = None
        self._hidden = 0
        self._in_title = False
        self._title: list[str] = []
        self._parts: list[str] = []
        self._anchors: list[_Anchor] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HIDDEN:
            self._hidden += 1
            return
        if self._hidden:
            return
        if tag == "title":
            self._in_title = True
        elif tag == "a":
            # Only the outermost anchor is tracked. Nested <a> is invalid HTML and a
            # browser closes the outer one anyway, but the real reason is cost: one
            # frame per level makes _close_anchor re-flatten the same, ever-longer text
            # once per level, and each level also re-appends the resolved URL. 40k
            # nested anchors -- 460 KB, well under the fetch cap -- took 8.5s of blocked
            # event loop and rendered 1 MB of text out of them.
            if not self._anchors:
                href = next((value for name, value in attrs if name == "href"), None)
                self._anchors.append(_Anchor(len(self._parts), href or ""))
        elif tag in _HEADINGS:
            self._parts.append("\n\n" + "#" * _HEADINGS[tag] + " ")
        elif tag == "li":
            self._parts.append("\n- ")
        elif tag == "br":
            self._parts.append("\n")
        elif tag in _BLOCKS:
            self._parts.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _HIDDEN:
            self._hidden = max(0, self._hidden - 1)
            return
        if self._hidden:
            return
        if tag == "title":
            self._in_title = False
        elif tag == "a" and self._anchors:
            self._close_anchor(self._anchors.pop())
        elif tag in _HEADINGS or tag in _BLOCKS:
            self._parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if self._hidden:
            return
        if self._in_title:
            self._title.append(data)
            return
        if not data.strip():
            # Whitespace between two inline elements is a word boundary: dropping it
            # outright glues "<b>foo</b> <i>bar</i>" into one word.
            if self._parts and not self._parts[-1].endswith((" ", "\n")):
                self._parts.append(" ")
            return
        self._parts.append(data)

    def _close_anchor(self, anchor: _Anchor) -> None:
        text = " ".join("".join(self._parts[anchor.start:]).split())
        del self._parts[anchor.start:]
        if not text:
            return
        target = self._absolute(anchor.href)
        self._parts.append(f"[{text}]({target})" if target else text)

    def _absolute(self, href: str) -> str:
        """An absolute http(s) target for *href*, or "" if it is not one worth showing."""
        href = href.strip()
        if not href or self._base is None or href.startswith(("#", "javascript:", "data:", "mailto:")):
            return ""
        try:
            target = self._base.join(href)
        except (httpx.InvalidURL, ValueError, UnicodeError):
            return ""
        if target.scheme not in {"http", "https"}:
            return ""
        rendered = str(target)
        return rendered if len(rendered) <= _MAX_LINK_CHARS else ""

    def title(self) -> str:
        return " ".join("".join(self._title).split())

    def text(self) -> str:
        lines = (" ".join(line.split()) for line in "".join(self._parts).split("\n"))
        return _BLANK_RUN.sub("\n\n", "\n".join(lines)).strip()


def _clip(value: str, limit: int) -> str:
    """One remote-written string, short enough to quote. The ellipsis is deliberate:
    a silently shortened value reads as the whole thing."""
    return value if len(value) <= limit else value[:limit] + "…"


def _readable(markup: str, base_url: str) -> tuple[str, str]:
    """``(text, title)`` for a markup document."""
    parser = _Readable(base_url)
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # noqa: BLE001, S110 - broken markup: keep whatever parsed first
        pass
    return parser.text(), _clip(parser.title(), _MAX_TITLE_CHARS)


def _is_markup(content_type: str | None, text: str) -> bool:
    bare = (content_type or "").split(";", 1)[0].strip().lower()
    if bare:
        return bare in {"text/html", "application/xhtml+xml"} or bare.endswith("+html")
    # No declared type: is_text_content_type already let this through as text, so the
    # only question left is whether it is markup.
    return text.lstrip().lstrip("\ufeff").lstrip().startswith("<")


# --- fetch -------------------------------------------------------------------------


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


async def _fetch(url: str) -> _Outcome:
    """One full fetch of *url*, reporting every failure as prose rather than raising.

    Runs under :func:`single_flight`, so this must stay side-effect-safe to share: the
    only state it writes is the negative cache, which is idempotent per verdict.
    """
    try:
        async with open_checked_stream(
            url, headers=_HEADERS, timeout=TIMEOUT_SECONDS
        ) as response:
            status = response.status_code
            content_type = response.headers.get("content-type")
            final_url = str(response.url)
            if status >= 400:
                record_failure(url, status)
                return _Outcome(
                    f"Could not read {url}: the server answered HTTP {status}. {_status_advice(status)}",
                    {"url": url, "status": status},
                )
            if not is_text_content_type(content_type):
                # Deliberately before read_bounded: leaving the body unread closes the
                # connection instead of buffering a megabyte of a file nobody can read.
                return _Outcome(
                    f"{url} is not a web page: the server returned binary content "
                    f"({_clip(' '.join((content_type or 'no type').split()), _MAX_TYPE_CHARS)}). "
                    "Use download_file to save it, or find an HTML "
                    "version of the same material.",
                    {"url": url, "status": status, "content_type": content_type},
                )
            body, truncated = await read_bounded(response, DEFAULT_MAX_FETCH_BYTES)
            text = decode_body(response, body)
    except UnsafeUrlError as error:
        return _Outcome(
            f"Refused to fetch {url}: {error}. Only public http(s) pages can be read — "
            "an internal or private address is never fetched, including via a redirect.",
            {"url": url, "refused": str(error)},
        )
    except httpx.TooManyRedirects:
        return _Outcome(
            f"Could not read {url}: it redirected more than {MAX_REDIRECT_HOPS} times. "
            "Follow the destination link yourself, or use another source.",
            {"url": url, "refused": "too many redirects"},
        )
    except httpx.TimeoutException:
        return _Outcome(
            f"Fetching {url} timed out after {TIMEOUT_SECONDS:g}s. Retry once, or use "
            "another source.",
            {"url": url, "refused": "timeout"},
        )
    except httpx.HTTPError as error:
        return _Outcome(
            f"Could not reach {url} ({type(error).__name__}). The site or the network may "
            "be down; retry once, or use another source.",
            {"url": url, "refused": type(error).__name__},
        )

    verdict = check_render(text, content_type)
    if verdict.kind != "ok":
        # Not a transport failure, so nothing above recorded it: this is the one place
        # that knows a 200 carried no content.
        record_failure(url, _NO_CONTENT_STATUS)
        return _Outcome(
            f"{url} returned no readable content. {verdict.advice}",
            {"url": url, "status": status, "render": verdict.kind},
        )
    if _is_markup(content_type, text):
        content, title = _readable(text, final_url)
    else:
        content, title = text, ""
    if not content.strip():
        # Markup whose visible text survived check_render but not extraction (a frameset,
        # a document that is one big <svg>). Reported, never returned as a blank page.
        record_failure(url, _NO_CONTENT_STATUS)
        return _Outcome(
            f"{url} has no extractable text — its content is probably in a frame, an "
            "image, or an embedded object. Use another source.",
            {"url": url, "status": status, "render": "empty"},
        )
    # Only now is the fetch known to have produced something: recording success any
    # earlier clears the failure the branch above is about to record, so that ban could
    # never reach its second strike no matter how often the page came back unreadable.
    record_success(url)

    # The title is page-written, so it goes inside the fence with the rest of the page,
    # never into this tool's own sentence: a <title> carrying the fence's own closing
    # marker would otherwise end the block early and let the page speak as the tool.
    page = f"Title: {title}\n\n{content}" if title else content

    notes = [f"Fetched {url}"]
    if final_url != url:
        notes.append(f"redirected to {final_url}")
    notes.append(f"{len(body)} bytes")
    header = "; ".join(notes) + "."
    warnings = []
    if truncated:
        warnings.append(
            f"The download was cut at {DEFAULT_MAX_FETCH_BYTES} bytes, so the end of the "
            "page is missing."
        )
    if len(page) > _MAX_TEXT_CHARS:
        warnings.append(
            f"Only the first {_MAX_TEXT_CHARS} characters of the extracted text are shown "
            f"(of {len(page)}); the rest was dropped."
        )
        page = page[:_MAX_TEXT_CHARS]
    if warnings:
        header = header + " " + " ".join(warnings)

    return _Outcome(
        header + "\n\n" + untrusted(url, page),
        {
            "url": url,
            "final_url": final_url,
            "status": status,
            "content_type": content_type,
            # The provenance stamp research/ledger.py evidence is anchored to: sha256
            # of the exact response bytes read, alongside the sha of the fenced text
            # that actually entered the context, which is what a quote is checked in.
            "sha256": hashlib.sha256(body).hexdigest(),
            "text_sha256": hashlib.sha256(page.encode()).hexdigest(),
            "fetched_at": int(time.time()),
            "bytes": len(body),
            "chars": len(page),
            "truncated": truncated,
            "title": title,
        },
    )


def create_web_fetch_tool_definition() -> (
    ToolDefinition[WebFetchToolInput | dict[str, Any], dict[str, Any] | None]
):
    """Build the web_fetch tool. Always available: it needs no key and no configuration."""

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

        # ponytail: aborts are checked before the request rather than raced against it,
        # the same trade web_search makes -- ceiling is one wasted round-trip of latency,
        # upgrade path is read.py's `abort_race`.
        if signal_aborted(signal):
            raise RuntimeError("Operation aborted")

        skip = skip_reason(url)
        if skip:
            return _result(_Outcome(skip, {"url": url, "skipped": True}))

        # Keyed on the asked-for URL, matching the negative cache: two sisters that pick
        # the same link out of one search result page share a single round-trip.
        return _result(await single_flight(url, lambda: _fetch(url)))

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

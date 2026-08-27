"""Detect a fetch that came back without the page text (a JavaScript app shell).

A single-page app answers a plain HTTP GET with ``<div id="root"></div>`` and a pile
of scripts. Handing that to a model silently is worse than an error: it reads as
"this page has nothing on it" and that verdict ends up in a report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal

# Searched over the payload head rather than anchored, so a comment or an XML preamble in
# front of the doctype costs nothing. Reached only for a payload that *starts* as markup
# (see _looks_like_html), because prose or JSON that merely quotes "<body>" would otherwise
# be parsed as a document and judged on the handful of characters between the quoted tags.
_HTML_DOC_RE = re.compile(r"<!doctype\s+html\b|<html\b|<head\b|<body\b", re.IGNORECASE)
_SCRIPT_RE = re.compile(r"<script\b", re.IGNORECASE)
_NOSCRIPT_JS_RE = re.compile(
    r"<noscript\b[^>]*>[\s\S]{0,400}?(?:enable|requires?|turn on|activate)\s+(?:your\s+)?javascript",
    re.IGNORECASE,
)
# Every empty div/main/section, with its attributes captured for a plain substring test.
# Attributes are NOT matched by this pattern: the body is remote, attacker-chosen text, and
# a pattern that hunts `id=`/`class=` inside `[^>]*` backtracks quadratically -- 24KB of
# `<div id=id=id=...` took 1.4s, which at the fetch cap is hours of wedged event loop.
# `[^>]{0,600}` followed by a literal `>` has exactly one way to match, so this is linear.
_EMPTY_ELEMENT_RE = re.compile(r"<(div|main|section)\b([^>]{0,600})>\s*</\1\s*>", re.IGNORECASE)
_MOUNT_TOKENS = ("root", "app", "__next", "__nuxt")
_PAYWALL_RE = re.compile(
    r"subscribe to (?:continue|read)|already a subscriber|to continue reading"
    r"|this (?:article|story|content) is (?:for|available to) subscribers"
    r"|create an? (?:free )?account to (?:continue|read)|sign in to (?:continue|read)",
    re.IGNORECASE,
)

# Measured against real shells: a pre-hydration DOM carries at most a logo or a "Loading"
# crumb, while the smallest real page still runs to a few hundred characters. The two
# script-based rules only fire together with an emptiness signal, so a short-but-real page
# (a status page, a definition stub) is never reported.
_SHELL_VISIBLE_CHARS = 80
_JS_INTERSTITIAL_CHARS = 400
_PAYWALL_VISIBLE_CHARS = 1200


@dataclass(frozen=True, slots=True)
class RenderCheck:
    """``kind == "ok"`` means the fetch carries page text; otherwise it does not."""

    kind: Literal["ok", "app_shell", "paywall", "empty"]
    advice: str


_OK = RenderCheck("ok", "")
_SHELL = RenderCheck(
    "app_shell",
    "This page renders its content with JavaScript; the fetched HTML has no article text. "
    "Do not report it as an empty page -- try another source, or a tool that runs a browser.",
)
_PAYWALL = RenderCheck(
    "paywall",
    "The fetched page shows only a subscription or sign-in prompt, not the full text. "
    "Do not report it as the article -- find another source for this content.",
)
_EMPTY = RenderCheck(
    "empty",
    "The response body has no text at all. Treat this fetch as failed rather than as a page without content.",
)


class _VisibleText(HTMLParser):
    """Text a reader would see: script/style/noscript payloads excluded."""

    _HIDDEN = frozenset({"script", "style", "noscript", "template", "svg"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._hidden = 0
        self._in_body = False
        self._saw_body = False
        self._body: list[str] = []
        self._all: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "body":
            self._saw_body = True
            self._in_body = True
        if tag in self._HIDDEN:
            self._hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._HIDDEN:
            self._hidden = max(0, self._hidden - 1)
        elif tag == "body":
            self._in_body = False

    def handle_data(self, data: str) -> None:
        if self._hidden or not data.strip():
            return
        self._all.append(data)
        if self._in_body:
            self._body.append(data)

    def text(self) -> str:
        # Markup with no <body> tag (a fragment, or a document too broken to parse that
        # far) is judged on everything visible instead of on nothing.
        parts = self._body if self._saw_body else self._all
        return " ".join(" ".join(parts).split())


def _visible_text(markup: str) -> str:
    parser = _VisibleText()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # noqa: BLE001, S110 - broken markup: judge on whatever parsed first
        pass
    return parser.text()


def _has_empty_mount(body: str) -> bool:
    """Whether an empty ``<div id="root">``-style hydration mount is present.

    # ponytail: a scan for empty elements plus a substring test on their attributes,
    # instead of FA's tag stack (which also needs void-element handling to keep its
    # frames balanced). It misses a mount that wraps a nested empty element -- but such
    # a page has no visible text at all, which the caller's first rule already catches.
    # Upgrade path if a real miss shows up: port FA's `_mount_stack`, `_VOID` included.
    """
    for match in _EMPTY_ELEMENT_RE.finditer(body):
        attributes = match.group(2).lower()
        if ("id=" in attributes or "class=" in attributes) and any(
            token in attributes for token in _MOUNT_TOKENS
        ):
            return True
    return False


def _looks_like_html(body: str) -> bool:
    """Whether *body* is markup, as opposed to text that quotes markup.

    # ponytail: "the payload opens with a tag" rather than FA's prefix walker over
    # comments and XML declarations -- those all begin with `<` too, so the cheap test
    # admits them for free. Ceiling: an XML payload carrying HTML inside CDATA is still
    # treated as a document; it has visible text, so it is reported `ok` regardless.
    """
    return body.startswith("<") and bool(_HTML_DOC_RE.search(body[:2048]))


def check_render(text: str, content_type: str | None = None) -> RenderCheck:
    """Judge whether a fetched body actually carries the page's content."""
    # A BOM survives str.strip() and would otherwise hide the leading tag from
    # _looks_like_html. Spelled as an escape: a literal BOM is invisible in source.
    body = (text or "").strip().lstrip("\ufeff").strip()
    if not body:
        return _EMPTY
    if content_type and "html" not in content_type.lower():
        return _OK
    if not _looks_like_html(body):
        return _OK
    visible = _visible_text(body)
    scripted = bool(_SCRIPT_RE.search(body))
    if scripted and (
        not visible
        or (len(visible) < _SHELL_VISIBLE_CHARS and _has_empty_mount(body))
    ):
        return _SHELL
    if len(visible) < _JS_INTERSTITIAL_CHARS and _NOSCRIPT_JS_RE.search(body):
        return _SHELL
    if not visible:
        return _EMPTY
    if len(visible) < _PAYWALL_VISIBLE_CHARS and _PAYWALL_RE.search(visible):
        return _PAYWALL
    return _OK

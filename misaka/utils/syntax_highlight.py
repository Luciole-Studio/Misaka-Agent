"""Syntax highlighting helpers built on Pygments."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from pygments import lex
from pygments.lexers import TextLexer, get_lexer_by_name, guess_lexer
from pygments.token import Comment, Keyword, Literal, Name, Operator, Token
from pygments.util import ClassNotFound

from misaka.utils.html import decode_html_entity_at

HighlightFormatter = Callable[[str], str]
HighlightTheme = dict[str, HighlightFormatter]

_SPAN_CLOSE = "</span>"
_HIGHLIGHT_CLASS_PREFIX = "hljs-"


@dataclass(slots=True)
class HighlightOptions:
    language: str | None = None
    ignoreIllegals: bool | None = None
    languageSubset: Sequence[str] | None = None
    theme: HighlightTheme | None = None


def render_highlighted_html(html: str, theme: HighlightTheme | None = None) -> str:
    resolved_theme = theme or {}
    output = ""
    text_buffer = ""
    scopes: list[str | None] = []

    def flush_text() -> None:
        nonlocal output, text_buffer
        if not text_buffer:
            return
        formatter = _get_active_formatter(scopes, resolved_theme)
        output += formatter(text_buffer) if formatter else text_buffer
        text_buffer = ""

    index = 0
    while index < len(html):
        if _is_span_open_tag_start(html, index):
            tag_end_index = html.find(">", index + 5)
            if tag_end_index != -1:
                flush_text()
                tag = html[index : tag_end_index + 1]
                scopes.append(_get_scope_from_span_tag(tag))
                index = tag_end_index + 1
                continue

        if html.startswith(_SPAN_CLOSE, index):
            flush_text()
            if scopes:
                scopes.pop()
            index += len(_SPAN_CLOSE)
            continue

        if html[index] == "&":
            decoded = decode_html_entity_at(html, index)
            if decoded is not None:
                text_buffer += decoded.text
                index += decoded.length
                continue

        text_buffer += html[index]
        index += 1

    flush_text()
    return output


def highlight(code: str, options: HighlightOptions | dict[str, Any] | None = None) -> str:
    resolved = _resolve_options(options)
    lexer = _resolve_lexer(code, resolved)
    output: list[str] = []
    theme = resolved.theme or {}

    for token, value in lex(code, lexer):
        scope = _scope_for_token(token)
        formatter = _get_scope_formatter(scope, theme) if scope else None
        if formatter is None:
            formatter = theme.get("default")
        output.append(formatter(value) if formatter else value)

    return "".join(output)


def supports_language(name: str) -> bool:
    try:
        get_lexer_by_name(name)
    except ClassNotFound:
        return False
    return True


def _resolve_options(options: HighlightOptions | dict[str, Any] | None) -> HighlightOptions:
    if isinstance(options, HighlightOptions):
        return options
    if isinstance(options, dict):
        return HighlightOptions(
            language=options.get("language"),
            ignoreIllegals=options.get("ignoreIllegals"),
            languageSubset=options.get("languageSubset"),
            theme=options.get("theme"),
        )
    return HighlightOptions()


def _resolve_lexer(code: str, options: HighlightOptions):
    if options.language:
        try:
            return get_lexer_by_name(options.language)
        except ClassNotFound:
            return TextLexer()
    if options.languageSubset:
        best = None
        best_score: tuple[float, int, int] | None = None
        for name in options.languageSubset:
            try:
                candidate = get_lexer_by_name(name)
            except ClassNotFound:
                continue
            analyse = getattr(candidate, "analyse_text", None)
            analyse_score = float(analyse(code)) if callable(analyse) else 0.0
            error_chars = 0
            non_text_chars = 0
            for token, value in lex(code, candidate):
                size = len(value)
                if token in Token.Error:
                    error_chars += size
                elif not (token is Token.Text or token in Token.Text):
                    non_text_chars += size
            score = (analyse_score, -error_chars, non_text_chars)
            if best_score is None or score > best_score:
                best = candidate
                best_score = score
        if best is not None:
            return best
    try:
        return guess_lexer(code)
    except ClassNotFound:
        return TextLexer()


def _get_scope_from_span_tag(tag: str) -> str | None:
    import re

    match = re.search(r"""\sclass\s*=\s*(?:"([^"]*)"|'([^']*)')""", tag)
    class_value = match.group(1) if match and match.group(1) is not None else match.group(2) if match else None
    if not class_value:
        return None
    for class_name in class_value.split():
        if class_name.startswith(_HIGHLIGHT_CLASS_PREFIX):
            return class_name[len(_HIGHLIGHT_CLASS_PREFIX) :]
    return None


def _get_scope_formatter(scope: str | None, theme: HighlightTheme) -> HighlightFormatter | None:
    if scope is None:
        return None
    exact = theme.get(scope)
    if exact is not None:
        return exact
    for separator in (".", "-"):
        index = scope.find(separator)
        if index != -1:
            formatter = theme.get(scope[:index])
            if formatter is not None:
                return formatter
    return None


def _get_active_formatter(scopes: list[str | None], theme: HighlightTheme) -> HighlightFormatter | None:
    for scope in reversed(scopes):
        formatter = _get_scope_formatter(scope, theme)
        if formatter is not None:
            return formatter
    return theme.get("default")


def _is_span_open_tag_start(html: str, index: int) -> bool:
    if not html.startswith("<span", index):
        return False
    next_index = index + len("<span")
    next_char = html[next_index] if next_index < len(html) else ""
    return next_char in {">", " ", "\t", "\n", "\r"}


def _scope_for_token(token: Any) -> str | None:
    if token in Keyword:
        return "keyword"
    if token in Literal.Number:
        return "number"
    if token in Literal.String:
        return "string"
    if token in Comment:
        return "comment"
    if token in Name.Function or token in Name.Class:
        return "title"
    if token in Name.Tag:
        return "tag"
    if token in Name.Attribute:
        return "attr"
    if token in Name.Builtin:
        return "built_in"
    if token in Name.Variable:
        return "variable"
    if token in Operator:
        return "operator"
    if token is Token.Text:
        return None
    return None


highlightHtml = render_highlighted_html
renderHighlightedHtml = render_highlighted_html
supportsLanguage = supports_language

__all__ = [
    "HighlightFormatter",
    "HighlightOptions",
    "HighlightTheme",
    "highlight",
    "renderHighlightedHtml",
    "supportsLanguage",
]

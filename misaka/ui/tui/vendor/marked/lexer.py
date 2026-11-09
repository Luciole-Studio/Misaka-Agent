"""Translated from marked's ``src/Lexer.ts`` (marked 18.0.11, MIT).

MISAKA fork: extension functions are called without a ``this`` (``{ lexer }`` in upstream), with the arguments they
declare (``misaka.utils.js.jsCall``); the tokenizer's ``def``/``del`` are ``def_``/``del_``. ``lex`` is both the static
``Lexer.lex(src, options)`` and the instance ``lexer.lex(src)``, as upstream, through a descriptor.
"""

from __future__ import annotations

import functools
import math
import sys
from collections.abc import Callable
from typing import Any

from misaka.utils import js_regexp
from misaka.utils.js import jsCall, jsLength, jsSlice

from .defaults import defaults
from .rules import Rules, block, inline, other
from .tokenizer import _Tokenizer
from .tokens import JSObject, TokensList

# A lone surrogate: what slicing in UTF-16 code units leaves of an astral character.
_LONE_SURROGATE = js_regexp.RegExp("[" + chr(0xD800) + "-" + chr(0xDFFF) + "]")


def _rest(src: str, raw: str) -> str:
    """``src.substring(raw.length)``. MISAKA fork: emStrong and del cut their ``raw`` in UTF-16 code units, as upstream
    does, which may end inside a surrogate pair; the rest of ``src`` is then cut in code units too."""
    if not _LONE_SURROGATE.test(raw):
        return src[len(raw) :]
    return jsSlice(src, jsLength(raw))


class _StaticAndInstance:
    """MISAKA fork: one name for a static method and an instance method, as a JavaScript class can have."""

    def __init__(self, static: Callable[..., Any], instance: Callable[..., Any]) -> None:
        self._static = static
        self._instance = instance

    def __get__(self, obj: Any, cls: Any = None) -> Callable[..., Any]:
        return self._static if obj is None else functools.partial(self._instance, obj)


class _Lexer:
    """Block Lexer"""

    def __init__(self, options: Any = None) -> None:
        # TokenList cannot be created in one go
        self.tokens = TokensList()
        self.tokens.links = {}
        self.options = options or defaults()
        self.options.tokenizer = self.options.tokenizer or _Tokenizer()
        self.tokenizer: _Tokenizer = self.options.tokenizer
        self.tokenizer.options = self.options
        self.tokenizer.lexer = self
        self.inlineQueue: list[JSObject] = []
        self.state = JSObject(
            inLink=False,
            inRawBlock=False,
            # a link was produced in the inline run currently being scanned
            linkEmitted=False,
            top=True,
        )

        rules = Rules(other, block["normal"], inline["normal"])

        if self.options.pedantic:
            rules = Rules(other, block["pedantic"], inline["pedantic"])
        elif self.options.gfm:
            if self.options.breaks:
                rules = Rules(other, block["gfm"], inline["breaks"])
            else:
                rules = Rules(other, block["gfm"], inline["gfm"])
        self.tokenizer.rules = rules

    @staticmethod
    def rules() -> dict[str, Any]:
        """Expose Rules"""
        return {
            "block": block,
            "inline": inline,
        }

    @staticmethod
    def _staticLex(src: str, options: Any = None) -> TokensList:
        """Static Lex Method"""
        lexer = _Lexer(options)
        return lexer.lex(src)

    @staticmethod
    def lexInline(src: str, options: Any = None) -> list[Any]:
        """Static Lex Inline Method"""
        lexer = _Lexer(options)
        return lexer.inlineTokens(src)

    def _lex(self, src: str) -> TokensList:
        """Preprocessing"""
        src = js_regexp.replace(src, other.carriageReturn, "\n")

        self.blockTokens(src, self.tokens)

        i = 0
        while i < len(self.inlineQueue):
            next_ = self.inlineQueue[i]
            self.inlineTokens(next_.src, next_.tokens)
            i += 1
        self.inlineQueue = []

        return self.tokens

    lex = _StaticAndInstance(_staticLex, _lex)

    def blockTokens(self, src: str, tokens: list[Any] | None = None, lastParagraphClipped: bool = False) -> Any:
        """Lexing"""
        if tokens is None:
            tokens = []
        self.tokenizer.lexer = self
        if self.options.pedantic:
            src = js_regexp.replace(js_regexp.replace(src, other.tabCharGlobal, "    "), other.spaceLine, "")

        srcLength = math.inf
        while src:
            if len(src) < srcLength:
                srcLength = len(src)
            else:
                self.infiniteLoopError(ord(src[0]))
                break

            token: Any = None

            extensions = self.options.extensions
            if extensions is not None and extensions.block:
                consumed = False
                for extTokenizer in extensions.block:
                    token = jsCall(extTokenizer, src, tokens)
                    if token:
                        src = _rest(src, token.raw)
                        tokens.append(token)
                        consumed = True
                        break
                if consumed:
                    continue

            # newline
            token = self.tokenizer.space(src)
            if token:
                src = _rest(src, token.raw)
                lastToken = tokens[-1] if tokens else None
                if len(token.raw) == 1 and lastToken is not None:
                    # if there's a single \n as a spacer, it's terminating the last line,
                    # so move it there so that we don't get unnecessary paragraph tags
                    lastToken.raw += "\n"
                else:
                    tokens.append(token)
                continue

            # code
            token = self.tokenizer.code(src)
            if token:
                src = _rest(src, token.raw)
                lastToken = tokens[-1] if tokens else None
                # An indented code block cannot interrupt a paragraph.
                if lastToken is not None and (lastToken.type == "paragraph" or lastToken.type == "text"):
                    lastToken.raw += ("" if lastToken.raw.endswith("\n") else "\n") + token.raw
                    lastToken.text += "\n" + token.text
                    self.inlineQueue[-1].src = lastToken.text
                else:
                    tokens.append(token)
                continue

            # fences
            token = self.tokenizer.fences(src)
            if token:
                src = _rest(src, token.raw)
                tokens.append(token)
                continue

            # heading
            token = self.tokenizer.heading(src)
            if token:
                src = _rest(src, token.raw)
                tokens.append(token)
                continue

            # hr
            token = self.tokenizer.hr(src)
            if token:
                src = _rest(src, token.raw)
                tokens.append(token)
                continue

            # blockquote
            token = self.tokenizer.blockquote(src)
            if token:
                src = _rest(src, token.raw)
                tokens.append(token)
                continue

            # list
            token = self.tokenizer.list(src)
            if token:
                src = _rest(src, token.raw)
                tokens.append(token)
                continue

            # html
            token = self.tokenizer.html(src)
            if token:
                src = _rest(src, token.raw)
                tokens.append(token)
                continue

            # def
            token = self.tokenizer.def_(src)
            if token:
                src = _rest(src, token.raw)
                lastToken = tokens[-1] if tokens else None
                if lastToken is not None and (lastToken.type == "paragraph" or lastToken.type == "text"):
                    lastToken.raw += ("" if lastToken.raw.endswith("\n") else "\n") + token.raw
                    lastToken.text += "\n" + token.raw
                    self.inlineQueue[-1].src = lastToken.text
                elif not self.tokens.links.get(token.tag):
                    self.tokens.links[token.tag] = JSObject(
                        href=token.href,
                        title=token.title,
                    )
                    tokens.append(token)
                continue

            # table (gfm)
            token = self.tokenizer.table(src)
            if token:
                src = _rest(src, token.raw)
                tokens.append(token)
                continue

            # lheading
            token = self.tokenizer.lheading(src)
            if token:
                src = _rest(src, token.raw)
                tokens.append(token)
                continue

            # top-level paragraph
            # prevent paragraph consuming extensions by clipping 'src' to extension start
            cutSrc = src
            if extensions is not None and extensions.startBlock:
                startIndex: float = math.inf
                tempSrc = src[1:]
                for getStartIndex in extensions.startBlock:
                    tempStart = jsCall(getStartIndex, tempSrc)
                    if isinstance(tempStart, int | float) and not isinstance(tempStart, bool) and tempStart >= 0:
                        startIndex = min(startIndex, tempStart)
                if startIndex < math.inf and startIndex >= 0:
                    cutSrc = src[: int(startIndex) + 1]
            if self.state.top:
                token = self.tokenizer.paragraph(cutSrc)
            if self.state.top and token:
                lastToken = tokens[-1] if tokens else None
                if lastParagraphClipped and lastToken is not None and lastToken.type == "paragraph":
                    lastToken.raw += ("" if lastToken.raw.endswith("\n") else "\n") + token.raw
                    lastToken.text += "\n" + token.text
                    self.inlineQueue.pop()
                    self.inlineQueue[-1].src = lastToken.text
                else:
                    tokens.append(token)
                lastParagraphClipped = len(cutSrc) != len(src)
                src = _rest(src, token.raw)
                continue

            # text
            token = self.tokenizer.text(src)
            if token:
                src = _rest(src, token.raw)
                lastToken = tokens[-1] if tokens else None
                if lastToken is not None and lastToken.type == "text":
                    lastToken.raw += ("" if lastToken.raw.endswith("\n") else "\n") + token.raw
                    lastToken.text += "\n" + token.text
                    self.inlineQueue.pop()
                    self.inlineQueue[-1].src = lastToken.text
                else:
                    tokens.append(token)
                continue

            if src:
                self.infiniteLoopError(ord(src[0]))
                break

        self.state.top = True
        return tokens

    def inline(self, src: str, tokens: list[Any] | None = None) -> list[Any]:
        if tokens is None:
            tokens = []
        self.inlineQueue.append(JSObject(src=src, tokens=tokens))
        return tokens

    def linkInText(self, text: str) -> bool:
        """Does this link text hold a link already? An image does not count: an image
        may hold a link, a link may not."""
        if "[" not in text:
            return False

        linkRule = self.tokenizer.rules.inline.link
        for match in js_regexp.matchAll(text, self.tokenizer.rules.inline.blockSkip):
            # blockSkip also matches code spans and html, and the `!` of an image is
            # left out of the match, so read the character before it.
            if linkRule.test(match[0]) and (text[match.index - 1] if match.index >= 1 else "") != "!":
                return True

        for match in js_regexp.matchAll(text, self.tokenizer.rules.inline.reflinkSearch):
            match0 = match[0]
            refStart = match0.rfind("[")
            if match0[:1] == "!" or match0[refStart + 1 : -1] not in self.tokens.links:
                continue
            # a candidate holding a link is not a link either, so it does not count
            if refStart > 1 and self.linkInText(match0[1 : refStart - 1]):
                continue
            return True

        return False

    def inlineTokens(self, src: str, tokens: list[Any] | None = None) -> list[Any]:
        """Lexing/Compiling"""
        if tokens is None:
            tokens = []
        self.tokenizer.lexer = self
        # String with links masked to avoid interference with em and strong
        maskedSrc = src

        # Mask out reflinks
        if self.tokens.links is not None and "[" in src:
            reflinkSearch = self.tokenizer.rules.inline.reflinkSearch

            def maskReflink(match0: str) -> str:
                refStart = match0.rfind("[")
                if match0[refStart + 1 : -1] not in self.tokens.links:
                    return match0
                # CommonMark: "Links may not contain other links, at any level of
                # nesting." A candidate whose text already holds one never becomes a
                # link, so flattening the whole span would hide the emphasis that
                # does still apply inside it. Mask the links it holds instead.
                # Images are exempt: their text is flattened into an alt attribute.
                if refStart > 1 and match0[:1] != "!":
                    text = match0[1 : refStart - 1]
                    if self.linkInText(text):
                        return (
                            "["
                            + js_regexp.replace(text, reflinkSearch, maskReflink)
                            + "]["
                            + "a" * (jsLength(match0) - jsLength(match0[:refStart]) - 2)
                            + "]"
                        )
                return "[" + "a" * (jsLength(match0) - 2) + "]"

            maskedSrc = js_regexp.replace(maskedSrc, reflinkSearch, maskReflink)

        # Mask out escaped characters.
        # Every mask must keep the length it replaces: emStrong and del line
        # maskedSrc up with src by slicing from the end. `anyPunctuation` matches
        # unicode punctuation, so an escaped astral character is 3 code units.
        # MISAKA fork: mask lengths are UTF-16 code units (`jsLength`), as upstream's `.length`.
        maskedSrc = js_regexp.replace(
            maskedSrc, self.tokenizer.rules.inline.anyPunctuation, lambda match0: "+" * jsLength(match0)
        )

        # Mask out other blocks
        def maskBlock(match0: str, _link: Any, context: Any) -> str:
            offset = jsLength(context) if context else 0
            return jsSlice(match0, 0, offset) + "[" + "a" * (jsLength(match0) - offset - 2) + "]"

        maskedSrc = js_regexp.replace(maskedSrc, self.tokenizer.rules.inline.blockSkip, maskBlock)

        # Mask out blocks from extensions
        hooks = self.options.hooks
        emStrongMask = getattr(hooks, "emStrongMask", None) if hooks is not None else None
        masked = jsCall(emStrongMask, maskedSrc) if emStrongMask is not None else None
        maskedSrc = masked if masked is not None else maskedSrc

        keepPrevChar = False
        prevChar = ""
        srcLength = math.inf
        while src:
            if len(src) < srcLength:
                srcLength = len(src)
            else:
                self.infiniteLoopError(ord(src[0]))
                break

            if not keepPrevChar:
                prevChar = ""
            keepPrevChar = False

            token: Any = None

            # extensions
            extensions = self.options.extensions
            if extensions is not None and extensions.inline:
                consumed = False
                for extTokenizer in extensions.inline:
                    token = jsCall(extTokenizer, src, tokens)
                    if token:
                        src = _rest(src, token.raw)
                        tokens.append(token)
                        consumed = True
                        break
                if consumed:
                    continue

            # escape
            token = self.tokenizer.escape(src)
            if token:
                src = _rest(src, token.raw)
                tokens.append(token)
                continue

            # tag
            token = self.tokenizer.tag(src)
            if token:
                src = _rest(src, token.raw)
                tokens.append(token)
                continue

            # link
            token = self.tokenizer.link(src)
            if token:
                src = _rest(src, token.raw)
                tokens.append(token)
                continue

            # reflink, nolink
            token = self.tokenizer.reflink(src, self.tokens.links)
            if token:
                src = _rest(src, token.raw)
                lastToken = tokens[-1] if tokens else None
                if token.type == "text" and lastToken is not None and lastToken.type == "text":
                    lastToken.raw += token.raw
                    lastToken.text += token.text
                else:
                    tokens.append(token)
                continue

            # em & strong
            token = self.tokenizer.emStrong(src, maskedSrc, prevChar)
            if token:
                src = _rest(src, token.raw)
                tokens.append(token)
                continue

            # code
            token = self.tokenizer.codespan(src)
            if token:
                src = _rest(src, token.raw)
                tokens.append(token)
                continue

            # br
            token = self.tokenizer.br(src)
            if token:
                src = _rest(src, token.raw)
                tokens.append(token)
                continue

            # del (gfm)
            token = self.tokenizer.del_(src, maskedSrc, prevChar)
            if token:
                src = _rest(src, token.raw)
                tokens.append(token)
                continue

            # autolink
            token = self.tokenizer.autolink(src)
            if token:
                src = _rest(src, token.raw)
                tokens.append(token)
                continue

            # url (gfm)
            if not self.state.inLink:
                token = self.tokenizer.url(src)
                if token:
                    src = _rest(src, token.raw)
                    tokens.append(token)
                    continue

            # text
            # prevent inlineText consuming extensions by clipping 'src' to extension start
            cutSrc = src
            if extensions is not None and extensions.startInline:
                startIndex: float = math.inf
                tempSrc = src[1:]
                for getStartIndex in extensions.startInline:
                    tempStart = jsCall(getStartIndex, tempSrc)
                    if isinstance(tempStart, int | float) and not isinstance(tempStart, bool) and tempStart >= 0:
                        startIndex = min(startIndex, tempStart)
                if startIndex < math.inf and startIndex >= 0:
                    cutSrc = src[: int(startIndex) + 1]
            token = self.tokenizer.inlineText(cutSrc)
            if token:
                src = _rest(src, token.raw)
                # MISAKA fork: the last UTF-16 code unit, as upstream's `slice(-1)`.
                if jsSlice(token.raw, -1) != "_":  # Track prevChar before string of ____ started
                    prevChar = jsSlice(token.raw, -1)
                keepPrevChar = True
                lastToken = tokens[-1] if tokens else None
                if lastToken is not None and lastToken.type == "text":
                    lastToken.raw += token.raw
                    lastToken.text += token.text
                else:
                    tokens.append(token)
                continue

            if src:
                self.infiniteLoopError(ord(src[0]))
                break

        return tokens

    def infiniteLoopError(self, byte: int) -> None:
        errMsg = "Infinite loop on byte: " + str(byte)
        if self.options.silent:
            print(errMsg, file=sys.stderr)
        else:
            raise RuntimeError(errMsg)


__all__ = ["_Lexer"]

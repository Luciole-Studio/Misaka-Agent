"""Translated from marked's ``src/Hooks.ts`` (marked 18.0.11, MIT).

MISAKA fork: only the lexing side is ported (pi's TUI lexes and renders tokens itself), so ``provideParser`` is not.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .defaults import defaults
from .lexer import _Lexer


class _Hooks:
    passThroughHooks = frozenset({
        "preprocess",
        "postprocess",
        "processAllTokens",
        "emStrongMask",
    })

    passThroughHooksRespectAsync = frozenset({
        "preprocess",
        "postprocess",
        "processAllTokens",
    })

    def __init__(self, options: Any = None) -> None:
        self.options = options or defaults()
        self.block: bool | None = None

    def preprocess(self, markdown: str) -> str:
        """Process markdown before marked"""
        return markdown

    def postprocess(self, html: Any) -> Any:
        """Process HTML after marked is finished"""
        return html

    def processAllTokens(self, tokens: list[Any]) -> list[Any]:
        """Process all tokens before walk tokens"""
        return tokens

    def emStrongMask(self, src: str) -> str:
        """Mask contents that should not be interpreted as em/strong delimiters"""
        return src

    def provideLexer(self, block: bool | None = None) -> Callable[..., Any]:
        """Provide function to tokenize markdown"""
        if block is None:
            block = self.block
        return _Lexer.lex if block else _Lexer.lexInline


__all__ = ["_Hooks"]

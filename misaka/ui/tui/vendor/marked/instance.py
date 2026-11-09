"""Translated from marked's ``src/Instance.ts`` (marked 18.0.11, MIT).

MISAKA fork: only the lexing side is ported (pi's TUI lexes and renders tokens itself): ``parse``, ``parseInline``,
``parser``, renderer extensions and the async mode are not, and ``use`` refuses a ``renderer``. Tokenizer and hook
overrides, tokenizer extensions, ``walkTokens`` and ``setOptions`` are as upstream.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from misaka.utils.js import jsCall

from .defaults import _getDefaults
from .hooks import _Hooks
from .lexer import _Lexer
from .tokenizer import _Tokenizer
from .tokens import JSObject, spread

_TOKENIZER_METHODS = {
    # MISAKA fork: an override named after upstream's `def`/`del` reaches `def_`/`del_`.
    "def": "def_",
    "del": "del_",
}


class Marked:
    Lexer = _Lexer
    Tokenizer = _Tokenizer
    Hooks = _Hooks

    def __init__(self, *args: Any) -> None:
        self.defaults = _getDefaults()
        self.use(*args)

    def walkTokens(self, tokens: list[Any], callback: Callable[[Any], Any]) -> list[Any]:
        """Run callback for every token"""
        values: list[Any] = []
        for token in tokens:
            values = values + _asList(callback(token))
            if token.type == "table":
                tableToken = token
                for cell in tableToken.header:
                    values = values + self.walkTokens(cell.tokens, callback)
                for row in tableToken.rows:
                    for cell in row:
                        values = values + self.walkTokens(cell.tokens, callback)
            elif token.type == "list":
                listToken = token
                values = values + self.walkTokens(listToken.items, callback)
            else:
                genericToken = token
                extensions = self.defaults.extensions
                childTokens = extensions.childTokens if extensions is not None else None
                if childTokens and childTokens.get(genericToken.type) is not None:
                    for name in childTokens[genericToken.type]:
                        values = values + self.walkTokens(_flatten(genericToken[name]), callback)
                elif genericToken.tokens:
                    values = values + self.walkTokens(genericToken.tokens, callback)
        return values

    def use(self, *args: Any) -> Marked:
        """Registers extensions with this Marked instance.

        The `renderer`, `tokenizer`, and `hooks` objects may each contain only the
        methods that should be overridden. Their methods are merged with built-in
        behavior and extensions registered by earlier calls.

        Use this method when supplying only some hook methods.
        """
        extensions = self.defaults.extensions or JSObject(renderers={}, childTokens={})

        for pack in args:
            pack = pack if isinstance(pack, JSObject) else JSObject(pack)
            # copy options to new object
            opts = JSObject(pack)

            # set async to true if it was set to true before
            opts["async"] = self.defaults["async"] or opts["async"] or False

            # ==-- Parse "addon" extensions --== #
            if pack.extensions is not None:
                for ext in pack.extensions:
                    ext = ext if isinstance(ext, JSObject) else JSObject(ext)
                    if not ext.name:
                        raise ValueError("extension name required")
                    if "renderer" in ext:  # Renderer extensions
                        # MISAKA fork: no renderer is ported.
                        raise ValueError("renderer extensions are not supported")
                    if "tokenizer" in ext:  # Tokenizer Extensions
                        if not ext.level or (ext.level != "block" and ext.level != "inline"):
                            raise ValueError("extension level must be 'block' or 'inline'")
                        extLevel = extensions[ext.level]
                        if extLevel:
                            extLevel.insert(0, ext.tokenizer)
                        else:
                            extensions[ext.level] = [ext.tokenizer]
                        if ext.start:  # Function to check for start of token
                            if ext.level == "block":
                                if extensions.startBlock:
                                    extensions.startBlock.append(ext.start)
                                else:
                                    extensions.startBlock = [ext.start]
                            elif ext.level == "inline":
                                if extensions.startInline:
                                    extensions.startInline.append(ext.start)
                                else:
                                    extensions.startInline = [ext.start]
                    if "childTokens" in ext and ext.childTokens is not None:  # Child tokens to be visited by walkTokens
                        extensions.childTokens[ext.name] = ext.childTokens
                opts.extensions = extensions

            # ==-- Parse "overwrite" extensions --== #
            if pack.renderer:
                # MISAKA fork: no renderer is ported.
                raise ValueError("renderer overrides are not supported")
            if pack.tokenizer:
                tokenizer = self.defaults.tokenizer or _Tokenizer(self.defaults)
                overrides = pack.tokenizer
                names = (
                    list(overrides)
                    if isinstance(overrides, JSObject | dict)
                    else [name for name in vars(type(overrides)) if not name.startswith("_")]
                )
                for prop in names:
                    method = _TOKENIZER_METHODS.get(prop, prop)
                    if not hasattr(tokenizer, method):
                        raise ValueError(f"tokenizer '{prop}' does not exist")
                    if prop in ("options", "rules", "lexer"):
                        # ignore options, rules, and lexer properties
                        continue
                    tokenizerFunc = overrides[prop] if isinstance(overrides, JSObject | dict) else getattr(overrides, prop)
                    prevTokenizer = getattr(tokenizer, method)

                    # Replace tokenizer with func to run extension, but fall back if false
                    def wrapped(*args: Any, _func: Any = tokenizerFunc, _prev: Any = prevTokenizer) -> Any:
                        ret = jsCall(_func, *args)
                        if ret is False:
                            ret = _prev(*args)
                        return ret

                    setattr(tokenizer, method, wrapped)
                opts.tokenizer = tokenizer

            # ==-- Parse Hooks extensions --== #
            if pack.hooks:
                hooks = self.defaults.hooks or _Hooks()
                overrides = pack.hooks
                names = list(overrides) if isinstance(overrides, JSObject | dict) else list(vars(overrides))
                for prop in names:
                    if not hasattr(hooks, prop):
                        raise ValueError(f"hook '{prop}' does not exist")
                    if prop in ("options", "block"):
                        # ignore options and block properties
                        continue
                    hooksFunc = overrides[prop] if isinstance(overrides, JSObject | dict) else getattr(overrides, prop)
                    prevHook = getattr(hooks, prop)
                    if prop in _Hooks.passThroughHooks:

                        def passThrough(arg: Any, _func: Any = hooksFunc, _prev: Any = prevHook) -> Any:
                            ret = jsCall(_func, arg)
                            return _prev(ret)

                        setattr(hooks, prop, passThrough)
                    else:

                        def fallBack(*args: Any, _func: Any = hooksFunc, _prev: Any = prevHook) -> Any:
                            ret = jsCall(_func, *args)
                            if ret is False:
                                ret = _prev(*args)
                            return ret

                        setattr(hooks, prop, fallBack)
                opts.hooks = hooks

            # ==-- Parse WalkTokens extensions --== #
            if pack.walkTokens:
                walkTokens = self.defaults.walkTokens
                packWalktokens = pack.walkTokens

                def combined(token: Any, _pack: Any = packWalktokens, _walk: Any = walkTokens) -> list[Any]:
                    values: list[Any] = []
                    values.append(jsCall(_pack, token))
                    if _walk:
                        values = values + _asList(_walk(token))
                    return values

                opts.walkTokens = combined

            self.defaults = spread(self.defaults, opts)

        return self

    def setOptions(self, opt: Any) -> Marked:
        self.defaults = spread(self.defaults, opt if isinstance(opt, JSObject) else JSObject(opt))
        return self

    options = setOptions

    def lexer(self, src: str, options: Any = None) -> Any:
        return _Lexer.lex(src, options if options is not None else self.defaults)


def _asList(value: Any) -> list[Any]:
    """``values.concat(value)``: an array is spread, anything else is one element."""
    return list(value) if isinstance(value, list) else [value]


def _flatten(value: Any) -> list[Any]:
    """``array.flat(Infinity)``."""
    if not isinstance(value, list):
        return [value]
    flat: list[Any] = []
    for item in value:
        flat.extend(_flatten(item))
    return flat


__all__ = ["Marked"]

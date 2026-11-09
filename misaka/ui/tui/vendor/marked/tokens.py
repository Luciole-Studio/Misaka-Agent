"""marked's ``src/Tokens.ts`` (marked 18.0.11, MIT).

MISAKA fork: upstream's tokens, options and extension tables are plain objects described by TypeScript types. Here
they are ``JSObject``s, read and written through attributes as upstream spells them (``token.type``,
``token.raw += "\\n"``, ``list.items``); a property that is not set reads as ``None``, as an absent property reads as
``undefined``, and ``"name" in obj`` asks whether it is set.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


class JSObject:
    """A plain JavaScript object."""

    def __init__(self, fields: dict[str, Any] | JSObject | None = None, **more: Any) -> None:
        if fields is not None:
            self.__dict__.update(fields.__dict__ if isinstance(fields, JSObject) else fields)
        self.__dict__.update(more)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return None

    def __setattr__(self, name: str, value: Any) -> None:
        self.__dict__[name] = value

    def __getitem__(self, name: str) -> Any:
        return self.__dict__.get(name)

    def __setitem__(self, name: str, value: Any) -> None:
        self.__dict__[name] = value

    def __contains__(self, name: object) -> bool:
        return name in self.__dict__

    def __iter__(self) -> Iterator[str]:
        """``for (const key in obj)``: the own property names, in insertion order."""
        return iter(list(self.__dict__))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, JSObject):
            return self.__dict__ == other.__dict__
        if isinstance(other, dict):
            return self.__dict__ == other
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.__dict__!r})"

    def toDict(self) -> dict[str, Any]:
        """The properties as a ``dict``, nested objects included."""

        def plain(value: Any) -> Any:
            if isinstance(value, JSObject):
                return value.toDict()
            if isinstance(value, list):
                return [plain(item) for item in value]
            if isinstance(value, dict):
                return {key: plain(item) for key, item in value.items()}
            return value

        return {key: plain(value) for key, value in self.__dict__.items()}


def spread(*objects: JSObject | dict[str, Any] | None) -> JSObject:
    """``{ ...a, ...b }``: a new object with the properties of each, later ones winning."""
    merged = JSObject()
    for item in objects:
        if item is not None:
            merged.__dict__.update(item.__dict__ if isinstance(item, JSObject) else item)
    return merged


class Token(JSObject):
    """One token: ``type`` and ``raw``, plus the fields of its kind."""


class TokensList(list[Any]):
    """The block tokens of a document, with the link definitions they hold (``links``)."""

    links: dict[str, Any]


__all__ = ["JSObject", "Token", "TokensList", "spread"]

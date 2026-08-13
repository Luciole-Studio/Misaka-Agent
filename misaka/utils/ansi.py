"""ANSI-stripping helpers derived from the upstream runtime."""

from __future__ import annotations

import re


def _ansi_regex(*, only_first: bool = False) -> re.Pattern[str]:
    st = r"(?:\u0007|\u001B\u005C|\u009C)"
    osc = rf"(?:\u001B\][\s\S]*?{st})"
    csi = r"[\u001B\u009B][\[\]()#;?]*(?:\d{1,4}(?:[;:]\d{0,4})*)?[\dA-PR-TZcf-nq-uy=><~]"
    pattern = f"{osc}|{csi}"
    return re.compile(pattern)


_REGEX = _ansi_regex()


def _js_typeof(value: object) -> str:
    if value is None:
        return "object"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float, complex)) and not isinstance(value, bool):
        return "number"
    if isinstance(value, str):
        return "string"
    return "object"


def strip_ansi(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"Expected a `string`, got `{_js_typeof(value)}`")
    if "\u001B" not in value and "\u009B" not in value:
        return value
    return _REGEX.sub("", value)


stripAnsi = strip_ansi

__all__ = ["stripAnsi"]

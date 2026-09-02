"""ANSI-stripping helpers derived from the upstream runtime.

Portions of this file are derived from:
- ansi-regex (https://github.com/chalk/ansi-regex)
- strip-ansi (https://github.com/chalk/strip-ansi)

MIT License

Copyright (c) Sindre Sorhus <sindresorhus@gmail.com> (https://sindresorhus.com)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

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

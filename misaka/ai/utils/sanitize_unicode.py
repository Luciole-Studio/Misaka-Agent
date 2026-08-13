"""Unicode cleanup helpers for provider-safe JSON serialization."""

from __future__ import annotations


def sanitize_surrogates(text: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(text):
        current = ord(text[index])
        if 0xD800 <= current <= 0xDBFF:
            if index + 1 < len(text):
                next_code = ord(text[index + 1])
                if 0xDC00 <= next_code <= 0xDFFF:
                    result.extend((text[index], text[index + 1]))
                    index += 2
                    continue
            index += 1
            continue
        if 0xDC00 <= current <= 0xDFFF:
            index += 1
            continue
        result.append(text[index])
        index += 1
    return "".join(result)


sanitizeSurrogates = sanitize_surrogates

__all__ = ["sanitizeSurrogates", "sanitize_surrogates"]

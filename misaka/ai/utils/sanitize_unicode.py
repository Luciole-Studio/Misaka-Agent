r"""Translated from pi's ``packages/ai/src/utils/sanitize-unicode.ts`` (v1.1.0).

MISAKA fork: a JavaScript string is a UTF-16 sequence, so a properly paired ``\ud83d\ude00`` already *is* the emoji
and upstream's regex leaves it alone. A Python ``str`` is a sequence of code points, so the same pair arrives as two
separate surrogate code points (an OpenAI-compatible endpoint emits the halves in separate stream chunks when an emoji
straddles a token boundary) that cannot be encoded to UTF-8. Combining such a pair into the astral character keeps
upstream's contract; unpaired surrogates are dropped as upstream drops them.
"""

from __future__ import annotations

_HIGH_START, _HIGH_END = 0xD800, 0xDBFF
_LOW_START, _LOW_END = 0xDC00, 0xDFFF


def sanitizeSurrogates(text: str) -> str:
    """Removes unpaired Unicode surrogate characters from a string.

    Unpaired surrogates (high surrogates 0xD800-0xDBFF without matching low surrogates 0xDC00-0xDFFF,
    or vice versa) cause JSON serialization errors in many API providers.

    Valid emoji and other characters outside the Basic Multilingual Plane use properly paired
    surrogates and will NOT be affected by this function.
    """
    result: list[str] = []
    index = 0
    while index < len(text):
        current = ord(text[index])
        if _HIGH_START <= current <= _HIGH_END and index + 1 < len(text):
            following = ord(text[index + 1])
            if _LOW_START <= following <= _LOW_END:
                result.append(
                    chr(0x10000 + ((current - _HIGH_START) << 10) + (following - _LOW_START))
                )
                index += 2
                continue
        if _HIGH_START <= current <= _LOW_END:
            index += 1  # unpaired, on either side: dropped, as upstream drops it
            continue
        result.append(text[index])
        index += 1
    return "".join(result)


__all__ = ["sanitizeSurrogates"]

r"""Unicode cleanup helpers for provider-safe JSON serialization.

Ported from pi's ``utils/sanitize-unicode.ts``, whose contract is: unpaired surrogates
are dropped, and "valid emoji and other characters outside the Basic Multilingual Plane
use properly paired surrogates and will NOT be affected".

That contract needs different code in Python than the regex upstream uses. A JavaScript
string is a UTF-16 sequence, so a properly paired ``\ud83d\ude00`` already *is* the
emoji and preserving it is a no-op. A Python ``str`` is a sequence of code points, so the
same pair arrives as two separate surrogate code points -- an object that is not a
character, cannot be encoded to UTF-8, and takes the next provider request down with
``UnicodeEncodeError``. Preserving it literally would keep the bytes and lose the
contract. Combining it into the astral character keeps the contract, which is why this
function decodes pairs rather than passing them through.

The pair really does arrive split: an OpenAI-compatible endpoint emits ``\ud83d`` and
``\ude00`` in separate stream chunks when an emoji straddles a token boundary, and
``json.loads`` only recombines the two halves when they land in the *same* chunk.
"""

from __future__ import annotations

_HIGH_START, _HIGH_END = 0xD800, 0xDBFF
_LOW_START, _LOW_END = 0xDC00, 0xDFFF


def sanitize_surrogates(text: str) -> str:
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


__all__ = ["sanitize_surrogates"]

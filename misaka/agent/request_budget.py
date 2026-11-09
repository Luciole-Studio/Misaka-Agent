"""How many tokens a provider request can cost: an upper bound on its input, and what it used.

The token-cap meter (``misaka.core.platform.metering``) leases each request's worst case from these
before it is sent, and records what it used when it ends.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import math
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from misaka.ai.utils.typebox_helpers import typeboxSchema
from misaka.utils.values import read_field

CONTEXT_FRAMING_TOKENS = 4096
# Providers that bill a flat amount per image, whatever its size, stay under this.
IMAGE_TOKEN_FLOOR = 2560


def usage_tokens(usage: Any) -> int:
    total = read_field(usage, "totalTokens")
    if total is not None:
        return max(0, int(total or 0))
    return max(
        0,
        sum(
            int(read_field(usage, key, 0) or 0)
            for key in (
                "input",
                "output",
                "cacheRead",
                "cacheWrite",
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
        ),
    )


def image_token_upper_bound(width: int, height: int) -> int:
    """Most tokens a supported provider charges for one image of this size.

    Each term is one provider family's published accounting, taken on the unscaled size
    (providers only ever scale down): Anthropic's w*h/750, OpenAI's 32 px patches at the
    largest per-model multiplier (2.46), OpenAI's 512 px tiles at 170 each plus 85, and
    Gemini's 768 px tiles at 258 each.
    """
    return max(
        math.ceil(width * height / 750),
        math.ceil(math.ceil(width / 32) * math.ceil(height / 32) * 2.46),
        math.ceil(width / 512) * math.ceil(height / 512) * 170 + 85,
        math.ceil(width / 768) * math.ceil(height / 768) * 258,
        IMAGE_TOKEN_FLOOR,
    )


# Keyed by (length, hash), never by the base64 itself: every request re-measures the same images,
# and a cache holding the strings would keep megabytes of image data alive after the context
# that carried them has been compacted away.
_IMAGE_SIZES: OrderedDict[tuple[int, int], tuple[int, int] | None] = OrderedDict()
_IMAGE_SIZES_KEPT = 256


def _image_size(data: str) -> tuple[int, int] | None:
    """Pixel size of a base64 image, read from its header; None when it cannot be read."""
    key = (len(data), hash(data))
    if key in _IMAGE_SIZES:
        _IMAGE_SIZES.move_to_end(key)
        return _IMAGE_SIZES[key]
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(base64.b64decode(data, validate=False))) as image:
            size = image.size
    except (binascii.Error, ValueError, OSError, UnidentifiedImageError):
        size = None
    _IMAGE_SIZES[key] = size
    if len(_IMAGE_SIZES) > _IMAGE_SIZES_KEPT:
        _IMAGE_SIZES.popitem(last=False)
    return size


def context_token_upper_bound(context: Any) -> int:
    """Conservatively bound tokenized input, including tools.

    Supported provider tokenizers cannot produce more ordinary text tokens
    than the number of UTF-8 bytes.  The fixed allowance covers provider
    message/tool framing that is not represented in ``Context`` itself.
    An image is not text: its base64 is replaced by the bound for its pixel
    size (a 400 KB screenshot is a few thousand tokens, not 530,000). One the
    header cannot be read from is counted as text, as before.
    """

    image_tokens = 0

    def without_images(value: Any) -> Any:
        nonlocal image_tokens
        if isinstance(value, dict):
            data = value.get("data")
            if value.get("type") == "image" and isinstance(data, str) and (size := _image_size(data)):
                image_tokens += image_token_upper_bound(*size)
                return {**value, "data": ""}
            return {key: without_images(item) for key, item in value.items()}
        if isinstance(value, list):
            return [without_images(item) for item in value]
        return value

    def jsonable(value: Any) -> Any:
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json", exclude_none=True)
        if isinstance(value, Mapping):
            return {str(key): jsonable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [jsonable(item) for item in value]
        return value

    tools = []
    for tool in read_field(context, "tools", []) or []:
        parameters = read_field(tool, "parameters", {})
        if hasattr(tool, "parameters_json_schema"):
            parameters = tool.parameters_json_schema()
        elif isinstance(parameters, type) and hasattr(parameters, "model_json_schema"):
            parameters = typeboxSchema(parameters)
        tools.append(
            {
                "name": str(read_field(tool, "name", "")),
                "description": str(read_field(tool, "description", "")),
                "parameters": jsonable(parameters),
            }
        )
    payload = {
        "systemPrompt": read_field(context, "systemPrompt"),
        "messages": without_images(jsonable(read_field(context, "messages", []) or [])),
        "tools": tools,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return len(encoded) + image_tokens + CONTEXT_FRAMING_TOKENS


__all__ = [
    "CONTEXT_FRAMING_TOKENS",
    "context_token_upper_bound",
    "image_token_upper_bound",
    "usage_tokens",
]

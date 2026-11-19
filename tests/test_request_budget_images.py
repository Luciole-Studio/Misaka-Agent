"""An image in the context is bounded by its pixel size, not by the length of its base64.

Counting the base64 as text made one 400 KB screenshot cost ~530,000 budget tokens, so a card
with a modest turn cap could not send a single request that carried an image.
"""
import base64
import io

from PIL import Image

from misaka.agent import request_budget
from misaka.agent.request_budget import (
    CONTEXT_FRAMING_TOKENS,
    context_token_upper_bound,
    image_token_upper_bound,
)
from misaka.ai.types import Context, ImageContent, TextContent, UserMessage


def _png(width, height):
    buffer = io.BytesIO()
    Image.effect_noise((width, height), 100).convert("RGB").save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def _context(*content):
    return Context(systemPrompt="system", messages=[UserMessage(content=list(content), timestamp=0)])


def test_an_image_costs_its_pixel_bound_not_its_base64():
    data = _png(600, 400)
    assert len(data) > 400_000
    with_image = context_token_upper_bound(_context(TextContent(text="look"), ImageContent(data=data, mimeType="image/png")))
    text_only = context_token_upper_bound(_context(TextContent(text="look")))
    assert with_image - text_only <= image_token_upper_bound(600, 400) + 100
    assert with_image < 20_000


def test_the_pixel_bound_covers_every_provider_scheme():
    for width, height in [(1, 1), (32, 32), (2000, 1), (1568, 1568), (2000, 2000)]:
        bound = image_token_upper_bound(width, height)
        assert bound >= width * height / 750
        assert bound >= -(-width // 32) * -(-height // 32) * 2.46
        assert bound >= -(-width // 512) * -(-height // 512) * 170 + 85
        assert bound >= -(-width // 768) * -(-height // 768) * 258


def test_an_unreadable_image_is_still_counted_as_text():
    data = "not really an image" * 1000
    bound = context_token_upper_bound(_context(ImageContent(data=data, mimeType="image/png")))
    assert bound > len(data) + CONTEXT_FRAMING_TOKENS - 100


def test_text_contexts_are_bounded_as_before():
    text = "数据" * 1000
    bound = context_token_upper_bound(_context(TextContent(text=text)))
    assert bound > len(text.encode()) + CONTEXT_FRAMING_TOKENS


def test_measured_images_are_not_kept_alive():
    data = _png(64, 48)
    request_budget._image_size(data)
    assert all(isinstance(key, tuple) and not any(isinstance(part, str) for part in key)
               for key in request_budget._IMAGE_SIZES)
    assert request_budget._image_size(data) == (64, 48)

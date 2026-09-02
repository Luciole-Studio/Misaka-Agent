"""Display-only Markdown transforms registered by extensions."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Literal

from misaka.core.extensions.types import MarkdownTransformContext, MarkdownTransformer


def create_markdown_transform(
    message_type: Literal["user", "assistant", "assistant-thinking"],
    is_streaming: Callable[[], bool],
    transformers: Sequence[MarkdownTransformer],
) -> Callable[[str, int], str]:
    def transform(markdown: str, available_width: int) -> str:
        context: MarkdownTransformContext = {
            "messageType": message_type,
            "isStreaming": is_streaming(),
            "availableWidth": available_width,
        }
        transformed_markdown = markdown
        for transformer in transformers:
            try:
                transformed = transformer(transformed_markdown, context)
            except Exception:  # noqa: BLE001 - one extension must not break later transformers
                transformed = None
            if isinstance(transformed, str):
                transformed_markdown = transformed
        return transformed_markdown

    return transform


__all__ = ["create_markdown_transform"]

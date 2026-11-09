"""Translated from pi's ``modes/interactive/components/markdown-transform.ts`` (v1.1.0)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Literal

from misaka.core.extensions.types import MarkdownTransformContext, MarkdownTransformer


def createMarkdownTransform(
    messageType: Literal["user", "assistant", "assistant-thinking"],
    isStreaming: bool,
    transformers: Sequence[MarkdownTransformer],
) -> Callable[[str, int], str]:
    return lambda markdown, availableWidth: applyMarkdownTransformers(
        markdown, {"messageType": messageType, "isStreaming": isStreaming, "availableWidth": availableWidth}, transformers
    )


def applyMarkdownTransformers(
    markdown: str,
    context: MarkdownTransformContext,
    transformers: Sequence[MarkdownTransformer],
) -> str:
    transformedMarkdown = markdown
    for transformer in transformers:
        try:
            transformed = transformer(transformedMarkdown, context)
            if isinstance(transformed, str):
                transformedMarkdown = transformed
        except Exception:  # noqa: BLE001, S110 - keep the current Markdown and continue with the next transformer
            pass
    return transformedMarkdown

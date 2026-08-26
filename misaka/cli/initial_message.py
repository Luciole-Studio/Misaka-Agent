"""Initial prompt assembly helpers for CLI mode."""

from __future__ import annotations

from dataclasses import dataclass

from misaka.ai.types import ImageContent
from misaka.cli.args import Args


@dataclass(slots=True)
class InitialMessageResult:
    initialMessage: str | None = None
    initialImages: list[ImageContent] | None = None


def build_initial_message(
    *,
    parsed: Args,
    fileText: str | None = None,
    fileImages: list[ImageContent] | None = None,
    stdinContent: str | None = None,
) -> InitialMessageResult:
    parts: list[str] = []
    if stdinContent is not None:
        parts.append(stdinContent)
    if fileText:
        parts.append(fileText)
    if parsed.messages:
        parts.append(parsed.messages.pop(0))

    return InitialMessageResult(
        initialMessage="".join(parts) if parts else None,
        initialImages=fileImages if fileImages else None,
    )


__all__ = ["InitialMessageResult", "build_initial_message"]

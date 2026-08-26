"""Initial prompt assembly helpers for CLI mode."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

from misaka.ai.types import ImageContent
from misaka.cli.args import Args


class _InitialMessageInputRequired(TypedDict):
    parsed: Args


class InitialMessageInput(_InitialMessageInputRequired, total=False):
    fileText: str
    fileImages: list[ImageContent]
    stdinContent: str


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


buildInitialMessage = build_initial_message

__all__ = ["InitialMessageInput", "InitialMessageResult", "buildInitialMessage"]

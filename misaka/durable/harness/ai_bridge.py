"""MISAKA fork: the bridge between the Harness's JSON messages and the pi-ai layer's message models (no upstream file).

The Harness stores and passes messages, usages and calls as plain ``dict`` objects with the pi-ai key names, as pi's
durable package does with plain objects; ``misaka.ai`` works on pydantic models. These helpers convert at the boundary
and read the two shapes the way pi-ai's own utilities do.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from misaka.ai.types import AssistantMessage, Context, Usage, validate_message
from misaka.ai.utils.estimate import calculateContextTokens as _calculateContextTokens
from misaka.ai.utils.estimate import estimateMessageTokens as _estimateMessageTokens
from misaka.ai.utils.overflow import isContextOverflow as _isContextOverflow
from misaka.ai.utils.retry import RetryPolicy
from misaka.ai.utils.retry import (
    isRetryableAssistantError as _isRetryableAssistantError,
)
from misaka.ai.utils.retry import retryDelayMs as _retryDelayMs


def toMessageDict(message: Any) -> dict[str, Any]:
    """A pi-ai message model as strict JSON, the shape entries store."""
    return message.model_dump(mode="json", exclude_none=True)  # type: ignore[no-any-return]


def toContext(messages: Sequence[dict[str, Any]]) -> Context:
    """A pi-ai request context over JSON messages."""
    return Context(messages=[validate_message(message) for message in messages])


def estimateMessageTokens(message: dict[str, Any]) -> int:
    return _estimateMessageTokens(validate_message(message))


def calculateContextTokens(usage: dict[str, Any]) -> int:
    """The provider's own total, falling back to the sum of its parts."""
    return _calculateContextTokens(Usage.model_construct(**usage))


def isContextOverflow(message: dict[str, Any]) -> bool:
    return _isContextOverflow(AssistantMessage.model_validate(message))


def isRetryableAssistantError(message: dict[str, Any]) -> bool:
    return _isRetryableAssistantError(AssistantMessage.model_validate(message))


def retryDelayMs(policy: dict[str, Any], attempt: int) -> int:
    return _retryDelayMs(
        RetryPolicy(
            enabled=policy["enabled"],
            maxRetries=policy["maxRetries"],
            baseDelayMs=policy["baseDelayMs"],
            maxAgentDelayMs=policy.get("maxAgentDelayMs"),
        ),
        attempt,
    )


__all__ = [
    "calculateContextTokens",
    "estimateMessageTokens",
    "isContextOverflow",
    "isRetryableAssistantError",
    "retryDelayMs",
    "toContext",
    "toMessageDict",
]

"""Translated from pi's ``packages/coding-agent/src/core/messages.ts`` (v1.1.0).

Custom message types and transformers for the coding agent.

Extends the base AgentMessage type with coding-agent specific message types,
and provides a transformer to convert them to LLM-compatible messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeVar

from misaka.agent.types import AgentMessage
from misaka.ai.types import (
    ImageContent,
    MessageValue,
    TextContent,
    UserMessage,
    validate_message,
)
from misaka.utils.values import read_field

COMPACTION_SUMMARY_PREFIX = """The conversation history before this point was compacted into the following summary:

<summary>
"""
COMPACTION_SUMMARY_SUFFIX = """
</summary>"""
BRANCH_SUMMARY_PREFIX = """The following is a summary of a branch that this conversation came back from:

<summary>
"""
BRANCH_SUMMARY_SUFFIX = "</summary>"

TDetails = TypeVar("TDetails")


@dataclass(slots=True)
class BashExecutionMessage:
    """Message type for bash executions via the ! command."""

    command: str
    output: str
    exitCode: int | None
    cancelled: bool
    truncated: bool
    timestamp: int
    fullOutputPath: str | None = None
    # If true, this message is excluded from LLM context (!! prefix)
    excludeFromContext: bool | None = None
    role: str = field(default="bashExecution", init=False)


@dataclass(slots=True)
class CustomMessage[TDetails]:
    """Message type for extension-injected messages via sendMessage().
    These are custom messages that extensions can inject into the conversation.
    """

    customType: str
    content: str | list[TextContent | ImageContent]
    display: bool
    timestamp: int
    details: TDetails | None = None
    role: str = field(default="custom", init=False)


@dataclass(slots=True)
class BranchSummaryMessage:
    summary: str
    fromId: str | None
    timestamp: int
    role: str = field(default="branchSummary", init=False)


@dataclass(slots=True)
class CompactionSummaryMessage:
    summary: str
    tokensBefore: int
    timestamp: int
    role: str = field(default="compactionSummary", init=False)


def bashExecutionToText(msg: BashExecutionMessage) -> str:
    """Convert a BashExecutionMessage to user message text for LLM context."""
    text = f"Ran `{msg.command}`\n"
    if msg.output:
        text += f"```\n{msg.output}\n```"
    else:
        text += "(no output)"
    if msg.cancelled:
        text += "\n\n(command cancelled)"
    elif msg.exitCode not in (None, 0):
        text += f"\n\nCommand exited with code {msg.exitCode}"
    if msg.truncated and msg.fullOutputPath:
        text += f"\n\n[Output truncated. Full output: {msg.fullOutputPath}]"
    return text


def createBranchSummaryMessage(
    summary: str,
    fromId: str,
    timestamp: str | int | datetime,
) -> BranchSummaryMessage:
    return BranchSummaryMessage(summary=summary, fromId=fromId, timestamp=_timestamp_ms(timestamp))


def createCompactionSummaryMessage(
    summary: str,
    tokensBefore: int,
    timestamp: str | int | datetime,
) -> CompactionSummaryMessage:
    return CompactionSummaryMessage(
        summary=summary,
        tokensBefore=tokensBefore,
        timestamp=_timestamp_ms(timestamp),
    )


def createCustomMessage(
    customType: str,
    content: str | list[TextContent | ImageContent],
    display: bool,
    details: Any,
    timestamp: str | int | datetime,
) -> CustomMessage[Any]:
    """Convert CustomMessageEntry to AgentMessage format"""
    return CustomMessage(
        customType=customType,
        content=content,
        display=display,
        details=details,
        timestamp=_timestamp_ms(timestamp),
    )


def convertToLlm(messages: list[AgentMessage]) -> list[MessageValue]:
    """Transform AgentMessages (including custom types) to LLM-compatible Messages.

    This is used by:
    - Agent's transormToLlm option (for prompt calls and queued messages)
    - Compaction's generateSummary (for summarization)
    - Custom extensions and tools
    """
    converted: list[MessageValue] = []
    for message in messages:
        role = read_field(message, "role")
        if role == "bashExecution":
            # Skip messages excluded from context (!! prefix)
            if read_field(message, "excludeFromContext"):
                continue
            payload = {
                "role": "user",
                "content": [{"type": "text", "text": bashExecutionToText(_coerce_bash_execution(message))}],
                "timestamp": int(read_field(message, "timestamp")),
            }
            converted.append(validate_message(payload))
            continue
        if role == "custom":
            content = read_field(message, "content")
            if isinstance(content, str):
                content = [TextContent(text=content)]
            converted.append(
                UserMessage(
                    content=content,
                    timestamp=int(read_field(message, "timestamp")),
                )
            )
            continue
        if role == "branchSummary":
            summary_text = BRANCH_SUMMARY_PREFIX + str(read_field(message, "summary")) + BRANCH_SUMMARY_SUFFIX
            converted.append(
                UserMessage(
                    content=[TextContent(text=summary_text)],
                    timestamp=int(read_field(message, "timestamp")),
                )
            )
            continue
        if role == "compactionSummary":
            converted.append(
                UserMessage(
                    content=[
                        TextContent(
                            text=COMPACTION_SUMMARY_PREFIX
                            + str(read_field(message, "summary"))
                            + COMPACTION_SUMMARY_SUFFIX
                        )
                    ],
                    timestamp=int(read_field(message, "timestamp")),
                )
            )
            continue
        if role in {"system", "user", "assistant", "toolResult"}:
            converted.append(validate_message(_message_dump(message)))
    return converted


def _message_dump(message: Any) -> Any:
    if hasattr(message, "model_dump"):
        return message.model_dump()
    if isinstance(message, dict):
        return dict(message)
    return message


def _coerce_bash_execution(message: Any) -> BashExecutionMessage:
    if isinstance(message, BashExecutionMessage):
        return message
    return BashExecutionMessage(
        command=str(read_field(message, "command")),
        output=str(read_field(message, "output") or ""),
        exitCode=read_field(message, "exitCode"),
        cancelled=bool(read_field(message, "cancelled")),
        truncated=bool(read_field(message, "truncated")),
        fullOutputPath=read_field(message, "fullOutputPath"),
        timestamp=int(read_field(message, "timestamp")),
    )


def _timestamp_ms(timestamp: str | int | datetime) -> int:
    """``new Date(timestamp).getTime()``: a date-only ISO string is UTC midnight, a date-time without an offset is
    local time."""
    if isinstance(timestamp, bool):
        raise TypeError("timestamp must be an ISO string, datetime, or Unix milliseconds")
    if isinstance(timestamp, int):
        return timestamp
    if isinstance(timestamp, datetime):
        dt = timestamp
    else:
        dt = datetime.fromisoformat(timestamp)
        if "T" not in timestamp and " " not in timestamp.strip():
            dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


__all__ = [
    "BRANCH_SUMMARY_PREFIX",
    "BRANCH_SUMMARY_SUFFIX",
    "COMPACTION_SUMMARY_PREFIX",
    "COMPACTION_SUMMARY_SUFFIX",
    "BashExecutionMessage",
    "BranchSummaryMessage",
    "CompactionSummaryMessage",
    "CustomMessage",
    "bashExecutionToText",
    "convertToLlm",
    "createBranchSummaryMessage",
    "createCompactionSummaryMessage",
    "createCustomMessage",
]

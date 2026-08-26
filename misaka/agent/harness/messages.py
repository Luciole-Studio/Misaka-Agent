"""Harness message helpers used for prompt assembly."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypeVar

from misaka.ai.types import ImageContent, MessageValue, TextContent, UserMessage, validate_message

from misaka.agent.types import AgentMessage

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
    command: str
    output: str
    exitCode: int | None
    cancelled: bool
    truncated: bool
    timestamp: int
    fullOutputPath: str | None = None
    excludeFromContext: bool | None = None
    role: str = field(default="bashExecution", init=False)


@dataclass(slots=True)
class CustomMessage[TDetails]:
    customType: str
    content: str | list[TextContent | ImageContent]
    display: bool
    timestamp: int
    details: TDetails | None = None
    role: str = field(default="custom", init=False)


@dataclass(slots=True)
class BranchSummaryMessage:
    summary: str
    fromId: str
    timestamp: int
    role: str = field(default="branchSummary", init=False)


@dataclass(slots=True)
class CompactionSummaryMessage:
    summary: str
    tokensBefore: int
    timestamp: int
    role: str = field(default="compactionSummary", init=False)


def bash_execution_to_text(msg: BashExecutionMessage) -> str:
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


def create_branch_summary_message(summary: str, from_id: str, timestamp: str) -> BranchSummaryMessage:
    return BranchSummaryMessage(summary=summary, fromId=from_id, timestamp=_timestamp_ms(timestamp))


def create_compaction_summary_message(
    summary: str,
    tokens_before: int,
    timestamp: str,
) -> CompactionSummaryMessage:
    return CompactionSummaryMessage(
        summary=summary,
        tokensBefore=tokens_before,
        timestamp=_timestamp_ms(timestamp),
    )


def create_custom_message(
    custom_type: str,
    content: str | list[TextContent | ImageContent],
    display: bool,
    details: Any,
    timestamp: str,
) -> CustomMessage[Any]:
    return CustomMessage(
        customType=custom_type,
        content=content,
        display=display,
        details=details,
        timestamp=_timestamp_ms(timestamp),
    )


def convert_to_llm(messages: list[AgentMessage]) -> list[MessageValue]:
    converted: list[MessageValue] = []
    for message in messages:
        role = _message_field(message, "role")
        if role == "bashExecution":
            if _message_field(message, "excludeFromContext"):
                continue
            payload = {
                "role": "user",
                "content": [{"type": "text", "text": bash_execution_to_text(_coerce_bash_execution(message))}],
                "timestamp": int(_message_field(message, "timestamp")),
            }
            converted.append(validate_message(payload))
            continue
        if role == "custom":
            content = _message_field(message, "content")
            if isinstance(content, str):
                content = [TextContent(text=content)]
            converted.append(
                UserMessage(
                    content=content,
                    timestamp=int(_message_field(message, "timestamp")),
                )
            )
            continue
        if role == "branchSummary":
            summary_text = BRANCH_SUMMARY_PREFIX + str(_message_field(message, "summary")) + BRANCH_SUMMARY_SUFFIX
            converted.append(
                UserMessage(
                    content=[TextContent(text=summary_text)],
                    timestamp=int(_message_field(message, "timestamp")),
                )
            )
            continue
        if role == "compactionSummary":
            converted.append(
                UserMessage(
                    content=[
                        TextContent(
                            text=COMPACTION_SUMMARY_PREFIX
                            + str(_message_field(message, "summary"))
                            + COMPACTION_SUMMARY_SUFFIX
                        )
                    ],
                    timestamp=int(_message_field(message, "timestamp")),
                )
            )
            continue
        if role in {"user", "assistant", "toolResult"}:
            converted.append(validate_message(_message_dump(message)))
    return converted


def _message_field(message: Any, name: str) -> Any:
    if isinstance(message, dict):
        return message.get(name)
    return getattr(message, name, None)


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
        command=str(_message_field(message, "command")),
        output=str(_message_field(message, "output") or ""),
        exitCode=_message_field(message, "exitCode"),
        cancelled=bool(_message_field(message, "cancelled")),
        truncated=bool(_message_field(message, "truncated")),
        fullOutputPath=_message_field(message, "fullOutputPath"),
        timestamp=int(_message_field(message, "timestamp")),
    )


def _timestamp_ms(timestamp: str | datetime) -> int:
    if isinstance(timestamp, datetime):
        dt = timestamp
    else:
        normalized = timestamp.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    return int(dt.timestamp() * 1000)


bashExecutionToText = bash_execution_to_text
createBranchSummaryMessage = create_branch_summary_message
createCompactionSummaryMessage = create_compaction_summary_message
createCustomMessage = create_custom_message
convertToLlm = convert_to_llm

__all__ = [
    "BRANCH_SUMMARY_PREFIX",
    "BRANCH_SUMMARY_SUFFIX",
    "BashExecutionMessage",
    "BranchSummaryMessage",
    "COMPACTION_SUMMARY_PREFIX",
    "COMPACTION_SUMMARY_SUFFIX",
    "CompactionSummaryMessage",
    "CustomMessage",
    "bashExecutionToText",
    "bash_execution_to_text",
    "convertToLlm",
    "convert_to_llm",
    "createBranchSummaryMessage",
    "createCompactionSummaryMessage",
    "createCustomMessage",
    "create_branch_summary_message",
    "create_compaction_summary_message",
    "create_custom_message",
]

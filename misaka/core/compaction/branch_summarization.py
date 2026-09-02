"""Branch summarization helpers for coding-agent tree navigation."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, TypedDict

from misaka.agent.harness.session.uuid import uuidv7
from misaka.agent.types import AgentMessage, StreamFn
from misaka.ai.types import Model, SimpleStreamOptions, Usage, UserMessage
from misaka.ai.utils.retry import RetryCallbacks, RetryPolicy
from misaka.core.compaction.compaction import (
    complete_summarization,
    estimate_tokens,
    get_summarization_failure,
)
from misaka.core.compaction.utils import (
    SUMMARIZATION_SYSTEM_PROMPT,
    FileOperations,
    _assistant_text,
    compute_file_lists,
    create_file_ops,
    extract_file_ops_from_message,
    format_file_operations,
    serialize_conversation,
)
from misaka.core.messages import (
    convertToLlm,
    createBranchSummaryMessage,
    createCompactionSummaryMessage,
    createCustomMessage,
)
from misaka.core.session_manager import ReadonlySessionManager, SessionEntry
from misaka.utils.values import read_field

_BRANCH_SUMMARY_PREAMBLE = """The user explored a different conversation branch before returning here.
Summary of that exploration:

"""

_BRANCH_SUMMARY_PROMPT = """Create a structured summary of this conversation branch for context when returning later.

Use this EXACT format:

## Goal
[What was the user trying to accomplish in this branch?]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned]
- [Or "(none)" if none were mentioned]

## Progress
### Done
- [x] [Completed tasks/changes]

### In Progress
- [ ] [Work that was started but not finished]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [What should happen next to continue this work]

Keep each section concise. Preserve exact file paths, function names, and error messages."""


@dataclass(slots=True)
class BranchSummaryResult:
    summary: str | None = None
    usage: Usage | None = None
    readFiles: list[str] | None = None
    modifiedFiles: list[str] | None = None
    aborted: bool | None = None
    error: str | None = None


class BranchSummaryDetails(TypedDict):
    readFiles: list[str]
    modifiedFiles: list[str]


@dataclass(slots=True)
class BranchPreparation:
    messages: list[AgentMessage]
    fileOps: FileOperations
    totalTokens: int


@dataclass(slots=True)
class CollectEntriesResult:
    entries: list[SessionEntry]
    commonAncestorId: str | None


@dataclass(slots=True)
class GenerateBranchSummaryOptions:
    model: Model[Any]
    apiKey: str | None
    signal: Any
    headers: dict[str, str] | None = None
    env: dict[str, str] | None = None
    customInstructions: str | None = None
    replaceInstructions: bool | None = None
    reserveTokens: int | None = None
    streamFn: StreamFn | None = None
    retry: RetryPolicy | None = None
    callbacks: RetryCallbacks | None = None


def collect_entries_for_branch_summary(
    session: ReadonlySessionManager,
    old_leaf_id: str | None,
    target_id: str,
) -> CollectEntriesResult:
    if not old_leaf_id:
        return CollectEntriesResult(entries=[], commonAncestorId=None)

    old_path = {entry["id"] for entry in session.getBranch(old_leaf_id) if isinstance(entry.get("id"), str)}
    target_path = session.getBranch(target_id)
    common_ancestor_id: str | None = None
    for entry in reversed(target_path):
        entry_id = entry.get("id")
        if isinstance(entry_id, str) and entry_id in old_path:
            common_ancestor_id = entry_id
            break

    entries: list[SessionEntry] = []
    current = old_leaf_id
    while current and current != common_ancestor_id:
        entry = session.getEntry(current)
        if entry is None:
            break
        entries.append(entry)
        parent_id = entry.get("parentId")
        current = parent_id if isinstance(parent_id, str) else None
    entries.reverse()
    return CollectEntriesResult(entries=entries, commonAncestorId=common_ancestor_id)


def prepare_branch_entries(entries: list[SessionEntry], token_budget: int = 0) -> BranchPreparation:
    messages: list[AgentMessage] = []
    file_ops = create_file_ops()
    total_tokens = 0

    for entry in entries:
        if entry.get("type") != "branch_summary" or entry.get("fromHook"):
            continue
        details = entry.get("details")
        read_files = details.get("readFiles") if isinstance(details, dict) else getattr(details, "readFiles", None)
        modified_files = (
            details.get("modifiedFiles") if isinstance(details, dict) else getattr(details, "modifiedFiles", None)
        )
        if isinstance(read_files, list):
            for path in read_files:
                if isinstance(path, str):
                    file_ops.read.add(path)
        if isinstance(modified_files, list):
            for path in modified_files:
                if isinstance(path, str):
                    file_ops.edited.add(path)

    for entry in reversed(entries):
        message = _get_message_from_entry(entry)
        if message is None:
            continue
        extract_file_ops_from_message(message, file_ops)
        tokens = estimate_tokens(message)
        if token_budget > 0 and total_tokens + tokens > token_budget:
            entry_type = entry.get("type")
            if entry_type in {"compaction", "branch_summary"} and total_tokens < token_budget * 0.9:
                messages.insert(0, message)
                total_tokens += tokens
            break

        messages.insert(0, message)
        total_tokens += tokens

    return BranchPreparation(messages=messages, fileOps=file_ops, totalTokens=total_tokens)


async def generate_branch_summary(
    entries: list[SessionEntry],
    options: GenerateBranchSummaryOptions,
) -> BranchSummaryResult:
    reserve_tokens = options.reserveTokens if options.reserveTokens is not None else 16384
    context_window = options.model.contextWindow or 128000
    token_budget = context_window - reserve_tokens
    preparation = prepare_branch_entries(entries, token_budget)

    if not preparation.messages:
        return BranchSummaryResult(summary="No content to summarize")

    if options.replaceInstructions and options.customInstructions:
        instructions = options.customInstructions
    elif options.customInstructions:
        instructions = f"{_BRANCH_SUMMARY_PROMPT}\n\nAdditional focus: {options.customInstructions}"
    else:
        instructions = _BRANCH_SUMMARY_PROMPT

    prompt_text = (
        "<conversation>\n"
        f"{serialize_conversation(convertToLlm(preparation.messages))}\n"
        f"</conversation>\n\n{instructions}"
    )
    response = await complete_summarization(
        options.model,
        {
            "systemPrompt": SUMMARIZATION_SYSTEM_PROMPT,
            "messages": [UserMessage(content=[{"type": "text", "text": prompt_text}], timestamp=_timestamp_ms())],
        },
        SimpleStreamOptions(
            apiKey=options.apiKey,
            headers=options.headers,
            env=options.env,
            signal=options.signal,
            maxTokens=2048,
            cacheRetention="none",
            sessionId=uuidv7(),
        ),
        options.streamFn,
        options.retry,
        options.callbacks,
    )
    if response.stopReason == "aborted":
        return BranchSummaryResult(aborted=True)
    failure = get_summarization_failure(response, "Branch summarization")
    if failure:
        return BranchSummaryResult(error=failure)
    if any(read_field(block, "type") == "toolCall" for block in response.content):
        return BranchSummaryResult(error="Branch summarization attempted to call a tool")

    summary = _BRANCH_SUMMARY_PREAMBLE + _assistant_text(response)
    file_lists = compute_file_lists(preparation.fileOps)
    summary += format_file_operations(file_lists["readFiles"], file_lists["modifiedFiles"])
    return BranchSummaryResult(
        summary=summary or "No summary generated",
        usage=response.usage,
        readFiles=file_lists["readFiles"],
        modifiedFiles=file_lists["modifiedFiles"],
    )


def _get_message_from_entry(entry: SessionEntry) -> AgentMessage | None:
    entry_type = entry.get("type")
    if entry_type == "message":
        message = entry.get("message")
        if read_field(message, "role") == "toolResult":
            return None
        return message
    if entry_type == "custom_message":
        return createCustomMessage(
            entry.get("customType"),
            entry.get("content"),
            entry.get("display"),
            entry.get("details"),
            entry.get("timestamp"),
        )
    if entry_type == "branch_summary":
        return createBranchSummaryMessage(
            entry.get("summary"),
            entry.get("fromId"),
            entry.get("timestamp"),
        )
    if entry_type == "compaction":
        return createCompactionSummaryMessage(
            entry.get("summary"),
            entry.get("tokensBefore"),
            entry.get("timestamp"),
        )
    return None


def _timestamp_ms() -> int:
    return int(time.time() * 1000)


collectEntriesForBranchSummary = collect_entries_for_branch_summary
generateBranchSummary = generate_branch_summary
prepareBranchEntries = prepare_branch_entries

__all__ = [
    "BranchPreparation",
    "BranchSummaryDetails",
    "BranchSummaryResult",
    "CollectEntriesResult",
    "FileOperations",
    "GenerateBranchSummaryOptions",
    "collectEntriesForBranchSummary",
    "collect_entries_for_branch_summary",
    "generateBranchSummary",
    "generate_branch_summary",
    "prepareBranchEntries",
    "prepare_branch_entries",
]

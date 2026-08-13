"""Branch-summary helpers for navigating across session-tree forks."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from misaka.ai.stream import complete_simple
from misaka.ai.types import SimpleStreamOptions, UserMessage

from misaka.agent.harness.compaction.compaction import SUMMARIZATION_SYSTEM_PROMPT, estimate_tokens
from misaka.agent.harness.compaction.utils import (
    FileOperations,
    compute_file_lists,
    create_file_ops,
    extract_file_ops_from_message,
    format_file_operations,
    serialize_conversation,
)
from misaka.agent.harness.messages import (
    convert_to_llm,
    create_branch_summary_message,
    create_compaction_summary_message,
    create_custom_message,
)
from misaka.agent.harness.types import (
    AgentMessage,
    BranchSummaryError,
    BranchSummaryResult,
    GenerateBranchSummaryOptions,
    SessionError,
    SessionTreeEntry,
    err,
    ok,
)

BRANCH_SUMMARY_PREAMBLE = """The user explored a different conversation branch before returning here.
Summary of that exploration:

"""

BRANCH_SUMMARY_PROMPT = """Create a structured summary of this conversation branch for context when returning later.

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
class CollectEntriesResult:
    entries: list[SessionTreeEntry]
    commonAncestorId: str | None


@dataclass(slots=True)
class BranchSummaryDetails:
    readFiles: list[str]
    modifiedFiles: list[str]


@dataclass(slots=True)
class BranchPreparation:
    messages: list[AgentMessage]
    fileOps: FileOperations
    totalTokens: int


async def collect_entries_for_branch_summary(
    session: Any,
    old_leaf_id: str | None,
    target_id: str,
) -> CollectEntriesResult:
    if not old_leaf_id:
        return CollectEntriesResult(entries=[], commonAncestorId=None)

    old_path = {str(_entry_field(entry, "id")) for entry in await session.getBranch(old_leaf_id)}
    target_path = await session.getBranch(target_id)
    common_ancestor_id: str | None = None
    for entry in reversed(target_path):
        entry_id = _entry_field(entry, "id")
        if entry_id in old_path:
            common_ancestor_id = str(entry_id)
            break

    entries: list[SessionTreeEntry] = []
    current = old_leaf_id
    while current and current != common_ancestor_id:
        entry = await session.getEntry(current)
        if entry is None:
            raise SessionError("invalid_session", f"Entry {current} not found")
        entries.append(entry)
        current = _entry_field(entry, "parentId")
    entries.reverse()
    return CollectEntriesResult(entries=entries, commonAncestorId=common_ancestor_id)


def prepare_branch_entries(entries: list[SessionTreeEntry], token_budget: int = 0) -> BranchPreparation:
    messages: list[AgentMessage] = []
    file_ops = create_file_ops()
    total_tokens = 0

    for entry in entries:
        if _entry_field(entry, "type") != "branch_summary":
            continue
        if _entry_field(entry, "fromHook"):
            continue
        details = _entry_field(entry, "details")
        if isinstance(details, dict):
            read_files = details.get("readFiles")
            modified_files = details.get("modifiedFiles")
        else:
            read_files = getattr(details, "readFiles", None)
            modified_files = getattr(details, "modifiedFiles", None)
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
            entry_type = _entry_field(entry, "type")
            if entry_type in {"compaction", "branch_summary"} and total_tokens < token_budget * 0.9:
                messages.insert(0, message)
                total_tokens += tokens
            break

        messages.insert(0, message)
        total_tokens += tokens

    return BranchPreparation(messages=messages, fileOps=file_ops, totalTokens=total_tokens)


async def generate_branch_summary(entries: list[SessionTreeEntry], options: GenerateBranchSummaryOptions):
    reserve_tokens = options.reserveTokens if options.reserveTokens is not None else 16384
    context_window = options.model.contextWindow or 128000
    token_budget = context_window - reserve_tokens
    preparation = prepare_branch_entries(entries, token_budget)

    if not preparation.messages:
        return ok(BranchSummaryResult(summary="No content to summarize", readFiles=[], modifiedFiles=[]))

    if options.replaceInstructions and options.customInstructions:
        instructions = options.customInstructions
    elif options.customInstructions:
        instructions = f"{BRANCH_SUMMARY_PROMPT}\n\nAdditional focus: {options.customInstructions}"
    else:
        instructions = BRANCH_SUMMARY_PROMPT

    prompt_text = (
        "<conversation>\n"
        f"{serialize_conversation(convert_to_llm(preparation.messages))}\n"
        f"</conversation>\n\n{instructions}"
    )
    response = await complete_simple(
        options.model,
        {
            "systemPrompt": SUMMARIZATION_SYSTEM_PROMPT,
            "messages": [UserMessage(content=[{"type": "text", "text": prompt_text}], timestamp=_timestamp_ms())],
        },
        SimpleStreamOptions(
            apiKey=options.apiKey,
            headers=options.headers,
            signal=options.signal,
            maxTokens=2048,
        ),
    )
    if response.stopReason == "aborted":
        return err(BranchSummaryError("aborted", response.errorMessage or "Branch summary aborted"))
    if response.stopReason == "error":
        return err(
            BranchSummaryError(
                "summarization_failed",
                f"Branch summary failed: {response.errorMessage or 'Unknown error'}",
            )
        )

    summary = BRANCH_SUMMARY_PREAMBLE + _assistant_text(response)
    read_files, modified_files = compute_file_lists(preparation.fileOps)
    summary += format_file_operations(read_files, modified_files)
    return ok(
        BranchSummaryResult(
            summary=summary or "No summary generated",
            readFiles=read_files,
            modifiedFiles=modified_files,
        )
    )


def _get_message_from_entry(entry: Any) -> AgentMessage | None:
    entry_type = _entry_field(entry, "type")
    if entry_type == "message":
        message = _entry_field(entry, "message")
        if _message_field(message, "role") == "toolResult":
            return None
        return message
    if entry_type == "custom_message":
        return create_custom_message(
            str(_entry_field(entry, "customType")),
            _entry_field(entry, "content"),
            bool(_entry_field(entry, "display")),
            _entry_field(entry, "details"),
            _entry_field(entry, "timestamp") or _timestamp_ms(),
        )
    if entry_type == "branch_summary":
        return create_branch_summary_message(
            str(_entry_field(entry, "summary")),
            str(_entry_field(entry, "fromId")),
            _entry_field(entry, "timestamp") or _timestamp_ms(),
        )
    if entry_type == "compaction":
        return create_compaction_summary_message(
            str(_entry_field(entry, "summary")),
            int(_entry_field(entry, "tokensBefore")),
            _entry_field(entry, "timestamp") or _timestamp_ms(),
        )
    return None


def _assistant_text(message: Any) -> str:
    parts: list[str] = []
    for block in _message_field(message, "content") or []:
        if _block_field(block, "type") == "text":
            text = _block_field(block, "text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _entry_field(entry: Any, name: str) -> Any:
    if isinstance(entry, dict):
        return entry.get(name)
    return getattr(entry, name, None)


def _message_field(message: Any, name: str) -> Any:
    if isinstance(message, dict):
        return message.get(name)
    return getattr(message, name, None)


def _block_field(block: Any, name: str) -> Any:
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


def _timestamp_ms() -> int:
    return int(time.time() * 1000)


collectEntriesForBranchSummary = collect_entries_for_branch_summary
generateBranchSummary = generate_branch_summary
prepareBranchEntries = prepare_branch_entries

__all__ = [
    "BRANCH_SUMMARY_PREAMBLE",
    "BRANCH_SUMMARY_PROMPT",
    "BranchPreparation",
    "BranchSummaryDetails",
    "CollectEntriesResult",
    "FileOperations",
    "collectEntriesForBranchSummary",
    "collect_entries_for_branch_summary",
    "generateBranchSummary",
    "generate_branch_summary",
    "prepareBranchEntries",
    "prepare_branch_entries",
]

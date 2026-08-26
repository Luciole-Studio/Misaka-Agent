"""Coding-agent message helpers and LLM conversion adapters."""

from misaka.agent.harness.messages import (
    BRANCH_SUMMARY_PREFIX,
    BRANCH_SUMMARY_SUFFIX,
    COMPACTION_SUMMARY_PREFIX,
    COMPACTION_SUMMARY_SUFFIX,
    BashExecutionMessage,
    BranchSummaryMessage,
    CompactionSummaryMessage,
    CustomMessage,
    bashExecutionToText,
    convertToLlm,
    createBranchSummaryMessage,
    createCompactionSummaryMessage,
    createCustomMessage,
)

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

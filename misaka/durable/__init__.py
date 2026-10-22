"""Durable conversation, task, and document runtime.

Translated from pi's ``packages/durable/src/index.ts`` (v1.3.0). Types are not exported here (the records are plain ``dict``
objects); the submodules hold the storages (``storage.memory``, ``storage.jsonl``, ``storage.sqlite``), the environment
(``env``), the coding tools (``tools``), ``codemode``, ``tool_search``, ``images`` and the conformance suite
(``testing``).
"""

from __future__ import annotations

from .documents import defineDoc, defineDocFamily
from .entries import (
    AssistantEntry,
    CompactionEntry,
    ResetEntry,
    SystemEntry,
    ToolResultEntry,
    UserEntry,
    defineEntry,
)
from .errors import ConversationBusy, ReadAfterWrite, SessionFailed, StorageRequestError
from .harness.agent import (
    DEFAULT_COMPACTION_POLICY,
    DEFAULT_PROGRESS_POLICY,
    DEFAULT_RETRY_POLICY,
    AgentDoc,
    configure,
)
from .harness.compaction import CompactionTask
from .harness.define import (
    defineExtension,
    defineTool,
    hook,
    section,
    wrapSection,
    wrapTool,
)
from .harness.events import watchEvents
from .harness.generation import GenerationTask
from .harness.harness import Harness
from .harness.inbox import InboxDoc
from .harness.live import LiveDoc
from .harness.provider import ProviderDoc
from .harness.registry import createRegistry
from .harness.tool import NestedCallDoc, ToolTask
from .harness.usage import UsageDoc
from .session.session import createSession
from .storage.memory import MemoryStorage
from .tasks import defineTask
from .types import ROOT_CONVERSATION_ID

__all__ = [
    "DEFAULT_COMPACTION_POLICY",
    "DEFAULT_PROGRESS_POLICY",
    "DEFAULT_RETRY_POLICY",
    "ROOT_CONVERSATION_ID",
    "AgentDoc",
    "AssistantEntry",
    "CompactionEntry",
    "CompactionTask",
    "ConversationBusy",
    "GenerationTask",
    "Harness",
    "InboxDoc",
    "LiveDoc",
    "MemoryStorage",
    "NestedCallDoc",
    "ProviderDoc",
    "ReadAfterWrite",
    "ResetEntry",
    "SessionFailed",
    "StorageRequestError",
    "SystemEntry",
    "ToolResultEntry",
    "ToolTask",
    "UsageDoc",
    "UserEntry",
    "configure",
    "createRegistry",
    "createSession",
    "defineDoc",
    "defineDocFamily",
    "defineEntry",
    "defineExtension",
    "defineTask",
    "defineTool",
    "hook",
    "section",
    "watchEvents",
    "wrapSection",
    "wrapTool",
]

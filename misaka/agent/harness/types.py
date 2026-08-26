"""Public harness type surface for prompt, session, and environment orchestration."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict, TypeVar, runtime_checkable

from misaka.ai.types import ImageContent, Model, TextContent, Transport

from misaka.agent.types import AgentEvent, AgentMessage, AgentTool, QueueMode, ThinkingLevel

# The harness ``Session`` class went away with the unused harness runtime; the
# protocols below only ever used it as an opaque annotation.
Session = Any

TValue = TypeVar("TValue")
TError = TypeVar("TError")
TSource = TypeVar("TSource")
TDetails = TypeVar("TDetails")
TMetadata = TypeVar("TMetadata", bound="SessionMetadata")
TCreateOptions = TypeVar("TCreateOptions", bound="SessionCreateOptions")
TListOptions = TypeVar("TListOptions")
TSkill = TypeVar("TSkill", bound="Skill")
TPromptTemplate = TypeVar("TPromptTemplate", bound="PromptTemplate")
TTool = TypeVar("TTool", bound=AgentTool)

type FileKind = Literal["file", "directory", "symlink"]
type FileErrorCode = Literal[
    "aborted",
    "not_found",
    "permission_denied",
    "not_directory",
    "is_directory",
    "invalid",
    "not_supported",
    "unknown",
]
type ExecutionErrorCode = Literal[
    "aborted",
    "timeout",
    "shell_unavailable",
    "spawn_error",
    "callback_error",
    "unknown",
]
type CompactionErrorCode = Literal["aborted", "summarization_failed", "invalid_session", "unknown"]
type BranchSummaryErrorCode = Literal["aborted", "summarization_failed", "invalid_session"]
type SessionErrorCode = Literal[
    "not_found",
    "invalid_session",
    "invalid_entry",
    "invalid_fork_target",
    "storage",
    "unknown",
]
type AgentHarnessErrorCode = Literal[
    "busy",
    "invalid_state",
    "invalid_argument",
    "session",
    "hook",
    "auth",
    "compaction",
    "branch_summary",
    "unknown",
]
type AgentHarnessPhase = Literal["idle", "turn", "compaction", "branch_summary", "retry"]
type Result[TValue, TError] = Ok[TValue] | Err[TError]
type SessionTreeEntry = (
    MessageEntry
    | ThinkingLevelChangeEntry
    | ModelChangeEntry
    | CompactionEntry[Any]
    | BranchSummaryEntry[Any]
    | CustomEntry[Any]
    | CustomMessageEntry[Any]
    | LabelEntry
    | SessionInfoEntry
    | LeafEntry
)
type JsonlSessionRepoApi = SessionRepo[JsonlSessionMetadata, JsonlSessionCreateOptions, JsonlSessionListOptions]
type PendingSessionWrite = dict[str, Any]
type AgentHarnessOwnEvent[TSkill: Skill, TPromptTemplate: PromptTemplate] = (
    QueueUpdateEvent
    | SavePointEvent
    | AbortEvent
    | SettledEvent
    | BeforeAgentStartEvent[TSkill, TPromptTemplate]
    | ContextEvent
    | BeforeProviderRequestEvent
    | BeforeProviderPayloadEvent
    | AfterProviderResponseEvent
    | ToolCallEvent
    | ToolResultEvent
    | SessionBeforeCompactEvent
    | SessionCompactEvent
    | SessionBeforeTreeEvent
    | SessionTreeEvent
    | ModelSelectEvent
    | ThinkingLevelSelectEvent
    | ResourcesUpdateEvent[TSkill, TPromptTemplate]
)
type AgentHarnessEvent[TSkill: Skill, TPromptTemplate: PromptTemplate] = (
    AgentEvent | AgentHarnessOwnEvent[TSkill, TPromptTemplate]
)


@dataclass(slots=True)
class Ok[TValue]:
    value: TValue
    ok: Literal[True] = field(default=True, init=False)


@dataclass(slots=True)
class Err[TError]:
    error: TError
    ok: Literal[False] = field(default=False, init=False)


def ok[TValue](value: TValue) -> Ok[TValue]:
    return Ok(value)


def err[TError](error: TError) -> Err[TError]:
    return Err(error)


def get_or_throw[TValue, TError](result: Result[TValue, TError]) -> TValue:
    if isinstance(result, Err):
        error = result.error
        if isinstance(error, BaseException):
            raise error
        raise Exception(str(error))
    return result.value


def get_or_undefined[TValue, TError](result: Result[TValue, TError]) -> TValue | None:
    if isinstance(result, Err):
        return None
    return result.value


def to_error(error: Any) -> Exception:
    if isinstance(error, Exception):
        return error
    if isinstance(error, str):
        return Exception(error)
    try:
        return Exception(json.dumps(error))
    except Exception:
        return Exception(str(error))


@dataclass(slots=True)
class Skill:
    name: str
    description: str
    content: str
    filePath: str
    disableModelInvocation: bool = False


@dataclass(slots=True)
class PromptTemplate:
    name: str
    description: str | None = None
    content: str = ""


@dataclass(slots=True)
class AgentHarnessResources[TSkill: "Skill", TPromptTemplate: "PromptTemplate"]:
    promptTemplates: list[TPromptTemplate] | None = None
    skills: list[TSkill] | None = None


@dataclass(slots=True)
class AgentHarnessStreamOptions:
    transport: Transport | None = None
    timeoutMs: int | None = None
    maxRetries: int | None = None
    maxRetryDelayMs: int | None = None
    headers: dict[str, str] | None = None
    metadata: dict[str, Any] | None = None
    cacheRetention: Literal["none", "short", "long"] | None = None


@dataclass(slots=True)
class AgentHarnessStreamOptionsPatch:
    transport: Transport | None = None
    timeoutMs: int | None = None
    maxRetries: int | None = None
    maxRetryDelayMs: int | None = None
    headers: dict[str, str | None] | None = None
    metadata: dict[str, Any | None] | None = None
    cacheRetention: Literal["none", "short", "long"] | None = None


class FileError(Exception):
    def __init__(
        self,
        code: FileErrorCode,
        message: str,
        path: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.name = "FileError"
        self.message = message
        self.code = code
        self.path = path
        if cause is not None:
            self.__cause__ = cause


class ExecutionError(Exception):
    def __init__(self, code: ExecutionErrorCode, message: str, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.name = "ExecutionError"
        self.message = message
        self.code = code
        if cause is not None:
            self.__cause__ = cause


class CompactionError(Exception):
    def __init__(self, code: CompactionErrorCode, message: str, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.name = "CompactionError"
        self.message = message
        self.code = code
        if cause is not None:
            self.__cause__ = cause


class BranchSummaryError(Exception):
    def __init__(self, code: BranchSummaryErrorCode, message: str, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.name = "BranchSummaryError"
        self.message = message
        self.code = code
        if cause is not None:
            self.__cause__ = cause


class SessionError(Exception):
    def __init__(self, code: SessionErrorCode, message: str, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.name = "SessionError"
        self.message = message
        self.code = code
        if cause is not None:
            self.__cause__ = cause


class AgentHarnessError(Exception):
    def __init__(self, code: AgentHarnessErrorCode, message: str, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.name = "AgentHarnessError"
        self.message = message
        self.code = code
        if cause is not None:
            self.__cause__ = cause


@dataclass(slots=True)
class FileInfo:
    name: str
    path: str
    kind: FileKind
    size: int
    mtimeMs: float


@dataclass(slots=True)
class ExecutionEnvExecOptions:
    cwd: str | None = None
    env: dict[str, str] | None = None
    timeout: int | float | None = None
    abortSignal: Any | None = None
    onStdout: Callable[[str], None] | None = None
    onStderr: Callable[[str], None] | None = None


@runtime_checkable
class FileSystem(Protocol):
    cwd: str

    async def absolutePath(self, path: str, abortSignal: Any | None = None) -> Result[str, FileError]: ...

    async def joinPath(self, parts: Sequence[str], abortSignal: Any | None = None) -> Result[str, FileError]: ...

    async def readTextFile(self, path: str, abortSignal: Any | None = None) -> Result[str, FileError]: ...

    async def readTextLines(
        self,
        path: str,
        options: dict[str, Any] | None = None,
    ) -> Result[list[str], FileError]: ...

    async def readBinaryFile(self, path: str, abortSignal: Any | None = None) -> Result[bytes, FileError]: ...

    async def writeFile(
        self,
        path: str,
        content: str | bytes,
        abortSignal: Any | None = None,
    ) -> Result[None, FileError]: ...

    async def appendFile(
        self,
        path: str,
        content: str | bytes,
        abortSignal: Any | None = None,
    ) -> Result[None, FileError]: ...

    async def fileInfo(self, path: str, abortSignal: Any | None = None) -> Result[FileInfo, FileError]: ...

    async def listDir(self, path: str, abortSignal: Any | None = None) -> Result[list[FileInfo], FileError]: ...

    async def canonicalPath(self, path: str, abortSignal: Any | None = None) -> Result[str, FileError]: ...

    async def exists(self, path: str, abortSignal: Any | None = None) -> Result[bool, FileError]: ...

    async def createDir(
        self,
        path: str,
        options: dict[str, Any] | None = None,
    ) -> Result[None, FileError]: ...

    async def remove(
        self,
        path: str,
        options: dict[str, Any] | None = None,
    ) -> Result[None, FileError]: ...

    async def createTempDir(
        self,
        prefix: str = "tmp-",
        abortSignal: Any | None = None,
    ) -> Result[str, FileError]: ...

    async def createTempFile(self, options: dict[str, Any] | None = None) -> Result[str, FileError]: ...

    async def cleanup(self) -> None: ...


@runtime_checkable
class Shell(Protocol):
    async def exec(
        self,
        command: str,
        options: ExecutionEnvExecOptions | None = None,
    ) -> Result[dict[str, Any], ExecutionError]: ...

    async def cleanup(self) -> None: ...


@runtime_checkable
class ExecutionEnv(FileSystem, Shell, Protocol):
    pass


@dataclass(slots=True)
class SessionTreeEntryBase:
    type: str
    id: str
    parentId: str | None
    timestamp: str


@dataclass(slots=True)
class MessageEntry(SessionTreeEntryBase):
    message: AgentMessage
    type: Literal["message"] = field(default="message", init=False)


@dataclass(slots=True)
class ThinkingLevelChangeEntry(SessionTreeEntryBase):
    thinkingLevel: str
    type: Literal["thinking_level_change"] = field(default="thinking_level_change", init=False)


@dataclass(slots=True)
class ModelChangeEntry(SessionTreeEntryBase):
    provider: str
    modelId: str
    type: Literal["model_change"] = field(default="model_change", init=False)


@dataclass(slots=True)
class CompactionEntry[TDetails](SessionTreeEntryBase):
    summary: str
    firstKeptEntryId: str
    tokensBefore: int
    details: TDetails | None = None
    fromHook: bool | None = None
    type: Literal["compaction"] = field(default="compaction", init=False)


@dataclass(slots=True)
class BranchSummaryEntry[TDetails](SessionTreeEntryBase):
    fromId: str
    summary: str
    details: TDetails | None = None
    fromHook: bool | None = None
    type: Literal["branch_summary"] = field(default="branch_summary", init=False)


@dataclass(slots=True)
class CustomEntry[TDetails](SessionTreeEntryBase):
    customType: str
    data: TDetails | None = None
    type: Literal["custom"] = field(default="custom", init=False)


@dataclass(slots=True)
class CustomMessageEntry[TDetails](SessionTreeEntryBase):
    customType: str
    content: str | list[TextContent | ImageContent]
    details: TDetails | None = None
    display: bool = True
    type: Literal["custom_message"] = field(default="custom_message", init=False)


@dataclass(slots=True)
class LabelEntry(SessionTreeEntryBase):
    targetId: str
    label: str | None = None
    type: Literal["label"] = field(default="label", init=False)


@dataclass(slots=True)
class SessionInfoEntry(SessionTreeEntryBase):
    name: str | None = None
    type: Literal["session_info"] = field(default="session_info", init=False)


@dataclass(slots=True)
class LeafEntry(SessionTreeEntryBase):
    targetId: str | None
    type: Literal["leaf"] = field(default="leaf", init=False)


class SessionModelInfo(TypedDict):
    provider: str
    modelId: str


@dataclass(slots=True)
class SessionContext:
    messages: list[AgentMessage]
    thinkingLevel: str
    model: SessionModelInfo | None = None


@dataclass(slots=True)
class SessionMetadata:
    id: str
    createdAt: str


@dataclass(slots=True)
class JsonlSessionMetadata(SessionMetadata):
    cwd: str
    path: str
    parentSessionPath: str | None = None


@runtime_checkable
class SessionStorage(Protocol[TMetadata]):
    async def getMetadata(self) -> TMetadata: ...

    async def getLeafId(self) -> str | None: ...

    async def setLeafId(self, leafId: str | None) -> None: ...

    async def createEntryId(self) -> str: ...

    async def appendEntry(self, entry: SessionTreeEntry) -> None: ...

    async def getEntry(self, id: str) -> SessionTreeEntry | None: ...

    async def findEntries(self, type: str) -> list[SessionTreeEntry]: ...

    async def getLabel(self, id: str) -> str | None: ...

    async def getPathToRoot(self, leafId: str | None) -> list[SessionTreeEntry]: ...

    async def getEntries(self) -> list[SessionTreeEntry]: ...


@dataclass(slots=True, kw_only=True)
class SessionCreateOptions:
    id: str | None = None


@dataclass(slots=True, kw_only=True)
class SessionForkOptions:
    entryId: str | None = None
    position: Literal["before", "at"] | None = None
    id: str | None = None


@runtime_checkable
class SessionRepo(Protocol[TMetadata, TCreateOptions, TListOptions]):
    async def create(self, options: TCreateOptions) -> Session: ...

    async def open(self, metadata: TMetadata) -> Session: ...

    async def list(self, options: TListOptions | None = None) -> list[TMetadata]: ...

    async def delete(self, metadata: TMetadata) -> None: ...

    async def fork(self, source: TMetadata, options: SessionForkOptions | TCreateOptions) -> Session: ...


@dataclass(slots=True, kw_only=True)
class JsonlSessionCreateOptions(SessionCreateOptions):
    cwd: str
    parentSessionPath: str | None = None


@dataclass(slots=True, kw_only=True)
class JsonlSessionListOptions:
    cwd: str | None = None


@dataclass(slots=True)
class QueueUpdateEvent:
    steer: list[AgentMessage]
    followUp: list[AgentMessage]
    nextTurn: list[AgentMessage]
    type: Literal["queue_update"] = field(default="queue_update", init=False)


@dataclass(slots=True)
class SavePointEvent:
    hadPendingMutations: bool
    type: Literal["save_point"] = field(default="save_point", init=False)


@dataclass(slots=True)
class AbortEvent:
    clearedSteer: list[AgentMessage]
    clearedFollowUp: list[AgentMessage]
    type: Literal["abort"] = field(default="abort", init=False)


@dataclass(slots=True)
class SettledEvent:
    nextTurnCount: int
    type: Literal["settled"] = field(default="settled", init=False)


@dataclass(slots=True)
class BeforeAgentStartEvent[TSkill: "Skill", TPromptTemplate: "PromptTemplate"]:
    prompt: str
    systemPrompt: str
    resources: AgentHarnessResources[TSkill, TPromptTemplate]
    images: list[ImageContent] | None = None
    type: Literal["before_agent_start"] = field(default="before_agent_start", init=False)


@dataclass(slots=True)
class ContextEvent:
    messages: list[AgentMessage]
    type: Literal["context"] = field(default="context", init=False)


@dataclass(slots=True)
class BeforeProviderRequestEvent:
    model: Model
    sessionId: str
    streamOptions: AgentHarnessStreamOptions
    type: Literal["before_provider_request"] = field(default="before_provider_request", init=False)


@dataclass(slots=True)
class BeforeProviderPayloadEvent:
    model: Model
    payload: Any
    type: Literal["before_provider_payload"] = field(default="before_provider_payload", init=False)


@dataclass(slots=True)
class AfterProviderResponseEvent:
    status: int
    headers: dict[str, str]
    type: Literal["after_provider_response"] = field(default="after_provider_response", init=False)


@dataclass(slots=True)
class ToolCallEvent:
    toolCallId: str
    toolName: str
    input: dict[str, Any]
    type: Literal["tool_call"] = field(default="tool_call", init=False)


@dataclass(slots=True)
class ToolResultEvent:
    toolCallId: str
    toolName: str
    input: dict[str, Any]
    content: list[TextContent | ImageContent]
    details: Any
    isError: bool
    type: Literal["tool_result"] = field(default="tool_result", init=False)


@dataclass(slots=True)
class SessionBeforeCompactEvent:
    preparation: CompactionPreparation
    branchEntries: list[SessionTreeEntry]
    signal: Any
    customInstructions: str | None = None
    type: Literal["session_before_compact"] = field(default="session_before_compact", init=False)


@dataclass(slots=True)
class SessionCompactEvent:
    compactionEntry: CompactionEntry[Any]
    fromHook: bool
    type: Literal["session_compact"] = field(default="session_compact", init=False)


@dataclass(slots=True)
class SessionBeforeTreeEvent:
    preparation: TreePreparation
    signal: Any
    type: Literal["session_before_tree"] = field(default="session_before_tree", init=False)


@dataclass(slots=True)
class SessionTreeEvent:
    newLeafId: str | None
    oldLeafId: str | None
    summaryEntry: BranchSummaryEntry[Any] | None = None
    fromHook: bool | None = None
    type: Literal["session_tree"] = field(default="session_tree", init=False)


@dataclass(slots=True)
class ModelSelectEvent:
    model: Model
    previousModel: Model | None = None
    source: Literal["set", "restore"] = "set"
    type: Literal["model_select"] = field(default="model_select", init=False)


@dataclass(slots=True)
class ThinkingLevelSelectEvent:
    level: ThinkingLevel
    previousLevel: ThinkingLevel
    type: Literal["thinking_level_select"] = field(default="thinking_level_select", init=False)


@dataclass(slots=True)
class ResourcesUpdateEvent[TSkill: "Skill", TPromptTemplate: "PromptTemplate"]:
    resources: AgentHarnessResources[TSkill, TPromptTemplate]
    previousResources: AgentHarnessResources[TSkill, TPromptTemplate]
    type: Literal["resources_update"] = field(default="resources_update", init=False)


@dataclass(slots=True)
class BeforeAgentStartResult:
    messages: list[AgentMessage] | None = None
    systemPrompt: str | None = None


@dataclass(slots=True)
class ContextResult:
    messages: list[AgentMessage]


@dataclass(slots=True)
class BeforeProviderRequestResult:
    streamOptions: AgentHarnessStreamOptionsPatch | None = None


@dataclass(slots=True)
class BeforeProviderPayloadResult:
    payload: Any


@dataclass(slots=True)
class ToolCallResult:
    block: bool | None = None
    reason: str | None = None


@dataclass(slots=True)
class ToolResultPatch:
    content: list[TextContent | ImageContent] | None = None
    details: Any | None = None
    isError: bool | None = None
    terminate: bool | None = None


@dataclass(slots=True)
class SessionBeforeTreeSummary:
    summary: str
    details: Any | None = None


@dataclass(slots=True)
class SessionBeforeCompactResult:
    cancel: bool | None = None
    compaction: CompactResult | None = None


@dataclass(slots=True)
class SessionBeforeTreeResult:
    cancel: bool | None = None
    summary: SessionBeforeTreeSummary | None = None
    customInstructions: str | None = None
    replaceInstructions: bool | None = None
    label: str | None = None


class AgentHarnessEventResultMap(TypedDict, total=False):
    before_agent_start: BeforeAgentStartResult | None
    context: ContextResult | None
    before_provider_request: BeforeProviderRequestResult | None
    before_provider_payload: BeforeProviderPayloadResult | None
    after_provider_response: None
    tool_call: ToolCallResult | None
    tool_result: ToolResultPatch | None
    session_before_compact: SessionBeforeCompactResult | None
    session_compact: None
    session_before_tree: SessionBeforeTreeResult | None
    session_tree: None
    model_select: None
    thinking_level_select: None
    resources_update: None
    queue_update: None
    save_point: None
    abort: None
    settled: None


@dataclass(slots=True)
class AgentHarnessPromptOptions:
    images: list[ImageContent] | None = None


@dataclass(slots=True)
class AbortResult:
    clearedSteer: list[AgentMessage]
    clearedFollowUp: list[AgentMessage]


@dataclass(slots=True)
class CompactResult:
    summary: str
    firstKeptEntryId: str
    tokensBefore: int
    details: Any | None = None


@dataclass(slots=True)
class NavigateTreeResult:
    cancelled: bool
    editorText: str | None = None
    summaryEntry: BranchSummaryEntry[Any] | None = None


@dataclass(slots=True)
class CompactionSettings:
    enabled: bool
    reserveTokens: int
    keepRecentTokens: int


@dataclass(slots=True)
class FileOperations:
    read: set[str] = field(default_factory=set)
    written: set[str] = field(default_factory=set)
    edited: set[str] = field(default_factory=set)


@dataclass(slots=True)
class CompactionPreparation:
    firstKeptEntryId: str
    messagesToSummarize: list[AgentMessage]
    turnPrefixMessages: list[AgentMessage]
    isSplitTurn: bool
    tokensBefore: int
    fileOps: FileOperations
    settings: CompactionSettings
    previousSummary: str | None = None


@dataclass(slots=True)
class TreePreparation:
    targetId: str
    oldLeafId: str | None
    commonAncestorId: str | None
    entriesToSummarize: list[SessionTreeEntry]
    userWantsSummary: bool
    customInstructions: str | None = None
    replaceInstructions: bool | None = None
    label: str | None = None


@dataclass(slots=True)
class GenerateBranchSummaryOptions:
    model: Model
    apiKey: str
    signal: Any
    headers: dict[str, str] | None = None
    customInstructions: str | None = None
    replaceInstructions: bool | None = None
    reserveTokens: int | None = None


@dataclass(slots=True)
class BranchSummaryResult:
    summary: str
    readFiles: list[str]
    modifiedFiles: list[str]


SystemPromptFactory = Callable[[dict[str, Any]], str | Awaitable[str]]
ApiKeyAndHeadersFn = Callable[[Model], Awaitable[dict[str, str] | dict[str, Any] | None]]


@dataclass(slots=True)
class AgentHarnessOptions[TSkill: "Skill", TPromptTemplate: "PromptTemplate", TTool: AgentTool]:
    env: ExecutionEnv
    session: Session
    model: Model
    tools: list[TTool] | None = None
    resources: AgentHarnessResources[TSkill, TPromptTemplate] | None = None
    systemPrompt: str | SystemPromptFactory | None = None
    getApiKeyAndHeaders: ApiKeyAndHeadersFn | None = None
    streamOptions: AgentHarnessStreamOptions | None = None
    thinkingLevel: ThinkingLevel = "off"
    activeToolNames: list[str] | None = None
    steeringMode: QueueMode = "one-at-a-time"
    followUpMode: QueueMode = "one-at-a-time"


getOrThrow = get_or_throw
getOrUndefined = get_or_undefined
toError = to_error



__all__ = [
    "AbortEvent",
    "AbortResult",
    "AfterProviderResponseEvent",
    "AgentHarnessError",
    "AgentHarnessErrorCode",
    "AgentHarnessEvent",
    "AgentHarnessEventResultMap",
    "AgentHarnessOptions",
    "AgentHarnessOwnEvent",
    "AgentHarnessPhase",
    "AgentHarnessPromptOptions",
    "AgentHarnessResources",
    "AgentHarnessStreamOptions",
    "AgentHarnessStreamOptionsPatch",
    "ApiKeyAndHeadersFn",
    "BeforeAgentStartEvent",
    "BeforeAgentStartResult",
    "BeforeProviderPayloadEvent",
    "BeforeProviderPayloadResult",
    "BeforeProviderRequestEvent",
    "BeforeProviderRequestResult",
    "BranchSummaryEntry",
    "BranchSummaryError",
    "BranchSummaryErrorCode",
    "BranchSummaryResult",
    "CompactResult",
    "CompactionEntry",
    "CompactionError",
    "CompactionErrorCode",
    "CompactionPreparation",
    "CompactionSettings",
    "ContextEvent",
    "ContextResult",
    "CustomEntry",
    "CustomMessageEntry",
    "Err",
    "ExecutionEnv",
    "ExecutionEnvExecOptions",
    "ExecutionError",
    "ExecutionErrorCode",
    "FileError",
    "FileErrorCode",
    "FileInfo",
    "FileKind",
    "FileOperations",
    "FileSystem",
    "GenerateBranchSummaryOptions",
    "JsonlSessionCreateOptions",
    "JsonlSessionListOptions",
    "JsonlSessionMetadata",
    "JsonlSessionRepoApi",
    "LabelEntry",
    "LeafEntry",
    "MessageEntry",
    "ModelChangeEntry",
    "ModelSelectEvent",
    "NavigateTreeResult",
    "Ok",
    "PendingSessionWrite",
    "PromptTemplate",
    "QueueUpdateEvent",
    "ResourcesUpdateEvent",
    "Result",
    "SavePointEvent",
    "Session",
    "SessionBeforeCompactEvent",
    "SessionBeforeCompactResult",
    "SessionBeforeTreeEvent",
    "SessionBeforeTreeResult",
    "SessionBeforeTreeSummary",
    "SessionContext",
    "SessionCreateOptions",
    "SessionError",
    "SessionErrorCode",
    "SessionForkOptions",
    "SessionInfoEntry",
    "SessionMetadata",
    "SessionRepo",
    "SessionStorage",
    "SessionTreeEntry",
    "SessionTreeEntryBase",
    "SessionTreeEvent",
    "SettledEvent",
    "Shell",
    "Skill",
    "SystemPromptFactory",
    "ThinkingLevelChangeEntry",
    "ThinkingLevelSelectEvent",
    "ToolCallEvent",
    "ToolCallResult",
    "ToolResultEvent",
    "ToolResultPatch",
    "TreePreparation",
    "err",
    "getOrThrow",
    "getOrUndefined",
    "get_or_throw",
    "get_or_undefined",
    "ok",
    "toError",
    "to_error",
]

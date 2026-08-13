# Ported from: third_party/pi-mono/packages/protocol/src/schemas.ts
# Upstream commit: 686f193e51ccdc56fdf3366ce5d092530c1007ac
# Ported: 2026-08-04 (D16 逐命名对齐; wire JSON keys keep upstream camelCase verbatim)
#
# PORT-NOTE: typebox runtime validators (*Schema consts) are NOT ported — stdlib
# has no JSON-Schema engine. Each XSchema symbol folds into its static type here
# (TypedDict / Literal / union alias); PORTMAP carries one row per upstream symbol.
# Runtime validation, if ever needed, goes to runtime/misaka_ext (optional jsonschema).
# PORT-NOTE: TS anonymous union members must be named in Python. Naming rule:
# PascalCase of the discriminant literal + suffix (Progress/Event/Envelope).
# PORT-NOTE: non-exported TS consts keep their names but stay out of __all__.

from typing import Dict, Final, List, Literal, NotRequired, TypeAlias, TypedDict, Union

PROTOCOL_VERSION: Final = 1

JsonValue: TypeAlias = Union[None, bool, int, float, str, List["JsonValue"], Dict[str, "JsonValue"]]

ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]

# Matches AgentHarnessPhase so adapters do not need a second phase vocabulary.
SessionPhase = Literal["idle", "turn", "compaction", "branch_summary", "retry"]


class ModelRef(TypedDict):
    provider: str
    id: str


class ModelCost(TypedDict):
    # PORT-NOTE: upstream exports ModelCostSchema without a static type; name derived mechanically.
    input: float
    output: float
    cacheRead: float
    cacheWrite: float


class ModelMetadata(TypedDict):
    provider: str
    id: str
    name: str
    api: str
    reasoning: bool
    input: List[Literal["text", "image"]]
    contextWindow: int
    maxTokens: int
    cost: ModelCost
    supportedThinkingLevels: List[ThinkingLevel]
    authenticated: bool


class TextContent(TypedDict):
    type: Literal["text"]
    text: str


class ThinkingContent(TypedDict):
    type: Literal["thinking"]
    thinking: str
    redacted: NotRequired[bool]


class ImageContent(TypedDict):
    type: Literal["image"]
    data: str
    mimeType: str


class ToolCallContent(TypedDict):
    type: Literal["toolCall"]
    toolCallId: str
    toolName: str
    input: JsonValue


UserContent = Union[TextContent, ImageContent]
AssistantContent = Union[TextContent, ThinkingContent, ToolCallContent]
ToolContent = Union[TextContent, ImageContent]


class UsageCost(TypedDict):
    # PORT-NOTE: inline object under UsageSchema.cost upstream; named out of necessity.
    input: float
    output: float
    cacheRead: float
    cacheWrite: float
    total: float


class Usage(TypedDict):
    input: int
    output: int
    cacheRead: int
    cacheWrite: int
    reasoning: NotRequired[int]
    totalTokens: int
    cost: UsageCost


class UserTranscriptItem(TypedDict):
    id: str
    role: Literal["user"]
    content: List[UserContent]
    timestamp: int


class AssistantTranscriptItemProperties(TypedDict):
    # PORT-NOTE: upstream is a non-exported `as const` property bag; base TypedDict here.
    id: str
    role: Literal["assistant"]
    content: List[AssistantContent]
    model: ModelRef
    responseModel: NotRequired[str]
    usage: NotRequired[Usage]
    timestamp: int


class StreamingAssistantTranscriptItem(AssistantTranscriptItemProperties):
    status: Literal["streaming"]


class CompleteAssistantTranscriptItem(AssistantTranscriptItemProperties):
    status: Literal["complete"]
    stopReason: Literal["stop", "length", "toolUse"]


class ErrorAssistantTranscriptItem(AssistantTranscriptItemProperties):
    status: Literal["error"]
    stopReason: Literal["error"]
    errorMessage: NotRequired[str]


class AbortedAssistantTranscriptItem(AssistantTranscriptItemProperties):
    status: Literal["aborted"]
    stopReason: Literal["aborted"]
    errorMessage: NotRequired[str]


AssistantTranscriptItem = Union[
    StreamingAssistantTranscriptItem,
    CompleteAssistantTranscriptItem,
    ErrorAssistantTranscriptItem,
    AbortedAssistantTranscriptItem,
]


class ToolTranscriptItemProperties(TypedDict):
    # PORT-NOTE: non-exported property bag upstream; base TypedDict here.
    id: str
    role: Literal["tool"]
    toolCallId: str
    toolName: str
    input: JsonValue
    content: List[ToolContent]
    details: NotRequired[JsonValue]
    usage: NotRequired[Usage]
    timestamp: int


class RunningToolTranscriptItem(ToolTranscriptItemProperties):
    status: Literal["running"]
    isError: Literal[False]


class CompleteToolTranscriptItem(ToolTranscriptItemProperties):
    status: Literal["complete"]
    isError: Literal[False]


class ErrorToolTranscriptItem(ToolTranscriptItemProperties):
    status: Literal["error"]
    isError: Literal[True]


ToolTranscriptItem = Union[RunningToolTranscriptItem, CompleteToolTranscriptItem, ErrorToolTranscriptItem]

TranscriptItem = Union[UserTranscriptItem, AssistantTranscriptItem, ToolTranscriptItem]


# Normalized incremental activity. Snapshots remain authoritative.
class ItemStartedProgress(TypedDict):
    type: Literal["item_started"]
    item: TranscriptItem


class AssistantDeltaProgress(TypedDict):
    type: Literal["assistant_delta"]
    messageId: str
    contentIndex: int
    kind: Literal["text", "thinking", "toolCall"]
    delta: str


class ItemUpdatedProgress(TypedDict):
    type: Literal["item_updated"]
    item: Union[AssistantTranscriptItem, ToolTranscriptItem]


class ItemFinishedProgress(TypedDict):
    type: Literal["item_finished"]
    item: Union[
        CompleteAssistantTranscriptItem,
        ErrorAssistantTranscriptItem,
        AbortedAssistantTranscriptItem,
        CompleteToolTranscriptItem,
        ErrorToolTranscriptItem,
    ]


TranscriptProgress = Union[ItemStartedProgress, AssistantDeltaProgress, ItemUpdatedProgress, ItemFinishedProgress]


class SessionSummary(TypedDict):
    id: str
    name: NotRequired[str]
    cwd: str
    createdAt: int
    updatedAt: int
    phase: SessionPhase
    model: ModelRef
    thinkingLevel: ThinkingLevel
    attached: bool
    locked: bool


class SessionSnapshot(SessionSummary):
    revision: int
    transcript: List[TranscriptItem]
    queuedSteer: List[UserTranscriptItem]
    queuedSteerCount: int


class ServerSnapshot(TypedDict):
    serverId: str
    protocolVersion: Literal[1]
    revision: int
    sessions: List[SessionSummary]
    models: List[ModelMetadata]


ProtocolErrorCode = Literal["version", "busy", "session_locked", "not_found", "invalid_request"]


class ProtocolError(TypedDict):
    code: ProtocolErrorCode
    message: str
    details: NotRequired[JsonValue]


class PromptPayloadProperties(TypedDict):
    # PORT-NOTE: non-exported property bag upstream; base TypedDict here.
    sessionId: str
    text: str


class ListCommand(TypedDict):
    command: Literal["list"]


class CreateCommand(TypedDict):
    command: Literal["create"]
    cwd: NotRequired[str]
    name: NotRequired[str]
    model: NotRequired[ModelRef]
    thinkingLevel: NotRequired[ThinkingLevel]


class AttachCommand(TypedDict):
    command: Literal["attach"]
    sessionId: str


class DetachCommand(TypedDict):
    command: Literal["detach"]
    sessionId: str


class PromptCommand(PromptPayloadProperties):
    command: Literal["prompt"]


class SteerCommand(PromptPayloadProperties):
    command: Literal["steer"]


class AbortCommand(TypedDict):
    command: Literal["abort"]
    sessionId: str


class SetModelCommand(TypedDict):
    command: Literal["set_model"]
    sessionId: str
    model: ModelRef


class SetThinkingCommand(TypedDict):
    command: Literal["set_thinking"]
    sessionId: str
    thinkingLevel: ThinkingLevel


Command = Union[
    ListCommand,
    CreateCommand,
    AttachCommand,
    DetachCommand,
    PromptCommand,
    SteerCommand,
    AbortCommand,
    SetModelCommand,
    SetThinkingCommand,
]

# PORT-NOTE: upstream derives CommandName as Command["command"]; spelled out here — keep in sync with Command.
CommandName = Literal["list", "create", "attach", "detach", "prompt", "steer", "abort", "set_model", "set_thinking"]


class CreateResult(TypedDict):
    command: Literal["create"]
    session: SessionSnapshot


class AttachResult(TypedDict):
    command: Literal["attach"]
    session: SessionSnapshot


class PromptResult(TypedDict):
    command: Literal["prompt"]
    session: SessionSnapshot


class SteerResult(TypedDict):
    command: Literal["steer"]
    session: SessionSnapshot


class AbortResult(TypedDict):
    command: Literal["abort"]
    session: SessionSnapshot


class SetModelResult(TypedDict):
    command: Literal["set_model"]
    session: SessionSnapshot


class SetThinkingResult(TypedDict):
    command: Literal["set_thinking"]
    session: SessionSnapshot


class ListResult(TypedDict):
    command: Literal["list"]
    sessions: List[SessionSummary]


class DetachResult(TypedDict):
    command: Literal["detach"]
    sessionId: str


CommandResult = Union[
    ListResult,
    CreateResult,
    AttachResult,
    DetachResult,
    PromptResult,
    SteerResult,
    AbortResult,
    SetModelResult,
    SetThinkingResult,
]

# PORT-NOTE: upstream ResultForCommand<TCommand> is a compile-time conditional type —
# inexpressible in Python's type system. Runtime lookup table provided instead.
RESULT_FOR_COMMAND: Final[Dict[str, type]] = {
    "list": ListResult,
    "create": CreateResult,
    "attach": AttachResult,
    "detach": DetachResult,
    "prompt": PromptResult,
    "steer": SteerResult,
    "abort": AbortResult,
    "set_model": SetModelResult,
    "set_thinking": SetThinkingResult,
}


# Must be the first frame sent by a client. Version is intentionally an integer, not a coercible string.
class ClientHello(TypedDict):
    type: Literal["hello"]
    version: int


class RequestEnvelope(TypedDict):
    type: Literal["request"]
    id: str
    request: Command


ClientMessage = Union[ClientHello, RequestEnvelope]


class ServerSnapshotEvent(TypedDict):
    type: Literal["server_snapshot"]
    snapshot: ServerSnapshot


class SessionSnapshotEvent(TypedDict):
    type: Literal["session_snapshot"]
    snapshot: SessionSnapshot


class SessionProgressEvent(TypedDict):
    type: Literal["session_progress"]
    sessionId: str
    progress: TranscriptProgress


class SessionRemovedEvent(TypedDict):
    type: Literal["session_removed"]
    sessionId: str


ServerEvent = Union[ServerSnapshotEvent, SessionSnapshotEvent, SessionProgressEvent, SessionRemovedEvent]


class ServerHello(TypedDict):
    type: Literal["hello"]
    version: Literal[1]
    connectionId: str
    snapshot: ServerSnapshot


class ServerHelloError(TypedDict):
    type: Literal["hello_error"]
    error: ProtocolError


class ResponseOkEnvelope(TypedDict):
    type: Literal["response"]
    id: str
    ok: Literal[True]
    result: CommandResult


class ResponseErrorEnvelope(TypedDict):
    type: Literal["response"]
    id: str
    ok: Literal[False]
    error: ProtocolError


ResponseEnvelope = Union[ResponseOkEnvelope, ResponseErrorEnvelope]


class EventEnvelope(TypedDict):
    type: Literal["event"]
    event: ServerEvent


ServerMessage = Union[ServerHello, ServerHelloError, ResponseEnvelope, EventEnvelope]

# Mirrors the upstream TS export list (Schema consts folded into their static types).
__all__ = [
    "PROTOCOL_VERSION",
    "JsonValue",
    "ThinkingLevel",
    "SessionPhase",
    "ModelRef",
    "ModelCost",
    "ModelMetadata",
    "TextContent",
    "ThinkingContent",
    "ImageContent",
    "ToolCallContent",
    "UserContent",
    "AssistantContent",
    "ToolContent",
    "UsageCost",
    "Usage",
    "UserTranscriptItem",
    "AssistantTranscriptItem",
    "ToolTranscriptItem",
    "TranscriptItem",
    "TranscriptProgress",
    "SessionSummary",
    "SessionSnapshot",
    "ServerSnapshot",
    "ProtocolErrorCode",
    "ProtocolError",
    "ListCommand",
    "CreateCommand",
    "AttachCommand",
    "DetachCommand",
    "PromptCommand",
    "SteerCommand",
    "AbortCommand",
    "SetModelCommand",
    "SetThinkingCommand",
    "Command",
    "CommandName",
    "CreateResult",
    "AttachResult",
    "PromptResult",
    "SteerResult",
    "AbortResult",
    "SetModelResult",
    "SetThinkingResult",
    "ListResult",
    "DetachResult",
    "CommandResult",
    "RESULT_FOR_COMMAND",
    "ClientHello",
    "RequestEnvelope",
    "ClientMessage",
    "ServerEvent",
    "ServerHello",
    "ServerHelloError",
    "ResponseEnvelope",
    "EventEnvelope",
    "ServerMessage",
]


if __name__ == "__main__":
    import json

    hello: ClientHello = {"type": "hello", "version": PROTOCOL_VERSION}
    req: RequestEnvelope = {
        "type": "request",
        "id": "r1",
        "request": {"command": "prompt", "sessionId": "s1", "text": "привет"},
    }
    item: CompleteAssistantTranscriptItem = {
        "id": "a1",
        "role": "assistant",
        "content": [{"type": "text", "text": "done"}],
        "model": {"provider": "anthropic", "id": "claude-fable-5"},
        "timestamp": 0,
        "status": "complete",
        "stopReason": "toolUse",
    }
    for obj in (hello, req, item):
        assert json.loads(json.dumps(obj, ensure_ascii=False)) == obj
    assert req["request"]["command"] in RESULT_FOR_COMMAND
    assert item["status"] == "complete" and item["stopReason"] == "toolUse"
    g = globals()
    missing = [n for n in __all__ if n not in g]
    assert not missing, f"__all__ 悬空: {missing}"
    print(f"selfcheck ok — {len(__all__)} exported symbols, wire keys camelCase preserved")

"""Typed RPC protocol structures for headless coding-agent operation."""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

from misaka.agent.types import AgentMessage, ThinkingLevel
from misaka.ai.types import ImageContent, Model

from misaka.core.agent_session import SessionStats
from misaka.core.bash_executor import BashResult
from misaka.core.compaction import CompactionResult
from misaka.core.source_info import SourceInfo


class RpcSlashCommand(TypedDict):
    name: str
    description: NotRequired[str]
    source: Literal["extension", "prompt", "skill"]
    sourceInfo: SourceInfo


class RpcSessionState(TypedDict):
    model: NotRequired[Model[Any]]
    thinkingLevel: ThinkingLevel
    isStreaming: bool
    isCompacting: bool
    steeringMode: Literal["all", "one-at-a-time"]
    followUpMode: Literal["all", "one-at-a-time"]
    sessionFile: NotRequired[str]
    sessionId: str
    sessionName: NotRequired[str]
    autoCompactionEnabled: bool
    messageCount: int
    pendingMessageCount: int


class _RpcCommandBase(TypedDict, total=False):
    id: str


class RpcPromptCommand(_RpcCommandBase):
    type: Literal["prompt"]
    message: str
    images: NotRequired[list[ImageContent]]
    streamingBehavior: NotRequired[Literal["steer", "followUp"]]


class RpcSteerCommand(_RpcCommandBase):
    type: Literal["steer"]
    message: str
    images: NotRequired[list[ImageContent]]


class RpcFollowUpCommand(_RpcCommandBase):
    type: Literal["follow_up"]
    message: str
    images: NotRequired[list[ImageContent]]


class RpcAbortCommand(_RpcCommandBase):
    type: Literal["abort"]


class RpcNewSessionCommand(_RpcCommandBase):
    type: Literal["new_session"]
    parentSession: NotRequired[str]


class RpcGetStateCommand(_RpcCommandBase):
    type: Literal["get_state"]


class RpcSetModelCommand(_RpcCommandBase):
    type: Literal["set_model"]
    provider: str
    modelId: str


class RpcCycleModelCommand(_RpcCommandBase):
    type: Literal["cycle_model"]


class RpcGetAvailableModelsCommand(_RpcCommandBase):
    type: Literal["get_available_models"]


class RpcSetThinkingLevelCommand(_RpcCommandBase):
    type: Literal["set_thinking_level"]
    level: ThinkingLevel


class RpcCycleThinkingLevelCommand(_RpcCommandBase):
    type: Literal["cycle_thinking_level"]


class RpcSetSteeringModeCommand(_RpcCommandBase):
    type: Literal["set_steering_mode"]
    mode: Literal["all", "one-at-a-time"]


class RpcSetFollowUpModeCommand(_RpcCommandBase):
    type: Literal["set_follow_up_mode"]
    mode: Literal["all", "one-at-a-time"]


class RpcCompactCommand(_RpcCommandBase):
    type: Literal["compact"]
    customInstructions: NotRequired[str]


class RpcSetAutoCompactionCommand(_RpcCommandBase):
    type: Literal["set_auto_compaction"]
    enabled: bool


class RpcSetAutoRetryCommand(_RpcCommandBase):
    type: Literal["set_auto_retry"]
    enabled: bool


class RpcAbortRetryCommand(_RpcCommandBase):
    type: Literal["abort_retry"]


class RpcBashCommand(_RpcCommandBase):
    type: Literal["bash"]
    command: str


class RpcAbortBashCommand(_RpcCommandBase):
    type: Literal["abort_bash"]


class RpcGetSessionStatsCommand(_RpcCommandBase):
    type: Literal["get_session_stats"]


class RpcExportHtmlCommand(_RpcCommandBase):
    type: Literal["export_html"]
    outputPath: NotRequired[str]


class RpcSwitchSessionCommand(_RpcCommandBase):
    type: Literal["switch_session"]
    sessionPath: str


class RpcForkCommand(_RpcCommandBase):
    type: Literal["fork"]
    entryId: str


class RpcCloneCommand(_RpcCommandBase):
    type: Literal["clone"]


class RpcGetForkMessagesCommand(_RpcCommandBase):
    type: Literal["get_fork_messages"]


class RpcGetLastAssistantTextCommand(_RpcCommandBase):
    type: Literal["get_last_assistant_text"]


class RpcSetSessionNameCommand(_RpcCommandBase):
    type: Literal["set_session_name"]
    name: str


class RpcGetMessagesCommand(_RpcCommandBase):
    type: Literal["get_messages"]


class RpcGetCommandsCommand(_RpcCommandBase):
    type: Literal["get_commands"]


type RpcCommand = (
    RpcPromptCommand
    | RpcSteerCommand
    | RpcFollowUpCommand
    | RpcAbortCommand
    | RpcNewSessionCommand
    | RpcGetStateCommand
    | RpcSetModelCommand
    | RpcCycleModelCommand
    | RpcGetAvailableModelsCommand
    | RpcSetThinkingLevelCommand
    | RpcCycleThinkingLevelCommand
    | RpcSetSteeringModeCommand
    | RpcSetFollowUpModeCommand
    | RpcCompactCommand
    | RpcSetAutoCompactionCommand
    | RpcSetAutoRetryCommand
    | RpcAbortRetryCommand
    | RpcBashCommand
    | RpcAbortBashCommand
    | RpcGetSessionStatsCommand
    | RpcExportHtmlCommand
    | RpcSwitchSessionCommand
    | RpcForkCommand
    | RpcCloneCommand
    | RpcGetForkMessagesCommand
    | RpcGetLastAssistantTextCommand
    | RpcSetSessionNameCommand
    | RpcGetMessagesCommand
    | RpcGetCommandsCommand
)

type RpcCommandType = Literal[
    "prompt",
    "steer",
    "follow_up",
    "abort",
    "new_session",
    "get_state",
    "set_model",
    "cycle_model",
    "get_available_models",
    "set_thinking_level",
    "cycle_thinking_level",
    "set_steering_mode",
    "set_follow_up_mode",
    "compact",
    "set_auto_compaction",
    "set_auto_retry",
    "abort_retry",
    "bash",
    "abort_bash",
    "get_session_stats",
    "export_html",
    "switch_session",
    "fork",
    "clone",
    "get_fork_messages",
    "get_last_assistant_text",
    "set_session_name",
    "get_messages",
    "get_commands",
]


class RpcCancelledData(TypedDict):
    cancelled: bool


class RpcCycleModelData(TypedDict):
    model: Model[Any]
    thinkingLevel: ThinkingLevel
    isScoped: bool


class RpcAvailableModelsData(TypedDict):
    models: list[Model[Any]]


class RpcCycleThinkingLevelData(TypedDict):
    level: ThinkingLevel


class RpcExportHtmlData(TypedDict):
    path: str


class RpcForkData(TypedDict):
    text: str
    cancelled: bool


class RpcForkMessage(TypedDict):
    entryId: str
    text: str


class RpcForkMessagesData(TypedDict):
    messages: list[RpcForkMessage]


class RpcLastAssistantTextData(TypedDict):
    text: str | None


class RpcMessagesData(TypedDict):
    messages: list[AgentMessage]


class RpcCommandsData(TypedDict):
    commands: list[RpcSlashCommand]


class _RpcResponseBase(TypedDict, total=False):
    id: str


class RpcPromptResponse(_RpcResponseBase):
    type: Literal["response"]
    command: Literal["prompt"]
    success: Literal[True]


class RpcSteerResponse(_RpcResponseBase):
    type: Literal["response"]
    command: Literal["steer"]
    success: Literal[True]


class RpcFollowUpResponse(_RpcResponseBase):
    type: Literal["response"]
    command: Literal["follow_up"]
    success: Literal[True]


class RpcAbortResponse(_RpcResponseBase):
    type: Literal["response"]
    command: Literal["abort"]
    success: Literal[True]


class RpcNewSessionResponse(_RpcResponseBase):
    type: Literal["response"]
    command: Literal["new_session"]
    success: Literal[True]
    data: RpcCancelledData


class RpcGetStateResponse(_RpcResponseBase):
    type: Literal["response"]
    command: Literal["get_state"]
    success: Literal[True]
    data: RpcSessionState


class RpcSetModelResponse(_RpcResponseBase):
    type: Literal["response"]
    command: Literal["set_model"]
    success: Literal[True]
    data: Model[Any]


class RpcCycleModelResponse(_RpcResponseBase):
    type: Literal["response"]
    command: Literal["cycle_model"]
    success: Literal[True]
    data: RpcCycleModelData | None


class RpcGetAvailableModelsResponse(_RpcResponseBase):
    type: Literal["response"]
    command: Literal["get_available_models"]
    success: Literal[True]
    data: RpcAvailableModelsData


class RpcSetThinkingLevelResponse(_RpcResponseBase):
    type: Literal["response"]
    command: Literal["set_thinking_level"]
    success: Literal[True]


class RpcCycleThinkingLevelResponse(_RpcResponseBase):
    type: Literal["response"]
    command: Literal["cycle_thinking_level"]
    success: Literal[True]
    data: RpcCycleThinkingLevelData | None


class RpcSetSteeringModeResponse(_RpcResponseBase):
    type: Literal["response"]
    command: Literal["set_steering_mode"]
    success: Literal[True]


class RpcSetFollowUpModeResponse(_RpcResponseBase):
    type: Literal["response"]
    command: Literal["set_follow_up_mode"]
    success: Literal[True]


class RpcCompactResponse(_RpcResponseBase):
    type: Literal["response"]
    command: Literal["compact"]
    success: Literal[True]
    data: CompactionResult


class RpcSetAutoCompactionResponse(_RpcResponseBase):
    type: Literal["response"]
    command: Literal["set_auto_compaction"]
    success: Literal[True]


class RpcSetAutoRetryResponse(_RpcResponseBase):
    type: Literal["response"]
    command: Literal["set_auto_retry"]
    success: Literal[True]


class RpcAbortRetryResponse(_RpcResponseBase):
    type: Literal["response"]
    command: Literal["abort_retry"]
    success: Literal[True]


class RpcBashResponse(_RpcResponseBase):
    type: Literal["response"]
    command: Literal["bash"]
    success: Literal[True]
    data: BashResult


class RpcAbortBashResponse(_RpcResponseBase):
    type: Literal["response"]
    command: Literal["abort_bash"]
    success: Literal[True]


class RpcGetSessionStatsResponse(_RpcResponseBase):
    type: Literal["response"]
    command: Literal["get_session_stats"]
    success: Literal[True]
    data: SessionStats


class RpcExportHtmlResponse(_RpcResponseBase):
    type: Literal["response"]
    command: Literal["export_html"]
    success: Literal[True]
    data: RpcExportHtmlData


class RpcSwitchSessionResponse(_RpcResponseBase):
    type: Literal["response"]
    command: Literal["switch_session"]
    success: Literal[True]
    data: RpcCancelledData


class RpcForkResponse(_RpcResponseBase):
    type: Literal["response"]
    command: Literal["fork"]
    success: Literal[True]
    data: RpcForkData


class RpcCloneResponse(_RpcResponseBase):
    type: Literal["response"]
    command: Literal["clone"]
    success: Literal[True]
    data: RpcCancelledData


class RpcGetForkMessagesResponse(_RpcResponseBase):
    type: Literal["response"]
    command: Literal["get_fork_messages"]
    success: Literal[True]
    data: RpcForkMessagesData


class RpcGetLastAssistantTextResponse(_RpcResponseBase):
    type: Literal["response"]
    command: Literal["get_last_assistant_text"]
    success: Literal[True]
    data: RpcLastAssistantTextData


class RpcSetSessionNameResponse(_RpcResponseBase):
    type: Literal["response"]
    command: Literal["set_session_name"]
    success: Literal[True]


class RpcGetMessagesResponse(_RpcResponseBase):
    type: Literal["response"]
    command: Literal["get_messages"]
    success: Literal[True]
    data: RpcMessagesData


class RpcGetCommandsResponse(_RpcResponseBase):
    type: Literal["response"]
    command: Literal["get_commands"]
    success: Literal[True]
    data: RpcCommandsData


class RpcErrorResponse(_RpcResponseBase):
    type: Literal["response"]
    command: str
    success: Literal[False]
    error: str


type RpcResponse = (
    RpcPromptResponse
    | RpcSteerResponse
    | RpcFollowUpResponse
    | RpcAbortResponse
    | RpcNewSessionResponse
    | RpcGetStateResponse
    | RpcSetModelResponse
    | RpcCycleModelResponse
    | RpcGetAvailableModelsResponse
    | RpcSetThinkingLevelResponse
    | RpcCycleThinkingLevelResponse
    | RpcSetSteeringModeResponse
    | RpcSetFollowUpModeResponse
    | RpcCompactResponse
    | RpcSetAutoCompactionResponse
    | RpcSetAutoRetryResponse
    | RpcAbortRetryResponse
    | RpcBashResponse
    | RpcAbortBashResponse
    | RpcGetSessionStatsResponse
    | RpcExportHtmlResponse
    | RpcSwitchSessionResponse
    | RpcForkResponse
    | RpcCloneResponse
    | RpcGetForkMessagesResponse
    | RpcGetLastAssistantTextResponse
    | RpcSetSessionNameResponse
    | RpcGetMessagesResponse
    | RpcGetCommandsResponse
    | RpcErrorResponse
)


class RpcExtensionUISelectRequest(TypedDict):
    type: Literal["extension_ui_request"]
    id: str
    method: Literal["select"]
    title: str
    options: list[str]
    timeout: NotRequired[int]


class RpcExtensionUIConfirmRequest(TypedDict):
    type: Literal["extension_ui_request"]
    id: str
    method: Literal["confirm"]
    title: str
    message: str
    timeout: NotRequired[int]


class RpcExtensionUIInputRequest(TypedDict):
    type: Literal["extension_ui_request"]
    id: str
    method: Literal["input"]
    title: str
    placeholder: NotRequired[str]
    timeout: NotRequired[int]


class RpcExtensionUIEditorRequest(TypedDict):
    type: Literal["extension_ui_request"]
    id: str
    method: Literal["editor"]
    title: str
    prefill: NotRequired[str]


class RpcExtensionUINotifyRequest(TypedDict):
    type: Literal["extension_ui_request"]
    id: str
    method: Literal["notify"]
    message: str
    notifyType: NotRequired[Literal["info", "warning", "error"]]


class RpcExtensionUISetStatusRequest(TypedDict):
    type: Literal["extension_ui_request"]
    id: str
    method: Literal["setStatus"]
    statusKey: str
    statusText: str | None


class RpcExtensionUISetWidgetRequest(TypedDict):
    type: Literal["extension_ui_request"]
    id: str
    method: Literal["setWidget"]
    widgetKey: str
    widgetLines: list[str] | None
    widgetPlacement: NotRequired[Literal["aboveEditor", "belowEditor"]]


class RpcExtensionUISetTitleRequest(TypedDict):
    type: Literal["extension_ui_request"]
    id: str
    method: Literal["setTitle"]
    title: str


class RpcExtensionUISetEditorTextRequest(TypedDict):
    type: Literal["extension_ui_request"]
    id: str
    method: Literal["set_editor_text"]
    text: str


type RpcExtensionUIRequest = (
    RpcExtensionUISelectRequest
    | RpcExtensionUIConfirmRequest
    | RpcExtensionUIInputRequest
    | RpcExtensionUIEditorRequest
    | RpcExtensionUINotifyRequest
    | RpcExtensionUISetStatusRequest
    | RpcExtensionUISetWidgetRequest
    | RpcExtensionUISetTitleRequest
    | RpcExtensionUISetEditorTextRequest
)


class RpcExtensionUIValueResponse(TypedDict):
    type: Literal["extension_ui_response"]
    id: str
    value: str


class RpcExtensionUIConfirmedResponse(TypedDict):
    type: Literal["extension_ui_response"]
    id: str
    confirmed: bool


class RpcExtensionUICancelledResponse(TypedDict):
    type: Literal["extension_ui_response"]
    id: str
    cancelled: Literal[True]


type RpcExtensionUIResponse = (
    RpcExtensionUIValueResponse
    | RpcExtensionUIConfirmedResponse
    | RpcExtensionUICancelledResponse
)


__all__ = [
    "RpcCommand",
    "RpcCommandType",
    "RpcExtensionUIRequest",
    "RpcExtensionUIResponse",
    "RpcResponse",
    "RpcSessionState",
    "RpcSlashCommand",
]

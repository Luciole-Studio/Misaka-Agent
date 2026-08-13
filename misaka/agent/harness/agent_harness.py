"""High-level session harness that composes the agent loop with persistence and hooks."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields
from typing import Any

from misaka.ai.stream import stream_simple
from misaka.ai.types import (
    AssistantMessage,
    ImageContent,
    Model,
    ProviderResponse,
    SimpleStreamOptions,
    TextContent,
    Usage,
    UserMessage,
    validate_message,
    validate_user_content,
)

from misaka.agent.agent import AbortController, AbortSignal
from misaka.agent.agent_loop import run_agent_loop
from misaka.agent.harness.compaction.branch_summarization import (
    collect_entries_for_branch_summary,
    generate_branch_summary,
)
from misaka.agent.harness.compaction.compaction import (
    DEFAULT_COMPACTION_SETTINGS,
    prepare_compaction,
)
from misaka.agent.harness.compaction.compaction import (
    compact as run_compaction,
)
from misaka.agent.harness.messages import (
    BashExecutionMessage,
    BranchSummaryMessage,
    CompactionSummaryMessage,
    CustomMessage,
    convert_to_llm,
)
from misaka.agent.harness.prompt_templates import format_prompt_template_invocation
from misaka.agent.harness.skills import format_skill_invocation
from misaka.agent.harness.types import (
    AbortEvent,
    AbortResult,
    AfterProviderResponseEvent,
    AgentHarnessError,
    AgentHarnessEvent,
    AgentHarnessOptions,
    AgentHarnessOwnEvent,
    AgentHarnessResources,
    AgentHarnessStreamOptions,
    AgentHarnessStreamOptionsPatch,
    BeforeAgentStartEvent,
    BeforeProviderPayloadEvent,
    BeforeProviderRequestEvent,
    BranchSummaryError,
    CompactionError,
    CompactResult,
    ContextEvent,
    GenerateBranchSummaryOptions,
    ModelSelectEvent,
    NavigateTreeResult,
    QueueUpdateEvent,
    ResourcesUpdateEvent,
    SavePointEvent,
    SessionBeforeCompactEvent,
    SessionBeforeTreeEvent,
    SessionCompactEvent,
    SessionError,
    SessionTreeEvent,
    SettledEvent,
    ThinkingLevelSelectEvent,
    ToolCallEvent,
    ToolResultEvent,
    TreePreparation,
    ok,
    to_error,
)
from misaka.agent.types import (
    AgentContext,
    AgentEndEvent,
    AgentEvent,
    AgentLoopConfig,
    AgentLoopTurnUpdate,
    AgentMessage,
    AgentTool,
    MessageEndEvent,
    MessageStartEvent,
    ThinkingLevel,
    TurnEndEvent,
)

SUBSCRIBER_EVENT_TYPE = "*"


@dataclass(slots=True)
class AgentHarnessTurnState:
    messages: list[AgentMessage]
    resources: AgentHarnessResources[Any, Any]
    streamOptions: AgentHarnessStreamOptions
    sessionId: str
    systemPrompt: str
    model: Model
    thinkingLevel: ThinkingLevel
    tools: list[AgentTool]
    activeTools: list[AgentTool]


def create_user_message(text: str, images: list[ImageContent] | None = None) -> UserMessage:
    content: list[Any] = [TextContent(text=text)]
    if images:
        content.extend(images)
    return UserMessage(content=content, timestamp=int(time.time() * 1000))


def create_failure_message(model: Model, error: Any, aborted: bool) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text="")],
        api=model.api,
        provider=model.provider,
        model=model.id,
        stopReason="aborted" if aborted else "error",
        errorMessage=str(error.message if isinstance(error, Exception) and hasattr(error, "message") else error),
        timestamp=int(time.time() * 1000),
        usage=Usage(
            input=0,
            output=0,
            cacheRead=0,
            cacheWrite=0,
            totalTokens=0,
            cost={"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
        ),
    )


def clone_stream_options(
    stream_options: AgentHarnessStreamOptions | dict[str, Any] | None = None,
) -> AgentHarnessStreamOptions:
    if stream_options is None:
        return AgentHarnessStreamOptions()
    options_map = _to_mapping(stream_options)
    headers = options_map.get("headers")
    metadata = options_map.get("metadata")
    if headers is not None:
        options_map["headers"] = dict(headers)
    if metadata is not None:
        options_map["metadata"] = dict(metadata)
    return AgentHarnessStreamOptions(**options_map)


def merge_headers(*entries: dict[str, str] | None) -> dict[str, str] | None:
    merged: dict[str, str] = {}
    has_headers = False
    for entry in entries:
        if not entry:
            continue
        merged.update(entry)
        has_headers = True
    return merged if has_headers else None


def apply_stream_options_patch(
    base: AgentHarnessStreamOptions,
    patch: AgentHarnessStreamOptionsPatch | dict[str, Any] | None = None,
) -> AgentHarnessStreamOptions:
    result = clone_stream_options(base)
    if patch is None:
        return result

    if isinstance(patch, AgentHarnessStreamOptionsPatch):
        patch_map = {
            key: value
            for key, value in {
                "transport": patch.transport,
                "timeoutMs": patch.timeoutMs,
                "maxRetries": patch.maxRetries,
                "maxRetryDelayMs": patch.maxRetryDelayMs,
                "cacheRetention": patch.cacheRetention,
                "headers": patch.headers,
                "metadata": patch.metadata,
            }.items()
            if value is not None
        }
    else:
        patch_map = dict(patch)

    for key in ("transport", "timeoutMs", "maxRetries", "maxRetryDelayMs", "cacheRetention"):
        if key in patch_map:
            setattr(result, key, patch_map[key])

    if "headers" in patch_map:
        headers_patch = patch_map["headers"]
        if headers_patch is None:
            result.headers = None
        else:
            headers = dict(result.headers or {})
            for name, value in dict(headers_patch).items():
                if value is None:
                    headers.pop(name, None)
                else:
                    headers[name] = value
            result.headers = headers or None

    if "metadata" in patch_map:
        metadata_patch = patch_map["metadata"]
        if metadata_patch is None:
            result.metadata = None
        else:
            metadata = dict(result.metadata or {})
            for name, value in dict(metadata_patch).items():
                if value is None:
                    metadata.pop(name, None)
                else:
                    metadata[name] = value
            result.metadata = metadata or None

    return result


def normalize_harness_error(error: Any, fallback_code: str) -> AgentHarnessError:
    if isinstance(error, AgentHarnessError):
        return error
    cause = to_error(error)
    if isinstance(cause, SessionError):
        return AgentHarnessError("session", str(cause), cause)
    if isinstance(cause, CompactionError):
        return AgentHarnessError("compaction", str(cause), cause)
    if isinstance(cause, BranchSummaryError):
        return AgentHarnessError("branch_summary", str(cause), cause)
    return AgentHarnessError(fallback_code, str(cause), cause)


def normalize_hook_error(error: Any) -> AgentHarnessError:
    return normalize_harness_error(error, "hook")


class AgentHarness:
    def __init__(self, options: AgentHarnessOptions[Any, Any, Any] | dict[str, Any]) -> None:
        opts = (
            options
            if isinstance(options, dict)
            else {field.name: getattr(options, field.name) for field in fields(options)}
        )
        self.env = opts["env"]
        self.session = opts["session"]
        self.phase = "idle"
        self.runAbortController: AbortController | None = None
        self.runPromise: asyncio.Future[None] | None = None
        self.pendingSessionWrites: list[dict[str, Any]] = []
        self.model = _coerce_model(opts["model"])
        self.thinkingLevel = opts.get("thinkingLevel", "off")
        self.systemPrompt = opts.get("systemPrompt")
        self.streamOptions = clone_stream_options(opts.get("streamOptions"))
        self.getApiKeyAndHeaders = opts.get("getApiKeyAndHeaders")
        self.resources = copy_resources(opts.get("resources"))
        self.tools: dict[str, AgentTool] = {}
        for tool in opts.get("tools") or []:
            self.tools[tool.name] = tool
        self.activeToolNames = list(
            opts.get("activeToolNames")
            or [tool.name for tool in opts.get("tools") or []]
        )
        self.steerQueue: list[AgentMessage] = []
        self.steeringQueueMode = opts.get("steeringMode", "one-at-a-time")
        self.followUpQueue: list[AgentMessage] = []
        self.followUpQueueMode = opts.get("followUpMode", "one-at-a-time")
        self.nextTurnQueue: list[AgentMessage] = []
        self.handlers: dict[str, list[Callable[..., Any]]] = {}

    def _get_handlers(self, event_type: str) -> list[Callable[..., Any]] | None:
        return self.handlers.get(event_type)

    async def _emit_own(self, event: AgentHarnessOwnEvent, signal: AbortSignal | None = None) -> None:
        for listener in list(self._get_handlers(SUBSCRIBER_EVENT_TYPE) or []):
            try:
                await maybe_await(listener(event, signal))
            except Exception as error:
                raise normalize_hook_error(error) from error

    async def _emit_any(self, event: AgentHarnessEvent, signal: AbortSignal | None = None) -> None:
        for listener in list(self._get_handlers(SUBSCRIBER_EVENT_TYPE) or []):
            try:
                await maybe_await(listener(event, signal))
            except Exception as error:
                raise normalize_hook_error(error) from error

    async def _emit_hook(self, event: Any) -> Any:
        handlers = self._get_handlers(event.type)
        if not handlers:
            return None
        last_result = None
        for handler in list(handlers):
            try:
                result = await maybe_await(handler(event))
            except Exception as error:
                raise normalize_hook_error(error) from error
            if result is not None:
                last_result = result
        return last_result

    async def _emit_before_provider_request(
        self,
        model: Model,
        session_id: str,
        stream_options: AgentHarnessStreamOptions,
    ) -> AgentHarnessStreamOptions:
        handlers = self._get_handlers("before_provider_request")
        current = clone_stream_options(stream_options)
        if not handlers:
            return current
        for handler in list(handlers):
            try:
                result = await maybe_await(
                    handler(
                        BeforeProviderRequestEvent(
                            model=model,
                            sessionId=session_id,
                            streamOptions=clone_stream_options(current),
                        )
                    )
                )
            except Exception as error:
                raise normalize_hook_error(error) from error
            patch = get_value(result, "streamOptions")
            if patch is not None:
                current = apply_stream_options_patch(current, patch)
        return current

    async def _emit_before_provider_payload(self, model: Model, payload: Any) -> Any:
        handlers = self._get_handlers("before_provider_payload")
        current = payload
        if not handlers:
            return current
        for handler in list(handlers):
            try:
                result = await maybe_await(
                    handler(BeforeProviderPayloadEvent(model=model, payload=current))
                )
            except Exception as error:
                raise normalize_hook_error(error) from error
            if result is not None:
                current = get_value(result, "payload")
        return current

    async def _emit_queue_update(self) -> None:
        await self._emit_own(
            QueueUpdateEvent(
                steer=list(self.steerQueue),
                followUp=list(self.followUpQueue),
                nextTurn=list(self.nextTurnQueue),
            )
        )

    def _start_run_promise(self) -> Callable[[], None]:
        loop = asyncio.get_running_loop()
        self.runPromise = loop.create_future()

        def finish() -> None:
            run_promise = self.runPromise
            self.runPromise = None
            if run_promise is not None and not run_promise.done():
                run_promise.set_result(None)

        return finish

    async def _create_turn_state(self) -> AgentHarnessTurnState:
        context = await self.session.buildContext()
        resources = self.getResources()
        session_metadata = await self.session.getMetadata()
        tools = list(self.tools.values())
        active_tools = [self.tools[name] for name in self.activeToolNames if name in self.tools]
        system_prompt = "You are a helpful assistant."
        if isinstance(self.systemPrompt, str):
            system_prompt = self.systemPrompt
        elif self.systemPrompt is not None:
            system_prompt = await maybe_await(
                self.systemPrompt(
                    {
                        "env": self.env,
                        "session": self.session,
                        "model": self.model,
                        "thinkingLevel": self.thinkingLevel,
                        "activeTools": active_tools,
                        "resources": resources,
                    }
                )
            )
        return AgentHarnessTurnState(
            messages=list(context.messages),
            resources=resources,
            streamOptions=clone_stream_options(self.streamOptions),
            sessionId=str(get_value(session_metadata, "id")),
            systemPrompt=system_prompt,
            model=self.model,
            thinkingLevel=self.thinkingLevel,
            tools=tools,
            activeTools=active_tools,
        )

    def _create_context(self, turn_state: AgentHarnessTurnState, system_prompt: str | None = None) -> AgentContext:
        return AgentContext(
            systemPrompt=system_prompt or turn_state.systemPrompt,
            messages=list(turn_state.messages),
            tools=list(turn_state.activeTools),
        )

    def _create_stream_fn(
        self,
        get_turn_state: Callable[[], AgentHarnessTurnState],
    ):
        async def stream_fn(model: Model, context: Any, stream_options: Any = None):
            turn_state = get_turn_state()
            auth = await maybe_await(self.getApiKeyAndHeaders(model)) if self.getApiKeyAndHeaders else None
            auth_headers = get_value(auth, "headers")
            auth_api_key = get_value(auth, "apiKey")
            snapshot_options = clone_stream_options(turn_state.streamOptions)
            snapshot_options.headers = merge_headers(snapshot_options.headers, auth_headers)
            request_options = await self._emit_before_provider_request(model, turn_state.sessionId, snapshot_options)

            async def on_payload(payload: dict[str, Any], _model: Model = model):
                return await self._emit_before_provider_payload(_model, payload)

            async def on_response(response: ProviderResponse | dict[str, Any], _model: Model = model):
                headers = dict(get_value(response, "headers") or {})
                await self._emit_own(
                    AfterProviderResponseEvent(
                        status=int(get_value(response, "status") or 0),
                        headers=headers,
                    ),
                    get_value(stream_options, "signal"),
                )

            return stream_simple(
                model,
                context,
                SimpleStreamOptions(
                    cacheRetention=request_options.cacheRetention,
                    headers=dict(request_options.headers) if request_options.headers is not None else None,
                    maxTokens=get_value(stream_options, "maxTokens"),
                    # Per-call wrappers (notably the shared hard token slice)
                    # must be able to force zero transparent retries.  Using
                    # ``or`` here would also discard the meaningful value 0.
                    maxRetries=(
                        get_value(stream_options, "maxRetries")
                        if get_value(stream_options, "maxRetries") is not None
                        else request_options.maxRetries
                    ),
                    maxRetryDelayMs=request_options.maxRetryDelayMs,
                    metadata=dict(request_options.metadata) if request_options.metadata is not None else None,
                    onPayload=on_payload,
                    onResponse=on_response,
                    reasoning=get_value(stream_options, "reasoning"),
                    signal=get_value(stream_options, "signal"),
                    sessionId=turn_state.sessionId,
                    timeoutMs=request_options.timeoutMs,
                    transport=request_options.transport,
                    apiKey=auth_api_key,
                ),
            )

        return stream_fn

    async def _drain_queued_messages(self, queue: list[AgentMessage], mode: str) -> list[AgentMessage]:
        if mode == "all":
            messages = list(queue)
            queue.clear()
        else:
            messages = queue[:1]
            del queue[:1]
        if not messages:
            return messages
        try:
            await self._emit_queue_update()
            return messages
        except Exception:
            queue[:0] = messages
            raise

    def _create_loop_config(
        self,
        get_turn_state: Callable[[], AgentHarnessTurnState],
        set_turn_state: Callable[[AgentHarnessTurnState], None],
    ) -> AgentLoopConfig:
        turn_state = get_turn_state()

        async def transform_context(messages: list[AgentMessage], _signal: Any = None) -> list[AgentMessage]:
            result = await self._emit_hook(ContextEvent(messages=list(messages)))
            next_messages = get_value(result, "messages")
            return messages if next_messages is None else next_messages

        async def before_tool_call(context: Any, _signal: Any = None):
            result = await self._emit_hook(
                ToolCallEvent(
                    toolCallId=context.toolCall.id,
                    toolName=context.toolCall.name,
                    input=context.args,
                )
            )
            if result is None:
                return None
            return {"block": get_value(result, "block"), "reason": get_value(result, "reason")}

        async def after_tool_call(context: Any, _signal: Any = None):
            result = await self._emit_hook(
                ToolResultEvent(
                    toolCallId=context.toolCall.id,
                    toolName=context.toolCall.name,
                    input=context.args,
                    content=context.result.content,
                    details=context.result.details,
                    isError=context.isError,
                )
            )
            if result is None:
                return None
            return {
                "content": get_value(result, "content"),
                "details": get_value(result, "details"),
                "isError": get_value(result, "isError"),
                "terminate": get_value(result, "terminate"),
            }

        async def prepare_next_turn(_context: Any) -> AgentLoopTurnUpdate:
            await self.flushPendingSessionWrites()
            next_turn_state = await self._create_turn_state()
            set_turn_state(next_turn_state)
            return AgentLoopTurnUpdate(
                context=self._create_context(next_turn_state),
                model=next_turn_state.model,
                thinkingLevel=next_turn_state.thinkingLevel,
            )

        return AgentLoopConfig(
            model=turn_state.model,
            reasoning=None if turn_state.thinkingLevel == "off" else turn_state.thinkingLevel,
            convertToLlm=convert_to_llm,
            transformContext=transform_context,
            beforeToolCall=before_tool_call,
            afterToolCall=after_tool_call,
            prepareNextTurn=prepare_next_turn,
            getSteeringMessages=lambda: self._drain_queued_messages(self.steerQueue, self.steeringQueueMode),
            getFollowUpMessages=lambda: self._drain_queued_messages(self.followUpQueue, self.followUpQueueMode),
        )

    def _validate_tool_names(self, tool_names: list[str], tools: dict[str, AgentTool] | None = None) -> None:
        available_tools = tools or self.tools
        missing = [name for name in tool_names if name not in available_tools]
        if missing:
            raise AgentHarnessError("invalid_argument", f"Unknown tool(s): {', '.join(missing)}")

    async def flushPendingSessionWrites(self) -> None:
        while self.pendingSessionWrites:
            write = self.pendingSessionWrites[0]
            write_type = write["type"]
            if write_type == "message":
                await self.session.appendMessage(_coerce_agent_message(write["message"]))
            elif write_type == "model_change":
                await self.session.appendModelChange(write["provider"], write["modelId"])
            elif write_type == "thinking_level_change":
                await self.session.appendThinkingLevelChange(write["thinkingLevel"])
            elif write_type == "custom":
                await self.session.appendCustomEntry(write["customType"], write.get("data"))
            elif write_type == "custom_message":
                await self.session.appendCustomMessageEntry(
                    write["customType"],
                    write["content"],
                    write["display"],
                    write.get("details"),
                )
            elif write_type == "label":
                await self.session.appendLabel(write["targetId"], write.get("label"))
            elif write_type == "session_info":
                await self.session.appendSessionName(write.get("name", ""))
            elif write_type == "leaf":
                await self.session.getStorage().setLeafId(write.get("targetId"))
            self.pendingSessionWrites.pop(0)

    async def _handle_agent_event(self, event: AgentEvent, signal: AbortSignal | None = None) -> None:
        if event.type == "message_end":
            await self.session.appendMessage(_coerce_agent_message(event.message))
            await self._emit_any(event, signal)
            return
        if event.type == "turn_end":
            event_error = None
            try:
                await self._emit_any(event, signal)
            except Exception as error:
                event_error = error
            had_pending_mutations = bool(self.pendingSessionWrites)
            await self.flushPendingSessionWrites()
            if event_error is not None:
                raise event_error
            await self._emit_own(SavePointEvent(hadPendingMutations=had_pending_mutations))
            return
        if event.type == "agent_end":
            await self.flushPendingSessionWrites()
            self.phase = "idle"
            await self._emit_any(event, signal)
            await self._emit_own(SettledEvent(nextTurnCount=len(self.nextTurnQueue)), signal)
            return
        await self._emit_any(event, signal)

    async def _emit_run_failure(
        self,
        model: Model,
        error: Any,
        aborted: bool,
        signal: AbortSignal,
    ) -> list[AgentMessage]:
        failure_message = create_failure_message(model, error, aborted)
        await self._handle_agent_event(MessageStartEvent(message=failure_message), signal)
        await self._handle_agent_event(MessageEndEvent(message=failure_message), signal)
        await self._handle_agent_event(TurnEndEvent(message=failure_message, toolResults=[]), signal)
        await self._handle_agent_event(AgentEndEvent(messages=[failure_message]), signal)
        return [failure_message]

    async def _execute_turn(
        self,
        turn_state: AgentHarnessTurnState,
        text: str,
        options: dict[str, Any] | None = None,
    ) -> AssistantMessage:
        active_turn_state = turn_state
        messages: list[AgentMessage] = [create_user_message(text, (options or {}).get("images"))]
        if self.nextTurnQueue:
            queued_messages = list(self.nextTurnQueue)
            self.nextTurnQueue.clear()
            try:
                await self._emit_queue_update()
            except Exception:
                self.nextTurnQueue[:0] = queued_messages
                raise
            messages = [*queued_messages, messages[0]]

        before_result = await self._emit_hook(
            BeforeAgentStartEvent(
                prompt=text,
                images=(options or {}).get("images"),
                systemPrompt=turn_state.systemPrompt,
                resources=turn_state.resources,
            )
        )
        extra_messages = get_value(before_result, "messages")
        if extra_messages:
            messages.extend(extra_messages)
        messages = [_coerce_agent_message(message) for message in messages]

        abort_controller = AbortController()
        self.runAbortController = abort_controller

        def get_turn_state() -> AgentHarnessTurnState:
            return active_turn_state

        def set_turn_state(next_turn_state: AgentHarnessTurnState) -> None:
            nonlocal active_turn_state
            active_turn_state = next_turn_state

        async def run_result() -> list[AgentMessage]:
            try:
                return await run_agent_loop(
                    messages,
                    self._create_context(turn_state, get_value(before_result, "systemPrompt")),
                    self._create_loop_config(get_turn_state, set_turn_state),
                    lambda event: self._handle_agent_event(event, abort_controller.signal),
                    abort_controller.signal,
                    self._create_stream_fn(get_turn_state),
                )
            except Exception as error:
                try:
                    return await self._emit_run_failure(
                        active_turn_state.model,
                        error,
                        abort_controller.signal.aborted,
                        abort_controller.signal,
                    )
                except Exception as failure_error:
                    cause = ExceptionGroup(
                        "Agent run failed and failure reporting failed",
                        [to_error(error), to_error(failure_error)],
                    )
                    raise AgentHarnessError("unknown", str(cause), cause) from failure_error

        try:
            new_messages = await run_result()
            for message in reversed(new_messages):
                if get_value(message, "role") == "assistant":
                    return message
            raise AgentHarnessError("invalid_state", "AgentHarness prompt completed without an assistant message")
        finally:
            try:
                await self.flushPendingSessionWrites()
            finally:
                self.runAbortController = None

    async def prompt(self, text: str, options: dict[str, Any] | None = None) -> AssistantMessage:
        if self.phase != "idle":
            raise AgentHarnessError("busy", "AgentHarness is busy")
        self.phase = "turn"
        finish_run = self._start_run_promise()
        try:
            turn_state = await self._create_turn_state()
            return await self._execute_turn(turn_state, text, options)
        except Exception as error:
            self.phase = "idle"
            raise normalize_harness_error(error, "unknown") from error
        finally:
            finish_run()

    async def skill(self, name: str, additionalInstructions: str | None = None) -> AssistantMessage:
        if self.phase != "idle":
            raise AgentHarnessError("busy", "AgentHarness is busy")
        self.phase = "turn"
        finish_run = self._start_run_promise()
        try:
            turn_state = await self._create_turn_state()
            skills = turn_state.resources.skills or []
            skill = next((candidate for candidate in skills if candidate.name == name), None)
            if skill is None:
                raise AgentHarnessError("invalid_argument", f"Unknown skill: {name}")
            return await self._execute_turn(turn_state, format_skill_invocation(skill, additionalInstructions))
        except Exception as error:
            self.phase = "idle"
            raise normalize_harness_error(error, "unknown") from error
        finally:
            finish_run()

    async def promptFromTemplate(self, name: str, args: list[str] | None = None) -> AssistantMessage:
        if self.phase != "idle":
            raise AgentHarnessError("busy", "AgentHarness is busy")
        self.phase = "turn"
        finish_run = self._start_run_promise()
        try:
            turn_state = await self._create_turn_state()
            templates = turn_state.resources.promptTemplates or []
            template = next((candidate for candidate in templates if candidate.name == name), None)
            if template is None:
                raise AgentHarnessError("invalid_argument", f"Unknown prompt template: {name}")
            return await self._execute_turn(turn_state, format_prompt_template_invocation(template, args or []))
        except Exception as error:
            self.phase = "idle"
            raise normalize_harness_error(error, "unknown") from error
        finally:
            finish_run()

    async def steer(self, text: str, options: dict[str, Any] | None = None) -> None:
        if self.phase == "idle":
            raise AgentHarnessError("invalid_state", "Cannot steer while idle")
        self.steerQueue.append(create_user_message(text, (options or {}).get("images")))
        await self._emit_queue_update()

    async def followUp(self, text: str, options: dict[str, Any] | None = None) -> None:
        if self.phase == "idle":
            raise AgentHarnessError("invalid_state", "Cannot follow up while idle")
        self.followUpQueue.append(create_user_message(text, (options or {}).get("images")))
        await self._emit_queue_update()

    async def nextTurn(self, text: str, options: dict[str, Any] | None = None) -> None:
        self.nextTurnQueue.append(create_user_message(text, (options or {}).get("images")))
        await self._emit_queue_update()

    async def appendMessage(self, message: AgentMessage) -> None:
        try:
            normalized = _coerce_agent_message(message)
            if self.phase == "idle":
                await self.session.appendMessage(normalized)
            else:
                self.pendingSessionWrites.append({"type": "message", "message": normalized})
        except Exception as error:
            raise normalize_harness_error(error, "session") from error

    async def compact(self, customInstructions: str | None = None) -> CompactResult:
        if self.phase != "idle":
            raise AgentHarnessError("busy", "compact() requires idle harness")
        self.phase = "compaction"
        try:
            model = self.model
            auth = await maybe_await(self.getApiKeyAndHeaders(model)) if self.getApiKeyAndHeaders else None
            if not auth:
                raise AgentHarnessError("auth", "No auth available for compaction")
            branch_entries = await self.session.getBranch()
            preparation_result = prepare_compaction(branch_entries, DEFAULT_COMPACTION_SETTINGS)
            if not preparation_result.ok:
                raise preparation_result.error
            preparation = preparation_result.value
            if preparation is None:
                raise AgentHarnessError("compaction", "Nothing to compact")
            hook_result = await self._emit_hook(
                SessionBeforeCompactEvent(
                    preparation=preparation,
                    branchEntries=branch_entries,
                    signal=AbortController().signal,
                    customInstructions=customInstructions,
                )
            )
            if get_value(hook_result, "cancel"):
                raise AgentHarnessError("compaction", "Compaction cancelled")
            provided = get_value(hook_result, "compaction")
            compact_result = ok(provided) if provided is not None else await run_compaction(
                preparation,
                model,
                get_value(auth, "apiKey"),
                get_value(auth, "headers"),
                customInstructions,
                None,
                self.thinkingLevel,
            )
            if not compact_result.ok:
                raise compact_result.error
            result = compact_result.value
            entry_id = await self.session.appendCompaction(
                result.summary,
                result.firstKeptEntryId,
                result.tokensBefore,
                result.details,
                provided is not None,
            )
            entry = await self.session.getEntry(entry_id)
            if get_value(entry, "type") == "compaction":
                await self._emit_own(
                    SessionCompactEvent(
                        compactionEntry=entry,
                        fromHook=provided is not None,
                    )
                )
            return result
        except Exception as error:
            raise normalize_harness_error(error, "compaction") from error
        finally:
            self.phase = "idle"

    async def navigateTree(
        self,
        targetId: str,
        options: dict[str, Any] | None = None,
    ) -> NavigateTreeResult:
        if self.phase != "idle":
            raise AgentHarnessError("busy", "navigateTree() requires idle harness")
        self.phase = "branch_summary"
        opts = options or {}
        try:
            old_leaf_id = await self.session.getLeafId()
            if old_leaf_id == targetId:
                return NavigateTreeResult(cancelled=False)
            target_entry = await self.session.getEntry(targetId)
            if target_entry is None:
                raise AgentHarnessError("invalid_argument", f"Entry {targetId} not found")
            collected = await collect_entries_for_branch_summary(self.session, old_leaf_id, targetId)
            preparation = TreePreparation(
                targetId=targetId,
                oldLeafId=old_leaf_id,
                commonAncestorId=collected.commonAncestorId,
                entriesToSummarize=collected.entries,
                userWantsSummary=bool(opts.get("summarize", False)),
                customInstructions=opts.get("customInstructions"),
                replaceInstructions=opts.get("replaceInstructions"),
                label=opts.get("label"),
            )
            hook_result = await self._emit_hook(
                SessionBeforeTreeEvent(preparation=preparation, signal=AbortController().signal)
            )
            if get_value(hook_result, "cancel"):
                return NavigateTreeResult(cancelled=True)

            summary_entry = None
            summary_text = get_value(get_value(hook_result, "summary"), "summary")
            summary_details = get_value(get_value(hook_result, "summary"), "details")
            if summary_text is None and opts.get("summarize") and collected.entries:
                model = self.model
                auth = await maybe_await(self.getApiKeyAndHeaders(model)) if self.getApiKeyAndHeaders else None
                if not auth:
                    raise AgentHarnessError("auth", "No auth available for branch summary")
                branch_summary = await generate_branch_summary(
                    collected.entries,
                    GenerateBranchSummaryOptions(
                        model=model,
                        apiKey=get_value(auth, "apiKey"),
                        headers=get_value(auth, "headers"),
                        signal=AbortController().signal,
                        customInstructions=(
                            get_value(hook_result, "customInstructions")
                            if get_value(hook_result, "customInstructions") is not None
                            else opts.get("customInstructions")
                        ),
                        replaceInstructions=(
                            get_value(hook_result, "replaceInstructions")
                            if get_value(hook_result, "replaceInstructions") is not None
                            else opts.get("replaceInstructions")
                        ),
                    ),
                )
                if not branch_summary.ok:
                    if branch_summary.error.code == "aborted":
                        return NavigateTreeResult(cancelled=True)
                    raise AgentHarnessError("branch_summary", branch_summary.error.message, branch_summary.error)
                summary_text = branch_summary.value.summary
                summary_details = {
                    "readFiles": branch_summary.value.readFiles,
                    "modifiedFiles": branch_summary.value.modifiedFiles,
                }

            new_leaf_id: str | None
            editor_text: str | None = None
            target_type = get_value(target_entry, "type")
            if target_type == "message" and get_value(get_value(target_entry, "message"), "role") == "user":
                new_leaf_id = get_value(target_entry, "parentId")
                editor_text = text_from_content(get_value(get_value(target_entry, "message"), "content"))
            elif target_type == "custom_message":
                new_leaf_id = get_value(target_entry, "parentId")
                editor_text = text_from_content(get_value(target_entry, "content"))
            else:
                new_leaf_id = targetId

            summary_id = await self.session.moveTo(
                new_leaf_id,
                {
                    "summary": summary_text,
                    "details": summary_details,
                    "fromHook": get_value(hook_result, "summary") is not None,
                }
                if summary_text
                else None,
            )
            if summary_id:
                entry = await self.session.getEntry(summary_id)
                if get_value(entry, "type") == "branch_summary":
                    summary_entry = entry
            await self._emit_own(
                SessionTreeEvent(
                    newLeafId=await self.session.getLeafId(),
                    oldLeafId=old_leaf_id,
                    summaryEntry=summary_entry,
                    fromHook=get_value(hook_result, "summary") is not None,
                )
            )
            return NavigateTreeResult(cancelled=False, editorText=editor_text, summaryEntry=summary_entry)
        except Exception as error:
            raise normalize_harness_error(error, "branch_summary") from error
        finally:
            self.phase = "idle"

    def getModel(self) -> Model:
        return self.model

    def getThinkingLevel(self) -> ThinkingLevel:
        return self.thinkingLevel

    async def setModel(self, model: Model) -> None:
        try:
            previous_model = self.model
            model = _coerce_model(model)
            if self.phase == "idle":
                await self.session.appendModelChange(model.provider, model.id)
            else:
                self.pendingSessionWrites.append(
                    {"type": "model_change", "provider": model.provider, "modelId": model.id}
                )
            self.model = model
            await self._emit_own(ModelSelectEvent(model=model, previousModel=previous_model, source="set"))
        except Exception as error:
            raise normalize_harness_error(error, "session") from error

    async def setThinkingLevel(self, level: ThinkingLevel) -> None:
        try:
            previous_level = self.thinkingLevel
            if self.phase == "idle":
                await self.session.appendThinkingLevelChange(level)
            else:
                self.pendingSessionWrites.append({"type": "thinking_level_change", "thinkingLevel": level})
            self.thinkingLevel = level
            await self._emit_own(ThinkingLevelSelectEvent(level=level, previousLevel=previous_level))
        except Exception as error:
            raise normalize_harness_error(error, "session") from error

    async def setActiveTools(self, toolNames: list[str]) -> None:
        try:
            self._validate_tool_names(toolNames)
            self.activeToolNames = list(toolNames)
        except Exception as error:
            raise normalize_harness_error(error, "invalid_argument") from error

    def getSteeringMode(self) -> str:
        return self.steeringQueueMode

    async def setSteeringMode(self, mode: str) -> None:
        self.steeringQueueMode = mode

    def getFollowUpMode(self) -> str:
        return self.followUpQueueMode

    async def setFollowUpMode(self, mode: str) -> None:
        self.followUpQueueMode = mode

    def getResources(self) -> AgentHarnessResources[Any, Any]:
        return copy_resources(self.resources)

    async def setResources(self, resources: AgentHarnessResources[Any, Any] | dict[str, Any]) -> None:
        previous_resources = self.getResources()
        self.resources = copy_resources(resources)
        await self._emit_own(
            ResourcesUpdateEvent(
                resources=self.getResources(),
                previousResources=previous_resources,
            )
        )

    def getStreamOptions(self) -> AgentHarnessStreamOptions:
        return clone_stream_options(self.streamOptions)

    async def setStreamOptions(self, streamOptions: AgentHarnessStreamOptions | dict[str, Any]) -> None:
        self.streamOptions = clone_stream_options(streamOptions)

    async def setTools(self, tools: list[AgentTool], activeToolNames: list[str] | None = None) -> None:
        try:
            next_tools = {tool.name: tool for tool in tools}
            next_active_tool_names = (
                list(activeToolNames) if activeToolNames is not None else list(self.activeToolNames)
            )
            self._validate_tool_names(next_active_tool_names, next_tools)
            self.tools = next_tools
            self.activeToolNames = next_active_tool_names
        except Exception as error:
            raise normalize_harness_error(error, "invalid_argument") from error

    async def abort(self) -> AbortResult:
        cleared_steer = list(self.steerQueue)
        cleared_follow_up = list(self.followUpQueue)
        self.steerQueue.clear()
        self.followUpQueue.clear()
        if self.runAbortController is not None:
            self.runAbortController.abort()
        errors: list[Exception] = []
        try:
            await self._emit_queue_update()
        except Exception as error:
            errors.append(to_error(error))
        try:
            await self.waitForIdle()
        except Exception as error:
            errors.append(to_error(error))
        try:
            await self._emit_own(
                AbortEvent(
                    clearedSteer=cleared_steer,
                    clearedFollowUp=cleared_follow_up,
                )
            )
        except Exception as error:
            errors.append(to_error(error))
        if errors:
            cause: Exception
            if len(errors) == 1:
                cause = errors[0]
            else:
                cause = ExceptionGroup("Abort completed with errors", errors)
            raise normalize_harness_error(cause, "hook") from cause
        return AbortResult(clearedSteer=cleared_steer, clearedFollowUp=cleared_follow_up)

    async def waitForIdle(self) -> None:
        if self.runPromise is not None:
            await self.runPromise

    def subscribe(
        self,
        listener: Callable[[AgentHarnessEvent, AbortSignal | None], Awaitable[None] | None],
    ) -> Callable[[], None]:
        handlers = self.handlers.setdefault(SUBSCRIBER_EVENT_TYPE, [])
        if listener not in handlers:
            handlers.append(listener)

        def unsubscribe() -> None:
            if listener in handlers:
                handlers.remove(listener)

        return unsubscribe

    def on(
        self,
        event_type: str,
        handler: Callable[[Any], Awaitable[Any] | Any],
    ) -> Callable[[], None]:
        handlers = self.handlers.setdefault(event_type, [])
        if handler not in handlers:
            handlers.append(handler)

        def unsubscribe() -> None:
            if handler in handlers:
                handlers.remove(handler)

        return unsubscribe


def copy_resources(
    resources: AgentHarnessResources[Any, Any] | dict[str, Any] | None,
) -> AgentHarnessResources[Any, Any]:
    if resources is None:
        return AgentHarnessResources()
    if isinstance(resources, AgentHarnessResources):
        return AgentHarnessResources(
            skills=list(resources.skills) if resources.skills is not None else None,
            promptTemplates=list(resources.promptTemplates) if resources.promptTemplates is not None else None,
        )
    return AgentHarnessResources(
        skills=list(resources.get("skills")) if resources.get("skills") is not None else None,
        promptTemplates=(
            list(resources.get("promptTemplates")) if resources.get("promptTemplates") is not None else None
        ),
    )


def text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if get_value(block, "type") == "text":
            text = get_value(block, "text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def get_value(value: Any, name: str) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _to_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        if isinstance(dumped, dict):
            return dict(dumped)
    return {field.name: getattr(value, field.name) for field in fields(value)}


def _coerce_model(model: Model | dict[str, Any]) -> Model:
    if isinstance(model, Model):
        return model
    return Model.model_validate(model)


def _coerce_agent_message(message: AgentMessage) -> AgentMessage:
    role = get_value(message, "role")
    if role in {"user", "assistant", "toolResult"}:
        return validate_message(_message_dump(message))
    if role == "custom":
        return CustomMessage(
            customType=str(get_value(message, "customType")),
            content=_normalize_custom_content(get_value(message, "content")),
            display=bool(get_value(message, "display")),
            details=get_value(message, "details"),
            timestamp=int(get_value(message, "timestamp") or int(time.time() * 1000)),
        )
    if role == "branchSummary":
        return BranchSummaryMessage(
            summary=str(get_value(message, "summary")),
            fromId=str(get_value(message, "fromId")),
            timestamp=int(get_value(message, "timestamp") or int(time.time() * 1000)),
        )
    if role == "compactionSummary":
        return CompactionSummaryMessage(
            summary=str(get_value(message, "summary")),
            tokensBefore=int(get_value(message, "tokensBefore") or 0),
            timestamp=int(get_value(message, "timestamp") or int(time.time() * 1000)),
        )
    if role == "bashExecution":
        return BashExecutionMessage(
            command=str(get_value(message, "command")),
            output=str(get_value(message, "output") or ""),
            exitCode=get_value(message, "exitCode"),
            cancelled=bool(get_value(message, "cancelled")),
            truncated=bool(get_value(message, "truncated")),
            fullOutputPath=get_value(message, "fullOutputPath"),
            excludeFromContext=get_value(message, "excludeFromContext"),
            timestamp=int(get_value(message, "timestamp") or int(time.time() * 1000)),
        )
    return message


def _normalize_custom_content(content: Any) -> str | list[TextContent | ImageContent]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return [validate_user_content(_message_dump(block)) for block in content]


def _message_dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return dict(value)
    return value


async def maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


__all__ = [
    "AgentHarness",
    "AgentHarnessTurnState",
    "apply_stream_options_patch",
    "clone_stream_options",
    "copy_resources",
    "create_failure_message",
    "create_user_message",
    "merge_headers",
    "normalize_harness_error",
]

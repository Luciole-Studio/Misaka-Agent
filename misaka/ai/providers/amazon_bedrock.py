"""Amazon Bedrock ConverseStream provider adapter."""

from __future__ import annotations

import asyncio
import base64
import os
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypedDict, cast
from urllib.parse import urlparse

try:
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config
    from botocore.exceptions import ClientError
except ImportError:  # optional extra: misaka[bedrock]
    boto3 = UNSIGNED = Config = None

    class ClientError(Exception):  # keeps `except ClientError` valid; never raised without boto3
        pass

from misaka.ai.models import calculate_cost
from misaka.ai.providers._common import (
    _await_with_signal,
    _close_stream,
    _create_abort_wait_task,
    _empty_usage,
    _option,
    resolve_cache_retention,
    safe_json_stringify,
)
from misaka.ai.providers.constrained_sampling import (
    get_json_schema_tool_parameters,
    resolve_json_schema_strict_sampling,
)
from misaka.ai.providers.sdk import require
from misaka.ai.providers.simple_options import (
    adjust_max_tokens_for_thinking,
    build_base_options,
    clamp_reasoning,
    clamp_thinking_budget_to_answer_room,
)
from misaka.ai.providers.transform_messages import transform_messages
from misaka.ai.types import (
    AssistantMessage,
    CacheRetention,
    Context,
    DoneEvent,
    ErrorEvent,
    Model,
    SimpleStreamOptions,
    StartEvent,
    StopReason,
    StreamOptions,
    TextContent,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ThinkingBudgets,
    ThinkingContent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingLevel,
    ThinkingStartEvent,
    Tool,
    ToolCall,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultMessage,
)
from misaka.ai.utils.diagnostics import (
    AssistantMessageDiagnostic,
    append_assistant_message_diagnostic,
)
from misaka.ai.utils.estimate import clamp_max_tokens_to_context
from misaka.ai.utils.event_stream import AssistantMessageEventStream, spawn_stream_task
from misaka.ai.utils.headers import headers_to_record
from misaka.ai.utils.json_parse import StreamingArgs
from misaka.ai.utils.node_http_proxy import create_http_proxy_agents_for_target
from misaka.ai.utils.provider_env import get_provider_env_value
from misaka.ai.utils.sanitize_unicode import sanitize_surrogates
from misaka.utils.values import maybe_await, signal_aborted

# Bedrock rejects a text block whose text is empty, and rejects a message whose content
# list is empty. Upstream substitutes this placeholder rather than dropping the message,
# because dropping one breaks the strict user/assistant alternation the API also requires.
EMPTY_TEXT_PLACEHOLDER = "<empty>"
# What a redacted reasoning block reads as in the transcript. Same string the anthropic
# adapter uses, so a redacted turn looks the same whichever provider produced it.
REDACTED_THINKING_PLACEHOLDER = "[Reasoning redacted]"
# A diagnostic is a breadcrumb, not a payload: anything longer than this is not an id.
MAX_BEDROCK_DIAGNOSTIC_VALUE_CHARS = 200
# SigV4 computes over these, and the bearer path owns Authorization. A caller header that
# lands on one of them either invalidates the signature or replaces the credential.
_RESERVED_HEADER_EXACT = frozenset({"authorization", "host"})
# An inference-profile ARN names its own region; it must beat AWS_REGION, which is
# usually set for some other service entirely.
_ARN_REGION_PATTERN = re.compile(r"^arn:aws(?:-[a-z0-9-]+)?:bedrock:([a-z0-9-]+):")

BedrockThinkingDisplay = Literal["summarized", "omitted"]


class BedrockOptions(TypedDict, total=False):
    apiKey: str
    headers: dict[str, str]
    signal: Any
    sessionId: str
    cacheRetention: str
    onPayload: Any
    onResponse: Any
    timeoutMs: int
    maxRetries: int
    region: str
    profile: str
    toolChoice: str | dict[str, str]
    reasoning: str
    thinkingBudgets: dict[str, int]
    interleavedThinking: bool
    thinkingDisplay: BedrockThinkingDisplay
    requestMetadata: dict[str, str]
    bearerToken: str

BEDROCK_ERROR_PREFIXES: dict[str, str] = {
    "InternalServerException": "Internal server error",
    "ModelStreamErrorException": "Model stream error",
    "ValidationException": "Validation error",
    "ThrottlingException": "Throttling error",
    "ServiceUnavailableException": "Service unavailable",
}
BEDROCK_DATA_RETENTION_DOCS_URL = "https://docs.aws.amazon.com/bedrock/latest/userguide/data-retention.html"
_DATA_RETENTION_PATTERN = re.compile(r"data retention mode", re.IGNORECASE)
_STANDARD_BEDROCK_ENDPOINT_PATTERN = re.compile(
    r"^bedrock-runtime(?:-fips)?\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?$"
)
_MATCH_NORMALIZATION_PATTERN = re.compile(r"[\s_.:]+")
_STREAM_SENTINEL = object()


@dataclass(frozen=True, slots=True)
class BedrockCredentials:
    accessKeyId: str
    secretAccessKey: str
    sessionToken: str | None = None


@dataclass(frozen=True, slots=True)
class BedrockClientSettings:
    profile_name: str | None
    region_name: str | None
    endpoint_url: str | None
    config_kwargs: dict[str, Any]
    default_headers: dict[str, str]
    bearer_token: str | None
    credentials: BedrockCredentials | None = None


class BedrockRuntimeServiceException(RuntimeError):
    def __init__(self, name: str, message: str) -> None:
        super().__init__(message)
        self.name = name


def create_client(model: Model, options: StreamOptions | dict[str, Any] | None = None) -> Any:
    settings = build_client_settings(model, options)
    session = require(boto3, "boto3").Session(profile_name=settings.profile_name)
    client_kwargs: dict[str, Any] = {}
    if settings.region_name is not None:
        client_kwargs["region_name"] = settings.region_name
    if settings.endpoint_url is not None:
        client_kwargs["endpoint_url"] = settings.endpoint_url
    if settings.credentials is not None:
        client_kwargs["aws_access_key_id"] = settings.credentials.accessKeyId
        client_kwargs["aws_secret_access_key"] = settings.credentials.secretAccessKey
        if settings.credentials.sessionToken:
            client_kwargs["aws_session_token"] = settings.credentials.sessionToken
    if settings.config_kwargs:
        client_kwargs["config"] = Config(**settings.config_kwargs)

    client = session.client("bedrock-runtime", **client_kwargs)
    _register_request_overrides(client, settings.default_headers, settings.bearer_token)
    return client


def get_configured_bedrock_credentials(env: Any = None) -> BedrockCredentials | None:
    """Explicit AWS keys, or ``None`` to leave resolution to the SDK's own chain.

    Both halves of the pair are required: an access key without its secret is a
    half-configured environment, and handing it to the SDK replaces a working default
    chain with one that cannot sign.
    """
    access_key_id = get_provider_env_value("AWS_ACCESS_KEY_ID", env)
    secret_access_key = get_provider_env_value("AWS_SECRET_ACCESS_KEY", env)
    if not access_key_id or not secret_access_key:
        return None
    return BedrockCredentials(
        accessKeyId=access_key_id,
        secretAccessKey=secret_access_key,
        sessionToken=get_provider_env_value("AWS_SESSION_TOKEN", env),
    )


def build_client_settings(model: Model, options: StreamOptions | dict[str, Any] | None = None) -> BedrockClientSettings:
    env = _option(options, "env")
    configured_region = get_configured_bedrock_region(options)
    # Deliberately ambient-only, like upstream: this asks whether the *machine* is set up
    # around a profile, which is what decides between pinning a catalog endpoint and
    # letting the SDK resolve one. A request-scoped profile does not answer that.
    has_configured_profile = has_configured_bedrock_profile()
    endpoint_region = get_standard_bedrock_endpoint_region(model.baseUrl)
    use_explicit_endpoint = should_use_explicit_bedrock_endpoint(
        model.baseUrl,
        configured_region,
        has_configured_profile,
    )

    config_kwargs: dict[str, Any] = {}
    retries = _option(options, "maxRetries")
    if retries is not None:
        # botocore counts the initial attempt; StreamOptions counts retries.
        config_kwargs["retries"] = {"total_max_attempts": max(0, int(retries)) + 1}
    timeout_ms = _option(options, "timeoutMs")
    if timeout_ms is not None:
        config_kwargs["read_timeout"] = timeout_ms / 1000
    proxy_agents = create_http_proxy_agents_for_target(model.baseUrl, _option(options, "env"))
    if proxy_agents is not None:
        config_kwargs["proxies"] = {
            "http": proxy_agents.httpAgent,
            "https": proxy_agents.httpsAgent,
        }
    # AWS_BEDROCK_FORCE_HTTP1 has no counterpart here and needs none: upstream sets it to
    # swap the SDK's default HTTP/2 handler for an HTTP/1.1 one, and botocore only ever
    # speaks HTTP/1.1. The endpoints that switch it on already get what they asked for.

    # A profile configured through the auth flow -- the option, or AWS_PROFILE scoped to
    # the stored credential -- must beat ambient access keys, so it also suppresses the
    # explicit-credentials branch below. The SDK's own chain already prefers a profile,
    # but only while `credentials` is left unset.
    options_profile = _option(options, "profile") or (env.get("AWS_PROFILE") if env else None)
    profile_name = options_profile or get_provider_env_value("AWS_PROFILE", env)

    skip_auth = get_provider_env_value("AWS_BEDROCK_SKIP_AUTH", env) == "1"
    bearer_token = (
        _option(options, "bearerToken")
        or _option(options, "apiKey")
        or get_provider_env_value("AWS_BEARER_TOKEN_BEDROCK", env)
    )
    if bearer_token and not skip_auth:
        config_kwargs["signature_version"] = UNSIGNED
    else:
        bearer_token = None

    credentials: BedrockCredentials | None = None
    if skip_auth:
        # A proxy that fronts Bedrock without authenticating still makes the SDK sign,
        # and the SDK refuses to sign with no credentials at all. These are the throwaway
        # ones upstream sends for exactly that case.
        credentials = BedrockCredentials(accessKeyId="dummy-access-key", secretAccessKey="dummy-secret-key")
    else:
        configured = get_configured_bedrock_credentials(env)
        if configured is not None and not options_profile:
            credentials = configured

    # Custom headers from models.json and from the call site: declared, and until now dropped.
    default_headers: dict[str, str] = {}
    for source in (model.headers, _option(options, "headers")):
        if source:
            default_headers.update(headers_to_record(source))

    # An ARN carries its own region and wins over everything: AWS_REGION is usually set
    # for some other service, and signing an ARN request for the wrong region just fails.
    arn_region = _ARN_REGION_PATTERN.match(model.id)
    region_name = arn_region.group(1) if arn_region else configured_region
    if region_name is None and endpoint_region is not None and use_explicit_endpoint:
        region_name = endpoint_region
    if region_name is None and not has_configured_profile:
        region_name = "us-east-1"

    return BedrockClientSettings(
        profile_name=profile_name,
        region_name=region_name,
        endpoint_url=model.baseUrl if use_explicit_endpoint else None,
        config_kwargs=config_kwargs,
        default_headers=default_headers,
        bearer_token=bearer_token,
        credentials=credentials,
    )


def is_reserved_bedrock_header(key: str) -> bool:
    """``True`` for a header the caller must not be allowed to set.

    ``x-amz-*`` carries the request's own date and content hash, ``host`` is what the
    signature binds the request to, and ``authorization`` *is* the signature. Overwriting
    any of them turns a valid request into a 403, so upstream drops them silently rather
    than letting a stray header from models.json break every call to the provider.
    """
    lower = key.lower()
    return lower.startswith("x-amz-") or lower in _RESERVED_HEADER_EXACT


def _set_header(request: Any, key: str, value: str) -> None:
    # botocore's header container is multi-valued (an email.message.Message underneath),
    # so a plain assignment appends a second copy instead of replacing the first.
    try:
        del request.headers[key]
    except (KeyError, TypeError):
        pass
    request.headers[key] = value


def _register_request_overrides(client: Any, headers: dict[str, str], bearer_token: str | None) -> None:
    if not headers and not bearer_token:
        return

    def apply(request: Any, **_kwargs: Any) -> None:
        for key, value in headers.items():
            if is_reserved_bedrock_header(key):
                continue
            _set_header(request, key, value)
        if bearer_token:
            _set_header(request, "Authorization", f"Bearer {bearer_token}")

    # `before-sign`, not `before-send`: SigV4 signs over the headers, so one added after
    # signing that collides with a signed name invalidates the signature. Upstream hooks
    # the SDK's `build` step for the same reason. botocore emits this event even under
    # UNSIGNED, so the bearer-token path still gets its Authorization header.
    client.meta.events.register("before-sign.bedrock-runtime.ConverseStream", apply)


def stream_bedrock(
    model: Model,
    context: Context,
    options: StreamOptions | dict[str, Any] | None = None,
) -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()

    async def run() -> None:
        output = AssistantMessage(
            content=[],
            api="bedrock-converse-stream",
            provider=model.provider,
            model=model.id,
            usage=_empty_usage(),
            stopReason="stop",
            timestamp=time.time_ns() // 1_000_000,
        )
        signal = _option(options, "signal")
        response_stream: Any = None
        # Declared out here so a failure anywhere below can still sweep what arrived.
        partial_json: dict[int, StreamingArgs] = {}
        redacted_chunks: dict[int, list[bytes]] = {}
        saw_message_stop = False
        # Kept out here so the catch can still correlate a mid-stream failure: an
        # exception delivered as a stream event carries no HTTP metadata of its own.
        response_request_id: str | None = None

        try:
            client = _option(options, "client")
            if not client:
                # boto3's Session/client construction reads botocore's JSON service models
                # off disk; on the loop that blocks every other stream, the TUI and the
                # lease heartbeats along with it.
                client = await asyncio.to_thread(create_client, model, options)
            env = _option(options, "env")
            cache_retention = resolve_cache_retention(_option(options, "cacheRetention"), _option(options, "env"))
            inference_max_tokens = _option(options, "maxTokens")
            if inference_max_tokens is None and is_anthropic_claude_model(model):
                inference_max_tokens = model.maxTokens

            command_input: dict[str, Any] = {
                "modelId": model.id,
                "messages": convert_messages(context, model, cache_retention, env),
                "system": build_system_prompt(context.systemPrompt, model, cache_retention, env),
                "inferenceConfig": {
                    **({"maxTokens": inference_max_tokens} if inference_max_tokens is not None else {}),
                    **({"temperature": _option(options, "temperature")} if _option(options, "temperature") is not None else {}),
                },
                "toolConfig": convert_tool_config(
                    context.tools,
                    _option(options, "toolChoice"),
                    bool(getattr(getattr(model, "compat", None), "supportsStrictMode", None)),
                ),
                "additionalModelRequestFields": build_additional_model_request_fields(model, options),
                **({"requestMetadata": _option(options, "requestMetadata")} if _option(options, "requestMetadata") is not None else {}),
            }

            on_payload = _option(options, "onPayload")
            if callable(on_payload):
                next_input = await maybe_await(on_payload(command_input, model))
                if next_input is not None:
                    command_input = next_input

            if signal_aborted(signal):
                raise RuntimeError("Request was aborted")

            request_input = {key: value for key, value in command_input.items() if value is not None}
            # Synchronous boto3: DNS, TLS, upload and time-to-first-byte all happen inside
            # this one call, so it has to run off the loop. The stream iteration below was
            # already off it; only the request that opens the stream was left behind.
            response = await _await_with_signal(asyncio.to_thread(client.converse_stream, **request_input), signal)
            response_metadata = response.get("ResponseMetadata", {}) if isinstance(response, dict) else {}
            response_request_id = normalize_bedrock_diagnostic_value(response_metadata.get("RequestId"))
            on_response = _option(options, "onResponse")
            if callable(on_response) and response_metadata.get("HTTPStatusCode") is not None:
                # botocore keeps the full response header dict on ResponseMetadata; pass it
                # through so a custom gateway's rate-limit/billing headers reach onResponse
                # (upstream reads the raw Smithy HttpResponse for the same reason).
                raw_headers = response_metadata.get("HTTPHeaders")
                headers: dict[str, str] = (
                    {str(key): str(value) for key, value in raw_headers.items()}
                    if isinstance(raw_headers, Mapping)
                    else {}
                )
                if response_metadata.get("RequestId"):
                    headers.setdefault("x-amzn-requestid", str(response_metadata["RequestId"]))
                await maybe_await(
                    on_response({"status": int(response_metadata["HTTPStatusCode"]), "headers": headers}, model)
                )

            response_stream = response.get("stream") if isinstance(response, dict) else None
            block_indices: dict[int, int] = {}

            async for item in iterate_stream_events(response_stream, signal):
                if signal_aborted(signal):
                    raise RuntimeError("Request was aborted")

                if "messageStart" in item:
                    message_start = item["messageStart"] or {}
                    if message_start.get("role") != "assistant":
                        raise RuntimeError("Unexpected assistant message start but got user message start instead")
                    stream.push(StartEvent(partial=output))
                    continue

                if "contentBlockStart" in item:
                    handle_content_block_start(item["contentBlockStart"], block_indices, partial_json, output, stream)
                    continue

                if "contentBlockDelta" in item:
                    handle_content_block_delta(
                        item["contentBlockDelta"], block_indices, partial_json, output, stream, redacted_chunks
                    )
                    continue

                if "contentBlockStop" in item:
                    handle_content_block_stop(
                        item["contentBlockStop"], block_indices, partial_json, output, stream, redacted_chunks
                    )
                    continue

                if "messageStop" in item:
                    message_stop = item["messageStop"] or {}
                    saw_message_stop = True
                    stop_result = map_stop_reason(message_stop.get("stopReason"))
                    output.stopReason = cast(StopReason, stop_result["stopReason"])
                    if stop_result.get("errorMessage"):
                        output.errorMessage = stop_result["errorMessage"]
                    continue

                if "metadata" in item:
                    handle_metadata(item["metadata"], model, output)
                    continue

                for event_name in (
                    "internalServerException",
                    "modelStreamErrorException",
                    "validationException",
                    "throttlingException",
                    "serviceUnavailableException",
                ):
                    if event_name in item:
                        payload = item[event_name] or {}
                        exception_name = event_name[0].upper() + event_name[1:]
                        raise BedrockRuntimeServiceException(exception_name, str(payload.get("message") or ""))

            finish_open_tool_arguments(partial_json, output, redacted_chunks)

            if signal_aborted(signal):
                raise RuntimeError("Request was aborted")
            # The same structural guard the other four adapters carry (anthropic raises on
            # "ended before message_stop"; the responses family on "ended before
            # response.completed"; completions and mistral finish their blocks
            # unconditionally after the loop). Without it a stream cut mid-tool-call still
            # carried the constructor's "stop", so a truncated call was pushed as a clean
            # DoneEvent and ran.
            # Not gated on ``saw_message_start``: a response whose "stream" key is missing,
            # or a proxy that answers 200 with an empty event stream, iterates zero times
            # and would otherwise reach here holding the constructor's "stop" and push a
            # clean DoneEvent for a turn that never happened. Upstream's equivalent is a
            # "pending" initial stopReason, which misaka's StopReason cannot express.
            if not saw_message_stop:
                raise RuntimeError("Bedrock stream ended without a stop reason")
            if output.stopReason in {"error", "aborted"}:
                raise RuntimeError(output.errorMessage or "An unknown error occurred")

            stream.push(DoneEvent(reason=output.stopReason, message=output))
        except Exception as error:  # noqa: BLE001
            finish_open_tool_arguments(partial_json, output, redacted_chunks)
            output.stopReason = "aborted" if signal_aborted(signal) else "error"
            output.errorMessage = format_bedrock_error(error)
            if output.stopReason == "error":
                append_bedrock_failure_diagnostic(output, error, response_request_id)
            stream.push(ErrorEvent(reason=output.stopReason, error=output), cause=error)
        finally:
            await _close_stream(response_stream)
            stream.end()

    spawn_stream_task(run(), stream=stream)
    return stream


def stream_simple_bedrock(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    base = build_base_options(model, context, options, None)
    if options is None or options.reasoning is None:
        return stream_bedrock(model, context, {**base.model_dump(), "reasoning": None})

    if is_anthropic_claude_model(model):
        if supports_adaptive_thinking(model.id, model.name):
            return stream_bedrock(
                model,
                context,
                {
                    **base.model_dump(),
                    "reasoning": options.reasoning,
                    "thinkingBudgets": options.thinkingBudgets,
                },
            )

        adjusted = adjust_max_tokens_for_thinking(
            base.maxTokens,
            model.maxTokens,
            options.reasoning,
            options.thinkingBudgets,
        )
        # ``adjust_max_tokens_for_thinking`` adds the budget on top of a base that was
        # already fitted to the context window, so the sum can exceed what is left of the
        # window again -- Bedrock answers that with a ValidationException instead of a
        # shorter reply. Clamp once more, then keep MIN_ANSWER_TOKENS of the clamped
        # ceiling for the answer (the anthropic adapter's stream_simple does both).
        max_tokens = clamp_max_tokens_to_context(model, context, adjusted.maxTokens)
        clamped_level = clamp_reasoning(options.reasoning)
        merged_budgets = dict(options.thinkingBudgets.model_dump() if options.thinkingBudgets is not None else {})
        if clamped_level is not None:
            merged_budgets[clamped_level] = clamp_thinking_budget_to_answer_room(adjusted.thinkingBudget, max_tokens)

        return stream_bedrock(
            model,
            context,
            {
                **base.model_dump(),
                "maxTokens": max_tokens,
                "reasoning": options.reasoning,
                "thinkingBudgets": merged_budgets,
            },
        )

    return stream_bedrock(
        model,
        context,
        {
            **base.model_dump(),
            "reasoning": options.reasoning,
            "thinkingBudgets": options.thinkingBudgets,
        },
    )


async def iterate_stream_events(response_stream: Any, signal: Any = None):
    if response_stream is None:
        return
    # One abort task for the whole stream, not one per event: creating and cancelling a
    # ``signal.wait()`` task per item put ~56us of task churn on the loop thread for every
    # line of every concurrent response. Cancelled in the finally, with nothing awaited
    # after it -- see _iterate_async_iterable, which does the same for the other adapters.
    abort_task = _create_abort_wait_task(signal)
    try:
        if hasattr(response_stream, "__aiter__"):
            iterator = response_stream.__aiter__()
            while True:
                try:
                    item = await _await_with_signal(
                        iterator.__anext__(),
                        signal,
                        on_abort=lambda: _close_stream(response_stream),
                        abort_task=abort_task,
                    )
                except StopAsyncIteration:
                    return
                yield item

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()

        def worker() -> None:
            try:
                for event in response_stream:
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except Exception as error:  # noqa: BLE001 - any stream failure is delivered to the consumer as an item
                loop.call_soon_threadsafe(queue.put_nowait, error)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, _STREAM_SENTINEL)

        threading.Thread(target=worker, daemon=True).start()

        while True:
            item = await _await_with_signal(
                queue.get(),
                signal,
                on_abort=lambda: _close_stream(response_stream),
                abort_task=abort_task,
            )
            if item is _STREAM_SENTINEL:
                return
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        if abort_task is not None:
            abort_task.cancel()


def normalize_bedrock_diagnostic_value(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    if not trimmed or len(trimmed) > MAX_BEDROCK_DIAGNOSTIC_VALUE_CHARS:
        return None
    return trimmed


def _bedrock_error_metadata(error: Any) -> dict[str, Any]:
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        metadata = response.get("ResponseMetadata")
        if isinstance(metadata, dict):
            return metadata
    return {}


def extract_bedrock_error_code(error: Any) -> str | None:
    """The service exception's own name, e.g. ``ThrottlingException``.

    Upstream reads ``error.name``, which the JS SDK sets to the modelled exception. The
    three shapes here are the same fact in botocore's vocabulary: the stream-event
    exception carries ``name``, a client error carries the code in its response body, and
    a modelled exception class is named after itself. The ``Exception`` suffix is the
    guard in all three -- a plain ``RuntimeError`` is not a service error code.
    """
    if not isinstance(error, BaseException):
        return None
    name = getattr(error, "name", None)
    if not isinstance(name, str) or not name:
        response = getattr(error, "response", None)
        if isinstance(response, dict) and isinstance(response.get("Error"), dict):
            name = response["Error"].get("Code")
    if not isinstance(name, str) or not name:
        name = type(error).__name__
    if not name.endswith("Exception"):
        return None
    return normalize_bedrock_diagnostic_value(name)


def append_bedrock_failure_diagnostic(
    output: AssistantMessage,
    error: Any,
    fallback_request_id: str | None,
) -> None:
    """Attach whatever correlates this failure with AWS's own record of it.

    Nothing here is the error message -- that is already on the message. This is the
    status, the service's code for it, and the request id, which is the only handle a
    user has when they take a Bedrock failure to AWS support.
    """
    metadata = _bedrock_error_metadata(error)
    details: dict[str, Any] = {}

    status = metadata.get("HTTPStatusCode")
    if isinstance(status, int) and not isinstance(status, bool):
        details["status"] = status

    error_code = extract_bedrock_error_code(error)
    if error_code is not None:
        details["errorCode"] = error_code

    request_id = normalize_bedrock_diagnostic_value(metadata.get("RequestId")) or fallback_request_id
    if request_id is not None:
        details["requestId"] = request_id

    if not details:
        return

    append_assistant_message_diagnostic(
        output,
        AssistantMessageDiagnostic(
            type="bedrock_response_failure",
            timestamp=int(time.time() * 1000),
            details=details,
        ),
    )


def _bedrock_data_retention_hint(core: str) -> str:
    """Some models reject the account/profile's configured data retention mode.

    Upstream (``bedrock-converse-stream.ts:382``) appends the AWS docs link to any error
    whose text mentions the mode, so the user is told where to change it.
    """
    return (
        f" See {BEDROCK_DATA_RETENTION_DOCS_URL} for supported data retention modes."
        if _DATA_RETENTION_PATTERN.search(core)
        else ""
    )


def format_bedrock_error(error: Any) -> str:
    if isinstance(error, ClientError):
        name = error.response.get("Error", {}).get("Code", "ClientError")
        prefix = BEDROCK_ERROR_PREFIXES.get(name, name)
        core = str(error.response.get("Error", {}).get("Message", str(error)))
        return f"{prefix}: {core}{_bedrock_data_retention_hint(core)}"

    message = str(error) if isinstance(error, Exception) else safe_json_stringify(error)
    hint = _bedrock_data_retention_hint(message)
    name = getattr(error, "name", None) or error.__class__.__name__
    prefix = BEDROCK_ERROR_PREFIXES.get(name)
    return f"{prefix}: {message}{hint}" if prefix else f"{message}{hint}"


def flush_redacted_content(
    output: AssistantMessage,
    content_index: int,
    redacted_chunks: dict[int, list[bytes]] | None,
) -> None:
    """Encode the buffered encrypted reasoning into ``thinkingSignature`` and drop the buffer.

    The buffer is raw bytes and must never reach a persisted message; encoding has to
    happen once over the whole run, because base64 of two chunks is not the two chunks'
    base64 concatenated unless every chunk length happens to be a multiple of three.
    """
    if not redacted_chunks:
        return
    chunks = redacted_chunks.pop(content_index, None)
    if not chunks or content_index >= len(output.content):
        return
    block = output.content[content_index]
    if block.type == "thinking":
        block.thinkingSignature = base64.b64encode(b"".join(chunks)).decode("ascii")


def finish_open_tool_arguments(
    partial_json: dict[int, StreamingArgs],
    output: AssistantMessage,
    redacted_chunks: dict[int, list[bytes]] | None = None,
) -> None:
    """Parse the buffer of every tool block the stream never closed.

    ``StreamingArgs`` skips re-parsing while the unparsed tail is under
    ``max(2048, len//8)`` bytes, and the exact parse happens in ``finish()`` -- which only
    the block-stop handler calls. A block left open by a cut stream would otherwise keep
    the last throttled view and silently drop everything that arrived after it. The raw
    buffer is always exact; this is the one parse that makes the block match it.
    """
    for content_index, accumulated in partial_json.items():
        if not accumulated.raw or content_index >= len(output.content):
            continue
        block = output.content[content_index]
        if block.type == "toolCall":
            block.arguments = accumulated.finish()
    partial_json.clear()
    # A stream can settle without stopping every block, so sweep the redacted buffers too:
    # what is left here belongs to blocks the stream never closed.
    if redacted_chunks:
        for content_index in list(redacted_chunks):
            flush_redacted_content(output, content_index, redacted_chunks)


def handle_content_block_start(
    event: dict[str, Any],
    block_indices: dict[int, int],
    partial_json: dict[int, StreamingArgs],
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
) -> None:
    content_block_index = int(event.get("contentBlockIndex") or 0)
    start = event.get("start") or {}
    tool_use = start.get("toolUse") or {}
    if not tool_use:
        return

    block = ToolCall(
        id=str(tool_use.get("toolUseId") or ""),
        name=str(tool_use.get("name") or ""),
        arguments={},
    )
    output.content.append(block)
    content_index = len(output.content) - 1
    block_indices[content_block_index] = content_index
    partial_json[content_index] = StreamingArgs()
    stream.push(ToolCallStartEvent(contentIndex=content_index, partial=output))


def handle_content_block_delta(
    event: dict[str, Any],
    block_indices: dict[int, int],
    partial_json: dict[int, StreamingArgs],
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
    redacted_chunks: dict[int, list[bytes]] | None = None,
) -> None:
    content_block_index = int(event.get("contentBlockIndex") or 0)
    delta = event.get("delta") or {}
    content_index = block_indices.get(content_block_index)
    block = output.content[content_index] if content_index is not None else None

    if delta.get("text") is not None:
        text_delta = str(delta.get("text") or "")
        if block is None:
            text_block = TextContent(text="")
            output.content.append(text_block)
            content_index = len(output.content) - 1
            block_indices[content_block_index] = content_index
            block = text_block
            stream.push(TextStartEvent(contentIndex=content_index, partial=output))

        if block.type == "text":
            block.text += text_delta
            stream.push(TextDeltaEvent(contentIndex=content_index, delta=text_delta, partial=output))
        return

    if delta.get("toolUse") and block is not None and block.type == "toolCall":
        tool_use = delta["toolUse"] or {}
        input_delta = str(tool_use.get("input") or "")
        accumulated = partial_json.setdefault(content_index, StreamingArgs())
        accumulated.append(input_delta)
        block.arguments = accumulated.arguments
        stream.push(ToolCallDeltaEvent(contentIndex=content_index, delta=input_delta, partial=output))
        return

    if delta.get("reasoningContent") is not None:
        reasoning_content = delta["reasoningContent"] or {}
        if block is None:
            thinking_block = ThinkingContent(thinking="", thinkingSignature="")
            output.content.append(thinking_block)
            content_index = len(output.content) - 1
            block_indices[content_block_index] = content_index
            block = thinking_block
            stream.push(ThinkingStartEvent(contentIndex=content_index, partial=output))

        if block.type == "thinking":
            text_delta = reasoning_content.get("text")
            if text_delta:
                block.thinking += str(text_delta)
                stream.push(ThinkingDeltaEvent(contentIndex=content_index, delta=str(text_delta), partial=output))
            # `thinkingSignature` holds either an Anthropic signature or an opaque
            # redacted payload, never both: mixing them corrupts whichever arrived first.
            if reasoning_content.get("signature") and not block.redacted:
                existing = block.thinkingSignature or ""
                block.thinkingSignature = existing + str(reasoning_content["signature"])
            redacted_content = reasoning_content.get("redactedContent")
            if redacted_content and redacted_chunks is not None:
                # Encrypted reasoning from a non-Anthropic model on Bedrock. The payload is
                # opaque, so it is kept verbatim the way the Anthropic path keeps redacted
                # thinking, and replayed on the next turn.
                if not block.redacted:
                    block.redacted = True
                    block.thinkingSignature = ""
                    block.thinking += REDACTED_THINKING_PLACEHOLDER
                    stream.push(
                        ThinkingDeltaEvent(
                            contentIndex=content_index,
                            delta=REDACTED_THINKING_PLACEHOLDER,
                            partial=output,
                        )
                    )
                redacted_chunks.setdefault(content_index, []).append(bytes(redacted_content))


def handle_metadata(event: dict[str, Any], model: Model, output: AssistantMessage) -> None:
    usage = event.get("usage") or {}
    if not usage:
        return

    output.usage.input = int(usage.get("inputTokens") or 0)
    output.usage.output = int(usage.get("outputTokens") or 0)
    output.usage.cacheRead = int(usage.get("cacheReadInputTokens") or 0)
    output.usage.cacheWrite = int(usage.get("cacheWriteInputTokens") or 0)
    output.usage.totalTokens = int(usage.get("totalTokens") or (output.usage.input + output.usage.output))
    calculate_cost(model, output.usage)


def handle_content_block_stop(
    event: dict[str, Any],
    block_indices: dict[int, int],
    partial_json: dict[int, StreamingArgs],
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
    redacted_chunks: dict[int, list[bytes]] | None = None,
) -> None:
    content_block_index = int(event.get("contentBlockIndex") or 0)
    content_index = block_indices.pop(content_block_index, None)
    if content_index is None:
        return

    block = output.content[content_index]
    if block.type == "text":
        stream.push(TextEndEvent(contentIndex=content_index, content=block.text, partial=output))
        return

    if block.type == "thinking":
        flush_redacted_content(output, content_index, redacted_chunks)
        stream.push(ThinkingEndEvent(contentIndex=content_index, content=block.thinking, partial=output))
        return

    if block.type == "toolCall":
        # Same guard as the anthropic adapter's content_block_stop (F4): a start block
        # that inlined the input and got no delta must keep it, not be handed the parse
        # of an empty buffer. Bedrock's contentBlockStart carries no input today, so this
        # only ever preserves ``{}`` -- but the shape has to match, not the luck.
        accumulated = partial_json.pop(content_index, None)
        if accumulated is not None and accumulated.raw:
            block.arguments = accumulated.finish()
        stream.push(ToolCallEndEvent(contentIndex=content_index, toolCall=block, partial=output))


def get_model_match_candidates(model_id: str, model_name: str | None = None) -> list[str]:
    values = [model_id, model_name] if model_name else [model_id]
    candidates: list[str] = []
    for value in values:
        if value is None:
            continue
        lowered = value.lower()
        candidates.append(lowered)
        candidates.append(_MATCH_NORMALIZATION_PATTERN.sub("-", lowered))
    return candidates


# Kept as literal substring lists, in upstream's order, so a new model is one line in one
# place. The catalog already ships Bedrock entries for opus-4-8 / opus-5 / sonnet-5 /
# fable-5; leaving them off these lists sent them a budget_tokens request instead of the
# adaptive/effort shape they expect.
_ADAPTIVE_THINKING_MARKERS = ("opus-4-6", "opus-4-7", "opus-4-8", "opus-5", "sonnet-4-6", "sonnet-5", "fable-5")
_NATIVE_XHIGH_MARKERS = ("opus-4-7", "opus-4-8", "opus-5", "sonnet-5", "fable-5")


def supports_adaptive_thinking(model_id: str, model_name: str | None = None) -> bool:
    candidates = get_model_match_candidates(model_id, model_name)
    return any(marker in value for value in candidates for marker in _ADAPTIVE_THINKING_MARKERS)


def supports_native_xhigh_effort(model: Model) -> bool:
    candidates = get_model_match_candidates(model.id, model.name)
    return any(marker in value for value in candidates for marker in _NATIVE_XHIGH_MARKERS)


def map_thinking_level_to_effort(
    model: Model,
    level: ThinkingLevel | None,
) -> Literal["low", "medium", "high", "xhigh", "max"]:
    if level == "xhigh" and supports_native_xhigh_effort(model):
        return "xhigh"

    mapped = model.thinkingLevelMap.get(level) if level and model.thinkingLevelMap is not None else None
    if isinstance(mapped, str):
        return mapped  # type: ignore[return-value]

    if level in {"minimal", "low"}:
        return "low"
    if level == "medium":
        return "medium"
    return "high"


def is_anthropic_claude_model(model: Model) -> bool:
    model_id = model.id.lower()
    model_name = (model.name or "").lower()
    return (
        "anthropic.claude" in model_id
        or "anthropic/claude" in model_id
        or "anthropic.claude" in model_name
        or "anthropic/claude" in model_name
        or "claude" in model_name
    )


def supports_prompt_caching(model: Model, env: Any = None) -> bool:
    candidates = get_model_match_candidates(model.id, model.name)
    has_claude_ref = any("claude" in value for value in candidates)
    if not has_claude_ref:
        # An application inference profile's ARN carries no model name, so nothing here
        # can tell a Claude behind one from a Nova. This is the manual override for it.
        return get_provider_env_value("AWS_BEDROCK_FORCE_CACHE", env) == "1"
    if any(("fable-5" in value or "opus-5" in value or "sonnet-5" in value) for value in candidates):
        return True
    if any("-4-" in value for value in candidates):
        return True
    if any("claude-3-7-sonnet" in value for value in candidates):
        return True
    return bool(any("claude-3-5-haiku" in value for value in candidates))


def supports_thinking_signature(model: Model) -> bool:
    return is_anthropic_claude_model(model)


def build_system_prompt(
    system_prompt: str | None,
    model: Model,
    cache_retention: CacheRetention,
    env: Any = None,
) -> list[dict[str, Any]] | None:
    if not system_prompt:
        return None

    blocks: list[dict[str, Any]] = [{"text": sanitize_surrogates(system_prompt)}]
    if cache_retention != "none" and supports_prompt_caching(model, env):
        cache_point: dict[str, Any] = {"type": "default"}
        if cache_retention == "long":
            cache_point["ttl"] = "1h"
        blocks.append({"cachePoint": cache_point})
    return blocks


def normalize_tool_call_id(tool_call_id: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", tool_call_id)
    return sanitized[:64] if len(sanitized) > 64 else sanitized


def create_non_blank_text_block(text: str) -> dict[str, Any] | None:
    """A Bedrock text block, or ``None`` when there is nothing left to send.

    Sanitising first is what makes this different from a bare ``strip()``: a string of
    nothing but lone surrogates is non-blank until the sanitiser removes them, and the
    empty block that survives is the one Bedrock rejects.
    """
    sanitized = sanitize_surrogates(text)
    return None if not sanitized.strip() else {"text": sanitized}


def create_required_text_block(text: str) -> dict[str, Any]:
    """The same, for a slot that must hold a block: blank becomes the placeholder."""
    return create_non_blank_text_block(text) or {"text": EMPTY_TEXT_PLACEHOLDER}


def sanitize_bedrock_document(value: Any) -> Any:
    """Drop empty-string keys, recursively. Bedrock's document type has no name for them.

    A model that emits ``{"": 1}`` in a tool call would otherwise fail the whole request
    with a validation error naming a field the user cannot see.
    """
    if isinstance(value, list):
        return [sanitize_bedrock_document(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize_bedrock_document(nested) for key, nested in value.items() if key != ""}
    return value


def convert_tool_result_content(content: list[Any]) -> list[dict[str, Any]]:
    """Blank text parts are dropped; a result that empties out keeps the placeholder."""
    result: list[dict[str, Any]] = []
    for part in content:
        if part.type == "image":
            result.append({"image": create_image_block(part.mimeType, part.data)})
            continue
        block = create_non_blank_text_block(part.text)
        if block is not None:
            result.append(block)
    if not result:
        result.append({"text": EMPTY_TEXT_PLACEHOLDER})
    return result


def decode_redacted_content(signature: str | None) -> bytes | None:
    """The stored opaque reasoning payload, or ``None`` if it is not replayable."""
    if not signature:
        return None
    try:
        return base64.b64decode(signature, validate=True)
    except Exception:  # noqa: BLE001
        return None


def convert_messages(
    context: Context,
    model: Model,
    cache_retention: CacheRetention,
    env: Any = None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    transformed_messages = transform_messages(context.messages, model, lambda tool_call_id, _target_model, _source: normalize_tool_call_id(tool_call_id))
    index = 0
    while index < len(transformed_messages):
        message = transformed_messages[index]

        if message.role == "user":
            content: list[dict[str, Any]] = []
            if isinstance(message.content, str):
                content.append(create_required_text_block(message.content))
            else:
                for item in message.content:
                    if item.type == "text":
                        block = create_non_blank_text_block(item.text)
                        if block is not None:
                            content.append(block)
                    elif item.type == "image":
                        content.append({"image": create_image_block(item.mimeType, item.data)})
                # A user turn is never dropped for being empty. Bedrock requires the roles
                # to alternate, so removing one leaves two assistant turns adjacent and
                # fails the whole request instead of the one blank message.
                if not content:
                    content.append({"text": EMPTY_TEXT_PLACEHOLDER})
            result.append({"role": "user", "content": content})
            index += 1
            continue

        if message.role == "assistant":
            if not message.content:
                index += 1
                continue

            content_blocks: list[dict[str, Any]] = []
            for block in message.content:
                if block.type == "text":
                    text_block = create_non_blank_text_block(block.text)
                    if text_block is not None:
                        content_blocks.append(text_block)
                    continue

                if block.type == "toolCall":
                    content_blocks.append(
                        {
                            "toolUse": {
                                "toolUseId": block.id,
                                "name": block.name,
                                "input": sanitize_bedrock_document(block.arguments),
                            }
                        }
                    )
                    continue

                if block.type == "thinking":
                    # Encrypted reasoning is opaque: replay the stored payload as the
                    # `redactedContent` member rather than lowering it to reasoning text,
                    # which is what the placeholder in `thinking` would become.
                    if block.redacted:
                        redacted_content = decode_redacted_content(block.thinkingSignature)
                        if redacted_content:
                            content_blocks.append({"reasoningContent": {"redactedContent": redacted_content}})
                        continue
                    if not sanitize_surrogates(block.thinking).strip():
                        continue
                    if supports_thinking_signature(model):
                        if not block.thinkingSignature or not block.thinkingSignature.strip():
                            content_blocks.append({"text": sanitize_surrogates(block.thinking)})
                        else:
                            content_blocks.append(
                                {
                                    "reasoningContent": {
                                        "reasoningText": {
                                            "text": sanitize_surrogates(block.thinking),
                                            "signature": block.thinkingSignature,
                                        }
                                    }
                                }
                            )
                    else:
                        content_blocks.append(
                            {
                                "reasoningContent": {
                                    "reasoningText": {"text": sanitize_surrogates(block.thinking)}
                                }
                            }
                        )
                    continue

            if content_blocks:
                result.append({"role": "assistant", "content": content_blocks})
            index += 1
            continue

        if message.role == "toolResult":
            tool_results: list[dict[str, Any]] = []
            while index < len(transformed_messages) and transformed_messages[index].role == "toolResult":
                tool_message = transformed_messages[index]
                assert isinstance(tool_message, ToolResultMessage)
                tool_results.append(
                    {
                        "toolResult": {
                            "toolUseId": tool_message.toolCallId,
                            "content": convert_tool_result_content(tool_message.content),
                            "status": "error" if tool_message.isError else "success",
                        }
                    }
                )
                index += 1

            result.append({"role": "user", "content": tool_results})
            continue

        index += 1

    if cache_retention != "none" and supports_prompt_caching(model, env) and result:
        last_message = result[-1]
        if last_message.get("role") == "user" and isinstance(last_message.get("content"), list):
            cache_point: dict[str, Any] = {"type": "default"}
            if cache_retention == "long":
                cache_point["ttl"] = "1h"
            last_message["content"].append({"cachePoint": cache_point})

    return result


def convert_tool_config(
    tools: list[Tool] | None,
    tool_choice: str | dict[str, Any] | None,
    supports_strict_mode: bool = False,
) -> dict[str, Any] | None:
    if not tools or tool_choice == "none":
        return None

    bedrock_tools: list[dict[str, Any]] = []
    for tool in tools:
        strict = resolve_json_schema_strict_sampling(tool, supports_strict_mode)
        bedrock_tools.append(
            {
                "toolSpec": {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": {"json": get_json_schema_tool_parameters(tool, strict)},
                    **({"strict": True} if strict is True else {}),
                }
            }
        )

    bedrock_tool_choice: dict[str, Any] | None = None
    if tool_choice == "auto":
        bedrock_tool_choice = {"auto": {}}
    elif tool_choice == "any":
        bedrock_tool_choice = {"any": {}}
    elif isinstance(tool_choice, dict) and tool_choice.get("type") == "tool":
        bedrock_tool_choice = {"tool": {"name": tool_choice.get("name")}}

    # Pi emits `toolChoice: undefined`, which the Smithy serializer drops; botocore's
    # parameter validation rejects an explicit None, so omit the key instead.
    return {"tools": bedrock_tools, **({"toolChoice": bedrock_tool_choice} if bedrock_tool_choice is not None else {})}


def map_stop_reason(reason: str | None) -> dict[str, str]:
    """Map Bedrock's ``messageStop.stopReason``, keeping the raw value on unknowns.

    Returns the same ``{"stopReason", "errorMessage"?}`` shape openai-completions'
    ``map_stop_reason`` does. A ``guardrail_intervened`` / ``content_filtered`` turn used
    to surface as a bare "An unknown error occurred"; the reason is what tells a user (and
    the retry layer) whether the turn is worth repeating.
    """
    if reason in {"end_turn", "stop_sequence"}:
        return {"stopReason": "stop"}
    if reason in {"max_tokens", "model_context_window_exceeded"}:
        return {"stopReason": "length"}
    if reason == "tool_use":
        return {"stopReason": "toolUse"}
    if reason:
        return {"stopReason": "error", "errorMessage": f"Provider stopped with: {reason}"}
    return {"stopReason": "error"}


def get_configured_bedrock_region(options: StreamOptions | dict[str, Any] | None = None) -> str | None:
    env = _option(options, "env")
    return (
        _option(options, "region")
        or get_provider_env_value("AWS_REGION", env)
        or get_provider_env_value("AWS_DEFAULT_REGION", env)
    )


def has_configured_bedrock_profile() -> bool:
    return bool(os.environ.get("AWS_PROFILE"))


def get_standard_bedrock_endpoint_region(base_url: str | None) -> str | None:
    if not base_url:
        return None
    try:
        hostname = urlparse(base_url).hostname or ""
    except Exception:  # noqa: BLE001
        return None
    match = _STANDARD_BEDROCK_ENDPOINT_PATTERN.match(hostname.lower())
    return match.group(1) if match else None


def should_use_explicit_bedrock_endpoint(
    base_url: str,
    configured_region: str | None,
    has_configured_profile: bool,
) -> bool:
    endpoint_region = get_standard_bedrock_endpoint_region(base_url)
    if endpoint_region is None:
        return True
    return not configured_region and not has_configured_profile


def is_govcloud_bedrock_target(model: Model, options: StreamOptions | dict[str, Any] | None = None) -> bool:
    region = get_configured_bedrock_region(options)
    if region and region.lower().startswith("us-gov-"):
        return True
    model_id = model.id.lower()
    return model_id.startswith(("us-gov.", "arn:aws-us-gov:"))


def build_additional_model_request_fields(
    model: Model,
    options: StreamOptions | dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    reasoning = _option(options, "reasoning")
    if not reasoning or not model.reasoning:
        return None

    if not is_anthropic_claude_model(model):
        return None

    display: BedrockThinkingDisplay | None = None if is_govcloud_bedrock_target(model, options) else _option(options, "thinkingDisplay", "summarized")

    if supports_adaptive_thinking(model.id, model.name):
        result: dict[str, Any] = {
            "thinking": {"type": "adaptive", **({"display": display} if display is not None else {})},
            "output_config": {"effort": map_thinking_level_to_effort(model, reasoning)},
        }
        return result

    default_budgets: dict[ThinkingLevel, int] = {
        "minimal": 1024,
        "low": 2048,
        "medium": 8192,
        "high": 16384,
        "xhigh": 16384,  # Budget-based Claude clamps extended levels to high
        "max": 16384,
    }
    thinking_budgets = _option(options, "thinkingBudgets")
    if isinstance(thinking_budgets, ThinkingBudgets):
        budget_map = thinking_budgets.model_dump()
    elif isinstance(thinking_budgets, dict):
        budget_map = dict(thinking_budgets)
    else:
        budget_map = {}

    # Custom budgets only cover token-based levels through high; clamp_reasoning is the
    # one place that fold lives (stream_simple_bedrock above takes the same route).
    budget = budget_map.get(clamp_reasoning(reasoning))
    if budget is None:
        budget = default_budgets[reasoning]
    result = {
        "thinking": {
            "type": "enabled",
            "budget_tokens": budget,
            **({"display": display} if display is not None else {}),
        }
    }
    if _option(options, "interleavedThinking", True):
        result["anthropic_beta"] = ["interleaved-thinking-2025-05-14"]
    return result


def create_image_block(mime_type: str, data: str) -> dict[str, Any]:
    if mime_type in {"image/jpeg", "image/jpg"}:
        image_format = "jpeg"
    elif mime_type == "image/png":
        image_format = "png"
    elif mime_type == "image/gif":
        image_format = "gif"
    elif mime_type == "image/webp":
        image_format = "webp"
    else:
        raise RuntimeError(f"Unknown image type: {mime_type}")

    return {
        "source": {"bytes": base64.b64decode(data)},
        "format": image_format,
    }


streamBedrock = stream_bedrock
streamSimpleBedrock = stream_simple_bedrock
__all__ = [
    "BedrockOptions",
    "BedrockThinkingDisplay",
    "streamBedrock",
    "streamSimpleBedrock",
    "stream_bedrock",
    "stream_simple_bedrock",
]

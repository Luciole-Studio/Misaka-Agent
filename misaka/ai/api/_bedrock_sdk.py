"""MISAKA fork: boto3 behind the ``BedrockRuntimeClient`` pi's Bedrock adapter talks to (no upstream file).

pi builds ``new BedrockRuntimeClient(config)`` from ``@aws-sdk/client-bedrock-runtime``, adds Smithy middleware and
calls ``client.send(new ConverseStreamCommand(input), {abortSignal})``. This class keeps that shape over boto3:

- ``config`` is pi's ``BedrockRuntimeClientConfig`` (``profile``, ``region``, ``endpoint``, ``credentials``,
  ``requestHandler``, ``token``, ``authSchemePreference``), turned into a boto3 session and client.
- A ``build`` middleware runs on botocore's ``before-sign`` event, which, like Smithy's build step, comes after
  serialization and before SigV4 signing; a ``deserialize`` middleware sees the HTTP status and headers botocore keeps
  on ``ResponseMetadata``.
- boto3 is synchronous: client construction (botocore reads its JSON service models off disk), the request and the
  event stream run off the loop, and every wait races the abort signal. An abort stops the wait; the boto3 call on its
  thread cannot be cancelled and runs out.
- botocore raises ``EventStreamError`` for every exception frame of a stream; ``_thrown_value`` turns it back into what
  the JavaScript SDK throws.

The SDK is an optional extra (``misaka[bedrock]``); only an adapter call loads this module.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

try:
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config
    from botocore.exceptions import ClientError, EventStreamError
except ImportError:  # optional extra: misaka[bedrock]
    boto3 = UNSIGNED = Config = None

    class ClientError(Exception):  # keeps `except ClientError` valid; never raised without boto3
        pass

    class EventStreamError(ClientError):
        pass


from misaka.ai.api._common import (
    _await_with_signal,
    _close_stream,
    _create_abort_wait_task,
)
from misaka.ai.api._sdk import require
from misaka.utils.values import maybe_await, signal_aborted

_STREAM_SENTINEL = object()


@dataclass
class _RawResponse:
    """The Smithy ``HttpResponse`` pi's deserialize middleware reads: ``statusCode`` and ``headers``."""

    statusCode: int | None
    headers: dict[str, str]


@dataclass
class ConverseStreamOutput:
    """``ConverseStreamCommandOutput``: the event ``stream`` and ``$metadata`` (``requestId``, ``httpStatusCode``)."""

    stream: AsyncIterator[dict[str, Any]]
    metadata: dict[str, Any]
    _raw: Any = field(default=None, repr=False)

    async def aclose(self) -> None:
        """MISAKA fork: release the botocore event stream (its HTTP connection) when the request ends."""
        await _close_stream(self._raw)


class MiddlewareStack:
    """Smithy's ``client.middlewareStack``. A ``build`` middleware is called with the request (it may edit the headers)
    on botocore's ``before-sign`` event; a ``deserialize`` middleware is called, and awaited, with the raw response.
    Unlike Smithy's, a middleware does not call ``next``: botocore runs the chain."""

    def __init__(self) -> None:
        self.entries: list[tuple[Callable[[Any], Any], dict[str, Any]]] = []

    def add(self, middleware: Callable[[Any], Any], options: dict[str, Any]) -> None:
        self.entries.append((middleware, options))

    def step(self, name: str) -> list[Callable[[Any], Any]]:
        return [middleware for middleware, options in self.entries if options.get("step") == name]


class BedrockRuntimeClient:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.middlewareStack = MiddlewareStack()

    async def send(self, commandInput: dict[str, Any], abortSignal: Any = None) -> ConverseStreamOutput:
        if signal_aborted(abortSignal):
            raise RuntimeError("Request was aborted")
        client = await asyncio.to_thread(_createBotoClient, self.config, self.middlewareStack.step("build"))
        # botocore validates the request against the service model and rejects a key set to None, where the JS SDK's
        # serializer drops an undefined one.
        request = {key: value for key, value in commandInput.items() if value is not None}
        response = await _await_with_signal(asyncio.to_thread(client.converse_stream, **request), abortSignal)
        responseMetadata = response.get("ResponseMetadata", {}) if isinstance(response, dict) else {}
        rawHeaders = responseMetadata.get("HTTPHeaders")
        raw = _RawResponse(
            statusCode=responseMetadata.get("HTTPStatusCode"),
            headers={str(key): str(value) for key, value in rawHeaders.items()} if isinstance(rawHeaders, dict) else {},
        )
        for middleware in self.middlewareStack.step("deserialize"):
            await maybe_await(middleware(raw))
        eventStream = response.get("stream") if isinstance(response, dict) else None
        return ConverseStreamOutput(
            stream=_iterateStreamEvents(eventStream, abortSignal),
            metadata={
                "requestId": responseMetadata.get("RequestId"),
                "httpStatusCode": responseMetadata.get("HTTPStatusCode"),
            },
            _raw=eventStream,
        )


def proxyRequestHandler(proxyUrl: str) -> dict[str, Any]:
    """``new NodeHttpHandler({httpAgent, httpsAgent})`` over the proxy: botocore's ``proxies`` config."""
    return {"proxies": {"http": proxyUrl, "https": proxyUrl}}


def _createBotoClient(config: dict[str, Any], buildMiddleware: list[Callable[[Any], None]]) -> Any:
    session = require(boto3, "boto3").Session(profile_name=config.get("profile"))
    clientKwargs: dict[str, Any] = {}
    if config.get("region") is not None:
        clientKwargs["region_name"] = config["region"]
    if config.get("endpoint") is not None:
        clientKwargs["endpoint_url"] = config["endpoint"]
    credentials = config.get("credentials")
    if credentials is not None:
        clientKwargs["aws_access_key_id"] = credentials["accessKeyId"]
        clientKwargs["aws_secret_access_key"] = credentials["secretAccessKey"]
        if credentials.get("sessionToken"):
            clientKwargs["aws_session_token"] = credentials["sessionToken"]
    configKwargs: dict[str, Any] = dict(config.get("requestHandler") or {})
    token = config.get("token")
    bearerToken = token["token"] if token and "httpBearerAuth" in (config.get("authSchemePreference") or []) else None
    if bearerToken:
        configKwargs["signature_version"] = UNSIGNED
    if configKwargs:
        clientKwargs["config"] = Config(**configKwargs)

    client = session.client("bedrock-runtime", **clientKwargs)

    if buildMiddleware or bearerToken:

        def beforeSign(request: Any, **_kwargs: Any) -> None:
            for middleware in buildMiddleware:
                middleware(request)
            if bearerToken:
                _set_header(request, "Authorization", f"Bearer {bearerToken}")

        # botocore emits `before-sign` even under UNSIGNED, so the bearer path still gets its header.
        client.meta.events.register("before-sign.bedrock-runtime.ConverseStream", beforeSign)
    return client


def _set_header(request: Any, key: str, value: str) -> None:
    """Replace a header: botocore's header container is multi-valued, so an assignment would add a second copy."""
    try:
        del request.headers[key]
    except (KeyError, TypeError):
        pass
    request.headers[key] = value


async def _iterateStreamEvents(eventStream: Any, signal: Any = None) -> AsyncIterator[dict[str, Any]]:
    """The botocore event stream's items, read on a worker thread and handed over through a queue."""
    if eventStream is None:
        return
    # One abort task for the whole stream, not one per event (see `_common._iterate_async_iterable`).
    abortTask = _create_abort_wait_task(signal)
    try:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()

        def worker() -> None:
            try:
                for event in eventStream:
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except Exception as error:  # noqa: BLE001 - any stream failure is delivered to the consumer as an item
                loop.call_soon_threadsafe(queue.put_nowait, error)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, _STREAM_SENTINEL)

        threading.Thread(target=worker, daemon=True).start()

        while True:
            item = await _await_with_signal(
                queue.get(), signal, on_abort=lambda: _close_stream(eventStream), abort_task=abortTask
            )
            if item is _STREAM_SENTINEL:
                return
            if isinstance(item, Exception):
                raise item
            # JavaScript's `for await` hands control to the consumer at every event, so it sees the partial as each
            # event left it; queued events would otherwise all be applied before it runs.
            await asyncio.sleep(0)
            yield item
    finally:
        if abortTask is not None:
            abortTask.cancel()


class _ThrownStreamValue(Exception):
    """A non-Error value upstream throws: a stream item's exception member, thrown as is."""

    def __init__(self, value: Any) -> None:
        super().__init__(str(value))
        self.value = value


# The exception members of ConverseStreamOutput: frames the JS SDK deserializes as modeled.
_MODELED_STREAM_EXCEPTIONS = frozenset(
    {
        "internalServerException",
        "modelStreamErrorException",
        "validationException",
        "throttlingException",
        "serviceUnavailableException",
    }
)


def _thrown_value(error: BaseException) -> Any:
    """What upstream's catch receives for ``error``: the JS SDK throws a modeled exception frame as a bare object
    literal and an unmodeled one as an ``Error`` named after its code; a stream item's exception member is thrown as
    is."""
    if isinstance(error, _ThrownStreamValue):
        return error.value
    if isinstance(error, EventStreamError):
        failure = error.response.get("Error", {})
        code = failure.get("Code") or ""
        message = failure.get("Message") or ""
        if code in _MODELED_STREAM_EXCEPTIONS:
            return {"message": message}
        unmodeled = RuntimeError(message or "UnknownError")
        unmodeled.name = code  # type: ignore[attr-defined]
        return unmodeled
    return error


def _service_exception(error: Any) -> tuple[str, str] | None:
    """``error instanceof BedrockRuntimeServiceException``: botocore's ``ClientError``, whose ``Error.Code`` and
    ``Error.Message`` are the JS error's ``name`` and ``message``."""
    if not isinstance(error, ClientError):
        return None
    failure = error.response.get("Error", {})
    return str(failure.get("Code", "ClientError")), str(failure.get("Message", str(error)))


def _error_name(error: Any) -> str | None:
    """``error.name``: an unmodeled stream error carries it, a client error carries the code in its response, and a
    modeled exception class is named after itself."""
    if not isinstance(error, BaseException):
        return None
    name = getattr(error, "name", None)
    if not isinstance(name, str) or not name:
        service = _service_exception(error)
        name = service[0] if service else None
    if not isinstance(name, str) or not name:
        name = type(error).__name__
    return name


def _sdk_error_metadata(error: Any) -> dict[str, Any]:
    """``error.$metadata``: botocore keeps the HTTP status and the request id on ``response["ResponseMetadata"]``."""
    response = getattr(error, "response", None)
    metadata = response.get("ResponseMetadata") if isinstance(response, dict) else None
    if not isinstance(metadata, dict):
        return {}
    return {"httpStatusCode": metadata.get("HTTPStatusCode"), "requestId": metadata.get("RequestId")}


__all__: list[str] = []

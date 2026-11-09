"""MISAKA fork: the google-genai glue behind pi's Google adapters (no upstream file).

pi's ``google-generative-ai.ts`` and ``google-vertex.ts`` talk to ``@google/genai``. The Python SDK differs in the
places these helpers cover, each called where the difference shows:

- Its enums come from here; without the optional extra (``misaka[google]``) str enums with the same members stand
  in, so ``google_shared`` builds requests without the SDK.
- Its response objects are pydantic models with snake_case attributes (``coalesce_attr``); ``Part.thought_signature``
  is ``bytes`` (``coerce_thought_signature``) and ``Candidate.finish_reason`` an enum (``raw_finish_reason``).
- It sends a streamed request when the stream is first read, not when it is opened (``open_google_stream``), and
  takes no abort signal (``open_google_stream``, ``iterate_google_stream``).
- Its client owns an httpx client to close (``close_client``), and its ``APIError`` message is a repr of the error
  body (``format_google_error``).
- Vertex: it takes loaded credentials where @google/genai takes ``googleAuthOptions`` (``load_google_credentials``),
  and reads the API version from the HTTP options alone (``with_api_version``).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import warnings
from collections.abc import AsyncIterator, Mapping
from enum import StrEnum
from typing import Any

try:
    from google.genai import errors as genai_errors
    from google.genai.types import (
        FinishReason,
        FunctionCallingConfigMode,
        ThinkingLevel,
    )
except ImportError:  # optional extra: misaka[google]
    genai_errors = None

    class FinishReason(StrEnum):  # type: ignore[no-redef]
        FINISH_REASON_UNSPECIFIED = "FINISH_REASON_UNSPECIFIED"
        STOP = "STOP"
        MAX_TOKENS = "MAX_TOKENS"
        SAFETY = "SAFETY"
        RECITATION = "RECITATION"
        LANGUAGE = "LANGUAGE"
        OTHER = "OTHER"
        BLOCKLIST = "BLOCKLIST"
        PROHIBITED_CONTENT = "PROHIBITED_CONTENT"
        SPII = "SPII"
        MALFORMED_FUNCTION_CALL = "MALFORMED_FUNCTION_CALL"
        IMAGE_SAFETY = "IMAGE_SAFETY"
        UNEXPECTED_TOOL_CALL = "UNEXPECTED_TOOL_CALL"
        TOO_MANY_TOOL_CALLS = "TOO_MANY_TOOL_CALLS"
        IMAGE_PROHIBITED_CONTENT = "IMAGE_PROHIBITED_CONTENT"
        NO_IMAGE = "NO_IMAGE"
        IMAGE_RECITATION = "IMAGE_RECITATION"
        IMAGE_OTHER = "IMAGE_OTHER"

    class FunctionCallingConfigMode(StrEnum):  # type: ignore[no-redef]
        MODE_UNSPECIFIED = "MODE_UNSPECIFIED"
        AUTO = "AUTO"
        ANY = "ANY"
        NONE = "NONE"
        VALIDATED = "VALIDATED"

    class ThinkingLevel(StrEnum):  # type: ignore[no-redef]
        THINKING_LEVEL_UNSPECIFIED = "THINKING_LEVEL_UNSPECIFIED"
        MINIMAL = "MINIMAL"
        LOW = "LOW"
        MEDIUM = "MEDIUM"
        HIGH = "HIGH"


from misaka.ai.api._common import _close_stream, _iterate_async_iterable
from misaka.ai.utils.error_body import formatProviderError, normalizeProviderError
from misaka.utils.values import maybe_await

# The scope google-auth-library applies for @google/genai by default; the Python SDK applies it only on its own ADC
# path, so a key file loaded here names it.
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def coalesce_attr(obj: Any, *names: str) -> Any:
    """``obj.<name>`` for the first of ``names`` that is set: the SDK's models spell their attributes in snake_case
    where @google/genai's objects use camelCase; a plain dict is read by key."""
    for name in names:
        value = obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name, None)
        if value is not None:
            return value
    return None


def coerce_thought_signature(value: object) -> str | None:
    """A ``thought_signature`` as pi's string: the SDK types ``Part.thought_signature`` as ``bytes``.

    Google's field is TYPE_BYTES, base64 on the wire, so base64 round-trips it losslessly and is the shape
    ``google_shared._isValidThoughtSignature`` asks for before a signature is echoed back.
    """
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(bytes(value)).decode("ascii")
    return value if isinstance(value, str) else None


def raw_finish_reason(finish_reason: Any) -> str:
    """The wire string of a finish reason: the SDK hands back its ``FinishReason`` enum."""
    return str(getattr(finish_reason, "value", finish_reason))


def _prepare_sdk_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """The SDK call's keyword arguments: it takes no ``abortSignal``, and a ``None`` is pi's ``undefined``."""
    sdk_params = dict(params)
    config = sdk_params.get("config")
    if isinstance(config, Mapping):
        sdk_params["config"] = {
            key: value for key, value in config.items() if key != "abortSignal" and value is not None
        }
    return sdk_params


async def open_google_stream(client: Any, params: Mapping[str, Any]) -> AsyncIterator[Any]:
    """``client.models.generateContentStream(params)``.

    @google/genai sends the request and throws an HTTP error before it returns the stream; the Python SDK sends it
    only when the stream is first read. The first chunk is pulled here, inside the retried call, and put back in
    front of the rest, so an HTTP failure is retried and arrives before the start event.
    """
    google_stream = await client.aio.models.generate_content_stream(**_prepare_sdk_params(params))
    iterator = google_stream.__aiter__()
    try:
        first = await iterator.__anext__()
    except StopAsyncIteration:
        return _prepend_chunks((), iterator)
    return _prepend_chunks((first,), iterator)


async def _prepend_chunks(chunks: tuple[Any, ...], iterator: Any) -> AsyncIterator[Any]:
    try:
        for chunk in chunks:
            yield chunk
        async for chunk in iterator:
            yield chunk
    finally:
        close = getattr(iterator, "aclose", None)
        if callable(close):
            await close()


async def iterate_google_stream(google_stream: Any, signal: Any = None) -> AsyncIterator[Any]:
    """``for await (const chunk of googleStream)``, minding the abort signal @google/genai would have been handed.

    Each read races the signal, and the stream is closed when the loop stops, as a ``for await`` returns its source.
    After each chunk the loop turns once, as a JavaScript ``for await`` hands control to the consumer before the next
    chunk is read.
    """
    try:
        async with contextlib.aclosing(_iterate_async_iterable(google_stream, signal)) as chunks:
            async for chunk in chunks:
                yield chunk
                await asyncio.sleep(0)
    finally:
        await _close_stream(google_stream)


async def close_client(client: Any) -> None:
    """``client.aio.aclose()``: the SDK client owns an httpx client, closed while its loop still runs. Best-effort,
    the stream has already ended."""
    aio_client = getattr(client, "aio", None)
    close = getattr(aio_client, "aclose", None) if aio_client is not None else None
    if callable(close):
        try:
            await maybe_await(close())
        except Exception:  # noqa: BLE001, S110 - closing the SDK client is best-effort after the stream ended
            pass


def format_google_error(error: Any) -> str:
    """``formatProviderError(normalizeProviderError(error))``.

    @google/genai's ApiError message is ``JSON.stringify`` of the error body. The Python SDK's APIError keeps that
    body as ``details`` and makes its message a repr of it, so the upstream text is rebuilt from ``details``.
    """
    if genai_errors is not None and isinstance(error, genai_errors.APIError) and error.details is not None:
        return json.dumps(error.details, separators=(",", ":"), ensure_ascii=False)
    return formatProviderError(normalizeProviderError(error))


def load_google_credentials(googleAuthOptions: Mapping[str, str]) -> Any:
    """The credentials ``googleAuthOptions.keyFilename`` names, loaded. The loader's deprecation warning is silenced
    the way google-auth silences it for ``GOOGLE_APPLICATION_CREDENTIALS``."""
    import google.auth

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        credentials, _project_id = google.auth.load_credentials_from_file(
            googleAuthOptions["keyFilename"], scopes=[CLOUD_PLATFORM_SCOPE]
        )
    return credentials


def with_api_version(httpOptions: Mapping[str, Any] | None, apiVersion: str) -> dict[str, Any]:
    """@google/genai takes ``apiVersion`` beside ``httpOptions`` and lets ``httpOptions.apiVersion`` override it; the
    Python SDK reads it from the HTTP options alone, so the default goes in there unless they already set one."""
    return {"apiVersion": apiVersion, **(httpOptions or {})}


__all__ = [
    "CLOUD_PLATFORM_SCOPE",
    "FinishReason",
    "FunctionCallingConfigMode",
    "ThinkingLevel",
    "close_client",
    "coalesce_attr",
    "coerce_thought_signature",
    "format_google_error",
    "iterate_google_stream",
    "load_google_credentials",
    "open_google_stream",
    "raw_finish_reason",
    "with_api_version",
]

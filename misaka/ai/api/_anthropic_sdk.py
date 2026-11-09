"""MISAKA fork: the Python Anthropic SDK glue behind ``misaka.ai.api.anthropic_messages`` (no upstream file).

pi builds ``PiAnthropic`` (a subclass that switches the SDK's credential chain off) and calls
``client.beta.messages.create(params, requestOptions).asResponse()``. The Python SDK differs in three ways handled
here, so the adapter itself reads like the upstream file:

- ``AsyncAnthropic`` reads ``ANTHROPIC_API_KEY``/``ANTHROPIC_AUTH_TOKEN`` (and credential files) when neither
  ``api_key`` nor ``auth_token`` is given, where pi's ``apiKey: null`` means none. Header-owned auth therefore passes
  a placeholder key and suppresses the header it would mint. A subclass would also stop ``with_options()`` sharing
  the federation token cache, which the SDK grants to the base class alone.
- A ``None`` header is pi's "do not send this header"; the SDK takes ``omit`` for that, and only per request.
- ``create()`` accepts only the fields it declares, so the rest travel in ``extra_body``.

The SDK is an optional extra: it is imported here, and only an adapter call loads this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

try:
    from anthropic import APIStatusError, AsyncAnthropic, DefaultAsyncHttpxClient, omit
except ImportError:  # optional extra: misaka[anthropic]
    APIStatusError = AsyncAnthropic = DefaultAsyncHttpxClient = omit = None

from misaka.ai.api._common import _await_maybe_with_signal, split_sdk_params
from misaka.ai.api._sdk import require

# The spelling the SDK uses for the headers it sets itself. A caller's header is renamed to
# it, so an override or an omit replaces the SDK's entry instead of sitting beside it (a dict
# keeps `User-Agent` and `user-agent` as two headers, where a JS `Headers` merges them).
_SDK_HEADER_NAMES = {
    name.lower(): name
    for name in (
        "Accept",
        "Content-Type",
        "User-Agent",
        "X-Api-Key",
        "Authorization",
        "anthropic-beta",
        "anthropic-version",
    )
}


def _split_sdk_headers(headers: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The client's default headers, and the per-request headers that carry every suppression."""
    default_headers: dict[str, Any] = {}
    request_headers: dict[str, Any] = {}
    for name, value in headers.items():
        sdk_name = _SDK_HEADER_NAMES.get(name.lower(), name)
        if value is None:
            request_headers[sdk_name] = omit
        else:
            default_headers[sdk_name] = value
    return default_headers, request_headers


def _sdk_client(
    *, api_key: str | None, auth_token: str | None, base_url: str, headers: Mapping[str, Any]
) -> tuple[Any, dict[str, Any]]:
    """``new PiAnthropic({apiKey, authToken, baseURL, defaultHeaders})``, and the per-request headers that carry
    every suppression (the SDK's request-time auth check needs an ``X-Api-Key`` or ``Authorization`` header, or one
    of them omitted per request)."""
    default_headers, request_headers = _split_sdk_headers(headers)
    if api_key is None and auth_token is None:
        api_key = "header-auth-placeholder"
        request_headers["X-Api-Key"] = omit
    client = require(AsyncAnthropic, "anthropic")(
        api_key=api_key,
        auth_token=auth_token,
        base_url=base_url,
        default_headers=default_headers,
    )
    return client, request_headers


def _federation_sdk_client(*, base_url: str, federation: Any) -> Any:
    """``new PiAnthropic({apiKey: null, authToken: null, config: federation, baseURL})``."""
    return require(AsyncAnthropic, "anthropic")(
        api_key=None,
        auth_token=None,
        config=federation,
        base_url=base_url,
    )


def _with_options(client: Any, headers: Mapping[str, Any]) -> tuple[Any, dict[str, Any]]:
    """``client.withOptions({defaultHeaders})``. The Python SDK's clone would also share the connection pool,
    which belongs to the event loop that opened it (``platform.session.run_coro`` runs one-shot loops), so each
    request's clone gets a pool of its own, closed with the request; the credentials and their token cache stay
    shared."""
    sdk_headers, request_headers = _split_sdk_headers(headers)
    return client.with_options(default_headers=sdk_headers, http_client=DefaultAsyncHttpxClient()), request_headers


async def _create_raw_response(
    client: Any, params: Mapping[str, Any], requestOptions: Mapping[str, Any], request_headers: Mapping[str, Any]
) -> Any:
    """``client.beta.messages.create(params, requestOptions).asResponse()``: the streaming HTTP response.

    ``requestOptions`` is upstream's ``{signal?, timeout?, maxRetries: 0}``, with ``timeout`` in milliseconds.
    """
    signal = requestOptions.get("signal")
    sdk_options: dict[str, Any] = {"max_retries": requestOptions.get("maxRetries", 0)}
    if requestOptions.get("timeout") is not None:
        sdk_options["timeout"] = requestOptions["timeout"] / 1000
    request_client = client.with_options(**sdk_options) if hasattr(client, "with_options") else client
    messages = request_client.beta.messages
    call_options: dict[str, Any] = {"extra_headers": dict(request_headers)} if request_headers else {}
    sdk_params = split_sdk_params(messages.create, params)
    raw = await _await_maybe_with_signal(messages.with_raw_response.create(**sdk_params, **call_options), signal)
    return raw.http_response


def _is_api_status_error(error: BaseException) -> bool:
    """``error instanceof Anthropic.APIError`` for an error that carries an HTTP status: the Python SDK raises its
    ``APIStatusError`` subclass for those, with the status as ``status_code`` and the body's ``error.type`` as
    ``type``."""
    return APIStatusError is not None and isinstance(error, APIStatusError)


async def _close_client(client: Any, *, federation: bool) -> None:
    """Close what the request opened while its event loop still runs. Left to the garbage collector, httpx closes
    the pool from its finaliser after a one-shot loop is gone, and asyncio prints "Event loop is closed". A
    federation clone shares the SDK's credentials (and their exchange client) with the cached client, so only the
    pool this request opened is closed."""
    try:
        if federation:
            await client._client.aclose()
        else:
            await client.close()
    except Exception:  # noqa: BLE001, S110 - closing is best-effort; the stream has already ended
        pass


def _invalidate_federation_token(client: Any) -> None:
    """The SDK drops a federated token on a 401 only from its own retry hook, which ``maxRetries: 0`` never
    reaches; dropped here so the next request exchanges a fresh token instead of resending the rejected one."""
    token_cache = getattr(client, "_token_cache", None)
    if token_cache is not None:
        token_cache.invalidate()


__all__: list[str] = []

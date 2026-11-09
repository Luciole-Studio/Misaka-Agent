"""MISAKA fork: where Python provider SDKs keep an error's HTTP status and response headers.

pi's SDK errors carry ``statusCode``/``status``/``$metadata``/``$response`` (``utils/error-body.ts``) and pi's
``ProviderError`` declares ``status`` and ``headers`` on the error (``utils/provider-retry.ts``). Python's SDKs do not:
openai and anthropic keep ``status_code`` and the headers on ``response``, botocore keeps a dict response, and
google-genai a numeric ``code``. ``utils.error_body`` and ``utils.provider_retry`` read the status and headers through
these two probes, so both recognise the same objects.
"""

from __future__ import annotations

from typing import Any


def provider_error_status(error: Any) -> int | None:
    """First numeric hit wins: ``status_code``/``status``/``statusCode``, then ``response.status_code``, then
    botocore's ``response["ResponseMetadata"]["HTTPStatusCode"]``, then google-genai's ``code`` when it is an HTTP
    status (provider-specific codes such as 1210 are not)."""
    for name in ("status_code", "status", "statusCode"):
        value = getattr(error, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value

    response = getattr(error, "response", None)
    if response is not None:
        value = getattr(response, "status_code", None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        # botocore keeps it in a dict rather than on an object.
        if isinstance(response, dict):
            metadata = response.get("ResponseMetadata")
            if isinstance(metadata, dict):
                value = metadata.get("HTTPStatusCode")
                if isinstance(value, int) and not isinstance(value, bool):
                    return value
    value = getattr(error, "code", None)
    if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
        return value
    return None


def provider_error_headers(error: Any) -> Any:
    """The response headers an SDK error carries: on the error itself (pi's ``ProviderError``), else on
    ``error.response`` (openai, anthropic)."""
    headers = getattr(error, "headers", None)
    if headers is not None:
        return headers
    response = getattr(error, "response", None)
    return getattr(response, "headers", None) if response is not None else None


__all__ = ["provider_error_headers", "provider_error_status"]

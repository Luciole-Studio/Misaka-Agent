"""Translated from pi's ``packages/ai/test/error-body.test.ts`` (v1.1.0).

Unit tests for the shared provider error-body normalizer: one synthesized error object per SDK shape, plus the
non-Error fallback, truncation, the empty parsed-body edge case, and the formatProviderError compose helper.
MISAKA fork: the shapes are the Python SDKs' where they differ (botocore keeps a dict ``response``; a streaming
response is anything with ``read`` or ``pipe``).
"""

from __future__ import annotations

from typing import Any, ClassVar

from misaka.ai.utils.error_body import (
    MAX_PROVIDER_ERROR_BODY_CHARS,
    formatProviderError,
    normalizeProviderError,
)
from misaka.utils.js import jsonStringify


def _error(message: str, **fields: Any) -> Exception:
    """``Object.assign(new Error(message), fields)``."""
    error = Exception(message)
    for name, value in fields.items():
        setattr(error, name, value)
    return error


def test_extracts_status_and_body_from_a_mistral_shaped_error():
    error = _error("Mistral request failed", statusCode=403, body='{"error":"blocked by gateway WAF"}')

    norm = normalizeProviderError(error)

    assert norm.status == 403
    assert norm.body == '{"error":"blocked by gateway WAF"}'
    assert norm.messageCarriesBody is False


def test_reads_the_parsed_body_off_an_openai_api_error_when_the_message_is_opaque():
    # makeMessage(status, error, message) yields "<status> status code (no body)"
    # when the parsed body is unparsed, while the body stays on error.error.
    error = _error("403 status code (no body)", status=403, error={"error": "blocked by gateway WAF"})

    norm = normalizeProviderError(error)

    assert norm.status == 403
    assert norm.body == '{"error":"blocked by gateway WAF"}'
    assert norm.messageCarriesBody is False


def test_preserves_the_message_when_google_genai_already_folds_the_body_into_it():
    body = {"error": {"code": 403, "message": "Permission denied"}}
    error = _error(jsonStringify(body), status=403)

    norm = normalizeProviderError(error)

    assert norm.status == 403
    assert norm.messageCarriesBody is True
    assert norm.message == jsonStringify(body)


def test_extracts_status_and_body_from_a_bedrock_shaped_service_exception():
    # MISAKA fork: botocore's ClientError keeps a dict response with the parsed error and the metadata.
    error = _error(
        "UnknownError",
        response={"Error": {"message": "blocked by gateway WAF"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
    )

    norm = normalizeProviderError(error)

    assert norm.status == 403
    assert norm.body == '{"message":"blocked by gateway WAF"}'
    assert norm.messageCarriesBody is False


def test_ignores_a_bedrock_response_stream_instead_of_serializing_its_internals():
    class _ResponseStream:
        status_code = 400
        _events: ClassVar[dict[str, list[None]]] = {"close": [None, None]}

        def pipe(self) -> None:
            return None

    error = _error(
        "Invocation of model ID anthropic.claude-opus-5 with on-demand throughput isn't supported.",
        response=_ResponseStream(),
    )

    norm = normalizeProviderError(error)

    assert norm.status == 400
    assert norm.body is None
    assert "on-demand throughput isn't supported" in norm.message
    assert norm.messageCarriesBody is True


def test_ignores_a_class_instance_response_body_without_a_pipe_method_instead_of_serializing_it():
    # Not every SDK response wrapper is a node stream: web ReadableStreams
    # and SDK-specific wrapper classes have no `pipe`, but serializing them
    # still yields internals-noise that would replace the real message.
    class SdkHttpResponseBody:
        def __init__(self) -> None:
            self.locked = False
            self.state = {"storedError": None}

    error = _error("Input is too long for requested model.", status=400, body=SdkHttpResponseBody())

    norm = normalizeProviderError(error)

    assert norm.status == 400
    assert norm.body is None
    assert "Input is too long" in norm.message
    assert norm.messageCarriesBody is True


def test_ignores_a_class_instance_error_field_instead_of_serializing_it():
    class SdkInnerError:
        def __init__(self) -> None:
            self.code = "EPROTO"
            self.internalState: dict[str, Any] = {}

    error = _error("TLS handshake failed", status=502, error=SdkInnerError())

    norm = normalizeProviderError(error)

    assert norm.body is None
    assert norm.message == "TLS handshake failed"
    assert norm.messageCarriesBody is True


def test_still_surfaces_a_plain_parsed_json_body_object():
    error = _error(
        "400 status code (no body)", status=400, error={"message": "schema validation failed", "field": "tools[0]"}
    )

    norm = normalizeProviderError(error)

    assert norm.body == '{"message":"schema validation failed","field":"tools[0]"}'
    assert norm.messageCarriesBody is False


def test_json_stringifies_a_non_error_thrown_value():
    norm = normalizeProviderError({"reason": "boom"})

    assert norm.status is None
    assert norm.body is None
    assert norm.message == '{"reason":"boom"}'
    assert norm.messageCarriesBody is False


def test_treats_an_empty_parsed_body_object_as_no_body():
    error = _error("403 status code (no body)", status=403, error={})

    norm = normalizeProviderError(error)

    assert norm.body is None
    assert norm.messageCarriesBody is True


def test_truncates_the_body_at_the_cap():
    longBody = "x" * (MAX_PROVIDER_ERROR_BODY_CHARS + 50)
    error = _error("failed", statusCode=500, body=longBody)

    norm = normalizeProviderError(error)

    assert norm.body is not None
    assert "... [truncated 50 chars]" in norm.body
    assert len(norm.body) < len(longBody)


def test_sets_message_carries_body_when_the_message_already_contains_the_extracted_body():
    error = _error("500: upstream exploded", statusCode=500, body="upstream exploded")

    norm = normalizeProviderError(error)

    assert norm.messageCarriesBody is True


def test_format_surfaces_status_and_body_without_a_prefix():
    norm = normalizeProviderError(
        _error("403 status code (no body)", status=403, error={"error": "blocked by gateway WAF"})
    )

    formatted = formatProviderError(norm)

    assert "403" in formatted
    assert "blocked by gateway WAF" in formatted
    assert formatted != "403 status code (no body)"


def test_format_applies_a_provider_prefix_with_status_and_body():
    norm = normalizeProviderError(
        _error("403 status code (no body)", status=403, error={"error": "blocked by gateway WAF"})
    )

    assert formatProviderError(norm, "OpenAI API error") == 'OpenAI API error (403): {"error":"blocked by gateway WAF"}'


def test_format_preserves_the_message_with_prefix_and_status_when_it_already_carries_the_body():
    body = jsonStringify({"error": {"message": "Permission denied"}})
    norm = normalizeProviderError(_error(body, status=403))

    assert formatProviderError(norm, "OpenAI API error") == f"OpenAI API error (403): {body}"


def test_format_returns_the_bare_message_for_a_non_error_value():
    norm = normalizeProviderError({"reason": "boom"})

    assert formatProviderError(norm) == '{"reason":"boom"}'

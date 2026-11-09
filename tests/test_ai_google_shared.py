"""Translated from pi's ``packages/ai/test/{google-shared-convert-tools,google-thinking-signature,google-shared-retry,
google-shared-gemini3-unsigned-tool-call,google-shared-image-tool-result-routing,google-shared-signed-empty-blocks}
.test.ts`` (v1.1.0)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from misaka.ai.api.google_shared import (
    convertMessages,
    convertTools,
    isThinkingPart,
    requiresToolCallId,
    resolveGoogleFunctionCallingMode,
    retainThoughtSignature,
    retryGoogleRequest,
    supportsGoogleStrictToolSampling,
)
from misaka.ai.types import Context, Model, Tool
from misaka.ai.utils.transcript import normalizeContext
from misaka.utils.js import jsonStringify


def _now() -> int:
    return time.time_ns() // 1_000_000


def _usage() -> dict[str, Any]:
    return {
        "input": 0,
        "output": 0,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": 0,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
    }


def _parts(contents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [part for content in contents for part in content.get("parts") or []]


# --- google-shared-convert-tools.test.ts --------------------------------------------------------


def makeTool(parameters: dict[str, Any]) -> Tool:
    return Tool(name="test_tool", description="A test tool", parameters=parameters)


def test_strips_json_schema_meta_keys_from_parameters_when_use_parameters_true():
    tools = [
        makeTool(
            {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "$id": "urn:bash-tool",
                "$comment": "A bash tool for demonstration",
                "$defs": {
                    "commandDef": {"type": "string"},
                },
                "definitions": {
                    "legacyDef": {"type": "number"},
                },
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                },
                "required": ["command"],
            }
        ),
    ]

    result = convertTools(tools, True)
    decl = result[0]["functionDeclarations"][0]

    assert decl["parameters"] == {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
        },
        "required": ["command"],
    }
    for key in ("$schema", "$id", "$comment", "$defs", "definitions"):
        assert key not in decl["parameters"]


def test_recursively_strips_nested_json_schema_meta_keys():
    tools = [
        makeTool(
            {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "type": "object",
                "properties": {
                    "deep": {
                        "$schema": "http://json-schema.org/draft-07/schema#",
                        "$id": "urn:nested",
                        "type": "string",
                    },
                },
            }
        ),
    ]

    result = convertTools(tools, True)
    decl = result[0]["functionDeclarations"][0]

    assert decl["parameters"] == {
        "type": "object",
        "properties": {
            "deep": {
                "type": "string",
            },
        },
    }


def test_preserves_ref_while_stripping_meta_keys():
    tools = [
        makeTool(
            {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "type": "object",
                "properties": {
                    "refProp": {
                        "$ref": "#/$defs/someDef",
                        "type": "string",
                    },
                },
            }
        ),
    ]

    result = convertTools(tools, True)
    decl = result[0]["functionDeclarations"][0]

    assert decl["parameters"] == {
        "type": "object",
        "properties": {
            "refProp": {
                "$ref": "#/$defs/someDef",
                "type": "string",
            },
        },
    }


def test_does_not_mutate_the_original_tool_parameters_object():
    originalParameters = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {
            "command": {"type": "string"},
        },
        "required": ["command"],
    }
    tools = [makeTool(originalParameters)]

    convertTools(tools, True)

    assert originalParameters == {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {
            "command": {"type": "string"},
        },
        "required": ["command"],
    }


def test_preserves_schema_in_parameters_json_schema_when_use_parameters_false():
    tools = [
        makeTool(
            {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                },
                "required": ["command"],
            }
        ),
    ]

    result = convertTools(tools, False)
    decl = result[0]["functionDeclarations"][0]

    assert decl["parametersJsonSchema"] == {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {
            "command": {"type": "string"},
        },
        "required": ["command"],
    }


def test_handles_tools_without_schema_gracefully():
    tools = [
        makeTool(
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
                "required": ["path"],
            }
        ),
    ]

    result = convertTools(tools, True)
    decl = result[0]["functionDeclarations"][0]

    assert decl["parameters"] == {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
        },
        "required": ["path"],
    }


def test_uses_validated_function_calling_for_strict_tools_on_gemini_3():
    tool = Tool(
        name="test_tool",
        description="A test tool",
        parameters={"type": "object", "properties": {}},
        constrainedSampling={"type": "json_schema", "strict": "require"},
    )

    assert supportsGoogleStrictToolSampling("gemini-3.1-pro-preview") is True
    assert supportsGoogleStrictToolSampling("gemini-2.5-pro") is False
    assert resolveGoogleFunctionCallingMode([tool], None, True) == "VALIDATED"
    with pytest.raises(Exception, match='Tool "test_tool" requires JSON-schema constrained sampling'):
        resolveGoogleFunctionCallingMode([tool], None, False)


def test_returns_undefined_for_empty_tool_list():
    assert convertTools([]) is None
    assert convertTools([], True) is None


# --- google-thinking-signature.test.ts ----------------------------------------------------------


def test_treats_part_thought_true_as_thinking():
    assert isThinkingPart({"thought": True, "thoughtSignature": None}) is True
    assert isThinkingPart({"thought": True, "thoughtSignature": "opaque-signature"}) is True


def test_does_not_treat_thought_signature_alone_as_thinking():
    # Per Google docs, thoughtSignature is for context replay and can appear on any part type.
    # Only thought === true indicates thinking content.
    # See: https://ai.google.dev/gemini-api/docs/thought-signatures
    assert isThinkingPart({"thought": None, "thoughtSignature": "opaque-signature"}) is False
    assert isThinkingPart({"thought": False, "thoughtSignature": "opaque-signature"}) is False


def test_does_not_treat_empty_or_missing_signatures_as_thinking_if_thought_is_not_set():
    assert isThinkingPart({"thought": None, "thoughtSignature": None}) is False
    assert isThinkingPart({"thought": False, "thoughtSignature": ""}) is False


def test_preserves_the_existing_signature_when_subsequent_deltas_omit_thought_signature():
    first = retainThoughtSignature(None, "sig-1")
    assert first == "sig-1"

    second = retainThoughtSignature(first, None)
    assert second == "sig-1"

    third = retainThoughtSignature(second, "")
    assert third == "sig-1"


def test_updates_the_signature_when_a_new_non_empty_signature_arrives():
    updated = retainThoughtSignature("sig-1", "sig-2")
    assert updated == "sig-2"


# --- google-shared-retry.test.ts ----------------------------------------------------------------


class _GoogleApiError(Exception):
    """Shaped like @google/genai's ApiError: has `status`, but no `headers`."""

    def __init__(self, status: int) -> None:
        super().__init__(f"got status: {status}")
        self.status = status


def _request(*outcomes: Any) -> Any:
    calls: list[int] = []

    async def request() -> Any:
        calls.append(1)
        outcome = outcomes[min(len(calls), len(outcomes)) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    request.calls = calls  # type: ignore[attr-defined]
    return request


async def test_retries_a_headers_less_sdk_error_with_a_retryable_status(monkeypatch):
    from misaka.ai.utils import provider_retry

    # vi.useFakeTimers(): the backoff sleep resolves at once.
    async def sleep(_ms: float, _signal: Any = None) -> None:
        return None

    monkeypatch.setattr(provider_retry, "_abortableSleep", sleep)
    request = _request(_GoogleApiError(429), "ok")

    result = await retryGoogleRequest(request, {"maxRetries": 1})

    assert result == "ok"
    assert len(request.calls) == 2


async def test_does_not_retry_when_max_retries_is_unset():
    error = _GoogleApiError(429)
    request = _request(error)

    with pytest.raises(_GoogleApiError) as raised:
        await retryGoogleRequest(request)
    assert raised.value is error
    assert len(request.calls) == 1


async def test_does_not_retry_a_non_retryable_status():
    error = _GoogleApiError(400)
    request = _request(error)

    with pytest.raises(_GoogleApiError) as raised:
        await retryGoogleRequest(request, {"maxRetries": 2})
    assert raised.value is error
    assert len(request.calls) == 1


# --- google-shared-gemini3-unsigned-tool-call.test.ts -------------------------------------------


def makeGemini3Model(api: str, provider: str, id: str = "gemini-3-pro-preview") -> Model:
    return Model.model_validate(
        {
            "id": id,
            "name": "Gemini 3 Pro Preview",
            "api": api,
            "provider": provider,
            "baseUrl": "https://example.com",
            "reasoning": True,
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 128000,
            "maxTokens": 8192,
        }
    )


def makeUnsignedContext(model: Any, thoughtSignature: str | None = None) -> Context:
    now = _now()
    return Context.model_validate(
        {
            "messages": [
                {"role": "user", "content": "Hi", "timestamp": now},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "call_1",
                            "name": "bash",
                            "arguments": {"command": "echo hi"},
                            **({"thoughtSignature": thoughtSignature} if thoughtSignature else {}),
                        },
                        {
                            "type": "toolCall",
                            "id": "call_2",
                            "name": "bash",
                            "arguments": {"command": "ls -la"},
                        },
                    ],
                    "api": model["api"],
                    "provider": model["provider"],
                    "model": model["id"],
                    "usage": _usage(),
                    "stopReason": "toolUse",
                    "timestamp": now,
                },
                {
                    "role": "toolResult",
                    "toolCallId": "call_1",
                    "toolName": "bash",
                    "content": [{"type": "text", "text": "hi"}],
                    "isError": False,
                    "timestamp": now,
                },
                {
                    "role": "toolResult",
                    "toolCallId": "call_2",
                    "toolName": "bash",
                    "content": [{"type": "text", "text": "files"}],
                    "isError": False,
                    "timestamp": now,
                },
            ],
        }
    )


def _identity(model: Model, **overrides: Any) -> dict[str, Any]:
    return {"api": model.api, "provider": model.provider, "id": model.id, **overrides}


@pytest.mark.parametrize(
    "model",
    [
        makeGemini3Model("google-generative-ai", "google"),
        makeGemini3Model("google-generative-ai", "google", "gemini-3.6-flash"),
        makeGemini3Model("google-vertex", "google-vertex"),
    ],
    ids=lambda model: f"{model.id} via {model.api}",
)
def test_preserves_tool_call_ids_via_history(model):
    context = makeUnsignedContext(_identity(model))
    contents = convertMessages(model, normalizeContext(context))
    functionCallIds = [part["functionCall"]["id"] for part in _parts(contents) if (part.get("functionCall") or {}).get("id")]
    functionResponseIds = [
        part["functionResponse"]["id"] for part in _parts(contents) if (part.get("functionResponse") or {}).get("id")
    ]

    assert functionCallIds == ["call_1", "call_2"]
    assert functionResponseIds == ["call_1", "call_2"]


def test_does_not_add_skip_thought_signature_validator_for_unsigned_google_gen_ai_tool_calls():
    model = makeGemini3Model("google-generative-ai", "google")
    contents = convertMessages(model, normalizeContext(makeUnsignedContext(_identity(model, id="other-model"))))

    modelTurn = next((c for c in contents if c["role"] == "model"), None)
    assert modelTurn

    functionCallParts = [p for p in modelTurn["parts"] if "functionCall" in p]
    assert len(functionCallParts) == 2
    assert "thoughtSignature" not in functionCallParts[0]
    assert "thoughtSignature" not in functionCallParts[1]
    assert "skip_thought_signature_validator" not in jsonStringify(modelTurn)

    textParts = [p for p in modelTurn["parts"] if "text" in p]
    historicalText = [p for p in textParts if "Historical context" in p["text"]]
    assert len(historicalText) == 0


def test_does_not_add_skip_thought_signature_validator_for_unsigned_vertex_tool_calls():
    model = makeGemini3Model("google-vertex", "google-vertex")
    contents = convertMessages(model, normalizeContext(makeUnsignedContext(_identity(model))))
    modelTurn = next((c for c in contents if c["role"] == "model"), None)
    functionCallParts = [p for p in modelTurn["parts"] if "functionCall" in p]

    assert len(functionCallParts) == 2
    assert "thoughtSignature" not in functionCallParts[0]
    assert "thoughtSignature" not in functionCallParts[1]
    assert "skip_thought_signature_validator" not in jsonStringify(modelTurn)


def test_preserves_valid_thought_signature_when_present_for_the_same_provider_and_model():
    model = makeGemini3Model("google-generative-ai", "google")
    validSig = "AAAAAAAAAAAAAAAAAAAAAA=="
    contents = convertMessages(model, normalizeContext(makeUnsignedContext(_identity(model), validSig)))
    modelTurn = next((c for c in contents if c["role"] == "model"), None)
    functionCallParts = [p for p in modelTurn["parts"] if "functionCall" in p]

    assert len(functionCallParts) == 2
    assert functionCallParts[0]["thoughtSignature"] == validSig
    assert "thoughtSignature" not in functionCallParts[1]


def test_does_not_add_a_thought_signature_for_non_gemini_3_models():
    model = makeGemini3Model("google-generative-ai", "google", "gemini-2.5-flash")
    contents = convertMessages(model, normalizeContext(makeUnsignedContext(_identity(model, id="other-model"))))
    modelTurn = next((c for c in contents if c["role"] == "model"), None)
    functionCallParts = [part for part in modelTurn["parts"] if "functionCall" in part]
    functionResponseParts = [part for part in _parts(contents) if "functionResponse" in part]

    assert len(functionCallParts) == 2
    assert all("id" not in part["functionCall"] for part in functionCallParts)
    assert all("thoughtSignature" not in part for part in functionCallParts)
    assert len(functionResponseParts) == 2
    assert all("id" not in part["functionResponse"] for part in functionResponseParts)


@pytest.mark.parametrize(
    ("expected", "modelId"),
    [
        (False, "gemini-2.5-flash"),
        (True, "gemini-3.6-flash"),
        (True, "claude-sonnet-4-5"),
        (True, "gpt-oss-120b"),
    ],
)
def test_requires_tool_call_id(expected, modelId):
    assert requiresToolCallId(modelId) is expected


# --- google-shared-image-tool-result-routing.test.ts --------------------------------------------


def makeImageModel(api: str, provider: str, id: str) -> Model:
    return Model.model_validate(
        {
            "id": id,
            "name": id,
            "api": api,
            "provider": provider,
            "baseUrl": "https://example.com",
            "reasoning": True,
            "input": ["text", "image"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 128000,
            "maxTokens": 8192,
        }
    )


def makeImageContext(model: Model) -> Context:
    now = _now()
    return Context.model_validate(
        {
            "messages": [
                {"role": "user", "content": "read the files", "timestamp": now},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "toolCall", "id": "call_a", "name": "read", "arguments": {"path": "a.txt"}},
                        {"type": "toolCall", "id": "call_img", "name": "read", "arguments": {"path": "image.png"}},
                        {"type": "toolCall", "id": "call_b", "name": "read", "arguments": {"path": "b.txt"}},
                    ],
                    "api": model.api,
                    "provider": model.provider,
                    "model": model.id,
                    "usage": _usage(),
                    "stopReason": "toolUse",
                    "timestamp": now,
                },
                {
                    "role": "toolResult",
                    "toolCallId": "call_a",
                    "toolName": "read",
                    "content": [{"type": "text", "text": "alpha text"}],
                    "isError": False,
                    "timestamp": now,
                },
                {
                    "role": "toolResult",
                    "toolCallId": "call_img",
                    "toolName": "read",
                    "content": [{"type": "image", "data": "abc", "mimeType": "image/png"}],
                    "isError": False,
                    "timestamp": now,
                },
                {
                    "role": "toolResult",
                    "toolCallId": "call_b",
                    "toolName": "read",
                    "content": [{"type": "text", "text": "beta text"}],
                    "isError": False,
                    "timestamp": now,
                },
            ],
        }
    )


def test_keeps_separate_synthetic_image_turn_for_gemini_2_x_google_api_models():
    model = makeImageModel("google-generative-ai", "google", "gemini-2.5-flash")
    contents = convertMessages(model, normalizeContext(makeImageContext(model)))

    assert len(contents) == 5
    assert all(part.get("functionResponse") for part in contents[2]["parts"])
    assert contents[3]["parts"][0]["text"] == "Tool result image:"
    assert contents[3]["parts"][1]["inlineData"]
    assert contents[4]["parts"][0]["functionResponse"]


def test_nests_image_tool_results_for_gemini_3_google_api_models():
    model = makeImageModel("google-generative-ai", "google", "gemini-3-pro-preview")
    contents = convertMessages(model, normalizeContext(makeImageContext(model)))

    assert len(contents) == 3
    toolResultTurn = contents[2]
    assert len(toolResultTurn["parts"]) == 3
    imageResponse = toolResultTurn["parts"][1]["functionResponse"]
    assert imageResponse
    assert len(imageResponse["parts"]) == 1
    assert imageResponse["parts"][0]["inlineData"]


# --- google-shared-signed-empty-blocks.test.ts --------------------------------------------------

# Gemini can attach `thoughtSignature` to a response part whose visible text is empty (e.g. a
# thought burst preceding a function call) and requires the signature echoed back on the next
# request. Dropping such blocks while rebuilding history silently breaks the reasoning chain:
# the model then intermittently ends a mid-task turn with a thought-only STOP and no tool call.
# These tests pin the rule: an empty text/thinking block is skipped only when it is UNSIGNED.


def makeSignedModel(id: str = "gemini-3-pro-preview") -> Model:
    return Model.model_validate(
        {
            "id": id,
            "name": id,
            "api": "google-generative-ai",
            "provider": "google",
            "baseUrl": "https://example.com",
            "reasoning": True,
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 128000,
            "maxTokens": 8192,
        }
    )


VALID_SIG = "AAAAAAAAAAAAAAAAAAAAAA=="


def makeSignedContext(model: dict[str, Any], content: list[dict[str, Any]]) -> Context:
    now = _now()
    return Context.model_validate(
        {
            "messages": [
                {"role": "user", "content": "Hi", "timestamp": now},
                {
                    "role": "assistant",
                    "content": content,
                    "api": model["api"],
                    "provider": model["provider"],
                    "model": model["id"],
                    "usage": _usage(),
                    "stopReason": "toolUse",
                    "timestamp": now,
                },
            ],
        }
    )


def test_keeps_a_signed_empty_thinking_block_so_its_signature_is_echoed_back():
    model = makeSignedModel()
    contents = convertMessages(
        model,
        normalizeContext(
            makeSignedContext(
                _identity(model),
                [
                    {"type": "thinking", "thinking": "", "thinkingSignature": VALID_SIG},
                    {"type": "toolCall", "id": "call_1", "name": "bash", "arguments": {"command": "ls"}},
                ],
            )
        ),
    )
    modelTurn = next((c for c in contents if c["role"] == "model"), None)
    signed = [p for p in modelTurn["parts"] if p.get("thoughtSignature") == VALID_SIG]
    assert len(signed) == 1
    assert signed[0]["thought"] is True


def test_keeps_a_signed_empty_text_block_the_same_way():
    model = makeSignedModel()
    contents = convertMessages(
        model,
        normalizeContext(
            makeSignedContext(
                _identity(model),
                [
                    {"type": "text", "text": "", "textSignature": VALID_SIG},
                    {"type": "toolCall", "id": "call_1", "name": "bash", "arguments": {"command": "ls"}},
                ],
            )
        ),
    )
    modelTurn = next((c for c in contents if c["role"] == "model"), None)
    signed = [p for p in modelTurn["parts"] if p.get("thoughtSignature") == VALID_SIG]
    assert len(signed) == 1


def test_still_drops_unsigned_empty_blocks():
    model = makeSignedModel()
    contents = convertMessages(
        model,
        normalizeContext(
            makeSignedContext(
                _identity(model),
                [
                    {"type": "thinking", "thinking": ""},
                    {"type": "text", "text": "   "},
                    {"type": "toolCall", "id": "call_1", "name": "bash", "arguments": {"command": "ls"}},
                ],
            )
        ),
    )
    modelTurn = next((c for c in contents if c["role"] == "model"), None)
    assert len(modelTurn["parts"]) == 1
    assert modelTurn["parts"][0]["functionCall"]


def test_still_drops_signed_empty_blocks_from_a_different_provider_or_model():
    model = makeSignedModel()
    contents = convertMessages(
        model,
        normalizeContext(
            makeSignedContext(
                _identity(model, id="other-model"),
                [
                    {"type": "thinking", "thinking": "", "thinkingSignature": VALID_SIG},
                    {"type": "text", "text": "", "textSignature": VALID_SIG},
                    {"type": "toolCall", "id": "call_1", "name": "bash", "arguments": {"command": "ls"}},
                ],
            )
        ),
    )
    modelTurn = next((c for c in contents if c["role"] == "model"), None)
    assert len(modelTurn["parts"]) == 1
    assert modelTurn["parts"][0]["functionCall"]
    assert VALID_SIG not in jsonStringify(modelTurn)

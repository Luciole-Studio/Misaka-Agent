"""Translated from pi's ``packages/ai/test/anthropic-auth-token.test.ts`` (v1.1.0).

MISAKA fork: upstream mocks the SDK constructor; here a local HTTP server receives the request and its headers show
the same choices (no ``x-api-key`` is the constructor's null ``apiKey``; a bearer ``Authorization`` with the OAuth beta
is its ``authToken``).
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

from misaka.ai.api.anthropic_messages import stream as streamAnthropic
from misaka.ai.auth.types import AuthResult, ModelAuth
from misaka.ai.env_api_keys import ANTHROPIC_AUTH_TOKEN_ENV, ANTHROPIC_OAUTH_TOKEN_ENV
from misaka.ai.models import CreateModelsOptions, createModels
from misaka.ai.providers.anthropic import anthropicProvider
from misaka.ai.types import Model, ModelCost, UserMessage
from misaka.ai.utils.pi_user_agent import getPiUserAgent
from misaka.ai.utils.transcript import normalizeContext
from misaka.utils.js import AbortController
from tests.http_capture import Reply, captureServer


def _sse() -> bytes:
    events = [
        {"type": "message_start", "message": {"id": "msg_test", "usage": {"input_tokens": 1, "output_tokens": 0}}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ]
    return "\n".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n" for event in events).encode()


context = normalizeContext(
    {"systemPrompt": "System prompt.", "messages": [UserMessage(content="Hello", timestamp=time.time_ns() // 1_000_000)]}
)


def _model(baseUrl: str, **update: object) -> Model:
    fields = {
        "id": "claude-test",
        "name": "Claude Test",
        "api": "anthropic-messages",
        "provider": "anthropic",
        "baseUrl": baseUrl,
        "reasoning": False,
        "input": ["text"],
        "cost": ModelCost(input=0, output=0, cacheRead=0, cacheWrite=0),
        "contextWindow": 100000,
        "maxTokens": 4096,
        **update,
    }
    return Model(**fields)


neverAbortedSignal = AbortController().signal


def _authContext(env: dict[str, str]) -> SimpleNamespace:
    async def readEnv(name: str) -> str | None:
        return env.get(name)

    async def fileExists(_path: str) -> bool:
        return False

    return SimpleNamespace(env=readEnv, fileExists=fileExists)


async def _modelsRequest(env: dict[str, str], options: dict | None = None):
    models = createModels(CreateModelsOptions(authContext=_authContext(env)))
    models.setProvider(anthropicProvider())
    with captureServer(lambda _request: Reply(body=_sse())) as server:
        result = await models.streamSimple(_model(server.baseUrl), context, options).result()
    assert result.stopReason == "stop", result.errorMessage
    return server.requests[0]


async def test_resolves_anthropic_auth_token_as_a_bearer_authorization_header():
    provider = anthropicProvider()
    auth = await provider.auth.apiKey.resolve(
        ctx=_authContext({
            "ANTHROPIC_AUTH_TOKEN": "auth-token",
            "ANTHROPIC_OAUTH_TOKEN": "oauth-token",
            "ANTHROPIC_API_KEY": "api-key",
        }),
        signal=neverAbortedSignal,
    )

    assert auth == AuthResult(
        auth=ModelAuth(headers={"Authorization": "Bearer auth-token"}),
        source=ANTHROPIC_AUTH_TOKEN_ENV,
    )


async def test_preserves_anthropic_oauth_token_as_oauth_shaped_api_auth():
    provider = anthropicProvider()
    auth = await provider.auth.apiKey.resolve(
        ctx=_authContext({
            "ANTHROPIC_OAUTH_TOKEN": "oauth-token",
            "ANTHROPIC_API_KEY": "api-key",
        }),
        signal=neverAbortedSignal,
    )

    assert auth == AuthResult(auth=ModelAuth(apiKey="oauth-token"), source=ANTHROPIC_OAUTH_TOKEN_ENV)


async def _request(options: dict, **modelUpdate: object):
    with captureServer(lambda _request: Reply(body=_sse())) as server:
        result = await streamAnthropic(_model(server.baseUrl, **modelUpdate), context, options).result()
    assert result.stopReason == "stop", result.errorMessage
    return server.requests[0]


async def test_uses_authorization_headers_without_oauth_mode_request_shaping():
    request = await _request({"headers": {"Authorization": "Bearer gateway-token"}})

    assert "x-api-key" not in request.headers
    assert request.headers["authorization"] == "Bearer gateway-token"
    assert "oauth-2025-04-20" not in request.headers.get("anthropic-beta", "")
    assert [block["text"] for block in request.body["system"]] == ["System prompt."]


async def test_threads_auth_context_anthropic_auth_token_through_request_headers():
    request = await _modelsRequest({"ANTHROPIC_AUTH_TOKEN": "ctx-token"})

    assert "x-api-key" not in request.headers
    assert request.headers["authorization"] == "Bearer ctx-token"
    assert "oauth-2025-04-20" not in request.headers.get("anthropic-beta", "")
    assert [block["text"] for block in request.body["system"]] == ["System prompt."]


async def test_preserves_oauth_request_shaping_for_anthropic_oauth_token():
    request = await _modelsRequest({"ANTHROPIC_OAUTH_TOKEN": "sk-ant-oat-test"})

    assert "x-api-key" not in request.headers
    assert request.headers["authorization"] == "Bearer sk-ant-oat-test"
    assert "oauth-2025-04-20" in request.headers["anthropic-beta"].split(",")


async def test_lets_explicit_request_headers_override_anthropic_auth_token():
    request = await _modelsRequest(
        {"ANTHROPIC_AUTH_TOKEN": "ctx-token"},
        {"headers": {"Authorization": "Bearer explicit-token"}},
    )

    assert request.headers["authorization"] == "Bearer explicit-token"


async def test_uses_pis_user_agent_by_default_for_anthropic_messages_requests():
    # MISAKA fork: the client name is misaka's (`getPiUserAgent`).
    request = await _request({"apiKey": "anthropic-key"})

    assert request.headers["user-agent"] == getPiUserAgent()


async def test_lets_explicit_headers_override_the_default_anthropic_messages_user_agent():
    request = await _request(
        {"apiKey": "kimi-key", "headers": {"User-Agent": "custom-client"}},
        id="kimi-for-coding",
        name="Kimi For Coding",
        provider="kimi-coding",
    )

    assert request.headers["user-agent"] == "custom-client"


async def test_preserves_explicit_anthropic_beta_header_replacement():
    request = await _request({"apiKey": "anthropic-key", "headers": {"anthropic-beta": "custom-beta"}})

    assert request.headers["anthropic-beta"] == "custom-beta"


async def test_preserves_explicit_anthropic_beta_header_suppression():
    request = await _request({"apiKey": "anthropic-key", "headers": {"anthropic-beta": None}})

    assert "anthropic-beta" not in request.headers

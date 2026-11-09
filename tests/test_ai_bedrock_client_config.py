"""Translated from pi's ``packages/ai/test/bedrock-credentials.test.ts``, ``bedrock-endpoint-resolution.test.ts`` and
``bedrock-custom-headers.test.ts`` (v1.1.0).

MISAKA fork: pi mocks ``BedrockRuntimeClient`` to record its config and middleware; here a recording subclass of
``_bedrock_sdk.BedrockRuntimeClient`` does the same. A build middleware takes the request (botocore runs the chain), so
the ``next`` spy of the custom-headers cases has no counterpart.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.api.bedrock_converse_stream import stream as streamBedrock
from misaka.ai.api.bedrock_converse_stream import streamSimple as streamSimpleBedrock
from misaka.ai.compat import getModel
from misaka.ai.compat import stream as compatStream
from misaka.ai.types import Context, Model, SimpleStreamOptions, UserMessage
from misaka.ai.utils.transcript import normalizeContext
from tests.bedrock_fake import recordBedrockClients

context = Context(messages=[UserMessage(content="hello", timestamp=time.time_ns() // 1_000_000)])


@pytest.fixture(autouse=True)
def _cleanAwsEnv(monkeypatch):
    for name in (
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_BEDROCK_SKIP_AUTH",
    ):
        monkeypatch.delenv(name, raising=False)


def _model(base: Model, **update: Any) -> Model:
    return type(base).model_validate({**base.model_dump(exclude_none=True), **update})


async def captureClientConfig(monkeypatch, model: Model, options: dict[str, Any] | None = None) -> dict[str, Any]:
    clients = recordBedrockClients(monkeypatch)
    await compatStream(model, context, {"cacheRetention": "none", **(options or {})}).result()
    assert len(clients) == 1
    return clients[0].config


# --- bedrock-credentials.test.ts --------------------------------------------------------------


async def test_prefers_explicit_and_scoped_profiles_over_ambient_aws_access_keys(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secretexample")
    model = getModel("amazon-bedrock", "us.anthropic.claude-opus-4-8")

    config = await captureClientConfig(monkeypatch, model, {"profile": "explicit-profile"})

    assert config["profile"] == "explicit-profile"
    assert "credentials" not in config

    config = await captureClientConfig(monkeypatch, model, {"env": {"AWS_PROFILE": "scoped-profile"}})

    assert config["profile"] == "scoped-profile"
    assert "credentials" not in config


async def test_uses_ambient_aws_access_keys_when_no_profile_is_configured(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secretexample")
    model = getModel("amazon-bedrock", "us.anthropic.claude-opus-4-8")

    config = await captureClientConfig(monkeypatch, model)

    assert config.get("profile") is None
    assert config["credentials"] == {"accessKeyId": "AKIAEXAMPLE", "secretAccessKey": "secretexample"}


async def test_uses_ambient_aws_access_keys_when_only_an_ambient_profile_is_set(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secretexample")
    monkeypatch.setenv("AWS_PROFILE", "ambient-profile")
    model = getModel("amazon-bedrock", "us.anthropic.claude-opus-4-8")

    config = await captureClientConfig(monkeypatch, model)

    assert config["profile"] == "ambient-profile"
    assert config["credentials"] == {"accessKeyId": "AKIAEXAMPLE", "secretAccessKey": "secretexample"}


# --- bedrock-endpoint-resolution.test.ts ------------------------------------------------------


def test_assigns_eu_central_1_runtime_urls_to_built_in_eu_inference_profiles():
    model = getModel("amazon-bedrock", "eu.anthropic.claude-sonnet-4-5-20250929-v1:0")

    assert model.baseUrl == "https://bedrock-runtime.eu-central-1.amazonaws.com"


async def test_does_not_pin_standard_aws_endpoints_when_aws_region_is_configured(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    config = await captureClientConfig(monkeypatch, getModel("amazon-bedrock", "us.anthropic.claude-opus-4-8"))

    assert config["region"] == "us-east-2"
    assert "endpoint" not in config


async def test_derives_region_from_a_built_in_eu_endpoint_when_no_region_or_profile_is_configured(monkeypatch):
    config = await captureClientConfig(
        monkeypatch, getModel("amazon-bedrock", "eu.anthropic.claude-sonnet-4-5-20250929-v1:0")
    )

    assert config["endpoint"] == "https://bedrock-runtime.eu-central-1.amazonaws.com"
    assert config["region"] == "eu-central-1"


async def test_handles_missing_regions_for_explicit_scoped_and_ambient_profiles(monkeypatch):
    model = getModel("amazon-bedrock", "eu.anthropic.claude-sonnet-4-5-20250929-v1:0")

    config = await captureClientConfig(monkeypatch, model, {"profile": "bedrock-profile"})

    assert config["profile"] == "bedrock-profile"
    assert config["endpoint"] == "https://bedrock-runtime.eu-central-1.amazonaws.com"
    assert config["region"] == "eu-central-1"

    config = await captureClientConfig(monkeypatch, model, {"env": {"AWS_PROFILE": "scoped-bedrock-profile"}})

    assert config["profile"] == "scoped-bedrock-profile"
    assert config["endpoint"] == "https://bedrock-runtime.eu-central-1.amazonaws.com"
    assert config["region"] == "eu-central-1"

    monkeypatch.setenv("AWS_PROFILE", "ambient-bedrock-profile")
    config = await captureClientConfig(monkeypatch, model)

    assert config["profile"] == "ambient-bedrock-profile"
    assert "endpoint" not in config
    assert "region" not in config


async def test_still_passes_custom_bedrock_endpoints_through_to_the_sdk_client(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    model = _model(getModel("amazon-bedrock", "us.anthropic.claude-opus-4-8"), baseUrl="https://bedrock-vpc.example.com")

    config = await captureClientConfig(monkeypatch, model)

    assert config["endpoint"] == "https://bedrock-vpc.example.com"
    assert config["region"] == "us-west-2"


async def test_extracts_region_from_inference_profile_arn_regardless_of_aws_region(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    model = _model(
        getModel("amazon-bedrock", "us.anthropic.claude-opus-4-8"),
        id="arn:aws:bedrock:us-west-2:123456789012:application-inference-profile/abc123",
    )

    config = await captureClientConfig(monkeypatch, model)

    assert config["region"] == "us-west-2"


async def test_extracts_region_from_gov_cloud_inference_profile_arn(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    model = _model(
        getModel("amazon-bedrock", "us.anthropic.claude-opus-4-8"),
        id="arn:aws-us-gov:bedrock:us-gov-west-1:123456789012:application-inference-profile/abc123",
    )

    config = await captureClientConfig(monkeypatch, model)

    assert config["region"] == "us-gov-west-1"


async def test_preserves_ambient_aws_auth_for_custom_model_ids_through_compat_dispatch(monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "bedrock-profile")
    model = _model(
        getModel("amazon-bedrock", "us.anthropic.claude-opus-4-8"),
        id="arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/example",
    )

    config = await captureClientConfig(monkeypatch, model)

    assert config["profile"] == "bedrock-profile"
    assert "token" not in config
    assert "authSchemePreference" not in config


async def test_uses_the_generic_api_key_option_as_a_bedrock_bearer_token(monkeypatch):
    config = await captureClientConfig(
        monkeypatch, getModel("amazon-bedrock", "us.anthropic.claude-opus-4-8"), {"apiKey": "bedrock-api-key"}
    )

    assert config["token"] == {"token": "bedrock-api-key"}
    assert config["authSchemePreference"] == ["httpBearerAuth"]


# --- bedrock-custom-headers.test.ts -------------------------------------------------------------

MIDDLEWARE_NAME = "pi-ai-custom-headers"
headerContext = normalizeContext(context)


async def driveBedrock(monkeypatch, options: dict[str, Any]) -> list[Any]:
    clients = recordBedrockClients(monkeypatch)
    await streamBedrock(getModel("amazon-bedrock", "us.anthropic.claude-opus-4-8"), headerContext, options).result()
    return findCustomHeadersRegistration(clients)


def findCustomHeadersRegistration(clients: list[Any]) -> list[tuple[Any, dict[str, Any]]]:
    return [
        (middleware, options)
        for client in clients
        for middleware, options in client.middlewareStack.entries
        if options.get("name") == MIDDLEWARE_NAME
    ]


async def test_vc1_registers_a_build_step_middleware_that_injects_the_caller_header(monkeypatch):
    registrations = await driveBedrock(monkeypatch, {"cacheRetention": "none", "headers": {"x-custom": "v"}})
    assert len(registrations) == 1

    handler, opts = registrations[0]
    assert opts["step"] == "build"
    assert opts["priority"] == "low"
    assert opts["name"] == MIDDLEWARE_NAME

    request = SimpleNamespace(headers={})
    handler(request)

    assert request.headers["x-custom"] == "v"


async def test_vc2_skips_reserved_headers_case_insensitively_while_applying_allowed_ones(monkeypatch):
    [(handler, _opts)] = await driveBedrock(
        monkeypatch,
        {
            "cacheRetention": "none",
            "headers": {
                "authorization": "evil",
                "x-amz-date": "evil",
                "x-allowed": "ok",
                "Authorization": "evil2",
                "X-Amz-Date": "evil2",
                "HOST": "evil3",
            },
        },
    )

    request = SimpleNamespace(headers={"authorization": "real-auth", "x-amz-date": "real-date", "host": "real-host"})
    handler(request)

    assert request.headers["authorization"] == "real-auth"
    assert request.headers["x-amz-date"] == "real-date"
    assert request.headers["host"] == "real-host"
    assert request.headers["x-allowed"] == "ok"
    # Mixed-case reserved keys must be skipped too: a case-sensitive guard would
    # add them back as distinct capitalised keys.
    assert sorted(request.headers) == sorted(["authorization", "host", "x-allowed", "x-amz-date"])


async def test_vc3_registers_no_middleware_when_headers_is_undefined(monkeypatch):
    assert await driveBedrock(monkeypatch, {"cacheRetention": "none"}) == []


async def test_vc3_registers_no_middleware_when_headers_is_empty(monkeypatch):
    assert await driveBedrock(monkeypatch, {"cacheRetention": "none", "headers": {}}) == []


async def test_vc3_passes_through_unchanged_when_the_request_has_no_headers(monkeypatch):
    [(handler, _opts)] = await driveBedrock(monkeypatch, {"cacheRetention": "none", "headers": {"x-custom": "v"}})

    handler(SimpleNamespace())
    handler(None)


async def test_vc4_stream_simple_bedrock_forwards_headers_end_to_end(monkeypatch):
    clients = recordBedrockClients(monkeypatch)
    await streamSimpleBedrock(
        getModel("amazon-bedrock", "us.anthropic.claude-opus-4-8"), headerContext, SimpleStreamOptions(headers={"x-custom": "v"})
    ).result()

    registrations = findCustomHeadersRegistration(clients)
    assert len(registrations) == 1
    handler, opts = registrations[0]
    assert opts["step"] == "build"

    request = SimpleNamespace(headers={})
    handler(request)

    assert request.headers["x-custom"] == "v"

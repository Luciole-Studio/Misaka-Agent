"""A boto3 ``bedrock-runtime`` client stand-in for the Bedrock adapter tests (pi's tests mock the AWS SDK)."""

from __future__ import annotations

from typing import Any, ClassVar

from misaka.ai.api import _bedrock_sdk


class FakeBedrockClient:
    """``converse_stream`` answers with ``events`` (then raises ``fail``, if given); requests are recorded."""

    def __init__(self, *events: Any, fail: BaseException | None = None, metadata: dict[str, Any] | None = None) -> None:
        self.events, self.fail, self.calls = events, fail, []
        self.metadata = metadata if metadata is not None else {"RequestId": "req-1", "HTTPStatusCode": 200}

    def converse_stream(self, **request: Any) -> dict[str, Any]:
        self.calls.append(request)

        def stream() -> Any:
            yield from self.events
            if self.fail is not None:
                raise self.fail

        return {"ResponseMetadata": self.metadata, "stream": stream()}


def useFakeBedrock(monkeypatch: Any, client: Any) -> list[tuple[dict[str, Any], list[Any]]]:
    """Every ``BedrockRuntimeClient.send`` talks to ``client``; returns the configs and middleware it was built with."""
    built: list[tuple[dict[str, Any], list[Any]]] = []

    def create(config: dict[str, Any], middleware: list[Any]) -> Any:
        built.append((config, middleware))
        return client

    monkeypatch.setattr(_bedrock_sdk, "_createBotoClient", create)
    return built


class RecordingBedrockRuntimeClient(_bedrock_sdk.BedrockRuntimeClient):
    """pi's mocked ``BedrockRuntimeClient``: records each client it builds; ``send`` fails like the mock."""

    instances: ClassVar[list[RecordingBedrockRuntimeClient]] = []

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        RecordingBedrockRuntimeClient.instances.append(self)

    async def send(self, commandInput: dict[str, Any], abortSignal: Any = None) -> Any:
        raise RuntimeError("mock send")


def recordBedrockClients(monkeypatch: Any) -> list[RecordingBedrockRuntimeClient]:
    RecordingBedrockRuntimeClient.instances = []
    monkeypatch.setattr(_bedrock_sdk, "BedrockRuntimeClient", RecordingBedrockRuntimeClient)
    return RecordingBedrockRuntimeClient.instances

"""Translated from pi's ``packages/coding-agent/test/print-mode.test.ts`` (v1.3.0)."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.modes.print_mode import runPrintMode


class Mock:
    """A ``vi.fn``: records calls and answers with ``impl``."""

    def __init__(self, impl: Any = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self._impl = impl

    def __call__(self, *args: Any) -> Any:
        self.calls.append(args)
        return self._impl(*args) if self._impl is not None else None


def asyncMock(impl: Any = None) -> Mock:
    async def run(*args: Any) -> Any:
        if impl is None:
            return None
        result = impl(*args)
        return await result if hasattr(result, "__await__") else result

    return Mock(run)


def createAssistantMessage(
    text: str | None = None, stopReason: str = "stop", errorMessage: str | None = None
) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": text}] if text else [],
        "api": "openai-responses",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "usage": {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 0,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
        },
        "stopReason": stopReason,
        "errorMessage": errorMessage,
        "timestamp": int(time.time() * 1000),
    }


def createRuntimeHost(assistantMessage: dict[str, Any]) -> SimpleNamespace:
    extensionRunner = SimpleNamespace(
        hasHandlers=lambda eventType: eventType == "session_shutdown",
        emit=asyncMock(),
    )

    state = SimpleNamespace(messages=[assistantMessage])

    session = SimpleNamespace(
        sessionManager=SimpleNamespace(getHeader=lambda: None),
        agent=SimpleNamespace(waitForIdle=asyncMock(), subscribe=Mock(lambda *_: lambda: None)),
        state=state,
        extensionRunner=extensionRunner,
        bindExtensions=asyncMock(),
        subscribe=Mock(lambda *_: lambda: None),
        prompt=asyncMock(),
        reload=asyncMock(),
    )

    async def dispose(*_: Any) -> None:
        await session.extensionRunner.emit({"type": "session_shutdown", "reason": "quit"})

    return SimpleNamespace(
        session=session,
        newSession=asyncMock(),
        fork=asyncMock(lambda *_: {"selectedText": ""}),
        switchSession=asyncMock(),
        dispose=asyncMock(dispose),
        setRebindSession=Mock(),
    )


async def test_emits_session_shutdown_in_text_mode():
    runtimeHost = createRuntimeHost(createAssistantMessage(text="done"))
    session = runtimeHost.session
    images = [{"type": "image", "mimeType": "image/png", "data": "abc"}]

    exitCode = await runPrintMode(
        runtimeHost, {"mode": "text", "initialMessage": "Say done", "initialImages": images}
    )

    assert exitCode == 0
    assert session.prompt.calls == [("Say done", {"images": images})]
    assert session.extensionRunner.emit.calls == [({"type": "session_shutdown", "reason": "quit"},)]


async def test_emits_session_shutdown_in_json_mode():
    runtimeHost = createRuntimeHost(createAssistantMessage(text="done"))
    session = runtimeHost.session

    exitCode = await runPrintMode(runtimeHost, {"mode": "json", "messages": ["hello"]})

    assert exitCode == 0
    assert session.prompt.calls == [("hello",)]
    assert session.extensionRunner.emit.calls == [({"type": "session_shutdown", "reason": "quit"},)]


async def test_emits_session_shutdown_and_returns_non_zero_on_assistant_error(capsys: pytest.CaptureFixture[str]):
    runtimeHost = createRuntimeHost(createAssistantMessage(stopReason="error", errorMessage="provider failure"))
    session = runtimeHost.session

    exitCode = await runPrintMode(runtimeHost, {"mode": "text"})

    assert exitCode == 1
    assert "provider failure\n" in capsys.readouterr().err
    assert session.extensionRunner.emit.calls == [({"type": "session_shutdown", "reason": "quit"},)]


# #10799
async def test_warns_on_stderr_and_returns_non_zero_when_the_response_was_truncated(
    capsys: pytest.CaptureFixture[str],
):
    runtimeHost = createRuntimeHost(createAssistantMessage(text="partial", stopReason="length"))

    exitCode = await runPrintMode(runtimeHost, {"mode": "text"})

    assert exitCode == 1
    captured = capsys.readouterr()
    assert "Response was truncated: the model reached its output token limit." in captured.err

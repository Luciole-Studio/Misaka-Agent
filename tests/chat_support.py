"""pi durable's ``test/chat-support.ts``."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from misaka.ai.models import createModels
from misaka.ai.providers.faux import fauxAssistantMessage, fauxProvider
from misaka.chord.context import abortError
from misaka.durable import Harness, createRegistry
from tests.durable_support import context


def chatSetup(options: dict[str, Any] | None = None) -> SimpleNamespace:
    """Models and registry that survive a close/reopen, like a host process's own objects. ``settings`` is the live
    Harness settings dict; tests assign keys to change them between decisions."""
    faux = fauxProvider(options)
    models = createModels()
    models.setProvider(faux.provider)
    return SimpleNamespace(
        faux=faux,
        models=models,
        registry=createRegistry(),
        reports=[],
        settings={},
        now=lambda: int(time.time() * 1000),
    )


async def openChat(storage: Any, setup: SimpleNamespace, options: dict[str, Any] | None = None) -> SimpleNamespace:
    """Open a Harness over ``storage`` and return its root, configured with the faux model on first creation. ``env`` is
    one environment for every conversation, or an ``env`` function."""
    options = options or {}
    env = options.get("env")
    harness = await Harness.open(
        storage,
        {
            "models": setup.models,
            "registry": setup.registry,
            "settings": setup.settings,
            **({} if env is None else {"env": env if callable(env) else (lambda target, callContext: env)}),
            "now": lambda: setup.now(),
            "onReport": setup.reports.append,
        },
        context,
    )
    root = await harness.root(context, {"agent": {"model": {"provider": "faux", "modelId": "faux-1"}}})
    return SimpleNamespace(harness=harness, root=root)


async def allEntries(conversation: Any, callContext: Any = context) -> list[dict[str, Any]]:
    """Raw entries of a conversation, oldest first."""
    page = await conversation.entries({}, 1000, None, callContext)
    return list(reversed(page["items"]))


def textOf(message: dict[str, Any] | None) -> str | None:
    """Text of the first text content of a message."""
    if message is None or message["role"] == "system":
        return None
    if isinstance(message["content"], str):
        return message["content"]  # type: ignore[no-any-return]
    text = next((content for content in message["content"] if content["type"] == "text"), None)
    return None if text is None else text["text"]  # type: ignore[no-any-return]


def requestText(message: Any) -> str | None:
    """Text of the first text content of a request message, which is a pi-ai message model rather than a stored dict."""
    if message.role == "system":
        return None
    content = message.content
    if isinstance(content, str):
        return content
    text = next((item for item in content if item.type == "text"), None)
    return None if text is None else text.text  # type: ignore[no-any-return]


async def waitFor(check: Callable[[], Any], timeoutMs: int = 5000) -> None:
    """Poll ``check`` in real time until it holds; for waits that span throttle windows and timers."""
    deadline = time.time() * 1000 + timeoutMs
    while True:
        result = check()
        if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
            result = await result
        if result:
            return
        if time.time() * 1000 > deadline:
            raise RuntimeError("Condition was not reached")
        await asyncio.sleep(0.005)


def unanswered() -> SimpleNamespace:
    """Faux response that never answers; the run stays busy until its generation is cancelled. ``reached`` resolves once
    the request was sent, after the generation's preparation and request commits."""
    reached: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def step(callContext: Any, options: Any, state: Any, model: Any) -> Any:
        if not reached.done():
            reached.set_result(None)
        signal = options.signal
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        signal.addEventListener("abort", lambda: future.done() or future.set_exception(abortError(signal)), once=True)
        return await future

    return SimpleNamespace(step=step, reached=reached)


def toolsNamed(setup: SimpleNamespace, *names: str) -> list[dict[str, Any]]:
    """The installed tools with these names, as ``configure()`` takes them."""
    installed = setup.registry.snapshot().tools()
    result = []
    for name in names:
        found = next((entry for entry in installed if entry["tool"]["name"] == name), None)
        if found is None:
            raise RuntimeError(f"Tool {name} is not installed")
        result.append(found["tool"])
    return result


class WithOverrides:
    """``models`` with some attributes replaced, as the upstream test's ``Proxy`` does."""

    def __init__(self, models: Any, **overrides: Any) -> None:
        self._models = models
        self._overrides = overrides

    def __getattr__(self, name: str) -> Any:
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._models, name)


class FakeStream:
    """An event stream that yields the given events and resolves to ``final``."""

    def __init__(self, events: Any, final: Any) -> None:
        self._events = events
        self._final = final

    def __aiter__(self) -> Any:
        return self._events()

    async def result(self) -> Any:
        return self._final


def withStream(models: Any, streamSimple: Any) -> Any:
    """``models`` with ``streamSimple`` replaced."""
    return WithOverrides(models, streamSimple=streamSimple)


class InvalidFinal:
    """A final message that is not strict JSON."""

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return {**fauxAssistantMessage("final").model_dump(mode="json", exclude_none=True), "invalid": lambda: None}


def invalidFinalStream(*_: Any) -> FakeStream:
    """A stream that commits one partial and then ends with a final message that is not strict JSON, so the
    classification commit throws and the scheduler faults the task."""

    async def events() -> Any:
        yield SimpleNamespace(type="start", partial=fauxAssistantMessage("partial", {"stopReason": "pending"}))
        await asyncio.sleep(0.3)

    return FakeStream(events, InvalidFinal())

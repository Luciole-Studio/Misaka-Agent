"""Translated from pi's ``packages/coding-agent/test/suite/regressions/`` ``6904-dns-transport-retry.test.ts``,
``6999-models-json-hot-reload.test.ts`` and ``7048-compaction-truncated-summary.test.ts`` (v1.1.0).

MISAKA fork: ``7187-malformed-package-manifest.test.ts`` is not translated; MISAKA's package manager has no
``packages`` sources and reads no ``package.json`` manifests.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.providers.faux import fauxAssistantMessage
from misaka.ai.types import Usage
from misaka.core.auth_storage import AuthStorage
from misaka.core.keybindings import KeybindingsManager
from misaka.modes.interactive.components.model_selector import ModelSelectorComponent
from misaka.modes.interactive.theme.theme import initTheme
from misaka.ui.tui import setKeybindings
from misaka.utils.ansi import stripAnsi
from misaka.utils.values import read_field
from tests.model_runtime_test_utils import createModelRegistry, getModelRuntime
from tests.suite_harness import createHarness

# --- issue #6904 DNS transport failure retry ----------------------------------------------------

wrappedDnsLookupError = (
    "The pending stream has been canceled (caused by: getaddrinfo ENOTFOUND bedrock-runtime.us-east-1.amazonaws.com)"
)


async def test_retries_a_transient_dns_lookup_failure():
    async with createHarness(settings={"retry": {"enabled": True, "maxRetries": 3, "baseDelayMs": 1}}) as harness:
        harness.setResponses([
            fauxAssistantMessage("", {"stopReason": "error", "errorMessage": wrappedDnsLookupError}),
            fauxAssistantMessage("recovered after DNS retry"),
        ])

        await harness.session.prompt("test")

        assert harness.faux.state.callCount == 2
        assert [read_field(event, "errorMessage") for event in harness.eventsOfType("auto_retry_start")] == [
            wrappedDnsLookupError
        ]
        assert [read_field(event, "success") for event in harness.eventsOfType("auto_retry_end")] == [True]


# --- issue #6999 models.json hot reload ---------------------------------------------------------


def observeRefreshRender() -> tuple[Any, asyncio.Future[None]]:
    renderedAfterRefresh: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    state = {"renderCount": 0}

    def requestRender(*_args: Any) -> None:
        state["renderCount"] += 1
        if state["renderCount"] == 2 and not renderedAfterRefresh.done():
            renderedAfterRefresh.set_result(None)

    return SimpleNamespace(requestRender=requestRender), renderedAfterRefresh


def modelsJson(provider: str, model: str) -> dict[str, Any]:
    return {
        "providers": {
            provider: {
                "baseUrl": "https://example.test/v1",
                "api": "openai-completions",
                "apiKey": "test-key",
                "models": [{"id": model}],
            }
        }
    }


@pytest.fixture
def selectorEnvironment():
    initTheme("dark")
    setKeybindings(KeybindingsManager())


async def test_reloads_models_json_when_opening_model(tmp_path: Path, selectorEnvironment: None):
    modelsPath = tmp_path / "models.json"
    modelsPath.write_text(json.dumps(modelsJson("old-provider", "old-model")), encoding="utf-8")
    modelRuntime = getModelRuntime(await createModelRegistry(AuthStorage.inMemory(), str(modelsPath)))
    assert modelRuntime.getModel("old-provider", "old-model") is not None

    modelsPath.write_text(json.dumps(modelsJson("new-provider", "new-model")), encoding="utf-8")
    tui, renderedAfterRefresh = observeRefreshRender()
    selector = ModelSelectorComponent(tui, None, modelRuntime, [], lambda _model: None, lambda: None)

    await asyncio.wait_for(renderedAfterRefresh, 5)
    rendered = stripAnsi("\n".join(selector.render(120)))
    assert "new-model [new-provider]" in rendered
    assert "old-model [old-provider]" not in rendered


# --- #7048 truncated compaction summaries -------------------------------------------------------


def seedCompactableSession(harness: Any) -> None:
    harness.settingsManager.applyOverrides({"compaction": {"keepRecentTokens": 1}})
    now = int(time.time() * 1000)
    harness.sessionManager.appendMessage({
        "role": "user",
        "content": [{"type": "text", "text": "message to compact"}],
        "timestamp": now - 1000,
    })
    model = harness.getModel()
    assistant = fauxAssistantMessage("assistant response to compact", {"timestamp": now - 500}).model_copy(update={
        "api": model.api,
        "provider": model.provider,
        "model": model.id,
        "usage": Usage.model_validate({
            "input": 100,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 100,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
        }),
    })
    harness.sessionManager.appendMessage(assistant)
    harness.session.agent.state.messages = harness.sessionManager.buildSessionContext().messages


async def test_does_not_persist_a_length_limited_summary():
    async with createHarness() as harness:
        seedCompactableSession(harness)
        harness.setResponses([fauxAssistantMessage("partial summar", {"stopReason": "length"})])

        with pytest.raises(Exception, match="generation hit the token cap"):
            await harness.session.compact()
        assert [entry for entry in harness.sessionManager.getEntries() if read_field(entry, "type") == "compaction"] == []

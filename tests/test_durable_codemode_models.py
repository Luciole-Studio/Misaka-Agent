"""pi durable's ``test/codemode-models.test.ts``.

Adaptations: the test provider is misaka.ai's ``createProvider(CreateProviderOptions(...))`` with pydantic models.
Upstream's classifier answers ``{ good: { value: true } }`` through a type cast; misaka's ``ClassifierResult`` validates
its answers, so the classifier answers with the bool answer ``{ type: "bool", probability: 1 }`` and the script reads
``answers.good.probability === 1`` where upstream reads ``answers.good.value`` (both ``true``).
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.auth.types import ApiKeyAuth, AuthResult, ModelAuth, ProviderAuth
from misaka.ai.models import CreateProviderOptions, createProvider
from misaka.ai.providers.faux import fauxAssistantMessage, fauxText, fauxToolCall
from misaka.ai.types import (
    AssistantImages,
    ClassifierModel,
    ClassifierResult,
    ImageModel,
)
from misaka.codemode import CodemodeSandbox
from misaka.durable import MemoryStorage, ToolResultEntry, UsageDoc
from misaka.durable.codemode.codemode import createCodemodeExtension
from misaka.durable.env.node import NodeExecutionEnv
from tests.chat_support import allEntries, chatSetup, openChat
from tests.durable_support import context, toMatchObject
from tests.task_support import deferred

COST = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
USAGE = {
    "input": 10,
    "output": 2,
    "cacheRead": 0,
    "cacheWrite": 0,
    "totalTokens": 12,
    "cost": {"input": 0.001, "output": 0.0002, "cacheRead": 0, "cacheWrite": 0, "total": 0.0012},
}
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
HEADER = re.compile(r"^Script (completed|failed)\nWall time [\d.]+ seconds\nOutput:\n")


async def resolveAuth(*_: Any, **__: Any) -> AuthResult:
    return AuthResult(auth=ModelAuth())


def modelsChat() -> SimpleNamespace:
    """A chat whose models also have a classifier and an image model of a test provider, and the code mode extension."""
    setup = chatSetup()
    calls = SimpleNamespace(classify=0, images=0, inFlight=0, maxInFlight=0)
    # `atFour`: hold classifier calls until four run, then a little longer, so a fifth would show up.
    gate = SimpleNamespace(open=deferred(), atFour=False)
    gate.open.resolve()

    class Classifier:
        async def classify(self, model: Any, classifierContext: Any, _options: Any = None) -> ClassifierResult:
            calls.classify += 1
            if classifierContext.state.get("fail") is True:
                return ClassifierResult.model_validate(
                    {
                        "api": model.api,
                        "provider": model.provider,
                        "model": model.id,
                        "answers": {},
                        "stopReason": "error",
                        "errorMessage": "quota exceeded",
                        "timestamp": 0,
                    }
                )
            calls.inFlight += 1
            calls.maxInFlight = max(calls.maxInFlight, calls.inFlight)
            if gate.atFour and calls.inFlight == 4:
                asyncio.get_running_loop().call_later(0.05, lambda: gate.open.resolve())
            await gate.open.promise
            calls.inFlight -= 1
            return ClassifierResult.model_validate(
                {
                    "api": model.api,
                    "provider": model.provider,
                    "model": model.id,
                    "answers": {"good": {"type": "bool", "probability": 1}},
                    "usage": USAGE,
                    "stopReason": "stop",
                    "timestamp": 0,
                }
            )

    class Images:
        async def generateImages(self, model: Any, _context: Any, _options: Any = None) -> AssistantImages:
            calls.images += 1
            return AssistantImages.model_validate(
                {
                    "api": model.api,
                    "provider": model.provider,
                    "model": model.id,
                    "output": [{"type": "image", "data": PNG, "mimeType": "image/png"}],
                    "stopReason": "stop",
                    "timestamp": 0,
                }
            )

    setup.models.setProvider(
        createProvider(
            CreateProviderOptions(
                id="test",
                auth=ProviderAuth(apiKey=ApiKeyAuth(name="Test", resolve=resolveAuth)),
                models=[
                    ClassifierModel.model_validate(
                        {
                            "id": "judge",
                            "name": "Judge",
                            "api": "test-classifier",
                            "provider": "test",
                            "baseUrl": "https://example.invalid",
                            "input": ["text"],
                            "cost": COST,
                            "headers": {"authorization": "secret"},
                            "type": "classifier",
                            "contextWindow": 1000,
                        }
                    ),
                    ImageModel.model_validate(
                        {
                            "id": "painter",
                            "name": "Painter",
                            "api": "test-images",
                            "provider": "test",
                            "baseUrl": "https://example.invalid",
                            "input": ["text"],
                            "cost": COST,
                            "type": "image",
                            "output": ["image"],
                        }
                    ),
                ],
                classifiers={"test-classifier": Classifier()},
                images={"test-images": Images()},
            )
        )
    )
    setup.registry.install(createCodemodeExtension({"sandbox": CodemodeSandbox()}))
    return SimpleNamespace(setup=setup, calls=calls, gate=gate)


async def run(chat: SimpleNamespace, code: str, env: Any = None) -> SimpleNamespace:
    chat.setup.faux.setResponses(
        [
            fauxAssistantMessage([fauxToolCall("codemode", {"code": code}, {"id": "s1"})], {"stopReason": "toolUse"}),
            fauxAssistantMessage([fauxText("done")]),
        ]
    )
    opened = await openChat(MemoryStorage(), chat.setup, {} if env is None else {"env": env})
    await (await opened.root.submit({"type": "input", "content": "go"}, context)).wait(context)
    entry = next(candidate for candidate in await allEntries(opened.root) if ToolResultEntry.is_(candidate))
    message = entry["model"][0]
    text = HEADER.sub(
        "", "".join(item["text"] if item.get("text") is not None else f"<{item['type']}>" for item in message["content"])
    )
    usage = await opened.harness.snapshot(UsageDoc, opened.root.id, context)
    await opened.harness.close(context)
    return SimpleNamespace(text=text, message=message, usage=usage)


class TestCodeModeModels:
    async def test_reads_the_model_catalog_without_credentials_in_headers(self) -> None:
        chat = modelsChat()
        done = await run(
            chat,
            """const classifiers = await models.getModelsOfType("classifier", "test");
            const one = await models.getModelOfType("classifier", "test", "judge");
            const available = await models.getAvailableOfType("image");
            return [classifiers.map((m) => m.id), one.id, "headers" in one, available.map((m) => m.id)];""",
        )
        assert json.loads(done.text) == [["judge"], "judge", False, ["painter"]]

    async def test_classifies_through_an_internal_tool_a_nested_call_with_its_usage_hidden_from_tools_and_listings(
        self,
    ) -> None:
        chat = modelsChat()
        done = await run(
            chat,
            """const model = await models.getModelOfType("classifier", "test", "judge");
            const result = await models.classify(model, {
                state: { text: "hi" },
                questions: { good: { type: "bool", instructions: "Is it good?", criteria: { true: "yes", false: "no" } } },
            });
            const found = (await searchTools("classify generate images models")).length;
            const described = (await describeTool("pi.codemode.models.classify")) !== undefined;
            return [result.stopReason, result.answers.good.probability === 1, Object.keys(tools).some((name) => name.includes("models")), ALL_TOOLS.some((tool) => tool.name.includes("models")), found, described];""",
        )
        assert json.loads(done.text) == ["stop", True, False, False, 0, False]
        assert chat.calls.classify == 1
        toMatchObject(done.message["details"]["calls"], [{"name": "models.classify", "status": "ok"}])
        # Counted once, under the internal tool, by the Harness.
        assert done.usage["tools"]["pi.codemode.models.classify"]["cost"]["total"] == pytest.approx(0.0012)

    async def test_resolves_a_model_call_that_failed_with_its_result_as_the_coding_agent_does(self) -> None:
        chat = modelsChat()
        done = await run(
            chat,
            """const model = await models.getModelOfType("classifier", "test", "judge");
            const result = await models.classify(model, {
                state: { fail: true },
                questions: { good: { type: "bool", instructions: "?", criteria: { true: "y", false: "n" } } },
            });
            return [result.stopReason, result.errorMessage];""",
        )
        assert json.loads(done.text) == ["error", "quota exceeded"]
        # The row still shows the failure.
        toMatchObject(done.message["details"]["calls"], [{"name": "models.classify", "status": "error"}])

    async def test_rejects_a_bad_classifier_context_with_the_expected_shape_before_any_request(self) -> None:
        chat = modelsChat()
        done = await run(
            chat,
            """const model = await models.getModelOfType("classifier", "test", "judge");
            try { await models.classify(model, { prompt: "x" }); } catch (error) { return error.message; }""",
        )
        assert "context.state must be an object" in done.text
        assert "Expected context:" in done.text
        assert chat.calls.classify == 0

    async def test_runs_at_most_four_model_calls_at_once(self) -> None:
        chat = modelsChat()
        chat.gate.open = deferred()
        chat.gate.atFour = True
        done = await run(
            chat,
            """const model = await models.getModelOfType("classifier", "test", "judge");
            const context = { state: {}, questions: { good: { type: "bool", instructions: "?", criteria: { true: "y", false: "n" } } } };
            return (await Promise.all(Array.from({ length: 8 }, () => models.classify(model, context)))).length;""",
        )
        assert done.text == "8"
        assert chat.calls.classify == 8
        assert chat.calls.maxInFlight == 4

    async def test_does_not_start_a_model_call_that_waited_for_a_slot_after_the_script_ended(self) -> None:
        chat = modelsChat()
        chat.gate.open = deferred()
        directory = tempfile.mkdtemp(prefix="pi-codemode-models-")

        # The result spills to a file after the script ended and before the tool returns. Then the running calls finish
        # and free their slots, which the two waiting calls would take.
        class SlowTempFiles(NodeExecutionEnv):
            async def createTempDir(self, prefix: str | None, ctx: Any) -> Any:
                chat.gate.open.resolve()
                await asyncio.sleep(0.1)
                return await super().createTempDir(prefix, ctx)

        created: list[str] = []
        try:
            done = await run(
                chat,
                """const model = await models.getModelOfType("classifier", "test", "judge");
                const context = { state: {}, questions: { good: { type: "bool", instructions: "?", criteria: { true: "y", false: "n" } } } };
                for (let i = 0; i < 6; i++) models.classify(model, context).catch(() => {});
                // A journaled call after them: by its result, the first four have been launched.
                await models.getModelsOfType("image");
                return "x".repeat(60000);""",
                SlowTempFiles({"cwd": directory}),
            )
            assert "[Full output: " in done.text
            created.append(done.message["details"]["fullOutputPath"])
            assert chat.calls.classify == 4
        finally:
            shutil.rmtree(directory, ignore_errors=True)
            for path in created:
                shutil.rmtree(path.rsplit("/", 1)[0], ignore_errors=True)

    async def test_notes_generated_images_the_script_did_not_show(self) -> None:
        chat = modelsChat()
        hidden = await run(
            chat,
            """const model = await models.getModelOfType("image", "test", "painter");
            const result = await models.generateImages(model, { input: [{ type: "text", text: "a cat" }] });
            return result.output.length;""",
        )
        assert "Note: models.generateImages() returned 1 image that the script did not show" in hidden.text
        shown = await run(
            modelsChat(),
            """const model = await models.getModelOfType("image", "test", "painter");
            const result = await models.generateImages(model, { input: [{ type: "text", text: "a cat" }] });
            image(result.output[0]);""",
        )
        assert "Note:" not in shown.text
        assert any(item["type"] == "image" for item in shown.message["content"])

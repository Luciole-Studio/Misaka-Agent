"""Translated from pi's ``packages/coding-agent/test/extensions-input-event.test.ts`` (v1.1.0).

pi's extensions are TypeScript modules; these are the same handlers as Python modules, and the shared ``testVar`` that
pi keeps on ``globalThis`` lives on ``builtins``.
"""

from __future__ import annotations

import builtins
import shutil
from pathlib import Path
from typing import Any

import pytest

from misaka.ai.types import ImageContent
from misaka.core.auth_storage import AuthStorage
from misaka.core.extensions.loader import discoverAndLoadExtensions
from misaka.core.extensions.runner import ExtensionRunner
from misaka.core.session_manager import SessionManager
from misaka.utils.values import read_field
from tests.model_runtime_test_utils import createInMemoryModelRegistry

UNSET = object()


def readTestVar() -> Any:
    return builtins.__dict__.get("testVar", UNSET)


@pytest.fixture(autouse=True)
def cleanTestVar():
    # Clean globalThis test vars
    builtins.__dict__.pop("testVar", None)
    yield
    builtins.__dict__.pop("testVar", None)


def extension(handler: str) -> str:
    """A module whose factory registers ``handler`` (an ``async def h(e, ctx)`` body) for ``input``."""
    body = "\n".join(f"        {line}" for line in handler.splitlines())
    return f"import builtins\n\n\ndef default(p):\n    async def h(e, ctx):\n{body}\n\n    p.on(\"input\", h)\n"


@pytest.fixture
def createRunner(tmp_path: Path) -> Any:
    extensionsDir = tmp_path / "extensions"
    extensionsDir.mkdir()

    async def create(*extensions: str) -> ExtensionRunner:
        # Clear and recreate extensions dir for clean state
        shutil.rmtree(extensionsDir, ignore_errors=True)
        extensionsDir.mkdir()
        for index, source in enumerate(extensions):
            (extensionsDir / f"e{index}.py").write_text(source, encoding="utf-8")
        result = await discoverAndLoadExtensions([], str(tmp_path), str(tmp_path))
        sm = SessionManager.inMemory()
        mr = await createInMemoryModelRegistry(AuthStorage.inMemory())
        return ExtensionRunner(result.extensions, result.runtime, str(tmp_path), sm, mr)

    return create


def result(value: Any) -> dict[str, Any]:
    keys = ("action", "text", "images")
    return {key: read_field(value, key) for key in keys if key == "action" or read_field(value, "action") == "transform"}


async def test_returns_continue_when_no_handlers_undefined_return_or_explicit_continue(createRunner: Any):
    # No handlers
    assert read_field(await (await createRunner()).emitInput("x", None, "interactive"), "action") == "continue"
    # Returns undefined
    r = await createRunner(extension("return None"))
    assert read_field(await r.emitInput("x", None, "interactive"), "action") == "continue"
    # Returns explicit continue
    r = await createRunner(extension('return {"action": "continue"}'))
    assert read_field(await r.emitInput("x", None, "interactive"), "action") == "continue"


async def test_transforms_text_and_preserves_images_when_omitted(createRunner: Any):
    r = await createRunner(extension('return {"action": "transform", "text": "T:" + e["text"]}'))
    imgs = [ImageContent(data="orig", mimeType="image/png")]
    assert result(await r.emitInput("hi", imgs, "interactive")) == {"action": "transform", "text": "T:hi", "images": imgs}


async def test_transforms_and_replaces_images_when_provided(createRunner: Any):
    r = await createRunner(extension(
        'return {"action": "transform", "text": "X", "images": [{"type": "image", "data": "new", "mimeType": "image/jpeg"}]}'
    ))
    outcome = await r.emitInput("hi", [ImageContent(data="orig", mimeType="image/png")], "interactive")
    assert read_field(outcome, "action") == "transform"
    assert read_field(outcome, "text") == "X"
    assert [
        (read_field(image, "type"), read_field(image, "data"), read_field(image, "mimeType"))
        for image in read_field(outcome, "images")
    ] == [("image", "new", "image/jpeg")]


async def test_chains_transforms_across_multiple_handlers(createRunner: Any):
    r = await createRunner(
        extension('return {"action": "transform", "text": e["text"] + "[1]"}'),
        extension('return {"action": "transform", "text": e["text"] + "[2]"}'),
    )
    assert result(await r.emitInput("X", None, "interactive")) == {"action": "transform", "text": "X[1][2]", "images": None}


async def test_short_circuits_on_handled_and_skips_subsequent_handlers(createRunner: Any):
    builtins.__dict__["testVar"] = False
    r = await createRunner(
        extension('return {"action": "handled"}'),
        extension('builtins.__dict__["testVar"] = True'),
    )
    assert result(await r.emitInput("X", None, "interactive")) == {"action": "handled"}
    assert readTestVar() is False


async def test_passes_source_correctly_for_all_source_types(createRunner: Any):
    r = await createRunner(extension('builtins.__dict__["testVar"] = e["source"]\nreturn {"action": "continue"}'))
    for source in ["interactive", "rpc", "extension"]:
        await r.emitInput("x", None, source)
        assert readTestVar() == source


async def test_passes_streaming_behavior_correctly(createRunner: Any):
    r = await createRunner(extension(
        'builtins.__dict__["testVar"] = e.get("streamingBehavior")\nreturn {"action": "continue"}'
    ))
    await r.emitInput("x", None, "interactive", "steer")
    assert readTestVar() == "steer"
    await r.emitInput("x", None, "interactive", "followUp")
    assert readTestVar() == "followUp"
    await r.emitInput("x", None, "interactive")
    assert readTestVar() is None


async def test_catches_handler_errors_and_continues(createRunner: Any):
    r = await createRunner(extension('raise RuntimeError("boom")'))
    errs: list[str] = []
    r.onError(lambda error: errs.append(read_field(error, "error")))
    assert read_field(await r.emitInput("x", None, "interactive"), "action") == "continue"
    assert "boom" in errs


async def test_has_handlers_returns_correct_value(createRunner: Any):
    r = await createRunner()
    assert r.hasHandlers("input") is False
    r = await createRunner(extension("return None"))
    assert r.hasHandlers("input") is True

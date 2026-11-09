"""Translated from pi's ``packages/coding-agent/test/image-resize-callers.test.ts`` and ``http-dispatcher.test.ts``
(v1.1.0).

MISAKA fork: the two "http dispatcher" cases (undici's global dispatcher tunnelling through a CONNECT proxy and its
HTTPS connection attempt timeout) are not translated; MISAKA's HTTP clients are httpx, which reads the proxy variables
these settings set.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.types import Model
from misaka.cli.file_processor import ProcessFileOptions, processFileArguments
from misaka.core.http_dispatcher import applyHttpProxySettings
from misaka.core.tools.read import createReadTool, createReadToolDefinition
from misaka.utils import image_process
from misaka.utils.values import read_field

TINY_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
)


@pytest.fixture
def resizeImage(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    mock = SimpleNamespace(calls=[])

    async def resize(*args: Any) -> None:
        mock.calls.append(args)

    monkeypatch.setattr(image_process, "resizeImage", resize)
    monkeypatch.setattr(image_process, "formatDimensionNote", lambda *_: None)
    return mock


@pytest.fixture
def imagePath(tmp_path: Path) -> Path:
    path = tmp_path / "test.png"
    path.write_bytes(base64.b64decode(TINY_PNG_BASE64))
    return path


# --- image resize callers -----------------------------------------------------------------------


async def test_read_tool_returns_text_only_output_when_auto_resize_cannot_produce_a_safe_image(
    resizeImage: SimpleNamespace, imagePath: Path
):
    result = await createReadTool(str(imagePath.parent)).execute("test-read-image", {"path": str(imagePath)}, None, None)

    assert len(result.content) == 1
    assert read_field(result.content[0], "type") == "text"
    assert "Image omitted" in read_field(result.content[0], "text")


async def test_file_processor_omits_image_attachments_when_auto_resize_cannot_produce_a_safe_image(
    resizeImage: SimpleNamespace, imagePath: Path
):
    result = await processFileArguments([str(imagePath)])

    assert result.images == []
    assert "Image omitted" in result.text


async def test_passes_the_current_model_resize_profile_to_the_read_tool(resizeImage: SimpleNamespace, imagePath: Path):
    resize = {"maxWidth": 1234, "maxHeight": 1000, "maxBytes": 500000, "jpegQuality": 70}
    model = Model.model_validate({
        "id": "vision-model",
        "name": "Vision model",
        "api": "test",
        "provider": "test",
        "baseUrl": "https://example.com",
        "reasoning": False,
        "input": ["text", "image"],
        "inputLimits": {"images": {"resize": resize}},
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 1000,
        "maxTokens": 100,
    })
    ctx = SimpleNamespace(cwd=str(imagePath.parent), model=model)

    await createReadToolDefinition(str(imagePath.parent)).execute(
        "test-read-model-profile", {"path": str(imagePath)}, None, None, ctx
    )

    assert len(resizeImage.calls) == 1
    inputBytes, mimeType, options = resizeImage.calls[0]
    assert isinstance(inputBytes, bytes | bytearray | memoryview)
    assert mimeType == "image/png"
    assert {key: read_field(options, key) for key in resize} == resize


async def test_can_defer_resizing_file_attachments_until_prompt_dispatch(resizeImage: SimpleNamespace, imagePath: Path):
    result = await processFileArguments([str(imagePath)], ProcessFileOptions(autoResizeImages=False))

    assert len(result.images) == 1
    assert resizeImage.calls == []


# --- http proxy settings ------------------------------------------------------------------------


@pytest.fixture
def proxyEnv():
    # applyHttpProxySettings writes os.environ itself, so restore both variables whatever the test set.
    saved = {key: os.environ.pop(key, None) for key in ("HTTP_PROXY", "HTTPS_PROXY")}
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_applies_http_proxy_to_http_proxy_and_https_proxy(proxyEnv: None):
    applyHttpProxySettings("http://127.0.0.1:7890")

    assert os.environ["HTTP_PROXY"] == "http://127.0.0.1:7890"
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:7890"


def test_does_not_override_existing_proxy_env_vars(proxyEnv: None, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HTTP_PROXY", "http://env-http:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://env-https:8080")

    applyHttpProxySettings("http://settings:7890")

    assert os.environ["HTTP_PROXY"] == "http://env-http:8080"
    assert os.environ["HTTPS_PROXY"] == "http://env-https:8080"


def test_ignores_empty_values(proxyEnv: None):
    applyHttpProxySettings("   ")

    assert "HTTP_PROXY" not in os.environ
    assert "HTTPS_PROXY" not in os.environ

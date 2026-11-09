"""Ported from pi's ``packages/coding-agent/test/llama-extension.test.ts`` (v1.1.0): the cases test_pi_099_port.py does
not already carry. Like upstream, a real local HTTP server answers the client."""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from misaka.ai.auth.types import ApiKeyCredential, AuthResult, ModelAuth
from misaka.ai.models import RefreshModelsContext
from misaka.core.event_bus import createEventBus
from misaka.core.extensions.loader import (
    createExtensionRuntime,
    loadExtensionFromFactory,
)
from misaka.extensions.llama import register as llamaExtension
from misaka.extensions.llama.client import (
    LlamaClient,
    _modelInfo,
    normalizeLlamaServerUrl,
)
from misaka.extensions.llama.huggingface import HuggingFaceClient, findHuggingFaceToken
from misaka.extensions.llama.provider import LLAMA_PROVIDER_ID, createLlamaProvider
from misaka.utils.js import AbortController


class _Server:
    def __init__(self, handler) -> None:
        test_handler = handler

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:
                pass

            def _handle(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                self.body = self.rfile.read(length) if length else b""
                test_handler(self)

            do_GET = _handle
            do_POST = _handle

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def listen():
    servers: list[_Server] = []

    def start(handler) -> str:
        server = _Server(handler)
        servers.append(server)
        return server.url

    yield start
    for server in servers:
        server.close()


def json_(request: BaseHTTPRequestHandler, value: Any) -> None:
    body = json.dumps(value).encode()
    request.send_response(200)
    request.send_header("Content-Type", "application/json")
    request.send_header("Content-Length", str(len(body)))
    request.end_headers()
    request.wfile.write(body)


def notFound(request: BaseHTTPRequestHandler) -> None:
    request.send_response(404)
    request.send_header("Content-Length", "0")
    request.end_headers()


def _sse(request: BaseHTTPRequestHandler, streams: set, closed: threading.Event) -> None:
    """Hold an event stream open until the test is done; `send` writes to every open stream."""
    request.send_response(200)
    request.send_header("Content-Type", "text/event-stream")
    request.send_header("Cache-Control", "no-cache")
    request.end_headers()
    streams.add(request)
    try:
        while not closed.wait(0.05):
            try:
                request.wfile.flush()
            except OSError:
                break
    finally:
        streams.discard(request)


def _sender(streams: set):
    lock = threading.Lock()

    def send(event: Any) -> None:
        payload = f"data: {json.dumps(event)}\n\n".encode()
        with lock:
            for request in list(streams):
                try:
                    request.wfile.write(payload)
                    request.wfile.flush()
                except OSError:
                    streams.discard(request)

    return send


async def test_registers_a_native_provider_and_llama_command():
    runtime = createExtensionRuntime()
    extension = await loadExtensionFromFactory(llamaExtension, os.getcwd(), createEventBus(), runtime, "builtin:llama.cpp")

    assert extension.commands.get("llama").description == "Manage llama.cpp router models"
    assert [entry.provider.id for entry in runtime.pendingNativeProviderRegistrations] == [LLAMA_PROVIDER_ID]


def test_normalizes_management_and_inference_urls():
    assert normalizeLlamaServerUrl("http://127.0.0.1:8080/v1/") == "http://127.0.0.1:8080"
    assert normalizeLlamaServerUrl("https://example.com/prefix/v1") == "https://example.com/prefix"
    with pytest.raises(Exception, match="http or https"):
        normalizeLlamaServerUrl("file:///tmp/llama")


def test_exposes_loaded_and_sleeping_models_with_router_metadata():
    controller = createLlamaProvider()
    # The catalog arrives parsed (`LlamaModelInfo`), as the client hands it over.
    controller.setCatalog(
        [_modelInfo(entry) for entry in [
            {
                "id": "loaded",
                "status": {"value": "loaded", "args": ["llama-server", "--n-gpu-layers", "999"]},
                "architecture": {"input_modalities": ["text", "image"]},
                "meta": {"n_ctx": 65536, "n_ctx_train": 131072},
            },
            {"id": "sleeping", "status": {"value": "sleeping"}},
            {"id": "unloaded", "status": {"value": "unloaded"}},
            {"id": "loading", "status": {"value": "loading"}},
        ]],
        "http://localhost:8080",
    )

    models = controller.provider.getModels()
    assert [(m.id, m.baseUrl) for m in models] == [
        ("loaded", "http://localhost:8080/v1"),
        ("sleeping", "http://localhost:8080/v1"),
    ]
    assert (models[0].contextWindow, models[0].maxTokens, list(models[0].input)) == (65536, 65536, ["text", "image"])


async def _refresh(controller, url: str) -> None:
    async def publish(publication):
        if publication.update:
            publication.update()
        return True

    await controller.provider.refreshModels(RefreshModelsContext(
        credential=ApiKeyCredential(key="local", env={"LLAMA_BASE_URL": url}),
        stored=None,
        publish=publish,
        allowNetwork=True,
        signal=AbortController().signal,
    ))


async def test_hides_unloaded_presets_when_router_autoload_is_disabled(listen):
    def handler(request):
        if request.path == "/models":
            json_(request, {"data": [{"id": "preset", "status": {"value": "unloaded"}, "source": "preset"}]})
            return
        if request.path == "/props":
            json_(request, {"role": "router", "models_autoload": False})
            return
        notFound(request)

    url = listen(handler)
    controller = createLlamaProvider()
    await _refresh(controller, url)

    assert controller.provider.getModels() == []


async def test_stays_dormant_until_configured_and_stores_url_plus_optional_key(listen):
    provider = createLlamaProvider().provider
    auth = provider.auth.apiKey

    async def env(_name):
        return None

    async def fileExists(_path):
        return False

    emptyContext = SimpleNamespace(env=env, fileExists=fileExists)
    signal = AbortController().signal
    assert await auth.check(ctx=emptyContext, signal=signal) is None
    assert await auth.resolve(ctx=emptyContext, signal=signal) is None

    authorizations: list = []

    def handler(request):
        authorizations.append(request.headers.get("authorization"))
        json_(request, {"data": []})

    url = listen(handler)
    answers = [url, "secret"]

    async def prompt(_prompt):
        return answers.pop(0)

    credential = await auth.login(SimpleNamespace(signal=signal, prompt=prompt, notify=lambda _event: None))
    assert credential == ApiKeyCredential(type="api_key", key="secret", env={"LLAMA_BASE_URL": url})
    assert authorizations and all(value == "Bearer secret" for value in authorizations)
    assert await auth.resolve(ctx=emptyContext, credential=credential, signal=signal) == AuthResult(
        auth=ModelAuth(apiKey="secret", baseUrl=f"{url}/v1"),
        env={"LLAMA_BASE_URL": url},
        source="stored credential",
    )


async def test_searches_hugging_face_and_reads_quantizations_plus_access_requirements(listen):
    seen: list = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        if request.path.startswith("/api/models?"):
            query = parse_qs(urlsplit(request.path).query)
            assert query["search"] == ["qwen coder"]
            assert query["filter"] == ["gguf"]
            assert query["sort"] == ["downloads"]
            json_(request, [{"id": "owner/model-GGUF", "downloads": 1200}])
            return
        if request.path == "/api/models/owner/model-GGUF?blobs=true":
            json_(request, {
                "id": "owner/model-GGUF",
                "gated": "manual",
                "siblings": [
                    {"rfilename": "model-Q5_K_M.gguf", "size": 6000},
                    {"rfilename": "model-Q4_K_M-00001-of-00002.gguf", "size": 2000},
                    {"rfilename": "model-Q4_K_M-00002-of-00002.gguf", "size": 3000},
                    {"rfilename": "mmproj-F16.gguf", "size": 1000},
                ],
            })
            return
        notFound(request)

    url = listen(handler)
    client = HuggingFaceClient("hf-secret", url)

    assert [(m.id, m.downloads) for m in await client.search("qwen coder")] == [("owner/model-GGUF", 1200)]
    details = await client.details("owner/model-GGUF")
    assert (details.id, details.gated) == ("owner/model-GGUF", "manual")
    assert [(q.name, q.size) for q in details.quantizations] == [("Q4_K_M", 5000), ("Q5_K_M", 6000)]
    assert await findHuggingFaceToken({"HF_TOKEN": " hf-secret "}) == "hf-secret"
    assert seen and all(value == "Bearer hf-secret" for value in seen)


async def test_loads_with_sse_progress_and_waits_for_the_loaded_catalog_state(listen):
    state = {"status": "unloaded"}
    streams: set = set()
    closed = threading.Event()
    send = _sender(streams)

    def later() -> None:
        time.sleep(0.02)
        send({
            "model": "test-model",
            "event": "status_change",
            "data": {
                "status": "loading",
                "progress": {"stages": ["text_model", "mmproj_model"], "current": "text_model", "value": 0.5},
            },
        })
        state["status"] = "loaded"
        send({"model": "test-model", "event": "status_change", "data": {"status": "loaded"}})

    def handler(request):
        if request.path == "/models/sse":
            _sse(request, streams, closed)
            return
        if request.path == "/models/load" and request.command == "POST":
            state["status"] = "loading"
            json_(request, {"success": True})
            threading.Thread(target=later, daemon=True).start()
            return
        if request.path == "/models":
            json_(request, {"data": [{"id": "test-model", "status": {"value": state["status"]}}]})
            return
        notFound(request)

    url = listen(handler)
    progress: list[str] = []
    try:
        model = await LlamaClient(url).loadAndWait("test-model", lambda entry: progress.append(entry.message))
    finally:
        closed.set()
    assert model.status.value == "loaded"
    assert "Loading text model" in progress


async def test_downloads_with_byte_progress_and_returns_the_refreshed_catalog(listen):
    state = {"status": "missing"}
    streams: set = set()
    closed = threading.Event()
    send = _sender(streams)

    def later() -> None:
        time.sleep(0.02)
        send({
            "model": "owner/repo:Q4_K_M",
            "event": "download_progress",
            "data": {"progress": {"https://example/model.gguf": {"done": 512, "total": 1024}}},
        })
        state["status"] = "unloaded"
        send({"model": "owner/repo:Q4_K_M", "event": "download_finished", "data": {}})

    def handler(request):
        if request.path == "/models/sse":
            _sse(request, streams, closed)
            return
        if request.path == "/models" and request.command == "POST":
            state["status"] = "downloading"
            json_(request, {"success": True})
            threading.Thread(target=later, daemon=True).start()
            return
        if request.path.startswith("/models"):
            json_(request, {
                "data": [] if state["status"] == "missing" else [{"id": "owner/repo:Q4_K_M", "status": {"value": state["status"]}}],
            })
            return
        notFound(request)

    url = listen(handler)
    progress: list = []
    try:
        models = await LlamaClient(url).downloadAndWait("owner/repo:Q4_K_M", progress.append)
    finally:
        closed.set()
    assert [(m.id, m.status.value) for m in models] == [("owner/repo:Q4_K_M", "unloaded")]
    assert any(
        (entry.message, entry.ratio, entry.detail) == ("Downloading model", 0.5, "512 B / 1.00 KiB") for entry in progress
    )

"""llama.cpp router client.

Translated from pi's ``coding-agent/src/extensions/llama/client.ts``.

MISAKA fork: a catalog entry is parsed into the dataclasses below once ``isModelInfo`` accepts it, where upstream
keeps the JSON object itself.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from misaka.utils.js import (
    UNDEFINED,
    AbortController,
    AbortSignal,
    Utf8Decoder,
    addAbortListener,
    isNumber,
    jsonParse,
    jsonStringify,
    jsString,
    jsTrim,
    jsTrimStart,
    spawn,
    toFixed,
)
from misaka.utils.values import signal_aborted
from misaka.utils.whatwg_fetch import RequestInit, fetch
from misaka.utils.whatwg_url import URL, URLSearchParams

# MISAKA fork: asyncio holds tasks weakly; `void this.watch(...)` keeps its task here until it settles.
_background: set[asyncio.Task[Any]] = set()


@dataclass(slots=True)
class LlamaModelStatusInfo:
    value: str
    args: list[str] | None = None
    failed: bool | None = None
    exit_code: int | float | None = None
    progress: dict[str, Any] | None = None


@dataclass(slots=True)
class LlamaArchitecture:
    input_modalities: list[str] | None = None
    output_modalities: list[str] | None = None


@dataclass(slots=True)
class LlamaModelMeta:
    n_ctx: int | float | None = None
    n_ctx_train: int | float | None = None
    size: int | float | None = None
    ftype: str | None = None


@dataclass(slots=True)
class LlamaModelInfo:
    id: str
    status: LlamaModelStatusInfo
    aliases: list[str] | None = None
    architecture: LlamaArchitecture | None = None
    source: str | None = None
    meta: LlamaModelMeta | None = None


@dataclass(slots=True)
class LlamaServerProps:
    models_autoload: bool | None = None
    chat_template: str | None = None


@dataclass(slots=True)
class LlamaModelEvent:
    model: str
    event: str
    data: Any = None


@dataclass(slots=True)
class LlamaProgress:
    """``ratio`` and ``detail`` are ``UNDEFINED`` when the progress leaves them out, and ``None`` when it carries them
    undefined; ``Object.assign`` copies only the fields that are there."""

    message: str
    ratio: Any = UNDEFINED
    detail: Any = UNDEFINED


def errorMessage(payload: Any, fallback: str) -> str:
    if not isinstance(payload, Mapping):
        return fallback
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return fallback
    message = error.get("message")
    return message if isinstance(message, str) and message else fallback


def isModelInfo(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    status = value.get("status")
    return isinstance(value.get("id"), str) and isinstance(status, Mapping) and isinstance(status.get("value"), str)


def _strings(value: Any) -> list[str] | None:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else None


def _number(value: Any) -> int | float | None:
    return value if isNumber(value) else None


def _modelInfo(value: Mapping[str, Any]) -> LlamaModelInfo:
    status = value["status"]
    architecture = value.get("architecture")
    meta = value.get("meta")
    progress = status.get("progress")
    return LlamaModelInfo(
        id=value["id"],
        aliases=_strings(value.get("aliases")),
        status=LlamaModelStatusInfo(
            value=status["value"],
            args=_strings(status.get("args")),
            failed=status.get("failed") if isinstance(status.get("failed"), bool) else None,
            exit_code=_number(status.get("exit_code")),
            progress=dict(progress) if isinstance(progress, Mapping) else None,
        ),
        architecture=(
            LlamaArchitecture(
                input_modalities=_strings(architecture.get("input_modalities")),
                output_modalities=_strings(architecture.get("output_modalities")),
            )
            if isinstance(architecture, Mapping)
            else None
        ),
        source=value.get("source") if isinstance(value.get("source"), str) else None,
        meta=(
            LlamaModelMeta(
                n_ctx=_number(meta.get("n_ctx")),
                n_ctx_train=_number(meta.get("n_ctx_train")),
                size=_number(meta.get("size")),
                ftype=meta.get("ftype") if isinstance(meta.get("ftype"), str) else None,
            )
            if isinstance(meta, Mapping)
            else None
        ),
    )


def linkSignal(source: Any, target: AbortController) -> Callable[[], None]:
    if not source:
        return lambda: None
    if signal_aborted(source):
        target.abort(getattr(source, "reason", UNDEFINED))
        return lambda: None

    def abort() -> None:
        target.abort(getattr(source, "reason", UNDEFINED))

    return addAbortListener(source, abort)


async def sleep(ms: float, signal: Any = None) -> None:
    if signal_aborted(signal):
        raise _cancelled(signal)
    future: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def abort() -> None:
        timeout.cancel()
        if not future.done():
            future.set_exception(_cancelled(signal))

    def resolve() -> None:
        remove()
        if not future.done():
            future.set_result(None)

    timeout = asyncio.get_running_loop().call_later(ms / 1000, resolve)
    remove = addAbortListener(signal, abort) if signal is not None else (lambda: None)
    try:
        await future
    finally:
        # MISAKA fork: a cancelled sleep drops its timer and listener; a promise cannot be cancelled.
        timeout.cancel()
        remove()


def _cancelled(signal: Any) -> BaseException:
    """``signal.reason ?? new Error("Cancelled")``."""
    reason = getattr(signal, "reason", None)
    return reason if isinstance(reason, BaseException) else RuntimeError("Cancelled")


def parseLoadProgress(data: Any) -> LlamaProgress | None:
    if not isinstance(data, Mapping):
        return None
    progress = data.get("progress")
    if not isinstance(progress, Mapping):
        return None
    value = progress
    stage = (
        value.get("current")
        if isinstance(value.get("current"), str)
        else value.get("stage")
        if isinstance(value.get("stage"), str)
        else None
    )
    stages = [entry for entry in value.get("stages") if isinstance(entry, str)] if isinstance(value.get("stages"), list) else []
    stageRatio = max(0, min(1, value["value"])) if isNumber(value.get("value")) else None
    ratio = stageRatio
    if stage and len(stages) > 0:
        index = stages.index(stage) if stage in stages else -1
        if index >= 0:
            ratio = (index + (stageRatio if stageRatio is not None else 0)) / len(stages)
    return LlamaProgress(
        message=f"Loading {stage.replace('_', ' ')}" if stage else "Loading model",
        ratio=ratio,
    )


def parseDownloadProgress(data: Any) -> LlamaProgress | None:
    if not isinstance(data, Mapping):
        return None
    nested = data.get("progress")
    files = nested if isinstance(nested, Mapping) else data
    done = 0
    total = 0
    for value in files.values():
        if not isinstance(value, Mapping):
            continue
        entry = value
        if not isNumber(entry.get("done")) or not isNumber(entry.get("total")):
            continue
        done += entry["done"]
        total += entry["total"]
    if total <= 0:
        return None
    return LlamaProgress(
        message="Downloading model",
        ratio=done / total,
        detail=f"{formatBytes(done)} / {formatBytes(total)}",
    )


def formatBytes(bytes: float) -> str:
    if bytes < 1024:
        return f"{jsString(bytes)} B"
    units = ["KiB", "MiB", "GiB", "TiB"]
    value = bytes / 1024
    unit = units[0]
    index = 1
    while index < len(units) and value >= 1024:
        value /= 1024
        unit = units[index]
        index += 1
    return f"{toFixed(value, 1) if value >= 10 else toFixed(value, 2)} {unit}"


def normalizeLlamaServerUrl(value: str) -> str:
    url = URL(jsTrim(value))
    if url.protocol != "http:" and url.protocol != "https:":
        raise ValueError("Server URL must use http or https")
    url.hash = ""
    url.search = ""
    url.pathname = re.sub(r"/v1\Z", "", re.sub(r"/+\Z", "", url.pathname)) or "/"
    return re.sub(r"/\Z", "", url.toString())


def llamaInferenceUrl(serverUrl: str) -> str:
    return f"{normalizeLlamaServerUrl(serverUrl)}/v1"


class LlamaClient:
    def __init__(self, serverUrl: str, apiKey: str | None = None) -> None:
        self.serverUrl = normalizeLlamaServerUrl(serverUrl)
        self.apiKey = apiKey

    async def request(self, path: str, init: RequestInit | None = None) -> Any:
        init = init if init is not None else {}
        headers = httpx.Headers(init.get("headers"))
        if init.get("body") is not None:
            headers["Content-Type"] = "application/json"
        if self.apiKey:
            headers["Authorization"] = f"Bearer {self.apiKey}"
        timeout = AbortSignal.timeout(15_000)
        signal = AbortSignal.any([init["signal"], timeout]) if init.get("signal") else timeout
        response = await fetch(f"{self.serverUrl}{path}", {**init, "headers": headers, "signal": signal})
        payload: Any
        try:
            payload = await response.json()
        except Exception:  # noqa: BLE001
            payload = None
        if not response.ok:
            raise RuntimeError(errorMessage(payload, f"llama.cpp returned HTTP {response.status}"))
        return payload

    async def list(self, options: Mapping[str, Any] | None = None) -> list[LlamaModelInfo]:
        options = options if options is not None else {}
        payload = await self.request(f"/models{'?reload=1' if options.get('reload') else ''}", {"signal": options.get("signal")})
        if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
            raise RuntimeError("llama.cpp returned an invalid model catalog")  # noqa: TRY004
        data = payload["data"]
        if not all(isModelInfo(value) for value in data):
            raise RuntimeError("Server is not running in llama.cpp router mode")
        return [_modelInfo(value) for value in data]

    async def props(self, options: Mapping[str, Any] | None = None) -> LlamaServerProps:
        options = options if options is not None else {}
        query = f"?{URLSearchParams({'model': options['model'], 'autoload': 'false'})}" if options.get("model") else ""
        payload = await self.request(f"/props{query}", {"signal": options.get("signal")})
        if not isinstance(payload, Mapping):
            return LlamaServerProps()
        modelsAutoload = payload.get("models_autoload")
        chatTemplate = payload.get("chat_template")
        return LlamaServerProps(
            models_autoload=modelsAutoload if isinstance(modelsAutoload, bool) else None,
            chat_template=chatTemplate if isinstance(chatTemplate, str) else None,
        )

    async def load(self, model: str, signal: Any = None) -> None:
        await self.request("/models/load", {"method": "POST", "body": jsonStringify({"model": model}), "signal": signal})

    async def unload(self, model: str, signal: Any = None) -> None:
        await self.request("/models/unload", {"method": "POST", "body": jsonStringify({"model": model}), "signal": signal})

    async def unloadAndWait(self, model: str, signal: Any = None) -> None:
        await self.unload(model, signal)
        while True:
            entry = next((candidate for candidate in await self.list({"signal": signal}) if candidate.id == model), None)
            if not entry or entry.status.value == "unloaded":
                return
            await sleep(100, signal)

    async def download(self, model: str, signal: Any = None) -> None:
        await self.request("/models", {"method": "POST", "body": jsonStringify({"model": model}), "signal": signal})

    async def watch(self, onEvent: Callable[[LlamaModelEvent], None], signal: Any = None) -> None:
        headers: dict[str, str] = {}
        if self.apiKey:
            headers["Authorization"] = f"Bearer {self.apiKey}"
        response = await fetch(f"{self.serverUrl}/models/sse", {"headers": headers, "signal": signal})
        if not response.ok or not response.body:
            raise RuntimeError(f"llama.cpp SSE returned HTTP {response.status}")
        reader = response.body
        decoder = Utf8Decoder()
        buffer = ""
        async for chunk in reader:
            buffer += decoder.decode(chunk).replace("\r\n", "\n")
            boundary = buffer.find("\n\n")
            while boundary >= 0:
                frame = buffer[:boundary]
                buffer = buffer[boundary + 2 :]
                data = "\n".join(jsTrimStart(line[5:]) for line in frame.split("\n") if line.startswith("data:"))
                if data:
                    try:
                        event = jsonParse(data)
                        if (
                            isinstance(event, Mapping)
                            and isinstance(event.get("model"), str)
                            and isinstance(event.get("event"), str)
                        ):
                            onEvent(LlamaModelEvent(model=event["model"], event=event["event"], data=event.get("data")))
                    except Exception:  # noqa: BLE001, S110
                        # Ignore malformed events; catalog polling remains authoritative.
                        pass
                boundary = buffer.find("\n\n")

    async def loadAndWait(
        self,
        model: str,
        onProgress: Callable[[LlamaProgress], None],
        signal: Any = None,
    ) -> LlamaModelInfo:
        watcher = AbortController()
        unlink = linkSignal(signal, watcher)
        eventLoaded = False
        eventError: str | None = None

        def onEvent(event: LlamaModelEvent) -> None:
            nonlocal eventLoaded, eventError
            if event.model != model:
                return
            if event.event != "model_status" and event.event != "status_change":
                return
            data = event.data
            status = data.get("status") if isinstance(data, Mapping) else None
            if status == "loaded":
                eventLoaded = True
            if status == "unloaded":
                eventError = "Model failed to load"
            progress = parseLoadProgress(event.data)
            if progress:
                onProgress(progress)

        spawn(self.watch(onEvent, watcher.signal), _background)
        try:
            await self.load(model, signal)
            onProgress(LlamaProgress(message="Loading model"))
            while True:
                if signal_aborted(signal):
                    raise _cancelled(signal)
                entry = next((candidate for candidate in await self.list({"signal": signal}) if candidate.id == model), None)
                if entry is not None and entry.status.value == "loaded":
                    return entry
                if eventLoaded and not entry:
                    return LlamaModelInfo(id=model, status=LlamaModelStatusInfo(value="loaded"))
                if (entry is not None and entry.status.failed) or eventError:
                    raise RuntimeError(
                        (eventError if eventError is not None else "Model failed to load")
                        if entry is None or entry.status.exit_code is None
                        else f"Model exited with code {jsString(entry.status.exit_code)}"
                    )
                await sleep(250, signal)
        finally:
            unlink()
            watcher.abort()

    async def downloadAndWait(
        self,
        model: str,
        onProgress: Callable[[LlamaProgress], None],
        signal: Any = None,
    ) -> list[LlamaModelInfo]:
        watcher = AbortController()
        unlink = linkSignal(signal, watcher)
        finished = False
        failure: str | None = None
        sawDownloading = False
        polls = 0

        def onEvent(event: LlamaModelEvent) -> None:
            nonlocal finished, failure, sawDownloading
            if event.model != model:
                return
            if event.event == "download_finished":
                finished = True
            if event.event == "download_failed":
                failure = errorMessage(event.data, "Download failed")
            if event.event == "download_progress":
                sawDownloading = True
                progress = parseDownloadProgress(event.data)
                if progress:
                    onProgress(progress)

        spawn(self.watch(onEvent, watcher.signal), _background)
        try:
            await self.download(model, signal)
            onProgress(LlamaProgress(message="Downloading model"))
            while True:
                if signal_aborted(signal):
                    raise _cancelled(signal)
                if failure:
                    raise RuntimeError(failure)
                models = await self.list({"signal": signal})
                polls += 1
                entry = next((candidate for candidate in models if candidate.id == model), None)
                if entry is not None and entry.status.value == "downloading":
                    sawDownloading = True
                    progress = parseDownloadProgress(entry.status.progress)
                    if progress:
                        onProgress(progress)
                elif finished or (entry and (sawDownloading or polls >= 2)):
                    return await self.list({"reload": True, "signal": signal})
                await sleep(500, signal)
        finally:
            unlink()
            watcher.abort()


__all__ = [
    "LlamaArchitecture",
    "LlamaClient",
    "LlamaModelEvent",
    "LlamaModelInfo",
    "LlamaModelMeta",
    "LlamaModelStatusInfo",
    "LlamaProgress",
    "LlamaServerProps",
    "formatBytes",
    "llamaInferenceUrl",
    "normalizeLlamaServerUrl",
]

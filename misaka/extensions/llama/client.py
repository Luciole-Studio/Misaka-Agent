"""llama.cpp router client, translated from Pi's built-in extension."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from misaka.ai.utils.abort import sleep, throw_if_aborted, wait_for_abort

_REQUEST_TIMEOUT_SECONDS = 15


@dataclass(slots=True)
class LlamaModelStatusInfo:
    value: str
    args: list[str] | None = None
    failed: bool | None = None
    exit_code: int | None = None
    progress: dict[str, Any] | None = None


@dataclass(slots=True)
class LlamaArchitecture:
    input_modalities: list[str] | None = None
    output_modalities: list[str] | None = None


@dataclass(slots=True)
class LlamaModelMeta:
    n_ctx: int | None = None
    n_ctx_train: int | None = None
    size: int | None = None
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


@dataclass(slots=True)
class LlamaModelEvent:
    model: str
    event: str
    data: Any = None


@dataclass(slots=True)
class LlamaProgress:
    message: str
    ratio: float | None = None
    detail: str | None = None


def _error_message(payload: Any, fallback: str) -> str:
    if not isinstance(payload, Mapping):
        return fallback
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return fallback
    message = error.get("message")
    return message if isinstance(message, str) and message else fallback


def _strings(value: Any) -> list[str] | None:
    return (
        [item for item in value if isinstance(item, str)]
        if isinstance(value, list)
        else None
    )


def _integer(value: Any) -> int | None:
    return (
        int(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def _model_info(value: Any) -> LlamaModelInfo | None:
    if not isinstance(value, Mapping) or not isinstance(value.get("id"), str):
        return None
    status = value.get("status")
    if not isinstance(status, Mapping) or not isinstance(status.get("value"), str):
        return None
    architecture = value.get("architecture")
    meta = value.get("meta")
    progress = status.get("progress")
    return LlamaModelInfo(
        id=value["id"],
        aliases=_strings(value.get("aliases")),
        status=LlamaModelStatusInfo(
            value=status["value"],
            args=_strings(status.get("args")),
            failed=status.get("failed")
            if isinstance(status.get("failed"), bool)
            else None,
            exit_code=_integer(status.get("exit_code")),
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
                n_ctx=_integer(meta.get("n_ctx")),
                n_ctx_train=_integer(meta.get("n_ctx_train")),
                size=_integer(meta.get("size")),
                ftype=meta.get("ftype") if isinstance(meta.get("ftype"), str) else None,
            )
            if isinstance(meta, Mapping)
            else None
        ),
    )


async def _await_request(operation: Any, signal: Any) -> Any:
    task = asyncio.ensure_future(operation)
    aborting: asyncio.Task[None] | None = None
    try:
        throw_if_aborted(signal)
        if signal is None:
            return await task
        aborting = asyncio.create_task(wait_for_abort(signal))
        done, _ = await asyncio.wait(
            {task, aborting}, return_when=asyncio.FIRST_COMPLETED
        )
        if aborting in done:
            throw_if_aborted(signal)
            raise RuntimeError("Request was aborted")
        throw_if_aborted(signal)
        return task.result()
    finally:
        children = [task]
        if aborting is not None:
            children.append(aborting)
        for child in children:
            if not child.done():
                child.cancel()
        await asyncio.gather(*children, return_exceptions=True)


@asynccontextmanager
async def _http_client(
    timeout: httpx.Timeout | None,
) -> AsyncIterator[httpx.AsyncClient]:
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    try:
        yield client
    except BaseException:
        try:
            await client.aclose()
        except BaseException:  # noqa: BLE001,S110 - preserve the request error
            pass
        raise
    else:
        await client.aclose()


def _parse_load_progress(data: Any) -> LlamaProgress | None:
    if not isinstance(data, Mapping) or not isinstance(data.get("progress"), Mapping):
        return None
    value = data["progress"]
    stage = (
        value.get("current")
        if isinstance(value.get("current"), str)
        else value.get("stage")
    )
    stage = stage if isinstance(stage, str) else None
    stages = _strings(value.get("stages")) or []
    raw_ratio = value.get("value")
    stage_ratio = (
        max(0.0, min(1.0, float(raw_ratio)))
        if isinstance(raw_ratio, (int, float)) and not isinstance(raw_ratio, bool)
        else None
    )
    ratio = stage_ratio
    if stage is not None and stages:
        try:
            index = stages.index(stage)
        except ValueError:
            pass
        else:
            ratio = (index + (stage_ratio or 0)) / len(stages)
    return LlamaProgress(
        message=f"Loading {stage.replace('_', ' ')}" if stage else "Loading model",
        ratio=ratio,
    )


def _parse_download_progress(data: Any) -> LlamaProgress | None:
    if not isinstance(data, Mapping):
        return None
    nested = data.get("progress")
    files = nested if isinstance(nested, Mapping) else data
    done = 0.0
    total = 0.0
    for value in files.values():
        if not isinstance(value, Mapping):
            continue
        item_done = value.get("done")
        item_total = value.get("total")
        if (
            not isinstance(item_done, (int, float))
            or isinstance(item_done, bool)
            or not isinstance(item_total, (int, float))
            or isinstance(item_total, bool)
        ):
            continue
        done += float(item_done)
        total += float(item_total)
    if total <= 0:
        return None
    return LlamaProgress(
        message="Downloading model",
        ratio=done / total,
        detail=f"{formatBytes(int(done))} / {formatBytes(int(total))}",
    )


def formatBytes(bytes_: int) -> str:
    if bytes_ < 1024:
        return f"{bytes_} B"
    units = ["KiB", "MiB", "GiB", "TiB"]
    value = bytes_ / 1024
    unit = units[0]
    for candidate in units[1:]:
        if value < 1024:
            break
        value /= 1024
        unit = candidate
    return f"{value:.1f} {unit}" if value >= 10 else f"{value:.2f} {unit}"


def normalizeLlamaServerUrl(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Server URL must use http or https")
    if not parsed.netloc:
        raise ValueError("Server URL must include a host")
    path = parsed.path.rstrip("/").removesuffix("/v1")
    normalized = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    return normalized.rstrip("/")


def llamaInferenceUrl(serverUrl: str) -> str:
    return f"{normalizeLlamaServerUrl(serverUrl)}/v1"


class LlamaClient:
    def __init__(self, serverUrl: str, apiKey: str | None = None) -> None:
        self.serverUrl = normalizeLlamaServerUrl(serverUrl)
        self.apiKey = apiKey

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        headers: dict[str, str] = {}
        if json_body:
            headers["Content-Type"] = "application/json"
        if self.apiKey:
            headers["Authorization"] = f"Bearer {self.apiKey}"
        return headers

    async def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        signal: Any = None,
    ) -> Any:
        async with _http_client(timeout=None) as client:
            async with asyncio.timeout(_REQUEST_TIMEOUT_SECONDS):
                response = await _await_request(
                    client.request(
                        method,
                        f"{self.serverUrl}{path}",
                        headers=self._headers(json_body=body is not None),
                        content=json.dumps(body) if body is not None else None,
                    ),
                    signal,
                )
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not response.is_success:
            raise RuntimeError(
                _error_message(
                    payload, f"llama.cpp returned HTTP {response.status_code}"
                )
            )
        return payload

    async def list(
        self, *, reload: bool = False, signal: Any = None
    ) -> list[LlamaModelInfo]:
        payload = await self._request(
            f"/models{'?reload=1' if reload else ''}", signal=signal
        )
        if not isinstance(payload, Mapping) or not isinstance(
            payload.get("data"), list
        ):
            raise RuntimeError("llama.cpp returned an invalid model catalog")  # noqa: TRY004
        models = [_model_info(value) for value in payload["data"]]
        if any(model is None for model in models):
            raise RuntimeError("Server is not running in llama.cpp router mode")
        return [model for model in models if model is not None]

    async def props(self, *, signal: Any = None) -> LlamaServerProps:
        payload = await self._request("/props", signal=signal)
        if not isinstance(payload, Mapping):
            return LlamaServerProps()
        value = payload.get("models_autoload")
        return LlamaServerProps(
            models_autoload=value if isinstance(value, bool) else None
        )

    async def load(self, model: str, signal: Any = None) -> None:
        await self._request(
            "/models/load", method="POST", body={"model": model}, signal=signal
        )

    async def unload(self, model: str, signal: Any = None) -> None:
        await self._request(
            "/models/unload", method="POST", body={"model": model}, signal=signal
        )

    async def unloadAndWait(self, model: str, signal: Any = None) -> None:
        await self.unload(model, signal)
        while True:
            entry = next(
                (
                    candidate
                    for candidate in await self.list(signal=signal)
                    if candidate.id == model
                ),
                None,
            )
            if entry is None or entry.status.value == "unloaded":
                return
            await sleep(100, signal)

    async def download(self, model: str, signal: Any = None) -> None:
        await self._request(
            "/models", method="POST", body={"model": model}, signal=signal
        )

    async def watch(
        self, onEvent: Callable[[LlamaModelEvent], None], signal: Any = None
    ) -> None:
        timeout = httpx.Timeout(15, read=None)
        async with _http_client(timeout) as client:
            request = client.build_request(
                "GET",
                f"{self.serverUrl}/models/sse",
                headers=self._headers(),
            )
            opened_response: httpx.Response | None = None

            async def open_response() -> httpx.Response:
                nonlocal opened_response
                opened_response = await client.send(request, stream=True)
                return opened_response

            try:
                response = await _await_request(open_response(), signal)
            except BaseException:
                if opened_response is not None:
                    try:
                        await opened_response.aclose()
                    except BaseException:  # noqa: BLE001,S110 - preserve the request error
                        pass
                raise
            try:
                if not response.is_success or response.status_code in {204, 205}:
                    raise RuntimeError(
                        f"llama.cpp SSE returned HTTP {response.status_code}"
                    )
                frame: list[str] = []
                lines = response.aiter_lines().__aiter__()
                while True:
                    try:
                        line = await _await_request(lines.__anext__(), signal)
                    except StopAsyncIteration:
                        break
                    if line:
                        frame.append(line)
                        continue
                    data = "\n".join(
                        item[5:].lstrip() for item in frame if item.startswith("data:")
                    )
                    frame.clear()
                    if not data:
                        continue
                    try:
                        payload = json.loads(data)
                        if (
                            isinstance(payload, Mapping)
                            and isinstance(payload.get("model"), str)
                            and isinstance(payload.get("event"), str)
                        ):
                            onEvent(
                                LlamaModelEvent(
                                    model=payload["model"],
                                    event=payload["event"],
                                    data=payload.get("data"),
                                )
                            )
                    except Exception:  # noqa: BLE001,S112 - Pi ignores malformed SSE events
                        continue
            except BaseException:
                try:
                    await response.aclose()
                except BaseException:  # noqa: BLE001,S110 - preserve the stream error
                    pass
                raise
            else:
                await response.aclose()

    async def loadAndWait(
        self,
        model: str,
        onProgress: Callable[[LlamaProgress], None],
        signal: Any = None,
    ) -> LlamaModelInfo:
        event_loaded = False
        event_error: str | None = None

        def on_event(event: LlamaModelEvent) -> None:
            nonlocal event_loaded, event_error
            if event.model != model or event.event not in {
                "model_status",
                "status_change",
            }:
                return
            status = (
                event.data.get("status") if isinstance(event.data, Mapping) else None
            )
            if status == "loaded":
                event_loaded = True
            if status == "unloaded":
                event_error = "Model failed to load"
            progress = _parse_load_progress(event.data)
            if progress is not None:
                onProgress(progress)

        watcher = asyncio.create_task(self.watch(on_event, signal))
        try:
            await self.load(model, signal)
            onProgress(LlamaProgress(message="Loading model"))
            while True:
                throw_if_aborted(signal)
                entry = next(
                    (
                        candidate
                        for candidate in await self.list(signal=signal)
                        if candidate.id == model
                    ),
                    None,
                )
                if entry is not None and entry.status.value == "loaded":
                    return entry
                if event_loaded and entry is None:
                    return LlamaModelInfo(
                        id=model, status=LlamaModelStatusInfo(value="loaded")
                    )
                if (entry is not None and entry.status.failed) or event_error:
                    if entry is not None and entry.status.exit_code is not None:
                        raise RuntimeError(
                            f"Model exited with code {entry.status.exit_code}"
                        )
                    raise RuntimeError(event_error or "Model failed to load")
                await sleep(250, signal)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def downloadAndWait(
        self,
        model: str,
        onProgress: Callable[[LlamaProgress], None],
        signal: Any = None,
    ) -> list[LlamaModelInfo]:
        finished = False
        failure: str | None = None
        saw_downloading = False

        def on_event(event: LlamaModelEvent) -> None:
            nonlocal finished, failure, saw_downloading
            if event.model != model:
                return
            if event.event == "download_finished":
                finished = True
            elif event.event == "download_failed":
                failure = _error_message(event.data, "Download failed")
            elif event.event == "download_progress":
                saw_downloading = True
                progress = _parse_download_progress(event.data)
                if progress is not None:
                    onProgress(progress)

        watcher = asyncio.create_task(self.watch(on_event, signal))
        polls = 0
        try:
            await self.download(model, signal)
            onProgress(LlamaProgress(message="Downloading model"))
            while True:
                throw_if_aborted(signal)
                if failure:
                    raise RuntimeError(failure)
                models = await self.list(signal=signal)
                polls += 1
                entry = next(
                    (candidate for candidate in models if candidate.id == model), None
                )
                if entry is not None and entry.status.value == "downloading":
                    saw_downloading = True
                    progress = _parse_download_progress(entry.status.progress)
                    if progress is not None:
                        onProgress(progress)
                elif finished or (
                    entry is not None and (saw_downloading or polls >= 2)
                ):
                    return await self.list(reload=True, signal=signal)
                await sleep(500, signal)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)


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

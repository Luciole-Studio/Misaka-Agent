"""Hugging Face GGUF discovery used by the llama.cpp model manager."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from misaka.ai.utils.abort import throw_if_aborted, wait_for_abort

DEFAULT_HUGGING_FACE_URL = "https://huggingface.co"
_QUANTIZATION_PATTERN = re.compile(
    r"(?:^|[-_.])((?:UD-)?(?:IQ\d(?:_[A-Z0-9]+)+|Q\d(?:_[A-Z0-9]+)+|BF16|F16|F32|MXFP\d(?:_[A-Z0-9]+)*))$",
    re.IGNORECASE,
)
_SHARD_SUFFIX_PATTERN = re.compile(r"-\d{5}-of-\d{5}$")


@dataclass(slots=True)
class HuggingFaceModel:
    id: str
    downloads: int


@dataclass(slots=True)
class HuggingFaceQuantization:
    name: str
    size: int | None = None


@dataclass(slots=True)
class HuggingFaceModelDetails:
    id: str
    gated: bool | str
    quantizations: list[HuggingFaceQuantization]


def _payload_error(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, str) and error:
            return error
    return fallback


def _rate_limit_delay(value: str | None) -> int | None:
    match = re.search(r"(?:^|;)t=(\d+)", value or "")
    return int(match.group(1)) if match else None


async def _read_token(path: str) -> str | None:
    try:
        token = (
            await asyncio.to_thread(Path(path).read_text, encoding="utf-8")
        ).strip()
    except (OSError, UnicodeError):
        return None
    return token or None


async def findHuggingFaceToken(env: dict[str, str] | None = None) -> str | None:
    resolved = dict(os.environ if env is None else env)
    from_environment = resolved.get("HF_TOKEN", "").strip()
    if from_environment:
        return from_environment
    candidates = [
        resolved.get("HF_TOKEN_PATH"),
        str(Path(resolved["HF_HOME"]) / "token") if resolved.get("HF_HOME") else None,
        (
            str(Path(resolved["XDG_CACHE_HOME"]) / "huggingface" / "token")
            if resolved.get("XDG_CACHE_HOME")
            else None
        ),
        str(Path.home() / ".cache" / "huggingface" / "token"),
    ]
    for path in dict.fromkeys(candidate for candidate in candidates if candidate):
        token = await _read_token(path)
        if token:
            return token
    return None


async def _await_request(operation: Any, signal: Any) -> Any:
    task = asyncio.ensure_future(operation)
    try:
        throw_if_aborted(signal)
    except BaseException:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    if signal is None:
        return await task
    aborting = asyncio.create_task(wait_for_abort(signal))
    try:
        await asyncio.wait({task, aborting}, return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            return task.result()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        throw_if_aborted(signal)
        raise RuntimeError("Request was aborted")
    finally:
        aborting.cancel()


class HuggingFaceClient:
    def __init__(
        self, token: str | None = None, baseUrl: str = DEFAULT_HUGGING_FACE_URL
    ) -> None:
        self.token = token
        self.baseUrl = baseUrl.rstrip("/")

    async def _request(self, path: str, signal: Any = None) -> Any:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        async with httpx.AsyncClient(timeout=15) as client:
            response = await _await_request(
                client.get(f"{self.baseUrl}{path}", headers=headers), signal
            )
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.is_success:
            return payload
        if response.status_code == 429:
            retry_after = response.headers.get("retry-after")
            try:
                delay = int(retry_after) if retry_after else None
            except ValueError:
                delay = None
            delay = delay or _rate_limit_delay(response.headers.get("ratelimit"))
            suffix = f"; retry in {delay}s" if delay else ""
            raise RuntimeError(f"Hugging Face rate limit reached{suffix}")
        raise RuntimeError(
            _payload_error(
                payload, f"Hugging Face returned HTTP {response.status_code}"
            )
        )

    async def search(self, query: str, signal: Any = None) -> list[HuggingFaceModel]:
        params = urlencode(
            {
                "search": query,
                "filter": "gguf",
                "sort": "downloads",
                "direction": "-1",
                "limit": "20",
            }
        )
        payload = await self._request(f"/api/models?{params}", signal)
        if not isinstance(payload, list):
            raise RuntimeError("Hugging Face returned invalid search results")  # noqa: TRY004
        result: list[HuggingFaceModel] = []
        for value in payload:
            if not isinstance(value, dict) or not isinstance(value.get("id"), str):
                continue
            downloads = value.get("downloads")
            result.append(
                HuggingFaceModel(
                    id=value["id"],
                    downloads=(
                        int(downloads)
                        if isinstance(downloads, (int, float))
                        and not isinstance(downloads, bool)
                        else 0
                    ),
                )
            )
        return result

    async def details(self, id: str, signal: Any = None) -> HuggingFaceModelDetails:
        encoded = "/".join(quote(part, safe="") for part in id.split("/"))
        payload = await self._request(f"/api/models/{encoded}?blobs=true", signal)
        if not isinstance(payload, dict):
            raise RuntimeError("Hugging Face returned invalid model details")  # noqa: TRY004
        sizes: dict[str, tuple[int, bool]] = {}
        siblings = payload.get("siblings")
        if isinstance(siblings, list):
            for value in siblings:
                if not isinstance(value, dict) or not isinstance(
                    value.get("rfilename"), str
                ):
                    continue
                filename = value["rfilename"].rsplit("/", 1)[-1]
                if not filename.lower().endswith(
                    ".gguf"
                ) or filename.lower().startswith("mmproj"):
                    continue
                stem = _SHARD_SUFFIX_PATTERN.sub("", filename[:-5])
                match = _QUANTIZATION_PATTERN.search(stem)
                if match is None:
                    continue
                quantization = match.group(1).upper()
                total, complete = sizes.get(quantization, (0, True))
                size = value.get("size")
                if isinstance(size, (int, float)) and not isinstance(size, bool):
                    total += int(size)
                else:
                    complete = False
                sizes[quantization] = (total, complete)
        quantizations = [
            HuggingFaceQuantization(name=name, size=total if complete else None)
            for name, (total, complete) in sizes.items()
        ]
        quantizations.sort(
            key=lambda item: (
                0 if item.name == "Q4_K_M" else 1,
                item.size if item.size is not None else 2**63 - 1,
                item.name,
            )
        )
        gated = payload.get("gated")
        return HuggingFaceModelDetails(
            id=payload["id"] if isinstance(payload.get("id"), str) else id,
            gated=gated if gated in {"auto", "manual"} else False,
            quantizations=quantizations,
        )


__all__ = [
    "HuggingFaceClient",
    "HuggingFaceModel",
    "HuggingFaceModelDetails",
    "HuggingFaceQuantization",
    "findHuggingFaceToken",
]

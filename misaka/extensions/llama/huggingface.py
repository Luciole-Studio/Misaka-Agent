"""Hugging Face GGUF discovery used by the llama.cpp model manager.

Translated from pi's ``coding-agent/src/extensions/llama/huggingface.ts``.
"""

from __future__ import annotations

import asyncio
import functools
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

import httpx

from misaka.utils.collation import localeCompare
from misaka.utils.js import AbortSignal, isNumber, jsNumber, jsString, jsTrim
from misaka.utils.whatwg_fetch import fetch
from misaka.utils.whatwg_url import URLSearchParams

DEFAULT_HUGGING_FACE_URL = "https://huggingface.co"
QUANTIZATION_PATTERN = re.compile(
    r"(?:^|[-_.])((?:UD-)?(?:IQ\d(?:_[A-Z0-9]+)+|Q\d(?:_[A-Z0-9]+)+|BF16|F16|F32|MXFP\d(?:_[A-Z0-9]+)*))\Z",
    re.IGNORECASE,
)
SHARD_SUFFIX_PATTERN = re.compile(r"-\d{5}-of-\d{5}\Z")
_MAX_SAFE_INTEGER = 2**53 - 1


@dataclass(slots=True)
class HuggingFaceModel:
    id: str
    downloads: int | float


@dataclass(slots=True)
class HuggingFaceQuantization:
    name: str
    size: int | float | None = None


@dataclass(slots=True)
class HuggingFaceModelDetails:
    id: str
    gated: Literal[False, "auto", "manual"]
    quantizations: list[HuggingFaceQuantization]


def payloadError(payload: Any, fallback: str) -> str:
    if not isinstance(payload, Mapping):
        return fallback
    error = payload.get("error")
    return error if isinstance(error, str) and error else fallback


def parseRateLimitDelay(value: str | None) -> int | float | None:
    match = re.search(r"(?:^|;)t=(\d+)", value) if value is not None else None
    return jsNumber(match.group(1)) if match else None


def _readText(path: str) -> str:
    # ``readFile(path, "utf8")`` replaces invalid bytes instead of failing.
    with open(path, encoding="utf-8", errors="replace", newline="") as file:
        return file.read()


async def readToken(path: str) -> str | None:
    try:
        token = jsTrim(await asyncio.to_thread(_readText, path))
        return token or None
    except Exception:  # noqa: BLE001
        return None


def _join(*parts: str) -> str:
    """``path.join``: the joined path, normalized."""
    return os.path.normpath(os.path.join(*parts))


async def findHuggingFaceToken(env: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    fromEnvironment = jsTrim(env["HF_TOKEN"]) if env.get("HF_TOKEN") is not None else None
    if fromEnvironment:
        return fromEnvironment

    paths = [
        path
        for path in [
            env.get("HF_TOKEN_PATH"),
            _join(env["HF_HOME"], "token") if env.get("HF_HOME") else None,
            _join(env["XDG_CACHE_HOME"], "huggingface", "token") if env.get("XDG_CACHE_HOME") else None,
            _join(os.path.expanduser("~"), ".cache", "huggingface", "token"),
        ]
        if path
    ]
    for path in dict.fromkeys(paths):
        token = await readToken(path)
        if token:
            return token
    return None


class HuggingFaceClient:
    def __init__(self, token: str | None = None, baseUrl: str = DEFAULT_HUGGING_FACE_URL) -> None:
        self.token = token
        self.baseUrl = re.sub(r"/+\Z", "", baseUrl)

    async def request(self, path: str, signal: Any = None) -> Any:
        headers = httpx.Headers()
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        timeout = AbortSignal.timeout(15_000)
        response = await fetch(
            f"{self.baseUrl}{path}",
            {"headers": headers, "signal": AbortSignal.any([signal, timeout]) if signal else timeout},
        )
        payload: Any
        try:
            payload = await response.json()
        except Exception:  # noqa: BLE001
            payload = None
        if not response.ok:
            fallback = f"Hugging Face returned HTTP {response.status}"
            if response.status == 429:
                retryAfter = jsNumber(response.headers.get("retry-after"))
                # `Number(...) || ...`: `NaN` is falsy too.
                delay = (
                    retryAfter
                    if retryAfter and not math.isnan(retryAfter)
                    else parseRateLimitDelay(response.headers.get("ratelimit"))
                )
                raise RuntimeError(
                    f"Hugging Face rate limit reached; retry in {jsString(delay)}s"
                    if delay
                    else "Hugging Face rate limit reached"
                )
            raise RuntimeError(payloadError(payload, fallback))
        return payload

    async def search(self, query: str, signal: Any = None) -> list[HuggingFaceModel]:
        params = URLSearchParams(
            {
                "search": query,
                "filter": "gguf",
                "sort": "downloads",
                "direction": "-1",
                "limit": "20",
            }
        )
        payload = await self.request(f"/api/models?{params}", signal)
        if not isinstance(payload, list):
            raise RuntimeError("Hugging Face returned invalid search results")  # noqa: TRY004
        return [
            HuggingFaceModel(
                id=value["id"],
                downloads=value.get("downloads") if isNumber(value.get("downloads")) else 0,
            )
            for value in payload
            if isinstance(value, Mapping) and isinstance(value.get("id"), str)
        ]

    async def details(self, id: str, signal: Any = None) -> HuggingFaceModelDetails:
        # `encodeURIComponent` leaves !~*'() alone too.
        encodedId = "/".join(quote(part, safe="!~*'()") for part in id.split("/"))
        payload = await self.request(f"/api/models/{encodedId}?blobs=true", signal)
        if not isinstance(payload, Mapping):
            raise RuntimeError("Hugging Face returned invalid model details")  # noqa: TRY004
        model = payload
        sizes: dict[str, dict[str, Any]] = {}
        siblings = model.get("siblings")
        if isinstance(siblings, list):
            for value in siblings:
                if not isinstance(value, Mapping):
                    continue
                file = value
                rfilename = file.get("rfilename")
                if not isinstance(rfilename, str) or not rfilename.lower().endswith(".gguf"):
                    continue
                filename = rfilename.split("/")[-1]
                if filename.lower().startswith("mmproj"):
                    continue
                stem = SHARD_SUFFIX_PATTERN.sub("", filename[:-5], count=1)
                match = QUANTIZATION_PATTERN.search(stem)
                quantization = match.group(1).upper() if match else None
                if not quantization:
                    continue
                current = sizes.get(quantization) or {"total": 0, "complete": True}
                if isNumber(file.get("size")):
                    current["total"] += file["size"]
                else:
                    current["complete"] = False
                sizes[quantization] = current

        def compare(left: HuggingFaceQuantization, right: HuggingFaceQuantization) -> int:
            if left.name == "Q4_K_M":
                return -1
            if right.name == "Q4_K_M":
                return 1
            difference = (left.size if left.size is not None else _MAX_SAFE_INTEGER) - (
                right.size if right.size is not None else _MAX_SAFE_INTEGER
            )
            if difference:
                return -1 if difference < 0 else 1
            return localeCompare(left.name, right.name)

        quantizations = sorted(
            (
                HuggingFaceQuantization(name=name, size=size["total"] if size["complete"] else None)
                for name, size in sizes.items()
            ),
            key=functools.cmp_to_key(compare),
        )
        gated = model.get("gated")
        return HuggingFaceModelDetails(
            id=model["id"] if isinstance(model.get("id"), str) else id,
            gated=gated if gated == "auto" or gated == "manual" else False,
            quantizations=quantizations,
        )


__all__ = [
    "HuggingFaceClient",
    "HuggingFaceModel",
    "HuggingFaceModelDetails",
    "HuggingFaceQuantization",
    "findHuggingFaceToken",
]

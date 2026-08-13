"""Version check helpers for update notifications."""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.request import Request, urlopen

from misaka.utils.harn_user_agent import get_harn_user_agent

LATEST_VERSION_URL = "https://harn.dev/api/latest-version"
DEFAULT_VERSION_CHECK_TIMEOUT_MS = 10_000


@dataclass(slots=True)
class LatestHarnRelease:
    version: str
    packageName: str | None = None
    note: str | None = None


@dataclass(slots=True)
class _ParsedVersion:
    major: int
    minor: int
    patch: int
    prerelease: str | None = None


def compare_package_versions(left_version: str, right_version: str) -> int | None:
    left = _parse_package_version(left_version)
    right = _parse_package_version(right_version)
    if left is None or right is None:
        return None
    if left.major != right.major:
        return left.major - right.major
    if left.minor != right.minor:
        return left.minor - right.minor
    if left.patch != right.patch:
        return left.patch - right.patch
    if left.prerelease == right.prerelease:
        return 0
    if not left.prerelease:
        return 1
    if not right.prerelease:
        return -1
    return (left.prerelease > right.prerelease) - (left.prerelease < right.prerelease)


def is_newer_package_version(candidate_version: str, current_version: str) -> bool:
    comparison = compare_package_versions(candidate_version, current_version)
    if comparison is not None:
        return comparison > 0
    return candidate_version.strip() != current_version.strip()


async def get_latest_harn_release(  # noqa: D401
    current_version: str,
    options: dict[str, Any] | None = None,
) -> LatestHarnRelease | None:
        # MISAKA: fork 断更新链——harn.dev 的新版本对本仓无意义，自更新会把包盖成原版 harn
    return None
    if os.environ.get("MISAKA_SKIP_VERSION_CHECK") or os.environ.get("MISAKA_OFFLINE"):
        return None

    timeout_ms = (
        int(options.get("timeoutMs", DEFAULT_VERSION_CHECK_TIMEOUT_MS))
        if options
        else DEFAULT_VERSION_CHECK_TIMEOUT_MS
    )
    headers = {
        "User-Agent": get_harn_user_agent(current_version),
        "accept": "application/json",
    }
    data = await _fetch_latest_release_json(LATEST_VERSION_URL, headers, timeout_ms)
    if not isinstance(data, dict):
        return None

    version = data.get("version")
    if not isinstance(version, str) or not version.strip():
        return None

    package_name = data.get("packageName")
    note = data.get("note")
    return LatestHarnRelease(
        version=version.strip(),
        packageName=package_name.strip() if isinstance(package_name, str) and package_name.strip() else None,
        note=note.strip() if isinstance(note, str) and note.strip() else None,
    )


async def get_latest_harn_version(current_version: str, options: dict[str, Any] | None = None) -> str | None:
    latest = await get_latest_harn_release(current_version, options)
    return latest.version if latest is not None else None


async def check_for_new_harn_version(current_version: str) -> LatestHarnRelease | None:
    try:
        latest = await get_latest_harn_release(current_version)
    except Exception:
        return None
    if latest is not None and is_newer_package_version(latest.version, current_version):
        return latest
    return None


def _parse_package_version(version: str) -> _ParsedVersion | None:
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+.*)?$", version.strip())
    if match is None:
        return None
    return _ParsedVersion(
        major=int(match.group(1)),
        minor=int(match.group(2)),
        patch=int(match.group(3)),
        prerelease=match.group(4),
    )


async def _fetch_latest_release_json(url: str, headers: dict[str, str], timeout_ms: int) -> dict[str, Any] | None:
    def _load() -> dict[str, Any] | None:
        request = Request(url, headers=headers)
        with urlopen(request, timeout=timeout_ms / 1000) as response:
            if response.status < 200 or response.status >= 300:
                return None
            payload = response.read()
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except Exception:
            return None
        return decoded if isinstance(decoded, dict) else None

    return await asyncio.to_thread(_load)


checkForNewHarnVersion = check_for_new_harn_version
comparePackageVersions = compare_package_versions
getLatestHarnRelease = get_latest_harn_release
getLatestHarnVersion = get_latest_harn_version
isNewerPackageVersion = is_newer_package_version

__all__ = [
    "LatestHarnRelease",
    "comparePackageVersions",
    "isNewerPackageVersion",
    "getLatestHarnRelease",
    "getLatestHarnVersion",
    "checkForNewHarnVersion",
]

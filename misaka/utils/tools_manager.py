"""External tool discovery and installation helpers."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import subprocess
import tarfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.request import Request, urlopen
from zipfile import ZipFile

from misaka.config import APP_NAME, get_bin_dir

TOOLS_DIR = get_bin_dir()
NETWORK_TIMEOUT_MS = 10_000
DOWNLOAD_TIMEOUT_MS = 120_000

type ToolName = Literal["fd", "rg"]


@dataclass(slots=True)
class ToolConfig:
    name: str
    repo: str
    binaryName: str
    tagPrefix: str
    getAssetName: Callable[[str, str, str], str | None]
    systemBinaryNames: list[str] | None = None


def _fd_asset_name(version: str, plat: str, architecture: str) -> str | None:
    if plat == "darwin":
        arch_str = "aarch64" if architecture == "arm64" else "x86_64"
        return f"fd-v{version}-{arch_str}-apple-darwin.tar.gz"
    if plat == "linux":
        arch_str = "aarch64" if architecture == "arm64" else "x86_64"
        return f"fd-v{version}-{arch_str}-unknown-linux-gnu.tar.gz"
    if plat == "win32":
        arch_str = "aarch64" if architecture == "arm64" else "x86_64"
        return f"fd-v{version}-{arch_str}-pc-windows-msvc.zip"
    return None


def _rg_asset_name(version: str, plat: str, architecture: str) -> str | None:
    if plat == "darwin":
        arch_str = "aarch64" if architecture == "arm64" else "x86_64"
        return f"ripgrep-{version}-{arch_str}-apple-darwin.tar.gz"
    if plat == "linux":
        if architecture == "arm64":
            return f"ripgrep-{version}-aarch64-unknown-linux-gnu.tar.gz"
        return f"ripgrep-{version}-x86_64-unknown-linux-musl.tar.gz"
    if plat == "win32":
        arch_str = "aarch64" if architecture == "arm64" else "x86_64"
        return f"ripgrep-{version}-{arch_str}-pc-windows-msvc.zip"
    return None


TOOLS: dict[ToolName, ToolConfig] = {
    "fd": ToolConfig(
        name="fd",
        repo="sharkdp/fd",
        binaryName="fd",
        systemBinaryNames=["fd", "fdfind"],
        tagPrefix="v",
        getAssetName=_fd_asset_name,
    ),
    "rg": ToolConfig(
        name="ripgrep",
        repo="BurntSushi/ripgrep",
        binaryName="rg",
        tagPrefix="",
        getAssetName=_rg_asset_name,
    ),
}

TERMUX_PACKAGES: dict[ToolName, str] = {"fd": "fd", "rg": "ripgrep"}


def is_offline_mode_enabled() -> bool:
    value = os.environ.get("MISAKA_OFFLINE")
    if not value:
        return False
    return value == "1" or value.lower() in {"true", "yes"}


def command_exists(command: str) -> bool:
    try:
        result = subprocess.run(
            [command, "--version"],
            check=False,
            capture_output=True,
            text=False,
        )
    except OSError:
        return False
    return result.returncode == 0 or result.returncode is not None


def get_tool_path(tool: ToolName, *, tools_dir: str | None = None) -> str | None:
    config = TOOLS.get(tool)
    if config is None:
        return None

    resolved_tools_dir = tools_dir or TOOLS_DIR
    binary_ext = ".exe" if _platform() == "win32" else ""
    local_path = str(Path(resolved_tools_dir) / f"{config.binaryName}{binary_ext}")
    if Path(local_path).exists():
        return local_path

    system_binary_names = config.systemBinaryNames or [config.binaryName]
    for system_binary_name in system_binary_names:
        if command_exists(system_binary_name):
            return system_binary_name
    return None


async def get_latest_version(repo: str) -> str:
    data = await _fetch_json(
        f"https://api.github.com/repos/{repo}/releases/latest",
        user_agent=f"{APP_NAME}-coding-agent",
        timeout_ms=NETWORK_TIMEOUT_MS,
    )
    tag_name = data.get("tag_name")
    if not isinstance(tag_name, str) or not tag_name:
        raise RuntimeError("GitHub API returned no tag_name")
    return tag_name.removeprefix("v")


async def download_file(url: str, dest: str) -> None:
    def _download() -> None:
        request = Request(url)
        with urlopen(request, timeout=DOWNLOAD_TIMEOUT_MS / 1000) as response:
            if getattr(response, "status", 200) < 200 or getattr(response, "status", 200) >= 300:
                raise RuntimeError(f"Failed to download: {getattr(response, 'status', 'unknown')}")
            with open(dest, "wb") as file_handle:
                shutil.copyfileobj(response, file_handle)

    await asyncio.to_thread(_download)


def find_binary_recursively(root_dir: str, binary_file_name: str) -> str | None:
    for current_dir, _dirs, files in os.walk(root_dir):
        if binary_file_name in files:
            return str(Path(current_dir) / binary_file_name)
    return None


def extract_tar_gz_archive(archive_path: str, extract_dir: str, asset_name: str) -> None:
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            archive.extractall(extract_dir, filter="data")
    except Exception as error:
        raise RuntimeError(f"Failed to extract {asset_name}: {error}") from error


def extract_zip_archive(archive_path: str, extract_dir: str, asset_name: str) -> None:
    try:
        with ZipFile(archive_path) as archive:
            archive.extractall(extract_dir)
    except Exception as error:
        raise RuntimeError(f"Failed to extract {asset_name}: {error}") from error


async def download_tool(tool: ToolName, *, tools_dir: str | None = None) -> str:
    config = TOOLS.get(tool)
    if config is None:
        raise RuntimeError(f"Unknown tool: {tool}")

    plat = _platform()
    architecture = _arch()
    version = await get_latest_version(config.repo)
    if tool == "fd" and plat == "darwin" and architecture == "x64":
        version = "10.3.0"

    asset_name = config.getAssetName(version, plat, architecture)
    if asset_name is None:
        raise RuntimeError(f"Unsupported platform: {plat}/{architecture}")

    resolved_tools_dir = Path(tools_dir or TOOLS_DIR)
    resolved_tools_dir.mkdir(parents=True, exist_ok=True)

    download_url = (
        f"https://github.com/{config.repo}/releases/download/{config.tagPrefix}{version}/{asset_name}"
    )
    archive_path = resolved_tools_dir / asset_name
    binary_ext = ".exe" if plat == "win32" else ""
    binary_path = resolved_tools_dir / f"{config.binaryName}{binary_ext}"

    await download_file(download_url, str(archive_path))

    extract_dir = resolved_tools_dir / (
        f"extract_tmp_{config.binaryName}_{os.getpid()}_{int(time.time() * 1000)}_{secrets.token_hex(4)}"
    )
    extract_dir.mkdir(parents=True, exist_ok=True)

    try:
        if asset_name.endswith(".tar.gz"):
            extract_tar_gz_archive(str(archive_path), str(extract_dir), asset_name)
        elif asset_name.endswith(".zip"):
            extract_zip_archive(str(archive_path), str(extract_dir), asset_name)
        else:
            raise RuntimeError(f"Unsupported archive format: {asset_name}")

        binary_file_name = f"{config.binaryName}{binary_ext}"
        extracted_dir = extract_dir / asset_name.removesuffix(".tar.gz").removesuffix(".zip")
        extracted_binary_candidates = [extracted_dir / binary_file_name, extract_dir / binary_file_name]
        extracted_binary = next((candidate for candidate in extracted_binary_candidates if candidate.exists()), None)
        if extracted_binary is None:
            recursive_match = find_binary_recursively(str(extract_dir), binary_file_name)
            extracted_binary = Path(recursive_match) if recursive_match is not None else None
        if extracted_binary is None:
            raise RuntimeError(
                f"Binary not found in archive: expected {binary_file_name} under {extract_dir}"
            )

        extracted_binary.replace(binary_path)

        if plat != "win32":
            binary_path.chmod(0o755)
    finally:
        archive_path.unlink(missing_ok=True)
        shutil.rmtree(extract_dir, ignore_errors=True)

    return str(binary_path)


async def ensure_tool(
    tool: ToolName,
    silent: bool = False,
    *,
    tools_dir: str | None = None,
    printer: Callable[[str], None] = print,
) -> str | None:
    existing_path = get_tool_path(tool, tools_dir=tools_dir)
    if existing_path:
        return existing_path

    config = TOOLS.get(tool)
    if config is None:
        return None

    if is_offline_mode_enabled():
        if not silent:
            printer(f"{config.name} not found. Offline mode enabled, skipping download.")
        return None

    if _platform() == "android":
        pkg_name = TERMUX_PACKAGES.get(tool, tool)
        if not silent:
            printer(f"{config.name} not found. Install with: pkg install {pkg_name}")
        return None

    if not silent:
        printer(f"{config.name} not found. Downloading...")

    try:
        path = await download_tool(tool, tools_dir=tools_dir)
        if not silent:
            printer(f"{config.name} installed to {path}")
        return path
    except Exception as error:
        if not silent:
            printer(f"Failed to download {config.name}: {error}")
        return None


async def _fetch_json(url: str, *, user_agent: str, timeout_ms: int) -> dict[str, Any]:
    def _load() -> dict[str, Any]:
        request = Request(url, headers={"User-Agent": user_agent})
        with urlopen(request, timeout=timeout_ms / 1000) as response:
            status = getattr(response, "status", 200)
            if status < 200 or status >= 300:
                raise RuntimeError(f"GitHub API error: {status}")
            payload = response.read()
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("Expected JSON object")
        return data

    return await asyncio.to_thread(_load)


def _platform() -> str:
    if sys_platform := os.environ.get("PYTHON_SYS_PLATFORM"):
        return sys_platform
    import sys

    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform in {"win32", "cygwin"}:
        return "win32"
    if sys.platform == "android":
        return "android"
    return sys.platform


def _arch() -> str:
    import platform

    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    if machine in {"x86_64", "amd64"}:
        return "x64"
    return machine


def getToolPath(tool: ToolName) -> str | None:
    return get_tool_path(tool)


async def ensureTool(tool: ToolName, silent: bool = False) -> str | None:
    return await ensure_tool(tool, silent)

__all__ = [
    "getToolPath",
    "ensureTool",
]

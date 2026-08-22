"""Engine-side (formerly harn/pi) configuration: paths, package metadata, identity.

The identity is now MISAKA's: dist metadata is looked up under ``misaka``, the
config directory is ``~/.misaka/``, and agent assets live in ``~/.misaka/agent/``.
Product-side CFG lives in misaka/config/product.py.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Literal

from misaka.utils.paths import normalize_path

InstallMethod = Literal["pipx", "uv-tool", "pip", "source", "unknown"]

isBunBinary = False
isBunRuntime = False


@dataclass(slots=True, frozen=True)
class SelfUpdateCommandStep:
    command: str
    args: tuple[str, ...]
    display: str


@dataclass(slots=True, frozen=True)
class SelfUpdateCommand(SelfUpdateCommandStep):
    steps: tuple[SelfUpdateCommandStep, ...] | None = None


def _quote_command_arg(arg: str) -> str:
    return f'"{arg}"' if any(char.isspace() for char in arg) else arg


def _make_self_update_command_step(parts: Sequence[str]) -> SelfUpdateCommandStep:
    if not parts:
        raise ValueError("command step cannot be empty")
    rendered = " ".join(_quote_command_arg(part) for part in parts)
    return SelfUpdateCommandStep(command=parts[0], args=tuple(parts[1:]), display=rendered)


def _make_self_update_command(
    install_step: SelfUpdateCommandStep,
    uninstall_step: SelfUpdateCommandStep | None = None,
) -> SelfUpdateCommand:
    if uninstall_step is None:
        return SelfUpdateCommand(
            command=install_step.command,
            args=install_step.args,
            display=install_step.display,
            steps=None,
        )

    return SelfUpdateCommand(
        command=install_step.command,
        args=install_step.args,
        display=f"{uninstall_step.display} && {install_step.display}",
        steps=(uninstall_step, install_step),
    )


def _find_package_root(start: Path) -> Path:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / "package.json").exists() or (candidate / "pyproject.toml").exists():
            return candidate
    return current


def get_package_dir() -> str:
    env_dir = os.environ.get("MISAKA_PACKAGE_DIR")
    if env_dir:
        return normalize_path(env_dir)

    if isBunBinary:
        return str(Path(sys.executable).resolve().parent)

    module_dir = Path(__file__).resolve().parent
    return str(_find_package_root(module_dir))


@lru_cache(maxsize=1)
def _get_package_metadata_path() -> Path | None:
    package_dir = Path(get_package_dir())
    package_json = package_dir / "package.json"
    if package_json.exists():
        return package_json

    pyproject_path = package_dir / "pyproject.toml"
    if pyproject_path.exists():
        return pyproject_path

    return None


@lru_cache(maxsize=1)
def _load_package_metadata() -> dict[str, Any]:
    # Try importlib.metadata first -- this is the most reliable source for the
    # version of an installed package (pip, uv tool, pipx, etc.) and avoids
    # accidentally reading an unrelated pyproject.toml/package.json that happens
    # to exist in a parent directory.
    try:
        distribution = importlib_metadata.metadata("misaka")
        dist_version = distribution.get("Version")
        if dist_version:
            return {
                "name": distribution.get("Name"),
                "version": dist_version,
                "harnConfig": {},
            }
    except importlib_metadata.PackageNotFoundError:
        pass

    # Fallback: read from a co-located pyproject.toml or package.json (useful
    # during development before the package metadata is installed).
    metadata_path = _get_package_metadata_path()
    if metadata_path is not None:
        if metadata_path.name == "package.json":
            parsed = json.loads(metadata_path.read_text(encoding="utf-8"))
            return {
                "name": parsed.get("name"),
                "version": parsed.get("version"),
                "harnConfig": parsed.get("harnConfig", {}),
            }

        parsed_toml = tomllib.loads(metadata_path.read_text(encoding="utf-8"))
        project = parsed_toml.get("project", {})
        tool_section = parsed_toml.get("tool", {})
        harn_config = (
            tool_section.get("harn", {})
            or tool_section.get("misaka", {}).get("harn_config", {})
            or tool_section.get("harn", {}).get("harn_config", {})
        )
        return {
            "name": project.get("name"),
            "version": project.get("version"),
            "harnConfig": {
                "name": harn_config.get("name"),
                "configDir": harn_config.get("configDir") or harn_config.get("config_dir"),
            },
        }

    return {
        "name": None,
        "version": None,
        "harnConfig": {},
    }


_PACKAGE_METADATA = _load_package_metadata()
_MISAKA_CONFIG = _PACKAGE_METADATA.get("harnConfig", {})
_MISAKA_CONFIG_NAME = _MISAKA_CONFIG.get("name")

PACKAGE_NAME = _PACKAGE_METADATA.get("name") or "misaka"
APP_NAME = _MISAKA_CONFIG_NAME or "misaka"
APP_TITLE = APP_NAME if _MISAKA_CONFIG_NAME else "misaka"
CONFIG_DIR_NAME = _MISAKA_CONFIG.get("configDir") or ".misaka"
VERSION = _PACKAGE_METADATA.get("version") or "0.0.0"

ENV_AGENT_DIR = f"{APP_NAME.upper()}_CODING_AGENT_DIR"
ENV_SESSION_DIR = f"{APP_NAME.upper()}_CODING_AGENT_SESSION_DIR"

DEFAULT_SHARE_VIEWER_URL = "https://harn.dev/session/"


def expand_tilde_path(path: str) -> str:
    return normalize_path(path)


def get_share_viewer_url(gist_id: str) -> str:
    base_url = os.environ.get("MISAKA_SHARE_VIEWER_URL", DEFAULT_SHARE_VIEWER_URL)
    return f"{base_url}#{gist_id}"


def _detect_install_method(package_dir: str | None = None, exec_path: str | None = None) -> InstallMethod:
    normalized_package_dir = normalize_path(package_dir or get_package_dir()).lower().replace("\\", "/")
    normalized_exec_path = normalize_path(exec_path or sys.executable).lower().replace("\\", "/")
    resolved_path = f"{normalized_package_dir}\0{normalized_exec_path}"

    if "/pipx/venvs/" in resolved_path or "/pipx/shared/" in resolved_path:
        return "pipx"
    if "/uv/tools/" in resolved_path or "/.local/share/uv/tools/" in resolved_path:
        return "uv-tool"
    if "/site-packages/" in resolved_path or "/dist-packages/" in resolved_path:
        return "pip"
    if (Path(package_dir or get_package_dir()) / "pyproject.toml").exists():
        return "source"
    return "unknown"


def detect_install_method() -> InstallMethod:
    return _detect_install_method()


def _get_python_pip_command(python_command: Sequence[str] | None = None) -> list[str]:
    if python_command:
        return list(python_command)
    return [sys.executable, "-m", "pip"]


def _get_self_update_command_for_method(
    method: InstallMethod,
    installed_package_name: str,
    update_package_name: str | None = None,
    python_command: Sequence[str] | None = None,
) -> SelfUpdateCommand | None:
    update_package = update_package_name or installed_package_name

    if method == "pipx":
        install_step = _make_self_update_command_step(["pipx", "upgrade", update_package])
        if update_package == installed_package_name:
            return _make_self_update_command(install_step)
        uninstall_step = _make_self_update_command_step(["pipx", "uninstall", installed_package_name])
        reinstall_step = _make_self_update_command_step(["pipx", "install", update_package])
        return _make_self_update_command(reinstall_step, uninstall_step)

    if method == "uv-tool":
        install_parts = ["uv", "tool", "upgrade", update_package]
        if update_package == installed_package_name:
            return _make_self_update_command(_make_self_update_command_step(install_parts))
        uninstall_step = _make_self_update_command_step(["uv", "tool", "uninstall", installed_package_name])
        reinstall_step = _make_self_update_command_step(["uv", "tool", "install", update_package])
        return _make_self_update_command(reinstall_step, uninstall_step)

    if method == "pip":
        pip_command = _get_python_pip_command(python_command)
        install_step = _make_self_update_command_step([*pip_command, "install", "--upgrade", update_package])
        if update_package == installed_package_name:
            return _make_self_update_command(install_step)
        uninstall_step = _make_self_update_command_step([*pip_command, "uninstall", "-y", installed_package_name])
        return _make_self_update_command(install_step, uninstall_step)

    return None


def _is_self_update_path_writable(package_dir: str | None = None) -> bool:
    resolved_package_dir = Path(package_dir or get_package_dir())
    try:
        return os.access(resolved_package_dir, os.W_OK) and os.access(resolved_package_dir.parent, os.W_OK)
    except OSError:
        return False


def get_self_update_command(
    package_name: str,
    python_command: Sequence[str] | None = None,
    update_package_name: str | None = None,
) -> SelfUpdateCommand | None:
    command = _get_self_update_command_for_method(
        detect_install_method(),
        package_name,
        update_package_name,
        python_command,
    )
    if command is None or not _is_self_update_path_writable():
        return None
    return command


def get_self_update_unavailable_instruction(
    package_name: str,
    python_command: Sequence[str] | None = None,
    update_package_name: str | None = None,
) -> str:
    method = detect_install_method()
    update_package = update_package_name or package_name
    command = _get_self_update_command_for_method(method, package_name, update_package, python_command)
    if command is not None:
        if not _is_self_update_path_writable():
            return (
                f"This installation is managed by a {method} install, but the install path is not writable. "
                f"Update it yourself with: {command.display}"
            )
        return f"Update it yourself with: {command.display}"
    if method == "source":
        return (
            "This installation comes from a source checkout. "
            "Update the checkout and resync its environment (for example: git pull && uv sync)."
        )
    return f"Update {update_package} using the Python environment or tool manager that provides this installation."


def get_update_instruction(package_name: str) -> str:
    command = _get_self_update_command_for_method(detect_install_method(), package_name)
    if command is not None:
        return f"Run: {command.display}"
    return get_self_update_unavailable_instruction(package_name)


def get_agent_dir() -> str:
    env_dir = os.environ.get(ENV_AGENT_DIR)
    if env_dir:
        return expand_tilde_path(env_dir)
    return str(Path.home() / CONFIG_DIR_NAME / "agent")


def get_custom_themes_dir() -> str:
    return str(Path(get_agent_dir()) / "themes")


def get_models_path() -> str:
    return str(Path(get_agent_dir()) / "models.json")


def get_auth_path() -> str:
    return str(Path(get_agent_dir()) / "auth.json")


def get_settings_path() -> str:
    return str(Path(get_agent_dir()) / "settings.json")


def get_tools_dir() -> str:
    return str(Path(get_agent_dir()) / "tools")


def get_bin_dir() -> str:
    return str(Path(get_agent_dir()) / "bin")


def get_prompts_dir() -> str:
    return str(Path(get_agent_dir()) / "prompts")


def _get_package_source_dir() -> Path:
    package_dir = Path(get_package_dir())
    source_dir = package_dir / "src" / "misaka"
    if source_dir.exists():
        return source_dir
    return package_dir


def _get_package_module_dir() -> Path:
    """Return the ``misaka`` package directory via ``__file__``.

    Works from both a source checkout and an installed wheel. This module lives
    one level down in misaka/config/, hence ``parents[1]``.
    """
    return Path(__file__).resolve().parents[1]


def get_themes_dir() -> str:
    if isBunBinary:
        return str(Path(get_package_dir()) / "theme")
    # Use __file__-relative resolution so that theme files are found correctly
    # both when running from source (uv run harn) and when installed as a
    # package (uv tool install harn / pip install harn).
    return str(_get_package_module_dir() / "modes" / "interactive" / "theme")


def get_export_template_dir() -> str:
    if isBunBinary:
        return str(Path(get_package_dir()) / "export-html")
    return str(_get_package_module_dir() / "core" / "export_html")


def get_package_json_path() -> str:
    metadata_path = _get_package_metadata_path()
    if metadata_path is not None:
        return str(metadata_path)
    return str(Path(get_package_dir()) / "package.json")


def get_readme_path() -> str:
    if isBunBinary:
        return str((Path(get_package_dir()) / "README.md").resolve())
    # Use __file__-relative resolution so that bundled assets are found correctly
    # both when running from source (uv run harn) and when installed as a
    # package (uv tool install harn / pip install harn).
    return str((_get_package_module_dir() / "README.md").resolve())


def get_docs_path() -> str:
    if isBunBinary:
        return str((Path(get_package_dir()) / "docs").resolve())
    return str((_get_package_module_dir() / "docs").resolve())


def get_examples_path() -> str:
    if isBunBinary:
        return str((Path(get_package_dir()) / "examples").resolve())
    return str((_get_package_module_dir() / "examples").resolve())


def get_changelog_path() -> str:
    if isBunBinary:
        return str((Path(get_package_dir()) / "CHANGELOG.md").resolve())
    return str((_get_package_module_dir() / "CHANGELOG.md").resolve())


def get_interactive_assets_dir() -> str:
    if isBunBinary:
        return str(Path(get_package_dir()) / "assets")
    return str(_get_package_module_dir() / "modes" / "interactive" / "assets")


def get_bundled_interactive_asset_path(name: str) -> str:
    return str(Path(get_interactive_assets_dir()) / name)


def get_sessions_dir() -> str:
    env_dir = os.environ.get(ENV_SESSION_DIR)
    if env_dir:
        return expand_tilde_path(env_dir)
    return str(Path(get_agent_dir()) / "sessions")


def get_debug_log_path() -> str:
    return str(Path(get_agent_dir()) / f"{APP_NAME}-debug.log")


expandTildePath = expand_tilde_path
detectInstallMethod = detect_install_method
getShareViewerUrl = get_share_viewer_url
getSelfUpdateCommand = get_self_update_command
getSelfUpdateUnavailableInstruction = get_self_update_unavailable_instruction
getUpdateInstruction = get_update_instruction
getAgentDir = get_agent_dir
getCustomThemesDir = get_custom_themes_dir
getModelsPath = get_models_path
getAuthPath = get_auth_path
getSettingsPath = get_settings_path
getToolsDir = get_tools_dir
getBinDir = get_bin_dir
getPromptsDir = get_prompts_dir
getPackageDir = get_package_dir
getThemesDir = get_themes_dir
getExportTemplateDir = get_export_template_dir
getPackageJsonPath = get_package_json_path
getReadmePath = get_readme_path
getDocsPath = get_docs_path
getExamplesPath = get_examples_path
getChangelogPath = get_changelog_path
getInteractiveAssetsDir = get_interactive_assets_dir
getBundledInteractiveAssetPath = get_bundled_interactive_asset_path
getSessionsDir = get_sessions_dir
getDebugLogPath = get_debug_log_path

__all__ = [
    "APP_NAME",
    "APP_TITLE",
    "CONFIG_DIR_NAME",
    "ENV_AGENT_DIR",
    "ENV_SESSION_DIR",
    "InstallMethod",
    "PACKAGE_NAME",
    "SelfUpdateCommand",
    "VERSION",
    "detectInstallMethod",
    "expandTildePath",
    "getAgentDir",
    "getAuthPath",
    "getBinDir",
    "getBundledInteractiveAssetPath",
    "getChangelogPath",
    "getCustomThemesDir",
    "getDebugLogPath",
    "getDocsPath",
    "getExamplesPath",
    "getExportTemplateDir",
    "getInteractiveAssetsDir",
    "getModelsPath",
    "getPackageDir",
    "getPackageJsonPath",
    "getPromptsDir",
    "getReadmePath",
    "getSessionsDir",
    "getSettingsPath",
    "getShareViewerUrl",
    "getSelfUpdateCommand",
    "getSelfUpdateUnavailableInstruction",
    "getThemesDir",
    "getToolsDir",
    "getUpdateInstruction",
    "isBunBinary",
    "isBunRuntime",
]

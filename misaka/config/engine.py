"""Engine-side (formerly harn/pi) configuration: paths, package metadata, identity.

The identity is now MISAKA's: dist metadata is looked up under ``misaka``, the
config directory is ``~/.misaka/``, and agent assets live in ``~/.misaka/agent/``.
Product-side CFG lives in misaka/config/product.py.
"""

from __future__ import annotations

import json
import os
import tomllib
from functools import lru_cache
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

from misaka.utils.paths import normalize_path


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
    # Use __file__-relative resolution so that theme files are found correctly
    # both when running from source (uv run misaka) and when installed as a
    # package (uv tool install misaka / pip install misaka).
    return str(_get_package_module_dir() / "ui" / "tui" / "interactive" / "theme")


def get_export_template_dir() -> str:
    return str(_get_package_module_dir() / "core" / "export_html")


def get_package_json_path() -> str:
    metadata_path = _get_package_metadata_path()
    if metadata_path is not None:
        return str(metadata_path)
    return str(Path(get_package_dir()) / "package.json")


def get_readme_path() -> str:
    # Use __file__-relative resolution so that bundled assets are found correctly
    # both when running from source (uv run harn) and when installed as a
    # package (uv tool install harn / pip install harn).
    return str((_get_package_module_dir() / "README.md").resolve())


def get_docs_path() -> str:
    return str((_get_package_module_dir() / "docs").resolve())


def get_examples_path() -> str:
    return str((_get_package_module_dir() / "examples").resolve())


def get_changelog_path() -> str:
    return str((_get_package_module_dir() / "CHANGELOG.md").resolve())


def get_sessions_dir() -> str:
    env_dir = os.environ.get(ENV_SESSION_DIR)
    if env_dir:
        return expand_tilde_path(env_dir)
    return str(Path(get_agent_dir()) / "sessions")


def get_debug_log_path() -> str:
    return str(Path(get_agent_dir()) / f"{APP_NAME}-debug.log")


expandTildePath = expand_tilde_path
getShareViewerUrl = get_share_viewer_url
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
getSessionsDir = get_sessions_dir
getDebugLogPath = get_debug_log_path

__all__ = [
    "APP_NAME",
    "APP_TITLE",
    "CONFIG_DIR_NAME",
    "ENV_AGENT_DIR",
    "ENV_SESSION_DIR",
    "PACKAGE_NAME",
    "VERSION",
    "expandTildePath",
    "getAgentDir",
    "getAuthPath",
    "getBinDir",
    "getChangelogPath",
    "getCustomThemesDir",
    "getDebugLogPath",
    "getDocsPath",
    "getExamplesPath",
    "getExportTemplateDir",
    "getModelsPath",
    "getPackageDir",
    "getPackageJsonPath",
    "getPromptsDir",
    "getReadmePath",
    "getSessionsDir",
    "getSettingsPath",
    "getShareViewerUrl",
    "getThemesDir",
    "getToolsDir",
]

"""Engine-side (formerly harn/pi) configuration: paths, package metadata, identity.

The identity is now MISAKA's: dist metadata is looked up under ``misaka``, the
config directory is ``~/.misaka/``, and agent assets live in ``~/.misaka/agent/``.
Product-side CFG lives in misaka/config/product.py.
"""

from __future__ import annotations

import os
from importlib import metadata as importlib_metadata
from pathlib import Path

from misaka.utils.paths import normalize_path

# MISAKA is not white-labelled: these were read out of a package.json / [tool.harn]
# section that this repository does not have, so every one of them always took its
# fallback. The version is the one value with a real source.
APP_NAME = "misaka"
APP_TITLE = "misaka"
CONFIG_DIR_NAME = ".misaka"
ENV_AGENT_DIR = "MISAKA_CODING_AGENT_DIR"
ENV_SESSION_DIR = "MISAKA_CODING_AGENT_SESSION_DIR"

try:
    VERSION = importlib_metadata.version("misaka")
except importlib_metadata.PackageNotFoundError:
    VERSION = "0.0.0"


def expand_tilde_path(path: str) -> str:
    return normalize_path(path)


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


def get_bin_dir() -> str:
    return str(Path(get_agent_dir()) / "bin")


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


def get_sessions_dir() -> str:
    env_dir = os.environ.get(ENV_SESSION_DIR)
    if env_dir:
        return expand_tilde_path(env_dir)
    return str(Path(get_agent_dir()) / "sessions")


__all__ = [
    "APP_NAME",
    "APP_TITLE",
    "CONFIG_DIR_NAME",
    "ENV_AGENT_DIR",
    "ENV_SESSION_DIR",
    "VERSION",
    ]

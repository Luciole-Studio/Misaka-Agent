"""Engine-side (formerly harn/pi) configuration: package metadata and the engine's path accessors.

Every location comes from the one table in :mod:`misaka.config.home`; the functions here keep
the names pi's code calls them by. Product-side CFG lives in misaka/config/product.py.
"""

from __future__ import annotations

from importlib import metadata as importlib_metadata
from pathlib import Path

from misaka.config import home
from misaka.utils.paths import normalize_path

# MISAKA is not white-labelled: these were read out of a package.json / [tool.harn]
# section that this repository does not have, so every one of them always took its
# fallback. The version is the one value with a real source.
APP_NAME = "misaka"
APP_TITLE = "misaka"
CONFIG_DIR_NAME = home.DIR_NAME

try:
    VERSION = importlib_metadata.version("misaka")
except importlib_metadata.PackageNotFoundError:
    VERSION = "0.0.0"


def expand_tilde_path(path: str) -> str:
    return normalize_path(path)


def get_agent_dir() -> str:
    return str(home.path("agent"))


def get_custom_themes_dir() -> str:
    return str(home.path("themes"))


def get_models_path() -> str:
    return str(home.path("models"))


def get_auth_path() -> str:
    return str(home.path("auth"))


def get_debug_log_path() -> str:
    """Where /debug writes. Named after the app so a user can find it without reading source."""
    return str(home.path("debug_log"))


def get_log_path() -> str:
    """Where warnings and errors from every misaka process go."""
    return str(home.path("log"))


def configure_logging() -> str | None:
    """Give the root logger a file so ``logger.warning`` never reaches the terminal.

    Without a handler Python's last-resort handler prints every warning to stderr, and a
    TUI's stderr is its own screen: the text lands wherever the cursor is, usually the input
    box (2026-09-18, B6). One rotating file per user, shared by the panel, the daemon and
    every session process, at WARNING and above. Idempotent; a failure to open the file
    leaves logging as it was rather than stopping the program.
    """
    import logging
    from logging.handlers import RotatingFileHandler

    root = logging.getLogger()
    path = get_log_path()
    for handler in root.handlers:
        if getattr(handler, "baseFilename", None) == path:
            return path
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    except OSError:
        return None
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s pid=%(process)d %(name)s: %(message)s"))
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.WARNING:
        root.setLevel(logging.WARNING)
    return path


def get_bin_dir() -> str:
    return str(home.path("bin"))


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
    """The store of sessions created without an explicit directory.

    pi's is ``<agent dir>/sessions``. MISAKA's is the coordinator's root in the product
    tree (``config.sessions``): a session that names no role is Last Order's, and it has
    to land where ``/resume`` and the panel look.
    """
    from misaka.config import sessions

    return sessions.role_dir()


__all__ = [
    "APP_NAME",
    "APP_TITLE",
    "CONFIG_DIR_NAME",
    "VERSION",
    ]

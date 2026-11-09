"""Translated from pi's ``packages/coding-agent/src/config.ts`` (v1.1.0), as far as MISAKA keeps it.

MISAKA fork: the home is MISAKA's own layout (``misaka.config.home``), so every path comes from that table and
``getAgentDir`` reads ``MISAKA_HOME``. pi's install-method detection and self-update, package/docs/examples/changelog
paths, the bundled interactive assets and the QuickJS wasm paths are not ported: MISAKA updates through
``misaka update`` and ships none of those files. ``configure_logging``, ``bundle_root`` and ``bundle_installer`` are MISAKA's.

Every location comes from the one table in :mod:`misaka.config.home`; the functions here keep
the names pi's code calls them by. Product-side CFG lives in misaka/config/product.py.
"""

from __future__ import annotations

import itertools
import os
import sys
from importlib import metadata as importlib_metadata
from pathlib import Path

from misaka.config import home
from misaka.utils.paths import normalizePath

# `{ kind: "updated"; version }` or `{ kind: "removed" }`.
type InstallChange = dict[str, str]


def detectInstallChange(metadataPath: Path | None = None) -> InstallChange | None:
    """Detect that the package this process runs from changed on disk, for example after `misaka update`
    in another terminal. Code loaded on demand can then be missing or from another version.

    Checks the metadata read at startup. Resolving it again could find another installation of the
    package on the path.
    """
    path = metadataPath if metadataPath is not None else _startupMetadataPath
    # MISAKA fork: a release archive keeps every version in a directory of its own, so replacing it does not
    # affect this process (pi: the Bun binary embeds its code).
    if bundle_root() is not None or not path:
        return None
    try:
        installed = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {"kind": "removed"}
    except OSError:
        return None
    version = next(
        (line.partition(":")[2].strip() for line in installed.splitlines() if line.startswith("Version:")), None
    )
    return {"kind": "updated", "version": version} if version and version != VERSION else None


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
    return str(_get_package_module_dir() / "modes" / "interactive" / "theme")


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


def _startupMetadataFile() -> Path | None:
    """MISAKA fork: pi records the package.json it read at startup; here the distribution's METADATA file."""
    try:
        distribution = importlib_metadata.distribution("misaka")
    except importlib_metadata.PackageNotFoundError:
        return None
    for file in distribution.files or []:
        if file.name == "METADATA" and file.parent.name.endswith(".dist-info"):
            return Path(str(distribution.locate_file(file)))
    return None


# The distribution metadata this process started from, if one existed.
_startupMetadataPath: Path | None = _startupMetadataFile()


def expand_tilde_path(path: str) -> str:
    return normalizePath(path)


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


def bundle_root() -> Path | None:
    """The release archive this interpreter runs from, or None. An archive carries its own
    CPython at ``python/`` beside ``bin/misaka`` and ``BUNDLED.txt`` (scripts/build_release.py)."""
    root = Path(sys.prefix).parent
    return root if (root / "BUNDLED.txt").is_file() and (root / "bin").is_dir() else None


def bundle_installer(root: Path) -> str:
    """Who put a release archive where it is: Homebrew keeps it in its Cellar, winget under its
    Packages folder; "" for MISAKA's own installer. Each is updated and removed by its own tool."""
    parts = [part.casefold() for part in Path(os.path.realpath(root)).parts]
    pairs = set(itertools.pairwise(parts))
    if ("cellar", "misaka") in pairs:                      # <prefix>/Cellar/misaka/<version>/libexec
        return "Homebrew"
    if ("winget", "packages") in pairs and any(part.startswith("luciole-studio.misaka") for part in parts):
        return "winget"
    return ""


__all__ = [
    "APP_NAME",
    "APP_TITLE",
    "CONFIG_DIR_NAME",
    "VERSION",
    "InstallChange",
    "detectInstallChange",
    ]

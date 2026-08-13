"""Windows self-update helpers."""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path
from uuid import uuid4

_QUARANTINE_DIR_NAME = ".harn-native-quarantine"
_SHARED_OBJECT_SUFFIXES = {".dll", ".dylib", ".pyd", ".so"}


def _normalize_path(path: str) -> Path:
    return Path(path).resolve()


def _get_quarantine_root(package_dir: str) -> Path | None:
    current = _normalize_path(package_dir)
    while True:
        if current.name.lower() == "node_modules":
            return current / _QUARANTINE_DIR_NAME
        if current.parent == current:
            return None
        current = current.parent


def _is_inside_root(file_path: Path, root: Path) -> bool:
    comparison_file = str(file_path).lower()
    comparison_root = str(root).lower()
    try:
        relative = os.path.relpath(comparison_file, comparison_root)
    except ValueError:
        return False
    return relative == "." or (
        relative != ".." and not relative.startswith(f"..{os.sep}") and not os.path.isabs(relative)
    )


def _iter_loaded_shared_object_paths() -> list[Path]:
    loaded_files: list[Path] = []
    seen: set[str] = set()
    for module in tuple(sys.modules.values()):
        file_path = getattr(module, "__file__", None)
        if not isinstance(file_path, str):
            continue
        resolved = _normalize_path(file_path)
        if resolved.suffix.lower() not in _SHARED_OBJECT_SUFFIXES:
            continue
        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)
        loaded_files.append(resolved)
    return loaded_files


def _get_loaded_shared_objects_in_package_dir(package_dir: str) -> list[Path]:
    resolved_package_dir = _normalize_path(package_dir)
    return [
        loaded_file
        for loaded_file in _iter_loaded_shared_object_paths()
        if _is_inside_root(loaded_file, resolved_package_dir)
    ]


def cleanup_windows_self_update_quarantine(package_dir: str) -> None:
    quarantine_root = _get_quarantine_root(package_dir)
    if quarantine_root is None:
        return
    try:
        shutil.rmtree(quarantine_root)
    except FileNotFoundError:
        return
    except OSError:
        return


def quarantine_windows_native_dependencies(package_dir: str) -> None:
    resolved_package_dir = _normalize_path(package_dir)
    quarantine_root = _get_quarantine_root(str(resolved_package_dir))
    if quarantine_root is None:
        return

    loaded_files = _get_loaded_shared_objects_in_package_dir(str(resolved_package_dir))
    if not loaded_files:
        return

    quarantine_run_dir = quarantine_root / f"{int(time.time() * 1000)}-{os.getpid()}-{uuid4()}"
    for loaded_file in loaded_files:
        if not loaded_file.exists():
            continue
        quarantine_path = quarantine_run_dir / os.path.relpath(loaded_file, resolved_package_dir)
        quarantine_path.parent.mkdir(parents=True, exist_ok=True)
        loaded_file.replace(quarantine_path)
        shutil.copyfile(quarantine_path, loaded_file)


cleanupWindowsSelfUpdateQuarantine = cleanup_windows_self_update_quarantine
quarantineWindowsNativeDependencies = quarantine_windows_native_dependencies

__all__ = ["cleanupWindowsSelfUpdateQuarantine", "quarantineWindowsNativeDependencies"]

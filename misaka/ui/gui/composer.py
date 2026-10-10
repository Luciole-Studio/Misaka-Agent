"""Read-only project file and skill catalogues for the GUI composer."""
from __future__ import annotations

import os
from pathlib import Path
import threading
import time


IGNORED_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".gui-runtime"}


class ComposerFiles:
    def __init__(self):
        self.cache = {}
        self.lock = threading.Lock()

    def search(self, workspace, query=""):
        root = Path(workspace).resolve()
        key = os.path.normcase(str(root))
        with self.lock:
            cached = self.cache.get(key)
            if not cached or time.monotonic() - cached[0] > 15:
                files, truncated = [], False
                started = time.monotonic()
                for folder, dirs, names in os.walk(root, followlinks=False):
                    dirs[:] = sorted(d for d in dirs if d.casefold() not in IGNORED_DIRS
                                     and not Path(folder, d).is_symlink()
                                     and not Path(folder, d).is_junction())
                    for name in sorted(names):
                        path = Path(folder, name)
                        try:
                            if not path.is_file() or not path.resolve().is_relative_to(root):
                                continue
                        except OSError:
                            continue
                        files.append({"path": path.relative_to(root).as_posix(), "name": name})
                        if len(files) >= 40000 or time.monotonic() - started > 3:
                            truncated = True
                            break
                    if len(files) >= 40000 or time.monotonic() - started > 3:
                        truncated = True
                        break
                cached = (time.monotonic(), files, truncated)
                self.cache[key] = cached
                if len(self.cache) > 16:
                    self.cache.pop(next(iter(self.cache)))
        query = str(query).casefold().replace("\\", "/")
        matches = [f for f in cached[1] if query in f["path"].casefold()]
        matches.sort(key=lambda f: (not f["name"].casefold().startswith(query), len(f["path"]), f["path"].casefold()))
        return {"files": matches[:60], "total": len(matches), "truncated": cached[2], "workspace": str(root)}


def referenced_message(text, files, workspace):
    if not files:
        return text
    if not isinstance(files, list) or len(files) > 24 or any(not isinstance(p, str) for p in files):
        raise ValueError("文件引用格式有误，最多引用 24 个文件")
    root = Path(workspace).resolve()
    paths = []
    for value in dict.fromkeys(files):
        path = (root / value).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("引用文件不存在或不属于这段对话的项目")
        paths.append(str(path))
    return text + "\n\n[引用的当前项目文件，可按需读取]\n" + "\n".join("- " + p for p in paths)


def skill_role(role):
    if role in (None, "", "last-order", "last_order"):
        return None
    if not isinstance(role, str):
        raise ValueError("未知的 Sister")
    return role.removeprefix("sister-")


def skill_catalogue(workspace, role=None):
    from misaka.config import current_config, sisters
    from misaka.core.skills import index
    from misaka.core.skills.layers import skill_roots
    from misaka.core.skills.wiring.skills import _runtime_name, _slash_entries

    role = skill_role(role)
    if role and role not in sisters():
        raise ValueError("未知的 Sister")
    cfg = current_config()
    profile = os.path.join(cfg["profiles_root"], role) if role else os.path.join(cfg["roles_root"], "last_order")
    entries = _slash_entries(index.runtime_build(skill_roots(profile, workspace)))
    return {"skills": [{"name": _runtime_name(e), "description": e.get("list_description") or e.get("description", "")}
                       for e in entries]}

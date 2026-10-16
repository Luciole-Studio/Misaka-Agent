"""GUI conversation preferences and recoverable deletion, without rewriting JSONL."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time

from filelock import FileLock


def path_key(path):
    return os.path.normcase(os.path.realpath(path))


class SessionLibrary:
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_bytes().decode("utf-8-sig", errors="strict"))
        if not isinstance(data, dict) or any(not isinstance(v, dict) for v in data.values()):
            raise ValueError("会话管理记录格式有误，请保留原文件后检查")
        return data

    def preferences(self, workspace):
        key = path_key(workspace)
        return {k: v for k, v in self.read().items() if path_key(v.get("workspace", "")) == key}

    def update(self, session, operation, value=None):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.path) + ".lock"):
            data = self.read()  # Strictly confirm existing encoding before writing.
            key = path_key(session["path"])
            entry = {**session, **data.get(key, {}), "path": session["path"],
                     "workspace": session["workspace"], "updated": time.time()}
            if operation == "pin":
                entry["pinned_at"] = (entry.get("pinned_at") or time.time()) if value else 0
            elif operation == "rename":
                entry["title"] = value
            elif operation == "delete":
                entry["deleted_at"] = time.time()
            elif operation == "restore":
                entry["deleted_at"] = 0
            else:
                raise ValueError("未知会话管理操作")
            data[key] = entry
            encoding = "utf-8-sig" if self.path.exists() and self.path.read_bytes().startswith(b"\xef\xbb\xbf") else "utf-8"
            fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".gui-sessions-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding=encoding, newline="\n") as stream:
                    json.dump(data, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                os.replace(temporary, self.path)
            finally:
                Path(temporary).unlink(missing_ok=True)
            return entry

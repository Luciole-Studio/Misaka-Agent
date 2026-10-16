"""Persistent project bookmarks, independent of a browser origin or GUI port."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from filelock import FileLock


class Projects:
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_bytes().decode("utf-8-sig", errors="strict"))
        if not isinstance(data, list) or any(not isinstance(p, str) for p in data):
            raise ValueError("项目列表格式有误，请保留原文件后检查")
        return data

    def add(self, paths):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.path) + ".lock"):
            saved = self.read()
            keys = {os.path.normcase(p) for p in saved}
            for path in paths:
                key = os.path.normcase(path)
                if key not in keys:
                    saved.append(path)
                    keys.add(key)
            if saved == self.read() and self.path.exists():
                return saved
            # Strictly decode above before replacement; retain an existing UTF-8 BOM.
            encoding = "utf-8-sig" if self.path.exists() and self.path.read_bytes().startswith(b"\xef\xbb\xbf") else "utf-8"
            fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".gui-projects-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding=encoding, newline="\n") as stream:
                    json.dump(saved, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                os.replace(temporary, self.path)
            finally:
                Path(temporary).unlink(missing_ok=True)
            return saved

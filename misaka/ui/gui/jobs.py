"""Noninteractive CLI jobs with bounded plain-text output for the GUI."""
import os
import subprocess
import sys
import threading
import uuid


class Jobs:
    def __init__(self):
        self.items = {}
        self.lock = threading.Lock()

    def start(self, workspace, args, title):
        key = uuid.uuid4().hex
        item = {"id": key, "workspace": workspace, "title": title,
                "status": "running", "output": "", "exit_code": None}
        proc = subprocess.Popen(
            [sys.executable, "-u", "-X", "utf8", "-m", "misaka", *args],
            cwd=workspace, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "NO_COLOR": "1", "TERM": "dumb", "PYTHONUTF8": "1"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with self.lock:
            self.items[key] = item

        def read():
            try:
                for line in proc.stdout:
                    with self.lock:
                        item["output"] = (item["output"] + line)[-200000:]
                code = proc.wait()
                with self.lock:
                    item.update(status="done" if code == 0 else "failed", exit_code=code)
            finally:
                proc.stdout.close()
        threading.Thread(target=read, daemon=True).start()
        return {"job_id": key}

    def get(self, key, workspace):
        with self.lock:
            item = self.items.get(key)
            if not item or os.path.normcase(item["workspace"]) != os.path.normcase(workspace):
                raise ValueError("当前项目没有这项操作")
            return dict(item)

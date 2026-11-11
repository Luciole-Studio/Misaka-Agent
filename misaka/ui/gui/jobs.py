"""Noninteractive CLI jobs with bounded plain-text output for the GUI."""
import codecs
import os
import subprocess
import sys
import threading
import time
import uuid
from collections import deque

from misaka.utils.shell import killProcessTree

OUTPUT_LIMIT = 200000
FINISHED_RETENTION = 86400


class Jobs:
    def __init__(self, *, timeout=1800, finished_limit=64, program=None):
        """Configure command execution limits and the synchronized job receipt registry."""
        self.items = {}
        self.lock = threading.Lock()
        self.timeout = timeout
        self.finished_limit = finished_limit
        self.program = program or [sys.executable, "-u", "-X", "utf8", "-m", "misaka"]

    def _prune_locked(self):
        """Evict old terminal receipts while leaving running jobs accessible."""
        finished = sorted((item for item in self.items.values() if item.get("_finished_at") is not None),
                          key=lambda item: item["_finished_at"])
        cutoff = time.monotonic() - FINISHED_RETENTION
        excess = max(0, len(finished) - self.finished_limit)
        for index, item in enumerate(finished):
            if index < excess or item["_finished_at"] <= cutoff:
                self.items.pop(item["id"], None)

    def _append_locked(self, item, chunk):
        """Retain an exact byte-bounded output tail without copying it per line."""
        chunk = chunk[-OUTPUT_LIMIT:]
        item["_chunks"].append(chunk)
        item["_bytes"] += len(chunk)
        while item["_bytes"] > OUTPUT_LIMIT:
            first = item["_chunks"].popleft()
            trim = min(len(first), item["_bytes"] - OUTPUT_LIMIT)
            if trim < len(first):
                item["_chunks"].appendleft(first[trim:])
            item["_bytes"] -= trim

    def _stop(self, item, message):
        """Stop this GUI-owned process tree and retain an explicit failure reason."""
        with self.lock:
            if item.get("_finished_at") is not None:
                return
            item["error"] = item.get("error") or message
            item["status"] = "failed"
            self._append_locked(item, ("\n" + message + "\n").encode("utf-8"))
        proc = item["_process"]
        if proc.poll() is None:
            try:
                killProcessTree(proc.pid)
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired) as error:
                with self.lock:
                    item["error"] += f"; failed to terminate process: {error}"

    def start(self, workspace, args, title):
        """Launch a noninteractive CLI command with bounded output and a timeout."""
        key = uuid.uuid4().hex
        item = {"id": key, "workspace": workspace, "title": title,
                "status": "running", "exit_code": None, "_chunks": deque(), "_bytes": 0}
        proc = subprocess.Popen(
            [*self.program, *args],
            cwd=workspace, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=True,
            env={**os.environ, "NO_COLOR": "1", "TERM": "dumb", "PYTHONUTF8": "1"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with self.lock:
            self._prune_locked()
            item["_process"] = proc
            self.items[key] = item
        timer = threading.Timer(self.timeout, self._stop, args=(item, "操作超过时限，已请求终止。请查看已有输出后再决定是否重试。"))
        timer.daemon = True
        timer.start()

        def read():
            """Drain process output and finalize the job receipt after exit or read failure."""
            code = None
            try:
                while chunk := proc.stdout.read1(8192):
                    with self.lock:
                        self._append_locked(item, chunk)
                code = proc.wait(timeout=max(5, self.timeout))
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                self._stop(item, f"操作输出读取或结束失败：{str(error) or type(error).__name__}")
            finally:
                timer.cancel()
                proc.stdout.close()
                with self.lock:
                    code = proc.poll() if code is None else code
                    item.update(status="done" if code == 0 and not item.get("error") else "failed",
                                exit_code=code, _finished_at=time.monotonic())
                    self._prune_locked()
        threading.Thread(target=read, daemon=True).start()
        return {"job_id": key}

    def get(self, key, workspace):
        """Return a workspace-scoped job receipt with a safely decoded output tail."""
        with self.lock:
            self._prune_locked()
            item = self.items.get(key)
            if not item or os.path.normcase(item["workspace"]) != os.path.normcase(workspace):
                raise ValueError("当前项目没有这项操作")
            raw = b"".join(item["_chunks"])
            start = 0
            while start < len(raw) and raw[start] & 0xC0 == 0x80:
                start += 1
            # A byte-bounded tail can start or currently end inside a character.
            # Keep complete UTF-8 characters; a later poll receives the next bytes.
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            output = decoder.decode(raw[start:], final=item.get("_finished_at") is not None)
            return {**{name: value for name, value in item.items() if not name.startswith("_")}, "output": output}

    def close_all(self):
        """Stop GUI command jobs during server shutdown; research owners are separate."""
        with self.lock:
            running = [item for item in self.items.values() if item.get("_finished_at") is None]
        for item in running:
            self._stop(item, "网页服务已关闭，已请求终止操作。请查看已有输出后再决定是否重试。")

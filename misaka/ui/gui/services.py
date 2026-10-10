"""Terminal-free services for the GUI: settings calls, browser sign-in, background research.

Settings run in ``settings_worker`` processes (one per call, one per sign-in). Research
runs are ``misaka research`` processes with no terminal attached: their own session group,
output to a log file, so closing the GUI server leaves them running exactly as closing a
pane used to. Plan approval never needed the terminal -- it is a conversation with the
node's Last Order, which the GUI opens as an attached session.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from misaka.ui.gui.settings_worker import MARK

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_NEW_GROUP = 0x00000200 if os.name == "nt" else 0
WORKER = [sys.executable, "-u", "-X", "utf8", "-m", "misaka.ui.gui.settings_worker"]
SLOW_OPS = {"test_provider": 90, "fetch_provider_models": 90, "verify": 90, "ping_custom": 90, "probe_custom": 60,
            "web_overview": 90, "web_save": 60, "web_provider": 60, "web_browser": 60, "web_enable": 60}


def _env() -> dict:
    return {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1", "TERM": "dumb"}


def _marked(line: str):
    if not line.startswith(MARK):
        return None
    try:
        payload = json.loads(line[len(MARK):])
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


class Settings:
    def __init__(self):
        self.logins: dict[str, dict] = {}
        self.lock = threading.Lock()

    def call(self, op: str, params: dict, workspace: str) -> dict:
        request = json.dumps({"op": op, "params": params, "workspace": workspace}, ensure_ascii=False)
        try:
            done = subprocess.run(WORKER, input=request + "\n", capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", cwd=workspace, env=_env(),
                                  timeout=SLOW_OPS.get(op, 45), creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise TimeoutError("设置操作超时，请稍后重试") from None
        for line in reversed(done.stdout.splitlines()):
            payload = _marked(line)
            if payload is not None and "ok" in payload:
                if not payload["ok"]:
                    raise ValueError(payload.get("error") or "设置操作失败")
                return payload.get("data") or {}
        tail = (done.stderr or "").strip().splitlines()[-3:]
        raise RuntimeError("设置进程没有返回结果" + ("：" + " ".join(tail) if tail else ""))

    # ---- browser / device sign-in: a worker that talks back ----

    def login_start(self, provider: str, workspace: str) -> dict:
        key = uuid.uuid4().hex[:12]
        proc = subprocess.Popen(WORKER, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace", cwd=workspace, env=_env(),
                                bufsize=1, creationflags=_NO_WINDOW)
        job = {"id": key, "provider": provider, "events": [], "status": "running", "message": "", "proc": proc,
               "prompt": None, "started": time.time()}
        with self.lock:
            for old in [k for k, v in self.logins.items() if v["status"] != "running" and time.time() - v["started"] > 3600]:
                self.logins.pop(old, None)
            self.logins[key] = job
        proc.stdin.write(json.dumps({"op": "login", "params": {"provider": provider}, "workspace": workspace}) + "\n")
        proc.stdin.flush()

        def pump():
            for line in proc.stdout:
                payload = _marked(line.rstrip("\n"))
                if payload is None:
                    continue
                with self.lock:
                    if "ok" in payload:
                        job["status"] = "done" if payload["ok"] else "failed"
                        job["message"] = (payload.get("data") or {}).get("message", "") if payload["ok"] else payload.get("error", "")
                        job["prompt"] = None
                    elif payload.get("event") == "prompt":
                        job["prompt"] = payload
                    elif payload.get("event") == "prompt_done":
                        if job["prompt"] and job["prompt"].get("id") == payload.get("id"):
                            job["prompt"] = None
                    else:
                        job["events"].append(payload)
            proc.wait()
            with self.lock:
                if job["status"] == "running":
                    job["status"], job["message"] = "failed", "登录进程意外退出"
        threading.Thread(target=pump, daemon=True).start()
        return {"login_id": key}

    def _job(self, key) -> dict:
        job = self.logins.get(key) if isinstance(key, str) else None
        if job is None:
            raise ValueError("登录已结束，请重新开始")
        return job

    def login_status(self, key) -> dict:
        with self.lock:
            job = self._job(key)
            return {k: v for k, v in job.items() if k != "proc"}

    def login_answer(self, key, prompt_id, value) -> dict:
        with self.lock:
            job = self._job(key)
            if job["status"] != "running":
                raise ValueError("登录已结束")
        line = json.dumps({"id": prompt_id, "value": value}, ensure_ascii=False)
        job["proc"].stdin.write(line + "\n")
        job["proc"].stdin.flush()
        return {"sent": True}

    def login_cancel(self, key) -> dict:
        with self.lock:
            job = self._job(key)
            if job["status"] == "running":
                job["status"], job["message"] = "failed", "已取消登录"
        try:
            job["proc"].kill()
        except OSError:
            pass
        return {"cancelled": True}


RUN_LINE = re.compile(r"Research run (\S+?):")


class ResearchProcesses:
    """``misaka research`` without a terminal; one log per process, kept in the temp folder."""

    def __init__(self):
        self.items: dict[str, dict] = {}
        self.lock = threading.Lock()

    def start(self, workspace: str, args: list[str], title: str) -> dict:
        key = uuid.uuid4().hex[:12]
        log_path = os.path.join(tempfile.gettempdir(), f"misaka-gui-research-{key}.log")
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(
                [sys.executable, "-u", "-X", "utf8", "-m", "misaka", *args], cwd=workspace,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=_env(),
                creationflags=_NO_WINDOW | _NEW_GROUP,
                **({} if os.name == "nt" else {"start_new_session": True}))
        item = {"id": key, "workspace": workspace, "title": title, "log": log_path, "proc": proc,
                "run_id": args[args.index("--resume") + 1] if "--resume" in args else None, "started": time.time()}
        with self.lock:
            self.items[key] = item
        return {"job_id": key}

    def _tail(self, path: str, limit: int = 60000) -> str:
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - limit))
                return f.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    def listing(self, workspace: str) -> list[dict]:
        rows = []
        with self.lock:
            items = list(self.items.values())
        for item in items:
            if os.path.normcase(item["workspace"]) != os.path.normcase(workspace):
                continue
            if item["run_id"] is None:
                match = RUN_LINE.search(self._tail(item["log"], 4000))
                if match:
                    item["run_id"] = match.group(1)
            code = item["proc"].poll()
            rows.append({"id": item["id"], "title": item["title"], "run_id": item["run_id"], "started": item["started"],
                         "running": code is None, "exit_code": code})
        return rows

    def log(self, workspace: str, key: str | None = None, run_id: str | None = None) -> dict:
        with self.lock:
            candidates = [i for i in self.items.values()
                          if os.path.normcase(i["workspace"]) == os.path.normcase(workspace)
                          and (i["id"] == key or (run_id and i["run_id"] == run_id))]
        if not candidates:
            raise ValueError("这次启动的日志不在本次网页服务中；研究进度仍可在研究记录里查看")
        item = max(candidates, key=lambda i: i["started"])
        return {"title": item["title"], "running": item["proc"].poll() is None, "exit_code": item["proc"].poll(),
                "output": self._tail(item["log"])}

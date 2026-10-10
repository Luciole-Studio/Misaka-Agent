"""GUI-side manager for native chat runner processes.

Each chat is one ``misaka.ui.gui.chat_runner`` subprocess. This module owns the
processes, turns their stdout event streams into per-chat ring buffers that the
browser long-polls with a cursor, and shuttles request/response ops over stdin.
Nothing here touches a terminal or a PTY: the runner is a structured IPC peer,
never a screen to scrape.
"""
from __future__ import annotations

import json
import itertools
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque

from misaka.config import sisters

# Events the browser replays after a reconnect. A trimmed ring older than the
# client's cursor forces a full replay ("reset"), which rebuilds identical state.
RING_LIMIT = 6000
REQUEST_TIMEOUT = 25.0
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class ChatChannel:
    """One runner process plus its event ring and pending op waiters."""

    def __init__(self, chat_id: str, meta: dict, log_path: str):
        self.id = chat_id
        self.meta = meta
        self.meta.setdefault("updatedAt", time.time())
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.events: deque[tuple[int, dict]] = deque(maxlen=RING_LIMIT)
        self.base_seq = 0     # seq of events[0]; everything below was trimmed
        self.next_seq = 0
        self.request_counter = itertools.count(1)
        self.status = "starting"
        self.condition = threading.Condition(self.lock)
        self.pending: dict[int, dict] = {}   # request id -> {"event": threading.Event, "reply": dict}
        self.write_lock = threading.Lock()

    def record(self, payload: dict) -> None:
        """Reader thread: a result line resolves its waiter; anything else streams."""
        kind = payload.get("type")
        if kind == "result":
            waiter = self.pending.pop(payload.get("id"), None)
            if waiter is not None:
                if waiter.get("op") == "snapshot" and payload.get("ok"):
                    payload.setdefault("data", {})["cursor"] = self.next_seq
                waiter["reply"] = payload
                waiter["event"].set()
            return
        if kind in {"ready", "session_settings"}:
            self.status = "ready"
            self.meta.update({key: payload[key] for key in
                              ("sessionId", "sessionFile", "cwd", "name", "model",
                               "thinkingLevel", "availableThinkingLevels", "modelFallbackMessage", "pendingPrompts", "capabilities")
                              if key in payload})
        elif kind == "prompt_queue":
            self.meta["pendingPrompts"] = payload.get("pendingPrompts", [])
            self.meta["queuePaused"] = bool(payload.get("queuePaused"))
        elif kind == "fatal":
            self.status = "error"
            self.meta["error"] = payload.get("message", "会话进程异常退出")
        elif kind == "event":
            inner = payload.get("event") or {}
            self.meta["updatedAt"] = time.time()
            if inner.get("type") == "agent_start":
                self.meta["streaming"] = True
            elif inner.get("type") in ("agent_end", "auto_retry_end"):
                self.meta["streaming"] = False
            elif inner.get("type") == "session_info_changed":
                self.meta["name"] = inner.get("name")
        elif kind == "bye":
            self.status = "closed"
        with self.condition:
            self.trim()
            self.events.append((self.next_seq, payload))
            self.next_seq += 1
            self.condition.notify_all()

    def trim(self) -> None:
        while len(self.events) >= RING_LIMIT:
            self.events.popleft()
            self.base_seq += 1

    def wait_for_result(self, request_id: int, timeout: float, waiter: dict) -> dict:
        if not waiter["event"].wait(timeout):
            self.pending.pop(request_id, None)
            raise TimeoutError("会话没有在时限内应答；它可能仍在处理，请稍后在界面查看结果")
        reply = waiter["reply"]
        if not reply.get("ok", False):
            raise ValueError(reply.get("error", "会话操作失败"))
        return reply.get("data") or {}

    def drop_waiters(self) -> None:
        for waiter in list(self.pending.values()):
            waiter["event"].set()
        self.pending.clear()

    def send_raw(self, payload: dict) -> None:
        if self.proc is None or self.proc.stdin is None or self.proc.poll() is not None:
            raise ValueError("会话进程已退出")
        line = json.dumps(payload, ensure_ascii=False)
        with self.write_lock:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()

    def summary(self) -> dict:
        return {"id": self.id, **{key: self.meta.get(key) for key in
                                  ("role", "workspace", "sessionId", "sessionFile", "name",
                                   "model", "thinkingLevel", "availableThinkingLevels",
                                   "streaming", "error", "startedAt", "updatedAt")},
                "status": self.status}


class ChatManager:
    """Owns every native chat runner the GUI server has spawned."""

    def __init__(self, workspace: str, program: list[str] | None = None):
        self.workspace = workspace
        self.program = program or [sys.executable, "-u", "-X", "utf8",
                                   "-m", "misaka.ui.gui.chat_runner"]
        self.channels: dict[str, ChatChannel] = {}
        self.lock = threading.Lock()

    # ---- lifecycle ----

    def create(self, spec: dict) -> dict:
        workspace = os.path.realpath(spec.get("workspace") or self.workspace)
        if not os.path.isdir(workspace):
            raise ValueError("项目文件夹不存在")
        role = spec.get("role") or None
        if role:
            if not isinstance(role, str) or len(role) > 128 or role not in sisters():
                raise ValueError("未知的 Sister；请从协作成员列表选择")
        session_path = spec.get("session_path") or None
        if session_path and (not isinstance(session_path, str) or not session_path.endswith(".jsonl")
                             or not os.path.isfile(session_path)):
            raise ValueError("会话文件不存在，请刷新历史列表")
        chat_id = uuid.uuid4().hex[:12]
        log_path = os.path.join(tempfile.gettempdir(), f"misaka-gui-chat-{chat_id}.log")
        channel = ChatChannel(chat_id, {
            "role": role, "workspace": workspace, "streaming": False,
            "startedAt": time.time(),
            "sourceSession": session_path,
        }, log_path)
        with self.lock:
            self.channels[chat_id] = channel
        runner_spec = {"workspace": workspace}
        if spec.get("continue"):
            runner_spec["continue"] = True
        if role:
            runner_spec["role"] = role
        if session_path:
            runner_spec["session_path"] = session_path
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        try:
            with open(log_path, "ab") as log:
                channel.proc = subprocess.Popen(
                    [*self.program, json.dumps(runner_spec, ensure_ascii=False)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                    env=env, cwd=workspace, text=True, encoding="utf-8",
                    errors="replace", bufsize=1,
                    creationflags=_CREATE_NO_WINDOW,
                    **({} if os.name == "nt" else {"start_new_session": True}))
        except OSError as error:
            self.channels.pop(chat_id, None)
            raise ValueError(f"会话进程启动失败：{error}") from error
        threading.Thread(target=self._pump, args=(channel,), daemon=True,
                         name=f"chat-{chat_id}").start()
        return {"chat_id": chat_id}

    def _pump(self, channel: ChatChannel) -> None:
        proc = channel.proc
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    channel.record(payload)
        except (ValueError, OSError):
            pass
        finally:
            code = proc.wait(timeout=15) if proc.poll() is None else proc.poll()
            proc.stdout.close()
            if proc.stdin:
                proc.stdin.close()
            channel.status = "error" if channel.status == "starting" or code not in (0, None) else "closed"
            if channel.status == "error" and "error" not in channel.meta:
                channel.meta["error"] = f"会话进程退出（代码 {code}）；详情见 {channel.log_path}"
            channel.drop_waiters()
            with channel.condition:
                channel.trim()
                channel.events.append((channel.next_seq, {"type": "closed", "code": code}))
                channel.next_seq += 1
                channel.condition.notify_all()

    # ---- browser API ----

    def _channel(self, chat_id) -> ChatChannel:
        if not isinstance(chat_id, str) or len(chat_id) > 64:
            raise ValueError("会话标识无效")
        channel = self.channels.get(chat_id)
        if channel is None:
            raise ValueError("会话不存在或已关闭，请刷新页面")
        return channel

    def events(self, data: dict) -> dict:
        """Long-poll: hold up to ~20s until events past the cursor exist."""
        channel = self._channel(data.get("chat_id"))
        cursor = data.get("cursor", 0)
        if not isinstance(cursor, int) or cursor < 0:
            raise ValueError("游标无效")
        with channel.condition:
            reset = cursor < channel.base_seq
            if not reset and channel.next_seq <= cursor:
                channel.condition.wait(timeout=min(float(data.get("wait", 20)), 25))
                reset = cursor < channel.base_seq
            # After a reset the client has dropped its state: replay the whole ring
            # (it still holds "ready" and "history"), not just what follows.
            start = channel.base_seq if reset else max(cursor, channel.base_seq)
            items = [{"seq": seq, **payload} for seq, payload in channel.events
                     if start <= seq < channel.next_seq]
            return {"events": items, "cursor": channel.next_seq, "reset": reset,
                    "status": channel.status, "meta": {key: channel.meta.get(key) for key in
                                                       ("role", "sessionId", "sessionFile", "name", "model",
                                                        "thinkingLevel", "availableThinkingLevels",
                                                        "streaming", "error", "pendingPrompts", "capabilities")}}

    def request(self, data: dict, op: str, params: dict | None = None, timeout: float = REQUEST_TIMEOUT) -> dict:
        channel = self._channel(data.get("chat_id"))
        request_id = next(channel.request_counter) * 100000 + 7   # never collides with a cursor
        # Register before writing: a fast reply can arrive before send_raw returns.
        waiter = {"event": threading.Event(), "reply": {}, "op": op}
        channel.pending[request_id] = waiter
        try:
            channel.send_raw({"id": request_id, "op": op, "params": params or {}})
            result = channel.wait_for_result(request_id, timeout, waiter)
            if op in {"set_model", "set_thinking", "rename", "status"}:
                channel.meta.update(result)
            return result
        finally:
            channel.pending.pop(request_id, None)

    def send(self, data: dict) -> dict:
        text = data.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 262144:
            raise ValueError("消息不能为空，且不能超过 262144 个字符")
        images = data.get("images") or []
        if not isinstance(images, list) or len(images) > 6:
            raise ValueError("图片附件最多 6 张")
        return self.request(data, "prompt", {"text": text, "images": images,
                            "files": data.get("files") or [],
                            "message_id": data.get("message_id"), "display_text": data.get("display_text"),
                            "streamingBehavior": data.get("streamingBehavior", "followUp")})

    def send_now(self, data: dict) -> dict:
        return self.request(data, "send_now", {key: data.get(key) for key in
                            ("message_id", "text", "display_text", "images", "files") if key in data})

    def withdraw(self, data: dict) -> dict:
        return self.request(data, "withdraw", {"message_id": data.get("message_id")})

    def stop(self, data: dict) -> dict:
        return self.request(data, "stop", {}, timeout=60)

    def compact(self, data: dict) -> dict:
        return self.request(data, "compact", {"instructions": data.get("instructions")}, timeout=120)

    def models(self, data: dict) -> dict:
        return self.request(data, "models", {})

    def set_model(self, data: dict) -> dict:
        return self.request(data, "set_model", {"provider": data.get("provider"),
                                                "id": data.get("model"), "persist": bool(data.get("persist"))})

    def set_thinking(self, data: dict) -> dict:
        level = data.get("level")
        if level not in ("off", "minimal", "low", "medium", "high", "xhigh"):
            raise ValueError("未知思考等级")
        return self.request(data, "set_thinking", {"level": level, "persist": bool(data.get("persist"))})

    def rename(self, data: dict) -> dict:
        name = data.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ValueError("会话名称不能为空，且不能超过 120 个字符")
        return self.request(data, "rename", {"name": name})

    def ui_response(self, data: dict) -> dict:
        key = data.get("id")
        if not isinstance(key, str) or len(key) > 64:
            raise ValueError("对话框标识无效")
        return self.request(data, "ui_response", {"id": key, "value": data.get("value")})

    def close(self, chat_id, timeout: float = 5.0) -> dict:
        channel = self.channels.get(chat_id) if isinstance(chat_id, str) else None
        if channel is None:
            return {"closed": False}
        try:
            channel.send_raw({"id": -1, "op": "shutdown"})
        except (ValueError, OSError):
            pass
        try:
            channel.proc.wait(timeout=timeout)
        except Exception:  # noqa: BLE001 - a hung runner must not hang the GUI
            channel.proc.kill()
        with channel.condition:
            channel.status = "closed"
            channel.condition.notify_all()
        return {"closed": True}

    def close_all(self) -> None:
        with self.lock:
            channels = list(self.channels.values())
        for channel in channels:
            try:
                self.close(channel.id, timeout=3)
            except Exception:  # noqa: BLE001 - exit must not be blocked by one chat
                pass

    def list(self) -> list[dict]:
        with self.lock:
            channels = list(self.channels.values())
        return [channel.summary() for channel in channels]

    def alive_count(self) -> int:
        return sum(1 for channel in self.channels.values() if channel.status in ("starting", "ready"))

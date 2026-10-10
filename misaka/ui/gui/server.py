"""Local GUI bridge. No new agent engine, shell execution, or third-party web dependency.

The daemon remains the owner of sessions. Closing a browser or this HTTP server
does not stop research. All API reads and writes require an ephemeral capability;
Host and Origin checks also protect the loopback listener from hostile websites.
"""
from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from misaka.config import CFG, VERSION, current_config, home, sisters
from misaka.ui.gui.chats import ChatManager
from misaka.ui.gui.revision import source_revision
from misaka.ui.gui.jobs import Jobs
from misaka.ui.gui.projects import Projects
from misaka.ui.gui.session_library import SessionLibrary, path_key
from misaka.ui.gui.composer import ComposerFiles, referenced_message, skill_catalogue, skill_role
from misaka.ui.gui.services import ResearchProcesses, Settings
from misaka.ui.gui.terminals import open_terminal
from misaka.ui.panel import client

STATIC = Path(__file__).with_name("static")
FONT_DIR = STATIC / "fonts"
FONTS = frozenset(p.name for p in FONT_DIR.glob("*.woff2")) if FONT_DIR.is_dir() else frozenset()
# Full native commands are available in an interactive pane, never through a shell.
COMMANDS = frozenset({"chat", "research", "setup", "init", "board", "allies", "create",
                      "remove", "skills", "bundles", "moa", "web", "doc", "auth"})
SETTINGS_OPS = frozenset({"overview", "models_overview", "models", "set_key", "logout", "set_default", "verify", "provider_models", "fetch_provider_models", "test_provider", "save_model_selection",
                          "probe_custom", "ping_custom", "save_custom", "remove_custom",
                          "sisters", "create_sister", "remove_sister", "read_role_file", "write_role_file",
                          "set_research", "init_project", "web_overview", "web_save", "web_provider",
                          "web_enable", "web_browser", "terminal", "set_terminal"})
SETUP_SECTIONS = {"environment", "model", "sisters", "skills", "documents", "web", "research", "project"}
KEYS = {"enter": "\r", "escape": "\x1b", "up": "\x1b[A", "down": "\x1b[B",
        "right": "\x1b[C", "left": "\x1b[D", "tab": "\t", "backspace": "\x7f",
        "interrupt": "\x03", "home": "\x1b[H", "end": "\x1b[F",
        "pageup": "\x1b[5~", "pagedown": "\x1b[6~", "delete": "\x1b[3~"}


def required_text(data, name, limit=16384):
    value = data.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\0" in value:
        raise ValueError(f"{name} 不能为空，且不能超过 {limit} 个字符")
    return value


def workspace_path(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("请填写项目文件夹")
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise ValueError("项目路径必须是已有文件夹")
    return str(path)


def research_args(data):
    goal = required_text(data, "goal")
    limits = {"depth": (0, 10, 3), "parallel": (1, 32, 4),
              "sister_parallel": (1, 32, 4), "followups": (0, 6, 2),
              "revisions": (0, 6, 2), "max_nodes": (1, 500, 30)}
    args = ["research"]
    for name, (low, high, default) in limits.items():
        value = data.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{name} 必须是 {low} 到 {high} 之间的整数")
        args += ["--" + name.replace("_", "-"), str(value)]
    return [*args, "--", goal]


class Bridge:
    def __init__(self, workspace):
        self.workspace = workspace_path(workspace)
        self.start_lock = threading.Lock()
        self.chat_start_lock = threading.Lock()
        self.chats = ChatManager(self.workspace)
        self.jobs = Jobs()
        self.settings = Settings()
        self.research_runs = ResearchProcesses()
        self.projects = Projects(home.home() / "state" / "gui-projects.json")
        self.session_library = SessionLibrary(home.home() / "state" / "gui-session-library.json")
        self.composer_files = ComposerFiles()
        self.picker_lock = threading.Lock()

    def request(self, method, params=None):
        return client.request(method, params, timeout=20)

    def ensure(self):
        with self.start_lock:
            return client.ensure(timeout=15)

    def launch(self, data, args, title):
        cwd = workspace_path(data.get("workspace", self.workspace))
        self.ensure()
        return self.request("pane.create", {
            "argv": [sys.executable, "-m", "misaka", *args], "cwd": cwd,
            "title": title, "env": {"MISAKA_THEME": "dark", "PYTHONUTF8": "1"},
        })

    def _research(self, data, args, title):
        cwd = workspace_path(data.get("workspace", self.workspace))
        try:
            # Node conversations (where plans are approved) are listed through the session
            # service; start it so they can be opened, but a research run does not need it.
            self.ensure()
        except Exception:  # noqa: BLE001
            pass
        result = self.research_runs.start(cwd, args, title)
        return {**result, "message": "研究已在后台启动"}

    def state(self, data):
        cwd = workspace_path(data.get("workspace", self.workspace))
        cfg = current_config()
        result = {"workspace": cwd, "version": VERSION, "platform": sys.platform,
                  "model": cfg.get("lo_model") or cfg.get("default_model", ""), "provider": cfg.get("provider", ""),
                  "home": str(home.home()), "panes": [], "cards": [], "sessions": [], "connected": False,
                  "chats": [chat for chat in self.chats.list()
                            if os.path.normcase(chat["workspace"]) == os.path.normcase(cwd)],
                  "sisters": sorted(sisters()), "preferences": self.session_library.preferences(cwd)}
        try:
            result["panes"] = self.request("panes.list")["panes"]
            result.update(self.request("cards.list", {"workspace": cwd}))
            result["connected"] = True
        except (ConnectionError, FileNotFoundError):
            result["notice"] = "会话服务尚未启动。点击“新建会话”即可开始。"
        return result

    # ---- native chats: the session runs in a headless runner process, not a PTY ----

    def _live_pane_session_paths(self):
        """Session files currently owned by a daemon pane, to avoid two writers."""
        paths = set()
        try:
            for pane in self.request("panes.list")["panes"]:
                if not pane.get("alive"):
                    continue
                argv = pane.get("argv", [])
                for flag in ("--session", "--catalog"):
                    if flag in argv[:-1]:
                        paths.add(os.path.abspath(argv[argv.index(flag) + 1]))
                reported = (pane.get("reported") or {}).get("session")
                if reported:
                    paths.add(os.path.abspath(reported))
        except (ConnectionError, FileNotFoundError, KeyError):
            pass
        return paths

    def native_chat(self, data):
        with self.chat_start_lock:
            return self._native_chat(data)

    def _native_chat(self, data):
        spec = {"workspace": workspace_path(data.get("workspace", self.workspace))}
        if data.get("role"):
            role = required_text(data, "role", 128)
            if role not in sisters():
                raise ValueError("未知的 Sister；请从协作成员列表选择")
            spec["role"] = role
        if data.get("session_path"):
            path = os.path.abspath(required_text(data, "session_path", 1024))
            if not path.endswith(".jsonl") or not Path(path).is_file():
                raise ValueError("会话文件不存在，请刷新历史列表")
            if path in self._live_pane_session_paths():
                raise ValueError("该会话正在终端窗格中运行；请先在那个窗格中继续，或关闭它")
            from misaka.core.session_manager import read_session_header
            header = read_session_header(path)
            if not header.get("cwd") or os.path.normcase(workspace_path(header["cwd"])) != os.path.normcase(spec["workspace"]):
                raise ValueError("该会话属于其他项目，请先切换到它的项目文件夹")
            from misaka.config import sessions as sessions_cfg
            bucket = Path(sessions_cfg.chat_dir(spec.get("role"), spec["workspace"])).resolve()
            if not Path(path).resolve().is_relative_to(bucket):
                raise ValueError("请从当前项目对应成员的历史列表恢复会话；任务记录只能查看")
            for channel in self.chats.channels.values():
                source = channel.meta.get("sessionFile") or channel.meta.get("sourceSession")
                if source and os.path.normcase(os.path.abspath(source)) == os.path.normcase(path) and channel.status in {"starting", "ready"}:
                    return {"chat_id": channel.id}
            spec["session_path"] = path
        if data.get("continue"):
            spec["continue"] = True
        return self.chats.create(spec)

    def saved_chat_sessions(self, data):
        if data.get("all_roles"):
            data = {**data, "_live_paths": self._live_pane_session_paths()}
            sessions = []
            for role in [None, *sorted(sisters())]:
                sessions.extend({**s, "role": role} for s in self.saved_chat_sessions({**data, "all_roles": False, "role": role})["sessions"])
            return {"sessions": sorted(sessions, key=lambda s: s["modified"], reverse=True)}
        import asyncio
        from misaka.config import sessions as sessions_cfg
        from misaka.core.session_manager import SessionManager
        cwd = workspace_path(data.get("workspace", self.workspace))
        role = data.get("role") or None
        if role and role not in sisters():
            raise ValueError("未知的 Sister")
        bucket = sessions_cfg.chat_dir(role, cwd)
        listing = asyncio.run(SessionManager.list(cwd, bucket))
        live = data["_live_paths"] if "_live_paths" in data else self._live_pane_session_paths()
        entries = []
        preferences = self.session_library.preferences(cwd)
        for item in sorted(listing, key=lambda s: s.modified, reverse=True):
            pref = preferences.get(path_key(item.path), {})
            if pref.get("deleted_at") and not data.get("_include_deleted"):
                continue
            entries.append({
                "id": item.id, "path": item.path, "name": item.name,
                "title": pref.get("title") or item.name or (item.firstMessage or "").strip()[:80] or "未命名会话",
                "pinned_at": pref.get("pinned_at", 0),
                "modified": item.modified.timestamp() if hasattr(item.modified, "timestamp") else 0,
                "messages": item.messageCount, "live": os.path.abspath(item.path) in live,
                "chat_id": next((c["id"] for c in self.chats.list()
                                 if c.get("sessionFile") and os.path.normcase(os.path.abspath(c["sessionFile"])) == os.path.normcase(os.path.abspath(item.path))
                                 and c["status"] in {"starting", "ready"}), None),
            })
        return {"sessions": entries, "role": role}

    def chat_snapshot(self, data):
        """Read-only dump of a saved session, rendered by the same message view.

        Parsed straight from the JSONL the way ``cli.chat.read_session_snapshot``
        does: opening a writable SessionManager here would guess at storage state
        this server does not own.
        """
        from misaka.core.session_manager import (
            _parse_jsonl_entries,
            build_context_entries,
            session_entry_to_context_messages,
        )
        from misaka.modes.jsonl import to_jsonable
        path = os.path.abspath(required_text(data, "path", 1024))
        if not path.endswith(".jsonl") or not Path(path).is_file():
            raise ValueError("会话文件不存在")
        if path in self._live_pane_session_paths():
            raise ValueError("该会话正在终端窗格中运行；请进入那个窗格查看实时内容")
        raw = Path(path).read_bytes()
        try:
            entries = _parse_jsonl_entries(raw[:raw.rfind(b"\n") + 1].decode("utf-8"), strict=True)
        except (ValueError, TypeError):
            raise ValueError("会话文件尚不完整，稍后再试") from None
        if not entries or entries[0].get("type") != "session":
            raise ValueError("会话文件尚不完整，稍后再试")
        messages = []
        for entry in build_context_entries(entries[1:]):
            for message in session_entry_to_context_messages(entry):
                role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
                if role in (None, "system"):
                    continue
                messages.append(to_jsonable(message))
        return {"messages": messages, "sessionId": entries[0].get("id")}

    def project_list(self, data):
        paths = data.get("paths", [])
        if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
            raise ValueError("项目列表格式有误")
        validated = []
        for path in paths:
            try:
                validated.append(workspace_path(path))
            except (OSError, ValueError):
                if not data.get("migrate"):
                    raise
        saved = self.projects.add(validated) if validated else self.projects.read()
        return {"projects": [{"path": p, "available": Path(p).is_dir()} for p in saved]}

    def pick_project_folder(self, data):
        if not self.picker_lock.acquire(blocking=False):
            raise ValueError("文件夹选择窗口已打开，请先完成选择")
        try:
            initial = workspace_path(data.get("workspace", self.workspace))
            result = subprocess.run([sys.executable, "-X", "utf8", "-m", "misaka.ui.gui.folder_picker", initial],
                                    capture_output=True, text=True, encoding="utf-8", timeout=300,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                response = json.loads(result.stdout)
            except ValueError:
                raise ValueError("无法打开系统文件夹选择窗口，请使用输入路径") from None
            if result.returncode or response.get("error"):
                raise ValueError("无法打开系统文件夹选择窗口，请使用输入路径")
            return {"path": workspace_path(response["path"]) if response.get("path") else None}
        except subprocess.TimeoutExpired:
            raise ValueError("文件夹选择已超时，请重试") from None
        finally:
            self.picker_lock.release()

    def project_sessions(self, data):
        cwd = workspace_path(data.get("workspace", self.workspace))
        if os.path.normcase(cwd) not in {os.path.normcase(p) for p in self.projects.read()}:
            raise ValueError("请先添加这个项目")
        result = {"workspace": cwd, "saved": self.saved_chat_sessions({"workspace": cwd, "all_roles": True})["sessions"],
                  "chats": [c for c in self.chats.list() if os.path.normcase(c["workspace"]) == os.path.normcase(cwd)],
                  "sessions": [], "preferences": self.session_library.preferences(cwd)}
        try:
            result["sessions"] = self.request("cards.list", {"workspace": cwd}).get("sessions", [])
        except (ConnectionError, FileNotFoundError):
            pass
        return result

    def session_update(self, data):
        cwd = workspace_path(data.get("workspace", self.workspace))
        path = required_text(data, "path", 4096)
        key = path_key(path)
        op = required_text(data, "op", 32)
        if op not in {"pin", "rename", "delete", "restore"}:
            raise ValueError("未知会话管理操作")
        if op == "delete" and data.get("confirmed") is not True:
            raise ValueError("请先确认删除这段对话")
        value = data.get("value")
        if op == "pin" and not isinstance(value, bool):
            raise ValueError("置顶状态格式有误")
        if op == "rename":
            value = required_text(data, "value", 120).strip()
        live = [c for c in self.chats.list() if path_key(c["workspace"]) == path_key(cwd)
                and path_key(c.get("sessionFile") or c.get("sourceSession") or "") == key]
        known = self.saved_chat_sessions({"workspace": cwd, "all_roles": True, "_include_deleted": True})["sessions"]
        try:
            known += self.request("cards.list", {"workspace": cwd}).get("sessions", [])
        except (ConnectionError, FileNotFoundError):
            pass
        target = next((s for s in known if s.get("path") and path_key(s["path"]) == key), None)
        stored = self.session_library.preferences(cwd).get(key)
        if not target and live:
            target = {"path": path, "title": live[0].get("name") or "未命名会话", "role": live[0].get("role")}
        if not target and op == "restore" and stored and stored.get("deleted_at"):
            target = stored
        if not target or not path.lower().endswith(".jsonl"):
            raise ValueError("当前项目没有这段对话，请刷新列表")
        # Only GUI-owned runners are closed. Task sessions retain their original owner.
        for chat in live:
            if chat.get("status") in {"starting", "ready"}:
                if op == "delete":
                    self.chats.close(chat["id"])
                elif op == "rename":
                    self.chats.rename({"chat_id": chat["id"], "name": value})
        session = {"path": str(Path(path).resolve()), "workspace": cwd,
                   "title": target.get("title") or target.get("name") or "未命名会话", "role": target.get("role")}
        self.session_library.update(session, op, value)
        return {"preferences": self.session_library.preferences(cwd)}

    def composer_skills(self, data):
        cwd = workspace_path(data.get("workspace", self.workspace))
        role = skill_role(data.get("role"))
        if role and role not in sisters():
            raise ValueError("未知的 Sister")
        if data.get("chat_id"):
            channel = self.chats._channel(data.get("chat_id"))
            if path_key(channel.meta["workspace"]) != path_key(cwd):
                raise ValueError("对话属于其他项目")
            try:
                return self.chats.request(data, "skills")
            except ValueError as error:
                if str(error) != "未知操作 'skills'":
                    raise
                # Runners started before the skills IPC operation remain usable.
                # Read their role/project roots without restarting or prompting them.
                return skill_catalogue(cwd, channel.meta.get("role"))
        return skill_catalogue(cwd, role)

    def dispatch(self, action, data):
        if action == "open_terminal":
            return open_terminal(workspace_path(data.get("workspace", self.workspace)))
        if action == "session_update":
            return self.session_update(data)
        if action == "session_trash":
            cwd = workspace_path(data.get("workspace", self.workspace))
            return {"sessions": sorted([s for s in self.session_library.preferences(cwd).values()
                                       if s.get("deleted_at")], key=lambda s: s["deleted_at"], reverse=True)}
        if action == "composer_skills":
            return self.composer_skills(data)
        if action == "composer_files":
            cwd = workspace_path(data.get("workspace", self.workspace))
            query = data.get("query", "")
            if not isinstance(query, str) or len(query) > 1024:
                raise ValueError("文件搜索词格式有误")
            return self.composer_files.search(cwd, query)
        if action == "projects":
            return self.project_list(data)
        if action == "pick_folder":
            return self.pick_project_folder(data)
        if action == "project_sessions":
            return self.project_sessions(data)
        if action == "job":
            return self.jobs.get(required_text(data, "job_id", 64), workspace_path(data.get("workspace", self.workspace)))
        if action == "settings":
            op = required_text(data, "op", 64)
            if op not in SETTINGS_OPS:
                raise ValueError("未知设置操作")
            params = data.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("参数格式不正确")
            return self.settings.call(op, params, workspace_path(data.get("workspace", self.workspace)))
        if action == "login_start":
            return self.settings.login_start(required_text(data, "provider", 128), workspace_path(data.get("workspace", self.workspace)))
        if action == "login_status":
            return self.settings.login_status(data.get("login_id"))
        if action == "login_answer":
            value = data.get("value")
            if value is not None and (not isinstance(value, str) or len(value) > 16384):
                raise ValueError("回答过长")
            return self.settings.login_answer(data.get("login_id"), str(data.get("prompt_id", "")), value)
        if action == "login_cancel":
            return self.settings.login_cancel(data.get("login_id"))
        if action == "research_jobs":
            return {"jobs": self.research_runs.listing(workspace_path(data.get("workspace", self.workspace)))}
        if action == "research_log":
            return self.research_runs.log(workspace_path(data.get("workspace", self.workspace)),
                                          data.get("job_id"), data.get("run_id"))
        if action == "command_output":
            args = data.get("args")
            # Noninteractive commands only: output is shown when they finish, nothing waits for a key.
            allowed = {("doc", "add"), ("doc", "find"), ("doc", "tree"), ("doc", "verify"), ("doc", "scan"),
                       ("doc", "list"), ("skills", "list"), ("skills", "pending"), ("skills", "scan"),
                       ("skills", "curator-status"), ("skills", "approve"), ("skills", "reject"),
                       ("skills", "usage"), ("skills", "ledger"), ("bundles", "list"), ("auth", "check"),
                       ("web", "status"), ("web", "providers"), ("web", "accounts"), ("web", "login"),
                       ("web", "logout"), ("web", "setup"), ("web", "browser-install"), ("web", "browser-status"),
                       ("web", "gateway-status"), ("moa", "list")}
            if (not isinstance(args, list) or len(args) < 2 or len(args) > 30
                    or any(not isinstance(a, str) or len(a) > 16384 or "\0" in a for a in args)
                    or tuple(args[:2]) not in allowed
                    or (args[0] == "web" and args[1] == "setup" and args[2:] != ["ddgs", "--install", "--yes"])):
                raise ValueError("这个命令需要交互，图形界面不支持；请使用设置页中的对应功能")
            return self.jobs.start(workspace_path(data.get("workspace", self.workspace)), args, str(data.get("title", "操作结果"))[:80])
        if action in {"session_snapshot", "session_input", "session_pause", "session_resume",
                      "session_models", "session_set_model", "session_set_thinking", "session_send_now", "session_withdraw"}:
            import asyncio
            from misaka.core.session_catalog import owner_record, _object
            from misaka.core.session_control import request
            from misaka.core.session_manager import session_entry_to_context_messages
            from misaka.modes.jsonl import to_jsonable
            cwd = workspace_path(data.get("workspace", self.workspace))
            listing = self.request("cards.list", {"workspace": cwd})["sessions"]
            entry = next((s for s in listing if s["id"] == data.get("session_id")), None)
            if entry is None:
                raise ValueError("当前项目没有这条会话，请刷新列表")
            path = entry.get("path")
            owner = owner_record(path) if path else _object(entry["catalog_file"])
            if action == "session_snapshot" and (not owner.get("control") or entry.get("state") == "saved"):
                return {**self.chat_snapshot({"path": path}), "state": "saved", "readonly": True}
            if not owner.get("control"):
                raise ValueError("会话已结束，无法发送消息；记录仍可查看")
            operation = {"session_snapshot": "snapshot", "session_input": "input",
                         "session_pause": "pause", "session_resume": "resume", "session_models": "models",
                         "session_set_model": "set_model", "session_set_thinking": "set_thinking",
                         "session_send_now": "send_now", "session_withdraw": "withdraw"}[action]
            params = {key: data.get(key) for key in ("provider", "model", "level", "message_id")}
            if operation == "input":
                params = {"text": referenced_message(required_text(data, "text"), data.get("files"), cwd),
                          "display_text": data.get("text"), "streamingBehavior": data.get("streamingBehavior", "steer"),
                          "message_id": data.get("message_id"), "files": data.get("files") or []}
            try:
                result = asyncio.run(request(owner, operation, **params))
            except ValueError as error:
                if "Unknown session operation:" in str(error):
                    raise ValueError("这个会话由旧版进程运行，尚不支持模型、思考深度或消息队列操作。请等任务结束并关闭原进程后，再用新版继续保存的会话；重开网页不会更新原进程。") from error
                raise
            if operation == "snapshot":
                messages = [to_jsonable(m) for entry in result.pop("entries", []) or []
                            for m in session_entry_to_context_messages(entry)]
                messages = [m for m in messages if m.get("role") != "system"]
                if result.get("streaming"):
                    messages.append(result["streaming"])
                return {**result, "capabilities": result.get("capabilities", []),
                        "messages": messages, "readonly": False}
            if operation in {"models", "set_model", "set_thinking", "send_now", "withdraw"}:
                return result
            return {**(result if isinstance(result, dict) else {}), "message": "已发送到原会话" if operation == "input" else "已请求暂停" if operation == "pause" else "已恢复"}
        # Native chats first: they never touch the daemon, so they work before
        # "连接" and cannot be broken by it.
        native = {"chat_native": self.native_chat,
                  "chat_sessions": self.saved_chat_sessions,
                  "chat_snapshot": self.chat_snapshot}
        if action in native:
            return native[action](data)
        if action == "chat_send" or (action == "chat_send_now" and data.get("text")):
            channel = self.chats._channel(data.get("chat_id"))
            data = {**data, "display_text": data.get("text"), "text": referenced_message(required_text(data, "text", 262144), data.get("files"), channel.meta["workspace"])}
        proxied = {"chat_events": self.chats.events, "chat_send": self.chats.send,
                   "chat_stop": self.chats.stop, "chat_compact": self.chats.compact,
                   "chat_models": self.chats.models, "chat_set_model": self.chats.set_model,
                   "chat_set_thinking": self.chats.set_thinking, "chat_rename": self.chats.rename,
                  "chat_ui_response": self.chats.ui_response, "chat_send_now": self.chats.send_now,
                  "chat_withdraw": self.chats.withdraw}
        if action in proxied:
            return proxied[action](data)
        if action == "chat_close":
            return self.chats.close(data.get("chat_id"))
        if action == "chat_replay":
            return self.chats.request(data, "snapshot")
        if action == "state":
            return self.state(data)
        if action == "connect":
            self.ensure()
            return self.state(data)
        if action == "folders":
            folder = Path(workspace_path(data.get("path") or data.get("workspace", self.workspace)))
            entries = []
            for path in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
                try:
                    if path.is_dir() and not path.name.startswith("."):
                        entries.append({"name": path.name, "path": str(path.resolve())})
                except OSError:
                    continue
            return {"path": str(folder), "parent": str(folder.parent), "entries": entries[:500]}
        if action == "chat":
            args = ["chat"]
            if data.get("role"):
                args += ["--as", required_text(data, "role", 128)]
            if data.get("continue"):
                args += ["--continue"]
            return self.launch(data, args, "Last Order" if not data.get("role") else f"Sister {data['role']}")
        if action == "research":
            return self._research(data, research_args(data), "研究 · " + data["goal"][:36])
        if action in {"research_list", "research_stop", "research_resume"}:
            from contextlib import closing
            from misaka.core.platform import tasks
            from misaka.core.research import runs
            if not Path(CFG["db"]).exists():
                if action == "research_list":
                    return {"runs": []}
                raise ValueError("研究记录不存在")
            cwd = tasks.canonical_workspace(workspace_path(data.get("workspace", self.workspace)))
            if action == "research_list":
                with closing(sqlite3.connect(Path(CFG["db"]).resolve().as_uri() + "?mode=ro", uri=True)) as con:
                    con.row_factory = sqlite3.Row
                    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='research_runs'").fetchone():
                        return {"runs": []}
                    items = con.execute("SELECT * FROM research_runs WHERE workspace=? ORDER BY created_at DESC", (cwd,)).fetchall()
                    return {"runs": [{**dict(r), "nodes": [dict(n) for n in runs.nodes(con, r["id"])],
                                      "edges": [dict(e) for e in runs.edges(con, r["id"])]} for r in items]}
            with closing(tasks.connect(CFG["db"])) as con:
                items = runs.listing(con, workspace=cwd)
                run_id = required_text(data, "run_id", 128)
                if not any(r["id"] == run_id for r in items):
                    raise ValueError("当前项目没有这项研究")
                if action == "research_stop":
                    run = next(r for r in items if r["id"] == run_id)
                    if run["status"] not in {"active", "waiting_input", "stopping"}:
                        raise ValueError("这项研究已经结束，无需停止")
                    runs.request_stop(con, run_id)
                    return {"message": "已请求停止研究；原生工作流将收尾并生成阶段性报告"}
            return self._research(data, ["research", "--resume", run_id], "恢复研究")
        if action == "setup":
            section = data.get("section", "")
            if section and section not in SETUP_SECTIONS:
                raise ValueError("未知设置项")
            return self.launch(data, ["setup", *([section] if section else [])], "设置向导")
        if action == "command":
            args = data.get("args")
            if (not isinstance(args, list) or not args or args[0] not in COMMANDS
                    or len(args) > 80 or any(not isinstance(a, str) or "\0" in a or len(a) > 16384 for a in args)):
                raise ValueError("不支持的命令或参数；请使用页面列出的 MISAKA 命令")
            return self.launch(data, args, data.get("title", "MISAKA 命令")[:80])
        if action == "session":
            self.ensure()
            cwd = workspace_path(data.get("workspace", self.workspace))
            sessions = self.request("cards.list", {"workspace": cwd})["sessions"]
            entry = next((s for s in sessions if s["id"] == data.get("session_id")), None)
            if entry is None:
                raise ValueError("会话已不存在，请刷新列表")
            # Attach to the original owner when it is live; never start a second writer.
            for pane in self.request("panes.list")["panes"]:
                argv = pane.get("argv", [])
                source_path = next((argv[argv.index(flag) + 1] for flag in ("--session", "--catalog") if flag in argv[:-1]), None)
                path = entry.get("path") or entry.get("catalog_file")
                if pane["alive"] and path and path in {(pane.get("reported") or {}).get("session"), source_path}:
                    return {"pane_id": pane["id"]}
            source = ["--session", entry["path"]] if entry.get("path") else ["--catalog", entry["catalog_file"]]
            from misaka.config import sisters
            role = entry.get("role")
            resumable = (entry.get("kind") == "foreground" and entry.get("state") == "saved"
                         and (role == "last-order" or role in sisters()))
            if not data.get("read_only", True) and resumable:
                args = ["chat", *source, *(["--as", role] if role != "last-order" else [])]
            else:
                # Task/node/DM runtimes keep their own lifecycle even after exiting.
                mode = "--attach" if not data.get("read_only", True) and entry.get("control") else "--read-only"
                args = ["chat", *source, mode]
            return self.launch({"workspace": entry.get("cwd") or cwd}, args, "历史会话")
        if action in {"screen", "input", "key", "send", "resize", "scroll", "close"}:
            pane_id = required_text(data, "id", 64)
            if action == "screen":
                from misaka.ui.panel.screen import cells_from_ansi
                screen = self.request("pane.screen", {"id": pane_id})
                # Coalesce styled cells into runs; text is always inserted with textContent.
                lines = []
                for line in screen["rows"]:
                    runs = []
                    for char, style in cells_from_ansi(line):
                        if runs and runs[-1][1] == style:
                            runs[-1][0] += char
                        else:
                            runs.append([char, style])
                    lines.append(runs)
                return {**screen, "rows": lines}
            if action in {"key", "input", "send"}:
                if action == "key":
                    if data.get("key") not in KEYS:
                        raise ValueError("未知按键")
                    from misaka.ui.panel.host_input import CTRL, Key
                    from misaka.ui.panel.pane_input import encode_key
                    mode = self.request("pane.screen", {"id": pane_id}).get("input", {})
                    name = data["key"]
                    key = Key("c", CTRL) if name == "interrupt" else Key("esc" if name == "escape" else name)
                    value = encode_key(key, mode).decode("utf-8")
                else:
                    value = required_text(data, "text", 65536) if action == "send" else data.get("text", "")
                    if not isinstance(value, str) or len(value) > 65536:
                        raise ValueError("输入过长")
                    if action == "send":
                        # Native setup prompts do not always enable bracketed paste.
                        # Respect the program's mode instead of typing escape codes into a wizard.
                        mode = self.request("pane.screen", {"id": pane_id}).get("input", {})
                        value = value.replace("\x1b", "")
                        if mode.get("bracketed_paste"):
                            value = "\x1b[200~" + value + "\x1b[201~"
                        else:
                            value = value.replace("\r\n", "\n").replace("\n", "\r")
                result = self.request("pane.input", {"id": pane_id, "data": base64.b64encode(value.encode()).decode()})
                if action == "send" and data.get("enter", True):
                    self.request("pane.input", {"id": pane_id, "data": "DQ=="})
                return result
            if action == "resize":
                rows, cols = int(data.get("rows", 30)), int(data.get("cols", 100))
                return self.request("pane.resize", {"id": pane_id, "rows": max(8, min(rows, 100)), "cols": max(40, min(cols, 240))})
            if action == "scroll":
                return self.request("pane.scroll", {"id": pane_id, "delta": max(-200, min(int(data.get("delta", 0)), 200)), "to": "bottom" if data.get("bottom") else None})
            return self.request("pane.close", {"id": pane_id})
        if action == "card":
            self.ensure()
            cwd = workspace_path(data.get("workspace", self.workspace))
            if not any(c["id"] == data.get("task_id") for c in self.request("cards.list", {"workspace": cwd}).get("cards", [])):
                raise ValueError("当前项目没有这张任务卡")
            methods = {"run": "pane.run_card", "open": "pane.open_card_session", "stop": "card.stop", "continue": "pane.continue_card"}
            if data.get("operation") not in methods:
                raise ValueError("未知任务操作")
            params = {"task_id": required_text(data, "task_id", 128)}
            if data["operation"] == "continue":
                params["say"] = required_text(data, "text")
            return self.request(methods[data["operation"]], params)
        if action == "roster":
            from misaka.core.network import roster
            from misaka.config import profiles
            members = []
            for sid in roster.roster_names():
                directory = Path(roster.ROOT) / sid
                path = directory / "DESCRIBE.md"
                desc = path.read_text(encoding="utf-8-sig") if path.exists() else "尚未填写专长"
                members.append({"id": sid, "description": desc, "model": profiles.pinned_model(str(directory))})
            return {"members": members}
        if action == "documents":
            from misaka.core.documents import index
            cwd = workspace_path(data.get("workspace", self.workspace))
            return {"documents": index.docs(workspace=cwd)}
        if action == "files":
            cwd = Path(workspace_path(data.get("workspace", self.workspace)))
            relative = str(data.get("path", "."))
            target = (cwd / relative).resolve()
            if not target.is_relative_to(cwd):
                raise ValueError("只能浏览当前项目内的文件")
            if target.is_dir():
                entries = []
                for p in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.casefold())):
                    if p.name.startswith(".") or not p.resolve().is_relative_to(cwd):
                        continue
                    entries.append({"name": p.name, "path": p.relative_to(cwd).as_posix(), "directory": p.is_dir()})
                    if len(entries) >= 500:
                        break
                return {"entries": entries, "path": target.relative_to(cwd).as_posix()}
            image_types = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
            if target.suffix.lower() not in image_types and target.suffix.lower() not in {".md", ".txt", ".csv", ".json", ".log", ".py", ".yaml", ".yml", ".toml",
                                            ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".xml", ".svg", ".rst", ".sql", ".sh", ".ps1"}:
                raise ValueError("此格式暂不支持预览，请用本机应用打开")
            if target.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("文件超过 2 MB，请用本机应用打开")
            raw = target.read_bytes()
            if target.suffix.lower() in image_types:
                return {"image": "data:" + image_types[target.suffix.lower()] + ";base64," + base64.b64encode(raw).decode("ascii"),
                        "path": target.relative_to(cwd).as_posix()}
            try:
                content = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                raise ValueError("文件不是 UTF-8，已停止预览以避免乱码；原文件未修改") from None
            return {"content": content, "path": target.relative_to(cwd).as_posix()}
        raise ValueError("未知操作")


class GUIServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def __init__(self, address, bridge):
        super().__init__(address, Handler)
        self.bridge = bridge
        self.token = secrets.token_urlsafe(32)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        # Keep the frontend paired with this running backend across source updates.
        self.assets = {name: (STATIC / name).read_bytes() for name in ("index.html", "app.js", "style.css")}
        self.source_revision = source_revision()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Neither credentials, messages nor capability URLs belong in access logs.

    def reply(self, status, payload, content_type="application/json; charset=utf-8", cache="no-store"):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def same_origin(self):
        if self.headers.get("Host") != urlsplit(self.server.origin).netloc:
            self.reply(403, {"error": "拒绝非本机访问"})
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin != self.server.origin:
            self.reply(403, {"error": "拒绝跨站请求"})
            return False
        return True

    def do_GET(self):
        if not self.same_origin():
            return
        files = {"/": ("index.html", "text/html; charset=utf-8"),
                 "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                 "/style.css": ("style.css", "text/css; charset=utf-8"),
                 "/Misaka.ico": ("Misaka.ico", "image/x-icon"),
                 "/favicon.ico": ("Misaka.ico", "image/x-icon")}
        path = urlsplit(self.path).path
        # Fonts hold no secrets and are large; exact-name lookup keeps traversal out.
        if path.removeprefix("/fonts/") in FONTS:
            return self.reply(200, (FONT_DIR / path.removeprefix("/fonts/")).read_bytes(), "font/woff2",
                              cache="public, max-age=604800")
        match = files.get(path)
        if not match:
            return self.reply(404, {"error": "页面不存在"})
        raw = self.server.assets.get(match[0])
        self.reply(200, raw if raw is not None else (STATIC / match[0]).read_bytes(), match[1])

    def do_POST(self):
        if not self.same_origin():
            return
        if not hmac.compare_digest(self.headers.get("X-Misaka-Token", ""), self.server.token):
            return self.reply(403, {"error": "访问凭证已失效，请从启动器重新打开页面"})
        try:
            if self.headers.get_content_type() != "application/json":
                raise ValueError("仅接受 JSON 请求")
            length = int(self.headers.get("Content-Length", "0"))
            limit = 36 * 1024 * 1024 if self.path == "/api/chat_send" else 262144
            if not 0 < length <= limit:
                raise ValueError("请求为空或过大")
            self.connection.settimeout(10)
            data = json.loads(self.rfile.read(length))
            # Long-poll handlers (chat_events) hold this socket well past 10s
            # before writing; only the body read above needed the timeout.
            self.connection.settimeout(None)
            if not isinstance(data, dict):
                raise ValueError("请求必须为对象")
            if not self.path.startswith("/api/"):
                return self.reply(404, {"error": "接口不存在"})
            if self.path == "/api/health":
                # native_chat lets the launcher detect and replace a pre-refactor
                # server process that would otherwise pass the version check.
                self.reply(200, {"version": VERSION, "pid": os.getpid(), "native_chat": True, "project_gui": 12,
                                 "source_revision": self.server.source_revision})
                return
            if self.path == "/api/shutdown":
                self.reply(200, {"message": "网页服务已关闭；网页对话已停止并保存，独立研究进程继续运行"})
                self.server.bridge.chats.close_all()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            result = self.server.bridge.dispatch(self.path[5:], data)
            self.reply(200, result)
        except (ValueError, TypeError, KeyError) as error:
            self.reply(400, {"error": str(error)})
        except Exception as error:
            # Keep failures visible; never turn a failed launch into a success notification.
            self.reply(500, {"error": str(error) or type(error).__name__})


def serve(*, port=0, open_browser=True, workspace=None):
    server = GUIServer(("127.0.0.1", port), Bridge(workspace or os.getcwd()))
    url = server.origin + "/#token=" + server.token
    print(f"MISAKA 中文界面已启动：{url}", flush=True)
    print("关闭页面不会停止会话。按 Ctrl+C 关闭网页服务。", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.bridge.chats.close_all()
        server.server_close()
    return 0


def main():
    parser = argparse.ArgumentParser(description="MISAKA 中文图形界面")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--workspace", default=os.getcwd())
    args = parser.parse_args()
    return serve(port=args.port, open_browser=not args.no_browser, workspace=args.workspace)


if __name__ == "__main__":
    raise SystemExit(main())

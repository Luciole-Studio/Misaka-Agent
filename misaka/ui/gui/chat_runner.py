"""Headless chat host for the GUI's native conversation view.

One process per GUI chat. It assembles exactly what ``misaka chat`` assembles --
role profile, bundled extensions, skills, tools, session persistence -- but owns no
terminal: the GUI server speaks line-delimited JSON over stdio (requests in,
session events out). Sessions are the same JSONL files the terminal writes, so a
conversation moves between the GUI and ``misaka chat`` freely.

The process boundary is deliberate: role environment (MISAKA_PROFILE_DIR and
friends) is read at runtime by MCP and friends, so concurrent Last Order and
Sister chats must not share one interpreter; and a wedged session must not take
the GUI server with it.

Wire form, both directions one JSON object per line:

  out  {"type": "ready"|"history"|"event"|"result"|"diagnostics"
        |"prompt_error"|"extension_error"|"fatal"|"bye", ...}
  in   {"id": 1, "op": "prompt", "params": {...}}          -- answered by "result"
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading

from misaka.modes.jsonl import to_json_event, to_jsonable
from misaka.core.gui_input_queue import GuiInputQueue

_WRITE_LOCK = threading.Lock()


def emit(payload: dict) -> None:
    """One JSON line on stdout. Unflushed bytes would be a lost event."""
    line = json.dumps(payload, ensure_ascii=False, default=str)
    with _WRITE_LOCK:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def fatal(message: str) -> None:
    emit({"type": "fatal", "message": str(message)})
    raise SystemExit(1)


def _image_content(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("图片附件格式不正确")
    data = value.get("data")
    mime = value.get("mime", "image/png")
    if not isinstance(data, str) or not data or len(data) > 12 * 1024 * 1024:
        raise ValueError("图片数据为空或超过 12 MB")
    if not isinstance(mime, str) or not mime.startswith("image/"):
        raise ValueError("仅支持图片附件")
    return {"type": "image", "data": data, "mime": mime}


def _model_summary(model) -> dict | None:
    if model is None:
        return None
    return {"provider": model.provider, "id": model.id, "name": model.name,
            "reasoning": bool(model.reasoning), "contextWindow": model.contextWindow}


class GuiUIContext:
    """Extension dialogs drawn by the browser instead of a terminal.

    A blocking prompt becomes a ``ui_request`` event and waits on a future that the
    ``ui_response`` op resolves; ``ui_resolved`` follows either way, so a replayed event
    ring knows the dialog is closed. Everything a terminal draws around the editor
    (widgets, footers, working indicators) has no GUI meaning and is a no-op.
    """

    def __init__(self):
        from misaka.core.extensions.runner import _NoUIContext
        self._fallback = _NoUIContext()
        self.theme = self._fallback.theme
        self._pending: dict[str, asyncio.Future] = {}
        self._counter = 0

    def __getattr__(self, name):
        return getattr(self._fallback, name)

    async def _ask(self, kind: str, **payload):
        self._counter += 1
        key = f"ui{self._counter}"
        future = asyncio.get_running_loop().create_future()
        self._pending[key] = future
        emit({"type": "ui_request", "id": key, "kind": kind, **payload})
        try:
            return await future
        finally:
            self._pending.pop(key, None)
            emit({"type": "ui_resolved", "id": key})

    def resolve(self, key: str, value) -> bool:
        future = self._pending.get(key)
        if future is None or future.done():
            return False
        future.set_result(value)
        return True

    def cancel_all(self) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_result(None)

    async def select(self, title, options, opts=None):
        value = await self._ask("select", title=str(title), options=[str(o) for o in options or []])
        return value if isinstance(value, str) else None

    async def confirm(self, title, message, opts=None):
        return bool(await self._ask("confirm", title=str(title), message=str(message or "")))

    async def input(self, title, placeholder=None, opts=None):
        value = await self._ask("input", title=str(title), placeholder=str(placeholder or ""))
        return value if isinstance(value, str) else None

    async def editor(self, title, prefill=None):
        value = await self._ask("editor", title=str(title), prefill=str(prefill or ""))
        return value if isinstance(value, str) else None

    async def ask_questions(self, questions: list) -> dict | None:
        """AskUserQuestion without its terminal component; returns the component's result shape."""
        value = await self._ask("questions", questions=questions)
        return value if isinstance(value, dict) else None

    async def custom(self, *_args, **_kwargs):
        # A custom component is terminal drawing code; there is nothing to show here.
        return None

    def notify(self, message, type=None):
        emit({"type": "ui_notify", "message": str(message), "level": type or "info"})


class ChatHost:
    """One conversation. Events flow out as they happen; ops arrive on stdin."""

    def __init__(self, spec: dict):
        self.spec = spec
        self.runtime = None
        self.session = None
        self.registry = None
        self.cwd = ""
        self.shutting_down = False
        self._unsubscribe = None
        self.ui = GuiUIContext()
        self.input_queue = None

    # ---- assembly (the engine.main skeleton, minus every terminal concern) ----

    def _prepare_environment(self):
        from misaka.cli import chat as chat_cli

        who = self.spec.get("role") or None
        if self.spec.get("session_path"):
            from misaka.core.session_manager import read_session_header
            session_path = os.path.abspath(self.spec["session_path"])
            header = read_session_header(session_path)
            folder = header.get("cwd")
            if not folder or not os.path.isdir(folder):
                fatal(f"无法恢复会话：它的工作目录 {folder or '(未知)'} 已不存在。")
            os.chdir(folder)
        else:
            os.chdir(self.spec["workspace"])
        prof, _model_default = chat_cli.assembly(who)
        return who, prof

    async def start(self):
        from misaka.cli.bootstrap import install
        from misaka.cli.args import parse_args
        from misaka.config import get_agent_dir, sessions
        from misaka.core.http_dispatcher import applyHttpProxySettings
        from misaka.core.settings_manager import SettingsManager
        from misaka.core.wiring import role_session_setup
        from misaka.core.agent_session_runtime import create_agent_session_runtime
        from misaka.cli.engine import create_runtime_factory
        from misaka.core.auth_storage import AuthStorage
        from misaka.core.session_manager import SessionManager

        install()
        who, prof = self._prepare_environment()
        cwd = os.getcwd()
        flags, session_assembly, env = role_session_setup(prof, cwd, receive_messages=True)
        os.environ.update(env)
        os.environ.setdefault("MISAKA_CODING_AGENT", "true")

        bucket = sessions.chat_dir(who, cwd)
        flags += ["--session-dir", bucket]
        if self.spec.get("session_path"):
            flags += ["--session", os.path.abspath(self.spec["session_path"])]
        elif self.spec.get("continue"):
            flags += ["-c"]

        parsed = parse_args(flags)
        if parsed.diagnostics:
            emit({"type": "diagnostics",
                  "items": [{"type": d.type, "message": d.message} for d in parsed.diagnostics]})
        if any(d.type == "error" for d in parsed.diagnostics):
            fatal("会话参数无效，无法启动。")
        if parsed.session:
            parsed.session = os.path.abspath(parsed.session)

        agent_dir = get_agent_dir()
        startup_settings = SettingsManager.create(cwd, agent_dir, {"projectTrusted": False})
        applyHttpProxySettings(startup_settings.getGlobalSettings().get("httpProxy"))
        session_dir = startup_settings.getSessionDir()
        if parsed.session:
            session_manager = SessionManager.open(parsed.session, session_dir)
        elif parsed.continue_:
            session_manager = SessionManager.continueRecent(cwd, session_dir)
        else:
            session_manager = SessionManager.create(cwd, session_dir)
        self.cwd = session_manager.getCwd()

        auth_storage = AuthStorage.create()
        runtime_factory = create_runtime_factory(
            parsed, auth_storage,
            extension_factories=session_assembly.extension_factories,
            custom_tools=session_assembly.custom_tools,
            parts=session_assembly.parts,
            model_profile=session_assembly.model_profile,
            model_defaults_read_only=session_assembly.model_defaults_read_only,
            app_mode="print",   # headless trust resolution, like print mode
            startup_settings_manager=startup_settings,
        )
        self.runtime = await create_agent_session_runtime(
            runtime_factory,
            {"cwd": self.cwd, "agentDir": agent_dir, "sessionManager": session_manager},
        )
        self.session = self.runtime.session
        self._create_input_queue()
        self.registry = self.runtime.services.modelRegistry
        errors = [d for d in self.runtime.diagnostics if d.type == "error"]
        if errors:
            fatal(errors[0].message)
        await self._bind()
        return self

    # ---- session binding and event flow ----

    async def _bind(self):
        def on_event(event) -> None:
            try:
                emit({"type": "event", "event": to_json_event(event)})
            except Exception:  # noqa: BLE001 - one bad event must not kill the stream
                pass

        if self._unsubscribe is not None:
            self._unsubscribe()
        self._unsubscribe = self.session.subscribe(on_event)
        await self.session.bindExtensions({
            "mode": "gui",
            "uiContext": self.ui,
            "commandContextActions": {
                "waitForIdle": lambda: self.session.waitForIdle(),
                "newSession": lambda new_session_options=None: self.runtime.newSession(new_session_options),
                "fork": lambda entry_id, fork_options=None: self.runtime.fork(entry_id, fork_options),
                "navigateTree": lambda target_id, navigate_options=None: self.session.navigateTree(
                    target_id, navigate_options),
                "switchSession": lambda path, switch_options=None: self.runtime.switchSession(path, switch_options),
                "reload": lambda: self.session.reload(),
            },
            "onError": lambda error: emit({
                "type": "extension_error",
                "message": str(error.get("error", error) if isinstance(error, dict) else error),
            }),
        })
        self.runtime.setRebindSession(self._rebind)
        emit({"type": "ready", **self._session_status()})
        emit({"type": "history", "messages": self._history()})

    async def _rebind(self, _session=None) -> None:
        """After /new, /fork or /tree the runtime hands us a fresh session object."""
        if self.input_queue is not None:
            self.input_queue.clear()
        self.session = self.runtime.session
        self._create_input_queue()
        await self._bind()

    def _create_input_queue(self):
        self.input_queue = GuiInputQueue(
            self.session, self._deliver_prompt,
            changed=lambda: emit({"type": "prompt_queue", "pendingPrompts": self.input_queue.snapshot(),
                                  "queuePaused": self.input_queue.paused}),
            failed=lambda error: emit({"type": "prompt_error", "message": str(error)}),
            settled=lambda: emit({"type": "run_settled"}),
        )

    async def _deliver_prompt(self, params):
        await self.session.prompt(params["text"], {
            "images": params.get("images") or None,
            "streamingBehavior": params.get("streamingBehavior") or "followUp",
        })

    def _session_status(self) -> dict:
        model = self.session.model
        return {
            "sessionId": self.session.sessionId,
            "sessionFile": self.session.sessionFile,
            "cwd": self.cwd,
            "name": self.session.sessionName,
            "model": _model_summary(model),
            "thinkingLevel": self.session.thinkingLevel,
            "availableThinkingLevels": self.session.getAvailableThinkingLevels(),
            "modelFallbackMessage": getattr(self.runtime, "modelFallbackMessage", None),
            "streaming": self.session.isStreaming,
            "pendingPrompts": self.input_queue.snapshot() if self.input_queue else [],
            "queuePaused": self.input_queue.paused if self.input_queue else False,
            "capabilities": ["models", "set_model", "set_thinking", "queue", "send_now", "withdraw"],
        }

    def _history(self) -> list:
        messages = []
        for message in self.session.messages:
            role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
            if role in (None, "system"):
                continue
            messages.append(to_jsonable(message))
        return messages

    # ---- ops ----

    def op_prompt(self, params: dict) -> dict:
        """Validate, ack at once, and run in the background: the ack must not wait
        for a whole agent run, and the next op (stop!) must stay reachable."""
        text = params.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 262144:
            raise ValueError("消息不能为空，且不能超过 262144 个字符")
        images = [_image_content(item) for item in params.get("images") or []]
        if not self.session.sessionName and not text.startswith("/"):
            self.session.setSessionName(text.strip().splitlines()[0][:60])
        result = self.input_queue.submit({**params, "text": text, "images": images})
        if self.input_queue.paused:
            self.input_queue.resume(result["message_id"])
        return result

    async def op_send_now(self, params):
        if params.get("text"):
            params = {**params, "message_id": self.op_prompt(params)["message_id"]}
        return await self.input_queue.send_now(params.get("message_id"))

    def op_withdraw(self, params):
        return self.input_queue.withdraw(params.get("message_id"))

    def op_ui_response(self, params: dict) -> dict:
        if not self.ui.resolve(str(params.get("id", "")), params.get("value")):
            raise ValueError("这个对话框已经关闭")
        return {"resolved": True}

    async def op_stop(self, _params: dict) -> dict:
        self.input_queue.pause()
        self.ui.cancel_all()
        await self.session.abort()
        return {"stopped": True}

    async def op_compact(self, params: dict) -> dict:
        instructions = params.get("instructions")
        await self.session.compact(instructions if isinstance(instructions, str) else None)
        return {"compacted": True}

    async def op_models(self, _params: dict) -> dict:
        await self.registry.refresh({"allowNetwork": False})
        models = []
        from misaka.ui.gui.model_preferences import visible_models
        for model in visible_models(self.registry.getAvailable()):
            models.append({**_model_summary(model), "providerName": self.registry.getProviderDisplayName(model.provider), "configured": self.registry.hasConfiguredAuth(model)})
        return {"models": models, "shortlisted": True, "current": _model_summary(self.session.model)}

    def op_skills(self, _params: dict) -> dict:
        from misaka.core.skills.wiring.skills import SkillsPart, _runtime_name, _slash_entries
        return {"skills": [{"name": _runtime_name(e), "description": e.get("list_description") or e.get("description", "")}
                           for part in self.session.moments.parts if isinstance(part, SkillsPart)
                           for e in _slash_entries(part._entries())]}

    async def op_set_model(self, params: dict) -> dict:
        await self.registry.refresh({"allowNetwork": False})
        model = self.registry.find(str(params.get("provider", "")), str(params.get("id", "")))
        if model is None:
            raise ValueError("没有这个模型")
        await self.session.setModel(model, persist=bool(params.get("persist")))
        emit({"type": "session_settings", **self._session_status()})
        return self._session_status()

    async def op_set_thinking(self, params: dict) -> dict:
        level = params.get("level")
        if level not in self.session.getAvailableThinkingLevels():
            raise ValueError("当前模型不支持这个思考等级")
        self.session.setThinkingLevel(level, persist=bool(params.get("persist")))
        emit({"type": "session_settings", **self._session_status()})
        return self._session_status()

    async def op_rename(self, params: dict) -> dict:
        name = str(params.get("name", "")).strip()
        if not name or len(name) > 120:
            raise ValueError("会话名称不能为空，且不能超过 120 个字符")
        self.session.setSessionName(name)
        return {"name": name}

    def op_status(self, _params: dict) -> dict:
        return self._session_status()

    def op_snapshot(self, _params: dict) -> dict:
        return {"messages": self._history(), "meta": self._session_status()}

    ASYNC_OPS = {"stop": op_stop, "compact": op_compact, "send_now": op_send_now,
                 "models": op_models, "set_model": op_set_model, "set_thinking": op_set_thinking, "rename": op_rename}
    SYNC_OPS = {"prompt": op_prompt, "skills": op_skills, "status": op_status, "snapshot": op_snapshot,
                "ui_response": op_ui_response, "withdraw": op_withdraw}

    async def _execute(self, request: dict, handler, params: dict) -> None:
        try:
            value = handler(self, params)
            if asyncio.iscoroutine(value):
                value = await value
            emit({"type": "result", "id": request.get("id"), "ok": True, "data": value})
        except Exception as error:  # noqa: BLE001 - every op failure reaches the GUI as text
            emit({"type": "result", "id": request.get("id"), "ok": False,
                  "error": str(error) or type(error).__name__})

    def dispatch(self, request: dict) -> bool:
        """Handle one request; returns False when the serve loop should end."""
        op = request.get("op")
        if op == "shutdown":
            self.shutting_down = True
            emit({"type": "result", "id": request.get("id"), "ok": True, "data": {"bye": True}})
            return False
        entry = self.ASYNC_OPS.get(op) or self.SYNC_OPS.get(op)
        if entry is None:
            emit({"type": "result", "id": request.get("id"), "ok": False, "error": f"未知操作 {op!r}"})
            return True
        params = request.get("params") or {}
        if not isinstance(params, dict):
            emit({"type": "result", "id": request.get("id"), "ok": False, "error": "params 必须是对象"})
            return True
        asyncio.get_running_loop().create_task(self._execute(request, entry, params))
        return True

    async def serve(self):
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def read_stdin() -> None:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    request = json.loads(line)
                except ValueError:
                    continue
                loop.call_soon_threadsafe(queue.put_nowait, request)
            loop.call_soon_threadsafe(queue.put_nowait, {"op": "shutdown"})

        threading.Thread(target=read_stdin, daemon=True, name="chat-ops").start()
        try:
            while not self.shutting_down:
                keep_going = self.dispatch(await queue.get())
                if not keep_going:
                    break
        finally:
            await self._dispose()

    async def _dispose(self):
        if self.input_queue is not None:
            self.input_queue.clear()
        try:
            if self.runtime is not None:
                await self.runtime.dispose()
        except Exception:  # noqa: BLE001 - shutdown must never mask the exit itself
            pass
        emit({"type": "bye"})


async def _amain(spec: dict) -> None:
    host = await ChatHost(spec).start()
    await host.serve()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        spec = json.loads(argv[0]) if argv else {}
    except ValueError:
        fatal("启动参数不是合法 JSON")
    if not isinstance(spec, dict) or not isinstance(spec.get("workspace"), str):
        fatal("缺少 workspace")
    for key in ("role", "session_path"):
        if key in spec and (not isinstance(spec[key], str) or len(spec[key]) > 1024 or "\0" in spec[key]):
            fatal(f"{key} 无效")
    try:
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", newline="\n")
        asyncio.run(_amain(spec))
    except SystemExit:
        raise
    except KeyboardInterrupt:
        pass
    except Exception as error:  # noqa: BLE001 - the GUI needs the failure as text, not a traceback
        fatal(error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

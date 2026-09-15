"""Minimum host glue for the ported synchronous vault handlers.

Disk/crypto work runs in a worker; CDP, subprocesses and masked UI stay on the
WebRuntime's loop. Cancellation closes and awaits those operations before the
worker is joined. Nothing is installed into global Hermes module namespaces.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import os
import shutil
import subprocess
import threading
from contextvars import ContextVar
from pathlib import Path

from misaka.config import get_agent_dir
from misaka.core.web.config import provider_env, redact_secrets
from misaka.core.web.scope import current_scope
from misaka.utils.ansi import strip_ansi
from misaka.utils.atomic import write_bytes
from misaka.utils.values import read_field

_bridge: ContextVar[Bridge | None] = ContextVar("web_vault_bridge", default=None)
_OP_ENV_ALLOWLIST = (
    "PATH", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "SystemRoot",
    "TMPDIR", "TMP", "TEMP", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR",
    "OP_ACCOUNT", "OP_CONNECT_HOST", "OP_CONNECT_TOKEN", "OP_LOAD_DESKTOP_APP_SETTINGS",
)


def get_hermes_home():
    """Upstream spelling, native profile resolution; never reads HERMES_HOME."""
    return Path(current_scope().profile_dir or get_agent_dir())


def atomic_write_bytes(path, data, *, mode=0o600, fsync_dir=True):
    return write_bytes(path, data, mode=mode)


def get_secret(name, default=""):
    value = provider_env(name) or default
    if name not in {"OP_CONNECT_HOST", "OP_ACCOUNT"}:
        register_vault_redaction_value(value)
    return value


def find_op(binary_path=""):
    found = binary_path or shutil.which("op")
    return Path(found) if found and os.access(found, os.X_OK) else None


def scrub_ansi(text):
    return strip_ansi(text or "").replace("\x1b", "")


def _scrub(text):
    return redact_secrets(scrub_ansi(text)).strip()


def register_vault_redaction_value(value):
    if isinstance(value, str) and value:
        scope = current_scope()
        with scope.lock:
            scope.vault_secrets.add(value)


def current_bridge():
    return _bridge.get()


class Bridge:
    def __init__(self, runtime, ctx, session):
        self.runtime, self.ctx, self.session = runtime, ctx, session
        self.loop = asyncio.get_running_loop()
        self.pending = set()
        self.tasks = set()
        self.lock = threading.Lock()
        self.cancelled = False
        self.interactive = bool(read_field(ctx, "hasUI", False) and
                                callable(getattr(read_field(ctx, "ui"), "custom", None)))

    def call(self, coroutine):
        # Called only by the worker, never by the event loop it is waiting on.
        async def tracked():
            task = asyncio.current_task()
            self.tasks.add(task)
            try:
                return await coroutine
            finally:
                self.tasks.discard(task)
        with self.lock:
            if self.cancelled:
                coroutine.close()
                raise asyncio.CancelledError
            future = asyncio.run_coroutine_threadsafe(tracked(), self.loop)
            self.pending.add(future)
        try:
            return future.result()
        except concurrent.futures.CancelledError:
            raise asyncio.CancelledError from None
        finally:
            with self.lock:
                self.pending.discard(future)
            if inspect.getcoroutinestate(coroutine) == inspect.CORO_CREATED:
                coroutine.close()

    def stop(self):
        with self.lock:
            self.cancelled = True
            pending = tuple(self.pending)
        for future in pending:
            future.cancel()

    async def drain(self):
        # concurrent Future cancellation acknowledges before the loop task's finally.
        await asyncio.sleep(0)
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)

    async def browser(self, name, args):
        if self.runtime.browser is None:
            from misaka.core.web.browser import BrowserManager
            self.runtime.browser = BrowserManager(read_field(self.ctx, "cwd", str(get_hermes_home())))
        return await self.runtime.browser.perform(name, {**args, "session": self.session})

    async def prompt(self, title):
        if not self.interactive:
            return ""
        from misaka.core.web.browser.vault.prompt import secret_input
        value = await secret_input(self.ctx.ui, title)
        register_vault_redaction_value(value)
        return value or ""

    async def login(self, origin, site):
        if not self.interactive:
            return None
        identifier = await self.ctx.ui.input(f"Save login for {origin}: username/email")
        if not identifier:
            return None
        password = await self.prompt(f"Password for {origin} (masked; stored locally)")
        return {"identifier": identifier, "password": password} if password else None


def evaluate(expression):
    bridge = current_bridge()
    if bridge is None:
        return {"success": False, "error_type": "supervisor_required", "error": "An owned CDP session is required."}
    response = bridge.call(bridge.browser("browser_cdp", {"method": "Runtime.evaluate",
        "params": {"expression": expression, "returnByValue": True, "awaitPromise": True}}))
    payload = response.get("result", {})
    if response.get("success") is False or response.get("action_pending") or payload.get("exceptionDetails"):
        return {"success": False, "error_type": "eval_failed", "error": "Vault page evaluation did not complete."}
    return {"success": True, "result": payload.get("result", {}).get("value")}


def focus_page(origin, accept):
    bridge = current_bridge()
    if bridge is None:
        return None
    try:
        result = bridge.call(bridge.browser("_vault_focus", {"origin": origin, "accept": accept}))
        return (origin or result.get("url")) if result.get("ok") else None
    except (ValueError, RuntimeError):
        return None  # Controllers may support CDP without multi-tab focus.


def confirm_payment(label, origin):
    bridge = current_bridge()
    if bridge is None or not bridge.interactive:
        return False
    return bool(bridge.call(bridge.ctx.ui.confirm(f"Fill payment card on {origin}",
        f"Enter saved card {label!r} on this checkout page? Card values stay out of the conversation.")))


def run_cli(argv, *, env, timeout, label, timeout_message, stdin=subprocess.DEVNULL, input_bytes=None):
    """Reuse native owned-process teardown for manager CLI calls, including unlock."""
    bridge = current_bridge()
    try:
        if bridge is None:
            proc = subprocess.run(list(argv), env=env, timeout=timeout, capture_output=True,
                                  input=input_bytes, check=False, **({} if input_bytes is not None else {"stdin": stdin}))
            return subprocess.CompletedProcess(list(argv), proc.returncode,
                proc.stdout.decode('utf-8', 'replace'), proc.stderr.decode('utf-8', 'replace'))
        from misaka.core.web.browser.process import command
        rc, out, err = bridge.call(command(list(argv), env, cwd=str(get_hermes_home()),
                                           timeout=timeout, input_bytes=input_bytes))
        return subprocess.CompletedProcess(list(argv), rc, out, err)
    except (TimeoutError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(timeout_message) from exc
    except OSError as exc:
        raise RuntimeError(f"failed to invoke {label}: {type(exc).__name__}") from exc


async def invoke(handler, args, runtime, ctx):
    """Bind callbacks only for this worker/call; drain it on cancellation."""
    from misaka.core.web.browser.vault.backends import unlock
    from misaka.utils.async_lifecycle import settle

    bridge = Bridge(runtime, ctx, args.get("session", ""))
    def work():
        token = _bridge.set(bridge)
        unlock.set_current_session_id(runtime.vault_id)
        unlock._callback_tls.allowed = bridge.interactive
        unlock.set_unlock_prompt_callback(lambda _name, label: bridge.call(bridge.prompt(f"Unlock {label} (masked)")))
        unlock.set_code_prompt_callback(lambda site, _hint: bridge.call(bridge.prompt(f"Verification code for {site} (masked)")))
        unlock.set_save_login_prompt_callback(lambda origin, site: bridge.call(bridge.login(origin, site)))
        try:
            return handler(args, task_id=runtime.vault_id)
        finally:
            unlock.set_unlock_prompt_callback(None)
            unlock.set_code_prompt_callback(None)
            unlock.set_save_login_prompt_callback(None)
            unlock._callback_tls.allowed = False
            unlock.set_current_session_id(None)
            _bridge.reset(token)
    task = asyncio.create_task(asyncio.to_thread(work))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        bridge.stop()
        # The worker also raises CancelledError when its bridge future is cancelled.
        # Observe that outcome without skipping the loop-side resource drain.
        await settle(asyncio.gather(task, return_exceptions=True))
        await settle(asyncio.create_task(bridge.drain()))
        raise

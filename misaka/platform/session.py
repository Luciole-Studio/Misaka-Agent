"""Session adapter for creating, resuming, and driving engine sessions."""
import asyncio
import weakref
import json
import os
import threading

from misaka.agent.request_budget import install_turn_budget


def run_coro(coro):
    """Run a coroutine from synchronous code, using a helper thread if needed."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box = {}

    def _target():
        try:
            box["r"] = asyncio.run(coro)
        except BaseException as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join()
    if "e" in box:
        raise box["e"]
    return box["r"]


async def open_session(flags, cwd, extension_factories=None):
    """Open a session and return ``(runtime, session, error)``."""
    from misaka.cli.args import parse_args
    from misaka.cli.engine import create_runtime_factory, resolve_cli_paths
    from misaka.config import get_agent_dir
    from misaka.core.agent_session_runtime import create_agent_session_runtime
    from misaka.core.auth_storage import AuthStorage
    from misaka.core.session_manager import SessionManager
    from misaka.utils.paths import normalize_path

    # Every headless MISAKA session runs with the engine's own skill loading off: the skills
    # extension (misaka.skills.index) is the one place that decides what a session sees.
    parsed = parse_args(list(flags))
    errs = [d.message for d in parsed.diagnostics if d.type == "error"]
    if errs:
        return None, None, "; ".join(errs)
    session_dir = normalize_path(parsed.sessionDir) if parsed.sessionDir else None
    if parsed.noSession:
        sm = SessionManager.inMemory(cwd)
    elif parsed.session:
        sm = SessionManager.open(normalize_path(parsed.session), session_dir, cwd)
    elif parsed.fork:
        sm = SessionManager.forkFrom(normalize_path(parsed.fork), cwd, session_dir)
    elif parsed.continue_:
        sm = SessionManager.continueRecent(cwd, session_dir)
    else:
        sm = SessionManager.create(cwd, session_dir)
    runtime = await create_agent_session_runtime(
        create_runtime_factory(
            parsed, AuthStorage.create(),
            resolved_extension_paths=resolve_cli_paths(cwd, parsed.extensions),
            resolved_skill_paths=resolve_cli_paths(cwd, parsed.skills),
            resolved_prompt_template_paths=resolve_cli_paths(cwd, parsed.promptTemplates),
            resolved_theme_paths=resolve_cli_paths(cwd, parsed.themes),
            extension_factories=list(extension_factories) if extension_factories else None,
        ),
        {"cwd": sm.getCwd(), "agentDir": get_agent_dir(), "sessionManager": sm},
    )
    hard = [d.message for d in runtime.diagnostics if d.type == "error"]
    if hard:
        return runtime, None, "; ".join(hard)
    if runtime.session.model is None:
        return runtime, None, "No model is available; check provider, model, and credentials."
    return runtime, runtime.session, None


async def dispose(runtime):
    """Dispose a runtime with a hard 30-second timeout."""
    if runtime is None:
        return
    try:
        await asyncio.wait_for(runtime.dispose(), 30)
    except Exception:  # noqa: BLE001, S110 - dispose is best-effort with a hard timeout
        pass


def event_line(ev):
    """Serialize an engine event in the existing JSONL wire format."""
    from misaka.modes.rpc.jsonl import to_json_event
    return json.dumps(to_json_event(ev), ensure_ascii=False, separators=(",", ":"))


_ENV_LOCKS = weakref.WeakKeyDictionary()      # one lock per event loop


def _env_lock():
    loop = asyncio.get_running_loop()
    lock = _ENV_LOCKS.get(loop)
    if lock is None:
        lock = _ENV_LOCKS[loop] = asyncio.Lock()
    return lock


async def run_session(flags, prompt, cwd, on_event=None, timeout=600, env=None,
                      extension_factories=None):
    """Run one prompt in a throwaway session and return its final text, timeout flag, error, and token usage.

    The session's identity (role, profile, workspace, usage lease, MCP config) travels through
    ``os.environ`` because the extensions read it there, so two sessions in one process must not
    overlap: the environment window is held under a per-loop lock. Separate processes (nodes,
    cards) are naturally isolated."""
    async with _env_lock():
        return await _run_session(flags, prompt, cwd, on_event=on_event, timeout=timeout, env=env,
                                  extension_factories=extension_factories)


async def _run_session(flags, prompt, cwd, on_event=None, timeout=600, env=None,
                       extension_factories=None):
    old_env = {}
    for k, v in (env or {}).items():
        old_env[k] = os.environ.get(k)
        os.environ[k] = v
    runtime = None
    limiter = None
    timed_out = False
    try:
        runtime, session, err = await open_session(flags, cwd, extension_factories)
        if err:
            return {"text": None, "timed_out": False, "error": err, "budget_usage": None}
        limiter = install_turn_budget(session)
        if on_event:
            session.subscribe(lambda ev: on_event(event_line(ev)))

        async def prompt_and_drain_subagents():
            await session.prompt(prompt)
            # One-shot worker sessions cannot outlive their event loop.  Keep
            # them open long enough for detached agents, their completion
            # notification, and the model's follow-up turn to settle.
            from misaka.extensions.sisters.subagent import extension as subagent

            if not (
                subagent.has_background_task_records()
                or subagent.has_async_hooks()
            ):
                return
            quiet_passes = 0
            while quiet_passes < 2:
                await subagent.wait_for_background_tasks()
                await subagent.wait_for_async_hooks()
                await asyncio.sleep(0.05)
                await session.agent.waitForIdle()
                await asyncio.sleep(0.05)
                quiet_passes = (
                    0
                    if (
                        subagent.has_background_tasks()
                        or subagent.has_async_hooks()
                        or session.isStreaming
                    )
                    else quiet_passes + 1
                )

        try:
            await asyncio.wait_for(prompt_and_drain_subagents(), timeout)
        except TimeoutError:
            timed_out = True
            try:
                await asyncio.wait_for(session.abort(), 10)
            except Exception:  # noqa: BLE001, S110 - aborting after a timeout is best-effort
                pass
        text, err = None, None
        msgs = session.state.messages or []
        last = msgs[-1] if msgs else None
        role = last.get("role") if isinstance(last, dict) else getattr(last, "role", None)
        if role == "assistant":
            stop = last.get("stopReason") if isinstance(last, dict) else getattr(last, "stopReason", None)
            if stop in ("error", "aborted") and not timed_out:
                err = (last.get("errorMessage") if isinstance(last, dict)
                       else getattr(last, "errorMessage", None)) or f"request {stop}"
            parts = last.get("content") if isinstance(last, dict) else getattr(last, "content", None)
            for c in parts or []:
                ctype = c.get("type") if isinstance(c, dict) else getattr(c, "type", None)
                if ctype == "text":
                    text = (text or "") + (c.get("text") if isinstance(c, dict) else getattr(c, "text", ""))
        return {
            "text": text,
            "timed_out": timed_out,
            "error": err,
            "budget_usage": limiter.accounted if limiter is not None else None,
        }
    except Exception as e:  # noqa: BLE001
        return {
            "text": None,
            "timed_out": timed_out,
            "error": f"{type(e).__name__}: {e}",
            "budget_usage": limiter.accounted if limiter is not None else None,
        }
    finally:
        await dispose(runtime)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def run_text(prompt, cwd, provider, model, *, timeout=600, max_tokens=None):
    """Run one tool-free, personality-free model turn and return its text."""

    flags = ["--provider", provider, "--model", model, "--no-session", "-nt"]
    env = {"MISAKA_TURN_TOKEN_LIMIT": str(int(max_tokens))} if max_tokens else None
    result = run_coro(run_session(flags, prompt, cwd, timeout=timeout, env=env))
    if result["timed_out"] or result["error"]:
        return None
    return result["text"]

"""engine 会话适配层：按 CLI 旗子词汇表在本进程装配/驱动/回收一个会话。

worker（跑卡）、subagent_child（分身）、chat（前台）都踩这一层——
复用 engine 自己的装配件（parse_args → create_runtime_factory → create_agent_session_runtime），
argv 只是配置词汇表，不自创第二套配置语言；事件序列化复用 --mode json 的 to_jsonable，
线格式与 fork 前逐字节一致（budget/tail/subagent 三处消费者零改动）。
"""
import asyncio
import json
import os
import threading
from misaka.agent.request_budget import install_turn_budget


def run_coro(coro):
    """同步入口跑协程。已在事件循环里（Last Order 的 misaka_dispatch 工具）就挪去
    独立线程——嵌套 asyncio.run 会 RuntimeError，这是实打实会踩的路径。"""
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
    """装配会话。返回 (runtime, session, err)；err 非 None 时调用方仍须 dispose(runtime)。"""
    from misaka.cli.args import parse_args
    from misaka.config import get_agent_dir
    from misaka.core.agent_session_runtime import create_agent_session_runtime
    from misaka.core.auth_storage import AuthStorage
    from misaka.core.session_manager import SessionManager
    from misaka.cli.engine import create_runtime_factory, resolve_cli_paths
    from misaka.utils.paths import normalize_path

    parsed = parse_args(flags)
    errs = [d.message for d in parsed.diagnostics if d.type == "error"]
    if errs:
        return None, None, "; ".join(errs)
    session_dir = normalize_path(parsed.sessionDir) if parsed.sessionDir else None
    # inMemory 必须显式给 cwd——默认取 os.getcwd()（调度进程在仓目录），
    # 判官会跑去仓里核产物。显式 --session 则必须真的打开该 side-chain；
    # sub-agent 的 SendMessage 正是靠这个路径跨子进程续聊。
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
        return runtime, None, "no model available（provider/model 配置或密钥问题）"
    return runtime, runtime.session, None


async def dispose(runtime):
    """ponytail: dispose 卡死不许拖住调度——30s 硬顶，超时弃疗。"""
    if runtime is None:
        return
    try:
        await asyncio.wait_for(runtime.dispose(), 30)
    except Exception:  # noqa: BLE001
        pass


def event_line(ev):
    """engine 事件 → JSON 行（与旧 --mode json 输出同一序列化器，线格式不变）。"""
    from misaka.modes.rpc.jsonl import to_jsonable
    return json.dumps(to_jsonable(ev), ensure_ascii=False, separators=(",", ":"))


async def run_session(flags, prompt, cwd, on_event=None, timeout=600, env=None,
                      extension_factories=None):
    """一轮会话：装配 → prompt → 收尾。返回 {text, timed_out, error}。"""
    old_env = {}
    # ponytail: 改进程级环境（一进程一会话的现状安全）；进程内并行会话前必须改注入式
    for k, v in (env or {}).items():  # 工具注册函数在装配时读 MISAKA_* 环境变量
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
            from misaka.extensions.subagent import extension as subagent

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
        except asyncio.TimeoutError:
            timed_out = True
            try:
                await asyncio.wait_for(session.abort(), 10)
            except Exception:  # noqa: BLE001
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
    except Exception as e:  # noqa: BLE001  设施故障如实上报，不装成模型输出
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

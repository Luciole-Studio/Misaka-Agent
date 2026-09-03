"""Claude Code-compatible subagent tools.

The public contract intentionally mirrors the reference implementation:
``Agent`` creates one fresh agent, ``TaskOutput`` reads or waits for a
background task, and ``TaskStop`` terminates a running task. Parallelism
comes from multiple ``Agent`` tool calls in the same model message; there is
no private batch or chain mini-language.

``SendMessage`` itself lives in the unified messaging layer; this module only
exports ``route_to_children``, which resumes a child this session spawned, and
the assembly code plugs it into that layer.

Last Order is excluded at registration time. Every other MISAKA role gets the
same tools, including named child agents.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from misaka.core.extensions.types import ToolDefinition
from misaka.core.subagent import agents as agent_roster
from misaka.core.subagent.runtime import (
    AgentCancelled,
    RoleContext,
    SubagentManager,
    format_async_launch,
    format_sync_result,
    format_task_output,
)

AGENT_TOOL_NAME = "Agent"
TASK_OUTPUT_TOOL_NAME = "TaskOutput"
TASK_STOP_TOOL_NAME = "TaskStop"
_ACTIVE_MANAGERS: dict[asyncio.AbstractEventLoop, set[SubagentManager]] = {}
_TOOL_CEILING_UNSET = object()


def _canonical_role(value: str | None) -> str:
    return (value or "").strip().casefold().replace("_", "-").replace(" ", "-")


def _is_last_order_signal(value: str | None) -> bool:
    return _canonical_role(value) in {"last-order", "lo"}


_ENV_UNSET = object()


def _profile_role(profile_dir: str | None) -> str:
    if not profile_dir:
        return ""
    try:
        from misaka.config.profiles import role_of

        return role_of(profile_dir)
    except Exception:  # noqa: BLE001 - defensive import boundary: the directory name is the fallback
        return os.path.basename(profile_dir)


def allows_subagents(
    profile_dir: str | None = None,
    role: str | None = None,
    *,
    _env_role: str | None | object = _ENV_UNSET,
) -> bool:
    """Reject delegation when any trusted role signal identifies Last Order."""

    env_role = os.environ.get("MISAKA_WHO") if _env_role is _ENV_UNSET else _env_role
    candidates = (role, env_role, _profile_role(profile_dir))
    return all(not _is_last_order_signal(candidate) for candidate in candidates)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentParams(_StrictModel):
    description: str = Field(description="A short (3-5 word) description of the task")
    prompt: str = Field(description="The task for the agent to perform")
    subagent_type: str | None = Field(
        default=None,
        description="The type of specialized agent to use for this task",
    )
    model: Literal["sonnet", "opus", "haiku"] | None = Field(
        default=None,
        description=(
            "Optional model override. It takes precedence over the agent "
            "definition; otherwise the parent model is inherited."
        ),
    )
    run_in_background: bool = Field(
        default=False,
        description="Run in the background and notify the parent when complete",
    )
    name: str | None = Field(
        default=None,
        description="Optional addressable name for SendMessage",
    )
    isolation: Literal["worktree"] | None = Field(
        default=None,
        description="Run in a temporary git worktree",
    )
    cwd: str | None = Field(
        default=None,
        description="Absolute working directory; mutually exclusive with isolation",
    )


class TaskOutputParams(_StrictModel):
    task_id: str = Field(description="The task ID to get output from")
    block: bool = Field(default=True, description="Whether to wait for completion")
    timeout: int = Field(default=30_000, ge=0, le=600_000, description="Max wait time in ms")


class TaskStopParams(_StrictModel):
    task_id: str | None = Field(default=None, description="The background task ID to stop")


def _text_result(text: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text}],
        "details": details or {},
    }


def _coerce(model: type[BaseModel], raw: Any) -> BaseModel:
    return raw if isinstance(raw, model) else model.model_validate(raw or {})


def _render_result(result: Any, context: Any = None, *_args: Any) -> Any:
    """Compact task line in the TUI; rendering failure never affects execution."""

    try:
        from misaka.ui.tui import Text

        details = result.get("details", {}) if isinstance(result, dict) else {}
        status = details.get("status")
        task_id = details.get("agentId") or details.get("task_id")
        if not status or not task_id:
            return None
        mark = {"completed": "✓", "running": "…", "pending": "…", "failed": "✗", "killed": "■"}.get(
            status, "·"
        )
        return Text(f"{mark} {task_id}  {status}", paddingX=0, paddingY=0)
    except Exception:  # noqa: BLE001
        return None


def _agent_prompt(
    context: RoleContext,
    project_trusted: bool | None = None,
    cwd: str | None = None,
) -> str:
    include_project = (
        getattr(context, "project_trusted", True)
        if project_trusted is None
        else project_trusted
    )
    agents = agent_roster.discover(
        cwd=cwd or context.workspace,
        include_project=include_project,
    )
    if context.allowed_agent_types:
        allowed = set(context.allowed_agent_types)
        allowed = {"general-purpose" if item in {"general", "general-purpose"} else item for item in allowed}
        agents = {
            name: definition
            for name, definition in agents.items()
            if ("general-purpose" if name in {"general", "general-purpose"} else name) in allowed
        }
    listing = agent_roster.roster_text(agents)
    return f"""Launch a new agent to handle a complex, multi-step task autonomously.

Available agent types and their tools:
{listing}

Usage notes:
- Always include a short description and a complete, self-contained prompt.
- Omit subagent_type to use general-purpose.
- One Agent call creates one agent. Launch independent agents with multiple Agent calls in one message.
- Foreground is the default when the result is needed immediately.
- Background agents notify you automatically; do not sleep or poll them.
- Continue an agent with SendMessage and read a task with TaskOutput.
- isolation=\"worktree\" creates a temporary git worktree.
"""


class SubagentPart:
    """Agent / TaskOutput / TaskStop for one role, the manager behind them, and the role's agent policy."""

    def __init__(self, context: RoleContext, permitted: bool) -> None:
        from misaka.core.subagent import policy as subagent_policy

        self.role_context = context
        self.session: Any = None
        self.commands: list[Any] = []
        self.tools: list[ToolDefinition] = []
        self.policy = subagent_policy.policy_for(context)
        self.manager: SubagentManager | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        if not permitted:
            return
        manager = self.manager = SubagentManager(None, context)

        async def launch_agent(tool_call_id: str, raw: Any, signal: Any, on_update: Any, ctx: Any) -> dict[str, Any]:
            params = _coerce(AgentParams, raw)
            assert isinstance(params, AgentParams)
            if params.cwd and params.isolation:
                raise ValueError('cwd and isolation="worktree" are mutually exclusive')
            if params.cwd and not os.path.isabs(os.path.expanduser(params.cwd)):
                raise ValueError("cwd must be an absolute path")

            session_cwd = getattr(ctx, "cwd", None) or context.workspace
            call_cwd = params.cwd or session_cwd
            session_project_trusted = manager._context_project_trusted(
                ctx,
                context.project_trusted,
            )
            include_project, _ = manager._project_trust_for_cwd(
                call_cwd,
                session_project_trusted=session_project_trusted,
                explicit_cwd=params.cwd is not None,
                session_cwd=session_cwd,
            )
            definition = manager.resolve_definition(
                params.subagent_type,
                call_cwd,
                include_project,
            )
            await manager.require_mcp(definition)
            task = await manager.create_task(
                definition=definition,
                description=params.description,
                prompt=params.prompt,
                model=params.model,
                background=params.run_in_background,
                name=params.name,
                isolation=params.isolation,
                cwd=params.cwd,
                tool_call_id=tool_call_id,
                context=ctx,
                on_update=on_update,
                session_project_trusted=session_project_trusted,
                project_cwd_explicit=params.cwd is not None,
            )

            if params.run_in_background or bool(manager.field(definition, "background", False)):
                manager.run_background(task, params.prompt)
                data = task.async_result()
                return _text_result(format_async_launch(data), data)

            try:
                await manager.run_foreground(task, params.prompt, signal)
            except AgentCancelled:
                raise asyncio.CancelledError from None
            data = task.completed_result()
            return _text_result(format_sync_result(data), data)

        async def task_output(_tool_call_id: str, raw: Any, signal: Any, _on_update: Any, ctx: Any) -> dict[str, Any]:
            params = _coerce(TaskOutputParams, raw)
            assert isinstance(params, TaskOutputParams)
            data = await manager.task_output(
                params.task_id,
                block=params.block,
                timeout_ms=params.timeout,
                signal=signal,
                context=ctx,
            )
            return _text_result(format_task_output(data), data)

        async def stop_task(_tool_call_id: str, raw: Any, _signal: Any, _on_update: Any, ctx: Any) -> dict[str, Any]:
            params = _coerce(TaskStopParams, raw)
            assert isinstance(params, TaskStopParams)
            task_id = params.task_id
            if not task_id:
                raise ValueError("Missing required parameter: task_id")
            data = await manager.stop_task(task_id, context=ctx)
            return _text_result(json.dumps(data, ensure_ascii=False), data)

        self.agent_tool = ToolDefinition(
            name=AGENT_TOOL_NAME,
            label="Agent",
            description=_agent_prompt(context, False),
            parameters=AgentParams.model_json_schema(),
            execute=launch_agent,
            renderResult=_render_result,
            promptSnippet="Delegate one task to an autonomous agent",
            promptGuidelines=[
                "Give fresh agents all relevant context; they do not see the parent conversation.",
                "Use multiple Agent tool calls in one message for independent parallel work.",
                "Use background only when useful work remains for you to do in parallel.",
                "Completion arrives as a <task-notification>; never sleep or poll. TaskOutput reads a result, SendMessage continues the same agent, TaskStop stops one that is still running.",
                "Findings a delegate brings back need the same source verification as your own before you cite them.",
            ],
        )
        self.tools = [
            self.agent_tool,
            ToolDefinition(
                name=TASK_OUTPUT_TOOL_NAME,
                label="Task Output",
                description=(
                    "Read or wait for a registered background task. block=true waits up to timeout ms; "
                    "block=false returns its current state."
                ),
                parameters=TaskOutputParams.model_json_schema(),
                execute=task_output,
                renderResult=_render_result,
                promptSnippet="Read output from a background task",
            ),
            ToolDefinition(
                name=TASK_STOP_TOOL_NAME,
                label="Stop Task",
                description="Stop a running background task by ID",
                parameters=TaskStopParams.model_json_schema(),
                execute=stop_task,
                renderResult=_render_result,
                promptSnippet="Stop a running task",
            ),
        ]

    def attach(self, session: Any) -> None:
        self.session = session
        if self.manager is not None:
            self.manager.session = session
        if self.policy is not None:
            self.policy.session = session

    async def _policy(self, name: str, event: Any, ctx: Any) -> Any:
        if self.policy is None:
            return None
        result = getattr(self.policy, name)(event, ctx)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def before_agent_start(self, event: Any, ctx: Any) -> Any:
        return await self._policy("before_agent", event, ctx)

    async def tool_call(self, event: Any, ctx: Any) -> Any:
        return await self._policy("before_tool", event, ctx)

    async def tool_result(self, event: Any, ctx: Any) -> Any:
        return await self._policy("after_tool", event, ctx)

    async def agent_end(self, event: Any, ctx: Any) -> Any:
        return await self._policy("on_event", event, ctx)

    async def session_start(self, _event: Any, ctx: Any) -> None:
        manager = self.manager
        if manager is None:
            return
        # The loop the session runs on is known here, not when the part was built:
        # ``route_to_children`` and the drains below find this manager through it.
        self._loop = asyncio.get_running_loop()
        _ACTIVE_MANAGERS.setdefault(self._loop, set()).add(manager)
        description = _agent_prompt(
            self.role_context,
            ctx.isProjectTrusted(),
            getattr(ctx, "cwd", None) or self.role_context.workspace,
        )
        if description != self.agent_tool.description:
            self.agent_tool.description = description
            if self.session is not None:
                self.session.refreshTools()

    async def session_shutdown(self, _event: Any, _ctx: Any) -> None:
        manager = self.manager
        if manager is None:
            return
        try:
            await manager.close()
        finally:
            managers = _ACTIVE_MANAGERS.get(self._loop) if self._loop is not None else None
            if managers is not None:
                managers.discard(manager)
                if not managers:
                    _ACTIVE_MANAGERS.pop(self._loop, None)


def part_for(
    profile_dir: str,
    role: str,
    workspace: str,
    mcp_role: str | None = None,
    tool_ceiling: tuple[str, ...] | list[str] | None | object = _TOOL_CEILING_UNSET,
) -> SubagentPart:
    """The part for an immutable role/workspace snapshot."""

    capture_args = {
        "profile_dir": profile_dir,
        "role": role,
        "workspace": workspace,
        "mcp_role": mcp_role,
    }
    if tool_ceiling is not _TOOL_CEILING_UNSET:
        capture_args["tool_ceiling"] = tool_ceiling
    context = RoleContext.capture(**capture_args)
    return SubagentPart(context, allows_subagents(profile_dir, role, _env_role=role))


async def wait_for_background_tasks() -> None:
    """Keep a nested child process alive until its detached agents settle."""

    while True:
        managers = tuple(_ACTIVE_MANAGERS.get(asyncio.get_running_loop(), ()))
        runners = [
            task.runner
            for manager in managers
            for task in manager._tasks.values()
            if task.background and task.runner is not None and not task.runner.done()
        ]
        if not runners:
            return
        await asyncio.gather(*runners, return_exceptions=True)


async def wait_for_async_hooks() -> None:
    """Drain hooks owned by this loop before a nested supervisor exits."""

    while True:
        await asyncio.sleep(0)
        managers = tuple(_ACTIVE_MANAGERS.get(asyncio.get_running_loop(), ()))
        jobs = [
            job
            for manager in managers
            for job in (
                *manager._async_hook_jobs,
                *manager._async_cleanup_jobs,
            )
        ]
        if not jobs:
            return
        await asyncio.gather(*jobs, return_exceptions=True)


def has_async_hooks() -> bool:
    try:
        managers = tuple(_ACTIVE_MANAGERS.get(asyncio.get_running_loop(), ()))
    except RuntimeError:
        return False
    return any(
        True
        for manager in managers
        for job in (
            *manager._async_hook_jobs,
            *manager._async_cleanup_jobs,
        )
    )


def has_background_tasks() -> bool:
    try:
        managers = tuple(_ACTIVE_MANAGERS.get(asyncio.get_running_loop(), ()))
    except RuntimeError:
        return False
    return any(
        task.background and task.runner is not None and not task.runner.done()
        for manager in managers
        for task in manager._tasks.values()
    )


async def route_to_children(to: str, message: str, _summary: str, ctx: Any):
    """In-process branch of SendMessage: resume the child if THIS session spawned it, else return
    None so the mailbox handles it. A child's address is (parent session, name): another session's
    same-named child is never a match, so only the caller's own manager is consulted."""

    from misaka.core.subagent.runtime import _safe_component
    try:
        sid = _safe_component(str(ctx.sessionManager.getSessionId()))
    except Exception:  # noqa: BLE001 - no session identity: no child can be addressed safely
        return None
    managers = _ACTIVE_MANAGERS.get(asyncio.get_running_loop(), set())
    for manager in tuple(managers):
        if manager._closed:
            managers.discard(manager)
            continue
        if manager._parent_session_id != sid:
            continue
        try:
            # No ctx in the lookup on purpose: route only looks for live tasks (in memory or already
            # bound); binding a blank manager here would poison concurrent sessions (review 2026-08-20).
            if await manager._find_task_async(to) is None:
                continue
        except RuntimeError:
            continue
        return await manager.send_message(to, message, context=ctx)
    return None


def has_background_task_records() -> bool:
    """Whether this loop's session launched any detached agent this turn."""

    try:
        managers = tuple(_ACTIVE_MANAGERS.get(asyncio.get_running_loop(), ()))
    except RuntimeError:
        return False
    return any(
        task.background
        for manager in managers
        for task in manager._tasks.values()
    )


__all__ = [
    "AGENT_TOOL_NAME",
    "TASK_OUTPUT_TOOL_NAME",
    "TASK_STOP_TOOL_NAME",
    "AgentParams",
    "SubagentPart",
    "TaskOutputParams",
    "TaskStopParams",
    "allows_subagents",
    "has_async_hooks",
    "has_background_task_records",
    "has_background_tasks",
    "part_for",
    "route_to_children",
    "wait_for_async_hooks",
    "wait_for_background_tasks",
]

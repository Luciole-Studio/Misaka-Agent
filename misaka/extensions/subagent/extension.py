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
import json
import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from misaka.core.extensions.types import ToolDefinition
from misaka.extensions.subagent import agents as agent_roster
from misaka.extensions.subagent.runtime import (
    AgentCancelled,
    RoleContext,
    SubagentManager,
    format_async_launch,
    format_sync_result,
    format_task_output,
)

AGENT_TOOL_NAME = "Agent"
LEGACY_AGENT_TOOL_NAME = "Task"
TASK_OUTPUT_TOOL_NAME = "TaskOutput"
TASK_STOP_TOOL_NAME = "TaskStop"
SUBAGENT_TOOL_NAMES = (
    AGENT_TOOL_NAME,
    TASK_OUTPUT_TOOL_NAME,
    TASK_STOP_TOOL_NAME,
)
_ACTIVE_MANAGERS: dict[asyncio.AbstractEventLoop, set[SubagentManager]] = {}
_TOOL_CEILING_UNSET = object()


def _canonical_role(value: str | None) -> str:
    return (value or "").strip().casefold().replace("_", "-").replace(" ", "-")


def _is_last_order_signal(value: str | None) -> bool:
    return _canonical_role(value) in {"last-order", "last-ordre", "lo"}


_ENV_UNSET = object()


def _profile_role(profile_dir: str | None) -> str:
    if not profile_dir:
        return ""
    try:
        from misaka.config.profiles import role_of

        return role_of(profile_dir)
    except Exception:  # pragma: no cover - defensive import boundary
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
    team_name: str | None = Field(default=None, description="Reserved for agent teams")
    mode: str | None = Field(default=None, description="Reserved teammate permission mode")
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
    shell_id: str | None = Field(default=None, description="Deprecated alias for task_id")


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
        from misaka.tui import Text

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


def _agent_prompt(context: RoleContext) -> str:
    agents = agent_roster.discover(cwd=context.workspace)
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


def _register(harn: Any, context: RoleContext, permitted: bool) -> None:
    # In a child process these values come from the selected agent definition.
    # Register before the management tools so scoped argument rules and hooks
    # govern the complete child tool pool.
    from misaka.extensions.subagent import policy as subagent_policy

    subagent_policy.register(harn, context)
    if not permitted:
        return

    manager = SubagentManager(harn, context)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # registration-only embedders/tests
        loop = None
    if loop is not None:
        _ACTIVE_MANAGERS.setdefault(loop, set()).add(manager)

    async def launch_agent(tool_call_id: str, raw: Any, signal: Any, on_update: Any, ctx: Any) -> dict[str, Any]:
        params = _coerce(AgentParams, raw)
        assert isinstance(params, AgentParams)
        if params.team_name:
            raise ValueError("Agent teams are not enabled in MISAKA")
        if params.cwd and params.isolation:
            raise ValueError('cwd and isolation="worktree" are mutually exclusive')

        call_cwd = params.cwd or getattr(ctx, "cwd", None) or context.workspace
        definition = manager.resolve_definition(params.subagent_type, call_cwd)
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
        task_id = params.task_id or params.shell_id
        if not task_id:
            raise ValueError("Missing required parameter: task_id")
        data = await manager.stop_task(task_id, context=ctx)
        return _text_result(json.dumps(data, ensure_ascii=False), data)

    harn.registerTool(
        ToolDefinition(
            name=AGENT_TOOL_NAME,
            label="Agent",
            description=_agent_prompt(context),
            parameters=AgentParams.model_json_schema(),
            execute=launch_agent,
            renderResult=_render_result,
            promptSnippet="Delegate one task to an autonomous agent",
            promptGuidelines=[
                "Give fresh agents all relevant context; they do not see the parent conversation.",
                "Use multiple Agent tool calls in one message for independent parallel work.",
                "Use background only when useful work remains for you to do in parallel.",
            ],
        )
    )
    harn.registerTool(
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
        )
    )
    harn.registerTool(
        ToolDefinition(
            name=TASK_STOP_TOOL_NAME,
            label="Stop Task",
            description="Stop a running background task by ID",
            parameters=TaskStopParams.model_json_schema(),
            execute=stop_task,
            renderResult=_render_result,
            promptSnippet="Stop a running task",
        )
    )

    async def cleanup(_event: Any, _ctx: Any) -> None:
        try:
            await manager.close()
        finally:
            managers = _ACTIVE_MANAGERS.get(loop) if loop is not None else None
            if managers is not None:
                managers.discard(manager)
                if not managers:
                    _ACTIVE_MANAGERS.pop(loop, None)

    harn.on("session_shutdown", cleanup)


def bind(
    profile_dir: str,
    role: str,
    workspace: str,
    mcp_role: str | None = None,
    tool_ceiling: tuple[str, ...] | list[str] | None | object = _TOOL_CEILING_UNSET,
):
    """Return a factory bound to an immutable role/workspace snapshot."""

    capture_args = {
        "profile_dir": profile_dir,
        "role": role,
        "workspace": workspace,
        "mcp_role": mcp_role,
    }
    if tool_ceiling is not _TOOL_CEILING_UNSET:
        capture_args["tool_ceiling"] = tool_ceiling
    context = RoleContext.capture(**capture_args)
    # An explicit binding is the trusted session snapshot.  Re-reading the
    # process-global role here would couple concurrent in-process sessions.
    permitted = allows_subagents(profile_dir, role, _env_role=role)

    def bound(harn: Any) -> None:
        _register(harn, context, permitted)

    return bound


def register(harn: Any) -> None:
    """Legacy factory: capture the process environment once, then bind it."""

    context = RoleContext.capture()
    permitted = allows_subagents(
        context.profile_dir,
        context.role,
        _env_role=context.role,
    )
    _register(harn, context, permitted)


async def wait_for_background_tasks() -> None:
    """Keep a nested child process alive until its detached agents settle."""

    while True:
        managers = tuple(_ACTIVE_MANAGERS.get(asyncio.get_running_loop(), ()))
        runners = [
            task.runner
            for manager in managers
            for task in manager._tasks.values()  # noqa: SLF001 - same subsystem supervisor
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
                *manager._async_hook_jobs,  # noqa: SLF001 - subsystem supervisor
                *manager._async_cleanup_jobs,  # noqa: SLF001
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
            *manager._async_hook_jobs,  # noqa: SLF001 - subsystem supervisor
            *manager._async_cleanup_jobs,  # noqa: SLF001
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
        for task in manager._tasks.values()  # noqa: SLF001 - same subsystem supervisor
    )


async def route_to_children(to: str, message: str, _summary: str, ctx: Any):
    """In-process branch of SendMessage: resume the child if this session spawned it, else return None so the mailbox handles it."""

    managers = _ACTIVE_MANAGERS.get(asyncio.get_running_loop(), set())
    for manager in tuple(managers):
        if manager._closed:  # noqa: SLF001 - left over from an abnormal shutdown; drop it from the roster
            managers.discard(manager)
            continue
        try:
            # No ctx here on purpose: route only looks for live tasks (in memory or already bound).
            # Passing ctx would bind a blank manager to the caller's session and poison
            # concurrent sessions in the same process (review 2026-08-20).
            if manager._find_task(to) is None:  # noqa: SLF001
                continue
        except RuntimeError:  # Another session's manager: the ownership check raised, so skip it rather than fail the tool.
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
        for task in manager._tasks.values()  # noqa: SLF001 - same subsystem supervisor
    )


__all__ = [
    "AGENT_TOOL_NAME",
    "LEGACY_AGENT_TOOL_NAME",
    "TASK_OUTPUT_TOOL_NAME",
    "TASK_STOP_TOOL_NAME",
    "SUBAGENT_TOOL_NAMES",
    "AgentParams",
    "TaskOutputParams",
    "TaskStopParams",
    "allows_subagents",
    "bind",
    "register",
    "route_to_children",
    "wait_for_async_hooks",
    "wait_for_background_tasks",
    "has_async_hooks",
    "has_background_tasks",
    "has_background_task_records",
]

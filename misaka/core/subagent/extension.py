"""Claude Code-compatible subagent tools.

The public contract intentionally mirrors the reference implementation:
``Agent`` creates one fresh agent, ``TaskOutput`` reads or waits for a
background task, and ``TaskStop`` terminates a running task. Parallelism
comes from multiple ``Agent`` tool calls in the same model message; there is
no private batch or chain mini-language.

``SendMessage`` itself lives in the unified messaging layer; this module only
exports ``route_to_children``, which resumes a child this session spawned, and
the assembly code plugs it into that layer.

Last Order is excluded at registration time. Sister roots get the management
tools; generic children use the upstream child-tool filter and retain policy hooks.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator

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
from misaka.utils.values import read_field, semantic_boolean

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
    model_config = ConfigDict(extra="forbid", strict=True)
    _semantic_boolean = field_validator("run_in_background", "block", mode="before", check_fields=False)(semantic_boolean)


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


# Two stable native validators, not a JSON-schema-only view that Pi can coerce
# before a hook rewrite is checked. Provider schemas remain ordinary objects.
AgentForegroundParams = create_model(
    "AgentForegroundParams", __base__=_StrictModel,
    **{name: (field.annotation, field) for name, field in AgentParams.model_fields.items()
       if name != "run_in_background"},
)


class TaskOutputParams(_StrictModel):
    task_id: str = Field(description="The task ID to get output from")
    block: bool = Field(default=True, description="Whether to wait for completion")
    timeout: float = Field(default=30_000, ge=0, le=600_000, description="Max wait time in ms")


class TaskStopParams(_StrictModel):
    task_id: str | None = Field(default=None, description="The background task ID to stop")
    shell_id: str | None = Field(default=None, description="Legacy task identifier; task_id takes precedence")


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
        # Source AgentTool/UI.tsx branch structure; native Text/expanded mode
        # replaces Ink/transcript widgets. Reuse native number/time formatting
        # instead of recreating Intl or another UI framework.
        from misaka.core.tools.bash import _format_duration
        from misaka.core.tools.render_utils import get_text_output
        from misaka.ui.tui.interactive.components.footer import format_tokens

        expanded = bool(read_field(context, "expanded", False))
        lines = []
        if status == "async_launched":
            lines.append("Backgrounded agent")
        elif status == "completed" and "agentId" in details:
            count = details.get("totalToolUseCount", 0)
            uses = "1 tool use" if count == 1 else f"{count} tool uses"
            lines.append(f"Done ({uses} · {format_tokens(details.get('totalTokens', 0))} tokens · "
                         f"{_format_duration(details.get('totalDurationMs', 0))})")
        elif status in {"running", "pending"} and "agentId" in details:
            progress = details.get("progress") or {}
            count, tokens = progress.get("toolUseCount", 0), progress.get("tokenCount", 0)
            if not progress or (not count and not tokens and not progress.get("recentActivities")):
                lines.append("Initializing…")
            else:
                lines.append(f"In progress… · {count} tool {'use' if count == 1 else 'uses'}" +
                             (f" · {format_tokens(tokens)} tokens" if tokens else ""))
                # Source collapsed progress shows the last three rows. The
                # native tracker owns the bounded five-activity history.
                activities = progress.get("recentActivities") or []
                displayed = activities if expanded else activities[-3:]
                for activity in displayed:
                    lines.append(str(read_field(activity, "toolName", "")))
                hidden = max(0, count - len(displayed))
                if hidden:
                    lines.append(f"+{hidden} more tool {'use' if hidden == 1 else 'uses'}")
        else:
            mark = {"completed": "✓", "running": "…", "pending": "…", "failed": "✗", "killed": "■"}.get(status, "·")
            lines.append(f"{mark} {task_id}  {status}")
        if expanded and status in {"completed", "async_launched"}:
            completion = lines.pop(0) if status == "completed" else None
            prompt = details.get("prompt")
            if prompt:
                lines.extend(("Prompt:", str(prompt)))
            if status == "completed" and details.get("content"):
                lines.extend(("Response:", get_text_output(details, False)))
            if completion is not None:
                lines.append(completion)
        # Child names, prompts and output are data; retain the native terminal
        # escape/control-byte sanitizer, including on expanded renders.
        text = get_text_output({"content": [{"type": "text", "text": "\n".join(lines)}]}, False)
        return Text(text, paddingX=0, paddingY=0)
    except Exception:  # noqa: BLE001
        return None


def _agent_prompt(
    context: RoleContext,
    project_trusted: bool | None = None,
    cwd: str | None = None,
    *,
    catalog: dict[str, agent_roster.AgentDefinition] | None = None,
    fork_mode: bool = False,
) -> str:
    include_project = (
        getattr(context, "project_trusted", True)
        if project_trusted is None
        else project_trusted
    )
    agents = catalog if catalog is not None else agent_roster.discover(
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
    from misaka.core.subagent.prompt import render_prompt

    return render_prompt(agents, fork_mode=fork_mode)


class SubagentPart:
    """Agent / TaskOutput / TaskStop for one role, the manager behind them, and the role's agent policy."""

    def __init__(self, context: RoleContext, permitted: bool, *, disallowed_tools: tuple[str, ...] = ()) -> None:
        from misaka.core.subagent import policy as subagent_policy

        self.role_context = context
        self.session: Any = None
        self.commands: list[Any] = []
        self.tools: list[ToolDefinition] = []
        self._disallowed_tools = frozenset(name.casefold() for name in disallowed_tools)
        self._can_manage_tasks = not {TASK_OUTPUT_TOOL_NAME.casefold(), TASK_STOP_TOOL_NAME.casefold()} & self._disallowed_tools
        self.policy = subagent_policy.policy_for(context)
        self.manager: SubagentManager | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._flag_agents: Any = None
        self._catalog_diagnostics: tuple[tuple[str, str], ...] = ()
        if not permitted or AGENT_TOOL_NAME.casefold() in self._disallowed_tools:
            return
        self.manager = SubagentManager(None, context)
        from misaka.core.subagent.commands import agent_commands

        self.commands = agent_commands(self)

        async def launch_agent(tool_call_id: str, raw: Any, signal: Any, on_update: Any, ctx: Any) -> dict[str, Any]:
            manager = self.manager
            assert manager is not None
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
            from misaka.core.subagent import fork

            fork_mode = fork.enabled(ctx)
            if os.environ.get("MISAKA_FORK_CHILD") == "1" or (
                fork_mode and fork.in_fork_child(manager.session.agent.state.messages)
            ):
                raise ValueError("Fork children execute directly; spawning descendants is disabled")
            definition = (
                fork.definition(manager.session)
                if fork_mode and not params.subagent_type
                else manager.resolve_definition(params.subagent_type, call_cwd, include_project)
            )
            await manager.require_mcp(definition)
            task = await manager.create_task(
                definition=definition,
                description=params.description,
                prompt=params.prompt,
                model=params.model,
                background=params.run_in_background or fork_mode,
                name=params.name,
                isolation=params.isolation,
                cwd=params.cwd,
                tool_call_id=tool_call_id,
                context=ctx,
                on_update=on_update,
                session_project_trusted=session_project_trusted,
                project_cwd_explicit=params.cwd is not None,
            )

            from misaka.core.subagent.background import background_disabled

            if background_disabled():
                task.background = False
                await task.persist()
            if not background_disabled() and (fork_mode or params.run_in_background or bool(manager.field(definition, "background", False))):
                manager.run_background(task, params.prompt)
                data = task.async_result()
                return _text_result(format_async_launch(data), data)

            try:
                await manager.run_foreground(task, params.prompt, signal)
            except AgentCancelled:
                raise asyncio.CancelledError from None
            if task._backgrounded.is_set() and task.status not in {"completed", "failed", "killed"}:
                data = task.async_result()
                return _text_result(format_async_launch(data), data)
            data = task.completed_result()
            return _text_result(format_sync_result(data), data)

        async def task_output(_tool_call_id: str, raw: Any, signal: Any, _on_update: Any, ctx: Any) -> dict[str, Any]:
            manager = self.manager
            assert manager is not None
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
            manager = self.manager
            assert manager is not None
            params = _coerce(TaskStopParams, raw)
            assert isinstance(params, TaskStopParams)
            task_id = params.task_id if params.task_id is not None else params.shell_id
            if not task_id:
                raise ValueError("Missing required parameter: task_id")
            data = await manager.stop_task(task_id, context=ctx)
            return _text_result(json.dumps(data, ensure_ascii=False), data)

        from misaka.core.subagent.background import background_disabled

        agent_schema = AgentForegroundParams if background_disabled() else AgentParams
        self._launch_agent = launch_agent
        self.agent_tool = ToolDefinition(
            name=AGENT_TOOL_NAME,
            aliases=("Task",),
            label="Agent",
            description=_agent_prompt(context, False),
            parameters=agent_schema,
            execute=launch_agent,
            renderResult=_render_result,
            promptSnippet="Delegate one task to an autonomous agent",
            # Definitions and detailed usage stay here; the native collaboration
            # section explains the currently active delegation surface once.
        )
        self.tools = [
            self.agent_tool,
            ToolDefinition(
                name=TASK_OUTPUT_TOOL_NAME,
                aliases=("AgentOutputTool", "BashOutputTool"),
                label="Task Output",
                description=(
                    "Read or wait for a registered background task. block=true waits up to timeout ms; "
                    "block=false returns its current state."
                ),
                parameters=TaskOutputParams,
                execute=task_output,
                renderResult=_render_result,
                promptSnippet="Read output from a background task",
            ),
            ToolDefinition(
                name=TASK_STOP_TOOL_NAME,
                aliases=("KillShell",),
                label="Stop Task",
                description="Stop a running background task by ID",
                parameters=TaskStopParams,
                execute=stop_task,
                renderResult=_render_result,
                promptSnippet="Stop a running task",
            ),
        ]
        self.tools = [tool for tool in self.tools if tool.name.casefold() not in self._disallowed_tools]

    def project_tools(self, tools: list[Any]) -> list[Any]:
        from misaka.core.subagent.background import background_disabled
        from misaka.core.tools.bash import BackgroundBashToolInput, BashToolInput

        if self.manager is None or (not background_disabled() and
                {TASK_OUTPUT_TOOL_NAME, TASK_STOP_TOOL_NAME}.issubset(tool.name for tool in tools)):
            return tools
        # Do not mutate the registry: leaving a scope restores the exact builtin.
        # Only our builtin input type is projected; extension/SDK overrides retain
        # their schema and execution. Reject stale background fields, not a silent
        # downgrade into a blocking foreground command.
        schema = BashToolInput.model_json_schema()
        schema["additionalProperties"] = False
        return [tool.model_copy(update={
            "parameters": schema, "description": tool.description.split(" run_in_background=true", 1)[0],
        }) if tool.name == "bash" and tool.parameters is BackgroundBashToolInput else tool for tool in tools]

    def configure_tool_options(self, options: dict[str, Any]) -> None:
        from misaka.core.subagent.background import background_disabled

        if self.manager is None or not self._can_manage_tasks or background_disabled():
            return

        def current_manager(*, required):
            manager = self.manager  # Reload replaces owners; resolve on each call.
            available = manager is not None and not background_disabled()
            if self.session is not None:
                available = available and {TASK_OUTPUT_TOOL_NAME, TASK_STOP_TOOL_NAME}.issubset(self.session.getActiveToolNames())
            if not available and required:
                raise ValueError("Activate TaskOutput and TaskStop before starting a background command")
            return manager if available else None

        def start_background(**kwargs):
            from misaka.core.subagent.shell import background_result
            # Source ShellCommand.background removes its foreground timeout.
            kwargs['timeout'] = None
            return background_result(current_manager(required=True).start_shell(**kwargs))

        def register_foreground(**kwargs):
            manager = current_manager(required=False)
            if manager is None:
                return None  # Ordinary Pi foreground semantics in a restricted scope.
            from misaka.core.tools.bash import _resolve_timeout_seconds
            _resolve_timeout_seconds(kwargs.pop("timeout"))
            return manager.start_shell(**kwargs, timeout=None, background=False)

        options.setdefault("bash", {}).update(startBackground=start_background, registerForeground=register_foreground)

    def get_permission_mode(self):
        return self.policy.permission_mode if self.policy is not None else self.role_context.permission_mode

    def set_permission_mode(self, mode):
        from misaka.core.subagent import policy as subagent_policy
        from misaka.core.subagent.configuration import validate_permission_mode

        mode = validate_permission_mode(mode)
        if self.policy is None:
            self.policy = subagent_policy.AgentPolicy(self.role_context)
            self.policy.session = self.session
        self.policy.permission_mode = mode

    def attach(self, session: Any) -> None:
        self.session = session
        if self.manager is not None:
            self.manager.session = session
        if self.policy is not None:
            self.policy.session = session

    def configure_agents(self, definitions: Any) -> None:
        """MISAKA CLI/SDK adapter for CCB --agents JSON (flagSettings)."""
        self._flag_agents = definitions
        if self.manager is not None:
            self.manager.flag_agents = definitions

    async def before_agent_start(self, event: Any, _ctx: Any) -> Any:
        eligible = await self._refresh_roster(_ctx) if self.manager is not None else {}
        result = await self.policy.before_agent(event) if self.policy is not None else None
        from misaka.core.subagent.catalog import list_in_messages, listing_delta

        # A hidden Agent tool must not leak an unactionable catalog into the prompt.
        if list_in_messages() and self.session is not None:
            names = self.session.getActiveToolNames()
            if AGENT_TOOL_NAME in names:
                message = listing_delta(eligible or {}, self.session.agent.state.messages)
                if message is not None:
                    result = dict(result or {})
                    result["messages"] = [*(result.get("messages") or []), message]
        return result

    async def context(self, event: Any, ctx: Any) -> Any:
        from misaka.core.subagent.catalog import list_in_messages, listing_delta

        messages = read_field(event, "messages", [])
        attachments = []
        if (list_in_messages() and self.manager is not None and self.session is not None
                and AGENT_TOOL_NAME in self.session.getActiveToolNames()):
            eligible = await self._refresh_roster(ctx)
            delta = listing_delta(eligible or {}, messages)
            if delta is not None:
                attachments.append({"role": "custom", "timestamp": 0, **delta})
        reminder = self.role_context.critical_system_reminder
        if reminder:
            # CCB getCriticalSystemReminderAttachment + wrapInSystemReminder,
            # after native engine preflight on EVERY request. This is not
            # durable chat history and does not mutate the LCM source prefix.
            attachments.append({
                "role": "custom", "customType": "critical_system_reminder",
                "content": f"<system-reminder>\n{reminder}\n</system-reminder>",
                "display": False, "timestamp": 0,
            })
        return {"messages": [*messages, *attachments]} if attachments else None

    async def tool_call(self, event: Any, _ctx: Any) -> Any:
        name = str(read_field(event, "toolName", ""))
        if name.casefold() in self._disallowed_tools:
            return {"block": True, "reason": f"{name} is excluded by the upstream generic-child tool policy."}
        if self.policy is not None:
            return await self.policy.before_tool(event)

    async def tool_result(self, event: Any, _ctx: Any) -> Any:
        if self.policy is not None:
            return await self.policy.after_tool(event)

    async def agent_end(self, event: Any, ctx: Any) -> Any:
        if self.policy is not None:
            return await self.policy.on_event(event, ctx)

    async def session_start(self, _event: Any, ctx: Any) -> None:
        manager = self.manager
        if manager is None:
            return
        # Parts survive reload/session replacement; a closed manager does not.
        # Tool callbacks resolve self.manager at invocation, never an old closure.
        if manager._closed:
            manager = self.manager = SubagentManager(self.session, self.role_context)
            manager.flag_agents = self._flag_agents
        owner = getattr(ctx, "sessionManager", None)
        if owner is not None:
            from misaka.core.subagent.runtime import _safe_component

            manager._parent_session_id = _safe_component(str(owner.getSessionId()))
        # The loop the session runs on is known here, not when the part was built:
        # ``route_to_children`` and the drains below find this manager through it.
        self._loop = asyncio.get_running_loop()
        _ACTIVE_MANAGERS.setdefault(self._loop, set()).add(manager)
        await self._refresh_roster(ctx)

    async def _refresh_roster(self, ctx: Any) -> dict[str, agent_roster.AgentDefinition]:
        manager = self.manager
        if manager is None:
            return
        cwd = getattr(ctx, "cwd", None) or self.role_context.workspace
        trusted = ctx.isProjectTrusted()
        catalog = await asyncio.to_thread(manager.catalog, cwd, trusted)
        diagnostics = tuple((item["path"], item["error"]) for item in catalog.diagnostics)
        if diagnostics != self._catalog_diagnostics:
            for path, error in diagnostics:
                from misaka.core.subagent.runtime import _log_warning

                await asyncio.to_thread(_log_warning, f"Agent definition {path}: {error}")
            self._catalog_diagnostics = diagnostics
        get_tools = getattr(self.session, "getAllTools", None)
        tools = get_tools() if get_tools is not None else []
        available_servers = [tool.name.split("__", 2)[1] for tool in tools
                             if tool.name.startswith("mcp__") and tool.name.count("__") >= 2]
        from misaka.core.subagent.configuration import denied_agent_types

        denied_agents = denied_agent_types(self.session, cwd=cwd, include_project=trusted)
        eligible = {
            name: agent for name, agent in catalog.active_agents.items()
            if agent.name not in denied_agents and agent_roster.has_required_mcp_servers(agent, [
                *available_servers,
                # An isolated MISAKA worker owns inline clients; the parent
                # cannot connect those in advance. Its ready barrier is final.
                *(server for server, _ in agent_roster.inline_mcp_entries(agent.mcp_servers)),
            ])
        }
        if self.role_context.allowed_agent_types:
            allowed = {agent_roster._canonical_name(name) for name in self.role_context.allowed_agent_types}
            eligible = {name: agent for name, agent in eligible.items()
                        if agent_roster._canonical_name(agent.name) in allowed}
        from misaka.core.subagent.fork import enabled as fork_enabled

        description = _agent_prompt(
            self.role_context, trusted, cwd,
            catalog=eligible, fork_mode=fork_enabled(ctx),
        )

        from misaka.core.subagent.background import background_disabled
        schema = AgentForegroundParams if background_disabled() or fork_enabled(ctx) else AgentParams
        if description != self.agent_tool.description or schema != self.agent_tool.parameters:
            self.agent_tool.description = description
            self.agent_tool.parameters = schema
            if self.session is not None:
                self.session.refreshTools()
        return eligible

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
    *,
    disallowed_tools: tuple[str, ...] = (),
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
    return SubagentPart(context, allows_subagents(profile_dir, role, _env_role=role), disallowed_tools=disallowed_tools)


async def wait_for_background_tasks() -> None:
    """Keep a nested child process alive until its detached agents settle."""

    while True:
        managers = tuple(_ACTIVE_MANAGERS.get(asyncio.get_running_loop(), ()))
        runners = [
            task.runner
            for manager in managers
            for task in (*manager._tasks.values(), *manager._shell_tasks.values())
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
        for task in (*manager._tasks.values(), *manager._shell_tasks.values())
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
            # Ownership was bound by session_start and checked above. Only this
            # session's manager may lazily open its persisted child registry.
            if await manager._find_task_async(to, context=ctx) is None:
                continue
        except RuntimeError:
            continue
        return await manager.send_message(to, message, context=ctx)
    return None


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
    "has_background_tasks",
    "part_for",
    "route_to_children",
    "wait_for_async_hooks",
    "wait_for_background_tasks",
]

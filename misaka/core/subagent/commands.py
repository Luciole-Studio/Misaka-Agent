"""Native MISAKA command surfaces for CCB agent catalog and background control."""
from __future__ import annotations

import json
import shlex

from misaka.core.moments import CoreCommand
from misaka.core.subagent.agents import tools_description


def agent_commands(part):
    async def agents(args, ctx):
        words = (args or '').split(None, 1)
        manager = part.manager
        if manager is None:
            return
        if words and words[0] == 'shell':
            if not part._can_manage_tasks:
                raise ValueError('This role does not expose background task management')
            task = manager.start_shell(words[1] if len(words) > 1 else '', context=ctx)
            ctx.ui.notify(f'Background shell {task.id} started; use TaskOutput/TaskStop with this ID.', 'info')
            return
        argv = shlex.split(args or '')
        if argv and argv[0] == 'mode':
            if len(argv) > 2:
                raise ValueError('Usage: /agents mode [permission-mode]')
            if len(argv) == 2:
                part.session.setPermissionMode(argv[1])
            ctx.ui.notify('Permission mode: ' + (part.session.getPermissionMode() or 'default'), 'info')
            return
        if argv and argv[0] == 'gc':
            if len(argv) > 2:
                raise ValueError('Usage: /agents gc [retention-days]')
            count = await manager.cleanup_stale_worktrees(days=float(argv[1]) if len(argv) == 2 else None)
            ctx.ui.notify(f'Removed {count} stale agent worktrees.', 'info')
            return
        if argv and argv[0] == "fork":
            from misaka.core.subagent.fork import enabled
            if not enabled(ctx):
                raise ValueError("Fork agents require MISAKA_FORK_SUBAGENT=1 in an interactive session")
            directive = (args or "").partition(" ")[2].strip()
            if not directive:
                raise ValueError("Usage: /agents fork <directive>")
            result = await part._launch_agent("", {"description": "Fork task", "prompt": directive},
                                              None, None, ctx)
            ctx.ui.notify("\n".join(block.get("text", "") for block in result["content"]), "info")
            return
        if argv and argv[0] == 'memory':
            from misaka.core.subagent.management import memory_command
            await memory_command(argv[1:], manager, ctx)
            return
        if argv and argv[0] == 'background':
            if len(argv) != 2:
                raise ValueError('Usage: /agents background <task-id|all>')
            if argv[1] == 'all':
                # Source backgroundAll: shells and ordinary local agents.
                accepted = 0
                for task in (*manager._shell_tasks.values(), *manager._tasks.values()):
                    if not task.background:
                        accepted += bool(await manager.request_background(task))
                ctx.ui.notify(f'Background transition requested for {accepted} tasks.', 'info')
                return
            task = manager._shell_tasks.get(argv[1])
            if task is None:
                task = await manager._find_task_async(argv[1], context=ctx)
            if task is None:
                raise ValueError(f'Unknown task: {argv[1]}')
            accepted = await manager.request_background(task)
            ctx.ui.notify('Background transition accepted.' if accepted else 'Task is finished, stopped, or background mode is disabled.', 'info')
            return
        await part._refresh_roster(ctx)
        catalog = manager.catalog(ctx.cwd, ctx.isProjectTrusted())
        from misaka.core.subagent.catalog import display_catalog
        from misaka.core.subagent.management import manage

        if await manage(argv, catalog, ctx, part.role_context):
            await part._refresh_roster(ctx)
            return
        if argv == ["list"]:
            ctx.ui.notify(display_catalog(catalog), "info")
            return
        if argv:
            name = argv[0]
        else:
            choices = list(dict.fromkeys(agent.name for agent in catalog.active_agents.values()))
            if not getattr(ctx, 'hasUI', False):
                ctx.ui.notify(display_catalog(catalog), 'info')
                return
            name = await ctx.ui.select('Agent definitions', choices)
            if not name:
                return
        agent = catalog.active_agents.get(name)
        if agent is None:
            raise ValueError(f'Unknown agent definition: {name}')
        metadata = {
            'name': agent.name, 'source': agent.source, 'path': agent.path,
            'model': agent.model, 'tools': tools_description(agent),
            'permissionMode': agent.permission_mode, 'skills': agent.skills,
            'memory': agent.memory, 'background': agent.background, 'color': agent.color,
        }
        ctx.ui.notify(json.dumps(metadata, ensure_ascii=False, indent=2)+'\n\n'+agent.description+'\n\n'+agent.prompt, 'info')

    return [CoreCommand('agents', 'Inspect agent definitions or move a running agent into the background.', agents, '[shell <command> | gc [days] | list | name | create <scope> <name> | edit <name> | delete <name> | memory <id> | background <id> | fork <directive>]')]

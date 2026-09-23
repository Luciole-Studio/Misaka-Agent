"""CCB agentFileUtils/validateAgent adapted to MISAKA's user/role/project roots.

Actual discovered paths (not agent names) identify existing files. Full Markdown
editing preserves frontmatter the upstream narrow editor would discard. Native
atomic writes and a file lock retain concurrent edits; create is exclusive.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from filelock import FileLock

from misaka.config import home
from misaka.core.subagent.agents import COLORS, AgentDefinition, _user_agents_dir, parse
from misaka.utils.atomic import write_text


def validate_name(name: str) -> None:
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9-]*[a-zA-Z0-9]', name) or not 3 <= len(name) <= 50:
        raise ValueError('Agent name must be 3–50 letters, numbers or hyphens, starting and ending with a letter or number')


def format_markdown(name: str, description: str, prompt: str, *, tools=None, model=None,
                    color=None, memory=None, effort=None) -> str:
    """Source formatAgentAsMarkdown: undefined/* tools omitted; [] stays empty."""
    description = description.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\\\n')
    lines = ['---', f'name: {name}', f'description: "{description}"']
    if tools is not None and tools != ['*']:
        lines.append('tools: ' + ', '.join(tools))
    for key, value in (('model', model), ('effort', effort), ('color', color), ('memory', memory)):
        if value is not None:
            lines.append(f'{key}: {value}')
    return '\n'.join([*lines, '---', '', prompt, ''])


def writable_root(scope: str, ctx, role_context) -> Path:
    if scope == 'user':
        return _user_agents_dir().resolve()
    if scope == 'role' and role_context.profile_dir:
        return home.path('subagents', role_context.profile_dir).resolve()
    if scope == 'project':
        if not ctx.isProjectTrusted():
            raise ValueError('Project agent editing requires project trust')
        workspace = Path(ctx.cwd).resolve()
        project_dir = home.project_dir(workspace)
        if project_dir is None:
            raise ValueError('This directory has no project scope: its config directory is the MISAKA home')
        root = (project_dir / home.SUBAGENTS_DIR).resolve()
        if not root.is_relative_to(workspace):
            raise ValueError('Project agent directory resolves outside the project')
        return root
    raise ValueError('Agent location must be user, role or project')


def editable_path(agent: AgentDefinition, ctx, role_context) -> Path:
    if agent.source not in {'userSettings', 'projectSettings', 'localSettings'} or not agent.path:
        raise ValueError('This agent is read-only here; edit its owning settings or plugin instead')
    path = Path(agent.path)
    if path.is_symlink():
        raise ValueError('Agent editor does not follow file symlinks')
    scopes = ('project',) if agent.source in {'projectSettings', 'localSettings'} else ('user', 'role')
    roots = [writable_root(scope, ctx, role_context) for scope in scopes
             if scope != 'role' or role_context.profile_dir]
    if not any(path.resolve().is_relative_to(root) for root in roots):
        raise ValueError('Agent file is outside this session’s editable roots')
    return path


def validate_markdown(text: str, name: str) -> None:
    validate_name(name)
    with tempfile.TemporaryDirectory(prefix='misaka-agent-') as tmp:
        path = Path(tmp) / 'agent.md'
        path.write_text(text, encoding='utf-8')
        errors = []
        agent = parse(path, diagnostics=errors)
    if agent is None or errors:
        raise ValueError('Invalid agent definition: ' + '; '.join(item['error'] for item in errors))
    if agent.name != name:
        raise ValueError('Agent name must remain unchanged; create another definition to rename it')
    if len(agent.prompt) < 20:
        raise ValueError('System prompt must contain at least 20 characters')
    if agent.color is not None and agent.color not in COLORS:
        raise ValueError('Agent color must be one of: ' + ', '.join(COLORS))


def mutate(path: Path, text: str | None, *, expected: bytes | None = None) -> None:
    """Expected bytes fence edit/delete; no expected bytes means exclusive create."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + '.lock'):
        if path.is_symlink():
            raise ValueError('Agent file became a symlink')
        if expected is None:
            if text is None:
                raise ValueError('Delete requires an existing file snapshot')
            with path.open('x', encoding='utf-8') as output:
                output.write(text)
                output.flush()
                os.fsync(output.fileno())
        else:
            if not path.is_file() or hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(expected).digest():
                raise ValueError('Agent file changed during editing; reload before saving')
            if text is None:
                path.unlink()
            else:
                write_text(path, text)


async def manage(argv: list[str], catalog, ctx, role_context) -> bool:
    """Native /agents CRUD. UI editor or explicit file input, never a model turn."""
    import asyncio

    if not argv or argv[0] not in {'create', 'edit', 'delete'}:
        return False
    action = argv[0]
    if action == 'create':
        if len(argv) < 3:
            raise ValueError('Usage: /agents create <user|role|project> <name> [--file PATH]')
        scope, name = argv[1:3]
        validate_name(name)
        path = writable_root(scope, ctx, role_context) / (name + '.md')
        prefill = format_markdown(name, 'Describe when to use this agent.', 'Describe the agent’s task and instructions here.')
        expected, tail = None, argv[3:]
    else:
        if len(argv) < 2:
            raise ValueError(f'Usage: /agents {action} <name>')
        name = argv[1]
        agent = catalog.active_agents.get(name)
        if agent is None:
            raise ValueError(f'Unknown agent: {name}')
        path = editable_path(agent, ctx, role_context)
        expected = await asyncio.to_thread(path.read_bytes)
        prefill, tail = expected.decode('utf-8'), argv[2:]
    if action == 'delete':
        if tail not in ([], ['--yes']):
            raise ValueError('Usage: /agents delete <name> [--yes]')
        if not tail and not await ctx.ui.confirm('Delete agent definition?', str(path)):
            return True
        text = None
    else:
        if tail:
            if len(tail) != 2 or tail[0] != '--file':
                raise ValueError('Expected --file PATH or no extra arguments')
            text = await asyncio.to_thread(Path(tail[1]).expanduser().read_text, encoding='utf-8')
        else:
            text = await ctx.ui.editor(f'{action.title()} agent: {name}', prefill)
            if text is None:
                return True
        await asyncio.to_thread(validate_markdown, text, name)
    # Trust may change while an editor/confirmation is open.
    if action == 'create':
        if path.parent != writable_root(scope, ctx, role_context):
            raise ValueError('Agent location changed during editing')
    else:
        editable_path(agent, ctx, role_context)
    await asyncio.to_thread(mutate, path, text, expected=expected)
    ctx.ui.notify(f'Agent {action} complete: {path}', 'info')
    return True


async def memory_command(argv, manager, ctx):
    """Source snapshot initialize/replace/keep decisions via native /agents UI."""
    import asyncio

    from misaka.core.subagent.memory import (
        check_snapshot,
        copy_snapshot,
        mark_synced,
        snapshot_dir,
    )
    from misaka.core.subagent.runtime import TERMINAL_STATUSES, _safe_component
    from misaka.utils.async_lifecycle import run_in_thread

    if not 1 <= len(argv) <= 2 or (len(argv) == 2 and argv[1] not in {'status', 'replace', 'keep'}):
        raise ValueError('Usage: /agents memory <agent-id> [status|replace|keep]')
    task = await manager._find_task_async(argv[0], context=ctx)
    if task is None or not task.definition.memory:
        raise ValueError('No memory-enabled agent with this ID in the current session')
    if not manager._memory_enabled(task) or not task.project_trusted or not ctx.isProjectTrusted():
        raise ValueError('Agent memory and project trust are required for snapshot management')
    local = manager._memory_directory(task)
    project = Path(task.worktree.repo if task.worktree else task.cwd)
    snapshot = snapshot_dir(project, _safe_component(task.agent_type))
    if snapshot is None:
        raise ValueError('This directory has no project scope: its config directory is the MISAKA home')
    state = await asyncio.to_thread(check_snapshot, snapshot, local)
    operation = argv[1] if len(argv) == 2 else 'status'
    if operation == 'status' or state['action'] == 'none':
        ctx.ui.notify(f"Memory: {local}\nSnapshot: {snapshot}\nAction: {state['action']}", 'info')
        return
    # A task actively writing memory must finish before this operator replaces it.
    def require_idle():
        if any(other.status not in TERMINAL_STATUSES and other.definition.memory
               and manager._memory_directory(other) == local for other in manager._tasks.values()):
            raise ValueError('An active agent is using this memory directory; wait for it to finish')

    require_idle()
    timestamp = state['snapshotTimestamp']
    if operation == 'replace':
        if not await ctx.ui.confirm('Replace agent memory from project snapshot?', str(local)):
            return
        if not ctx.isProjectTrusted():
            raise ValueError('Project trust changed during confirmation')
        latest = await asyncio.to_thread(check_snapshot, snapshot, local)
        if latest.get('snapshotTimestamp') != timestamp:
            raise ValueError('Snapshot changed during confirmation; inspect it again')
        require_idle()  # An agent may have started while confirmation was open.
        await run_in_thread(copy_snapshot, snapshot, local, timestamp, replace=True)
    else:
        await run_in_thread(mark_synced, local, timestamp)
    ctx.ui.notify(f'Agent memory snapshot decision saved: {operation}', 'info')

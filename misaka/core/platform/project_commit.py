"""Committing the project to git, only when the user asks and only after the user confirms.

Nothing in MISAKA commits by itself: workflows, cards and their files leave the working tree as
it is, and the history is the user's to write. Two doors lead to a commit, both in a window the
user is sitting at: the ``/commit`` command, and the ``project_commit`` tool Last Order (or a
Sister chatting with the user) calls when the user asks in words. Either way the files that would
be committed are shown and the user presses confirm; a session with no screen cannot commit. The
tool exists only in foreground sessions that are not a workflow's own windows, and a workflow's
phase turn narrows the tools to its own set, so no phase of a run can reach it. The commit is made
under the repository's own configured identity.
"""
from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from misaka.core.extensions.types import ToolDefinition
from misaka.core.moments import CoreCommand
from misaka.core.platform import repo

_SHOWN = 40


def _text(value):
    return {"content": [{"type": "text", "text": value}], "details": {}}


async def commit(ctx, message, paths=()):
    """Show what would be committed, ask the user, and commit only on their confirmation. Returns
    what happened, in words for the model or the notification."""
    workspace = getattr(ctx, "cwd", None)
    if not workspace or not await asyncio.to_thread(repo.enabled, workspace):
        return "This project is not a git repository; nothing was committed. `misaka init` (or `git init`) creates one."
    if not getattr(ctx, "hasUI", False):
        return "A commit needs the user's confirmation in an interactive window, and this session has none; nothing was committed."
    try:
        lines = await asyncio.to_thread(repo.changes, workspace, list(paths))
    except OSError as error:
        return f"git could not list the changes: {error}"
    if not lines:
        return "Nothing to commit: the working tree matches the last commit" + (" for those paths." if paths else ".")
    shown = "\n".join(lines[:_SHOWN]) + (f"\n… and {len(lines) - _SHOWN} more" if len(lines) > _SHOWN else "")
    if not await ctx.ui.confirm(f"Commit {len(lines)} change(s)?", f"{message}\n\n{shown}"):
        return "The user did not confirm; nothing was committed."
    try:
        head = await asyncio.to_thread(repo.commit, workspace, message, list(paths))
    except OSError as error:
        return f"git refused the commit: {error}"
    return f"Committed {head}: {message} ({len(lines)} change(s))."


class CommitParams(BaseModel):
    message: str = Field(min_length=1, description="The commit message.")
    paths: list[str] = Field(default_factory=list, description=(
        "Paths to commit, relative to the project; empty commits every change in the project."))


class ProjectCommitPart:
    """``project_commit`` and ``/commit``."""

    def __init__(self):
        async def execute(_call_id, raw, _signal, _on_update, ctx):
            params = raw if isinstance(raw, CommitParams) else CommitParams(**(raw or {}))
            return _text(await commit(ctx, params.message, params.paths))

        async def command(args, ctx):
            message = (args or "").strip()
            if not message:
                message = (await ctx.ui.input("Commit message") or "").strip()
            if not message:
                ctx.ui.notify("No commit message; nothing was committed.", "info")
                return
            ctx.ui.notify(await commit(ctx, message), "info")

        description = ("Commit the project to git -- only when the user has asked for a commit in this conversation. "
                       "The user sees the files and confirms before anything is committed.")
        self.tools = [ToolDefinition(
            name="project_commit", label="Commit the project", description=description,
            parameters=CommitParams, execute=execute, promptSnippet="Commit the project to git when the user asks",
            promptGuidelines=[("Call `project_commit` only when the user asks for a commit; never on your own "
                               "initiative or as a step of other work.")])]
        self.commands = [CoreCommand("commit", "Commit the project to git after you confirm the files: /commit [MESSAGE].",
                                     command)]


SESSION_KINDS = {"foreground"}


def part(spec):
    from misaka.core.platform import owners
    if spec.workflow_context or owners.owns_window():   # a workflow's window, not the user's desk
        return None
    return ProjectCommitPart()

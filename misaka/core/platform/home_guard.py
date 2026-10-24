"""The agents' side of the home's rule about who writes where.

Inside the home an agent owns ``shared`` and the directories its session was handed; the rest
belongs to the program and the user (:func:`misaka.config.home.agent_may_write`). Before there
was a rule, Last Order and the Sisters filed whatever they needed at the top of the home --
``skill-library``, ``audits``, a ``state`` of their own -- and cards then cited those places by
absolute path, so nobody could tell a program directory from something an agent had left.

Two halves, because the tools differ in what can be known about them:

* ``write``, ``edit`` and ``office`` name their target, so an unattended session is simply
  refused (:func:`refusal`), in :meth:`HomeGuard.tool_call`, where those targets are resolved.
  A session with a person in it is left alone: she may have been asked to edit a setting. The
  same check refuses a worker rewriting the project's context files and anyone changing what a
  workflow's run froze (:func:`misaka.core.platform.owners.frozen_refusal`).
* A shell command does not say what it writes, and guessing would refuse honest reads -- the
  mistake of 2026-09-18 (B23). So nothing is refused: after each command the top of the home is
  compared with the layout table, and a new stray name is reported back on that very result,
  where the model can still move it.
"""

from __future__ import annotations

import os

from misaka.config import home
from misaka.core.wiring import KINDS
from misaka.utils.values import read_field

# Directories a session is handed through its environment, besides its workspace.
_GRANTS = ("MISAKA_TASK_OUTPUT_DIR", "MISAKA_SUBAGENT_MEMORY_DIR")
_SHELLS = frozenset({"bash", "powershell"})
# Sisters' cards and the sub-agents they start: the sessions that work for Last Order, unattended.
_WORKER_KINDS = frozenset({"card", "beast", "child"})
# The sessions whose file tools are checked: every kind with a person or a card in it, and Last
# Order's own one-shots (Sister one-shots and beast wrap-ups have no file tools to check).
_PATH_KINDS = frozenset({"foreground", "dm", "card", "child"})

SESSION_KINDS = KINDS


def refusal(target: str, workspace: str, kind: str) -> str | None:
    """Why an unattended session's file tool may not write ``target``, or None."""
    if kind == "foreground":
        return None
    if home.agent_may_write(target, (workspace, *(os.environ.get(name) for name in _GRANTS))):
        return None
    return (f"{home.display(target)} is inside the MISAKA home, which belongs to the program: there, "
            f"only {home.display(home.path('shared'))} and this session's own workspace may be written. "
            f"Material that several cards need goes under {home.display(home.path('shared'))}; a card's own "
            "work goes in its project folder.")


def _is_context_file_above(target, workspace):
    """Whether ``target`` is a file a session in ``workspace`` loads as its instructions: a context
    file name in the workspace or a folder above it (issue #10 audit, H4: a card could rewrite
    PROJECT.md, and every later session of the project took it as its brief)."""
    from misaka.core.resource_loader import CONTEXT_FILE_NAMES
    target = home.folded(target)
    if target.name not in {home.fold(name) for name in CONTEXT_FILE_NAMES}:
        return False
    here = home.folded(workspace)
    return here == target.parent or target.parent in here.parents


class HomeGuard:
    """Refuses a file tool's write where this session may not write, and reports a name a shell
    command left at the top of the home, once, on that command's result."""

    def __init__(self, kind="foreground", workspace=None, *, guard_paths=False, report_strays=True):
        self.tools = []                     # a part without tools of its own
        self._kind = kind
        self._workspace = workspace or os.getcwd()
        self._guard_paths = guard_paths
        self._report_strays = report_strays
        self._seen = set(home.strays())     # what was already there is not this session's doing

    def _refusal(self, path):
        """Why this session's file tool may not write ``path``, or None."""
        from misaka.core.platform import owners
        from misaka.core.tools.path_utils import resolveToCwd

        # Resolved as write and edit resolve it (a leading @, file://, Unicode spaces): a target
        # read any other way let `@PROJECT.md` past this check onto PROJECT.md.
        target = os.path.realpath(resolveToCwd(str(path or ""), self._workspace))
        if self._kind in _WORKER_KINDS and _is_context_file_above(target, self._workspace):
            return ("PROJECT.md, AGENTS.md and CLAUDE.md here are what every later session in this "
                    "project reads as its instructions; a Sister or sub-agent does not change them. "
                    "Tell Last Order what should change.")
        # The card owners' rule too: what a workflow's run saved and froze is not the tools' to change (B126).
        return refusal(target, self._workspace, self._kind) or owners.frozen_refusal(target)

    async def tool_call(self, event, ctx=None):
        if not self._guard_paths:
            return None
        tool = str(read_field(event, "toolName", "") or "")
        args = read_field(event, "input", None)
        args = args if isinstance(args, dict) else {}
        prepared = None
        if tool == "office":
            from misaka.core.tools._office.paths import output_paths
            from misaka.core.tools.office import prepare_office_input
            parsed, path, ops = prepare_office_input(args, self._workspace)
            # Pass the very ops we checked onward; re-reading @ops could change destinations.
            prepared = {"path": path, "ops": ops, "overwrite": parsed.overwrite}
            reason = next(filter(None, (self._refusal(target) for target in output_paths(path, ops))), None)
        elif tool in ("write", "edit"):
            reason = self._refusal(args.get("path"))
        else:
            return None
        if reason:
            return {"block": True, "reason": reason}
        return {"updatedInput": prepared} if prepared is not None else None

    async def tool_result(self, event, ctx=None):
        if not self._report_strays or read_field(event, "toolName") not in _SHELLS:
            return None
        found = [name for name in home.strays() if name not in self._seen]
        if not found:
            return None
        self._seen.update(found)
        shared = home.display(home.path("shared"))
        note = (f"[MISAKA home] This command left {', '.join(found)} at the top of {home.display()}. That "
                f"directory belongs to the program; only {shared} is yours there. Move it under {shared} "
                "(or into the project folder) and refer to it by its new path.")
        content = list(read_field(event, "content", None) or [])
        return {"content": [*content, {"type": "text", "text": note}]}


def part(spec):
    from misaka.config.profiles import is_last_order
    from misaka.core.wiring import role_key

    # As parts_for counts it: Last Order's beast wrap-up is one of her one-shots.
    kind = "bare" if role_key(spec) == "last_order" and spec.kind == "beast" else spec.kind
    guard_paths = kind in _PATH_KINDS or (kind == "bare" and is_last_order(spec.profile_dir))
    if kind == "bare" and not guard_paths:
        return None   # a Sister's one-shot or beast wrap-up: nothing to check
    return HomeGuard(spec.kind, spec.workspace, guard_paths=guard_paths, report_strays=kind != "bare")


__all__ = ["HomeGuard", "part", "refusal"]

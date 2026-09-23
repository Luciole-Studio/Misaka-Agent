"""The agents' side of the home's rule about who writes where.

Inside the home an agent owns ``shared`` and the directories its session was handed; the rest
belongs to the program and the user (:func:`misaka.config.home.agent_may_write`). Before there
was a rule, Last Order and the Sisters filed whatever they needed at the top of the home --
``skill-library``, ``audits``, a ``state`` of their own -- and cards then cited those places by
absolute path, so nobody could tell a program directory from something an agent had left.

Two halves, because the tools differ in what can be known about them:

* ``write``, ``edit`` and ``office`` name their target, so an unattended session is simply
  refused (:func:`refusal`). The check runs where those targets are already resolved, in the
  path guard of ``core.skills.wiring.skills``. A session with a person in it is left alone: she
  may have been asked to edit a setting.
* A shell command does not say what it writes, and guessing would refuse honest reads -- the
  mistake of 2026-09-18 (B23). So nothing is refused: after each command the top of the home is
  compared with the layout table, and a new stray name is reported back on that very result,
  where the model can still move it.
"""

from __future__ import annotations

import os

from misaka.config import home
from misaka.utils.values import read_field

# Directories a session is handed through its environment, besides its workspace.
_GRANTS = ("MISAKA_TASK_OUTPUT_DIR", "MISAKA_SUBAGENT_MEMORY_DIR")
_SHELLS = frozenset({"bash", "powershell"})


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


class HomeGuard:
    """Reports a name a shell command left at the top of the home, once, on that command's result."""

    def __init__(self):
        self.tools = []                     # a part without tools of its own
        self._seen = set(home.strays())     # what was already there is not this session's doing

    async def tool_result(self, event, ctx=None):
        if read_field(event, "toolName") not in _SHELLS:
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
    return HomeGuard()


__all__ = ["HomeGuard", "part", "refusal"]

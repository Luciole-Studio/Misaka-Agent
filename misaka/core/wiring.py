"""Session assembly: which bundled extensions a session gets, and in what order.

Pi's ``src/extensions/index.ts`` is a four-line array of the extensions that ship in the
package; the loader turns each into an ``InlineExtension`` through ``extensionFactories``.
MISAKA has roles and session kinds Pi does not, so its array is a registry whose entries
qualify themselves: a module takes part by defining ``activate(spec) -> register | None``
(``register(harn)`` installs it into the harness, ``None`` skips it for this session), and
may declare ``ROLES`` (default: every role), ``SESSION_KINDS`` (default: every kind but
``bare``), ``EXTENSION_NAME`` and ``HIDDEN``.

This was ``misaka.extensions.discover``, which found entries by scanning the
``extensions/`` folder and read a module's role off the sub-folder it sat in. The folder
is Pi's and stays, but what the scan found was product wiring that other packages import
as a library, not plug-ins, and a role was a fact about where a file lived rather than
something the file said. Now the registry names each entry outright, the entry names its
own roles, and ``extensions/`` is left holding what Pi's does: the bundled providers.

The registry is ordered and the order is load-bearing. Extension order is handler order
for every event the runner folds (``before_agent_start`` threads each handler's system
prompt into the next), and registration order is the ``tools`` array's order, which is
what a provider's prompt cache keys on. The order below is the one the folder scan
produced -- shared entries by name, then Last Order's, then the Sisters' -- so nothing a
model sees changes with the mechanism.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from misaka.config import profiles

type SessionKind = Literal["foreground", "dm", "card", "child", "beast", "bare"]

KINDS = frozenset({"foreground", "dm", "card", "child", "beast", "bare"})
DEFAULT_KINDS = KINDS - {"bare"}
# The two roles the registry tells apart: the coordinator, and everyone she dispatches.
ROLE_KEYS = frozenset({"last_order", "sisters"})


@dataclass(frozen=True, slots=True)
class SessionSpec:
    profile_dir: str
    role: str
    workspace: str
    kind: SessionKind
    sender: str | None = None
    mcp_role: str | None = None
    receive_messages: bool = False
    task_id: str | None = None
    tool_ceiling: tuple[str, ...] | None = None
    # Where this session's skills come from, as ``(layer, root)`` pairs. None = the role's
    # three layers (project, role, shared); a card passes its read-only sandbox instead.
    skill_roots: tuple[tuple[str, str], ...] | None = None


# Shared entries by name, then Last Order's, then the Sisters': the folder scan's order.
REGISTRY: tuple[str, ...] = (
    "misaka.extensions.agent_state",
    "misaka.extensions.ask_user",
    "misaka.extensions.coverage",
    "misaka.extensions.documents",
    "misaka.extensions.fork_split",
    "misaka.core.lcm",
    "misaka.extensions.llama",
    "misaka.extensions.mcp",
    "misaka.core.network.wiring.messages",
    "misaka.extensions.moa",
    "misaka.core.network.wiring.observe",
    "misaka.core.network.wiring.roster",
    "misaka.core.skills.wiring.skills",
    "misaka.core.network.wiring.todo",
    "misaka.core.web",
    "misaka.core.network.ally",
    "misaka.core.network.wiring.network",
    "misaka.core.network.wiring.peek",
    "misaka.core.research.wiring.research",
    "misaka.core.network.wiring.roster_admin",
    "misaka.core.subagent",
)


def inline(
    name: str, factory: Callable[[Any], Any], *, hidden: bool = False
) -> dict[str, Any]:
    """Build the named inline-extension descriptor used by Pi's resource loader."""
    return {"name": name, "factory": factory, "hidden": hidden}


def delegates(spec: SessionSpec) -> bool:
    """Whether the session may spawn sub-agents: every role except Last Order."""
    return not profiles.is_last_order(spec.profile_dir)


def role_key(spec: SessionSpec) -> str:
    """Which of ``ROLE_KEYS`` a session is: Last Order, or one of the Sisters."""
    return "last_order" if profiles.is_last_order(spec.profile_dir) else "sisters"


def build_extensions(spec: SessionSpec) -> list[dict[str, Any]]:
    """The inline extensions for one session, in registry order."""
    role = role_key(spec)
    kind = "bare" if role == "last_order" and spec.kind == "beast" else spec.kind  # a budget-less Last Order runs tool-less
    out: list[dict[str, Any]] = []
    for path in REGISTRY:
        mod = importlib.import_module(path)
        activate = getattr(mod, "activate", None)
        if activate is None:
            continue
        if role not in getattr(mod, "ROLES", ROLE_KEYS) or kind not in getattr(mod, "SESSION_KINDS", DEFAULT_KINDS):
            continue
        register = activate(spec)
        if register is not None:
            out.append(
                inline(
                    getattr(mod, "EXTENSION_NAME", path.rsplit(".", 1)[-1]),
                    register,
                    hidden=bool(getattr(mod, "HIDDEN", False)),
                )
            )
    return out


__all__ = [
    "DEFAULT_KINDS",
    "KINDS",
    "REGISTRY",
    "ROLE_KEYS",
    "SessionKind",
    "SessionSpec",
    "build_extensions",
    "delegates",
    "inline",
    "role_key",
]

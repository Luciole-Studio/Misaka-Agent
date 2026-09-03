"""Session assembly: which bundled extensions a session gets, and in what order.

Pi's ``src/extensions/index.ts`` is a four-line array of the extensions that ship in the
package; the loader turns each into an ``InlineExtension`` through ``extensionFactories``.
MISAKA has roles and session kinds Pi does not, so its array is a registry whose entries
qualify themselves: a module takes part by defining ``activate(spec) -> register | None``
(``register(harn)`` installs it into the harness, ``None`` skips it for this session), and
may declare ``ROLES`` (default: every role), ``SESSION_KINDS`` (default: every kind but
``bare``) and ``EXTENSION_NAME``. Every entry is hidden: core does not appear on the startup
screen, as Pi's built-ins do not.

This was ``misaka.extensions.discover``, which found entries by scanning the
``extensions/`` folder and read a module's role off the sub-folder it sat in. The folder
is Pi's and stays, but what the scan found was product wiring that other packages import
as a library, not plug-ins, and a role was a fact about where a file lived rather than
something the file said. Now the registry names each entry outright, the entry names its
own roles. ``misaka/extensions`` keeps its own folder scan for the bundled extensions --
the ones that are plug-ins in Pi's sense -- and core never imports it: the process entry
assigns that scan to ``bundled`` (``misaka.cli.bootstrap``), the way Pi's ``main.ts``
composes ``builtInExtensions`` ahead of everything else, and ``build_extensions`` puts its
result first.

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
from dataclasses import dataclass, field
from typing import Any, Literal

from misaka.config import profiles
from misaka.core.extensions.types import ToolDefinition

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
    "misaka.core.mcp",
    "misaka.core.network.wiring.messages",
    "misaka.core.network.wiring.roster",
    "misaka.core.skills.wiring.skills",
    "misaka.core.network.wiring.todo",
    "misaka.core.network.wiring.network",
    "misaka.core.research.wiring.research",
    "misaka.core.network.wiring.roster_admin",
    "misaka.core.subagent",
)


# Core modules whose whole contribution is tools. Pi's built-ins reach the tool table by
# direct construction; the SDK door for an embedder's tools is ``customTools``, which lands
# in the same table under ``<sdk:name>``. These take that door and never appear as
# extensions. Order is the tools-array order.
TOOL_MODULES: tuple[str, ...] = (
    "misaka.core.ask_user",
    "misaka.core.documents.wiring.documents",
    "misaka.core.web",
    "misaka.core.network.ally",
)


class ToolCollector:
    """The one harness capability a tool-only module may use: registering tools.

    Anything else -- an event subscription, a command, a message -- is not a tool and
    would have to reach the kernel another way; asking for it here fails at assembly,
    before a session exists, instead of silently doing nothing.
    """

    def __init__(self) -> None:
        self.tools: list[ToolDefinition] = []

    def registerTool(self, definition: ToolDefinition) -> None:
        self.tools.append(definition)

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(f"a tool-only module asked the harness for {name!r}; only registerTool is available here")


# Core modules the kernel calls at its moments (``core.moments``): each exposes
# ``part(spec) -> object | None`` with a ``tools`` list and the moment methods it needs.
# Their tools take the same ``customTools`` door as ``TOOL_MODULES``. Order is call order.
PART_MODULES: tuple[str, ...] = (
    "misaka.core.lcm",
)


def _qualifies(mod: Any, role: str, kind: str) -> bool:
    return role in getattr(mod, "ROLES", ROLE_KEYS) and kind in getattr(mod, "SESSION_KINDS", DEFAULT_KINDS)


def tools_for(spec: SessionSpec) -> list[ToolDefinition]:
    """The core tools a session gets through Pi's ``customTools`` door, in ``TOOL_MODULES`` order."""
    role = role_key(spec)
    kind = "bare" if role == "last_order" and spec.kind == "beast" else spec.kind
    collector = ToolCollector()
    for path in TOOL_MODULES:
        mod = importlib.import_module(path)
        if not _qualifies(mod, role, kind):
            continue
        register = mod.activate(spec)
        if register is not None:
            register(collector)
    return collector.tools


def parts_for(spec: SessionSpec) -> list[Any]:
    """The parts a session gets, in ``PART_MODULES`` order."""
    role = role_key(spec)
    kind = "bare" if role == "last_order" and spec.kind == "beast" else spec.kind
    parts: list[Any] = []
    for path in PART_MODULES:
        mod = importlib.import_module(path)
        if not _qualifies(mod, role, kind):
            continue
        part = mod.part(spec)
        if part is not None:
            parts.append(part)
    return parts


@dataclass(frozen=True, slots=True)
class Assembly:
    """Everything a session is handed for its spec, in the two shapes Pi's SDK takes them."""

    extension_factories: list[dict[str, Any]]
    custom_tools: list[ToolDefinition]
    parts: list[Any] = field(default_factory=list)

    def engine_options(self) -> dict[str, Any]:
        return {
            "extensionFactories": self.extension_factories,
            "customTools": self.custom_tools,
            "parts": self.parts,
        }


def assemble(spec: SessionSpec) -> Assembly:
    parts = parts_for(spec)
    return Assembly(
        extension_factories=build_extensions(spec),
        custom_tools=[*tools_for(spec), *(tool for part in parts for tool in part.tools)],
        parts=parts,
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


def _no_bundled(spec: SessionSpec) -> list[dict[str, Any]]:
    return []


# The bundled extensions, composed in by the process entry (``misaka.cli.bootstrap``) --
# core does not import ``misaka.extensions``. A process that never runs an entry, such
# as a bare kernel run in a test, builds sessions without them.
bundled: Callable[[SessionSpec], list[dict[str, Any]]] = _no_bundled


def build_extensions(spec: SessionSpec) -> list[dict[str, Any]]:
    """The inline extensions for one session: the bundled ones first, then core in registry order."""
    role = role_key(spec)
    kind = "bare" if role == "last_order" and spec.kind == "beast" else spec.kind  # a budget-less Last Order runs tool-less
    out: list[dict[str, Any]] = []
    for path in REGISTRY:
        mod = importlib.import_module(path)
        activate = getattr(mod, "activate", None)
        if activate is None:
            continue
        if not _qualifies(mod, role, kind):
            continue
        register = activate(spec)
        if register is not None:
            out.append(
                inline(
                    getattr(mod, "EXTENSION_NAME", path.rsplit(".", 1)[-1]),
                    register,
                    # Core is not listed, the way Pi's own built-ins are not: the startup
                    # screen's "Extensions" section is for what the user added.
                    hidden=True,
                )
            )
    return [*bundled(spec), *out]


__all__ = [
    "DEFAULT_KINDS",
    "KINDS",
    "PART_MODULES",
    "REGISTRY",
    "ROLE_KEYS",
    "TOOL_MODULES",
    "Assembly",
    "SessionKind",
    "SessionSpec",
    "ToolCollector",
    "assemble",
    "build_extensions",
    "bundled",
    "delegates",
    "inline",
    "parts_for",
    "role_key",
    "tools_for",
]

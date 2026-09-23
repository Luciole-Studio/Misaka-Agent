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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from misaka.config import profiles

if TYPE_CHECKING:
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
    startup_skills: tuple[str, ...] = ()
    # Optional one-call catalog snapshot shared with Research's assignment validator.
    sister_catalog: tuple[dict[str, Any], ...] | None = None
    research_context: bool = False


# Shared entries by name, then Last Order's, then the Sisters': the folder scan's order.


# Core modules whose whole contribution is tools. Pi's built-ins reach the tool table by
# direct construction; the SDK door for an embedder's tools is ``customTools``, which lands
# in the same table under ``<sdk:name>``. These take that door and never appear as
# extensions. Order is the tools-array order.
TOOL_MODULES: tuple[str, ...] = (
    "misaka.core.ask_user",
    "misaka.core.documents.wiring.documents",
    "misaka.core.research.tools",
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
    "misaka.core.session_catalog",
    "misaka.core.moa",
    "misaka.core.mcp",
    "misaka.core.web",
    "misaka.core.network.wiring.messages",
    "misaka.core.network.wiring.roster",
    "misaka.core.network.wiring.todo",
    "misaka.core.network.wiring.network",
    "misaka.core.network.wiring.panel",
    "misaka.core.network.wiring.roster_admin",
    "misaka.core.network.wiring.capabilities",
    "misaka.core.network.wiring.collaboration",
    "misaka.core.skills.wiring.skills",
    "misaka.core.platform.home_guard",
    "misaka.core.research.wiring.research",
    "misaka.core.research.wiring.node",
    "misaka.core.subagent",
)


# Core modules that are providers (``provider_config(configured, find) -> (name, config) | None``,
# with the registry's own credential check and model lookup): part of every model registry,
# recomputed on each reload, as pi's built-in providers are.
PROVIDER_MODULES: tuple[str, ...] = (
    "misaka.core.moa",
)


def core_providers(
    configured: Callable[[str], bool],
    find: Callable[[str, str], Any] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """``(name, config)`` for each core provider that has something to publish right now.

    ``configured`` is the registry's credential check and ``find`` its model lookup; a
    provider that composes other models needs the second to size itself.
    """
    out: list[tuple[str, dict[str, Any]]] = []
    for path in PROVIDER_MODULES:
        entry = importlib.import_module(path).provider_config(configured, find)
        if entry is not None:
            out.append(entry)
    return out


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


class Assembly:
    """What a session is made of; each piece is built when first read.

    Built late on purpose: a worker applies the session's environment (``platform.session``'s
    environment window) after it assembled, and a part reads that environment when it is
    built -- as an extension factory used to, at load time. Pass the pieces explicitly to
    bypass the build (tests, a bare engine).
    """

    def __init__(
        self,
        extension_factories: list[dict[str, Any]] | None = None,
        custom_tools: list[ToolDefinition] | None = None,
        parts: list[Any] | None = None,
        *,
        spec: SessionSpec | None = None,
        extra_tools: tuple[ToolDefinition, ...] = (),
    ) -> None:
        self.spec = spec
        self._extension_factories = extension_factories
        self._custom_tools = custom_tools
        self._extra_tools = extra_tools
        self._parts = parts

    @property
    def extension_factories(self) -> list[dict[str, Any]]:
        if self._extension_factories is None:
            self._extension_factories = build_extensions(self.spec) if self.spec else []
        return self._extension_factories

    @property
    def parts(self) -> list[Any]:
        if self._parts is None:
            self._parts = parts_for(self.spec) if self.spec else []
        return self._parts

    @property
    def custom_tools(self) -> list[ToolDefinition]:
        if self._custom_tools is None:
            own = tools_for(self.spec) if self.spec else []
            self._custom_tools = [*own, *(tool for part in self.parts for tool in part.tools), *self._extra_tools]
        return self._custom_tools

    def engine_options(self) -> dict[str, Any]:
        return {
            "extensionFactories": self.extension_factories,
            "customTools": self.custom_tools,
            "parts": self.parts,
            "modelProfile": self.model_profile,
            "modelDefaultsReadOnly": self.model_defaults_read_only,
        }

    @property
    def model_profile(self) -> str | None:
        # A generic child owns its agent definition, not its parent's role pin.
        return self.spec.profile_dir if self.spec and self.spec.kind != "child" else None

    @property
    def model_defaults_read_only(self) -> bool:
        return bool(self.spec and self.spec.kind == "child")


def role_session_setup(profile_dir, workspace, *, model=None,
                       receive_messages=False, research_context=False, startup_skills=()):
    """One role session entry, independent of terminal, root/fork and lifetime.

    Callers add only their transport/session-selection flags. Research changes the
    mode overlay and per-phase tool scope, never the base capability registry.
    """
    from misaka.config import identity

    role = profiles.role_of(profile_dir)
    sender = "last-order" if profiles.is_last_order(profile_dir) else role.rsplit("/", 1)[-1]
    flags = []
    override = profiles.explicit_model_override(profile_dir, model)
    if override:
        flags += ["--model", override]
    for section in identity.base_prompt_sources(profile_dir, role):
        flags += ["--append-system-prompt", section]
    assembly = assemble(SessionSpec(
        profile_dir=profile_dir, role=role, workspace=workspace, kind="foreground",
        sender=sender, mcp_role=sender, receive_messages=receive_messages,
        research_context=research_context, startup_skills=tuple(startup_skills)))
    from misaka.config import env as env_file

    # The role's own .env first (its vendor keys, its plugin knobs), then the hand-off names.
    env = {**env_file.role_overlay(profile_dir),
           "MISAKA_PROFILE_DIR": profile_dir, "MISAKA_WHO": sender,
           "MISAKA_MCP_ROLE": sender, "MISAKA_WORKSPACE": workspace}
    return flags, assembly, env


def assemble(spec: SessionSpec) -> Assembly:
    return Assembly(spec=spec)


def inline(
    name: str, factory: Callable[[Any], Any], *, hidden: bool = False
) -> dict[str, Any]:
    """Build the named inline-extension descriptor used by Pi's resource loader."""
    return {"name": name, "factory": factory, "hidden": hidden}


def delegates(spec: SessionSpec) -> bool:
    """Sister roots delegate; generic children follow the upstream tool filter."""
    from misaka.core.subagent import disallowed_management_tools

    return not profiles.is_last_order(spec.profile_dir) and "Agent" not in disallowed_management_tools(spec.kind)


def sender_address(spec: SessionSpec) -> str:
    """The mailbox name this session sends as and, with ``receive_messages``, reads."""
    return spec.sender or spec.role.rsplit("/", 1)[-1]


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
    """The inline extensions for one session: the bundled ones the process entry installed.

    Core contributes none: its tools take ``customTools`` and its moments are called by the
    kernel (``PART_MODULES``), the way pi's core never sits on its own extension runner.
    """
    return list(bundled(spec))


__all__ = [
    "DEFAULT_KINDS",
    "KINDS",
    "PART_MODULES",
    "PROVIDER_MODULES",
    "ROLE_KEYS",
    "TOOL_MODULES",
    "Assembly",
    "SessionKind",
    "SessionSpec",
    "ToolCollector",
    "assemble",
    "build_extensions",
    "bundled",
    "core_providers",
    "delegates",
    "inline",
    "parts_for",
    "role_key",
    "tools_for",
]

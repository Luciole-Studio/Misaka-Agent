"""The single assembly point for bundled Pi extensions.

Feature modules own their implementation.  This module only maps declared
capability requirements to bound inline factories for a concrete session.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import Callable

from misaka.config import capabilities
from misaka.extensions import inline


@dataclass(frozen=True, slots=True)
class SessionSpec:
    profile_dir: str
    role: str
    workspace: str
    kind: capabilities.SessionKind
    sender: str | None = None
    mcp_role: str | None = None
    receive_messages: bool = False
    task_id: str | None = None
    tool_ceiling: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class Feature:
    name: str
    requires: frozenset[str]
    factory: Callable[[SessionSpec], Callable]
    hidden: bool = False


def _documents(_spec: SessionSpec):
    from misaka.documents import tools as docs

    return docs.register


def _ask_user(_spec: SessionSpec):
    from misaka.extensions.ask_user import extension

    return extension.register


def _network(_spec: SessionSpec):
    from misaka.network import start

    return start.register


def _research(_spec: SessionSpec):
    from misaka.network import worker
    from misaka.network.sister_runtime import SisterRuntime
    from misaka.research import start

    return start.bind(
        lambda harn, con_factory, cfg_factory: SisterRuntime(harn, con_factory, cfg_factory),
        worker,
    )


def _ally(_spec: SessionSpec):
    from misaka.extensions.ally import extension

    return extension.register


def _subagent(spec: SessionSpec):
    # Bind when Pi loads the factory, after run_session has installed this
    # worker's budget/identity environment.  Building the composition earlier
    # must not capture the parent process environment.
    def bound(harn):
        from misaka.extensions.subagent import extension

        extension.bind(
            spec.profile_dir,
            spec.role,
            spec.workspace,
            mcp_role=spec.mcp_role or spec.role,
            tool_ceiling=spec.tool_ceiling,
        )(harn)

    return bound


def _messages(spec: SessionSpec):
    from misaka.network import messages

    route = None
    if capabilities.DELEGATE in capabilities.resolve(spec.profile_dir, spec.kind):
        from misaka.extensions.subagent import extension as subagent

        route = subagent.route_to_children
    return partial(
        messages.register,
        sender=(spec.sender or spec.role.rsplit("/", 1)[-1]),
        route=route,
        receive=spec.receive_messages,
    )


def _switch(_spec: SessionSpec):
    from misaka.network import switch

    return switch.register


def _roster(_spec: SessionSpec):
    from misaka.network import roster

    return roster.register


def _moa_provider(_spec: SessionSpec):
    from misaka.extensions.moa import extension as moa

    return moa.register_provider


def _moa_command(_spec: SessionSpec):
    from misaka.extensions.moa import extension as moa

    return moa.register_command


def _views(_spec: SessionSpec):
    from misaka.observability import commands as views

    return views.register


def _observe_tools(_spec: SessionSpec):
    from misaka.observability import tools

    return tools.register


def _lcm(_spec: SessionSpec):
    from misaka.extensions.lcm import extension as lcm

    return lcm.register


def _skill_commands(spec: SessionSpec):
    from misaka.skills import tools as skill_invoke

    return skill_invoke.commands_for(spec.profile_dir)


def _skill_tools(spec: SessionSpec):
    from misaka.skills import tools as skill_invoke

    return skill_invoke.tools_for(spec.profile_dir)


def _todo(spec: SessionSpec):
    from misaka.network import todo

    return todo.tools_for(spec.task_id)


def _mcp(spec: SessionSpec):
    from misaka.extensions import mcp

    return mcp.bind(spec.profile_dir, spec.mcp_role or spec.role)


# Ordering is stable for diagnostics and tests, but selection is entirely
# capability-driven.  A feature cannot grant itself authority.
FEATURES = (
    # A configured ``provider=moa`` must resolve in card/child/headless
    # sessions too.  This hidden registrar exposes no command or tool; the
    # user-facing /moa command remains capability-gated below.
    Feature("moa-provider", frozenset(), _moa_provider, hidden=True),
    Feature("ask-user", frozenset({capabilities.ASK_USER}), _ask_user),
    Feature("doc-tools", frozenset({capabilities.DOCUMENTS}), _documents),
    Feature("board-tools", frozenset({capabilities.NETWORK_CONTROL}), _network),
    Feature("research", frozenset({capabilities.RESEARCH}), _research),
    Feature("ally-tools", frozenset({capabilities.ALLY}), _ally),
    Feature("subagent", frozenset({capabilities.DELEGATE}), _subagent),
    Feature("messages", frozenset({capabilities.MESSAGES}), _messages),
    Feature("switch", frozenset({capabilities.ROSTER}), _switch),
    Feature("roster", frozenset({capabilities.ROSTER}), _roster),
    Feature("moa", frozenset({capabilities.MOA}), _moa_command),
    Feature("views", frozenset({capabilities.OBSERVE}), _views),
    Feature(
        "observability-tools",
        frozenset({capabilities.OBSERVE, capabilities.NETWORK_CONTROL}),
        _observe_tools,
    ),
    Feature("lcm", frozenset({capabilities.LCM}), _lcm),
    Feature("skill-invoke", frozenset({capabilities.SKILLS}), _skill_commands),
    Feature("skill-tools", frozenset({capabilities.SKILLS}), _skill_tools),
    Feature("todo", frozenset({capabilities.TODO}), _todo),
    Feature("mcp", frozenset({capabilities.MCP}), _mcp),
)


def build_extensions(spec: SessionSpec) -> list[dict]:
    granted = capabilities.resolve(spec.profile_dir, spec.kind)
    result = []
    for feature in FEATURES:
        if not feature.requires <= granted:
            continue
        if feature.name == "todo" and not spec.task_id:
            continue
        if (
            feature.name == "messages"
            and spec.kind in {"one-shot", "beast"}
            and capabilities.DELEGATE not in granted
        ):
            continue
        result.append(inline(feature.name, feature.factory(spec), hidden=feature.hidden))
    return result


__all__ = ["FEATURES", "Feature", "SessionSpec", "build_extensions"]

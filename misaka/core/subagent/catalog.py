"""CCB agentDisplay.ts and attachments.ts agent-list delta, adapted to Pi messages.

Source: 77a7934e15d69da13879112ed7db695c9ee7a52a. No parallel catalog cache:
reconstruct announcements from the active conversation, including after compact.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from misaka.core.subagent.agents import (
    AgentDefinition,
    AgentDefinitionsResult,
    roster_text,
)
from misaka.utils.values import read_field

SOURCE_GROUPS = (
    ('User agents', 'userSettings'), ('Project agents', 'projectSettings'),
    ('Local agents', 'localSettings'), ('Managed agents', 'policySettings'),
    ('Plugin agents', 'plugin'), ('CLI arg agents', 'flagSettings'),
    ('Built-in agents', 'built-in'),
)


def resolved_agents(catalog: AgentDefinitionsResult) -> list[dict[str, Any]]:
    """Source resolveAgentOverrides; deduplicate worktree copies, retain shadows."""
    active = {agent.name: agent for agent in catalog.active_agents.values()}
    seen = set()
    result = []
    for agent in catalog.all_agents:
        key = agent.name, agent.source
        if key in seen:
            continue
        seen.add(key)
        winner = active.get(agent.name)
        result.append({'agent': agent, 'overriddenBy': winner.source
                       if winner is not None and winner.source != agent.source else None})
    return result


def display_catalog(catalog: AgentDefinitionsResult) -> str:
    entries = resolved_agents(catalog)
    lines = []
    total = 0
    for label, source in SOURCE_GROUPS:
        group = sorted((item for item in entries if item['agent'].source == source),
                       key=lambda item: item['agent'].name.casefold())
        if not group:
            continue
        lines.append(label + ':')
        for item in group:
            agent = item['agent']
            fields = [agent.name, agent.model or 'inherit']
            if agent.memory:
                fields.append(agent.memory + ' memory')
            if agent.color:
                fields.append(agent.color)
            shadow = item['overriddenBy']
            prefix = f'(shadowed by {shadow}) ' if shadow else ''
            lines.append('  ' + prefix + ' · '.join(fields))
            total += shadow is None
        lines.append('')
    return f'{total} active agents\n\n' + '\n'.join(lines).rstrip() if lines else 'No agents found.'


def list_in_messages() -> bool:
    # Native host has no GrowthBook gate; explicit opt-in mirrors source default false.
    from misaka.config.product import setting

    return setting("subagents", "agent_list_in_messages", False, bool)


def listing_delta(agents: Mapping[str, AgentDefinition], messages: Sequence[Any]) -> dict[str, Any] | None:
    announced: set[str] = set()
    for message in messages:
        if read_field(message, 'customType') != 'agent_listing_delta':
            continue
        details = read_field(message, 'details') or {}
        announced.update(read_field(details, 'addedTypes', []))
        announced.difference_update(read_field(details, 'removedTypes', []))
    current = {agent.name: agent for agent in agents.values()}
    added = sorted(set(current) - announced)
    removed = sorted(announced - set(current))
    if not added and not removed:
        return None
    parts = []
    if added:
        header = 'Available agent types for the Agent tool:' if not announced else 'New agent types are now available for the Agent tool:'
        parts.append(header + '\n' + roster_text({name: current[name] for name in added}))
    if removed:
        parts.append('The following agent types are no longer available:\n' + '\n'.join('- ' + name for name in removed))
    return {'customType': 'agent_listing_delta', 'display': False,
            'content': '<system-reminder>\n' + '\n\n'.join(parts) + '\n</system-reminder>',
            'details': {'addedTypes': added, 'removedTypes': removed, 'isInitial': not announced}}

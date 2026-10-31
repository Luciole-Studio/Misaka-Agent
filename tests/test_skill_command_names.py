"""Qualified plugin skill names survive command registration, completion and dispatch."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from misaka.core.agent_session import AgentSession
from misaka.core.extensions.runner import ExtensionRunner
from misaka.core.moments import Moments
from misaka.core.skills import index
from misaka.core.skills.layers import extension_roots
from misaka.core.skills.wiring.skills import SkillsPart, _slash_entries
from misaka.modes.interactive.interactive_mode import InteractiveMode

NAME = '<inline:misaka_lcm>:misaka_lcm'
# A bundled extension loads as `builtin:<name>`, and its skills are named by that path the same way.
SOURCES = [('extension:inline:misaka_lcm', NAME, 'inlinemisaka-lcmmisaka-lcm'),
           ('extension:builtin:misaka_lcm', 'builtin:misaka_lcm:misaka_lcm', 'builtinmisaka-lcm:misaka-lcm')]


def _part(entries):
    part = object.__new__(SkillsPart)
    part.session = None
    part._commands = []
    part._refresh_roots = lambda: None
    part._entries = lambda: entries
    part._bundles = dict
    part._read_lock = asyncio.Lock()
    part._run_activation = AsyncMock(return_value='loaded fixture')
    part._session_id = lambda ctx: 'fixture-session'
    return part


def _entries(tmp_path, source='extension:inline:misaka_lcm'):
    skill = tmp_path / 'skills' / 'misaka-lcm' / 'SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('---\nname: misaka-lcm\ndescription: Recover compacted conversation details.\n---\nFixture body.\n', encoding='utf-8')
    roots = list(extension_roots([{'path': str(skill.parent.parent),
                                  'metadata': {'source': source}}]))
    return index._assemble(roots)["entries"]


@pytest.mark.asyncio
@pytest.mark.parametrize('source,name,flattened', SOURCES)
async def test_inline_skill_completion_and_actual_command_dispatch(tmp_path, source, name, flattened):
    entries = _entries(tmp_path, source)
    assert entries[0]['runtime_name'] == name
    part = _part(entries)
    session = object.__new__(AgentSession)
    session._extensionRunner = ExtensionRunner([], SimpleNamespace(), str(tmp_path), SimpleNamespace(), None)
    session._resourceLoader = SimpleNamespace(getPrompts=lambda: {"prompts": []})
    session.moments = Moments(session, [])
    session.moments.parts = [part]
    mode = SimpleNamespace(session=session, sessionManager=SimpleNamespace(getCwd=lambda: str(tmp_path)), fdPath=None)
    mode.getAutocompleteSourceTag = lambda sourceInfo=None: InteractiveMode.getAutocompleteSourceTag(mode, sourceInfo)
    mode.prefixAutocompleteDescription = lambda description, sourceInfo=None: (
        InteractiveMode.prefixAutocompleteDescription(mode, description, sourceInfo)
    )
    provider = InteractiveMode.createBaseAutocompleteProvider(mode)
    from misaka.utils.js import AbortController

    suggestions = await provider.getSuggestions(['/'], 0, 1, {'signal': AbortController().signal})
    item = next(item for item in suggestions.items if item.value == name)
    assert item.label == name
    assert not any(item.value == flattened for item in suggestions.items)
    completed = provider.applyCompletion(['/'], 0, 1, item, suggestions.prefix)
    assert completed['lines'] == ['/' + name + ' ']
    text = completed['lines'][0] + 'inspect evidence'
    command = session.getCorePromptCommand(text)
    assert command is not None
    assert await session._expandCorePromptCommand(command, text) == 'loaded fixture'
    _, table, keys, instruction, _ = part._run_activation.await_args.args
    assert keys == ['/' + name] and instruction == 'inspect evidence'
    assert table[keys[0]]['path'] == entries[0]['path']


@pytest.mark.asyncio
async def test_stacked_inline_skill_preserves_underscores(tmp_path):
    entries = [*_entries(tmp_path), {'name': 'Git_Helper', 'runtime_name': 'Git_Helper'}]
    part = _part(entries)
    command = next(c for c in part.commands if c.name == 'git-helper')
    runner = ExtensionRunner([], SimpleNamespace(), str(tmp_path), SimpleNamespace(), None)
    assert await command.handler('/' + NAME + ' compare evidence', runner.createCommandContext()) == 'loaded fixture'
    _, _, keys, instruction, _ = part._run_activation.await_args.args
    assert keys == ['/git-helper', '/' + NAME] and instruction == 'compare evidence'


def test_qualified_commands_do_not_collide_with_flattened_names(tmp_path):
    entries = [*_entries(tmp_path),
               {'name': 'inlinemisaka-lcmmisaka-lcm'},
               {'name': 'provider:skill_name', 'namespace': 'provider'},
               {'name': 'Git_Helper'}, {'name': 'git-helper'}]
    commands = {c.name for c in _part(entries).commands}
    assert commands == {NAME, 'inlinemisaka-lcmmisaka-lcm', 'provider:skill_name', 'git-helper'}
    assert len(_slash_entries(entries)) == 4  # ordinary Hermes slug collision remains first-wins

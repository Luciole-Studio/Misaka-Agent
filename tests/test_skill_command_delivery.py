"""Skill commands use the real prompt/queue/transcript path; only the model is offline."""
import asyncio
import os
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from misaka.ai.models import get_models
from misaka.ai.types import AssistantMessage, DoneEvent
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.core.auth_storage import AuthStorage
from misaka.core.resource_loader import DefaultResourceLoader
from misaka.core.sdk import create_agent_session
from misaka.core.session_manager import SessionManager
from misaka.core.settings_manager import SettingsManager
from misaka.core.skills.wiring.skills import SkillsPart
from misaka.ui.tui.interactive.interactive_mode import InteractiveMode
from misaka.utils.values import read_field


def text_of(message):
    content = read_field(message, 'content', [])
    return content if isinstance(content, str) else '\n'.join(
        read_field(block, 'text', '') for block in content if read_field(block, 'type') == 'text')


@pytest.fixture
async def host(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith(('MISAKA_', 'HERMES_', 'PI_', 'LCM_')) or key.endswith(('_API_KEY', '_TOKEN')):
            monkeypatch.delenv(key, raising=False)
    for key in ('HOME', 'HERMES_HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME'):
        folder = tmp_path / key
        folder.mkdir()
        monkeypatch.setenv(key, str(folder))
    monkeypatch.chdir(tmp_path)
    from misaka.config import CFG
    monkeypatch.setitem(CFG, 'roles_root', str(tmp_path / 'profiles'))
    monkeypatch.setitem(CFG, 'profiles_root', str(tmp_path / 'profiles' / 'sisters'))

    def no_network(*_args, **_kwargs):
        raise AssertionError('Offline skill test attempted a network connection')
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    monkeypatch.setattr(socket, 'create_connection', no_network)
    profile = tmp_path / 'profile'
    root = profile / 'skills'
    for name in ('agent-reach', 'second'):
        folder = root / name
        folder.mkdir(parents=True)
        (folder / 'SKILL.md').write_text(
            f'---\nname: {name}\ndescription: Fixture skill.\n---\nFixture body for {name}.\n')
    bundles = profile / 'skill-bundles'
    bundles.mkdir()
    (bundles / 'combo.yaml').write_text('name: combo\ndescription: Fixture bundle.\nskills:\n  - agent-reach\n  - second\n')
    part = SkillsPart([('role', str(root))], str(profile), str(tmp_path), kind='bare')
    loader = DefaultResourceLoader({'cwd': str(tmp_path), 'agentDir': str(tmp_path / 'agent'),
        'noExtensions': True, 'noPromptTemplates': True, 'noThemes': True})
    await loader.reload()
    model = next(m for m in get_models('openai') if 'image' in m.input)
    auth = AuthStorage.inMemory()
    auth.setRuntimeApiKey(model.provider, 'fixture')
    result = await create_agent_session({'cwd': str(tmp_path), 'agentDir': str(tmp_path / 'agent'),
        'model': model, 'authStorage': auth, 'resourceLoader': loader,
        'settingsManager': SettingsManager.inMemory({'compaction': {'enabled': False}, 'retry': {'enabled': False}}),
        'sessionManager': SessionManager.create(str(tmp_path), str(tmp_path / 'sessions')),
        'parts': [part], 'tools': []})
    session = result['session']
    calls, errors, inputs = [], [], []
    session.extensionRunner.on_error(lambda error: errors.append(error.error))
    async def observe_input(event, _ctx):
        inputs.append(event)
    part.input = observe_input

    def stream(model, context, options=None):
        assert not part._read_lock.locked(), 'The model started while the skill read lock was held'
        calls.append(context)
        message = AssistantMessage(content=[{'type': 'text', 'text': 'fixture answer'}], api=model.api,
            provider=model.provider, model=model.id, stopReason='stop', timestamp=1,
            usage={'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0, 'totalTokens': 0,
                   'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0, 'total': 0}})
        output = AssistantMessageEventStream()
        output.push(DoneEvent(reason='stop', message=message))
        output.end(message)
        return output
    session.agent.streamFn = stream
    try:
        yield SimpleNamespace(session=session, part=part, calls=calls, errors=errors, inputs=inputs, root=root)
    finally:
        session._isAgentRunActive = False
        session._compactionAbortController = None
        await part.session_shutdown({'reason': 'test'}, session.extensionRunner.create_context())
        session.dispose()


COMMANDS = ['/skill agent-reach inspect', '/agent-reach inspect', '/agent-reach /second inspect', '/combo inspect', '/learn inspect']


@pytest.mark.parametrize('command', COMMANDS)
async def test_command_delivers_once_with_real_context_and_persists(host, command):
    acknowledgements = []
    await host.session.prompt(command, {'preflightResult': acknowledgements.append})
    assert host.errors == []
    assert acknowledgements == [True]
    assert len(host.calls) == 1
    users = [m for m in host.session.state.messages if read_field(m, 'role') == 'user']
    assert len(users) == 1
    assert 'inspect' in text_of(users[0])
    if not command.startswith('/learn'):
        assert 'Fixture body for agent-reach.' in text_of(users[0])
    reopened = SessionManager.open(host.session.sessionManager.getSessionFile())
    persisted = [m for m in reopened.buildSessionContext().messages if read_field(m, 'role') == 'user']
    assert len(persisted) == 1
    assert text_of(persisted[0]) == text_of(users[0])


@pytest.mark.parametrize('behavior', ['steer', 'followUp'])
@pytest.mark.parametrize('entry', ['prompt', 'direct'])
async def test_streaming_skill_preserves_queue_choice(host, behavior, entry):
    host.session._isAgentRunActive = True
    if entry == 'prompt':
        await host.session.prompt('/agent-reach inspect', {'streamingBehavior': behavior})
    else:
        await getattr(host.session, behavior)('/agent-reach inspect')
    expected = host.session.getSteeringMessages() if behavior == 'steer' else host.session.getFollowUpMessages()
    other = host.session.getFollowUpMessages() if behavior == 'steer' else host.session.getSteeringMessages()
    assert len(expected) == 1 and 'Fixture body for agent-reach.' in expected[0]
    assert other == [] and host.calls == [] and host.errors == []
    assert host.session.clearQueue()[('steering' if behavior == 'steer' else 'followUp')] == expected


async def test_skill_preserves_images_and_generated_message_origin(host):
    # A real 1x1 PNG: pi 0.87 resizes prompt attachments to the model's profile before the
    # request, and an undecodable image is dropped with a hint rather than sent.
    image = {'type': 'image', 'mimeType': 'image/png',
             'data': 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='}
    await host.session.prompt('/agent-reach inspect', {'images': [image], 'source': 'interactive'})
    # Keep the generated-message origin: pending /research questions ignore it.
    assert len(host.inputs) == 1 and read_field(host.inputs[0], 'source') == 'extension'
    users = [m for m in host.session.state.messages if read_field(m, 'role') == 'user']
    assert len(users) == 1
    assert any(read_field(b, 'type') == 'image' for b in read_field(users[0], 'content'))
    await host.session.prompt('ordinary message')
    assert read_field(host.inputs[-1], 'source') == 'interactive'


async def test_compaction_rejects_before_loading_and_reports_preflight_failure(host, monkeypatch):
    activation = Mock(wraps=host.part._run_activation)
    monkeypatch.setattr(host.part, '_run_activation', activation)
    host.session._compactionAbortController = object()
    acknowledgements = []
    with pytest.raises(RuntimeError, match='compaction'):
        await host.session.prompt('/agent-reach inspect', {'preflightResult': acknowledgements.append})
    assert acknowledgements == [False]
    activation.assert_not_called()
    assert host.calls == []


def ui_for(session):
    mode = object.__new__(InteractiveMode)
    mode.session = session
    mode.editor = SimpleNamespace(addToHistory=Mock())
    mode.compactionQueuedMessages = []
    mode.deferredInputMessages = []
    mode._backgroundTasks = set()
    mode._set_editor_text = Mock()
    mode.updatePendingMessagesDisplay = Mock()
    mode.showStatus = Mock()
    mode.showError = Mock()
    mode._request_render = Mock()
    mode.flushPendingBashComponents = Mock()
    mode.onInputCallback = Mock()
    return mode


@pytest.mark.parametrize('behavior', ['steer', 'followUp'])
async def test_ui_queues_skill_during_compaction_then_flushes_once(host, behavior):
    mode = ui_for(host.session)
    host.session._compactionAbortController = object()
    if behavior == 'steer':
        await mode.handleSubmittedText('/agent-reach inspect')
    else:
        mode._get_editor_text = lambda: '/agent-reach inspect'
        await mode.handleFollowUp()
    assert mode.compactionQueuedMessages == [{'text': '/agent-reach inspect', 'mode': behavior}]
    assert host.calls == []
    host.session._compactionAbortController = None
    await mode.flushCompactionQueue()
    if mode._backgroundTasks:
        await asyncio.gather(*list(mode._backgroundTasks))
    await asyncio.sleep(0)
    assert len(host.calls) == 1 and mode.compactionQueuedMessages == []
    mode.showError.assert_not_called()


async def test_skill_error_propagates_and_does_not_acknowledge_acceptance(host, monkeypatch):
    async def broken(*_args, **_kwargs):
        raise ValueError('fixture activation failed')
    monkeypatch.setattr(host.part, '_run_activation', broken)
    acknowledgements = []
    with pytest.raises(ValueError, match='fixture activation failed'):
        await host.session.prompt('/agent-reach inspect', {'preflightResult': acknowledgements.append})
    assert acknowledgements == [False] and host.calls == []
    assert not host.part._read_lock.locked()


async def test_cancelled_activation_releases_lock_and_never_sends(host, monkeypatch):
    entered = asyncio.Event()
    async def blocked(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(host.part, '_run_activation', blocked)
    task = asyncio.create_task(host.session.prompt('/agent-reach inspect'))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not host.part._read_lock.locked()
    assert host.calls == []


async def test_listing_and_management_commands_do_not_start_model(host):
    await host.session.prompt('/skill')
    await host.session.prompt('/reload-skills')
    assert host.calls == [] and host.errors == []


async def test_compaction_failure_retains_original_input_for_retry(host, monkeypatch):
    mode = ui_for(host.session)
    original = '/agent-reach inspect'
    mode.compactionQueuedMessages = [{'text': original, 'mode': 'steer'}]
    activate = host.part._run_activation
    async def fail(*_args, **_kwargs):
        raise ValueError('fixture unavailable')
    monkeypatch.setattr(host.part, '_run_activation', fail)
    await mode.flushCompactionQueue()
    await asyncio.gather(*list(mode._backgroundTasks), return_exceptions=True)
    await asyncio.sleep(0)
    assert mode.compactionQueuedMessages == [{'text': original, 'mode': 'steer'}]
    assert host.calls == []
    assert 'fixture unavailable' in mode.showError.call_args.args[0]
    monkeypatch.setattr(host.part, '_run_activation', activate)
    await mode.flushCompactionQueue()
    await asyncio.gather(*list(mode._backgroundTasks))
    await asyncio.sleep(0)
    assert len(host.calls) == 1 and mode.compactionQueuedMessages == []


async def test_streaming_failure_is_visible_and_original_input_stays_in_editor_history(host, monkeypatch):
    mode = ui_for(host.session)
    host.session._isAgentRunActive = True
    async def fail(*_args, **_kwargs):
        raise ValueError('fixture unavailable')
    monkeypatch.setattr(host.part, '_run_activation', fail)
    mode._schedule_task(mode.handleSubmittedText('/agent-reach inspect'))
    await asyncio.gather(*list(mode._backgroundTasks), return_exceptions=True)
    await asyncio.sleep(0)
    mode.showError.assert_called_once_with('fixture unavailable')
    mode.editor.addToHistory.assert_called_once_with('/agent-reach inspect')
    assert host.session.getSteeringMessages() == [] and host.calls == []


async def test_owned_activation_worker_is_drained_before_cancellation_returns(host, monkeypatch):
    import threading
    entered, drained = threading.Event(), threading.Event()
    def _stack_message(*_args, cancelled=None):
        entered.set()
        try:
            assert cancelled.wait(3), 'The owned worker was not cancelled'
            return 'late result must not be submitted'
        finally:
            drained.set()
    monkeypatch.setattr(host.part, '_stack_message', _stack_message)
    task = asyncio.create_task(host.session.prompt('/agent-reach inspect'))
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert drained.is_set() and not host.part._read_lock.locked()
    assert host.calls == []


async def test_input_interception_and_expansion_opt_out_remain_effective(host, monkeypatch):
    async def handled(event, _ctx):
        return {'action': 'handled'}
    monkeypatch.setattr(host.part, 'input', handled)
    await host.session.prompt('/agent-reach inspect')
    assert host.calls == []
    monkeypatch.setattr(host.part, 'input', lambda *_: None)
    activation = Mock(wraps=host.part._run_activation)
    monkeypatch.setattr(host.part, '_run_activation', activation)
    await host.session.prompt('/agent-reach literal', {'expandPromptTemplates': False})
    activation.assert_not_called()
    users = [m for m in host.session.state.messages if read_field(m, 'role') == 'user']
    assert text_of(users[-1]) == '/agent-reach literal'


async def test_render_and_history_rebuild_do_not_reactivate_or_mutate_skill(host, monkeypatch):
    from copy import deepcopy

    from misaka.ui.tui import Container
    from misaka.ui.tui.interactive.components.skill_invocation_message import (
        SkillInvocationMessageComponent,
    )
    from misaka.ui.tui.interactive.components.transcript import TranscriptRenderer
    activation = Mock(wraps=host.part._run_activation)
    monkeypatch.setattr(host.part, '_run_activation', activation)
    await host.session.prompt('/agent-reach inspect')
    users = [m for m in host.session.state.messages if read_field(m, 'role') == 'user']
    before = deepcopy(users)
    for expanded in (False, True, False):
        container = Container()
        TranscriptRenderer(ui=None, cwd=host.session.sessionManager.getCwd(), expanded=expanded).addMessage(container, users[0])
        assert sum(isinstance(child, SkillInvocationMessageComponent) for child in container.children) == 1
        rendered = '\n'.join(container.render(90))
        assert 'agent-reach' in rendered and 'inspect' in rendered
    assert users == before and activation.call_count == 1


async def test_skill_does_not_consume_pending_research_question(host, monkeypatch, tmp_path):
    from misaka.core.platform import tasks
    from misaka.core.research.wiring import research
    connection = tasks.connect(str(tmp_path / 'research.db'))
    research.runs.init(connection)
    monkeypatch.setattr(research, '_con', lambda: connection)
    part = research.ResearchPart()
    part.attach(host.session)
    host.session.moments.parts.append(part)
    notifications = []
    host.session.extensionRunner.set_ui_context(SimpleNamespace(notify=lambda *args: notifications.append(args)))
    try:
        await host.session.prompt('/research 2')
        await host.session.prompt('/agent-reach inspect')
        assert len(host.calls) == 1
        await host.session.prompt('/research status')
        assert 'waiting for a question' in notifications[-1][0]
        assert host.errors == []
    finally:
        await part.session_shutdown({'reason': 'test'}, host.session.extensionRunner.create_context())
        connection.close()


async def test_non_prompt_core_and_extension_commands_keep_their_dispatch(host):
    from misaka.core.extensions.types import Extension, RegisteredCommand
    from misaka.core.moments import CoreCommand
    from misaka.core.source_info import create_synthetic_source_info
    calls = []
    async def command(args, ctx):
        calls.append((args, type(ctx).__name__))
    host.part._commands.append(CoreCommand('control-fixture', 'fixture', command))
    extension = Extension(path='fixture', resolvedPath='fixture', sourceInfo=create_synthetic_source_info('fixture', {'source': 'sdk'}))
    extension.commands['extension-fixture'] = RegisteredCommand(name='extension-fixture', sourceInfo=extension.sourceInfo, description='fixture', handler=command)
    host.session.extensionRunner.extensions.append(extension)
    await host.session.prompt('/control-fixture core')
    await host.session.prompt('/extension-fixture extension')
    assert calls == [('core', '_CommandContextView'), ('extension', '_CommandContextView')]
    assert host.calls == []


async def test_compaction_starting_during_skill_preparation_prevents_submission(host, monkeypatch):
    activate = host.part._run_activation
    async def compact_mid_preparation(*args, **kwargs):
        result = await activate(*args, **kwargs)
        host.session._compactionAbortController = object()
        return result
    monkeypatch.setattr(host.part, '_run_activation', compact_mid_preparation)
    with pytest.raises(RuntimeError, match='compaction'):
        await host.session.prompt('/agent-reach inspect')
    assert host.calls == []


async def test_compaction_batch_finishes_after_first_fast_turn(host):
    mode = ui_for(host.session)
    mode.compactionQueuedMessages = [
        {'text': '/agent-reach first', 'mode': 'steer'},
        {'text': '/second next', 'mode': 'followUp'},
    ]
    await mode.flushCompactionQueue()
    await asyncio.gather(*list(mode._backgroundTasks))
    await asyncio.sleep(0)
    users = [m for m in host.session.state.messages if read_field(m, 'role') == 'user']
    assert len(users) == 2
    assert 'agent-reach' in text_of(users[0]) and 'second' in text_of(users[1])
    assert not host.session.getFollowUpMessages() and not host.session.getSteeringMessages()
    assert mode.compactionQueuedMessages == []


async def test_compaction_batch_failure_does_not_restore_an_accepted_message(host, monkeypatch):
    mode = ui_for(host.session)
    activate = host.part._run_activation
    async def fail_second(function, table, keys, *args, **kwargs):
        if keys == ['/second']:
            await asyncio.sleep(0)
            raise ValueError('second fixture unavailable')
        return await activate(function, table, keys, *args, **kwargs)
    monkeypatch.setattr(host.part, '_run_activation', fail_second)
    mode.compactionQueuedMessages = [
        {'text': '/agent-reach first', 'mode': 'steer'},
        {'text': '/second next', 'mode': 'followUp'},
    ]
    await mode.flushCompactionQueue()
    await asyncio.gather(*list(mode._backgroundTasks), return_exceptions=True)
    await asyncio.sleep(0)
    assert mode.compactionQueuedMessages == [{'text': '/second next', 'mode': 'followUp'}]
    assert 'second fixture unavailable' in mode.showError.call_args.args[0]


async def test_retry_flush_preserves_accepted_queue_when_later_skill_fails(host, monkeypatch):
    mode = ui_for(host.session)
    host.session._isAgentRunActive = True
    activate = host.part._run_activation
    async def fail_second(function, table, keys, *args, **kwargs):
        if keys == ['/second']:
            raise ValueError('second unavailable')
        return await activate(function, table, keys, *args, **kwargs)
    monkeypatch.setattr(host.part, '_run_activation', fail_second)
    mode.compactionQueuedMessages = [
        {'text': '/agent-reach first', 'mode': 'steer'},
        {'text': '/second next', 'mode': 'followUp'},
    ]
    await mode.flushCompactionQueue({'willRetry': True})
    assert len(host.session.getSteeringMessages()) == 1
    assert 'Fixture body for agent-reach.' in host.session.getSteeringMessages()[0]
    assert mode.compactionQueuedMessages == [{'text': '/second next', 'mode': 'followUp'}]
    assert host.calls == []


async def test_failed_first_compaction_prompt_does_not_dispatch_later_inputs(host, monkeypatch):
    mode = ui_for(host.session)
    activations = []
    async def fail(*args, **_kwargs):
        activations.append(args)
        raise ValueError('first unavailable')
    monkeypatch.setattr(host.part, '_run_activation', fail)
    queued = [{'text': '/agent-reach first', 'mode': 'steer'}, {'text': '/second next', 'mode': 'followUp'}]
    mode.compactionQueuedMessages = list(queued)
    await mode.flushCompactionQueue()
    await asyncio.sleep(0)
    assert mode.compactionQueuedMessages == queued
    assert len(activations) == 1 and host.calls == []


async def test_cancelled_compaction_flush_drains_preparation_and_restores_input(host, monkeypatch):
    mode = ui_for(host.session)
    entered, drained = asyncio.Event(), asyncio.Event()
    async def blocked(*_args, **_kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()
    monkeypatch.setattr(host.part, '_run_activation', blocked)
    queued = [{'text': '/agent-reach inspect', 'mode': 'steer'}]
    mode.compactionQueuedMessages = list(queued)
    task = asyncio.create_task(mode.flushCompactionQueue())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert drained.is_set() and not host.part._read_lock.locked()
    assert mode.compactionQueuedMessages == queued
    assert not mode._backgroundTasks and host.calls == []


async def test_compaction_starting_in_preflight_does_not_persist_a_skill_message(host, monkeypatch):
    prepare = host.session._prepare_agent_start
    async def compact_during_preflight(*args):
        result = await prepare(*args)
        host.session._compactionAbortController = object()
        return result
    monkeypatch.setattr(host.session, '_prepare_agent_start', compact_during_preflight)
    accepted = []
    with pytest.raises(RuntimeError, match='compaction'):
        await host.session.prompt('/agent-reach inspect', {'preflightResult': accepted.append})
    assert accepted == [False] and host.calls == []
    assert not any(read_field(m, 'role') == 'user' for m in host.session.state.messages)


@pytest.mark.parametrize('value', ['', None, {'bad': 'shape'}])
async def test_core_prompt_result_contract_preserves_empty_text(host, value):
    from misaka.core.moments import CoreCommand
    host.part._commands.append(CoreCommand('prompt-fixture', 'fixture', lambda *_: value, is_prompt=True))
    if isinstance(value, dict):
        with pytest.raises(TypeError, match='must return text or None'):
            await host.session.prompt('/prompt-fixture')
    else:
        await host.session.prompt('/prompt-fixture')
        assert len(host.calls) == (1 if value == '' else 0)


async def test_skill_view_does_not_submit_a_new_turn(host):
    view = next(tool for tool in host.part.tools if tool.name == 'skill_view')
    result = await view.execute('fixture', {'name': 'agent-reach'}, None, None, host.session.extensionRunner.create_context())
    assert not result.get('isError')
    assert 'Fixture body for agent-reach.' in result['content'][0]['text']
    assert host.calls == []


async def test_missing_skill_is_an_error_even_without_ui(host):
    accepted = []
    with pytest.raises(ValueError):
        await host.session.prompt('/skill missing-fixture', {'preflightResult': accepted.append})
    assert accepted == [False] and host.calls == []


@pytest.mark.parametrize('command', ['/agent-reach inspect', '/combo inspect'])
async def test_skill_disappearing_during_activation_is_not_acknowledged(host, monkeypatch, command):
    async def gone(*_args, **_kwargs):
        return None
    monkeypatch.setattr(host.part, '_run_activation', gone)
    accepted = []
    with pytest.raises(ValueError, match='no longer|no available'):
        await host.session.prompt(command, {'preflightResult': accepted.append})
    assert accepted == [False] and host.calls == []


async def test_learn_gate_remains_closed_and_failure_is_visible_without_ui(host, monkeypatch):
    from misaka.core.skills import write
    monkeypatch.setattr(write, 'evaluate_gate', lambda: ('off', ''))
    accepted = []
    with pytest.raises(ValueError, match='disabled globally'):
        await host.session.prompt('/learn inspect', {'preflightResult': accepted.append})
    assert accepted == [False] and host.calls == []


@pytest.mark.parametrize('will_retry', [False, True])
async def test_turn_ending_during_queued_skill_preparation_does_not_strand_message(host, monkeypatch, will_retry):
    stream_started, preparing = asyncio.Event(), asyncio.Event()
    stream = host.session.agent.streamFn
    async def held_stream(*args, **kwargs):
        stream_started.set()
        await preparing.wait()
        return stream(*args, **kwargs)
    monkeypatch.setattr(host.session.agent, 'streamFn', held_stream)
    first_turn = asyncio.create_task(host.session.prompt('original turn'))
    await stream_started.wait()
    activate = host.part._run_activation
    async def slow_activation(*args, **kwargs):
        preparing.set()
        await first_turn
        return await activate(*args, **kwargs)
    monkeypatch.setattr(host.part, '_run_activation', slow_activation)
    mode = ui_for(host.session)
    mode.compactionQueuedMessages = [{'text': '/agent-reach next', 'mode': 'followUp'}]
    await mode.flushCompactionQueue({'willRetry': will_retry})
    await asyncio.gather(*list(mode._backgroundTasks))
    users = [m for m in host.session.state.messages if read_field(m, 'role') == 'user']
    assert len(users) == 2 and 'Fixture body for agent-reach.' in text_of(users[-1])
    assert not host.session.getFollowUpMessages() and not mode.compactionQueuedMessages


@pytest.mark.parametrize('fail_preflight', [False, True])
async def test_skill_preflight_keeps_unaccepted_next_turn_notifications(host, monkeypatch, fail_preflight):
    await host.session.sendCustomMessage({'customType': 'fixture', 'content': 'before'}, {'deliverAs': 'nextTurn'})
    prepare = host.session._prepare_agent_start
    async def notification_during_preflight(*args):
        result = await prepare(*args)
        await host.session.sendCustomMessage({'customType': 'fixture', 'content': 'during'}, {'deliverAs': 'nextTurn'})
        if fail_preflight:
            host.session._compactionAbortController = object()
        return result
    monkeypatch.setattr(host.session, '_prepare_agent_start', notification_during_preflight)
    if fail_preflight:
        with pytest.raises(RuntimeError, match='compaction'):
            await host.session.prompt('/agent-reach inspect')
    else:
        await host.session.prompt('/agent-reach inspect')
    assert [text_of(m) for m in host.session._pendingNextTurnMessages] == (
        ['before', 'during'] if fail_preflight else ['during'])

"""Live previews come from native memory, not partially committed transcripts."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_skill_command_delivery import (
    host as host,  # noqa: PLC0414 - explicit offline fixture re-export
)

from misaka.agent.agent import Agent
from misaka.core.session_control import SessionControl, request
from misaka.core.session_manager import SessionManager


def assistant(text):
    return {'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': 'checking'},
                                           {'type': 'text', 'text': text}],
            'timestamp': 1, 'stopReason': 'stop', 'api': 'anthropic-messages',
            'provider': 'fixture', 'model': 'fixture',
            'usage': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0, 'totalTokens': 0,
                      'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0, 'total': 0}}}


def fixture_session(tmp_path):
    manager = SessionManager.create(str(tmp_path), str(tmp_path / 'sessions'))
    manager.appendMessage({'role': 'user', 'content': 'question', 'timestamp': 0})
    manager.appendMessage(assistant('saved answer'))
    agent, listeners = Agent(), []

    def subscribe(callback):
        listeners.append(callback)
        return lambda: listeners.remove(callback)

    session = SimpleNamespace(sessionManager=manager, sessionId=manager.sessionId,
                              agent=agent, state=agent.state, isIdle=False,
                              getSteeringMessages=list, getFollowUpMessages=list, subscribe=subscribe)

    def emit(kind, message=None):
        event = SimpleNamespace(type=kind, message=message)
        agent._reduce_state(event)  # Native source of streamingMessage; no parallel buffer.
        for callback in tuple(listeners):
            callback(event)

    return session, emit, listeners


async def snapshot(control, previous=None):
    return await control._execute({'operation': 'snapshot',
                                  'cursor': previous['cursor'] if previous else None,
                                  'stream_cursor': previous['stream_cursor'] if previous else None})


async def test_stream_updates_without_replaying_or_writing_history(tmp_path):
    session, emit, listeners = fixture_session(tmp_path)
    control = SessionControl(session, None)
    path = Path(session.sessionManager.getSessionFile())
    before = path.read_bytes()
    await control.start()
    try:
        first = await snapshot(control)
        emit('message_start', assistant('par'))
        live = await snapshot(control, first)
        assert live['entries'] is None and live['cursor'] == first['cursor']
        assert live['stream_cursor'] != first['stream_cursor']
        assert live['streaming'] is session.state.streamingMessage
        assert live['streaming']['content'][0]['thinking'] == 'checking'
        unchanged = await snapshot(control, live)
        assert unchanged['stream_cursor'] == live['stream_cursor']
        assert unchanged['streaming'] is None and unchanged['entries'] is None
        emit('message_update', assistant('partial 中文'))
        updated = await snapshot(control, live)
        assert updated['stream_cursor'] != live['stream_cursor']
        assert updated['streaming']['content'][1]['text'] == 'partial 中文'
        assert updated['entries'] is None
        assert path.read_bytes() == before
        assert 'partial' not in path.read_text()
        emit('message_end', assistant('final answer'))
        session.sessionManager.appendMessage(assistant('final answer'))
        final = await snapshot(control, updated)
        assert final['streaming'] is None and final['stream_cursor'] != updated['stream_cursor']
        assert len(final['entries']) == len(first['entries']) + 1
        assert sum('final answer' in str(entry) for entry in final['entries']) == 1
    finally:
        control.close()
    assert listeners == []


@pytest.mark.parametrize('end', ['agent_end', 'reset', 'message_end'])
async def test_abort_reset_or_end_explicitly_clears_preview(tmp_path, end):
    session, emit, _ = fixture_session(tmp_path)
    control = SessionControl(session, None)
    await control.start()
    try:
        emit('message_start', assistant('partial'))
        live = await snapshot(control)
        if end == 'reset':
            session.agent.reset()
            session.sessionManager.resetLeaf()
        else:
            emit(end, assistant('stopped'))
        cleared = await snapshot(control, live)
        assert cleared['stream_cursor'] != live['stream_cursor']
        assert cleared['streaming'] is None
    finally:
        control.close()


async def test_reconnect_seeds_existing_stream_and_lifecycle_has_one_subscription(tmp_path):
    session, emit, listeners = fixture_session(tmp_path)
    emit('message_start', assistant('already generating'))
    control = SessionControl(session, None)
    await control.start()
    await control.start()
    try:
        assert len(listeners) == 1
        initial = await snapshot(control)
        assert initial['streaming']['content'][1]['text'] == 'already generating'
        # Native bookkeeping changes history while the same assistant keeps streaming.
        session.sessionManager.appendCustomEntry('progress', {'value': 1})
        updated = await snapshot(control, initial)
        assert updated['entries'] is not None
        assert updated['stream_cursor'] != initial['stream_cursor']
        assert updated['streaming'] is session.state.streamingMessage
    finally:
        control.close()
        control.close()
    assert listeners == []
    await control.start()
    assert len(listeners) == 1
    control.close()
    assert not listeners


async def test_socket_serializes_preview_and_still_rejects_wrong_owner(tmp_path):
    session, emit, _ = fixture_session(tmp_path)
    record = {'id': session.sessionId, 'instance': 'owner'}
    path = tmp_path / 'catalog.json'
    path.write_text(json.dumps(record))
    catalog = SimpleNamespace(record=str(path), instance='owner', spec=None)
    control = SessionControl(session, catalog)
    await control.start()
    record['control'] = control.path
    try:
        emit('message_update', assistant('socket preview'))
        wire = await request(record, 'snapshot')
        assert wire['streaming']['content'][1]['text'] == 'socket preview'
        with pytest.raises(ValueError, match='changed owners'):
            await request({**record, 'instance': 'different'}, 'snapshot')
    finally:
        control.close()


async def test_real_agent_session_publishes_native_stream_and_commits_once(host):
    import asyncio

    from misaka.ai.types import AssistantMessage, DoneEvent, StartEvent, TextDeltaEvent
    from misaka.ai.utils.event_stream import AssistantMessageEventStream
    from misaka.utils.values import read_field

    session = host.session
    control = SessionControl(session, None)
    output = AssistantMessageEventStream()
    partial = AssistantMessage(**{**assistant('NATIVE_PARTIAL'), 'api': session.model.api,
                                  'provider': session.model.provider, 'model': session.model.id})
    started, updated = asyncio.Event(), asyncio.Event()

    def observe(event):
        if read_field(event, 'type') == 'message_start' and read_field(read_field(event, 'message'), 'role') == 'assistant':
            started.set()
        if read_field(event, 'type') == 'message_update':
            updated.set()

    unsubscribe = session.subscribe(observe)
    session.agent.streamFn = lambda *_args, **_kwargs: output
    await control.start()
    turn = asyncio.create_task(session.prompt('native fixture question'))
    try:
        output.push(StartEvent(partial=partial))
        await asyncio.wait_for(started.wait(), 3)
        first = await snapshot(control)
        assert first['streaming'].content[1].text == 'NATIVE_PARTIAL'
        assert 'NATIVE_PARTIAL' not in str(session.sessionManager.getEntries())
        final = partial.model_copy(deep=True)
        final.content[1].text = 'NATIVE_FINAL'
        output.push(TextDeltaEvent(contentIndex=1, delta='_FINAL', partial=final))
        await asyncio.wait_for(updated.wait(), 3)
        second = await snapshot(control, first)
        assert second['entries'] is None and second['stream_cursor'] != first['stream_cursor']
        assert second['streaming'].content[1].text == 'NATIVE_FINAL'
        output.push(DoneEvent(reason='stop', message=final))
        output.end(final)
        await asyncio.wait_for(turn, 3)
        done = await snapshot(control, second)
        assert done['streaming'] is None
        assert sum('NATIVE_FINAL' in str(e) for e in done['entries']) == 1
        reopened = SessionManager.open(session.sessionFile)
        assert sum('NATIVE_FINAL' in str(e) for e in reopened.getEntries()) == 1
        assert host.errors == []
    finally:
        if not turn.done():
            turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        unsubscribe()
        control.close()

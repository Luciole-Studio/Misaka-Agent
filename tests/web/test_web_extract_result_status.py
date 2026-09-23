"""MISAKA's result envelope, not Hermes' extraction algorithm, owns tool status."""
import json
from types import SimpleNamespace

import pytest
from test_web_hermes_origin import _tool_batch
from test_web_hermes_origin import (
    scope as scope,  # noqa: PLC0414 - expose shared pytest fixtures
)
from test_web_hermes_origin import (
    web_session as web_session,  # noqa: PLC0414 - expose shared pytest fixtures
)

from misaka.ai.types import ToolCall
from misaka.core.platform.prompt_guard import untrusted
from misaka.core.web import extract
from misaka.core.wiring import ToolCollector

CASES = [
    pytest.param({'results': [{'content': 'Page text', 'error': None}]}, False, [], id='error-null'),
    pytest.param({'results': [{'content': '"error" and "failed" are document words'}]}, False, [], id='error-prose'),
    pytest.param({'results': [{'content': '', 'error': 'timeout'}, {'content': '', 'error': '403'}]}, True, [], id='all-failed'),
    pytest.param({'results': [{'content': 'Page text'}, {'error': 'timeout'}]}, False, [], id='partial-success'),
    pytest.param({'results': [{'content': 'error response', 'error': '403'}]}, True, [], id='error-content'),
    pytest.param({'results': [{'content': ''}]}, True, [], id='empty-content'),
    pytest.param({'results': [{'content': ' \n\t'}]}, True, [], id='whitespace-content'),
    pytest.param({'results': [{'content': '', 'saved_path': 'downloads/page.md'}]}, False, ['downloads/page.md'], id='saved-only'),
    pytest.param({'results': [{'error': 'preview failed', 'saved_path': 'downloads/page.md'}]}, False, ['downloads/page.md'], id='saved-with-error'),
    pytest.param({'results': [{'content': 'Page text', 'saved_path': 'downloads/page.md'}, {'saved_path': 'downloads/page.md'}]}, False, ['downloads/page.md'], id='deduplicated-path'),
    pytest.param({'results': [{'content': 'Page text', 'saved_path': {'invalid': 'path'}}]}, False, [], id='invalid-path-valid-content'),
    pytest.param({'results': [{'content': ['invalid']} ]}, True, [], id='invalid-content'),
    pytest.param({'results': [{'saved_path': ['invalid']}]}, True, [], id='invalid-path'),
    pytest.param({'results': [{'saved_path': ' \t'}]}, True, [], id='blank-path'),
    pytest.param({'results': [None, 1, 'invalid', {'content': 'Page text'}]}, False, [], id='mixed-result-shape'),
    pytest.param({'results': [None, 1, 'invalid', {}]}, True, [], id='no-valid-entry'),
    pytest.param({'results': []}, True, [], id='empty-results'),
    pytest.param({'success': True, 'results': []}, True, [], id='empty-success'),
    pytest.param({'results': {}}, True, [], id='invalid-results'),
    pytest.param({'results': 'invalid'}, True, [], id='string-results'),
    pytest.param({'success': True}, True, [], id='missing-results'),
    pytest.param({'success': False, 'error': 'global failure'}, True, [], id='top-level-error'),
    pytest.param({'success': False, 'results': [{'content': 'Page text'}]}, True, [], id='top-level-failure-priority'),
    pytest.param([], True, [], id='array-root'),
    pytest.param(None, True, [], id='null-root'),
    pytest.param('not JSON', True, [], id='invalid-json'),
]


def _reply(monkeypatch, payload):
    raw = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)

    async def respond(*_args, **_kwargs):
        return raw

    monkeypatch.setattr(extract, 'web_extract_tool', respond)
    return raw


@pytest.mark.parametrize('payload,failure,paths', CASES)
async def test_extract_adapter_classifies_without_changing_body(scope, monkeypatch, payload, failure, paths):
    raw = _reply(monkeypatch, payload)
    collector = ToolCollector()
    extract.register(collector, str(scope))
    result = await collector.tools[0].execute('extract-status', {'urls': ['https://page.example/']}, None, None, None)
    assert result['isError'] is failure
    assert result['content'] == [{'type': 'text', 'text': untrusted('web-extract', raw)}]
    assert result['details'] == {'saved_paths': paths}


@pytest.mark.parametrize('payload,failure,paths', CASES)
async def test_extract_status_reaches_native_host(web_session, monkeypatch, payload, failure, paths):
    raw = _reply(monkeypatch, payload)
    batch, events = await _tool_batch(web_session, [ToolCall(
        id='extract-status', name='web_extract', arguments={'urls': ['https://page.example/']})])
    result = batch.messages[0]
    assert result.isError is failure
    assert result.content[0].text == untrusted('web-extract', raw)
    assert [event.isError for event in events if event.type == 'tool_execution_end'] == [failure]
    assert batch.terminate is False
    assert result.details == ({} if failure else {'saved_paths': paths})


@pytest.mark.parametrize('saved_only', [False, True])
async def test_partial_success_keeps_card_artifact_and_research_source(web_session, scope, monkeypatch, saved_only):
    from misaka.core.network.todo import TodoPart
    from misaka.core.platform import cards, tasks
    from misaka.core.research.bundle import _Index, consulted_in_session

    saved = scope / 'downloads/page.md'
    saved.parent.mkdir()
    saved.write_text('# A saved source\nVerified page text.\n')
    _reply(monkeypatch, {'results': [
        {'content': '' if saved_only else 'Verified page text.', 'saved_path': 'downloads/page.md', 'error': None},
        {'content': '', 'error': 'timeout'},
    ]})
    con = tasks.connect(str(scope / 'board.db'))
    tid = cards.create(con, str(scope), 'Fixture research card', '## deliverable\nreport.md\n', '10032')
    con.execute("UPDATE tasks SET status='running', claim_lock='fixture', output_dir=? WHERE id=?",
                (str(scope / 'outputs'), tid))
    row = tasks.get(con, tid)
    for name in ('MISAKA_SISTER_OWNER_TASK_ID', 'MISAKA_SISTER_OWNER_GENERATION', 'MISAKA_SISTER_OWNER_CLAIM_LOCK'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('MISAKA_USAGE_TASK_ID', tid)
    monkeypatch.setenv('MISAKA_USAGE_CLAIM_LOCK', 'fixture')
    monkeypatch.setenv('MISAKA_USAGE_GENERATION', str(row['generation']))
    todo = TodoPart(tid, '10032')
    todo._con = con
    todo.session = SimpleNamespace(cwd=str(scope), moments=SimpleNamespace(send_message=lambda *_: None))
    try:
        batch, _ = await _tool_batch(web_session, [ToolCall(
            id='extract-material', name='web_extract', arguments={'urls': ['https://page.example/']})])
        result = batch.messages[0]
        assert result.isError is False
        await todo.tool_result({'toolName': 'web_extract', 'isError': result.isError, 'details': result.details})
        assert json.loads(tasks.latest_payload(con, tid, 'artifact_written')) == {'path': 'downloads/page.md'}
        assert tasks.get(con, tid)['status'] == 'running'
        transcript = scope / 'session.jsonl'
        transcript.write_text(json.dumps({'message': result.model_dump()}) + '\n')
        assert consulted_in_session(str(transcript), _Index(str(scope))) == {str(saved.resolve())}
    finally:
        await todo.session_shutdown()
        con.close()

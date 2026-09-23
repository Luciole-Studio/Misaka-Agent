"""Only text headings belong in artifact previews, never binary payloads. Offline fixtures."""
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from misaka import workspace
from misaka.core.platform import tasks
from misaka.core.research import runs, tools, workflow


def artifact(tmp_path, suffix='.md', content=b'# Heading\n', metadata=None):
    path = tmp_path / ('artifact' + suffix)
    path.write_bytes(content)
    return {'id': 'a_fixture', 'path': str(path), 'title': 'Fixture', 'kind': 'task_output',
            'metadata_json': json.dumps(metadata or {})}


@pytest.mark.parametrize('suffix,metadata', [('.png', {}), ('.pdf', {}), ('.csv', {}),
                                            ('.txt', {}), ('.docx', {}),
                                            ('.json', {}), ('.md', {'binary': True})])
def test_non_markdown_and_binary_files_are_never_opened(tmp_path, monkeypatch, suffix, metadata):
    row = artifact(tmp_path, suffix, b'# BINARY PAYLOAD\xc2\x85\n', metadata)

    def forbidden(*args, **kwargs):
        pytest.fail('Navigation must not open binary/non-Markdown payloads')

    monkeypatch.setattr(workspace, 'open', forbidden, raising=False)
    node = workspace._artifact_node(row)
    assert node == {'node_id': 'artifact:a_fixture', 'title': 'Fixture',
                    'summary': 'task_output', 'path': row['path'], 'nodes': []}


@pytest.mark.parametrize('content', [b'# partial\n\xffbad', b'# partial\n\0bad',
                                      b'# partial\n\x01bad', b'\x89PNG\r\n\x1a\n# fake'])
def test_mislabeled_binary_markdown_has_no_partial_headings(tmp_path, content):
    assert workspace._artifact_node(artifact(tmp_path, content=content))['nodes'] == []


@pytest.mark.parametrize('suffix', ['.md', '.MD', '.markdown', '.MARKDOWN'])
def test_markdown_line_locators_and_unicode_are_preserved(tmp_path, suffix):
    content = '# 中文\r\nbody\r\n  ## alpha\u0085beta\u2028gamma\u2029delta\n### last'
    node = workspace._artifact_node(artifact(tmp_path, suffix, content.encode()))
    assert [(h['node_id'], h['title']) for h in node['nodes']] == [
        ('artifact:a_fixture#L1', '中文'),
        ('artifact:a_fixture#L3', 'alpha\u0085beta\u2028gamma\u2029delta'),
        ('artifact:a_fixture#L4', 'last')]


@pytest.mark.parametrize('metadata_json', ['{broken', '[]', 'null'])
def test_missing_file_and_invalid_metadata_leave_file_navigable(tmp_path, metadata_json):
    row = artifact(tmp_path)
    row['metadata_json'] = metadata_json
    assert workspace._artifact_node(row)['nodes'] == []
    row['metadata_json'] = '{}'
    Path(row['path']).unlink()
    node = workspace._artifact_node(row)
    assert node['path'] == row['path'] and node['nodes'] == []


def test_heading_count_and_title_length_are_bounded(tmp_path):
    text = ''.join(f'# {n} ' + 'x' * 1000 + '\n' for n in range(130))
    node = workspace._artifact_node(artifact(tmp_path, content=text.encode()))
    assert len(node['nodes']) == 120
    assert max(len(h['title']) for h in node['nodes']) <= 200
    assert node['nodes'][-1]['node_id'] == 'artifact:a_fixture#L120'
    assert 'truncated' in node['summary']


@pytest.mark.parametrize('count', [120, 121])
def test_heading_count_limit_marks_only_actual_truncation(tmp_path, count):
    text = ''.join(f'# Heading {n}\n' for n in range(count))
    node = workspace._artifact_node(artifact(tmp_path, content=text.encode()))
    assert len(node['nodes']) == min(count, 120)
    assert ('truncated' in node['summary']) == (count > 120)


@pytest.mark.parametrize('length', [200, 201])
@pytest.mark.parametrize('char', ['x', '中'])
def test_heading_title_limit_marks_only_actual_truncation(tmp_path, length, char):
    title = char * length
    node = workspace._artifact_node(artifact(tmp_path, content=('# ' + title).encode()))
    assert node['nodes'][0]['title'] == (title if length == 200 else char * 199 + '…')
    assert ('truncated' in node['summary']) == (length > 200)


def test_scan_is_bounded_and_never_emits_a_cut_line(tmp_path, monkeypatch):
    row = artifact(tmp_path)
    requested = []

    class BoundedReader(io.StringIO):
        def read(self, size=-1):
            assert 0 < size <= 256 * 1024 + 1
            requested.append(size)
            return super().read(size)

        def __next__(self):
            pytest.fail('Unbounded line iteration may allocate an arbitrarily large line')

    monkeypatch.setattr(workspace, 'open', lambda *a, **k: BoundedReader('# first\n# ' + 'x' * 400000), raising=False)
    node = workspace._artifact_node(row)
    assert requested and [x['title'] for x in node['nodes']] == ['first']
    assert 'truncated' in node['summary']


def test_unicode_at_scan_boundary_does_not_break_decoding(tmp_path):
    # Text-mode reading counts characters, not bytes; don't produce a clipped heading.
    text = '# first\n' + '中' * (256 * 1024 - 12) + '\n# 中文 heading beyond the limit\n'
    node = workspace._artifact_node(artifact(tmp_path, content=text.encode()))
    assert [h['title'] for h in node['nodes']] == ['first']
    assert 'truncated' in node['summary']


async def test_both_live_view_and_saved_index_preserve_binary_paths_without_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, '_commit', lambda *a, **k: None)
    monkeypatch.setattr(workspace.corpus, 'docs', lambda **k: [])
    con = tasks.connect(str(tmp_path / 'board.db'))
    try:
        run = runs.create(con, workspace=str(tmp_path), question='fixture')
        child = runs.create_node(con, run['id'], parent_id=runs.nodes(con, run['id'])[0]['id'],
                                 trigger='child', depth=1)
        for suffix,branch in [('.png', None), ('.pdf', child['id']), ('.md', None)]:
            row = artifact(tmp_path, suffix, b'# BINARY PAYLOAD\xc2\x85\n' if suffix != '.md' else b'# Real heading\n')
            runs.register_file(con, run['id'], 'task_output', 'Fixture '+suffix, row['path'],
                               sha256=hashlib.sha256(Path(row['path']).read_bytes()).hexdigest(),
                               branch_id=branch, metadata={'binary': suffix != '.md'})
        aid, path = workflow._refresh_workspace_index(con, run)
        saved = Path(path).read_text()
        monkeypatch.setitem(tools.CFG, 'db', str(tmp_path / 'board.db'))
        registered = []
        tools.register(SimpleNamespace(registerTool=registered.append))
        result = await registered[0].execute('fixture', {'view': 'workspace'}, None, None,
                                             SimpleNamespace(cwd=str(tmp_path)))
        live = result['content'][0]['text']
        for rendered in (saved, live):
            assert 'BINARY PAYLOAD' not in rendered
            assert 'Real heading' in rendered
            assert str(tmp_path / 'artifact.png') in rendered
            assert str(tmp_path / 'artifact.pdf') in rendered
        assert runs.artifact(con, aid)['kind'] == 'workspace_index'
    finally:
        con.close()

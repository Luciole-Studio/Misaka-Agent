"""A child owns its lifecycle; its parent card owns a separate workflow state."""
import ast
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from misaka.config import home
from misaka.core import session_catalog


@pytest.fixture
def inventory(tmp_path, monkeypatch):
    monkeypatch.setenv('MISAKA_HOME', str(tmp_path))
    root = home.path('sessions')
    root.mkdir(parents=True)
    monkeypatch.setattr(session_catalog, 'get_sessions_dir', lambda: str(root))
    with closing(sqlite3.connect(':memory:')) as con:
        con.row_factory = sqlite3.Row
        con.execute('CREATE TABLE tasks (id, session_dir, workspace, assignee, title, status)')
        directory = root / 'cards' / 't_123456'
        con.execute('INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)',
                    ('t_123456', str(directory), str(tmp_path), '10032', 'parent card', 'running'))
        yield con, directory, tmp_path


def _transcript(path, cwd, ident):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'type': 'session', 'version': 3, 'id': ident,
                                'cwd': str(cwd), 'timestamp': '2026-09-20T00:00:00Z'}) + '\n')


@pytest.mark.parametrize('status', ['completed', 'failed', 'killed', 'running', None])
def test_child_lifecycle_is_not_the_parent_card_status(inventory, status):
    con, directory, cwd = inventory
    parent = directory / 'parent.jsonl'
    child = directory / 'parent' / 'subagents' / 'agent-child.jsonl'
    _transcript(parent, cwd, 'parent')
    _transcript(child, cwd, 'child')
    if status:
        child.with_suffix('.meta.json').write_text(json.dumps({
            'status': status, 'agentType': 'Explore', 'description': 'check source',
        }))
    rows = {row['id']: row for row in session_catalog.list_entries(con)}
    assert rows['parent']['task_status'] == 'running'
    assert rows['parent']['kind'] == 'card'
    row = rows['child']
    assert row['kind'] == 'child' and row['state'] == 'saved'
    assert row['parent_task_status'] == 'running'
    assert row.get('child_status') == status
    assert 'task_status' not in row
    assert 'live_key' not in row


def _gather(entries, cwd):
    # Execute the panel's real pure listing closure, without starting its TUI or daemon.
    path = Path(__file__).parents[1] / 'misaka/ui/panel/panel.py'
    tree = ast.parse(path.read_text())
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'gather_sessions')
    scope = {'os': os, 'spaces': {}, 'listing': [], 'focused': None, 'side': {'ws': 0, 'sess_mode': 'all'},
             'effective_space_folder': lambda *args: str(cwd), 'cards_cache': {'sessions': entries},
             'session_meta': lambda path: {}, 'session_stamp': lambda stamp: 'saved', 'sess_folds': {},
             'folder_groups': lambda rows: rows, 'session_rows': lambda rows, folds: rows}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), scope)  # noqa: S102 - local source only
    return scope['gather_sessions']()


def test_panel_renders_child_status_independently_from_parent(inventory):
    con, directory, cwd = inventory
    parent = directory / 'parent.jsonl'
    child = directory / 'parent' / 'subagents' / 'agent-child.jsonl'
    _transcript(parent, cwd, 'parent')
    _transcript(child, cwd, 'child')
    child.with_suffix('.meta.json').write_text(json.dumps({'status': 'completed', 'agentType': 'Explore'}))
    rows = {row['id']: row for row in _gather(session_catalog.list_entries(con), cwd)}
    assert rows['child']['label'].startswith('completed · card running · ')
    assert rows['parent']['label'].startswith('card running · ')

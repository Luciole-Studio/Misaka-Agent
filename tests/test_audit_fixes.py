"""Offline regressions for the 2026-09-15 lifecycle, persistence and privacy audit."""
import asyncio
import errno
import gc
import json
import os
import sqlite3
import stat
import warnings
from pathlib import Path

import pytest


def test_office_cache_does_not_bypass_private_material_guard(tmp_path):
    import asyncio

    from misaka.core.documents.office import cache
    from misaka.core.tools._web.evidence import check_material_read
    from misaka.core.tools.read import create_read_tool_definition

    project = tmp_path / 'project'
    project.mkdir()
    source = project / 'table.csv'
    source.write_text('name,value\nfixture,1\n')
    private = tmp_path / 'private' / 'web-evidence' / 'originals' / 'page.md'
    private.parent.mkdir(parents=True)
    private.write_text('PRIVATE_FIXTURE_MARKER\n')
    with pytest.raises(ValueError, match='Private Web material'):
        check_material_read(str(private))
    entry = cache._directory(str(project)) / (cache._key(str(source)) + '.md')
    entry.symlink_to(private)
    result = asyncio.run(create_read_tool_definition(str(project)).execute('audit', {'path': 'table.csv'}))
    content = '\n'.join(item.text for item in result.content if hasattr(item, 'text'))
    assert 'PRIVATE_FIXTURE_MARKER' not in content, content


def test_fork_preserves_private_session_permissions(tmp_path):
    from misaka.core.session_manager import SessionManager

    old = os.umask(0o022)
    try:
        original = SessionManager.create(str(tmp_path), str(tmp_path / 'original'))
        original.appendMessage({'role': 'user', 'content': 'PRIVATE_FIXTURE_HISTORY', 'timestamp': 0})
        original.rewrite_file()
        assert stat.S_IMODE(Path(original.sessionFile).stat().st_mode) == 0o600
        fork = SessionManager.forkFrom(original.sessionFile, str(tmp_path), str(tmp_path / 'fork'))
        assert stat.S_IMODE(Path(fork.sessionFile).stat().st_mode) == 0o600
    finally:
        os.umask(old)


def test_role_config_survives_failed_model_write(tmp_path, monkeypatch):
    from misaka.config import profiles

    profile = tmp_path / 'profile'
    profile.mkdir()
    config = profile / 'config.json'
    original = json.dumps({'model': 'old/model', 'custom_option': 'keep this'})
    config.write_text(original)

    def disk_full(*args, **kwargs):
        raise OSError(errno.ENOSPC, 'fixture disk full')

    monkeypatch.setattr(profiles.atomic.os, 'replace', disk_full)
    assert profiles.persist_role_default_model(str(profile), 'new/model') is False
    assert config.read_text() == original


def test_uninstall_project_inspection_closes_database(tmp_path):
    from misaka.cli.uninstall import _projects

    db = tmp_path / 'board.db'
    con = sqlite3.connect(db)
    try:
        con.execute('CREATE TABLE tasks(workspace TEXT)')
        con.commit()
    finally:
        con.close()
    gc.collect()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always', ResourceWarning)
        _projects(str(db))
        gc.collect()
    assert not [str(w.message) for w in caught if issubclass(w.category, ResourceWarning)]


@pytest.mark.asyncio
async def test_streaming_mail_is_not_acknowledged_before_persistence(tmp_path):
    from misaka.agent.agent import Agent
    from misaka.core.agent_session import AgentSession
    from misaka.core.network import messages
    from misaka.core.session_manager import SessionManager

    con = messages.connect(str(tmp_path / 'messages.db'))
    delivery = None
    try:
        mid = messages.send(con, 'last-order', 'DURABLE_FIXTURE_MESSAGE', sender='10032')
        session = object.__new__(AgentSession)
        session.agent = Agent()
        session._isAgentRunActive = True
        session._customMessageReceipts = {}
        session.sessionManager = SessionManager.create(str(tmp_path), str(tmp_path / 'sessions'))
        session.sessionManager.rewrite_file()
        transcript_before = Path(session.sessionManager.sessionFile).read_bytes()
        inbox = messages.MessagesPart(sender='last-order', receive=True)
        inbox.attach(session)
        delivery = asyncio.create_task(inbox._deliver_once(con))
        async with asyncio.timeout(5):
            while not session.agent.hasQueuedMessages() and not delivery.done():
                await asyncio.sleep(0.01)
        assert session.agent.hasQueuedMessages()
        assert not session.sessionManager.getEntries()
        assert Path(session.sessionManager.sessionFile).read_bytes() == transcript_before
        row = con.execute('SELECT delivered_at FROM messages WHERE id=?', (mid,)).fetchone()
        assert row['delivered_at'] is None, 'mail ACKed with only an in-memory follow-up; a crash loses it'
        # The real transcript append fires the receipt, then the inbox may ACK.
        queued = session.agent._follow_up_queue.drain()
        for message in queued:
            session._append_custom_message(message)
        await asyncio.wait_for(delivery, 5)
        assert con.execute('SELECT delivered_at FROM messages WHERE id=?', (mid,)).fetchone()[0] is not None
        assert 'DURABLE_FIXTURE_MESSAGE' in Path(session.sessionManager.sessionFile).read_text()
    finally:
        if delivery is not None:
            delivery.cancel()
            await asyncio.gather(delivery, return_exceptions=True)
        con.close()


def test_mailbox_initialization_failure_closes_database(tmp_path, monkeypatch):
    from misaka.core.network import messages

    db = tmp_path / 'messages.db'
    connection = sqlite3.connect(db, isolation_level=None, check_same_thread=False)
    connection.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY)')
    monkeypatch.setattr(messages.sqlite3, 'connect', lambda *args, **kwargs: connection)
    try:
        with pytest.raises(sqlite3.OperationalError, match='delivered_at'):
            messages.connect(str(db))
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            connection.execute('SELECT 1')
    finally:
        connection.close()


def test_budget_cache_matches_ledger_after_task_deletion(tmp_path, monkeypatch):
    from misaka.core.platform import budget, tasks

    path = str(tmp_path / 'board.db')
    con = tasks.connect(path)
    monkeypatch.setattr(tasks, 'task_state_dir', lambda tid: str(tmp_path / 'state' / tid))
    try:
        tid = tasks.create_task(con, 'fixture', workspace=str(tmp_path), assignee='10032')
        budget.commit_agent_usage(con, None, tid, 1, 100)
        assert budget.spent(con) == 100
        assert tasks.delete_task(con, tid)[0]
        fresh = tasks.connect(path)
        try:
            assert budget.spent(con) == budget.spent(fresh)
        finally:
            fresh.close()
    finally:
        con.close()


def test_budget_cache_does_not_keep_rolled_back_usage(tmp_path):
    from misaka.core.platform import budget, tasks

    con = tasks.connect(str(tmp_path / 'board.db'))
    try:
        con.execute('BEGIN IMMEDIATE')
        con.execute("INSERT INTO events(task_id,kind,payload,created_at) VALUES(?,?,?,?)",
                    ('fixture', 'budget_usage', '{"totalTokens": 100}', 0))
        assert budget.spent(con) == 100
        con.rollback()
        assert con.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 0
        assert budget.spent(con) == 0
    finally:
        con.close()


def test_notification_maintenance_runs_after_initial_schema_creation(tmp_path):
    import time

    from misaka.core.platform import notifications, tasks

    con = tasks.connect(str(tmp_path / 'board.db'))
    try:
        sid = notifications.subscribe(con, 'fixture-owner', 'fixture-channel')
        old = int(time.time()) - notifications.SUBSCRIPTION_IDLE_SECONDS - 1
        con.execute('UPDATE notification_subscriptions SET updated_at=? WHERE id=?', (old, sid))
        notifications.init(con)
        assert con.execute('SELECT id FROM notification_subscriptions WHERE id=?', (sid,)).fetchone() is None
    finally:
        con.close()


@pytest.mark.asyncio
async def test_extension_exec_timeout_reaps_owned_descendants(tmp_path):
    import contextlib
    import sys

    import psutil

    from misaka.core.exec import exec_command

    pidfile = tmp_path / 'fixture-child.pid'
    program = (
        'import pathlib,subprocess,sys,time\n'
        'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"],'
        'stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n'
        f'pathlib.Path({str(pidfile)!r}).write_text(str(child.pid))\n'
        'time.sleep(30)\n'
    )
    try:
        result = await exec_command(sys.executable, ['-c', program], str(tmp_path), {'timeout': 500})
        assert result.killed and pidfile.exists()
        pid = int(pidfile.read_text())
        alive = psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        assert not alive, f'owned child {pid} survived timeout of its parent'
    finally:
        if pidfile.exists():
            # Only the test-created child, never a user process or shared process group.
            with contextlib.suppress(psutil.NoSuchProcess):
                child = psutil.Process(int(pidfile.read_text()))
                child.kill()
                child.wait(timeout=5)


@pytest.mark.asyncio
async def test_interactive_bash_spill_is_private(tmp_path, monkeypatch):
    from misaka.core import bash_executor

    class Operations:
        async def exec(self, command, cwd, options):
            options['onData'](b'PRIVATE_FIXTURE_OUTPUT\n' * 6000)
            return {'exitCode': 0}

    monkeypatch.setattr(bash_executor.tempfile, 'gettempdir', lambda: str(tmp_path))
    old = os.umask(0o022)
    try:
        result = await bash_executor.execute_bash_with_operations('fixture', str(tmp_path), Operations())
        assert result.fullOutputPath
        assert stat.S_IMODE(Path(result.fullOutputPath).stat().st_mode) == 0o600
    finally:
        os.umask(old)


def test_board_initialization_failure_closes_database(tmp_path, monkeypatch):
    from misaka.core.platform import notifications, tasks

    opened = []
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        con = real_connect(*args, **kwargs)
        opened.append(con)
        return con

    def fail(con):
        raise sqlite3.OperationalError('fixture initialization failure')

    monkeypatch.setattr(tasks.sqlite3, 'connect', connect)
    monkeypatch.setattr(notifications, 'init', fail)
    try:
        with pytest.raises(sqlite3.OperationalError, match='fixture initialization failure'):
            tasks.connect(str(tmp_path / 'board.db'))
        assert len(opened) == 1
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            opened[0].execute('SELECT 1')
    finally:
        for con in opened:
            con.close()


def test_update_decodes_local_install_file_uri(tmp_path, monkeypatch):
    import importlib.metadata

    from misaka.cli import update

    checkout = tmp_path / 'checkout with spaces'
    (checkout / '.git').mkdir(parents=True)

    class Distribution:
        version = '0.15.2'

        def read_text(self, name):
            if name == 'INSTALLER':
                return 'uv'
            if name == 'direct_url.json':
                return json.dumps({'url': checkout.as_uri(), 'dir_info': {'editable': True}})
            return None

    monkeypatch.setattr(importlib.metadata, 'distribution', lambda name: Distribution())
    found = update.describe()
    assert found.kind == 'checkout' and found.path == checkout, found


def _busy_mail_session(tmp_path):
    from misaka.agent.agent import Agent
    from misaka.core.agent_session import AgentSession
    from misaka.core.session_manager import SessionManager

    session = object.__new__(AgentSession)
    session.agent = Agent()
    session._isAgentRunActive = True
    session._customMessageReceipts = {}
    session.sessionManager = SessionManager.create(str(tmp_path), str(tmp_path / 'sessions'))
    session.sessionManager.rewrite_file()
    return session


async def _wait_for_queue(session, count=1):
    async with asyncio.timeout(5):
        while len(session.agent._follow_up_queue._messages) < count:
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_mail_shutdown_or_cancel_keeps_unpersisted_rows_retryable(tmp_path, cancel):
    from misaka.core.network import messages

    con = messages.connect(str(tmp_path / 'mail.db'))
    session = _busy_mail_session(tmp_path)
    inbox = messages.MessagesPart(sender='last-order', receive=True)
    inbox.attach(session)
    job = None
    try:
        mid = messages.send(con, 'last-order', 'fixture', sender='10032')
        job = asyncio.create_task(inbox._deliver_once(con))
        await _wait_for_queue(session)
        if cancel:
            job.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(job, 5)
        else:
            inbox._stop.set()
            await asyncio.wait_for(job, 5)
        row = con.execute('SELECT delivered_at,lease_token FROM messages WHERE id=?', (mid,)).fetchone()
        assert tuple(row) == (None, None)
        # A late persistence callback does not use the already-closed connection.
        con.close()
        for message in session.agent._follow_up_queue.drain():
            session._append_custom_message(message)
    finally:
        if job is not None:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        con.close()


@pytest.mark.asyncio
async def test_mail_retry_after_ack_failure_deduplicates_each_row(tmp_path, monkeypatch):
    from misaka.core.network import messages

    con = messages.connect(str(tmp_path / 'mail.db'))
    session = _busy_mail_session(tmp_path)
    inbox = messages.MessagesPart(sender='last-order', receive=True)
    inbox.attach(session)
    original_ack = messages.ack
    job = None
    try:
        first = messages.send(con, 'last-order', 'first fixture', sender='10032')
        def fail_ack(con, ids, **kwargs):
            if ids:
                raise sqlite3.OperationalError('fixture ACK failure')
        monkeypatch.setattr(messages, 'ack', fail_ack)
        job = asyncio.create_task(inbox._deliver_once(con))
        await _wait_for_queue(session)
        for message in session.agent._follow_up_queue.drain():
            session._append_custom_message(message)
        with pytest.raises(sqlite3.OperationalError, match='ACK failure'):
            await asyncio.wait_for(job, 5)
        assert con.execute('SELECT delivered_at FROM messages WHERE id=?', (first,)).fetchone()[0] is None
        # Change batch membership. The first delivery ID must still deduplicate.
        second = messages.send(con, 'last-order', 'second fixture', sender='10032')
        monkeypatch.setattr(messages, 'ack', original_ack)
        job = asyncio.create_task(inbox._deliver_once(con))
        await _wait_for_queue(session)
        queued = session.agent._follow_up_queue.drain()
        assert len(queued) == 1
        assert 'second fixture' in queued[0]['content']
        session._append_custom_message(queued[0])
        await asyncio.wait_for(job, 5)
        assert con.execute('SELECT COUNT(*) FROM messages WHERE delivered_at IS NOT NULL').fetchone()[0] == 2
        entries = session.sessionManager.getEntries()
        assert len(entries) == 2 and first != second
    finally:
        if job is not None:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        con.close()


@pytest.mark.asyncio
async def test_mail_persist_error_does_not_ack_batch(tmp_path):
    from misaka.core.network import messages

    con = messages.connect(str(tmp_path / 'mail.db'))
    session = _busy_mail_session(tmp_path)
    inbox = messages.MessagesPart(sender='last-order', receive=True)
    inbox.attach(session)
    job = None
    try:
        for body in ('one', 'two'):
            messages.send(con, 'last-order', body, sender='10032')
        job = asyncio.create_task(inbox._deliver_once(con))
        await _wait_for_queue(session)
        delivery_id = next(iter(session._customMessageReceipts))
        session._fail_custom_receipt(delivery_id, OSError('fixture transcript write failed'))
        with pytest.raises(OSError, match='transcript write failed'):
            await asyncio.wait_for(job, 5)
        assert all(tuple(r) == (None, None) for r in con.execute('SELECT delivered_at,lease_token FROM messages'))
    finally:
        if job is not None:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        con.close()


@pytest.mark.asyncio
async def test_mail_renews_lease_while_waiting_for_persistence(tmp_path, monkeypatch):
    from misaka.core.network import messages

    con = messages.connect(str(tmp_path / 'mail.db'))
    session = _busy_mail_session(tmp_path)
    inbox = messages.MessagesPart(sender='last-order', receive=True)
    inbox.attach(session)
    renewals = []
    original = messages.renew
    def renew(*args, **kwargs):
        result = original(*args, **kwargs)
        renewals.append(True)
        return result
    monkeypatch.setattr(messages, 'renew', renew)
    monkeypatch.setattr(messages, 'DELIVERY_RENEW_SECONDS', 0.02)
    job = None
    try:
        messages.send(con, 'last-order', 'fixture', sender='10032')
        job = asyncio.create_task(inbox._deliver_once(con))
        await _wait_for_queue(session)
        async with asyncio.timeout(5):
            while not renewals:
                await asyncio.sleep(0.01)
        assert con.execute('SELECT delivered_at FROM messages').fetchone()[0] is None
        for message in session.agent._follow_up_queue.drain():
            session._append_custom_message(message)
        await asyncio.wait_for(job, 5)
    finally:
        if job is not None:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        con.close()


def test_profile_success_preserves_fields_and_invalid_config_is_not_overwritten(tmp_path):
    from misaka.config import profiles

    config = tmp_path / 'config.json'
    config.write_text('{"custom": 0, "model": "old"}')
    assert profiles.persist_role_default_model(str(tmp_path), 'new')
    assert json.loads(config.read_text()) == {'custom': 0, 'model': 'new'}
    assert not profiles.persist_role_default_model(str(tmp_path), 'new')
    config.write_text('{broken')
    assert not profiles.persist_role_default_model(str(tmp_path), 'another')
    assert config.read_text() == '{broken'


def test_budget_tracks_other_connections_updates_and_deletes(tmp_path):
    from misaka.core.platform import budget, tasks

    first = tasks.connect(str(tmp_path / 'board.db'))
    second = tasks.connect(str(tmp_path / 'board.db'))
    try:
        budget.commit_agent_usage(first, None, 'fixture', 1, 100)
        assert budget.spent(first) == budget.spent(second) == 100
        second.execute('UPDATE events SET payload=?', ('{"totalTokens": 70}',))
        assert budget.spent(first) == budget.spent(second) == 70
        second.execute('DELETE FROM events')
        assert budget.spent(first) == budget.spent(second) == 0
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize('kind', ['hardlink', 'fifo'])
def test_office_cache_rejects_non_regular_or_linked_entries(tmp_path, kind):
    from misaka.core.documents.office import cache

    source = tmp_path / 'source.csv'
    source.write_text('a,b\n1,2')
    secret = tmp_path / 'private.txt'
    secret.write_text('PRIVATE_FIXTURE_MARKER')
    entry = cache._directory(tmp_path) / (cache._key(source) + '.md')
    if kind == 'hardlink':
        os.link(secret, entry)
    else:
        if not hasattr(os, 'mkfifo'):
            pytest.skip('POSIX FIFO test')
        os.mkfifo(entry)
    assert cache.render_cached(source, lambda: 'rendered', workspace=tmp_path) == 'rendered'
    assert secret.read_text() == 'PRIVATE_FIXTURE_MARKER'


@pytest.mark.asyncio
@pytest.mark.parametrize('trigger', ['abort', 'cancel', 'timeout'])
async def test_extension_exec_cleans_detached_sigterm_resistant_child(tmp_path, trigger):
    import contextlib
    import sys

    import psutil

    from misaka.core.exec import exec_command

    pidfile = tmp_path / 'child.pid'
    child_script = 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)'
    parent_script = (
        'import pathlib,subprocess,sys,time\n'
        f'p=subprocess.Popen([sys.executable,"-c",{child_script!r}],start_new_session=True, '
        'stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n'
        f'pathlib.Path({str(pidfile)!r}).write_text(str(p.pid))\n'
        'time.sleep(30)\n')
    abort = asyncio.Event()
    job = asyncio.create_task(exec_command(sys.executable, ['-c', parent_script], str(tmp_path),
                                           {'signal': abort, 'timeout': 700 if trigger == 'timeout' else 0}))
    try:
        async with asyncio.timeout(5):
            while not pidfile.exists():
                await asyncio.sleep(0.01)
        # Let the child install its handler before teardown.
        await asyncio.sleep(0.1)
        if trigger == 'abort':
            abort.set()
        elif trigger == 'cancel':
            job.cancel()
        if trigger == 'cancel':
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(job, 8)
        else:
            assert (await asyncio.wait_for(job, 8)).killed
        pid = int(pidfile.read_text())
        assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    finally:
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        if pidfile.exists():
            with contextlib.suppress(psutil.NoSuchProcess):
                child = psutil.Process(int(pidfile.read_text()))
                child.kill()
                child.wait(timeout=5)


@pytest.mark.asyncio
async def test_extension_exec_keeps_output_and_normal_exit_status(tmp_path):
    import sys

    from misaka.core.exec import exec_command

    result = await exec_command(sys.executable, ['-c', 'import sys;print("out");print("err",file=sys.stderr);sys.exit(7)'], str(tmp_path))
    assert (result.stdout, result.stderr, result.code, result.killed) == ('out\n', 'err\n', 7, False)


@pytest.mark.parametrize('active_subscriber', [False, True])
def test_notification_retention_respects_active_unacknowledged_cursor(tmp_path, active_subscriber):
    import time

    from misaka.core.platform import notifications, tasks

    con = tasks.connect(str(tmp_path / 'board.db'))
    try:
        sid = notifications.subscribe(con, 'owner', 'channel') if active_subscriber else None
        notifications.publish(con, 'research', 'fixture', 'progress', {'step': 1})
        cutoff = int(time.time()) - notifications.EVENT_RETENTION_SECONDS - 1
        con.execute('UPDATE notification_events SET created_at=?', (cutoff,))
        notifications.init(con)
        assert con.execute('SELECT COUNT(*) FROM notification_events').fetchone()[0] == int(active_subscriber)
        if sid:
            con.execute('UPDATE notification_subscriptions SET cursor=100 WHERE id=?', (sid,))
            notifications.init(con)
            assert con.execute('SELECT COUNT(*) FROM notification_events').fetchone()[0] == 0
    finally:
        con.close()


def test_atomic_private_write_is_private_before_first_byte(tmp_path, monkeypatch):
    import builtins

    from misaka.utils import atomic

    original_open = builtins.open
    observed = []
    def opened(path, mode='r', *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        if str(path).endswith('.tmp') and any(flag in mode for flag in ('w', 'x')):
            observed.append(stat.S_IMODE(os.fstat(handle.fileno()).st_mode))
        return handle
    monkeypatch.setattr(builtins, 'open', opened)
    mask = os.umask(0o022)
    try:
        atomic.write_text(tmp_path / 'session.jsonl', 'PRIVATE_FIXTURE_BYTES', mode=0o600)
        assert observed == [0o600]
    finally:
        os.umask(mask)


@pytest.mark.parametrize('dirname,authority', [('checkout with spaces', ''), ('日本語#?%', ''), ('local checkout', 'localhost')])
def test_update_uri_roundtrip(tmp_path, monkeypatch, dirname, authority):
    import importlib.metadata
    from types import SimpleNamespace

    from misaka.cli import update

    checkout = tmp_path / dirname
    (checkout / '.git').mkdir(parents=True)
    url = checkout.as_uri()
    if authority:
        url = url.replace('file:///', 'file://localhost/', 1)
    dist = SimpleNamespace(version='fixture', read_text=lambda name: (
        json.dumps({'url': url, 'dir_info': {'editable': True}}) if name == 'direct_url.json' else 'uv'))
    monkeypatch.setattr(importlib.metadata, 'distribution', lambda name: dist)
    found = update.describe()
    assert found.kind == 'checkout' and found.path == checkout


def test_atomic_write_does_not_remove_an_unowned_temp_path(tmp_path, monkeypatch):
    from misaka.utils import atomic

    target = tmp_path / 'state'
    target.write_text('old')
    monkeypatch.setattr(atomic.secrets, 'token_hex', lambda _: 'fixture')
    collision = Path(f'{target}.{os.getpid()}.fixture.tmp')
    collision.write_text('unrelated')
    with pytest.raises(FileExistsError):
        atomic.write_text(target, 'new', mode=0o600)
    assert target.read_text() == 'old' and collision.read_text() == 'unrelated'


def test_atomic_write_preserves_public_default_and_existing_mode(tmp_path):
    from misaka.utils import atomic

    target = tmp_path / 'public'
    mask = os.umask(0o022)
    try:
        atomic.write_text(target, 'one')
        assert stat.S_IMODE(target.stat().st_mode) == 0o644
        target.chmod(0o640)
        atomic.write_text(target, 'two')
        assert stat.S_IMODE(target.stat().st_mode) == 0o640
    finally:
        os.umask(mask)


@pytest.mark.asyncio
async def test_mail_cancelled_batch_reattaches_receipts_without_duplicating_queue(tmp_path):
    from misaka.core.network import messages

    con = messages.connect(str(tmp_path / 'mail.db'))
    session = _busy_mail_session(tmp_path)
    inbox = messages.MessagesPart(sender='last-order', receive=True)
    inbox.attach(session)
    job = None
    try:
        for body in ('first', 'second'):
            messages.send(con, 'last-order', body, sender='10032')
        job = asyncio.create_task(inbox._deliver_once(con))
        await _wait_for_queue(session)
        assert len(session.agent._follow_up_queue._messages) == 1  # one batch, one wake-up
        assert session.agent._follow_up_queue._messages[0]['details']['count'] == 2
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        messages.send(con, 'last-order', 'third', sender='10032')
        job = asyncio.create_task(inbox._deliver_once(con))
        await _wait_for_queue(session, 2)
        assert len(session.agent._follow_up_queue._messages) == 2
        while session.agent.hasQueuedMessages():
            for message in session.agent._follow_up_queue.drain():
                session._append_custom_message(message)
        await asyncio.wait_for(job, 5)
        assert con.execute('SELECT COUNT(*) FROM messages WHERE delivered_at IS NOT NULL').fetchone()[0] == 3
        assert len(session.sessionManager.getEntries()) == 2
    finally:
        if job is not None:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        con.close()

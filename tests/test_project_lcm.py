"""Project isolation, disposable ownership and portable native checkpoints."""
from __future__ import annotations

import hashlib
import json
import select
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from misaka.core.session_manager import SessionManager
from misaka.extensions.misaka_lcm.host import carry, config_bridge, storage
from misaka.extensions.misaka_lcm.host import context_engine as ce
from misaka.extensions.misaka_lcm.vendor.dag import SummaryNode


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    ce.close_all()
    monkeypatch.setenv("MISAKA_CODING_AGENT_DIR", str(tmp_path / "agent"))
    monkeypatch.setenv("MISAKA_SESSIONS", str(tmp_path / "sessions"))
    monkeypatch.delenv("MISAKA_SUBAGENT_PARENT_SESSION_ID", raising=False)
    yield
    ce.close_all()


def session(workspace, *, execution=None):
    workspace.mkdir(parents=True, exist_ok=True)
    manager = SessionManager.create(str(execution or workspace), str(workspace.parent / "sessions"))
    manager.appendCustomEntry("lcm-project", {"workspace": str(workspace.resolve())})
    return storage.context(SimpleNamespace(cwd=str(execution or workspace), sessionManager=manager, model=None), workspace)


def say(ctx, text, tick=0):
    ctx.sessionManager.appendMessage({"role": "user", "content": text, "timestamp": 1789460410000 + tick})


def summary(ctx):
    """Publish an ordinary native checkpoint over real stored source rows."""
    say(ctx, "alpha-project original evidence 中文")
    ce.sync(ctx)
    built = ce.bound_engine(ctx)
    snapshot = ce.snapshot(ctx)
    _, rows = ce._archive_map(built, snapshot)
    refs = [key for key, ids in rows.items() if ids]
    ids = [row for key in refs for row in rows[key]]
    node = SummaryNode(session_id=built.current_session_id, depth=0, summary="condensed evidence",
                       source_ids=ids, source_type="messages", token_count=5, source_token_count=20)
    built._dag.add_node(node)
    frame = ce._node_text(node)
    ctx.sessionManager.appendCompaction("LCM checkpoint", "", 20, details={"lcm": {
        "nodes": [{"id": node.node_id, "depth": 0, "summary": node.summary, "frame": frame,
                   "sourceEntries": refs}], "scaffolds": [0]}},
        contextMessages=[{"role": "user", "content": frame, "timestamp": 1789460411000}])
    ce.sync(ctx)
    return node


def count(built):
    return built._store._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


def test_project_paths_ignore_global_overrides_and_bind_all_content(tmp_path, monkeypatch):
    for key in ("MISAKA_LCM_DB", "LCM_DATABASE_PATH", "LCM_EXTRACTION_OUTPUT_PATH", "LCM_LARGE_OUTPUT_EXTERNALIZATION_PATH"):
        monkeypatch.setenv(key, str(tmp_path / "global"))
    ctx = session(tmp_path / "project")
    conf = config_bridge.load_config(ctx=ctx)
    root = storage.directory(storage.project(ctx))
    assert conf.database_path == str(root / "lcm.db")
    assert Path(conf.extraction_output_path).parent == root
    assert Path(conf.large_output_externalization_path).parent == root
    assert not root.exists()  # resolving a path is not opening a runtime


def test_project_isolation_shared_roles_and_session_switch(tmp_path):
    a, sister, b = session(tmp_path / "a"), session(tmp_path / "a"), session(tmp_path / "b")
    say(a, "ALPHA_CANARY")
    say(sister, "SISTER_CANARY")
    say(b, "BETA_CANARY")
    for ctx in (a, sister, b):
        ce.sync(ctx)
    assert count(ce.bound_engine(a)) == 2
    assert count(ce.bound_engine(sister)) == 2
    assert count(ce.bound_engine(b)) == 1
    output = ce.bound_engine(b).handle_tool_call("lcm_grep", {"pattern": "ALPHA_CANARY", "scope": "all"})
    assert "ALPHA_CANARY" not in output
    ce.close(a)
    ce.release_project(a)
    assert Path(config_bridge.database_path(sister)).is_file()
    ce.close(sister)
    assert Path(config_bridge.database_path(sister)).is_file()  # /new or reload is not application exit
    ce.release_project(sister)
    assert not storage.directory(storage.project(a)).exists()
    assert Path(config_bridge.database_path(b)).is_file()


def test_project_marker_preserves_child_worktree_identity(tmp_path):
    ctx = session(tmp_path / "project", execution=tmp_path / "worktree")
    assert storage.project(ctx) == (tmp_path / "project").resolve()
    reopened = storage.context(ctx._ctx, tmp_path / "unrelated-launch-cwd")
    assert storage.project(reopened) == storage.project(ctx)
    frozen = ce.freeze_context(ctx, ce.snapshot(ctx))
    assert config_bridge.database_path(frozen) == config_bridge.database_path(ctx)


def test_full_checkpoint_rebuild_and_fork_preserve_session_bytes(tmp_path):
    ctx = session(tmp_path / "project")
    summary(ctx)
    source = Path(ctx.sessionManager.getSessionFile())
    before = source.read_bytes()
    ce.close(ctx)
    ce.release_project(ctx)
    assert not storage.directory(storage.project(ctx)).exists()
    restored = SessionManager.openInMemory(str(source))
    resumed = storage.context(SimpleNamespace(cwd=ctx.cwd, sessionManager=restored, model=None), ctx.lcm_project)
    ce.sync(resumed)
    built = ce.bound_engine(resumed)
    nodes, _ = ce._checkpoint_nodes(built, ce._checkpoint(ce.snapshot(resumed)), ce.snapshot(resumed))
    assert len(nodes) == 1
    rows = built._store.get_batch(carry.source_ids(built, nodes[0]))
    assert any("original evidence" in row["content"] for row in rows.values())
    assert source.read_bytes() == before
    assert built.get_runtime_identity()["plugin_name"] == "misaka-lcm"


def carried(ctx):
    ce.bound_engine(ctx)._config.new_session_retain_depth = -1
    target = SessionManager.create(ctx.cwd, ctx.sessionManager.getSessionDir())
    carry.rollover(target, ce.snapshot(ctx), ctx)
    return storage.context(SimpleNamespace(cwd=ctx.cwd, sessionManager=target, model=None), ctx.lcm_project)


def test_carry_rebuild_after_reordered_row_ids_and_chained_carry(tmp_path):
    original = session(tmp_path / "project")
    old_node = summary(original)
    old_rows = carry.source_ids(ce.bound_engine(original), old_node)
    next_ctx = carried(original)
    next_ctx.sessionManager.appendCustomEntry("lcm-project", {"workspace": next_ctx.lcm_project})
    ce.sync(next_ctx)
    final_ctx = carried(next_ctx)
    receipt = carry.receipt(ce.snapshot(final_ctx))
    assert all(isinstance(ref, dict) for ref in receipt["details"]["lcm"]["carry"]["sources"].values())
    source_files = [Path(ctx.sessionManager.getSessionFile()) for ctx in (original, next_ctx, final_ctx)]
    saved = {p: p.read_bytes() for p in source_files}
    ce.close_all()
    # Change row allocation before rebuilding. Old store IDs must not select this text.
    other = session(tmp_path / "project")
    say(other, "DECOY new cache row one")
    ce.sync(other)
    ce.sync(final_ctx)
    built = ce.bound_engine(final_ctx)
    restored = carry.sources(built, ce.snapshot(final_ctx))
    rows = built._store.get_batch(list(restored.values()))
    assert rows and all("DECOY" not in row["content"] for row in rows.values())
    assert any("original evidence" in row["content"] for row in rows.values())
    assert count(built) == 2
    assert not built._store.get_batch(old_rows)
    assert built._dag.get_node(old_node.node_id) is None
    ce.sync(final_ctx)
    assert count(built) == 2  # idempotent restore, not duplicate import
    assert all(p.read_bytes() == saved[p] for p in source_files)


@pytest.mark.parametrize("damage", ["entry", "project", "missing"])
def test_carry_rejects_missing_modified_or_cross_project_sources(tmp_path, damage):
    original = session(tmp_path / "project")
    summary(original)
    target = carried(original)
    ce.close_all()
    source = Path(original.sessionManager.getSessionFile())
    if damage == "missing":
        source.unlink()
    else:
        lines = [json.loads(line) for line in source.read_text().splitlines()]
        if damage == "entry":
            next(item for item in lines if item["type"] == "message")["message"]["content"] = "tampered"
        else:
            next(item for item in lines if item.get("customType") == "lcm-project")["data"]["workspace"] = str(tmp_path / "other")
        source.write_text("".join(json.dumps(line) + "\n" for line in lines))
    with pytest.raises((ValueError, FileNotFoundError)):
        ce.sync(target)


def test_core_tool_schemas_unchanged_and_product_guidance(tmp_path):
    from misaka.extensions.misaka_lcm.host import tools
    from misaka.extensions.misaka_lcm.vendor.engine import LCMEngine
    for schema in LCMEngine.get_tool_schemas():
        definition = tools._definition(schema, str(tmp_path))
        assert definition.parameters == schema["parameters"]
        assert definition.name == schema["name"]
    assert "MISAKA LCM" in tools.recall_guideline()
    assert "Hermes-LCM" not in tools.recall_guideline()


def test_cleanup_refuses_unknown_directory_and_symlinks(tmp_path):
    project = tmp_path / "project"
    root = storage.directory(project)
    root.mkdir(parents=True)
    (root / "valuable").write_text("keep")
    with pytest.raises(ValueError, match="Not a MISAKA"):
        storage.acquire(project)
    assert (root / "valuable").read_text() == "keep"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    root.rename(project / "saved")
    root.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        storage.acquire(project)


def test_interrupted_marker_initialization_and_failed_engine_retry(tmp_path, monkeypatch):
    ctx = session(tmp_path / "project")
    root = storage.directory(storage.project(ctx))
    root.mkdir(parents=True)
    (root / storage._MARKER).touch()
    original = storage.namespace_ids
    def fail(connection):
        raise RuntimeError("initialization interrupted")
    monkeypatch.setattr(storage, "namespace_ids", fail)
    with pytest.raises(RuntimeError, match="initialization interrupted"):
        ce.engine(ctx)
    assert ce._key(ctx) not in ce._ENGINES
    monkeypatch.setattr(storage, "namespace_ids", original)
    say(ctx, "retry survives")
    ce.sync(ctx)
    assert count(ce.bound_engine(ctx)) == 1
    ce.close(ctx)
    ce.release_project(ctx)
    assert not root.exists()


_CHILD = '''
import sys
from pathlib import Path
from misaka.extensions.misaka_lcm.host import storage
p = Path(sys.argv[1]).resolve()
storage.acquire(p)
print("ready", flush=True)
sys.stdin.readline()
storage.release(p)
'''


def child(project):
    proc = subprocess.Popen([sys.executable, "-c", _CHILD, str(project)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if not select.select([proc.stdout], [], [], 20)[0] or proc.stdout.readline().strip() != "ready":
        proc.kill()
        raise AssertionError(proc.communicate(timeout=10))
    return proc


def test_cross_process_last_owner_and_crash_recovery(tmp_path):
    project = (tmp_path / "project").resolve()
    storage.acquire(project)
    proc = child(project)
    root = storage.directory(project)
    (root / "payload").write_text("active")
    storage.release(project)
    assert (root / "payload").exists()
    out, err = proc.communicate("finish\n", timeout=20)
    assert proc.returncode == 0, (out, err)
    assert not root.exists()
    proc = child(project)
    (root / "payload").write_text("abandoned")
    proc.kill()
    proc.communicate(timeout=20)
    assert root.exists()
    storage.acquire(project)
    assert not (root / "payload").exists()
    storage.release(project)
    assert not root.exists()


def test_atexit_cleans_after_closing_real_engine(tmp_path):
    project = tmp_path / "project"
    result = subprocess.run([sys.executable, "-c", '''
import sys
from types import SimpleNamespace
from misaka.core.session_manager import SessionManager
from misaka.extensions.misaka_lcm.host import context_engine
ctx = SimpleNamespace(cwd=sys.argv[1], model=None, sessionManager=SessionManager.inMemory(sys.argv[1]))
context_engine.bound_engine(ctx)
''', str(project)], capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert not storage.directory(project).exists()


def test_real_compaction_rebuild_then_fork(tmp_path, monkeypatch):
    from misaka.extensions.misaka_lcm.vendor import engine as upstream
    ctx = session(tmp_path / "project")
    built = ce.bound_engine(ctx)
    built._config.fresh_tail_count = 2
    built._config.leaf_chunk_tokens = 40
    monkeypatch.setattr(upstream, "summarize_with_escalation",
                        lambda **_: ("Summary of ALPHA evidence. [Expand for details: original]", 1))
    for i in range(12):
        say(ctx, f"ALPHA evidence {i} " + "material " * 50, tick=i)
    native = ctx.sessionManager.buildSessionContext().messages
    prepared = ce.prepare({"reason": "manual", "messages": native}, ctx)
    result = ce.compact(prepared)
    assert result is not None and result.details["lcm"]["nodes"]
    ctx.sessionManager.appendCompaction(result.summary, result.firstKeptEntryId, result.tokensBefore,
                                       details=result.details, contextMessages=result.contextMessages)
    source = Path(ctx.sessionManager.getSessionFile())
    before = source.read_bytes()
    ce.close_all()
    ce.sync(ctx)
    assert count(ce.bound_engine(ctx)) == 12
    assert source.read_bytes() == before
    ce.close(ctx)
    ctx.sessionManager.createBranchedSession(ctx.sessionManager.getLeafId())
    ce.sync(ctx)
    built = ce.bound_engine(ctx)
    snap = ce.snapshot(ctx)
    nodes, _ = ce._checkpoint_nodes(built, ce._checkpoint(snap), snap)
    assert nodes and all(built._store.get_batch(carry.source_ids(built, node)) for node in nodes)
    assert source.read_bytes() == before


async def test_extension_new_reload_and_quit_lifetime(tmp_path):
    from misaka.extensions.misaka_lcm.host.extension import register
    ctx = session(tmp_path / "project")
    events = {}
    harn = SimpleNamespace(registerProvider=lambda *_: None, registerTool=lambda *_: None,
                           registerCommand=lambda *_: None,
                           on=lambda name, callback: events.__setitem__(name, callback),
                           appendEntry=ctx.sessionManager.appendCustomEntry, getActiveTools=list)
    register(harn, kind="foreground", workspace=ctx.lcm_project)
    await events["session_start"]({}, ctx)
    say(ctx, "retain archive")
    ctx.sessionManager.rewrite_file()
    await events["agent_end"]({}, ctx)
    source = Path(ctx.sessionManager.getSessionFile())
    before = source.read_bytes()
    await events["session_shutdown"]({"reason": "reload"}, ctx)
    assert Path(config_bridge.database_path(ctx)).exists()
    await events["session_start"]({}, ctx)
    await events["session_shutdown"]({"reason": "quit"}, ctx)
    assert not storage.directory(storage.project(ctx)).exists()
    assert source.read_bytes() == before


def test_operator_database_paths_cannot_escape_project(tmp_path):
    from misaka.extensions.misaka_lcm.host.operators import _owned_option
    path = str(tmp_path / "project/lcm.db")
    assert _owned_option([], "--db", path) == ["--db", path]
    assert _owned_option(["--db=" + path], "--db", path) == ["--db=" + path, "--db", path]
    with pytest.raises(ValueError):
        _owned_option(["--db", str(tmp_path / "other/lcm.db")], "--db", path)


def test_status_names_disposable_project_and_cleans_up(tmp_path, monkeypatch):
    from misaka.extensions.misaka_lcm.host import operations
    monkeypatch.chdir(tmp_path)
    output = operations.command("status")
    assert output.startswith("MISAKA LCM status\n")
    assert "storage_lifetime: project-runtime" in output
    assert "no active Hermes session" not in output
    assert not storage.directory(tmp_path).exists()


def test_operator_import_keeps_payloads_scoped_and_live_owner_intact(tmp_path, monkeypatch):
    from misaka.extensions.misaka_lcm.host import operators
    ctx = session(tmp_path / "project")
    monkeypatch.chdir(ctx.lcm_project)
    monkeypatch.setenv("LCM_LARGE_OUTPUT_EXTERNALIZATION_PATH", str(tmp_path / "outside"))
    monkeypatch.setenv("LCM_LARGE_OUTPUT_EXTERNALIZATION_ENABLED", "1")
    monkeypatch.setenv("LCM_LARGE_OUTPUT_EXTERNALIZATION_THRESHOLD_CHARS", "200")
    raw = "IMPORT_CANARY " + "x" * 5000
    source = tmp_path / "source.jsonl"
    source.write_text("\n".join(json.dumps(row) for row in [
        {"type": "session", "id": "source", "version": 3, "cwd": str(tmp_path)},
        {"type": "message", "id": "entry", "parentId": None,
         "timestamp": "2026-09-15T00:00:00Z", "message": {"role": "user", "content": raw}},
    ]) + "\n")
    saved = source.read_bytes()
    built = ce.bound_engine(ctx)
    assert operators.main(["import", "--source-jsonl", str(source), "--apply", "--json",
                           "--target-d", str(tmp_path / "outside.db")]) == 0
    root = storage.directory(storage.project(ctx))
    payloads = list((root / "lcm-large-outputs").glob("*.json"))
    assert len(payloads) == 1 and json.loads(payloads[0].read_text())["content"] == raw
    assert not (tmp_path / "outside").exists()
    assert not (tmp_path / "outside.db").exists()
    assert count(built) == 1
    row = built._store._conn.execute("SELECT store_id FROM messages").fetchone()[0]
    assert row > (1 << 32)
    ce.close(ctx)
    ce.release_project(ctx)
    assert not root.exists() and source.read_bytes() == saved


def test_core_changed_only_at_existing_namespace_import_seams():
    root = Path(__file__).resolve().parents[1] / "misaka/extensions/misaka_lcm"
    manifest = json.loads((root / "CORE_INTEGRITY.json").read_text())
    for name, digest in manifest["files"].items():
        content = (root / name).read_bytes()
        normalized = content.replace(b"misaka.extensions.misaka_lcm", b"misaka.extensions.hermes_lcm")
        normalized = normalized.replace(b"misaka/extensions/misaka_lcm", b"misaka/extensions/hermes_lcm")
        assert hashlib.sha256(normalized).hexdigest() == digest, name


def test_audit_carry_target_preserves_project_and_visible_context(tmp_path):
    ctx = session(tmp_path / 'project', execution=tmp_path / 'worktree')
    summary(ctx)
    target = carried(ctx)
    reopened = storage.context(target._ctx, tmp_path / 'different-launch-project')
    assert storage.project(reopened) == storage.project(ctx)
    assert 'condensed evidence' in str(target.sessionManager.buildSessionContext().messages)
    ce.close_all()
    ce.sync(reopened)
    assert 'condensed evidence' in str(ce._messages(ce.snapshot(reopened), ce.bound_engine(reopened)))


def test_audit_carry_import_does_not_unregister_live_source(tmp_path):
    from misaka.extensions.misaka_lcm.vendor.engine_registry import (
        resolve_active_lcm_engine,
    )
    ctx = session(tmp_path / 'project')
    summary(ctx)
    target = carried(ctx)
    ce.close_all()
    ce.sync(ctx)
    source = ce.bound_engine(ctx)
    source_id = ctx.sessionManager.getSessionId()
    assert resolve_active_lcm_engine(source_id) is source
    ce.sync(target)
    assert resolve_active_lcm_engine(source_id) is source


def test_audit_shutdown_closes_only_owned_semantic_reader_pool(tmp_path):
    from misaka.extensions.misaka_lcm.vendor import retrieval_core as retrieval
    from misaka.extensions.misaka_lcm.vendor.vector_store import VectorStore
    ctx, other = session(tmp_path / 'project'), session(tmp_path / 'other')
    a, b = ce.bound_engine(ctx), ce.bound_engine(other)
    try:
        first, _, _ = retrieval._acquire_vector_store(a, vector_store_cls=VectorStore, scan_rows=None)
        second, _, _ = retrieval._acquire_vector_store(b, vector_store_cls=VectorStore, scan_rows=None)
        ce.close(ctx)
        ce.release_project(ctx)
        assert all(key[0] != config_bridge.database_path(ctx) for key in retrieval._vector_store_pool)
        assert any(item['store'] is second for item in retrieval._vector_store_pool.values())
        assert first._conn is None
    finally:
        retrieval._reset_vector_store_pool()


def test_audit_quit_waits_for_timed_out_tool_worker(tmp_path, monkeypatch):
    import threading

    from misaka.extensions.misaka_lcm.vendor import tools as upstream_tools
    from misaka.extensions.misaka_lcm.vendor.engine import LCMEngine
    ctx = session(tmp_path / 'project')
    built = ce.bound_engine(ctx)
    entered, unblock, finished, closed = (threading.Event() for _ in range(4))
    def work():
        entered.set()
        unblock.wait(5)
        built._store._conn.execute('SELECT 1').fetchone()
        finished.set()
    def handler(*args, **kwargs):
        return upstream_tools._run_within_deadline(work, remaining_s=0.01, name='audit-lcm-worker')
    monkeypatch.setattr(LCMEngine, 'handle_tool_call', handler)
    with pytest.raises(TimeoutError):
        built.handle_tool_call('fixture', {})
    assert entered.wait(1)
    def quit():
        ce.close(ctx)
        ce.release_project(ctx)
        closed.set()
    closer = threading.Thread(target=quit)
    closer.start()
    try:
        assert not closed.wait(0.05), 'engine/cache closed while a timed-out worker still owns it'
        assert storage.directory(storage.project(ctx)).exists()
    finally:
        unblock.set()
        closer.join(5)
        finished.wait(1)
    assert closed.is_set() and finished.is_set()
    assert not storage.directory(storage.project(ctx)).exists()


def test_audit_image_calibration_does_not_write_global_cache(tmp_path, monkeypatch):
    from misaka.extensions.misaka_lcm.host import native
    from misaka.extensions.misaka_lcm.native import image_token_cost as costs
    from misaka.extensions.misaka_lcm.native.usage_anchor import capture_usage_anchor
    ctx = session(tmp_path / 'project')
    built = ce.bound_engine(ctx)
    built.model = 'audit-image-model'
    messages = [{'role': 'user', 'content': 'plain prefix'}]
    built._usage_anchor = capture_usage_anchor(100, 0, messages)
    messages.append({'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,aA=='}}]})
    monkeypatch.setattr(costs, '_LEARNED', {})
    monkeypatch.setattr(costs, '_LOADED', False)
    assert costs.calibrate_from_usage(built, messages, 1600) is not None
    ce.close(ctx)
    ce.release_project(ctx)
    assert not native._cache_file('image_token_costs.json').exists()


def test_audit_deadline_worker_rejection_and_bounded_drain(tmp_path):
    import threading

    from misaka.extensions.misaka_lcm.host import execution
    from misaka.extensions.misaka_lcm.vendor import tools as upstream
    ctx = session(tmp_path / 'project')
    built = ce.bound_engine(ctx)
    with execution.worker_owner(built):
        with pytest.raises(upstream._WorkerCapacityError):
            upstream._run_within_deadline(lambda: None, remaining_s=1, name='capacity',
                                          worker_slots=threading.BoundedSemaphore(0))
        with pytest.raises(TimeoutError):
            upstream._run_within_deadline(lambda: None, remaining_s=0, name='zero')
    execution.drain_tool_workers(built, timeout=0)
    event = threading.Event()
    try:
        with execution.worker_owner(built), pytest.raises(TimeoutError):
            upstream._run_within_deadline(event.wait, remaining_s=0.01, name='delayed')
        with pytest.raises(TimeoutError, match='still active'):
            execution.drain_tool_workers(built, timeout=0.01)
        assert storage.directory(storage.project(ctx)).exists()
    finally:
        event.set()
        execution.drain_tool_workers(built, timeout=1)


def test_audit_one_failed_shutdown_does_not_retain_other_projects(tmp_path, monkeypatch):
    a, b = session(tmp_path / 'a'), session(tmp_path / 'b')
    broken, _ = ce.bound_engine(a), ce.bound_engine(b)
    original = ce._shutdown
    def fail(built):
        if built is broken:
            raise RuntimeError('fixture close failure')
        original(built)
    with monkeypatch.context() as scoped:
        scoped.setattr(ce, '_shutdown', fail)
        ce.close_all()
    assert storage.directory(storage.project(a)).exists()
    assert not storage.directory(storage.project(b)).exists()


def test_audit_archive_import_does_not_rebind_live_lifecycle(tmp_path):
    ctx = session(tmp_path / 'project')
    summary(ctx)
    target = carried(ctx)
    ce.close_all()
    ce.sync(target)
    built = ce.bound_engine(target)
    state = built._lifecycle.get_by_conversation(built._conversation_id)
    assert state.current_session_id == target.sessionManager.getSessionId()


def test_audit_cleaned_carry_sources_rebuild_without_stale_node_hint(tmp_path):
    from misaka.extensions.misaka_lcm.vendor.command import (
        _delete_clean_candidates_atomically,
    )
    original = session(tmp_path / 'project')
    summary(original)
    target = carried(original)
    ce.close_all()
    for _ in range(3):
        ce.sync(target)
        built = ce.bound_engine(target)
        snap = ce.snapshot(target)
        nodes, _ = ce._checkpoint_nodes(built, ce._checkpoint(snap), snap)
        assert nodes and all(built._store.get_batch(carry.source_ids(built, node)) for node in nodes)
        _delete_clean_candidates_atomically(built, {original.sessionManager.getSessionId()})
        # A same-process resume keeps this project's cache, but must rebuild
        # archive-backed references removed by explicit operator maintenance.
        ce.close(target)


async def test_audit_all_tools_use_project_scope_and_mark_host_errors(tmp_path, monkeypatch):
    from misaka.extensions.misaka_lcm.host import tools
    from misaka.extensions.misaka_lcm.vendor.engine import LCMEngine
    ctx = session(tmp_path / 'project', execution=tmp_path / 'worktree')
    observed = []
    def answer(name, args, transcript, scoped):
        observed.append((name, args, transcript.project, str(storage.project(scoped))))
        return '{"ok":true}'
    monkeypatch.setattr(tools, '_answer', answer)
    for schema in LCMEngine.get_tool_schemas():
        definition = tools._definition(schema, workspace=ctx.lcm_project)
        result = await definition.execute('fixture-call', {}, None, None, ctx._ctx)
        assert result['content'][0]['text'] == '{"ok":true}'
        assert observed[-1] == (schema['name'], {}, ctx.lcm_project, ctx.lcm_project)
    assert len(observed) == 15
    def fail(*args):
        raise RuntimeError('fixture host error')
    monkeypatch.setattr(tools, '_answer', fail)
    result = await definition.execute('fixture-call', {}, None, None, ctx._ctx)
    assert result.get('isError') is True


def test_audit_abbreviated_apply_initializes_generation_ids(tmp_path, monkeypatch):
    import sqlite3
    from contextlib import closing

    from misaka.extensions.misaka_lcm.host import operators
    from misaka.extensions.misaka_lcm.vendor.scripts import (
        import_lossless_claw as importer,
    )
    monkeypatch.chdir(tmp_path)
    def observe(options):
        args = importer._build_parser().parse_args(options)
        assert args.apply
        with closing(sqlite3.connect(f'file:{args.target_db}?mode=ro', uri=True)) as connection:
            assert connection.execute("SELECT seq FROM sqlite_sequence WHERE name='messages'").fetchone()[0] > 2**32
        return 0
    monkeypatch.setattr(importer, 'main', observe)
    assert operators.main(['import', '--source-jsonl', str(tmp_path / 'source.jsonl'), '--app']) == 0
    assert not storage.directory(tmp_path).exists()


def test_audit_six_real_engines_share_until_final_process_exit(tmp_path):
    import sqlite3
    from contextlib import closing
    project = tmp_path / 'project'
    code = '''
import sys
from types import SimpleNamespace
from misaka.core.session_manager import SessionManager
from misaka.extensions.misaka_lcm.host import context_engine as ce
ctx = SimpleNamespace(cwd=sys.argv[1], model=None, sessionManager=SessionManager.inMemory(sys.argv[1]))
ctx.sessionManager.appendMessage({'role':'user','content':'PROCESS_'+sys.argv[2], 'timestamp':1789460410000})
ce.sync(ctx)
print('ready',flush=True)
sys.stdin.readline()
ce.close(ctx)
ce.release_project(ctx)
'''
    processes = [subprocess.Popen([sys.executable, '-c', code, str(project), str(i)], text=True,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for i in range(6)]
    try:
        for process in processes:
            assert select.select([process.stdout], [], [], 30)[0], 'child startup timed out'
            assert process.stdout.readline().strip() == 'ready', process.stderr.read()
        root = storage.directory(project)
        with closing(sqlite3.connect(f'file:{root / "lcm.db"}?mode=ro', uri=True)) as connection:
            assert connection.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 6
        for process in processes[:-1]:
            _, error = process.communicate('quit\n', timeout=30)
            assert process.returncode == 0, error
            assert root.exists(), 'another live process still owns this cache'
        _, error = processes[-1].communicate('quit\n', timeout=30)
        assert processes[-1].returncode == 0, error
        assert not root.exists()
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)


@pytest.mark.parametrize('worker', ['rollup', 'assertion', 'fts'])
def test_audit_shutdown_timeout_retains_live_owner(tmp_path, monkeypatch, worker):
    import threading

    from misaka.extensions.misaka_lcm.vendor import db_bootstrap
    ctx = session(tmp_path / 'project')
    built = ce.bound_engine(ctx)
    unblock = threading.Event()
    thread = None
    with monkeypatch.context() as scoped:
        if worker == 'rollup':
            def drain(timeout=None):
                assert timeout is not None and timeout <= 25
                return False
            scoped.setattr(built, 'drain_rollup_maintenance', drain)
        elif worker == 'assertion':
            def wait(timeout=None):
                assert timeout is not None and timeout <= 25
                return False
            scoped.setattr(built._assertion_extraction_idle, 'wait', wait)
        else:
            thread = threading.Thread(target=unblock.wait)
            thread.start()
            scoped.setattr(db_bootstrap, '_integrity_scan_threads',
                           {(str(built._store.db_path), 'fixture'): thread})
        try:
            with pytest.raises(TimeoutError):
                ce._shutdown(built, timeout=0.01)
            assert ce._ENGINES[ce._key(ctx)] is built
            assert built._store._conn.execute('SELECT 1').fetchone()[0] == 1
            assert storage.directory(storage.project(ctx)).exists()
        finally:
            unblock.set()
            if thread is not None:
                thread.join(1)
    ce.close(ctx)
    ce.release_project(ctx)
    assert not storage.directory(storage.project(ctx)).exists()


def test_audit_shutdown_does_not_wait_for_other_projects_fts(tmp_path, monkeypatch):
    import threading

    from misaka.extensions.misaka_lcm.vendor import db_bootstrap
    a, b = session(tmp_path / 'a'), session(tmp_path / 'b')
    ce.bound_engine(a)
    other = ce.bound_engine(b)
    unblock, closed = threading.Event(), threading.Event()
    thread = threading.Thread(target=unblock.wait)
    thread.start()
    monkeypatch.setattr(db_bootstrap, '_integrity_scan_threads',
                        {(str(other._store.db_path), 'fixture'): thread})
    def quit():
        ce.close(a)
        ce.release_project(a)
        closed.set()
    closer = threading.Thread(target=quit)
    closer.start()
    try:
        assert closed.wait(1), 'project A waited for unrelated project B background scan'
        assert not storage.directory(storage.project(a)).exists()
        assert storage.directory(storage.project(b)).exists()
    finally:
        unblock.set()
        thread.join(2)
        closer.join(2)


def test_status_separates_native_catalog_from_loaded_lcm_history(tmp_path, monkeypatch):
    from misaka.extensions.misaka_lcm.host import native, tools
    old = session(tmp_path / 'project')
    summary(old)
    archive = Path(old.sessionManager.getSessionFile())
    before = archive.read_bytes()
    ce.close(old)
    ce.release_project(old)
    current = session(tmp_path / 'project')
    say(current, 'current session only')
    monkeypatch.setattr(native, 'session_ids', lambda: {
        old.sessionManager.getSessionId(), current.sessionManager.getSessionId()})
    status = json.loads(tools._answer('lcm_status', {}, ce.snapshot(current), current))
    fragmentation = status['lifecycle_fragmentation']
    assert fragmentation['state_sessions_total'] == 2
    assert fragmentation['state_sessions_missing_in_lcm_any'] == 1
    assert fragmentation['distinct_lcm_any_sessions'] == 1
    assert status['store']['messages'] == 1
    assert status['dag']['total_nodes'] == 0
    output = ce.bound_engine(current).handle_tool_call('lcm_grep', {'pattern': 'alpha-project', 'scope': 'all'})
    assert 'alpha-project' not in output
    assert archive.read_bytes() == before
    identity = status['runtime_identity']
    assert identity['history_scope'] == 'loaded-project-cache'
    assert identity['native_session_catalog_contains'] == 'session-identifiers-not-conversation-content'
    guideline = tools.recall_guideline()
    assert 'Tool availability is not evidence' in guideline
    assert 'state_only_sessions' in guideline
    assert 'final project owner exits' in guideline

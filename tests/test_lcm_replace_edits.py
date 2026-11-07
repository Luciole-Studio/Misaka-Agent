"""A context edit replaces what the model sees, and the LCM copy of that row stays as it was.

The edit is an entry of its own in the session file, so reopening, rebuilding the cache and
compacting around it must all give the same rows without clearing the session.
"""

import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from misaka.core.session_manager import SessionManager
from misaka.extensions.misaka_lcm.host import context_engine as ce
from misaka.extensions.misaka_lcm.host import storage
from misaka.extensions.misaka_lcm.vendor import engine as upstream


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MISAKA_HOME", str(tmp_path / "home"))
    ce.close_all()
    yield
    ce.close_all()


def start(tmp_path):
    p = tmp_path / "project"
    p.mkdir()
    m = SessionManager.create(str(p), str(tmp_path / "sessions"))
    m.appendCustomEntry("lcm-project", {"workspace": str(p.resolve())})
    return storage.context(NS(cwd=str(p), sessionManager=m, model=None), str(p), "test")


def say(c, text, t=0):
    return c.sessionManager.appendMessage({"role": "user", "content": text, "timestamp": 1789460410000 + t})


def reply(c, text, t=0):
    return c.sessionManager.appendMessage(
        {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "api": "a",
            "provider": "p",
            "model": "m",
            "usage": {
                "input": 0,
                "output": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
                "totalTokens": 0,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
            },
            "stopReason": "stop",
            "timestamp": 1789460410000 + t,
        }
    )


def rows(c):
    b = ce.bound_engine(c)
    return [
        r[0]
        for r in b._store._conn.execute(
            "select content from messages where session_id=? order by store_id", (b.current_session_id,)
        )
    ]


def reopen(c):
    src = c.sessionManager.getSessionFile()
    c.sessionManager.rewrite_file()
    ce.close_all()
    m = SessionManager.open(src, str(Path(src).parent))
    n = storage.context(NS(cwd=c.cwd, sessionManager=m, model=None), c.lcm_project)
    ce.sync(n)
    return n


def held(c):
    b = ce.bound_engine(c)
    originals = [x for g in ce._session_originals(ce.snapshot(c)).values() for x in g]
    return ce._rows_held(b, originals), len(originals)


def spy_clear(monkeypatch):
    seen = []
    real = ce._clear_session_rows
    monkeypatch.setattr(ce, "_clear_session_rows", lambda *a, **k: (seen.append(a[2]), real(*a, **k))[1])
    return seen


NEW = {"role": "user", "content": "REPLACED text", "timestamp": 1789460419000}


def test_edit_after_ingest_keeps_the_original_row_and_is_stable(tmp_path, monkeypatch):
    c = start(tmp_path)
    seen = spy_clear(monkeypatch)
    a = say(c, "first original")
    say(c, "second", 1)
    ce.sync(c)
    before = rows(c)
    c.sessionManager.appendContextEdit(a, NEW)
    ce.sync(c)
    assert rows(c) == before
    n = reopen(c)
    assert rows(n) == before and not seen
    assert held(n) == (len(before), len(before))
    say(n, "third", 2)
    ce.sync(n)
    assert len(rows(n)) == len(before) + 1 and not seen


def test_model_sees_the_replacement(tmp_path):
    c = start(tmp_path)
    a = say(c, "first original")
    say(c, "second", 1)
    ce.sync(c)
    c.sessionManager.appendContextEdit(a, NEW)
    ce.sync(c)
    seen = json.dumps(ce._messages(ce.snapshot(c), ce.bound_engine(c)), default=str)
    assert "REPLACED text" in seen and "first original" not in seen


def test_edit_before_ingest_stores_the_replacement_and_is_stable(tmp_path, monkeypatch):
    c = start(tmp_path)
    seen = spy_clear(monkeypatch)
    a = say(c, "first original")
    c.sessionManager.appendContextEdit(a, NEW)
    say(c, "second", 1)
    ce.sync(c)
    before = rows(c)
    assert any("REPLACED" in r for r in before) and not any("first original" in r for r in before)
    n = reopen(c)
    assert rows(n) == before and not seen


def test_two_edits_on_one_target(tmp_path, monkeypatch):
    c = start(tmp_path)
    seen = spy_clear(monkeypatch)
    a = say(c, "first original")
    say(c, "second", 1)
    ce.sync(c)
    before = rows(c)
    c.sessionManager.appendContextEdit(a, NEW)
    c.sessionManager.appendContextEdit(a, {**NEW, "content": "AGAIN"})
    ce.sync(c)
    n = reopen(c)
    assert rows(n) == before and not seen
    assert "AGAIN" in json.dumps(ce._messages(ce.snapshot(n), ce.bound_engine(n)), default=str)


def test_replace_then_drop_same_target(tmp_path, monkeypatch):
    c = start(tmp_path)
    seen = spy_clear(monkeypatch)
    a = say(c, "first original")
    say(c, "second", 1)
    ce.sync(c)
    before = rows(c)
    c.sessionManager.appendContextEdit(a, NEW)
    c.sessionManager.appendContextEdit(a, None)
    ce.sync(c)
    n = reopen(c)
    assert rows(n) == before and not seen


def test_replacing_an_assistant_row(tmp_path, monkeypatch):
    c = start(tmp_path)
    seen = spy_clear(monkeypatch)
    say(c, "question")
    a = reply(c, "answer original", 1)
    say(c, "next", 2)
    ce.sync(c)
    before = rows(c)
    c.sessionManager.appendContextEdit(
        a,
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "answer REPLACED"}],
            "api": "a",
            "provider": "p",
            "model": "m",
            "usage": {
                "input": 0,
                "output": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
                "totalTokens": 0,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
            },
            "stopReason": "stop",
            "timestamp": 1789460411500,
        },
    )
    ce.sync(c)
    n = reopen(c)
    assert rows(n) == before and not seen


def test_rebuild_from_the_session_file_gives_the_same_rows(tmp_path):
    c = start(tmp_path)
    a = say(c, "first original")
    say(c, "second", 1)
    ce.sync(c)
    c.sessionManager.appendContextEdit(a, NEW)
    e = say(c, "third", 2)
    c.sessionManager.appendContextEdit(e, {**NEW, "content": "THIRD REPLACED"})
    ce.sync(c)
    before = rows(c)
    src = c.sessionManager.getSessionFile()
    c.sessionManager.rewrite_file()
    ce.close(c)
    ce.release_project(c)
    assert storage.clear(storage.project(c))
    m = SessionManager.open(src, str(Path(src).parent))
    n = storage.context(NS(cwd=c.cwd, sessionManager=m, model=None), c.lcm_project)
    ce.sync(n)
    assert rows(n) == before


def compacted(c, monkeypatch):
    b = ce.bound_engine(c)
    b._config.fresh_tail_count = 2
    b._config.leaf_chunk_tokens = 40
    monkeypatch.setattr(
        upstream,
        "summarize_with_escalation",
        lambda **_: ("Summary of ALPHA. [Expand for details: original]", 1),
    )
    ids = [say(c, f"ALPHA evidence {i} " + "material " * 50, i) for i in range(12)]
    native = c.sessionManager.buildSessionContext().messages
    r = ce.compact(ce.prepare({"reason": "manual", "messages": native}, c))
    c.sessionManager.appendCompaction(
        r.summary, r.firstKeptEntryId, r.tokensBefore, details=r.details, contextMessages=r.contextMessages
    )
    return ids, r


def test_edit_inside_a_compacted_region(tmp_path, monkeypatch):
    c = start(tmp_path)
    seen = spy_clear(monkeypatch)
    _, r = compacted(c, monkeypatch)
    source = r.details["lcm"]["nodes"][0]["sourceEntries"][0]
    c.sessionManager.appendContextEdit(source, NEW)
    ce.sync(c)
    before = rows(c)
    n = reopen(c)
    assert rows(n) == before and not seen
    b = ce.bound_engine(n)
    snap = ce.snapshot(n)
    assert ce._checkpoint_nodes(b, ce._checkpoint(snap), snap)


def test_edit_of_a_kept_tail_row_after_compaction(tmp_path, monkeypatch):
    c = start(tmp_path)
    seen = spy_clear(monkeypatch)
    ids, _ = compacted(c, monkeypatch)
    c.sessionManager.appendContextEdit(ids[-1], NEW)
    ce.sync(c)
    before = rows(c)
    n = reopen(c)
    assert rows(n) == before and not seen


def test_edit_in_a_compacted_region_survives_clearing_the_cache(tmp_path, monkeypatch):
    c = start(tmp_path)
    _, r = compacted(c, monkeypatch)
    source = r.details["lcm"]["nodes"][0]["sourceEntries"][0]
    c.sessionManager.appendContextEdit(source, NEW)
    ce.sync(c)
    before = rows(c)
    src = c.sessionManager.getSessionFile()
    c.sessionManager.rewrite_file()
    ce.close(c)
    ce.release_project(c)
    assert storage.clear(storage.project(c))
    m = SessionManager.open(src, str(Path(src).parent))
    n = storage.context(NS(cwd=c.cwd, sessionManager=m, model=None), c.lcm_project)
    ce.sync(n)
    assert rows(n) == before
    b = ce.bound_engine(n)
    snap = ce.snapshot(n)
    assert ce._checkpoint_nodes(b, ce._checkpoint(snap), snap)

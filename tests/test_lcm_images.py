"""An image stays in the session file, and LCM behaves as if it held it.

`my-research` kept 5.1 GB of its 5.4 GB cache in `lcm-large-outputs/`, every byte of it a second copy
of images the session files already hold, and nothing read them back. Leaving them out takes three
things, and the two that were first missed fail silently. Compaction maps messages to rows by
content, so a comparison that still sees the image drops those rows from what a summary covers (8%
of entries on real sessions). And `fence` calls a placeholder opaque, so a marker it can read through
takes the fence off every summary over an image row.
"""
from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest
from filelock import (
    ReadWriteLock,
    Timeout,  # noqa: F401
)

from misaka.core.platform.prompt_guard import MARKER
from misaka.core.session_manager import SessionManager
from misaka.extensions.misaka_lcm.host import carry, fence, images, storage
from misaka.extensions.misaka_lcm.host import context_engine as ce
from misaka.extensions.misaka_lcm.vendor import engine as upstream
from misaka.utils.values import read_field

PIXELS = base64.b64encode(b"\x89PNG not really an image " * 4000).decode()
USAGE = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0,
         "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MISAKA_HOME", str(tmp_path / "home"))
    ce.close_all()
    yield
    ce.close_all()


def start(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    manager = SessionManager.create(str(project), str(tmp_path / "sessions"))
    manager.appendCustomEntry("lcm-project", {"workspace": str(project.resolve())})
    return storage.context(SimpleNamespace(cwd=str(project), sessionManager=manager, model=None), str(project), "test")


def read_image(ctx, tick, data=PIXELS):
    """The tool call and result of reading an image; the result's entry id."""
    call = f"call_{tick}"
    ctx.sessionManager.appendMessage({
        "role": "assistant", "content": [{"type": "toolCall", "id": call, "name": "read", "arguments": {"path": "a.png"}}],
        "api": "a", "provider": "p", "model": "m", "usage": USAGE, "stopReason": "toolUse",
        "timestamp": 1789460410000 + tick})
    return ctx.sessionManager.appendMessage({
        "role": "toolResult", "toolCallId": call, "toolName": "read", "isError": False,
        "content": [{"type": "text", "text": f"Read image file {tick}"},
                    {"type": "image", "mimeType": "image/png", "data": data}],
        "timestamp": 1789460410000 + tick})


def say(ctx, text, tick):
    return ctx.sessionManager.appendMessage({"role": "user", "content": text, "timestamp": 1789460410000 + tick})


def rows(ctx):
    built = ce.bound_engine(ctx)
    return [row[0] for row in built._store._conn.execute(
        "SELECT content FROM messages WHERE session_id = ? ORDER BY store_id", (built.current_session_id,))]


def payloads(ctx):
    folder = storage.directory(Path(ctx.lcm_project)) / "lcm-large-outputs"
    return [path for path in folder.rglob("*") if path.is_file()] if folder.exists() else []


def compacted(ctx, monkeypatch, *, count=10):
    built = ce.bound_engine(ctx)
    built._config.fresh_tail_count = 2
    built._config.leaf_chunk_tokens = 40
    monkeypatch.setattr(upstream, "summarize_with_escalation",
                        lambda **_: ("Summary of the reading. [Expand for details: original]", 1))
    ids = []
    for index in range(count):
        say(ctx, f"look at page {index} " + "material " * 50, index * 10)
        ids.append(read_image(ctx, index * 10 + 1))
    native = ctx.sessionManager.buildSessionContext().messages
    return ids, ce.compact(ce.prepare({"reason": "manual", "messages": native}, ctx))


def reopen(ctx):
    source = ctx.sessionManager.getSessionFile()
    ctx.sessionManager.rewrite_file()
    ce.close_all()
    manager = SessionManager.open(source, str(Path(source).parent))
    again = storage.context(SimpleNamespace(cwd=ctx.cwd, sessionManager=manager, model=None), ctx.lcm_project)
    ce.sync(again)
    return again


def originals(ctx):
    return [message for group in ce._session_originals(ce.snapshot(ctx)).values() for message in group]


def test_an_image_reaches_neither_the_rows_nor_a_side_file(tmp_path):
    ctx = start(tmp_path)
    read_image(ctx, 1)
    ce.sync(ctx)
    stored = "\n".join(rows(ctx))
    assert PIXELS not in stored and "base64," not in stored
    assert "Read image file 1" in stored and "Externalized image: image/png" in stored
    assert payloads(ctx) == []


def test_only_an_inline_image_is_replaced():
    plain = {"role": "user", "content": "text"}
    linked = {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}]}
    assert images.without(plain) is plain and images.without(linked) is linked
    url = f"data:image/png;base64,{PIXELS}"
    message = {"role": "tool", "content": [{"type": "text", "text": "kept"}, {"type": "image_url", "image_url": {"url": url}}]}
    replaced = images.without(message)
    assert replaced["content"][0] == {"type": "text", "text": "kept"} and "base64" not in str(replaced)
    assert message["content"][1]["type"] == "image_url"
    assert images.without(replaced) is replaced


def test_the_same_image_gets_the_same_marker_and_another_a_different_one():
    def marker(data):
        return images.without({"content": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}]})
    assert marker(PIXELS) == marker(PIXELS) and marker(PIXELS) != marker(PIXELS[:-4] + "AAAA")


def test_the_marker_is_no_placeholder_upstream_would_try_to_load(tmp_path):
    from misaka.extensions.misaka_lcm.vendor.ingest_protection import (
        extract_all_externalized_payload_refs,
    )
    message = images.without({"content": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PIXELS}"}}]})
    assert extract_all_externalized_payload_refs(message["content"][0]["text"]) == []


def test_a_row_that_held_an_image_is_still_opaque_to_the_fence(tmp_path):
    ctx = start(tmp_path)
    read_image(ctx, 1)
    say(ctx, "plain words", 2)
    ce.sync(ctx)
    built = ce.bound_engine(ctx)
    held, plain = [row[0] for row in built._store._conn.execute(
        "SELECT store_id FROM messages WHERE session_id = ? AND role IN ('tool', 'user') ORDER BY store_id",
        (built.current_session_id,))]
    assert fence.is_tainted(built, store_ids=[held])
    assert not fence.is_tainted(built, store_ids=[plain])


def test_a_summary_over_an_image_row_keeps_its_fence(tmp_path, monkeypatch):
    ctx = start(tmp_path)
    _, result = compacted(ctx, monkeypatch)
    assert any(MARKER in str(read_field(message, "content")) for message in result.contextMessages)


def test_compaction_covers_the_rows_that_held_images(tmp_path, monkeypatch):
    ctx = start(tmp_path)
    ids, result = compacted(ctx, monkeypatch)
    covered = {entry for node in result.details["lcm"]["nodes"] for entry in node["sourceEntries"]}
    assert covered & set(ids), "a compaction left every image row out of its summaries"
    assert set(ids[:-1]) <= covered


def test_the_model_still_gets_the_image(tmp_path, monkeypatch):
    ctx = start(tmp_path)
    _, result = compacted(ctx, monkeypatch)
    blocks = [block for message in result.contextMessages for block in (read_field(message, "content") or [])
              if not isinstance(block, str) and read_field(block, "type") == "image"]
    assert blocks and all(read_field(block, "data") == PIXELS for block in blocks)


def test_comparison_finds_the_rows_that_held_images(tmp_path):
    ctx = start(tmp_path)
    for index in range(3):
        read_image(ctx, index)
    ce.sync(ctx)
    built = ce.bound_engine(ctx)
    messages = originals(ctx)
    assert built._reconcile_ingest_cursor_from_store(messages) == len(messages)
    assert len(built._get_store_id_map_for_messages(messages)) == len(messages)


def test_a_session_end_recognises_the_rows_that_held_images(tmp_path):
    ctx = start(tmp_path)
    for index in range(3):
        read_image(ctx, index)
    ce.sync(ctx)
    built = ce.bound_engine(ctx)
    messages = originals(ctx)
    assert built._session_end_store_prefix_count(built.current_session_id, messages) == len(messages)


def test_an_image_written_one_message_at_a_time_stays_out_too(tmp_path):
    ctx = start(tmp_path)
    say(ctx, "hello", 1)
    ce.sync(ctx)
    built = ce.bound_engine(ctx)
    url = f"data:image/png;base64,{PIXELS}"
    built._store.append(built.current_session_id, {"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]})
    built._store.append_batch(built.current_session_id, [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}])
    assert "base64," not in "\n".join(rows(ctx)) and payloads(ctx) == []


def test_nothing_is_stored_twice_through_a_reopen_and_a_session_end(tmp_path):
    ctx = start(tmp_path)
    for index in range(3):
        read_image(ctx, index)
    ce.sync(ctx)
    before = rows(ctx)
    again = reopen(ctx)
    assert rows(again) == before
    ce.end(again)
    assert rows(reopen(again)) == before


def test_a_session_with_images_carries_over(tmp_path, monkeypatch):
    ctx = start(tmp_path)
    _, result = compacted(ctx, monkeypatch)
    ctx.sessionManager.appendCompaction(result.summary, result.firstKeptEntryId, result.tokensBefore,
                                        details=result.details, contextMessages=result.contextMessages)
    ce.sync(ctx)
    ce.bound_engine(ctx)._config.new_session_retain_depth = -1
    target = SessionManager.create(ctx.cwd, ctx.sessionManager.getSessionDir())
    carry.rollover(target, ce.snapshot(ctx), ctx)
    carried = storage.context(SimpleNamespace(cwd=ctx.cwd, sessionManager=target, model=None), ctx.lcm_project)
    ce.sync(carried)
    built, snapshot = ce.bound_engine(carried), ce.snapshot(carried)
    nodes = ce._checkpoint_nodes(built, ce._checkpoint(snapshot), snapshot)
    assert nodes and len(built._store.get_batch(list(carry.sources(built, snapshot).values()))) > 0
    assert payloads(ctx) == []


def test_rows_with_images_rebuild_when_their_bindings_are_lost(tmp_path):
    ctx = start(tmp_path)
    for index in range(3):
        read_image(ctx, index)
    ce.sync(ctx)
    before = rows(ctx)
    ctx.sessionManager.rewrite_file()
    ce.close_all()
    storage.release_all()
    for path in storage.directory(Path(ctx.lcm_project)).glob("entry-rows.db*"):
        path.unlink()
    assert rows(reopen(ctx)) == before


def stale(ctx):
    """A cache as the format before this one left it, with a side file in it."""
    ce.sync(ctx)
    ce.close_all()
    storage.release_all()
    root = storage.directory(Path(ctx.lcm_project))
    (root / storage._MARKER).write_text("misaka-lcm\n", encoding="utf-8")
    (root / "lcm-large-outputs").mkdir(exist_ok=True)
    (root / "lcm-large-outputs" / "old-image").write_text("old", encoding="utf-8")
    return root


def test_a_cache_of_the_old_format_is_rebuilt(tmp_path):
    ctx = start(tmp_path)
    say(ctx, "hello", 1)
    root = stale(ctx)
    storage.acquire(Path(ctx.lcm_project))
    assert not (root / "lcm-large-outputs" / "old-image").exists()
    assert not list(root.parent.glob("lcm.unrecognized-*"))
    assert (root / storage._MARKER).read_text(encoding="utf-8") == storage._STAMP
    storage.release(Path(ctx.lcm_project))


def test_a_cache_of_the_old_format_in_use_is_left_alone(tmp_path):
    ctx = start(tmp_path)
    say(ctx, "hello", 1)
    root = stale(ctx)
    _, _, lock_path = storage._paths(Path(ctx.lcm_project))
    other = ReadWriteLock(lock_path, is_singleton=False)
    other.acquire_read(timeout=5)
    try:
        storage.acquire(Path(ctx.lcm_project))
        assert (root / "lcm-large-outputs" / "old-image").exists()
        storage.release(Path(ctx.lcm_project))
    finally:
        other.close()
    storage.acquire(Path(ctx.lcm_project))
    assert not (root / "lcm-large-outputs" / "old-image").exists()
    storage.release(Path(ctx.lcm_project))


def test_a_cache_of_this_format_is_kept(tmp_path):
    ctx = start(tmp_path)
    say(ctx, "hello", 1)
    ce.sync(ctx)
    before = rows(ctx)
    assert rows(reopen(ctx)) == before

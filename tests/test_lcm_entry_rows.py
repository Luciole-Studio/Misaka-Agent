"""Stored rows are bound to the session entries they came from when they are written (entry_rows),
not recovered by matching content. Content matching is upstream's best effort: identical rows are
dealt out by count, and a row whose content upstream rewrites matches nothing, so a checkpoint's
source could map to no row and fail the turn (2026-10-25 card X1, "LCM checkpoint source entries
are absent"; 2026-10-26 Last Order, R42). A row with no binding is still matched by content."""
import sqlite3
from contextlib import closing

from test_project_lcm import (  # noqa: F401
    ce,
    count,
    failed_attempt,
    isolated,
    say,
    session,
    summary,
)

from misaka.extensions.misaka_lcm.host import entry_rows


def drop_bindings(built):
    entry_rows._connection(built._store).execute("DELETE FROM rows")


def tool_turn(ctx, call, output, tick):
    ctx.sessionManager.appendMessage({
        "role": "assistant", "content": [{"type": "toolCall", "id": call, "name": "read", "arguments": {}}],
        "api": "a", "provider": "p", "model": "m", "stopReason": "toolUse", "timestamp": 1789460430000 + tick,
        "usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0,
                  "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}}})
    ctx.sessionManager.appendMessage({"role": "toolResult", "toolCallId": call, "toolName": "read",
                                      "content": [{"type": "text", "text": output}], "isError": False,
                                      "timestamp": 1789460430001 + tick})


def test_each_entry_maps_to_the_rows_written_for_it(tmp_path):
    ctx = session(tmp_path / "project")
    say(ctx, "question")
    tool_turn(ctx, "c1", "same output", 1)
    tool_turn(ctx, "c2", "same output", 2)                      # identical content, its own entry
    failed_attempt(ctx, 3, retried=False)
    say(ctx, "after", tick=4)
    ce.sync(ctx)
    built, snapshot = ce.bound_engine(ctx), ce.snapshot(ctx)
    projected, bound = ce._archive_map(built, snapshot)
    drop_bindings(built)
    _, matched = ce._archive_map(built, snapshot)
    assert bound == matched                                     # the binding agrees with content where content can tell
    rows = [row for entry, group in projected.items() if group for row in bound[entry]]
    assert len(rows) == len(set(rows)) == count(built)


def test_a_source_whose_content_upstream_rewrote_still_maps(tmp_path):
    """Upstream rewrites a stored row's content when it externalizes a payload
    (gc_externalized_tool_result); matching by content then finds no row for that entry."""
    ctx = session(tmp_path / "project")
    summary(ctx)
    built = ce.bound_engine(ctx)
    checkpoint = ce._checkpoint(ce.snapshot(ctx))
    source = checkpoint["details"]["lcm"]["nodes"][0]["sourceEntries"][0]
    _, rows = ce._archive_map(built, ce.snapshot(ctx))
    built._store._conn.execute("UPDATE messages SET content = ? WHERE store_id = ?",
                               ("[externalized payload]", rows[source][0]))
    built._store._conn.commit()
    say(ctx, "next", tick=1)
    ce.sync(ctx)                                                # the request's _checkpoint_nodes
    nodes, _ = ce._checkpoint_nodes(built, ce._checkpoint(ce.snapshot(ctx)), ce.snapshot(ctx))
    assert nodes
    drop_bindings(built)                                        # what content matching alone made of it
    try:
        ce._checkpoint_nodes(built, ce._checkpoint(ce.snapshot(ctx)), ce.snapshot(ctx))
    except ValueError as error:
        assert "absent" in str(error)
    else:
        raise AssertionError("content matching mapped a row whose content changed")


def test_a_row_written_twice_maps_once_and_its_copy_is_nobodys(tmp_path):
    ctx = session(tmp_path / "project")
    say(ctx, "question")
    ce.sync(ctx)
    built = ce.bound_engine(ctx)
    entry = next(item for item in ce.snapshot(ctx).entries if item["type"] == "message")
    message = ce._session_originals(ce.snapshot(ctx))[entry["id"]][0]
    copy = built._store._append_protected_batch(built._session_id, [message], [1])[0]   # a rebind's duplicate
    say(ctx, "question", tick=1)                                # identical text, a different entry, unbound
    ce.sync(ctx)
    snapshot = ce.snapshot(ctx)
    later = [item for item in snapshot.entries if item["type"] == "message"][-1]
    _, rows = ce._archive_map(built, snapshot)
    assert rows[entry["id"]] != [copy] and len(rows[entry["id"]]) == 1
    assert copy not in rows[later["id"]]


def test_bindings_never_reach_the_session_file(tmp_path):
    ctx = session(tmp_path / "project")
    say(ctx, "question")
    tool_turn(ctx, "c1", "output", 1)
    summary(ctx)
    say(ctx, "next", tick=2)
    ce.sync(ctx)
    native = ctx.sessionManager.buildSessionContext().messages
    ce.compact(ce.prepare({"reason": "manual", "messages": native}, ctx))
    with open(ctx.sessionManager.getSessionFile(), encoding="utf-8") as handle:
        assert entry_rows.KEY not in handle.read()


def test_an_unchanged_context_prepares_nothing(tmp_path):
    ctx = session(tmp_path / "project")
    say(ctx, "question")
    tool_turn(ctx, "c1", "output", 1)
    ce.sync(ctx)
    native = ctx.sessionManager.buildSessionContext().messages
    for _ in range(2):                                          # the second meets upstream's cached replay
        assert ce.prepare({"messages": native, "reason": "auto", "allowCompression": False}, ctx) is None


def test_the_session_end_flush_binds_what_it_writes(tmp_path):
    ctx = session(tmp_path / "project")
    say(ctx, "question")
    ce.sync(ctx)
    built = ce.bound_engine(ctx)
    say(ctx, "written only by the final flush", tick=1)
    store = built._store
    ce.end(ctx)
    later = [item for item in ce.snapshot(ctx).entries if item["type"] == "message"][-1]
    with closing(sqlite3.connect(str(store.db_path))) as con:          # end() closed the engine
        ids = [row[0] for row in con.execute("SELECT store_id FROM messages")]
    bound, _ = entry_rows.bound(store, ids)
    assert later["id"] in bound

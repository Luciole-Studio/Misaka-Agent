"""Focused provider-free tests for bounded V4 assertion rebuilds."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from hermes_lcm.assertion_rebuild import (
    AssertionExtraction,
    rebuild_assertions,
)
from hermes_lcm.assertion_store import AssertionCandidate, AssertionStore
from hermes_lcm.command import handle_lcm_command
from hermes_lcm.config import LCMConfig
from hermes_lcm.store import MessageStore


def _extract_preference(snapshot) -> AssertionExtraction:
    quote = "tea"
    if snapshot.role != "user" or quote not in snapshot.content:
        return AssertionExtraction()
    start = snapshot.content.index(quote)
    return AssertionExtraction(assertions=(AssertionCandidate(
        source_span_start=start,
        source_span_end=start + len(quote),
        subject_key="user",
        predicate_key="prefers",
        object_value=quote,
        value_text=quote,
        kind="preference",
    ),))


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    }


def test_read_only_rebuild_plan_never_invokes_extractor_or_writes(tmp_path):
    db_path = tmp_path / "lcm.db"
    messages = MessageStore(db_path)
    assertions = AssertionStore(db_path)
    messages.append(
        "session-a", {"role": "user", "content": "I prefer tea ☕."}
    )
    assertions.commit()
    assertions.close()
    invoked = False

    def forbidden_extractor(_snapshot):
        nonlocal invoked
        invoked = True
        raise AssertionError("dry-run invoked the extractor")

    reader = AssertionStore(db_path, read_only=True)
    try:
        before = reader.connection.total_changes
        result = rebuild_assertions(
            reader,
            apply=False,
            extractor=forbidden_extractor,
            limit=10,
        )
        assert result.mode == "dry-run"
        assert result.pending_count == result.selected_count == 1
        assert result.processed_count == result.assertions_written == 0
        assert reader.connection.total_changes == before == 0
        assert invoked is False
    finally:
        reader.close()
        messages.close()


def test_apply_is_resumable_idempotent_and_rebuild_digest_stable(tmp_path):
    db_path = tmp_path / "lcm.db"
    messages = MessageStore(db_path)
    assertions = AssertionStore(db_path)
    first_id = messages.append(
        "session-a", {"role": "user", "content": "I prefer tea."}
    )
    second_id = messages.append(
        "session-b", {"role": "user", "content": "tea is my favorite drink."}
    )
    raw_before = messages._conn.execute(
        "SELECT store_id, session_id, role, content, timestamp FROM messages ORDER BY store_id"
    ).fetchall()
    fts_before = messages._conn.execute(
        "SELECT rowid, content FROM messages_fts ORDER BY rowid"
    ).fetchall()

    first = rebuild_assertions(
        assertions, apply=True, extractor=_extract_preference, limit=2
    )
    assert first.pending_count == first.selected_count == 2
    assert first.processed_count == 2
    assert first.assertions_written == 2
    assert first.remaining_count == 0
    assert {row["source_store_id"] for row in assertions.query_assertions()} == {
        first_id,
        second_id,
    }

    repeat = rebuild_assertions(
        assertions, apply=True, extractor=_extract_preference, limit=2
    )
    assert repeat.pending_count == repeat.selected_count == 0
    assert repeat.processed_count == repeat.assertions_written == 0

    assertions.remove_version(first.extraction_version)
    rebuilt = rebuild_assertions(
        assertions, apply=True, extractor=_extract_preference, limit=2
    )
    assert rebuilt.receipt_digest == first.receipt_digest
    assert rebuilt.assertions_written == 2
    assert messages._conn.execute(
        "SELECT store_id, session_id, role, content, timestamp FROM messages ORDER BY store_id"
    ).fetchall() == raw_before
    assert messages._conn.execute(
        "SELECT rowid, content FROM messages_fts ORDER BY rowid"
    ).fetchall() == fts_before
    assertions.close()
    messages.close()


def test_apply_records_zero_results_and_continues_after_failure(tmp_path):
    db_path = tmp_path / "lcm.db"
    messages = MessageStore(db_path)
    assertions = AssertionStore(db_path)
    bad_id = messages.append(
        "session-a", {"role": "user", "content": "bad source"}
    )
    good_id = messages.append(
        "session-a", {"role": "user", "content": "nothing to extract"}
    )

    def extractor(snapshot):
        if snapshot.store_id == bad_id:
            raise RuntimeError("synthetic extractor failure")
        return AssertionExtraction()

    result = rebuild_assertions(
        assertions, apply=True, extractor=extractor, limit=10
    )
    assert result.processed_count == 1
    assert result.failed_count == 1
    assert result.remaining_count == 1
    assert result.failures[0].source_store_id == bad_id
    assert "synthetic extractor failure" in result.failures[0].error
    receipt = assertions.connection.execute(
        "SELECT assertion_count FROM lcm_assertion_sources WHERE source_store_id = ?",
        (good_id,),
    ).fetchone()
    assert receipt is not None and receipt[0] == 0
    assertions.close()
    messages.close()


def test_command_is_default_off_and_does_not_materialize_schema(tmp_path):
    db_path = tmp_path / "lcm.db"
    messages = MessageStore(db_path)
    config = LCMConfig(database_path=str(db_path))
    engine = SimpleNamespace(_config=config, _store=messages, _assertions=None)
    before = _table_names(messages._conn)

    output = handle_lcm_command("assertions rebuild --limit 5", engine)

    assert "status: disabled" in output
    assert _table_names(messages._conn) == before
    assert not any(name.startswith("lcm_assertion") for name in before)
    messages.close()


def test_command_dry_run_and_explicit_injected_apply_are_bounded(tmp_path):
    db_path = tmp_path / "lcm.db"
    messages = MessageStore(db_path)
    config = LCMConfig(database_path=str(db_path), assertions_enabled=True)
    assertions = AssertionStore(db_path)
    messages.append(
        "session-a", {"role": "user", "content": "I prefer tea."}
    )
    engine = SimpleNamespace(
        _config=config,
        _store=messages,
        _assertions=assertions,
    )

    dry_run = handle_lcm_command("assertions rebuild --limit 1", engine)
    assert "status: complete" in dry_run
    assert "mode: dry-run" in dry_run
    assert "pending: 1" in dry_run
    assert "selected: 1" in dry_run
    assert "note: read-only dry-run; extractor was not invoked" in dry_run
    assert assertions.connection.execute(
        "SELECT COUNT(*) FROM lcm_assertion_sources"
    ).fetchone()[0] == 0

    refused = handle_lcm_command("assertions rebuild --apply", engine)
    assert "status: refused" in refused
    assert "no structured assertion extractor is configured" in refused
    assert assertions.connection.execute(
        "SELECT COUNT(*) FROM lcm_assertion_sources"
    ).fetchone()[0] == 0

    arbitrary_version = handle_lcm_command(
        "assertions rebuild --version assertions-v2", engine
    )
    assert "unsupported assertion rebuild argument: --version" in arbitrary_version
    assert assertions.connection.execute(
        "SELECT COUNT(*) FROM lcm_assertion_sources"
    ).fetchone()[0] == 0

    engine._assertion_extractor = _extract_preference
    applied = handle_lcm_command(
        "assertions rebuild --apply --limit=1", engine
    )
    assert "status: complete" in applied
    assert "mode: apply" in applied
    assert "processed: 1" in applied
    assert "assertions_written: 1" in applied
    assert "remaining: 0" in applied
    assertions.close()
    messages.close()

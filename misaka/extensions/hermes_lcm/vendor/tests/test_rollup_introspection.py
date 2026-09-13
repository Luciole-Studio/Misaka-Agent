from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

import pytest

import hermes_lcm.command as command_module
import hermes_lcm.engine as engine_module
import hermes_lcm.rollup_builder as builder_module
from hermes_lcm import tools as lcm_tools
from hermes_lcm.command import handle_lcm_command
from hermes_lcm.config import LCMConfig
from hermes_lcm.dag import SummaryNode
from hermes_lcm.engine import LCMEngine
from hermes_lcm.rollup_store import RollupStore
from hermes_lcm.tokens import count_tokens


@pytest.fixture
def engine(tmp_path):
    config = LCMConfig(
        database_path=str(tmp_path / "rollup-introspection.db"),
        temporal_rollups_enabled=True,
        rollup_builds_per_pass=2,
    )
    instance = LCMEngine(config=config, hermes_home=str(tmp_path / "home"))
    instance.on_session_start("rollup-session", conversation_id="rollup-conversation")
    assert instance.drain_rollup_maintenance(timeout=5.0)
    try:
        yield instance
    finally:
        instance.shutdown()


def _ready(store, kind, period_start, scope):
    token = store.upsert_building(kind, period_start, scope)
    store.mark_ready(token, f"old {kind}", 3, [], f"old-{kind}")
    return token.rollup_id


def _assert_zero_shape(block, *, enabled):
    assert block == {
        "enabled": enabled,
        "scope": "rollup-session",
        "counts": {
            kind: {status: 0 for status in ("ready", "stale", "building", "failed")}
            for kind in ("day", "week", "month")
        },
        "oldest_stale_age_seconds": None,
        "last_build_cursors": {kind: None for kind in ("day", "week", "month")},
        "last_built_at": {kind: None for kind in ("day", "week", "month")},
        "last_error": None,
    }


def test_lcm_inspect_rollup_block_is_well_formed_when_enabled_and_empty(engine):
    block = json.loads(lcm_tools.lcm_inspect({}, engine=engine))["temporal_rollups"]

    _assert_zero_shape(block, enabled=True)


def test_lcm_inspect_rollup_block_is_zeroed_when_feature_is_off(engine):
    engine._config.temporal_rollups_enabled = False

    block = json.loads(lcm_tools.lcm_inspect({}, engine=engine))["temporal_rollups"]

    _assert_zero_shape(block, enabled=False)


def test_lcm_inspect_reports_counts_cursor_stale_age_and_last_error(engine):
    store = RollupStore(engine._dag.db_path)
    try:
        _ready(store, "day", "2026-07-15", engine.current_session_id)
        _ready(store, "week", "2026-07-13", engine.current_session_id)
        store.connection.execute(
            "UPDATE lcm_rollups SET status = 'stale' WHERE period_kind = 'week'"
        )
        failed_token = store.upsert_building(
            "month", "2026-07-01", engine.current_session_id
        )
        store.mark_failed(failed_token, "mocked summary failure")
        store.set_cursor("day", "2026-07-15", engine.current_session_id, built_at="2026-07-15T12:00:00+00:00")
        store.connection.commit()

        block = json.loads(lcm_tools.lcm_inspect({}, engine=engine))["temporal_rollups"]

        assert block["counts"]["day"]["ready"] == 1
        assert block["counts"]["week"]["stale"] == 1
        assert block["counts"]["month"]["failed"] == 1
        assert block["oldest_stale_age_seconds"] >= 0
        assert block["last_build_cursors"]["day"] == "2026-07-15"
        assert block["last_built_at"]["day"] == "2026-07-15T12:00:00+00:00"
        assert block["last_error"] == "mocked summary failure"
    finally:
        store.close()


def test_inspect_and_rollups_status_never_call_llm(engine, monkeypatch):
    monkeypatch.setattr(
        builder_module,
        "summarize_with_escalation",
        lambda *_args, **_kwargs: pytest.fail("status called the summarizer"),
    )

    inspect = json.loads(lcm_tools.lcm_inspect({}, engine=engine))
    result = handle_lcm_command("rollups", engine)

    assert inspect["temporal_rollups"]["enabled"] is True
    assert "LCM temporal rollups" in result
    assert "period | ready | stale | building | failed" in result
    assert "day | 0 | 0 | 0 | 0" in result
    assert "last_error: (none)" in result


def test_rollups_rebuild_marks_all_targets_stale_and_builds_bounded(engine, monkeypatch):
    scope = engine.current_session_id
    timestamp = datetime(2026, 7, 15, 12, tzinfo=timezone.utc).timestamp()
    summary = "source summary for bounded rebuild"
    node_id = engine._dag.add_node(
        SummaryNode(
            session_id=scope,
            depth=0,
            summary=summary,
            token_count=count_tokens(summary),
            source_token_count=count_tokens(summary) * 2,
            source_ids=[1],
            source_type="messages",
            created_at=timestamp,
            earliest_at=timestamp,
            latest_at=timestamp,
        )
    )
    store = RollupStore(engine._dag.db_path)
    try:
        for kind, start in (
            ("day", "2026-07-15"),
            ("week", "2026-07-13"),
            ("month", "2026-07-01"),
        ):
            rollup_id = _ready(store, kind, start, scope)
            store.connection.execute(
                "INSERT OR IGNORE INTO lcm_rollup_sources(rollup_id, node_id) VALUES(?, ?)",
                (rollup_id, node_id),
            )
        store.connection.commit()
        calls = []
        monkeypatch.setattr(
            builder_module,
            "summarize_with_escalation",
            lambda text, **_kwargs: (calls.append(text) or "rebuilt summary", 1),
        )

        result = handle_lcm_command("rollups rebuild all 2026-07-15", engine)

        statuses = [
            store.get_rollup(kind, start, scope)["status"]
            for kind, start in (
                ("day", "2026-07-15"),
                ("week", "2026-07-13"),
                ("month", "2026-07-01"),
            )
        ]
        assert statuses == ["ready", "ready", "stale"]
        assert len(calls) == 2
        assert "build_limit: 2" in result
        assert "- day 2026-07-15: ready" in result
        assert "- week 2026-07-13: ready" in result
        assert "- month 2026-07-01: stale (bounded; not attempted)" in result
    finally:
        store.close()


def test_rollups_rebuild_refuses_while_background_maintenance_is_active(
    engine,
    monkeypatch,
):
    scope = engine.current_session_id
    key = engine._rollup_maintenance_key(scope)
    started = threading.Event()
    release = threading.Event()

    def blocking_maintenance():
        started.set()
        if not release.wait(timeout=5):
            raise AssertionError("test did not release background maintenance")

    assert engine_module._ROLLUP_MAINTENANCE_SCHEDULER.schedule(
        key,
        blocking_maintenance,
    )
    assert started.wait(timeout=2)
    try:
        monkeypatch.setattr(
            command_module,
            "RollupStore",
            lambda *_args, **_kwargs: pytest.fail(
                "busy operator rebuild touched SQLite before acquiring its lease"
            ),
        )
        result = handle_lcm_command("rollups rebuild day 2026-07-15", engine)

        assert "status: busy" in result
        assert "background temporal rollup maintenance is active" in result
        assert "retry" in result.lower()
    finally:
        release.set()
        assert engine_module._ROLLUP_MAINTENANCE_SCHEDULER.drain({key}, timeout=5)


def test_rollups_rebuild_releases_operator_lease_when_store_open_fails(
    engine,
    monkeypatch,
):
    scope = engine.current_session_id
    key = engine._rollup_maintenance_key(scope)

    def fail_store_open(*_args, **_kwargs):
        raise RuntimeError("forced store-open failure")

    monkeypatch.setattr(command_module, "RollupStore", fail_store_open)

    result = handle_lcm_command("rollups rebuild day 2026-07-15", engine)

    assert "status: error" in result
    assert "forced store-open failure" in result
    assert engine_module._ROLLUP_MAINTENANCE_SCHEDULER.try_acquire_exclusive(key)
    engine_module._ROLLUP_MAINTENANCE_SCHEDULER.release_exclusive(key)


def test_rollups_rebuild_releases_operator_lease_when_store_close_fails(
    engine,
    monkeypatch,
):
    scope = engine.current_session_id
    key = engine._rollup_maintenance_key(scope)

    class CloseFailRollupStore(RollupStore):
        def close(self):
            super().close()
            raise RuntimeError("forced store-close failure")

    monkeypatch.setattr(command_module, "RollupStore", CloseFailRollupStore)

    with pytest.raises(RuntimeError, match="forced store-close failure"):
        handle_lcm_command("rollups rebuild day 2026-07-15", engine)

    assert engine_module._ROLLUP_MAINTENANCE_SCHEDULER.try_acquire_exclusive(key)
    engine_module._ROLLUP_MAINTENANCE_SCHEDULER.release_exclusive(key)


def test_rollups_rebuild_all_durably_seeds_unattempted_targets(engine, monkeypatch):
    # Regression for maintainer #391: targets beyond the per-pass budget must be
    # durably seeded as 'stale' rows (not absent) so later maintenance builds
    # them. Start with NO pre-existing rollup rows.
    scope = engine.current_session_id
    engine._config.rollup_builds_per_pass = 1
    timestamp = datetime(2026, 7, 15, 12, tzinfo=timezone.utc).timestamp()
    engine._dag.add_node(
        SummaryNode(
            session_id=scope,
            depth=0,
            summary="lone source",
            token_count=count_tokens("lone source"),
            source_token_count=4,
            source_ids=[1],
            source_type="messages",
            created_at=timestamp,
            earliest_at=timestamp,
            latest_at=timestamp,
        )
    )
    monkeypatch.setattr(
        builder_module,
        "summarize_with_escalation",
        lambda _text, **_kwargs: ("rebuilt", 1),
    )

    result = handle_lcm_command("rollups rebuild all 2026-07-15", engine)

    store = RollupStore(engine._dag.db_path)
    try:
        statuses = {
            kind: (store.get_rollup(kind, start, scope) or {}).get("status", "missing")
            for kind, start in (("day", "2026-07-15"), ("week", "2026-07-13"), ("month", "2026-07-01"))
        }
    finally:
        store.close()
    # Only one build ran (the day), but the unattempted week and month exist as
    # durable stale rows rather than being absent.
    assert statuses["day"] == "ready"
    assert statuses["week"] == "stale"
    assert statuses["month"] == "stale"
    assert "not attempted" in result


def test_rollups_rebuild_attempted_failure_is_not_reported_complete(engine, monkeypatch):
    # Maintainer #391 D1 repro: an attempted daily build that persists
    # status='failed' was still reported top-level 'status: complete' alongside
    # '- day ...: failed'. A failed attempted target must downgrade the top-level
    # status to 'partial' (or error), never 'complete'.
    scope = engine.current_session_id
    timestamp = datetime(2026, 7, 15, 12, tzinfo=timezone.utc).timestamp()
    engine._dag.add_node(
        SummaryNode(
            session_id=scope,
            depth=0,
            summary="source for a failing daily",
            token_count=count_tokens("source for a failing daily"),
            source_token_count=4,
            source_ids=[1],
            source_type="messages",
            created_at=timestamp,
            earliest_at=timestamp,
            latest_at=timestamp,
        )
    )

    def boom(_text, **_kwargs):
        raise RuntimeError("summarizer down")

    monkeypatch.setattr(builder_module, "summarize_with_escalation", boom)

    result = handle_lcm_command("rollups rebuild day 2026-07-15", engine)

    assert "status: complete" not in result
    assert "status: partial" in result
    assert "- day 2026-07-15: failed" in result


def test_rollups_rebuild_attempted_no_source_is_not_reported_complete(engine):
    result = handle_lcm_command("rollups rebuild day 2026-07-15", engine)

    assert "status: complete" not in result
    assert "status: partial" in result
    assert "- day 2026-07-15: no-source" in result


def test_rollups_rebuild_attempted_incomplete_aggregate_is_deferred(engine):
    scope = engine.current_session_id
    timestamp = datetime(2026, 7, 13, 12, tzinfo=timezone.utc).timestamp()
    engine._dag.add_node(
        SummaryNode(
            session_id=scope,
            depth=0,
            summary="Monday content without a ready daily",
            token_count=6,
            source_token_count=6,
            source_ids=[1],
            source_type="messages",
            created_at=timestamp,
            earliest_at=timestamp,
            latest_at=timestamp,
        )
    )

    result = handle_lcm_command("rollups rebuild week 2026-07-13", engine)

    assert "status: complete" not in result
    assert "status: partial" in result
    assert "- week 2026-07-13: deferred (incomplete:" in result


def test_rollup_operator_surfaces_bound_adversarial_error_text(engine):
    store = RollupStore(engine._dag.db_path)
    try:
        token = store.upsert_building(
            "day", "2026-07-15", engine.current_session_id
        )
        store.mark_failed(token, "x" * 50_000)
    finally:
        store.close()

    slash = handle_lcm_command("rollups", engine)
    inspect_raw = lcm_tools.lcm_inspect({"limit": 1}, engine=engine)
    inspect = json.loads(inspect_raw)

    assert len(slash) <= 20_000
    assert "truncated: true" in slash
    assert "truncated_fields: last_error" in slash
    assert len(inspect_raw) <= 20_000
    assert inspect["char_limit"] == 20_000
    assert inspect["truncated"] is True
    assert inspect["temporal_rollups"]["truncated_fields"] == ["last_error"]
    assert len(inspect["temporal_rollups"]["last_error"]) <= 1_000


def test_rollups_rebuild_multi_target_seed_is_atomic(engine, monkeypatch):
    # Maintainer #391 D2: the multi-target stale-seed must be transactional. If
    # seeding fails mid-batch, NO target may be left half-seeded (no missing
    # month with no row). Force the batch seed to fail and assert nothing seeded.
    scope = engine.current_session_id
    import hermes_lcm.rollup_store as rollup_store_module

    def boom(_self, _targets):
        raise sqlite3.OperationalError("seed boom")

    monkeypatch.setattr(rollup_store_module.RollupStore, "upsert_stale_many", boom)

    result = handle_lcm_command("rollups rebuild all 2026-07-15", engine)

    assert "status: error" in result
    store = RollupStore(engine._dag.db_path)
    try:
        for kind, start in (
            ("day", "2026-07-15"),
            ("week", "2026-07-13"),
            ("month", "2026-07-01"),
        ):
            assert store.get_rollup(kind, start, scope) is None
    finally:
        store.close()


def test_rollups_rebuild_respects_disabled_flag(engine, monkeypatch):
    engine._config.temporal_rollups_enabled = False
    monkeypatch.setattr(
        builder_module,
        "build_day",
        lambda *_args, **_kwargs: pytest.fail("disabled rebuild called a builder"),
    )

    result = handle_lcm_command("rollups rebuild day 2026-07-15", engine)

    assert "status: disabled" in result
    assert "temporal rollups are disabled" in result

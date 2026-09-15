"""Provider-free contract tests for V4.5 automatic pre-answer evidence."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest

from hermes_lcm.config import LCMConfig
from hermes_lcm.evidence_pack import normalize_question_date
from hermes_lcm.preanswer_evidence import build_preanswer_evidence
from hermes_lcm.store import MessageStore


def _engine(tmp_path):
    config = LCMConfig(database_path=str(tmp_path / "lcm.db"))
    store = MessageStore(config.database_path, ingest_protection_config=config)
    return SimpleNamespace(_config=config, _store=store, _assertions=None)


def _append(engine, content, *, observed_at=None, session_id="session-a"):
    message = {"role": "user", "content": content}
    if observed_at is not None:
        message["timestamp"] = observed_at
    store_id = engine._store.append(session_id, message)
    return {
        "exact_ref": f"lcm:{store_id}:0-{len(content)}",
        "quote": content,
    }


def _result(engine, question, *, refs=(), retrieve=None, **kwargs):
    return build_preanswer_evidence(
        question,
        engine=engine,
        baseline_refs=list(refs),
        retrieve=retrieve,
        **kwargs,
    )


def test_preanswer_feature_flag_defaults_off_and_parses_env(monkeypatch):
    monkeypatch.delenv("LCM_PREANSWER_EVIDENCE_ENABLED", raising=False)
    assert LCMConfig.from_env().preanswer_evidence_enabled is False
    monkeypatch.setenv("LCM_PREANSWER_EVIDENCE_ENABLED", "true")
    assert LCMConfig.from_env().preanswer_evidence_enabled is True


def test_human_question_date_with_valid_weekday_reaches_product_planner(tmp_path):
    engine = _engine(tmp_path)
    taxi = _append(engine, "The taxi cost $60.", observed_at=1_680_000_000)
    train = _append(engine, "The train cost $20.", observed_at=1_680_000_100)
    try:
        result = _result(
            engine,
            "What is the total of the two costs?",
            refs=[taxi, train],
            enabled=True,
            question_date="2023/06/06 (Tue) 08:19",
        )
        invalid_weekday = _result(
            engine,
            "What is the total of the two costs?",
            refs=[taxi, train],
            enabled=True,
            question_date="2023/06/06 (Wed) 08:19",
        )
    finally:
        engine._store.close()

    assert result["status"] == "computed"
    normalized, error = normalize_question_date("2023/06/06 (Tue) 08:19")
    assert error is None
    assert normalized is not None
    assert normalized.public_dict() == {
        "input": "2023/06/06 (Tue) 08:19",
        "date": "2023-06-06",
        "normalization": "weekday_date_component",
    }
    assert invalid_weekday["reason_code"] == "question_date_invalid"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"enabled": False}, "feature_disabled"),
        (
            {"enabled": True, "context_engine_enabled": False},
            "context_engine_toolset_disabled",
        ),
        ({"enabled": True}, "question_required"),
    ],
)
def test_inert_paths_preserve_ordinary_answer(tmp_path, kwargs, reason):
    engine = _engine(tmp_path)
    try:
        result = _result(
            engine,
            "" if reason == "question_required" else "Where do I live now?",
            **kwargs,
        )
    finally:
        engine._store.close()

    assert result["status"] == "no_augmentation"
    assert result["reason_code"] == reason
    assert result["context"] is None
    assert result["retrieval"]["calls"] == 0


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (RuntimeError("recall failed"), "retrieval_error"),
        ({"timeout": True, "hits": []}, "retrieval_timeout"),
        ({"hits": []}, "no_hit"),
    ],
)
def test_retrieval_failure_paths_inject_nothing(tmp_path, payload, reason):
    engine = _engine(tmp_path)
    austin = _append(
        engine,
        "I used to live in Austin.",
        observed_at=datetime(2024, 3, 1, tzinfo=timezone.utc).timestamp(),
    )
    calls = []

    def retrieve(args):
        calls.append(args)
        if isinstance(payload, Exception):
            raise payload
        return json.dumps(payload)

    try:
        result = _result(
            engine,
            "Where do I live now?",
            refs=[austin],
            retrieve=retrieve,
            enabled=True,
        )
    finally:
        engine._store.close()

    assert len(calls) == 1
    assert result["status"] == "no_augmentation"
    assert result["reason_code"] == reason
    assert result["context"] is None
    assert result["retrieval"]["calls"] == 1


def test_latest_state_performs_one_named_targeted_delta_and_admits_only_novel_ref(
    tmp_path,
):
    engine = _engine(tmp_path)
    austin = _append(
        engine,
        "I used to live in Austin.",
        observed_at=datetime(2024, 3, 1, tzinfo=timezone.utc).timestamp(),
        session_id="old",
    )
    denver = _append(
        engine,
        "I moved to Denver and live there now.",
        observed_at=datetime(2024, 4, 5, tzinfo=timezone.utc).timestamp(),
        session_id="new",
    )
    calls = []

    def retrieve(args):
        calls.append(args)
        return json.dumps(
            {"hits": [denver, denver], "metrics": {"embedding_query_calls": 1}}
        )

    question = "Where do I live now?"
    try:
        result = _result(
            engine,
            question,
            refs=[austin, austin],
            retrieve=retrieve,
            enabled=True,
        )
    finally:
        engine._store.close()

    assert len(calls) == 1
    assert calls[0]["query"] != question
    assert calls[0]["detail"] == "answer_ready"
    assert calls[0]["seen_refs"] == [austin["exact_ref"]]
    assert result["decision"]["missing_requirement"]["kind"] == "latest_state_update"
    assert result["status"] == "augmented"
    assert result["retrieval"]["calls"] == 1
    assert result["novel_exact_refs"] == [denver["exact_ref"]]
    assert result["context"].count(denver["exact_ref"]) == 1
    assert austin["exact_ref"] not in result["context"]


def test_no_novel_ref_and_zero_budget_preserve_baseline(tmp_path):
    engine = _engine(tmp_path)
    austin = _append(engine, "I used to live in Austin.", observed_at=1_710_000_000)
    calls = []

    def retrieve(args):
        calls.append(args)
        return json.dumps({"hits": [austin]})

    try:
        no_novel = _result(
            engine,
            "Where do I live now?",
            refs=[austin],
            retrieve=retrieve,
            enabled=True,
        )
        no_budget = _result(
            engine,
            "Where do I live now?",
            refs=[austin],
            retrieve=retrieve,
            enabled=True,
            budgets={"max_retrieval_calls": 0},
        )
    finally:
        engine._store.close()

    assert no_novel["reason_code"] == "no_novel_exact_ref"
    assert no_novel["context"] is None
    assert no_budget["reason_code"] == "retrieval_budget_exhausted"
    assert no_budget["context"] is None
    assert len(calls) == 1


def test_fixed_count_computes_but_open_cardinality_never_augments(tmp_path):
    engine = _engine(tmp_path)
    bali = _append(
        engine, "I took a vacation to Bali this year.", observed_at=1_710_000_000
    )
    kyoto = _append(
        engine, "I took a vacation to Kyoto this year.", observed_at=1_720_000_000
    )
    try:
        closed = _result(
            engine,
            "How many of the two vacations did I take this year?",
            refs=[bali, kyoto],
            enabled=True,
            question_date="2024-12-31",
        )
        open_world = _result(
            engine,
            "How many vacations did I take this year?",
            refs=[bali, kyoto],
            enabled=True,
            question_date="2024-12-31",
        )
    finally:
        engine._store.close()

    assert closed["status"] == "computed"
    assert closed["computation"]["result"] == "2 items"
    assert closed["context"] is not None
    assert open_world["status"] == "no_augmentation"
    assert open_world["reason_code"] == "open_cardinality"
    assert open_world["context"] is None


def test_sum_difference_and_mixed_units_are_grounded_from_exact_quotes(tmp_path):
    engine = _engine(tmp_path)
    taxi = _append(engine, "The taxi cost $60.", observed_at=1_710_000_000)
    train = _append(engine, "The train cost $20.", observed_at=1_720_000_000)
    hotel = _append(engine, "The hotel took 2 hours.", observed_at=1_730_000_000)
    try:
        total = _result(
            engine,
            "What is the total of the two costs?",
            refs=[taxi, train],
            enabled=True,
        )
        difference = _result(
            engine,
            "What is the difference between the taxi and train costs?",
            refs=[taxi, train],
            enabled=True,
        )
        mixed = _result(
            engine,
            "What is the total of the two values?",
            refs=[taxi, hotel],
            enabled=True,
        )
    finally:
        engine._store.close()

    assert total["computation"]["result"] == "$80"
    assert difference["computation"]["result"] == "$40"
    assert mixed["status"] == "no_augmentation"
    assert mixed["context"] is None


def test_real_question_anchor_resolves_bounded_date_but_singular_open_window_stays_inert(
    tmp_path,
):
    engine = _engine(tmp_path)
    deck = _append(
        engine,
        "I finished the deck today.",
        observed_at=datetime(2024, 3, 15, 9, tzinfo=timezone.utc).timestamp(),
    )
    try:
        bounded = _result(
            engine,
            "Which one event happened five days ago?",
            refs=[deck],
            enabled=True,
            question_date="2024-03-20",
        )
        open_window = _result(
            engine,
            "What happened five days ago?",
            refs=[deck],
            enabled=True,
            question_date="2024-03-20",
        )
    finally:
        engine._store.close()

    assert bounded["status"] == "computed"
    assert bounded["computation"]["operation"] == "date_filter"
    assert bounded["evidence"][0]["occurrence_time"]["event_date"] == "2024-03-15"
    assert (
        bounded["evidence"][0]["occurrence_time"]["observed_at"]
        != bounded["evidence"][0]["occurrence_time"]["occurred_at"]
    )
    assert open_window["reason_code"] == "open_cardinality"
    assert open_window["context"] is None


def test_computation_context_is_digest_bound_to_immutable_trace(tmp_path):
    engine = _engine(tmp_path)
    first = _append(engine, "The first invoice cost $40.")
    second = _append(engine, "The second invoice cost $60.")
    try:
        result = _result(
            engine,
            "What is the difference between the two invoice costs?",
            refs=[first, second],
            enabled=True,
        )
    finally:
        engine._store.close()

    canonical = json.dumps(result["computation"], sort_keys=True, separators=(",", ":"))
    assert (
        result["computation_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()
    )
    assert result["computation"]["result"] in result["context"]
    assert (
        result["trace"]["context_sha256"]
        == hashlib.sha256(result["context"].encode()).hexdigest()
    )

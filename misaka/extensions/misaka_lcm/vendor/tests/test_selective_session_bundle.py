"""Provider-free V4.6.2 selective baseline-first session-bundle fixtures."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from hermes_lcm.config import LCMConfig
from hermes_lcm.selective_recall import (
    build_selective_session_bundle,
    route_selective_recall,
)
from hermes_lcm.store import MessageStore


def _engine(tmp_path):
    config = LCMConfig(database_path=str(tmp_path / "lcm.db"))
    store = MessageStore(config.database_path, ingest_protection_config=config)
    return SimpleNamespace(
        _config=config,
        _store=store,
        _session_occurrence_dates={},
    )


def _append(engine, session_id, content, *, observed_at=None, role="user"):
    message = {"role": role, "content": content}
    if observed_at is not None:
        message["timestamp"] = observed_at
    store_id = engine._store.append(session_id, message)
    return store_id


def _ref(store_id, content, *, start=0, end=None, **extra):
    if end is None:
        end = len(content)
    return {
        "exact_ref": f"lcm:{store_id}:{start}-{end}",
        "quote": content[start:end],
        **extra,
    }


def test_router_leaves_ordinary_direct_fact_on_byte_identical_baseline():
    routed = route_selective_recall("What color is my bicycle?")
    assert routed == {
        "version": "selective-recall-v1",
        "route": "ordinary",
        "reason_code": "no_selective_cue",
    }


def test_router_uses_only_generic_question_and_explicit_date_cues():
    examples = {
        "When did I submit my research paper?": "session_bundle",
        "What happened five days ago?": "session_bundle",
        "What is the order of the three trips from earliest to latest?": "session_bundle",
        "What is the total number of days across both trips?": "session_bundle",
        "How many weeks did the three projects take in total?": "session_bundle",
        "What was my previous personal best?": "session_bundle",
        "Where am I planning to stay for my birthday trip?": "session_bundle",
        "Which hotel would fit my usual preferences?": "session_bundle",
    }
    for question, expected in examples.items():
        routed = route_selective_recall(question, "2024-04-20")
        assert routed["route"] == expected
    rendered = str(examples).lower()
    assert "longmemeval" not in rendered
    assert "gpt4_" not in rendered


def test_bounded_bundle_expands_adjacent_exact_turn_with_source_date(tmp_path):
    engine = _engine(tmp_path)
    observed = datetime(2024, 5, 3, 9, 30, tzinfo=timezone.utc).timestamp()
    lead_text = "I am finishing my sentiment analysis research paper."
    lead_id = _append(engine, "research", lead_text, observed_at=observed - 60)
    _append(engine, "research", "That sounds like good progress.", role="assistant")
    answer_text = "I submitted the research paper on May 3."
    answer_id = _append(engine, "research", answer_text, observed_at=observed)
    try:
        result = build_selective_session_bundle(
            "When did I submit my research paper?",
            engine=engine,
            baseline_refs=[_ref(lead_id, lead_text)],
            question_date="2024-05-20",
            enabled=True,
        )
    finally:
        engine._store.close()

    assert result["status"] == "augmented"
    assert result["route"] == "session_bundle"
    assert result["reason_code"] == "novel_adjacent_exact_evidence"
    assert f"lcm:{answer_id}:0-{len(answer_text)}" in result["novel_exact_refs"]
    assert answer_text in result["context"]
    assert "2024-05-03" in result["context"]
    assert result["metrics"]["session_loads"] == 1
    assert result["provenance"]["selector_calls"] == 0
    assert result["provenance"]["provider_calls"] == 0


def test_bundle_loads_a_bounded_turn_before_a_late_matched_anchor(tmp_path):
    engine = _engine(tmp_path)
    fact = "The final workshop was held in the north library."
    fact_id = _append(engine, "workshop", fact, observed_at=1_715_000_000)
    anchor = "I was pleased with how that workshop concluded."
    anchor_id = _append(engine, "workshop", anchor, observed_at=1_715_000_060)
    try:
        result = build_selective_session_bundle(
            "Where was the final workshop held last month?",
            engine=engine,
            baseline_refs=[_ref(anchor_id, anchor)],
            question_date="2024-05-20",
            enabled=True,
            budgets={"max_messages_per_session": 3},
        )
    finally:
        engine._store.close()

    assert result["status"] == "augmented"
    assert f"lcm:{fact_id}:0-{len(fact)}" in result["novel_exact_refs"]
    assert fact in result["context"]


def test_non_routed_path_does_not_read_sessions_or_change_baseline_digest(
    tmp_path, monkeypatch
):
    engine = _engine(tmp_path)
    content = "My bicycle is blue."
    store_id = _append(engine, "bike", content)
    baseline = [_ref(store_id, content)]

    def forbidden(*_args, **_kwargs):
        raise AssertionError("ordinary path must not load a session")

    monkeypatch.setattr(engine._store, "load_session_page", forbidden)
    monkeypatch.setattr(engine._store, "load_session_window", forbidden)
    try:
        first = build_selective_session_bundle(
            "What color is my bicycle?",
            engine=engine,
            baseline_refs=baseline,
            enabled=True,
        )
        second = build_selective_session_bundle(
            "What color is my bicycle?",
            engine=engine,
            baseline_refs=baseline,
            enabled=True,
        )
    finally:
        engine._store.close()

    assert first["status"] == "no_augmentation"
    assert first["reason_code"] == "ordinary_baseline"
    assert first["context"] is None
    assert first["baseline"] == second["baseline"]
    assert first["metrics"]["session_loads"] == 0


def test_bundle_is_stable_deduplicated_and_bounded(tmp_path):
    engine = _engine(tmp_path)
    refs = []
    expected_session_order = []
    for index in range(5):
        session_id = f"session-{index}"
        expected_session_order.append(session_id)
        lead = f"Trip {index} planning notes."
        lead_id = _append(engine, session_id, lead, observed_at=1_710_000_000 + index)
        refs.extend([_ref(lead_id, lead), _ref(lead_id, lead)])
        _append(
            engine,
            session_id,
            f"Trip {index} happened in city {index}. " + ("x" * 1_000),
            observed_at=1_710_000_100 + index,
        )
    try:
        result = build_selective_session_bundle(
            "What is the order of all trips from earliest to latest?",
            engine=engine,
            baseline_refs=refs,
            question_date="2024-12-31",
            enabled=True,
            budgets={
                "max_sessions": 3,
                "max_messages_per_session": 2,
                "max_novel_refs": 4,
                "max_context_chars": 1_800,
                "max_quote_chars": 500,
            },
        )
    finally:
        engine._store.close()

    assert result["status"] == "augmented"
    assert result["selected_sessions"] == expected_session_order[:3]
    assert len(result["novel_exact_refs"]) <= 4
    assert len(result["novel_exact_refs"]) == len(set(result["novel_exact_refs"]))
    assert result["metrics"]["context_chars"] <= 1_800
    assert result["trace"]["truncated"] is True


def test_default_bundle_budget_is_small_enough_for_baseline_first_answering(tmp_path):
    engine = _engine(tmp_path)
    lead = "I started comparing the two travel plans."
    lead_id = _append(engine, "travel", lead, observed_at=1_710_000_000)
    for index in range(8):
        _append(
            engine,
            "travel",
            f"Travel detail {index}: " + ("x" * 1_000),
            observed_at=1_710_000_100 + index,
        )
    try:
        result = build_selective_session_bundle(
            "What is the total number of days across both trips?",
            engine=engine,
            baseline_refs=[_ref(lead_id, lead)],
            question_date="2024-12-31",
            enabled=True,
        )
    finally:
        engine._store.close()

    assert result["status"] == "augmented"
    assert result["budgets"] == {
        "max_sessions": 3,
        "max_messages_per_session": 4,
        "max_novel_refs": 4,
        "max_context_chars": 3_000,
        "max_quote_chars": 600,
    }
    assert result["metrics"]["context_chars"] <= 3_000
    assert len(result["novel_exact_refs"]) <= 4


def test_unknown_source_time_is_valid_and_never_relabels_ingest_as_event(tmp_path):
    engine = _engine(tmp_path)
    lead = "I was planning a charity event."
    lead_id = _append(engine, "charity", lead)
    answer = "The charity event was held at the library."
    _append(engine, "charity", answer)
    try:
        result = build_selective_session_bundle(
            "Where was the charity event held two weeks ago?",
            engine=engine,
            baseline_refs=[_ref(lead_id, lead)],
            question_date="2024-06-20",
            enabled=True,
        )
    finally:
        engine._store.close()

    assert result["status"] == "augmented"
    assert "date=unknown" in result["context"]
    assert "ingest" not in result["context"].lower()
    assert result["provenance"]["unknown_source_time_valid"] is True


def test_invalid_date_error_and_no_progress_fail_to_unchanged_baseline(tmp_path):
    engine = _engine(tmp_path)
    content = "I submitted the research paper on May 3."
    store_id = _append(engine, "research", content, observed_at=1_714_727_400)
    baseline = [_ref(store_id, content)]
    try:
        invalid = build_selective_session_bundle(
            "When did I submit my research paper?",
            engine=engine,
            baseline_refs=baseline,
            question_date="2024/05/20 (Tue)",
            enabled=True,
        )
        no_progress = build_selective_session_bundle(
            "When did I submit my research paper?",
            engine=engine,
            baseline_refs=baseline,
            question_date="2024-05-20",
            enabled=True,
        )
    finally:
        engine._store.close()

    assert invalid["status"] == "no_augmentation"
    assert invalid["reason_code"] == "question_date_invalid"
    assert invalid["context"] is None
    assert no_progress["status"] == "no_augmentation"
    assert no_progress["reason_code"] == "no_novel_exact_evidence"
    assert no_progress["context"] is None

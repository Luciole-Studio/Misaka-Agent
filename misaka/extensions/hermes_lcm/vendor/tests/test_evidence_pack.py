"""Provider-free tests for the bounded V4.2 evidence-pack tool."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from hermes_lcm.config import LCMConfig
from hermes_lcm.evidence_pack import build_evidence_pack
from hermes_lcm.store import MessageStore
from hermes_lcm.tools import lcm_evidence_pack


def _engine(tmp_path):
    config = LCMConfig(database_path=str(tmp_path / "lcm.db"))
    store = MessageStore(config.database_path, ingest_protection_config=config)
    return SimpleNamespace(
        _config=config,
        _store=store,
        _assertions=None,
        _session_occurrence_dates={},
    )


def _append(
    engine,
    content: str,
    *,
    session_id: str = "session-a",
    observed_at: float | None = None,
) -> int:
    message = {"role": "user", "content": content}
    if observed_at is not None:
        message["timestamp"] = observed_at
    return engine._store.append(session_id, message)


def _whole_ref(store_id: int, content: str, **facets):
    return {
        "exact_ref": f"lcm:{store_id}:0-{len(content)}",
        **facets,
    }


def test_pack_normalizes_date_anchor_and_repairs_unique_exact_spans(tmp_path):
    engine = _engine(tmp_path)
    content = "The taxi costs $60. The train costs $20."
    store_id = _append(engine, content)
    engine._session_occurrence_dates = {"session-a": "2023-05-29"}
    try:
        payload = json.loads(lcm_evidence_pack({
            "question": "What is the difference between the taxi and train fares?",
            "question_date": "2023-05-30T23:42:00",
            "baseline_refs": [
                _whole_ref(
                    store_id,
                    content,
                    quote="taxi costs $60",
                    value=60,
                    unit="USD",
                    key="taxi",
                    label="taxi",
                ),
                _whole_ref(
                    store_id,
                    content,
                    quote="train costs $20",
                    value=20,
                    unit="USD",
                    key="train",
                    label="train",
                ),
            ],
        }, engine=engine))
    finally:
        engine._store.close()

    assert payload["status"] == "computed", json.dumps(payload, indent=2)
    assert payload["question_date"] == {
        "input": "2023-05-30T23:42:00",
        "date": "2023-05-30",
        "normalization": "date_component",
    }
    assert payload["completeness"]["state"] == "closed"
    assert payload["computation"]["result"] == "$40"
    assert [item["exact_ref"] for item in payload["evidence"]] == [
        f"lcm:{store_id}:{content.index('taxi costs $60')}-"
        f"{content.index('taxi costs $60') + len('taxi costs $60')}",
        f"lcm:{store_id}:{content.index('train costs $20')}-"
        f"{content.index('train costs $20') + len('train costs $20')}",
    ]


def test_pack_rejects_ambiguous_quote_inside_declared_ref(tmp_path):
    engine = _engine(tmp_path)
    content = "The fare is $20. Later the fare is $20."
    store_id = _append(engine, content)
    try:
        payload = json.loads(lcm_evidence_pack({
            "question": "What was the total fare?",
            "baseline_refs": [
                _whole_ref(store_id, content, quote="fare is $20", value=20, unit="USD")
            ],
        }, engine=engine))
    finally:
        engine._store.close()

    assert payload["status"] == "fallback"
    assert payload["evidence"] == []
    assert payload["rejections"][0]["reason_code"] == "ambiguous_quote_in_exact_ref"


def test_pack_rejects_label_not_grounded_in_exact_quote(tmp_path):
    engine = _engine(tmp_path)
    content = "The invoice total is 20 USD."
    store_id = _append(engine, content)
    try:
        payload = json.loads(lcm_evidence_pack({
            "question": "What is the invoice total?",
            "baseline_refs": [
                _whole_ref(store_id, content, quote=content, value=20, label="rent")
            ],
        }, engine=engine))
    finally:
        engine._store.close()

    assert payload["status"] == "fallback"
    assert payload["evidence"] == []
    assert payload["rejections"][0]["reason_code"] == "label_not_in_exact_quote"


def test_pack_rejects_trailing_question_date_garbage(tmp_path):
    engine = _engine(tmp_path)
    content = "The project is green."
    store_id = _append(engine, content)
    try:
        payload = json.loads(lcm_evidence_pack({
            "question": "What is the project status?",
            "question_date": "2024-03-20garbage",
            "baseline_refs": [_whole_ref(store_id, content)],
        }, engine=engine))
    finally:
        engine._store.close()

    assert payload == {
        "status": "fallback",
        "reason_code": "question_date_invalid",
        "question_date": None,
    }


@pytest.mark.parametrize(
    ("facet", "value", "reason_code"),
    [
        ("unit", "u" * 101, "unit_budget_exceeded"),
        ("key", "k" * 301, "key_budget_exceeded"),
        ("label", "l" * 301, "label_budget_exceeded"),
    ],
)
def test_pack_rejects_overlong_facets_instead_of_truncating(
    tmp_path, facet, value, reason_code
):
    engine = _engine(tmp_path)
    content = "The invoice total is 20 USD."
    store_id = _append(engine, content)
    candidate = _whole_ref(store_id, content, quote=content, value=20)
    candidate[facet] = value
    try:
        payload = json.loads(lcm_evidence_pack({
            "question": "What is the invoice total?",
            "baseline_refs": [candidate],
        }, engine=engine))
    finally:
        engine._store.close()

    assert payload["status"] == "fallback"
    assert payload["evidence"] == []
    assert payload["rejections"][0]["reason_code"] == reason_code


def test_pack_counts_each_resolved_exact_ref_once(tmp_path):
    engine = _engine(tmp_path)
    content = "I completed the fence repair today."
    store_id = _append(engine, content)
    engine._session_occurrence_dates = {"session-a": "2024-03-20"}
    candidate = _whole_ref(
        store_id,
        content,
        quote=content,
        key="fence repair",
        label="fence repair",
    )
    try:
        payload = json.loads(lcm_evidence_pack({
            "question": "How many repairs did I complete?",
            "question_date": "2024-03-21",
            "baseline_refs": [candidate, candidate],
        }, engine=engine))
    finally:
        engine._store.close()

    assert payload["metrics"]["input_ref_count"] == 2
    assert payload["metrics"]["unique_evidence_count"] == 1
    assert payload["metrics"]["deduplicated_ref_count"] == 1
    assert payload["completeness"]["state"] == "partial"
    assert payload["computation"] is None


def test_pack_preserves_explicit_relative_and_unknown_occurrence_time(tmp_path):
    engine = _engine(tmp_path)
    explicit = "The launch happened on 2024-03-01."
    relative = "I fixed the fence 5 days ago."
    unknown = "The project status is green."
    explicit_id = _append(engine, explicit, session_id="explicit")
    relative_id = _append(engine, relative, session_id="relative")
    unknown_id = _append(engine, unknown, session_id="unknown")
    engine._session_occurrence_dates = {
        "explicit": "2024-03-20",
        "relative": "2024-03-20",
        "unknown": "2024-03-20",
    }
    try:
        payload = json.loads(lcm_evidence_pack({
            "question": "Put these events in chronological order.",
            "question_date": "2024-03-21",
            "baseline_refs": [
                _whole_ref(explicit_id, explicit, quote=explicit, label="launch"),
                _whole_ref(relative_id, relative, quote=relative, label="fence"),
                _whole_ref(unknown_id, unknown, quote=unknown, label="status"),
            ],
        }, engine=engine))
    finally:
        engine._store.close()

    occurrences = [item["occurrence_time"] for item in payload["evidence"]]
    assert [item["event_time_source"] for item in occurrences] == [
        "explicit",
        "relative_to_session",
        "unknown",
    ]
    assert occurrences[2]["event_at"] is None
    assert occurrences[2]["observed_at"] != occurrences[2]["event_at"]
    assert payload["status"] == "evidence_ready"
    assert payload["computation"] is None
    assert payload["completeness"]["reason_code"] == "open_cardinality_not_product_closed"


def test_pack_bounds_refs_quotes_and_response_without_mutating_source(tmp_path):
    engine = _engine(tmp_path)
    content = "x" * 100 + " grounded evidence " + "y" * 100
    store_id = _append(engine, content)
    before = engine._store.get(store_id)
    refs = [
        _whole_ref(store_id, content, quote="grounded evidence", key="grounded evidence")
        for _ in range(10)
    ]
    try:
        raw = lcm_evidence_pack({
            "question": "Tell me the grounded evidence.",
            "baseline_refs": refs,
            "budgets": {"max_refs": 3, "max_quote_chars": 64},
        }, engine=engine)
        after = engine._store.get(store_id)
    finally:
        engine._store.close()

    payload = json.loads(raw)
    assert len(raw) <= 64_000
    assert payload["budgets"]["max_refs"] == 3
    assert payload["budgets"]["max_quote_chars"] == 64
    assert payload["metrics"]["input_ref_count"] == 10
    assert payload["metrics"]["processed_ref_count"] == 3
    assert payload["truncation"]["refs_truncated"] is True
    assert before == after


def test_pack_reports_observation_separately_from_unknown_occurrence(tmp_path):
    engine = _engine(tmp_path)
    content = "The preferred color is green."
    store_id = _append(engine, content)
    observed = datetime(2024, 3, 20, tzinfo=timezone.utc).timestamp()
    engine._store._conn.execute(
        "UPDATE messages SET timestamp = ? WHERE store_id = ?", (observed, store_id)
    )
    engine._store._conn.commit()
    engine._session_occurrence_dates = {"session-a": "2024-03-20"}
    try:
        payload = json.loads(lcm_evidence_pack({
            "question": "What is the preferred color?",
            "baseline_refs": [
                _whole_ref(store_id, content, quote="color is green", value="green")
            ],
        }, engine=engine))
    finally:
        engine._store.close()

    item = payload["evidence"][0]
    assert item["observation_time"]["date"] == "2024-03-20"
    assert item["occurrence_time"]["event_time_source"] == "unknown"
    assert item["occurrence_time"]["event_date"] is None


def test_pack_treats_out_of_range_host_timestamp_as_malformed(tmp_path):
    engine = _engine(tmp_path)
    content = "I visited Paris."
    store_id = _append(engine, content, observed_at=1_715_000_000_000)
    stored = engine._store.get(store_id)
    try:
        payload = json.loads(lcm_evidence_pack({
            "question": "Where did I visit?",
            "baseline_refs": [
                _whole_ref(store_id, content, quote=content, key="paris")
            ],
        }, engine=engine))
    finally:
        engine._store.close()

    assert stored["observed_at"] is None
    assert stored["observed_at_source"] is None
    assert payload["evidence"][0]["observation_time"]["source"] == "ingest_fallback"


def test_pack_computes_explicit_three_item_order_without_open_cardinality_claim(tmp_path):
    engine = _engine(tmp_path)
    rows = [
        ("I returned from the Muir Woods trip today.", "muir woods", "2024-01-03"),
        ("I returned from the Big Sur trip today.", "big sur", "2024-02-04"),
        ("I returned from the Yosemite trip today.", "yosemite", "2024-03-05"),
    ]
    refs = []
    for index, (content, label, session_date) in enumerate(rows):
        session_id = f"trip-{index}"
        store_id = _append(engine, content, session_id=session_id)
        engine._session_occurrence_dates[session_id] = session_date
        refs.append(_whole_ref(store_id, content, quote=content, key=label, label=label))
    try:
        payload = json.loads(lcm_evidence_pack({
            "question": "What is the order of the three trips from earliest to latest?",
            "question_date": "2024-03-20T09:30:00",
            "baseline_refs": list(reversed(refs)),
        }, engine=engine))
    finally:
        engine._store.close()

    assert payload["status"] == "computed", json.dumps(payload, indent=2)
    assert payload["completeness"] == {
        "state": "closed",
        "reason_code": "fixed_cardinality_satisfied",
        "product_verified": True,
    }
    assert payload["computation"]["result"] == "muir woods -> big sur -> yosemite"


def test_pack_selects_relative_date_evidence_without_singular_closure(tmp_path):
    engine = _engine(tmp_path)
    content = "I bought a smoker today."
    store_id = _append(engine, content)
    engine._session_occurrence_dates = {"session-a": "2023-03-15"}
    try:
        payload = json.loads(lcm_evidence_pack({
            "question": "What kitchen appliance did I buy 10 days ago?",
            "question_date": "2023-03-25T18:26:00",
            "baseline_refs": [
                _whole_ref(
                    store_id,
                    content,
                    quote="bought a smoker today",
                    value="smoker",
                    key="smoker",
                    label="smoker",
                )
            ],
        }, engine=engine))
    finally:
        engine._store.close()

    assert payload["status"] == "evidence_ready", json.dumps(payload, indent=2)
    assert payload["completeness"]["reason_code"] == "open_cardinality_not_product_closed"
    assert payload["completeness"]["product_verified"] is False
    assert payload["computation"] is None
    assert payload["evidence"][0]["facets"]["value"] == "smoker"


def test_pack_optional_retrieval_no_novel_is_measured_but_does_not_fake_closure(tmp_path):
    engine = _engine(tmp_path)
    content = "I completed the fence repair."
    store_id = _append(engine, content)
    observed = []

    def retrieve(args):
        observed.append(args)
        return {
            "hits": [],
            "delta": {"termination_reason": "no_novel_exact_ref"},
            "provenance": {"coverage": {"fts": "full", "chunk": "full"}},
            "metrics": {
                "embedding_query_calls": 1,
                "embedding_query_tokens": 7,
                "embedding_queries": [
                    {"provider": "mock", "model": "mock-model", "usage_tokens": 7}
                ],
            },
        }

    try:
        payload = json.loads(build_evidence_pack({
            "question": "How many repairs did I complete?",
            "baseline_refs": [
                _whole_ref(store_id, content, quote=content, key="fence repair")
            ],
            "budgets": {"max_retrieval_calls": 1, "max_novel_refs": 2},
        }, engine=engine, retrieve=retrieve))
    finally:
        engine._store.close()

    assert len(observed) == 1
    assert observed[0]["seen_refs"] == [f"lcm:{store_id}:0-{len(content)}"]
    assert payload["retrieval"]["status"] == "no_novel"
    assert payload["retrieval"]["query_calls"] == 1
    assert payload["retrieval"]["usage"]["embedding_query_tokens"] == 7
    assert payload["completeness"]["state"] == "partial"
    assert payload["computation"] is None


def test_pack_does_not_admit_unfaceted_novel_retrieval_refs_on_failed_compute(tmp_path):
    engine = _engine(tmp_path)
    baseline = "I completed the fence repair."
    novel = "I completed the roof repair."
    baseline_id = _append(engine, baseline, session_id="session-a")
    novel_id = _append(engine, novel, session_id="session-b")

    def retrieve(_args):
        return {
            "hits": [{"exact_ref": f"lcm:{novel_id}:0-{len(novel)}"}],
            "metrics": {"embedding_query_calls": 1},
        }

    try:
        payload = json.loads(build_evidence_pack({
            "question": "How many repairs did I complete?",
            "baseline_refs": [
                _whole_ref(baseline_id, baseline, quote=baseline, key="fence repair")
            ],
            "budgets": {"max_retrieval_calls": 1},
        }, engine=engine, retrieve=retrieve))
    finally:
        engine._store.close()

    assert [item["store_id"] for item in payload["evidence"]] == [baseline_id]
    assert payload["retrieval"]["status"] == "novel_refs_available"
    assert payload["retrieval"]["novel_exact_refs"] == [
        f"lcm:{novel_id}:0-{len(novel)}"
    ]
    assert "quote" not in payload["retrieval"]


def test_pack_applies_stable_session_and_observation_date_diversity(tmp_path):
    engine = _engine(tmp_path)
    refs = []
    for index in range(5):
        session_id = "dense" if index < 4 else "other"
        content = f"Repair event {index}."
        store_id = _append(engine, content, session_id=session_id)
        engine._session_occurrence_dates[session_id] = (
            "2024-03-01" if session_id == "dense" else "2024-03-02"
        )
        refs.append(_whole_ref(store_id, content, quote=content, key=f"repair event {index}"))
    try:
        payload = json.loads(lcm_evidence_pack({
            "question": "How many repair events were there?",
            "baseline_refs": refs,
            "budgets": {"max_per_session": 2, "max_per_date": 2},
        }, engine=engine))
    finally:
        engine._store.close()

    assert [item["session_id"] for item in payload["evidence"]] == [
        "dense",
        "dense",
        "other",
    ]
    assert payload["metrics"]["diversity_dropped_count"] == 2


def test_pack_selects_latest_observed_update_without_calling_it_occurrence(tmp_path):
    engine = _engine(tmp_path)
    old = "My usual gym time is 7:00 pm."
    new = "My usual gym time is 6:00 pm."
    old_id = _append(
        engine,
        old,
        session_id="old",
        observed_at=datetime(2024, 3, 1, tzinfo=timezone.utc).timestamp(),
    )
    new_id = _append(
        engine,
        new,
        session_id="new",
        observed_at=datetime(2024, 3, 10, tzinfo=timezone.utc).timestamp(),
    )
    try:
        payload = json.loads(lcm_evidence_pack({
            "question": "What time do I usually go to the gym?",
            "question_date": "2024-03-20",
            "baseline_refs": [
                _whole_ref(old_id, old, quote=old, value="7:00 pm", key="gym time"),
                _whole_ref(new_id, new, quote=new, value="6:00 pm", key="gym time"),
            ],
        }, engine=engine))
    finally:
        engine._store.close()

    assert payload["intent"]["operation"] == "latest_fact"
    assert payload["selection"] == {
        "status": "selected",
        "basis": "observation_time",
        "exact_refs": [f"lcm:{new_id}:0-{len(new)}"],
        "reason_code": "latest_unique_bounded_candidate",
    }
    latest = next(item for item in payload["evidence"] if item["store_id"] == new_id)
    assert latest["observation_time"]["source"] == "host_message_timestamp"
    assert latest["occurrence_time"]["event_time_source"] == "unknown"
    assert payload["computation"] is None


def test_user_value_latest_location_respects_present_and_historical_as_of(tmp_path):
    engine = _engine(tmp_path)
    austin = "I now live in Austin."
    denver = "I now live in Denver."
    austin_id = _append(
        engine,
        austin,
        session_id="march",
        observed_at=datetime(2024, 3, 10, tzinfo=timezone.utc).timestamp(),
    )
    denver_id = _append(
        engine,
        denver,
        session_id="april",
        observed_at=datetime(2024, 4, 5, tzinfo=timezone.utc).timestamp(),
    )
    refs = [
        _whole_ref(austin_id, austin, quote=austin, value="Austin", key="live"),
        _whole_ref(denver_id, denver, quote=denver, value="Denver", key="live"),
    ]
    try:
        present = json.loads(lcm_evidence_pack({
            "question": "Where do I currently live?",
            "question_date": "2024-04-20",
            "baseline_refs": refs,
        }, engine=engine))
        historical = json.loads(lcm_evidence_pack({
            "question": "Where did I currently live as of March 20?",
            "question_date": "2024-03-20",
            "baseline_refs": refs,
        }, engine=engine))
    finally:
        engine._store.close()

    assert present["selection"]["exact_refs"] == [f"lcm:{denver_id}:0-{len(denver)}"]
    assert historical["selection"]["exact_refs"] == [f"lcm:{austin_id}:0-{len(austin)}"]
    assert historical["exclusions"] == [{
        "exact_ref": f"lcm:{denver_id}:0-{len(denver)}",
        "reason_code": "source_observed_after_question_as_of",
    }]


def test_user_value_vacation_count_closes_only_with_fixed_cardinality(tmp_path):
    engine = _engine(tmp_path)
    bali = "I took a vacation to Bali this year."
    kyoto = "I took a vacation to Kyoto this year."
    bali_id = _append(engine, bali, observed_at=1_710_000_000)
    kyoto_id = _append(engine, kyoto, observed_at=1_720_000_000)
    refs = [
        _whole_ref(bali_id, bali, quote=bali, key="vacation bali"),
        _whole_ref(kyoto_id, kyoto, quote=kyoto, key="vacation kyoto"),
    ]
    try:
        closed = json.loads(lcm_evidence_pack({
            "question": "How many of the two vacations did I take this year?",
            "question_date": "2024-12-31",
            "baseline_refs": refs,
        }, engine=engine))
        open_world = json.loads(lcm_evidence_pack({
            "question": "How many vacations did I take this year?",
            "question_date": "2024-12-31",
            "baseline_refs": refs,
        }, engine=engine))
    finally:
        engine._store.close()

    assert closed["status"] == "computed"
    assert closed["computation"]["result"] == "2 items"
    assert closed["completeness"]["reason_code"] == "fixed_cardinality_satisfied"
    assert open_world["completeness"]["reason_code"] == "open_cardinality_not_product_closed"
    assert open_world["computation"] is None


def test_user_value_sum_and_difference_require_exact_spans_and_units(tmp_path):
    engine = _engine(tmp_path)
    first = "The first invoice cost $40."
    second = "The second invoice cost $60."
    first_id = _append(engine, first, observed_at=1_710_000_000)
    second_id = _append(engine, second, observed_at=1_720_000_000)
    refs = [
        _whole_ref(first_id, first, quote=first, value=40, unit="usd", key="first invoice"),
        _whole_ref(second_id, second, quote=second, value=60, unit="usd", key="second invoice"),
    ]
    try:
        total = json.loads(lcm_evidence_pack({
            "question": "What is the total of the two costs?",
            "baseline_refs": refs,
        }, engine=engine))
        difference = json.loads(lcm_evidence_pack({
            "question": "What is the difference between the two invoice costs?",
            "baseline_refs": refs,
        }, engine=engine))
    finally:
        engine._store.close()

    assert total["computation"]["result"] == "$100"
    assert difference["computation"]["result"] == "$20"


def test_user_value_five_days_ago_uses_question_and_source_anchors(tmp_path):
    engine = _engine(tmp_path)
    content = "I finished the deck today."
    source_time = datetime(2024, 3, 15, 9, tzinfo=timezone.utc).timestamp()
    store_id = _append(engine, content, observed_at=source_time)
    try:
        payload = json.loads(lcm_evidence_pack({
            "question": "What happened five days ago?",
            "question_date": "2024-03-20T18:00:00",
            "baseline_refs": [
                _whole_ref(
                    store_id,
                    content,
                    quote=content,
                    value="finished the deck",
                    key="deck",
                    label="deck",
                )
            ],
        }, engine=engine))
    finally:
        engine._store.close()

    assert payload["status"] == "evidence_ready", json.dumps(payload, indent=2)
    assert payload["completeness"] == {
        "state": "partial",
        "reason_code": "open_cardinality_not_product_closed",
        "product_verified": False,
    }
    assert payload["computation"] is None
    evidence = payload["evidence"][0]
    assert evidence["observation_time"]["source"] == "host_message_timestamp"
    assert evidence["occurrence_time"]["event_date"] == "2024-03-15"
    assert evidence["occurrence_time"]["occurred_at"] != evidence["occurrence_time"]["observed_at"]


def test_legacy_source_ingested_after_as_of_cannot_leak_explicit_event(tmp_path):
    engine = _engine(tmp_path)
    content = "The launch happened on 2024-03-01."
    store_id = _append(engine, content)
    try:
        payload = json.loads(lcm_evidence_pack({
            "question": "What happened before March ended?",
            "question_date": "2024-03-20",
            "baseline_refs": [_whole_ref(store_id, content, quote=content)],
        }, engine=engine))
    finally:
        engine._store.close()

    assert payload["status"] == "fallback"
    assert payload["evidence"] == []
    assert payload["rejections"][-1]["reason_code"] == "operand_grounding_failed"
    assert "after the question-date boundary" in payload["rejections"][-1]["reason"]


def test_temporal_singular_grammar_never_claims_window_completeness(tmp_path):
    engine = _engine(tmp_path)
    content = "I met Jordan today."
    store_id = _append(
        engine,
        content,
        observed_at=datetime(2024, 3, 15, tzinfo=timezone.utc).timestamp(),
    )
    try:
        payload = json.loads(lcm_evidence_pack({
            "question": "Who did I meet five days ago?",
            "question_date": "2024-03-20",
            "baseline_refs": [
                _whole_ref(store_id, content, quote=content, value="Jordan", key="Jordan")
            ],
        }, engine=engine))
    finally:
        engine._store.close()

    assert payload["intent"]["operation"] == "date_filter"
    assert payload["intent"]["plan"]["exact_operands"] is None
    assert payload["completeness"]["state"] == "partial"
    assert payload["completeness"]["product_verified"] is False
    assert payload["computation"] is None


def test_user_value_conflicting_latest_evidence_falls_back(tmp_path):
    engine = _engine(tmp_path)
    austin = "My preferred city is Austin."
    denver = "My preferred city is Denver."
    shared_time = datetime(2024, 4, 5, tzinfo=timezone.utc).timestamp()
    austin_id = _append(engine, austin, session_id="a", observed_at=shared_time)
    denver_id = _append(engine, denver, session_id="b", observed_at=shared_time)
    try:
        payload = json.loads(lcm_evidence_pack({
            "question": "What is my current preferred city?",
            "question_date": "2024-04-20",
            "baseline_refs": [
                _whole_ref(austin_id, austin, quote=austin, value="Austin", key="preferred city"),
                _whole_ref(denver_id, denver, quote=denver, value="Denver", key="preferred city"),
            ],
        }, engine=engine))
    finally:
        engine._store.close()

    assert payload["selection"] == {
        "status": "fallback",
        "basis": "observation_time",
        "exact_refs": [],
        "reason_code": "latest_candidates_tied",
    }
    assert payload["computation"] is None

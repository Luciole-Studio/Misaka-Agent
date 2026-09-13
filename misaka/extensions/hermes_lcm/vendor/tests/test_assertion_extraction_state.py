"""Synthetic V4 fixtures for strict extraction and bitemporal state reads."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from hermes_lcm.assertion_extraction import parse_assertion_extraction
from hermes_lcm.assertion_state import query_assertion_state
from hermes_lcm.assertion_store import AssertionStore
from hermes_lcm.store import MessageStore


def _source(messages, assertions, content, observed_at, *, role="user"):
    store_id = messages.append(
        "session-a", {"role": role, "content": content}
    )
    messages._conn.execute(
        "UPDATE messages SET timestamp = ? WHERE store_id = ?",
        (float(observed_at), store_id),
    )
    messages._conn.commit()
    return assertions.snapshot_source(store_id)


def _assertion(
    snapshot,
    quote,
    *,
    subject="user:self",
    resolution="self",
    predicate="status",
    value="active",
    kind="fact",
    scope="",
    event_at=None,
    valid_from=None,
    valid_to=None,
):
    start = snapshot.content.index(quote)
    return {
        "source_span_start": start,
        "source_span_end": start + len(quote),
        "source_quote": quote,
        "subject_key": subject,
        "subject_resolution": resolution,
        "predicate_key": predicate,
        "object_value": value,
        "value_text": str(value),
        "kind": kind,
        "polarity": "positive",
        "strength": None,
        "scope_key": scope,
        "event_at": event_at,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "confidence": 1.0,
    }


def _payload(snapshot, assertions, relations=()):
    return {
        "schema_version": 1,
        "source_store_id": snapshot.store_id,
        "source_content_sha256": snapshot.content_sha256,
        "assertions": list(assertions),
        "relations": list(relations),
    }


def _relation(snapshot, quote, *, relation_type, target_id):
    start = snapshot.content.index(quote)
    return {
        "source_span_start": start,
        "source_span_end": start + len(quote),
        "source_quote": quote,
        "from_index": 0,
        "relation_type": relation_type,
        "to_assertion_id": target_id,
        "evidence": "explicit",
        "confidence": 1.0,
    }


@pytest.fixture
def state_db(tmp_path):
    db_path = tmp_path / "lcm.db"
    messages = MessageStore(db_path)
    assertions = AssertionStore(db_path)
    try:
        yield messages, assertions
    finally:
        assertions.close()
        messages.close()


def test_event_time_stays_distinct_and_future_observation_is_excluded(state_db):
    messages, store = state_db
    observed_at = datetime(2024, 3, 10, tzinfo=timezone.utc).timestamp()
    event_at = datetime(2024, 1, 2, tzinfo=timezone.utc).timestamp()
    snapshot = _source(
        messages,
        store,
        "On January 2 I attended the workshop.",
        observed_at,
    )
    wire = _assertion(
        snapshot,
        "attended the workshop",
        predicate="workshop.attendance",
        value="attended",
        kind="event",
        event_at="2024-01-02",
    )
    extraction = parse_assertion_extraction(
        snapshot, _payload(snapshot, [wire]), store=store
    )
    assert extraction.assertions[0].event_at == event_at
    assert extraction.assertions[0].valid_from is None
    store.publish_source(snapshot, extraction.assertions)

    before_learning = query_assertion_state(
        store,
        subject_key="user:self",
        predicate_key="workshop.attendance",
        as_of=datetime(2024, 2, 1, tzinfo=timezone.utc).timestamp(),
    )
    after_learning = query_assertion_state(
        store,
        subject_key="user:self",
        predicate_key="workshop.attendance",
        as_of=datetime(2024, 3, 11, tzinfo=timezone.utc).timestamp(),
    )
    assert before_learning.assertions == ()
    assert len(after_learning.assertions) == 1
    assert after_learning.assertions[0]["observed_at"] == observed_at
    assert after_learning.assertions[0]["event_at"] == event_at


def test_explicit_supersession_applies_only_after_relation_is_observed(state_db):
    messages, store = state_db
    old = _source(messages, store, "My plan is tea.", 100.0)
    old_extraction = parse_assertion_extraction(
        old,
        _payload(old, [_assertion(
            old,
            "plan is tea",
            predicate="drink.plan",
            value="tea",
            kind="status",
        )]),
        store=store,
    )
    old_result = store.publish_source(old, old_extraction.assertions)
    old_id = old_result.assertion_ids[0]

    new = _source(messages, store, "Correction: my plan is coffee.", 200.0)
    new_wire = _assertion(
        new,
        "plan is coffee",
        predicate="drink.plan",
        value="coffee",
        kind="status",
    )
    new_extraction = parse_assertion_extraction(
        new,
        _payload(
            new,
            [new_wire],
            [_relation(
                new,
                "Correction: my plan is coffee",
                relation_type="supersedes",
                target_id=old_id,
            )],
        ),
        store=store,
    )
    store.publish_source(
        new, new_extraction.assertions, relations=new_extraction.relations
    )

    historical = query_assertion_state(
        store,
        subject_key="user:self",
        predicate_key="drink.plan",
        as_of=150.0,
    )
    current = query_assertion_state(
        store,
        subject_key="user:self",
        predicate_key="drink.plan",
        as_of=250.0,
    )
    assert historical.active_assertion_ids == (old_id,)
    assert len(historical.relations) == 0
    assert len(current.assertions) == 2
    assert current.assertions_truncated is False
    assert current.relations_truncated is False
    old_row = next(row for row in current.assertions if row["assertion_id"] == old_id)
    new_row = next(row for row in current.assertions if row["assertion_id"] != old_id)
    assert old_row["active"] is False
    assert old_row["lifecycle_status"] == ("superseded",)
    assert new_row["active"] is True
    assert current.conflict_assertion_ids == ()


def test_recency_alone_never_supersedes_and_scoped_alternatives_stay_visible(state_db):
    messages, store = state_db
    for timestamp, beverage, scope in (
        (100.0, "tea", "morning"),
        (200.0, "coffee", "evening"),
    ):
        snapshot = _source(
            messages,
            store,
            f"For {scope}, I prefer {beverage}.",
            timestamp,
        )
        extraction = parse_assertion_extraction(
            snapshot,
            _payload(snapshot, [_assertion(
                snapshot,
                f"prefer {beverage}",
                predicate="drink.preference",
                value=beverage,
                kind="preference",
                scope=scope,
            )]),
            store=store,
        )
        store.publish_source(snapshot, extraction.assertions)

    state = query_assertion_state(
        store,
        subject_key="user:self",
        predicate_key="drink.preference",
    )
    assert len(state.active_assertion_ids) == 2
    assert state.conflict_assertion_ids == ()
    assert state.relations == ()


def test_explicit_cancellation_closes_only_the_cited_same_identity_state(state_db):
    messages, store = state_db
    commitment = _source(
        messages, store, "I will submit the report.", 100.0
    )
    commitment_extraction = parse_assertion_extraction(
        commitment,
        _payload(commitment, [_assertion(
            commitment,
            "submit the report",
            predicate="report.submission",
            value="committed",
            kind="commitment",
        )]),
        store=store,
    )
    commitment_id = store.publish_source(
        commitment, commitment_extraction.assertions
    ).assertion_ids[0]

    cancellation = _source(
        messages, store, "I explicitly cancel the report submission.", 200.0
    )
    cancellation_extraction = parse_assertion_extraction(
        cancellation,
        _payload(
            cancellation,
            [_assertion(
                cancellation,
                "cancel the report submission",
                predicate="report.submission",
                value="cancelled",
                kind="status",
            )],
            [_relation(
                cancellation,
                "explicitly cancel the report submission",
                relation_type="cancels",
                target_id=commitment_id,
            )],
        ),
        store=store,
    )
    store.publish_source(
        cancellation,
        cancellation_extraction.assertions,
        relations=cancellation_extraction.relations,
    )
    state = query_assertion_state(
        store,
        subject_key="user:self",
        predicate_key="report.submission",
    )
    old = next(
        row for row in state.assertions if row["assertion_id"] == commitment_id
    )
    assert old["active"] is False
    assert old["lifecycle_status"] == ("cancelled",)
    assert len(state.active_assertion_ids) == 1


def test_unrelated_identity_and_ambiguous_subject_fail_closed(state_db):
    messages, store = state_db
    old = _source(messages, store, "Alex works at NovaTech.", 100.0)
    old_extraction = parse_assertion_extraction(
        old,
        _payload(old, [_assertion(
            old,
            "Alex works at NovaTech",
            subject="person:alex",
            resolution="explicit",
            predicate="employment.current",
            value="novatech",
            kind="status",
        )]),
        store=store,
    )
    old_id = store.publish_source(old, old_extraction.assertions).assertion_ids[0]

    ambiguous = _source(messages, store, "They moved to Orion Labs.", 200.0)
    ambiguous_wire = _assertion(
        ambiguous,
        "moved to Orion Labs",
        subject="person:alex",
        resolution="ambiguous",
        predicate="employment.current",
        value="orion labs",
        kind="status",
    )
    with pytest.raises(ValueError, match="ambiguous"):
        parse_assertion_extraction(
            ambiguous, _payload(ambiguous, [ambiguous_wire]), store=store
        )

    other = _source(messages, store, "Alexander now works at Orion Labs.", 300.0)
    other_wire = _assertion(
        other,
        "Alexander now works at Orion Labs",
        subject="person:alexander",
        resolution="explicit",
        predicate="employment.current",
        value="orion labs",
        kind="status",
    )
    with pytest.raises(ValueError, match="across identities"):
        parse_assertion_extraction(
            other,
            _payload(
                other,
                [other_wire],
                [_relation(
                    other,
                    "Alexander now works at Orion Labs",
                    relation_type="supersedes",
                    target_id=old_id,
                )],
            ),
            store=store,
        )


def test_explicit_subject_must_occur_in_exact_source_span(state_db):
    messages, store = state_db
    snapshot = _source(messages, store, "Alice likes tea.", 100.0)
    wire = _assertion(
        snapshot,
        "Alice likes tea",
        subject="person:bob",
        resolution="explicit",
        predicate="preference.drink",
        value="tea",
    )

    with pytest.raises(
        ValueError,
        match="explicit identity is not in the exact source span",
    ):
        parse_assertion_extraction(
            snapshot,
            _payload(snapshot, [wire]),
            store=store,
        )


def test_assertion_value_must_occur_in_exact_source_span(state_db):
    messages, store = state_db
    snapshot = _source(messages, store, "Alice likes tea.", 100.0)
    wire = _assertion(
        snapshot,
        "Alice likes tea",
        subject="person:alice",
        resolution="explicit",
        predicate="preference.drink",
        value="coffee",
    )

    with pytest.raises(
        ValueError,
        match="assertion value is not in the exact source span",
    ):
        parse_assertion_extraction(
            snapshot,
            _payload(snapshot, [wire]),
            store=store,
        )
    assert store.query_assertions(subject_key="person:alice") == []


def test_strict_payload_rejects_bad_spans_unknown_fields_and_implicit_updates(state_db):
    messages, store = state_db
    snapshot = _source(messages, store, "I prefer tea.", 100.0)
    wire = _assertion(
        snapshot,
        "prefer tea",
        predicate="drink.preference",
        value="tea",
        kind="preference",
    )

    wrong_quote = dict(wire, source_quote="prefer coffee")
    with pytest.raises(ValueError, match="does not match"):
        parse_assertion_extraction(
            snapshot, _payload(snapshot, [wrong_quote]), store=store
        )

    unknown = _payload(snapshot, [wire])
    unknown["unsupported_hint"] = "tea"
    with pytest.raises(ValueError, match="unsupported fields"):
        parse_assertion_extraction(snapshot, unknown, store=store)

    first = parse_assertion_extraction(
        snapshot, _payload(snapshot, [wire]), store=store
    )
    target_id = store.publish_source(snapshot, first.assertions).assertion_ids[0]
    update = _source(messages, store, "Actually I prefer coffee.", 200.0)
    update_wire = _assertion(
        update,
        "prefer coffee",
        predicate="drink.preference",
        value="coffee",
        kind="preference",
    )
    relation = _relation(
        update,
        "Actually I prefer coffee",
        relation_type="supersedes",
        target_id=target_id,
    )
    relation["evidence"] = "inferred from recency"
    with pytest.raises(ValueError, match="must be explicit"):
        parse_assertion_extraction(
            update, _payload(update, [update_wire], [relation]), store=store
        )


def test_state_changing_relation_requires_semantic_cue_in_exact_quote(state_db):
    messages, store = state_db
    old = _source(messages, store, "I prefer tea.", 100.0)
    old_wire = _assertion(
        old,
        "I prefer tea",
        predicate="drink.preference",
        value="tea",
        kind="preference",
    )
    old_id = store.publish_source(
        old,
        parse_assertion_extraction(
            old, _payload(old, [old_wire]), store=store
        ).assertions,
    ).assertion_ids[0]

    plain = _source(messages, store, "I prefer coffee.", 200.0)
    plain_wire = _assertion(
        plain,
        "I prefer coffee",
        predicate="drink.preference",
        value="coffee",
        kind="preference",
    )
    with pytest.raises(ValueError, match="requires an explicit semantic cue"):
        parse_assertion_extraction(
            plain,
            _payload(
                plain,
                [plain_wire],
                [_relation(
                    plain,
                    "I prefer coffee",
                    relation_type="supersedes",
                    target_id=old_id,
                )],
            ),
            store=store,
        )


def test_relation_truncation_marks_lifecycle_and_active_state_unknown():
    target = "a" * 64
    row = {
        "assertion_id": target,
        "subject_key": "user:self",
        "predicate_key": "drink.preference",
        "scope_key": "",
        "kind": "preference",
        "object_value": "tea",
        "polarity": "positive",
        "value_text": "tea",
        "speaker_role": "user",
    }
    relations = [
        {
            "relation_type": "confirms",
            "from_assertion_id": target,
            "to_assertion_id": target,
        }
        for _ in range(500)
    ]

    class FakeStore:
        def query_assertions(self, **_kwargs):
            return [row]

        def query_relations(self, *, limit, **_kwargs):
            return relations[:limit]

    state = query_assertion_state(
        FakeStore(), subject_key="user:self", predicate_key="drink.preference"
    )

    assert state.relations_truncated is True
    assert state.active_assertion_ids is None
    assert state.assertions[0]["active"] is None
    assert state.assertions[0]["lifecycle_status"] is None
    assert state.assertions[0]["semantic_state"] == "unknown"

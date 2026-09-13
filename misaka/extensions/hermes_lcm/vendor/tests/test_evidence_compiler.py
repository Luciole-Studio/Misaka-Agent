"""Provider-free V4.6 evidence-compiler contract fixtures."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest

from hermes_lcm.config import LCMConfig
from hermes_lcm.evidence_compiler import (
    EVIDENCE_COMPILER_VERSION,
    SELECTOR_SCHEMA_VERSION,
    compile_evidence,
    derive_evidence_request,
)
from hermes_lcm.query_view_store import QueryViewStore
from hermes_lcm.schemas import LCM_COMPILE_EVIDENCE
from hermes_lcm.store import MessageStore
from hermes_lcm.tools import lcm_compile_evidence


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
    observed_at: float | None = None,
    session_id: str = "session-a",
    role: str = "user",
):
    message = {"role": role, "content": content}
    if observed_at is not None:
        message["timestamp"] = observed_at
    store_id = engine._store.append(session_id, message)
    return {
        "exact_ref": f"lcm:{store_id}:0-{len(content)}",
        "quote": content,
    }


def _claim(facet: str, source: dict, claim_id: str, **values):
    return {
        "claim_id": claim_id,
        "facet": facet,
        "exact_ref": source["exact_ref"],
        "quote": source["quote"],
        **values,
    }


def _selector(*selections, operation=None, missing_facets=(), **extra):
    calls = []

    def select(request):
        calls.append(request)
        if operation is not None:
            assert request["operation"] == operation
        return {
            "version": SELECTOR_SCHEMA_VERSION,
            "selections": list(selections),
            "missing_facets": list(missing_facets),
            **extra,
        }

    select.calls = calls
    return select


def _compile(engine, question, *, refs, selector, **kwargs):
    return compile_evidence(
        question,
        engine=engine,
        baseline_refs=list(refs),
        selector=selector,
        enabled=True,
        **kwargs,
    )


def test_generic_facets_and_as_of_are_derived_without_hidden_metadata():
    request = derive_evidence_request(
        "Why did we choose Atlas, who owns rollout, and when is it due?",
        "2026-07-20",
    )

    assert request["as_of"] == "2026-07-20"
    assert [facet["name"] for facet in request["facets"]] == [
        "decision",
        "rationale",
        "owner",
        "deadline",
    ]
    assert request["operation"] == "none"
    assert request["exhaustive"] is False
    assert not {
        "question_id",
        "benchmark",
        "category",
        "reference",
        "answer_session",
        "judge",
    } & set(request)


@pytest.mark.parametrize(
    "extra",
    [
        {"final_prose": "Trust me"},
        {"benchmark_id": "hidden"},
        {"operation": "average"},
        {"selections": "not-an-array"},
    ],
)
def test_selector_schema_rejects_adversarial_outputs(tmp_path, extra):
    engine = _engine(tmp_path)
    source = _append(engine, "The Atlas decision is approved.")

    def selector(_request):
        payload = {
            "version": SELECTOR_SCHEMA_VERSION,
            "operation": "none",
            "selections": [],
            "missing_facets": [],
        }
        payload.update(extra)
        return payload

    try:
        result = _compile(
            engine,
            "What was the Atlas decision?",
            refs=[source],
            selector=selector,
        )
    finally:
        engine._store.close()

    assert result["status"] == "fallback"
    assert result["state"] == "unknown"
    assert result["reason_code"] == "selector_schema_invalid"
    assert result["evidence"] == []
    assert result["computation"] is None


def test_exact_span_entity_date_value_unit_role_and_key_are_product_validated(
    tmp_path,
):
    engine = _engine(tmp_path)
    observed = datetime(2026, 7, 18, 9, tzinfo=timezone.utc).timestamp()
    source = _append(
        engine,
        "On 2026-07-18, Maya approved the Atlas budget of $40.",
        observed_at=observed,
    )
    valid = _claim(
        "decision",
        source,
        "atlas-budget",
        entity="Maya",
        date="2026-07-18",
        value=40,
        unit="usd",
        distinct_key="atlas budget",
        role="user",
    )
    invalid = dict(valid, claim_id="invalid", entity="Jordan", unit="hour")
    try:
        accepted = _compile(
            engine,
            "What Atlas budget did Maya approve?",
            refs=[source],
            selector=_selector(valid),
            question_date="2026-07-20",
        )
        rejected = _compile(
            engine,
            "What Atlas budget did Maya approve?",
            refs=[source],
            selector=_selector(invalid),
            question_date="2026-07-20",
        )
    finally:
        engine._store.close()

    assert accepted["state"] == "answer_sufficient"
    assert accepted["metrics"]["selected_claims"] == 1
    assert accepted["metrics"]["exact_span_valid"] == 1
    evidence = accepted["evidence"][0]
    assert evidence["facets"] == {
        "value": 40,
        "unit": "usd",
        "key": "atlas budget",
        "date": "2026-07-18",
        "entity": "Maya",
    }
    assert rejected["state"] == "unknown"
    assert {item["reason_code"] for item in rejected["rejections"]} >= {
        "entity_not_in_exact_quote",
        "unit_not_in_exact_quote",
    }


def test_partial_and_answer_sufficient_are_distinct_for_operational_facets(tmp_path):
    engine = _engine(tmp_path)
    decision = _append(engine, "We decided to ship Atlas in September.")
    rationale = _append(engine, "Atlas was chosen because migration risk was lowest.")
    owner = _append(engine, "Maya owns the Atlas rollout.")
    deadline = _append(engine, "The Atlas rollout is due on 2026-09-15.")
    question = "Why did we decide to ship Atlas, who owns it, and when is it due?"
    selected = [
        _claim("decision", decision, "decision", value="ship Atlas"),
        _claim("rationale", rationale, "rationale", value="migration risk was lowest"),
        _claim("owner", owner, "owner", entity="Maya", value="Maya"),
        _claim("deadline", deadline, "deadline", date="2026-09-15", value="2026-09-15"),
    ]
    try:
        partial = _compile(
            engine,
            question,
            refs=[decision, rationale, owner, deadline],
            selector=_selector(
                *selected[:1], missing_facets=["rationale", "owner", "deadline"]
            ),
        )
        complete = _compile(
            engine,
            question,
            refs=[decision, rationale, owner, deadline],
            selector=_selector(*selected),
        )
    finally:
        engine._store.close()

    assert partial["state"] == "partial"
    assert partial["missing_facets"] == ["rationale", "owner", "deadline"]
    assert complete["state"] == "answer_sufficient"
    assert complete["missing_facets"] == []
    assert {item["kind"] for item in complete["operational_candidates"]} == {
        "decision",
        "commitment",
    }
    assert complete["provenance"]["persisted"] is False


def test_semantic_proposal_can_replace_only_the_generic_answer_facet(tmp_path):
    engine = _engine(tmp_path)
    owner = _append(engine, "Maya owns the Atlas rollout.")
    deadline = _append(engine, "The Atlas rollout is due on August 15.")
    question = "Give me the key facts about the Atlas rollout."
    assert [facet["name"] for facet in derive_evidence_request(question)["facets"]] == [
        "answer"
    ]

    complete = _compile(
        engine,
        question,
        refs=[owner, deadline],
        selector=_selector(
            _claim("owner", owner, "owner-1", entity="Maya"),
            _claim("deadline", deadline, "deadline-1"),
            requested_facets=["owner", "deadline"],
        ),
    )
    partial = _compile(
        engine,
        question,
        refs=[owner, deadline],
        selector=_selector(
            _claim("owner", owner, "owner-1", entity="Maya"),
            requested_facets=["owner", "deadline"],
            missing_facets=["deadline"],
        ),
    )

    assert complete["state"] == "answer_sufficient"
    assert complete["request"]["facet_source"] == "semantic_proposal"
    assert [facet["name"] for facet in complete["request"]["facets"]] == [
        "owner",
        "deadline",
    ]
    assert partial["state"] == "partial"
    assert partial["missing_facets"] == ["deadline"]


def test_semantic_facets_cannot_erase_deterministic_facets_or_claim_coverage(tmp_path):
    engine = _engine(tmp_path)
    owner = _append(engine, "Maya owns the Atlas rollout.")
    result = _compile(
        engine,
        "Who owns Atlas and what is the deadline?",
        refs=[owner],
        selector=_selector(
            _claim("owner", owner, "owner-1", entity="Maya"),
            requested_facets=["status"],
            missing_facets=["deadline", "status"],
        ),
    )

    assert result["state"] == "partial"
    assert [facet["name"] for facet in result["request"]["facets"]] == [
        "owner",
        "deadline",
        "status",
    ]
    assert result["finite_coverage"] is False


@pytest.mark.parametrize(
    "requested_facets",
    [
        ["unsafe facet"],
        ["answer", "answer"],
        [f"facet_{index}" for index in range(13)],
    ],
)
def test_semantic_facet_schema_is_bounded_and_strict(tmp_path, requested_facets):
    engine = _engine(tmp_path)
    source = _append(engine, "Maya owns Atlas.")
    result = _compile(
        engine,
        "Give me the key facts about Atlas.",
        refs=[source],
        selector=_selector(requested_facets=requested_facets),
    )

    assert result["state"] == "unknown"
    assert result["reason_code"] == "selector_schema_invalid"


def test_latest_state_selects_denver_and_retains_austin_as_history(tmp_path):
    engine = _engine(tmp_path)
    austin = _append(
        engine,
        "I live in Austin.",
        observed_at=datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp(),
        session_id="old",
    )
    denver = _append(
        engine,
        "I moved to Denver and live there now.",
        observed_at=datetime(2026, 4, 5, tzinfo=timezone.utc).timestamp(),
        session_id="new",
    )
    selector = _selector(
        _claim("current_state", austin, "austin", entity="Austin", value="Austin"),
        _claim("current_state", denver, "denver", entity="Denver", value="Denver"),
        operation="latest_fact",
    )
    try:
        result = _compile(
            engine,
            "Where do I currently live?",
            refs=[austin, denver],
            selector=selector,
            question_date="2026-04-20",
        )
    finally:
        engine._store.close()

    assert result["state"] == "answer_sufficient"
    assert [item["facets"]["value"] for item in result["evidence"]] == ["Denver"]
    assert [item["facets"]["value"] for item in result["historical_evidence"]] == [
        "Austin"
    ]
    assert result["finite_coverage"] is False


def test_five_days_ago_is_answer_sufficient_without_exhaustive_window_claim(tmp_path):
    engine = _engine(tmp_path)
    source = _append(
        engine,
        "I completed the Plank Challenge today.",
        observed_at=datetime(2026, 3, 15, 9, tzinfo=timezone.utc).timestamp(),
    )
    claim = _claim(
        "event",
        source,
        "plank",
        entity="Plank Challenge",
        value="completed the Plank Challenge",
        date="2026-03-15",
        distinct_key="plank challenge",
    )
    try:
        result = _compile(
            engine,
            "What happened five days ago?",
            refs=[source],
            selector=_selector(claim, operation="date_filter"),
            question_date="2026-03-20",
        )
    finally:
        engine._store.close()

    assert result["state"] == "answer_sufficient"
    assert result["finite_coverage"] is False
    assert result["computation"] is None
    assert result["evidence"][0]["occurrence_time"]["event_date"] == "2026-03-15"


def test_closed_and_open_vacation_counts_preserve_finite_coverage_boundary(tmp_path):
    engine = _engine(tmp_path)
    bali = _append(engine, "I took a vacation to Bali this year.")
    kyoto = _append(engine, "I took a vacation to Kyoto this year.")
    claims = [
        _claim("events", bali, "bali", entity="Bali", distinct_key="vacation bali"),
        _claim("events", kyoto, "kyoto", entity="Kyoto", distinct_key="vacation kyoto"),
    ]
    try:
        closed = _compile(
            engine,
            "How many of the two vacations did I take this year?",
            refs=[bali, kyoto],
            selector=_selector(*claims, operation="count_distinct"),
            question_date="2026-12-31",
        )
        open_world = _compile(
            engine,
            "How many vacations did I take this year?",
            refs=[bali, kyoto],
            selector=_selector(*claims, operation="count_distinct"),
            question_date="2026-12-31",
        )
    finally:
        engine._store.close()

    assert closed["state"] == "computation_sufficient"
    assert closed["finite_coverage"] is True
    assert closed["computation"]["result"] == "2 items"
    assert open_world["state"] == "partial"
    assert open_world["finite_coverage"] is False
    assert open_world["computation"] is None


def test_duplicate_grounded_ref_cannot_certify_finite_coverage(tmp_path):
    engine = _engine(tmp_path)
    bali = _append(engine, "I took a vacation to Bali this year.")
    duplicate_claims = [
        _claim("events", bali, "bali-1", entity="Bali", distinct_key="vacation bali"),
        _claim("events", bali, "bali-2", entity="Bali", distinct_key="vacation bali"),
    ]
    try:
        result = _compile(
            engine,
            "How many of the two vacations did I take this year?",
            refs=[bali],
            selector=_selector(*duplicate_claims, operation="count_distinct"),
            question_date="2026-12-31",
        )
    finally:
        engine._store.close()

    assert result["finite_coverage"] is False
    assert result["state"] == "partial"
    assert result["computation"] is None


@pytest.mark.parametrize(
    ("question", "operation", "expected"),
    [
        ("What is the total of the two costs?", "sum", "$100"),
        (
            "How much more was the second invoice than the first invoice?",
            "difference",
            "$20",
        ),
        (
            "What is the order of the two events from earliest to latest?",
            "order",
            "first invoice -> second invoice",
        ),
    ],
)
def test_exact_sum_difference_and_order_compile_canonical_results(
    tmp_path, question, operation, expected
):
    engine = _engine(tmp_path)
    first = _append(
        engine,
        "The first invoice cost $40 on 2026-01-02.",
        observed_at=datetime(2026, 1, 2, tzinfo=timezone.utc).timestamp(),
    )
    second = _append(
        engine,
        "The second invoice cost $60 on 2026-02-03.",
        observed_at=datetime(2026, 2, 3, tzinfo=timezone.utc).timestamp(),
    )
    claims = [
        _claim(
            "operands",
            first,
            "first",
            value=40,
            unit="usd",
            label="first invoice",
            date="2026-01-02",
            distinct_key="first invoice",
        ),
        _claim(
            "operands",
            second,
            "second",
            value=60,
            unit="usd",
            label="second invoice",
            date="2026-02-03",
            distinct_key="second invoice",
        ),
    ]
    if operation == "difference":
        claims.reverse()
    try:
        result = _compile(
            engine,
            question,
            refs=[first, second],
            selector=_selector(*claims, operation=operation),
            question_date="2026-03-01",
        )
    finally:
        engine._store.close()

    assert result["state"] == "computation_sufficient", json.dumps(result, indent=2)
    assert result["computation"]["result"] == expected
    assert (
        result["computation_sha256"]
        == hashlib.sha256(
            json.dumps(
                result["computation"], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )


def test_workflow_gotcha_and_premise_awareness_require_exact_support(tmp_path):
    engine = _engine(tmp_path)
    procedure = _append(engine, "Deploy Atlas with the internal blue command.")
    gotcha = _append(
        engine, "The generic green command fails because this tenant is blue-only."
    )
    premise = _append(engine, "This Atlas tenant has no green deployment lane.")
    question = (
        "How do we deploy Atlas, what gotcha matters, and is the green lane available?"
    )
    selector = _selector(
        _claim("procedure", procedure, "procedure", value="internal blue command"),
        _claim("gotcha", gotcha, "gotcha", value="green command fails"),
        _claim("premise", premise, "premise", value="no green deployment lane"),
    )
    try:
        result = _compile(
            engine,
            question,
            refs=[procedure, gotcha, premise],
            selector=selector,
        )
    finally:
        engine._store.close()

    assert result["state"] == "answer_sufficient"
    assert {item["kind"] for item in result["operational_candidates"]} == {
        "workflow",
        "gotcha",
        "state",
    }


def test_conflict_and_unknown_are_explicit_states(tmp_path):
    engine = _engine(tmp_path)
    when = datetime(2026, 4, 5, tzinfo=timezone.utc).timestamp()
    austin = _append(engine, "My current office is Austin.", observed_at=when)
    denver = _append(engine, "My current office is Denver.", observed_at=when)
    try:
        conflict = _compile(
            engine,
            "What is my current office?",
            refs=[austin, denver],
            selector=_selector(
                _claim("current_state", austin, "austin", value="Austin"),
                _claim("current_state", denver, "denver", value="Denver"),
                operation="latest_fact",
            ),
        )
        unknown = _compile(
            engine,
            "What is my current office?",
            refs=[austin],
            selector=_selector(
                missing_facets=["current_state"], operation="latest_fact"
            ),
        )
    finally:
        engine._store.close()

    assert conflict["state"] == "conflicted"
    assert len(conflict["evidence"]) == 2
    assert unknown["state"] == "unknown"
    assert unknown["evidence"] == []


@pytest.mark.parametrize(
    ("enabled", "selector_mode", "reason"),
    [
        (False, "valid", "feature_disabled"),
        (True, "missing", "selector_unavailable"),
        (True, "error", "selector_error"),
        (True, "oversize", "selector_budget_exhausted"),
    ],
)
def test_disabled_unavailable_error_and_budget_fallback_preserve_baseline_bytes(
    tmp_path, enabled, selector_mode, reason
):
    engine = _engine(tmp_path)
    source = _append(engine, "The Atlas decision is pending.")
    baseline = [source]
    before = json.dumps(baseline, sort_keys=True, separators=(",", ":")).encode()

    if selector_mode == "missing":
        selector = None
    elif selector_mode == "error":

        def selector(_request):
            raise RuntimeError("provider failed")
    elif selector_mode == "oversize":
        selector = _selector(_claim("answer", source, "x", value="x" * 9_000))
    else:
        selector = _selector()
    try:
        result = compile_evidence(
            "What was the Atlas decision?",
            engine=engine,
            baseline_refs=baseline,
            selector=selector,
            enabled=enabled,
        )
    finally:
        engine._store.close()

    after = json.dumps(baseline, sort_keys=True, separators=(",", ":")).encode()
    assert after == before
    assert result["status"] == "fallback"
    assert result["reason_code"] == reason
    assert result["evidence"] == []
    assert result["computation"] is None


def test_no_progress_retrieval_stops_and_trace_is_secret_safe(tmp_path):
    engine = _engine(tmp_path)
    source = _append(engine, "The Atlas decision is pending.")
    selector = _selector(
        _claim("decision", source, "decision", value="pending"),
        missing_facets=["rationale"],
    )
    retrieval_calls = []

    def retrieve(args):
        retrieval_calls.append(args)
        return {"hits": [source], "metrics": {"embedding_query_calls": 0}}

    try:
        result = _compile(
            engine,
            "Why is the Atlas decision pending?",
            refs=[source],
            selector=selector,
            retrieve=retrieve,
            budgets={"max_retrieval_calls": 2},
        )
    finally:
        engine._store.close()

    assert len(selector.calls) == 1
    assert len(retrieval_calls) == 1
    assert retrieval_calls[0]["facet"] == "rationale"
    assert result["state"] == "partial"
    assert result["retrieval"]["status"] == "no_progress"
    assert result["retrieval"]["calls"] == 1
    trace_text = json.dumps(result["trace"], sort_keys=True)
    assert "provider failed" not in trace_text
    assert "api_key" not in trace_text.casefold()
    assert (
        result["trace_sha256"]
        == hashlib.sha256(
            json.dumps(result["trace"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert result["version"] == EVIDENCE_COMPILER_VERSION


def test_registered_tool_uses_the_product_compiler_path(tmp_path):
    engine = _engine(tmp_path)
    # Pin the observation time before the pinned question_date: an unpinned
    # append is observed "now", which crosses the 2026-07-20 as_of boundary
    # once the wall clock passes it and collapses the compile into fallback.
    source = _append(
        engine,
        "Maya owns the Atlas rollout.",
        observed_at=datetime(2026, 7, 19, 9, tzinfo=timezone.utc).timestamp(),
    )
    proposal = _selector(
        _claim("owner", source, "atlas-owner", entity="Maya", role="user")
    )({})
    try:
        payload = json.loads(
            lcm_compile_evidence(
                {
                    "question": "Who owns the Atlas rollout?",
                    "question_date": "2026-07-20",
                    "baseline_refs": [source],
                    "proposal": proposal,
                    "budgets": {"max_retrieval_calls": 0},
                },
                engine=engine,
            )
        )
    finally:
        engine._store.close()

    assert payload["status"] == "compiled"
    assert payload["state"] == "answer_sufficient"
    assert payload["evidence"][0]["exact_ref"] == source["exact_ref"]
    assert payload["provenance"]["storage"] == "same_lcm_db"
    assert payload["provenance"]["final_prose_cached"] is False


def test_public_proposal_schema_does_not_require_code_derived_operation(tmp_path):
    """The proposal schema's required/allowed keys must match what
    _validate_proposal actually accepts. operation is a deterministic,
    code-derived field surfaced to the selector as input (via
    selector_request["operation"]) -- it is never a selector output, so the
    public schema must not require (or allow) callers to echo it back
    (F-PR436-4: the schema required it while the runtime rejected it)."""
    proposal_schema = LCM_COMPILE_EVIDENCE["parameters"]["properties"]["proposal"]
    assert "operation" not in proposal_schema["required"]
    assert "operation" not in proposal_schema["properties"]

    engine = _engine(tmp_path)
    source = _append(
        engine,
        "Maya owns the Atlas rollout.",
        observed_at=datetime(2026, 7, 19, 9, tzinfo=timezone.utc).timestamp(),
    )
    # A caller who builds a proposal containing ONLY the schema's required
    # keys (the honest, schema-compliant path) must succeed end to end.
    schema_compliant_proposal = {
        key: value
        for key, value in _selector(
            _claim("owner", source, "atlas-owner", entity="Maya", role="user")
        )({}).items()
        if key in proposal_schema["required"]
    }
    assert set(schema_compliant_proposal) == set(proposal_schema["required"])
    try:
        payload = json.loads(
            lcm_compile_evidence(
                {
                    "question": "Who owns the Atlas rollout?",
                    "question_date": "2026-07-20",
                    "baseline_refs": [source],
                    "proposal": schema_compliant_proposal,
                },
                engine=engine,
            )
        )
    finally:
        engine._store.close()

    assert payload["status"] == "compiled"
    assert payload["reason_code"] != "selector_schema_invalid"


def test_selective_query_view_persistence_is_same_db_and_default_off(tmp_path):
    engine = _engine(tmp_path)
    query_views = QueryViewStore(engine._config.database_path)
    engine._query_views = query_views
    source = _append(engine, "Maya owns the Atlas rollout.")
    selector = _selector(
        _claim("owner", source, "atlas-owner", entity="Maya", role="user")
    )
    try:
        default_result = _compile(
            engine,
            "Who owns the Atlas rollout?",
            refs=[source],
            selector=selector,
        )
        persisted_result = compile_evidence(
            "Who owns the Atlas rollout?",
            engine=engine,
            baseline_refs=[source],
            selector=selector,
            enabled=True,
            persist_view=True,
        )
        count = query_views._conn.execute(
            "SELECT COUNT(*) FROM lcm_query_view_versions"
        ).fetchone()[0]
    finally:
        query_views.close()
        engine._store.close()

    assert default_result["persistence"]["status"] == "not_requested"
    assert default_result["provenance"]["persisted"] is False
    assert persisted_result["persistence"]["status"] == "published"
    assert persisted_result["provenance"]["persisted"] is True
    assert persisted_result["persistence"]["view_id"]
    assert count == 1


def test_persist_compiled_view_releases_lease_on_publish_failure(tmp_path, monkeypatch):
    """A claim_build() lease must not survive a post-claim publish_ready()
    exception -- otherwise the view sits status='building' for the full
    300s lease and an immediate, correct retry reports busy instead of
    rebuilding (F-PR436-2)."""
    engine = _engine(tmp_path)
    query_views = QueryViewStore(engine._config.database_path)
    engine._query_views = query_views
    source = _append(engine, "Maya owns the Atlas rollout.")
    selector = _selector(
        _claim("owner", source, "atlas-owner", entity="Maya", role="user")
    )
    try:
        monkeypatch.setattr(
            QueryViewStore,
            "publish_ready",
            lambda self, token, **kwargs: (_ for _ in ()).throw(
                ValueError("simulated manifest rejection")
            ),
        )
        failed_result = compile_evidence(
            "Who owns the Atlas rollout?",
            engine=engine,
            baseline_refs=[source],
            selector=selector,
            enabled=True,
            persist_view=True,
        )
        assert failed_result["persistence"]["status"] == "error"
        assert failed_result["persistence"]["reason_code"] == "query_view_publish_failed"

        row = query_views._conn.execute(
            "SELECT status, build_nonce FROM lcm_query_views"
        ).fetchone()
        assert row["status"] != "building"
        assert row["build_nonce"] == ""

        monkeypatch.undo()
        retried_result = compile_evidence(
            "Who owns the Atlas rollout?",
            engine=engine,
            baseline_refs=[source],
            selector=selector,
            enabled=True,
            persist_view=True,
        )
    finally:
        query_views.close()
        engine._store.close()

    assert retried_result["persistence"]["status"] == "published"


def test_persist_compiled_view_cleanup_failure_does_not_escape(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    query_views = QueryViewStore(engine._config.database_path)
    engine._query_views = query_views
    source = _append(engine, "Maya owns the Atlas rollout.")
    selector = _selector(
        _claim("owner", source, "atlas-owner", entity="Maya", role="user")
    )
    try:
        monkeypatch.setattr(
            QueryViewStore,
            "publish_ready",
            lambda self, token, **kwargs: (_ for _ in ()).throw(
                ValueError("original publish failure")
            ),
        )
        monkeypatch.setattr(
            QueryViewStore,
            "mark_failed",
            lambda self, token, error: (_ for _ in ()).throw(
                RuntimeError("cleanup failure")
            ),
        )

        result = compile_evidence(
            "Who owns the Atlas rollout?",
            engine=engine,
            baseline_refs=[source],
            selector=selector,
            enabled=True,
            persist_view=True,
        )
    finally:
        query_views.close()
        engine._store.close()

    assert result["persistence"]["status"] == "error"
    assert result["persistence"]["reason_code"] == "query_view_publish_failed"


def test_selective_persistence_rejects_generic_or_ungrounded_state(tmp_path):
    engine = _engine(tmp_path)
    query_views = QueryViewStore(engine._config.database_path)
    engine._query_views = query_views
    source = _append(engine, "The Atlas color is blue.")
    try:
        result = compile_evidence(
            "What color is Atlas?",
            engine=engine,
            baseline_refs=[source],
            selector=_selector(_claim("answer", source, "atlas-color", value="blue")),
            enabled=True,
            persist_view=True,
        )
        count = query_views._conn.execute(
            "SELECT COUNT(*) FROM lcm_query_view_versions"
        ).fetchone()[0]
    finally:
        query_views.close()
        engine._store.close()

    assert result["state"] == "answer_sufficient"
    assert result["persistence"] == {
        "requested": True,
        "status": "not_eligible",
        "view_id": None,
        "reason_code": "no_high_value_grounded_state",
    }
    assert count == 0

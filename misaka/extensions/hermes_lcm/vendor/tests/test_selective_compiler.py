"""Provider-free V4.6.2 minimal-selector and compiler fixtures."""

from __future__ import annotations

from types import SimpleNamespace

from hermes_lcm.config import LCMConfig
from hermes_lcm.selective_compiler import (
    SELECTIVE_SELECTOR_VERSION,
    compile_selective_evidence,
    prepare_selective_compiler,
)
from hermes_lcm.store import MessageStore


def _engine(tmp_path):
    config = LCMConfig(database_path=str(tmp_path / "lcm.db"))
    store = MessageStore(config.database_path, ingest_protection_config=config)
    return SimpleNamespace(
        _config=config,
        _store=store,
        _session_occurrence_dates={},
        _assertions=None,
    )


def _evidence(engine, content, *, session="s", observed_at=None):
    message = {"role": "user", "content": content}
    if observed_at is not None:
        message["timestamp"] = observed_at
    store_id = engine._store.append(session, message)
    return {
        "exact_ref": f"lcm:{store_id}:0-{len(content)}",
        "quote": content,
    }


def test_ordinary_and_open_cardinality_questions_never_request_selector(tmp_path):
    engine = _engine(tmp_path)
    evidence = _evidence(engine, "My bicycle is blue.")
    try:
        ordinary = prepare_selective_compiler(
            "What color is my bicycle?", baseline_refs=[evidence]
        )
        open_count = prepare_selective_compiler(
            "How many weddings have I attended this year?",
            baseline_refs=[evidence],
            question_date="2024-12-31",
        )
    finally:
        engine._store.close()

    assert ordinary["status"] == "not_routed"
    assert open_count["status"] == "not_routed"
    assert ordinary["prompt"] is None
    assert open_count["prompt"] is None
    assert ordinary["provenance"]["selector_calls"] == 0


def test_closed_sum_uses_handle_quote_facet_and_optional_literal_only(tmp_path):
    engine = _engine(tmp_path)
    first = _evidence(engine, "The first of the two purchases cost $20.", session="a")
    second = _evidence(engine, "The second purchase cost $30.", session="b")
    try:
        prepared = prepare_selective_compiler(
            "What was the total cost of the two purchases?",
            baseline_refs=[first, second],
        )
        assert prepared["status"] == "selector_required"
        assert prepared["request"] == {
            "version": SELECTIVE_SELECTOR_VERSION,
            "operation": "sum",
            "as_of": None,
            "facet": "operand",
            "expected_operands": 2,
        }
        assert set(prepared["selector_contract"]) == {"version", "selections"}
        assert set(prepared["selector_contract"]["selections"][0]) == {
            "handle",
            "quote",
            "facet",
            "value",
            "unit",
        }
        assert "missing_facets" not in prepared["prompt"]
        assert "final prose" in prepared["prompt"]

        result = compile_selective_evidence(
            "What was the total cost of the two purchases?",
            engine=engine,
            compiler_refs=prepared["compiler_refs"],
            selector_proposal={
                "version": SELECTIVE_SELECTOR_VERSION,
                "selections": [
                    {
                        "handle": "e01",
                        "quote": "$20",
                        "facet": "operand",
                        "value": 20,
                        "unit": "usd",
                    },
                    {
                        "handle": "e02",
                        "quote": "$30",
                        "facet": "operand",
                        "value": 30,
                        "unit": "usd",
                    },
                ],
            },
            enabled=True,
        )
    finally:
        engine._store.close()

    assert result["status"] == "compiled"
    assert result["state"] == "computation_sufficient"
    assert result["computation"]["result_value"] == 50
    assert result["computation"]["unit"] == "usd"
    assert result["provenance"]["selector_contract"] == SELECTIVE_SELECTOR_VERSION
    assert "question" not in result["selector_proposal"]
    assert "operation" not in result["selector_proposal"]


def test_product_maps_unique_subquote_to_exact_subspan_and_rejects_bad_handle(tmp_path):
    engine = _engine(tmp_path)
    content = "You need 15 points to redeem the reward."
    evidence = _evidence(engine, content)
    try:
        prepared = prepare_selective_compiler(
            "How many points do I need to redeem the reward?",
            baseline_refs=[evidence],
        )
        bad = compile_selective_evidence(
            "How many points do I need to redeem the reward?",
            engine=engine,
            compiler_refs=prepared["compiler_refs"],
            selector_proposal={
                "version": SELECTIVE_SELECTOR_VERSION,
                "selections": [
                    {
                        "handle": "e99",
                        "quote": "15 points",
                        "facet": "value",
                        "value": 15,
                        "unit": "point",
                    }
                ],
            },
            enabled=True,
        )
        good = compile_selective_evidence(
            "How many points do I need to redeem the reward?",
            engine=engine,
            compiler_refs=prepared["compiler_refs"],
            selector_proposal={
                "version": SELECTIVE_SELECTOR_VERSION,
                "selections": [
                    {
                        "handle": "e01",
                        "quote": "15 points",
                        "facet": "value",
                        "value": 15,
                        "unit": "point",
                    }
                ],
            },
            enabled=True,
        )
    finally:
        engine._store.close()

    assert bad["status"] == "fallback"
    assert bad["reason_code"] == "selector_handle_invalid"
    assert good["evidence"][0]["exact_ref"].endswith(":9-18")
    assert good["finite_coverage"] is True


def test_extra_selector_fields_and_nonliteral_quotes_fail_closed(tmp_path):
    engine = _engine(tmp_path)
    evidence = _evidence(engine, "The first purchase cost $20.")
    try:
        prepared = prepare_selective_compiler(
            "What was the total cost of the two purchases?", baseline_refs=[evidence]
        )
        extra = compile_selective_evidence(
            "What was the total cost of the two purchases?",
            engine=engine,
            compiler_refs=prepared["compiler_refs"],
            selector_proposal={
                "version": SELECTIVE_SELECTOR_VERSION,
                "selections": [],
                "missing_facets": [],
            },
            enabled=True,
        )
        invented = compile_selective_evidence(
            "What was the total cost of the two purchases?",
            engine=engine,
            compiler_refs=prepared["compiler_refs"],
            selector_proposal={
                "version": SELECTIVE_SELECTOR_VERSION,
                "selections": [
                    {
                        "handle": "e01",
                        "quote": "$999",
                        "facet": "operand",
                        "value": 999,
                        "unit": "usd",
                    }
                ],
            },
            enabled=True,
        )
    finally:
        engine._store.close()

    assert extra["reason_code"] == "selector_schema_invalid"
    assert invented["reason_code"] == "selector_quote_not_exact"


def test_latest_state_is_routed_but_never_claims_finite_coverage_from_wording(tmp_path):
    engine = _engine(tmp_path)
    evidence = _evidence(engine, "I currently live in Denver.", observed_at=1_715_000_000)
    try:
        prepared = prepare_selective_compiler(
            "Where do I currently live?", baseline_refs=[evidence]
        )
    finally:
        engine._store.close()

    assert prepared["status"] == "selector_required"
    assert prepared["request"]["operation"] == "latest_fact"
    assert prepared["request"]["expected_operands"] is None
    assert prepared["provenance"]["finite_coverage_claimed"] is False


def test_historical_cutoff_excludes_future_handles_before_selector_prompt():
    past = "The first purchase cost $20."
    future = "The second purchase cost $30."

    prepared = prepare_selective_compiler(
        "What was the total cost of the two purchases?",
        baseline_refs=[
            {
                "exact_ref": f"lcm:1:0-{len(past)}",
                "quote": past,
                "date": "2024-01-01",
            },
            {
                "exact_ref": f"lcm:2:0-{len(future)}",
                "quote": future,
                "date": "2026-01-01",
            },
        ],
        question_date="2024-12-31",
    )

    assert prepared["status"] == "selector_required"
    assert [item["exact_ref"] for item in prepared["compiler_refs"]] == [
        f"lcm:1:0-{len(past)}"
    ]
    assert future not in prepared["prompt"]


def test_historical_cutoff_hydrates_trusted_store_timestamp(tmp_path):
    engine = _engine(tmp_path)
    past = _evidence(engine, "The first purchase cost $20.")
    future = _evidence(engine, "The second purchase cost $30.")
    engine._store._conn.executemany(
        "UPDATE messages SET timestamp = ? WHERE store_id = ?",
        [
            (1_704_067_200, int(past["exact_ref"].split(":")[1])),
            (1_767_225_600, int(future["exact_ref"].split(":")[1])),
        ],
    )
    engine._store._conn.commit()
    past["date"] = "2024-01-01"
    future["date"] = "2024-01-01"
    try:
        prepared = prepare_selective_compiler(
            "What was the total cost of the two purchases?",
            baseline_refs=[past, future],
            question_date="2024-12-31",
            engine=engine,
        )
    finally:
        engine._store.close()

    assert [item["exact_ref"] for item in prepared["compiler_refs"]] == [
        past["exact_ref"]
    ]

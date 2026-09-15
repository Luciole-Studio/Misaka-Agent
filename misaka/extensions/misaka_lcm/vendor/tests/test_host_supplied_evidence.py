"""Provider-free V4.6.1 host-supplied evidence envelope fixtures."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sys
import types
from types import SimpleNamespace

from hermes_lcm.config import LCMConfig
from hermes_lcm.evidence_compiler import SELECTOR_SCHEMA_VERSION
from hermes_lcm.host_evidence import (
    build_host_supplied_evidence,
    call_auxiliary_selector,
    prepare_host_evidence_selector,
)
from hermes_lcm.store import MessageStore

# Fixtures that pin question_date="2026-07-20" must also pin the observation
# time of their appended sources: an unpinned append is observed "now", which
# crosses the as_of boundary once the wall clock passes the pinned date and
# collapses grounding into compiler_fallback.
_OBSERVED_BEFORE_QUESTION_DATE = datetime(
    2026, 7, 19, 9, tzinfo=timezone.utc
).timestamp()


def _engine(tmp_path):
    config = LCMConfig(database_path=str(tmp_path / "lcm.db"))
    return SimpleNamespace(
        _config=config,
        _store=MessageStore(config.database_path, ingest_protection_config=config),
        _assertions=None,
        _session_occurrence_dates={},
    )


def test_code_owns_host_envelope_and_selector_proposes_semantics_only(tmp_path):
    engine = _engine(tmp_path)
    owner_text = "Maya owns the Atlas rollout."
    deadline_text = "The Atlas rollout was due on 2026-07-15."
    owner_id = engine._store.append(
        "session-a",
        {
            "role": "user",
            "content": owner_text,
            "timestamp": _OBSERVED_BEFORE_QUESTION_DATE,
        },
    )
    deadline_id = engine._store.append(
        "session-a",
        {
            "role": "user",
            "content": deadline_text,
            "timestamp": _OBSERVED_BEFORE_QUESTION_DATE,
        },
    )
    owner = {
        "exact_ref": f"lcm:{owner_id}:0-{len(owner_text)}",
        "quote": owner_text,
    }
    deadline = {
        "exact_ref": f"lcm:{deadline_id}:0-{len(deadline_text)}",
        "quote": deadline_text,
    }
    calls = []

    def selector(request):
        calls.append(request)
        return {
            "version": SELECTOR_SCHEMA_VERSION,
            "requested_facets": [],
            "selections": [
                {
                    "claim_id": "atlas-owner",
                    "facet": "owner",
                    "exact_ref": owner["exact_ref"],
                    "quote": owner["quote"],
                    "entity": "Maya",
                    "value": "Maya",
                },
                {
                    "claim_id": "atlas-deadline",
                    "facet": "deadline",
                    "exact_ref": deadline["exact_ref"],
                    "quote": deadline["quote"],
                    "date": "2026-07-15",
                    "value": "2026-07-15",
                },
            ],
            "missing_facets": [],
            "usage": {
                "provider": "fixture",
                "model": "none",
                "effort": "none",
                "calls": 1,
                "input_tokens": 0,
                "output_tokens": 0,
                "latency_ms": 0,
                "cost_usd": 0,
            },
        }

    try:
        result = build_host_supplied_evidence(
            "Who owns the Atlas rollout and when is it due?",
            engine=engine,
            baseline_refs=[owner, deadline],
            question_date="2026-07-20",
            selector=selector,
            enabled=True,
            budgets={"max_input_refs": 8, "max_selections": 8},
        )
    finally:
        engine._store.close()

    assert len(calls) == 1
    envelope = calls[0]
    assert envelope["question"] == "Who owns the Atlas rollout and when is it due?"
    assert envelope["as_of"] == "2026-07-20"
    assert envelope["operation"] == "none"
    assert envelope["baseline_evidence"] == [owner, deadline]
    assert envelope["budgets"] == {"max_selections": 8, "max_quote_chars": 2400}
    prepared = prepare_host_evidence_selector(
        "Who owns the Atlas rollout and when is it due?",
        baseline_refs=[owner, deadline],
        question_date="2026-07-20",
        budgets={"max_input_refs": 8, "max_selections": 8},
    )
    assert prepared["baseline_exact_ref_count"] == 2
    assert prepared["request"]["operation"] == "none"
    assert "Do not return the question" in prepared["prompt"]
    assert len(prepared["envelope_sha256"]) == 64
    assert result["state"] == "answer_sufficient", json.dumps(result, sort_keys=True)
    assert result["baseline_retained"] is True
    assert result["context"].startswith("<lcm-compiled-evidence")
    assert owner["exact_ref"] in result["context"]
    assert deadline["exact_ref"] in result["context"]
    assert result["provenance"]["envelope_owner"] == "hermes_lcm_product_code"
    assert result["provenance"]["registered_tool_transport_used"] is False


def test_named_facet_delta_is_selected_before_the_only_semantic_call(tmp_path):
    engine = _engine(tmp_path)
    baseline_text = "Atlas rollout notes are available."
    owner_text = "Maya owns the Atlas rollout."
    baseline_id = engine._store.append(
        "session-a",
        {
            "role": "user",
            "content": baseline_text,
            "timestamp": _OBSERVED_BEFORE_QUESTION_DATE,
        },
    )
    owner_id = engine._store.append(
        "session-b",
        {
            "role": "user",
            "content": owner_text,
            "timestamp": _OBSERVED_BEFORE_QUESTION_DATE,
        },
    )
    baseline = {
        "exact_ref": f"lcm:{baseline_id}:0-{len(baseline_text)}",
        "quote": baseline_text,
    }
    owner = {
        "exact_ref": f"lcm:{owner_id}:0-{len(owner_text)}",
        "quote": owner_text,
    }
    retrieval_calls = []

    def retrieve(args):
        retrieval_calls.append(args)
        return json.dumps(
            {
                "hits": [{"exact_ref": owner["exact_ref"], "content": owner_text}],
                "metrics": {
                    "embedding_query_calls": 1,
                    "embedding_query_tokens": 4,
                    "embedding_query_tokens_complete": True,
                },
            }
        )

    prepared = prepare_host_evidence_selector(
        "Who owns the Atlas rollout?",
        baseline_refs=[baseline],
        question_date="2026-07-20",
        retrieve=retrieve,
    )
    selector_calls = []

    def selector(request):
        selector_calls.append(request)
        assert owner in request["baseline_evidence"]
        return {
            "version": SELECTOR_SCHEMA_VERSION,
            "requested_facets": [],
            "selections": [
                {
                    "claim_id": "atlas-owner",
                    "facet": "owner",
                    "exact_ref": owner["exact_ref"],
                    "quote": owner["quote"],
                    "entity": "Maya",
                    "value": "Maya",
                }
            ],
            "missing_facets": [],
        }

    try:
        result = build_host_supplied_evidence(
            "Who owns the Atlas rollout?",
            engine=engine,
            baseline_refs=prepared["compiler_refs"],
            question_date="2026-07-20",
            selector=selector,
            retrieve=None,
            enabled=True,
            budgets={"max_retrieval_calls": 0},
            prepared_retrieval=prepared["retrieval"],
        )
    finally:
        engine._store.close()

    assert len(retrieval_calls) == 1
    assert retrieval_calls[0]["facet"] == "owner"
    assert len(selector_calls) == 1
    assert prepared["baseline_exact_ref_count"] == 1
    assert prepared["selector_exact_ref_count"] == 2
    assert prepared["retrieval"]["novel_exact_refs"] == [owner["exact_ref"]]
    assert result["state"] == "answer_sufficient"
    assert result["retrieval"]["status"] == "novel"
    assert result["provenance"]["retrieval_stage"] == "preselector"


def test_selector_cannot_override_host_fields_and_failure_retains_baseline(tmp_path):
    engine = _engine(tmp_path)
    content = "Maya owns the Atlas rollout."
    store_id = engine._store.append("session-a", {"role": "user", "content": content})
    source = {
        "exact_ref": f"lcm:{store_id}:0-{len(content)}",
        "quote": content,
    }

    def hostile_selector(_request):
        return {
            "version": SELECTOR_SCHEMA_VERSION,
            "question": "benchmark-authored replacement",
            "question_date": "2099-01-01",
            "baseline_refs": [],
            "budgets": {"max_input_refs": 0},
            "selections": [],
            "missing_facets": [],
        }

    try:
        result = build_host_supplied_evidence(
            "Who owns the Atlas rollout?",
            engine=engine,
            baseline_refs=[source],
            question_date="2026-07-20",
            selector=hostile_selector,
            enabled=True,
        )
    finally:
        engine._store.close()

    assert result["status"] == "fallback"
    assert result["reason_code"] == "selector_schema_invalid"
    assert result["baseline_retained"] is True
    assert result["context"] is None
    assert result["evidence"] == []
    assert result["computation"] is None


def test_latest_state_text_value_uses_source_time_not_ingest_time(tmp_path):
    engine = _engine(tmp_path)
    engine._session_occurrence_dates["session-a"] = "2024-03-15"
    content = "I moved from Austin to Denver in March 2024."
    store_id = engine._store.append("session-a", {"role": "user", "content": content})
    source = {
        "exact_ref": f"lcm:{store_id}:0-{len(content)}",
        "quote": content,
    }

    def selector(_request):
        return {
            "version": SELECTOR_SCHEMA_VERSION,
            "requested_facets": [],
            "selections": [
                {
                    "claim_id": "current-location",
                    "facet": "current_state",
                    "exact_ref": source["exact_ref"],
                    "quote": content,
                    "entity": "Denver",
                    "value": "Denver",
                }
            ],
            "missing_facets": [],
        }

    try:
        result = build_host_supplied_evidence(
            "Where do I live now?",
            engine=engine,
            baseline_refs=[source],
            question_date="2024-04-06",
            selector=selector,
            enabled=True,
        )
    finally:
        engine._store.close()

    assert result["status"] == "compiled", json.dumps(result, sort_keys=True)
    assert result["state"] == "answer_sufficient"
    assert result["evidence"][0]["facets"]["value"] == "Denver"


def test_compiled_brief_over_context_budget_is_not_injected(tmp_path):
    engine = _engine(tmp_path)
    content = "Maya owns the Atlas rollout."
    store_id = engine._store.append("session-a", {"role": "user", "content": content})
    source = {
        "exact_ref": f"lcm:{store_id}:0-{len(content)}",
        "quote": content,
    }

    def selector(_request):
        return {
            "version": SELECTOR_SCHEMA_VERSION,
            "requested_facets": [],
            "selections": [
                {
                    "claim_id": "atlas-owner",
                    "facet": "owner",
                    "exact_ref": source["exact_ref"],
                    "quote": content,
                    "entity": "Maya",
                    "value": "Maya",
                }
            ],
            "missing_facets": [],
        }

    try:
        result = build_host_supplied_evidence(
            "Who owns the Atlas rollout?",
            engine=engine,
            baseline_refs=[source],
            selector=selector,
            enabled=True,
            max_context_chars=256,
        )
    finally:
        engine._store.close()

    assert result["status"] == "compiled"
    assert result["state"] == "answer_sufficient"
    assert result["context"] is None
    assert result["context_status"] == "context_budget_exhausted"
    assert result["baseline_retained"] is True
    assert result["trace"]["state"] == result["state"]


def test_auxiliary_selector_uses_existing_structured_model_seam_and_actual_usage(
    monkeypatch,
):
    calls = []
    response_payload = {
        "version": SELECTOR_SCHEMA_VERSION,
        "requested_facets": [],
        "selections": [],
        "missing_facets": ["owner"],
        "usage": {"provider": "model-authored-value-must-not-win"},
    }
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(response_payload))
            )
        ],
        usage=SimpleNamespace(input_tokens=123, output_tokens=45),
        provider="fixture-provider",
        model="fixture-model",
    )
    auxiliary = types.ModuleType("agent.auxiliary_client")

    def call_llm(**kwargs):
        calls.append(kwargs)
        return response

    auxiliary.call_llm = call_llm
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", auxiliary)
    request = {
        "version": SELECTOR_SCHEMA_VERSION,
        "question": "Who owns Atlas?",
        "as_of": "2026-07-20",
        "facets": [{"name": "owner", "required": True}],
        "operation": "none",
        "exhaustive": False,
        "expected_cardinality": None,
        "baseline_evidence": [],
        "budgets": {"max_selections": 8, "max_quote_chars": 2400},
    }

    proposal = call_auxiliary_selector(
        request, model="fixture-model", timeout_seconds=7.0
    )

    assert len(calls) == 1
    assert calls[0]["task"] == "evidence_selector"
    assert calls[0]["temperature"] == 0
    assert calls[0]["timeout"] == 7.0
    assert "Do not return the question" in calls[0]["messages"][0]["content"]
    assert proposal["usage"] == {
        "provider": "fixture-provider",
        "model": "fixture-model",
        "effort": "host_default",
        "calls": 1,
        "input_tokens": 123,
        "output_tokens": 45,
        "latency_ms": proposal["usage"]["latency_ms"],
    }

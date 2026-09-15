"""Provider-free V4.6.3 answer-contract fixtures."""

from __future__ import annotations

from datetime import date

from hermes_lcm.answer_contract import compile_answer_contract
from hermes_lcm.config import LCMConfig


def test_scalar_first_quantity_is_not_open_enumeration():
    decision = compile_answer_contract(
        "How many points do I need to redeem the reward?"
    )

    assert decision.status == "planned"
    assert decision.contract.answer_kind == "quantity"
    assert decision.contract.operation == "scalar"
    assert decision.contract.coverage_policy == "source_asserted_fact"
    assert decision.contract.slots[0].value_type == "number"
    assert decision.contract.slots[0].unit == "point"
    assert {"redeem", "reward"}.issubset(decision.contract.anchors)


def test_scalar_first_count_does_not_claim_finite_coverage():
    decision = compile_answer_contract(
        "How many vacations did I take this year?",
        "2025-12-31",
    )

    assert decision.status == "planned"
    assert decision.contract.operation == "scalar"
    assert decision.contract.coverage_policy == "source_asserted_or_finite_enumeration"
    assert decision.contract.finite_cardinality is None
    assert decision.contract.temporal_window is not None


def test_named_sum_operands_create_fixed_slots():
    decision = compile_answer_contract(
        "What was the total time I spent jogging and doing yoga?"
    )

    assert decision.status == "planned"
    assert decision.contract.operation == "sum"
    assert decision.contract.coverage_policy == "fixed_operands"
    assert decision.contract.finite_cardinality == 2
    assert [slot.anchor for slot in decision.contract.slots] == ["jogging", "yoga"]


def test_unquoted_comma_sum_creates_all_named_operand_slots():
    decision = compile_answer_contract(
        "How much total did I spend on taxi, train, and hotel?"
    )

    assert decision.status == "planned"
    assert decision.contract.operation == "sum"
    assert decision.contract.coverage_policy == "fixed_operands"
    assert decision.contract.finite_cardinality == 3
    assert [slot.anchor for slot in decision.contract.slots] == [
        "taxi",
        "train",
        "hotel",
    ]


def test_instead_of_preserves_direction_and_mentions():
    decision = compile_answer_contract(
        "How much time did I save by taking the bus instead of a taxi?"
    )

    assert decision.status == "planned"
    assert decision.contract.operation == "difference"
    assert decision.contract.difference_direction == "second_minus_first"
    assert [slot.anchor for slot in decision.contract.slots] == ["bus", "taxi"]


def test_relative_event_window_requires_real_question_anchor():
    missing = compile_answer_contract("What happened five days ago?")
    anchored = compile_answer_contract(
        "What happened five days ago?", "2024-03-20"
    )

    assert missing.status == "fallback"
    assert missing.reason_code == "question_as_of_required"
    assert anchored.status == "planned"
    assert anchored.contract.operation == "date_filter"
    assert anchored.contract.temporal_window.start == date(2024, 3, 15)
    assert anchored.contract.temporal_window.end == date(2024, 3, 16)
    assert anchored.contract.coverage_policy == "source_asserted_fact"
    assert anchored.contract.finite_cardinality is None


def test_rolling_day_and_week_windows_have_the_requested_length():
    seven_days = compile_answer_contract(
        "How many vacations did I take in the last 7 days?",
        "2024-08-20",
    )
    two_weeks = compile_answer_contract(
        "How many vacations did I take in the last 2 weeks?",
        "2024-08-20",
    )

    assert seven_days.contract.temporal_window.start == date(2024, 8, 14)
    assert seven_days.contract.temporal_window.end == date(2024, 8, 21)
    assert two_weeks.contract.temporal_window.start == date(2024, 8, 7)
    assert two_weeks.contract.temporal_window.end == date(2024, 8, 21)


def test_latest_previous_and_ordinary_classification():
    latest = compile_answer_contract("Where do I currently live?")
    previous = compile_answer_contract("What was my previous occupation?")
    ordinary = compile_answer_contract("Tell me about the Atlas project")

    assert latest.contract.operation == "latest"
    assert latest.contract.answer_kind == "place"
    assert previous.contract.operation == "previous"
    assert ordinary.status == "not_applicable"


def test_ambiguous_or_unsupported_forms_fail_closed():
    assert compile_answer_contract("What was the average price?").status == "fallback"
    assert compile_answer_contract("How much was it?").status == "fallback"


def test_vague_wh_questions_do_not_recreate_broad_keyword_routing():
    assert compile_answer_contract("What did we discuss?").status == "not_applicable"
    assert compile_answer_contract("What happened?").status == "not_applicable"
    assert compile_answer_contract("Why did that happen?").status == "not_applicable"


def test_strong_person_place_and_advice_shapes_remain_routable():
    person = compile_answer_contract("Who manages the Atlas launch?")
    place = compile_answer_contract("Where is the Atlas offsite located?")
    advice = compile_answer_contract("What advice did you give about dry basil?")

    assert person.status == "planned"
    assert person.contract.answer_kind == "person"
    assert place.status == "planned"
    assert place.contract.answer_kind == "place"
    assert advice.status == "planned"
    assert advice.contract.answer_kind == "advice"
    assert advice.contract.slots[0].expected_role == "assistant"


def test_preanswer_mode_is_default_off_and_env_is_additive(monkeypatch):
    monkeypatch.delenv("LCM_PREANSWER_EVIDENCE_ENABLED", raising=False)
    monkeypatch.delenv("LCM_PREANSWER_EVIDENCE_MODE", raising=False)
    assert LCMConfig.from_env().preanswer_evidence_enabled is False
    assert LCMConfig.from_env().preanswer_evidence_mode == ""

    monkeypatch.setenv("LCM_PREANSWER_EVIDENCE_ENABLED", "true")
    monkeypatch.setenv("LCM_PREANSWER_EVIDENCE_MODE", "requirements_v1")
    config = LCMConfig.from_env()
    assert config.preanswer_evidence_enabled is True
    assert config.preanswer_evidence_mode == "requirements_v1"

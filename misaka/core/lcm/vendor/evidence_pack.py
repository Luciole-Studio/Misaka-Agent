"""Bounded, provider-neutral exact-evidence assembly for V4.2.

The pack is deliberately not an answer generator.  It normalizes the
question-time anchor, hydrates caller-selected exact refs from the authoritative
message store, repairs a proposed quote only when it occurs uniquely inside the
declared source window, validates grounded operands, and optionally emits the
existing immutable computation trace.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import math
import re
import time
from typing import Any, Callable, Mapping, Sequence

from .occurrence_time import resolve_occurrence_time
from .reasoning import (
    EvidencePlan,
    compile_evidence_plan,
    execute_plan,
    ground_evidence,
    question_date_as_of_epoch,
    validate_selector_alignment,
)


EVIDENCE_PACK_VERSION = "evidence-pack-v1"
MAX_INPUT_REFS = 50
DEFAULT_MAX_REFS = 25
DEFAULT_MAX_QUOTE_CHARS = 2_400
MAX_QUOTE_CHARS = 2_400
RESPONSE_CHAR_CAP = 64_000
_EXACT_REF_RE = re.compile(r"^lcm:(?P<store_id>[1-9]\d*):(?P<start>\d+)-(?P<end>\d+)$")
_DATE_PREFIX_RE = re.compile(
    r"^(?P<year>\d{4})[-/](?P<month>\d{2})[-/](?P<day>\d{2})(?=$|[Tt\s])"
)
_WEEKDAY_SUFFIX_RE = re.compile(
    r"^\s*\((?P<weekday>mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|"
    r"thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\)\s*",
    re.IGNORECASE,
)
_WEEKDAY_INDEX = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}


@dataclass(frozen=True)
class QuestionDate:
    raw: str
    day: date
    normalization: str

    def public_dict(self) -> dict[str, str]:
        return {
            "input": self.raw,
            "date": self.day.isoformat(),
            "normalization": self.normalization,
        }


@dataclass(frozen=True)
class PackBudgets:
    max_refs: int
    max_quote_chars: int
    max_retrieval_calls: int
    max_novel_refs: int
    max_per_session: int
    max_per_date: int

    def public_dict(self) -> dict[str, int]:
        return {
            "max_refs": self.max_refs,
            "max_quote_chars": self.max_quote_chars,
            "max_retrieval_calls": self.max_retrieval_calls,
            "max_novel_refs": self.max_novel_refs,
            "max_per_session": self.max_per_session,
            "max_per_date": self.max_per_date,
            "response_char_cap": RESPONSE_CHAR_CAP,
        }


@dataclass(frozen=True)
class ResolvedCandidate:
    operand: dict[str, Any]
    public: dict[str, Any]


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(maximum, max(minimum, parsed))


def _parse_budgets(raw: Any) -> PackBudgets:
    value = raw if isinstance(raw, Mapping) else {}
    return PackBudgets(
        max_refs=_bounded_int(
            value.get("max_refs"),
            default=DEFAULT_MAX_REFS,
            minimum=1,
            maximum=MAX_INPUT_REFS,
        ),
        max_quote_chars=_bounded_int(
            value.get("max_quote_chars"),
            default=DEFAULT_MAX_QUOTE_CHARS,
            minimum=64,
            maximum=MAX_QUOTE_CHARS,
        ),
        max_retrieval_calls=_bounded_int(
            value.get("max_retrieval_calls"),
            default=0,
            minimum=0,
            maximum=1,
        ),
        max_novel_refs=_bounded_int(
            value.get("max_novel_refs"),
            default=4,
            minimum=1,
            maximum=8,
        ),
        max_per_session=_bounded_int(
            value.get("max_per_session"),
            default=5,
            minimum=1,
            maximum=5,
        ),
        max_per_date=_bounded_int(
            value.get("max_per_date"),
            default=5,
            minimum=1,
            maximum=8,
        ),
    )


def normalize_question_date(value: Any) -> tuple[QuestionDate | None, str | None]:
    """Interpret the dedicated question-time field as a calendar-day anchor.

    LongMemEval and ordinary hosts may carry a local wall-clock timestamp with
    no timezone.  Time-of-day is not semantically used by the supported date
    operations, so this path intentionally validates and retains only its date
    component.  Other timestamp fields remain timezone-strict.
    """
    raw = str(value or "").strip()
    if not raw:
        return None, None
    match = _DATE_PREFIX_RE.match(raw)
    if match is None:
        return None, "question_date_invalid"
    try:
        day = date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
    except ValueError:
        return None, "question_date_invalid"
    if day == date.max:
        return None, "question_date_invalid"
    suffix = raw[match.end() :]
    weekday_match = _WEEKDAY_SUFFIX_RE.match(suffix)
    if weekday_match is not None:
        weekday = weekday_match.group("weekday").casefold()
        if _WEEKDAY_INDEX[weekday] != day.weekday():
            return None, "question_date_invalid"
        suffix = suffix[weekday_match.end() :]
    if (
        suffix
        and re.fullmatch(
            r"(?:[Tt]|[ \t]+)?(?:[01]\d|2[0-3]):[0-5]\d"
            r"(?::[0-5]\d(?:\.\d{1,6})?)?(?:Z|[+-](?:[01]\d|2[0-3]):?[0-5]\d)?",
            suffix,
        )
        is None
    ):
        return None, "question_date_invalid"
    canonical = day.isoformat()
    if raw == canonical:
        normalization = "identity"
    elif raw.replace("/", "-") == canonical:
        normalization = "separator"
    elif weekday_match is not None:
        normalization = "weekday_date_component"
    else:
        normalization = "date_component"
    return QuestionDate(raw=raw, day=day, normalization=normalization), None


def _parse_exact_ref(candidate: Mapping[str, Any]) -> tuple[int, int, int] | None:
    exact_ref = str(candidate.get("exact_ref") or "").strip()
    if exact_ref:
        match = _EXACT_REF_RE.fullmatch(exact_ref)
        if match is None:
            return None
        return (
            int(match.group("store_id")),
            int(match.group("start")),
            int(match.group("end")),
        )
    try:
        return (
            int(candidate["store_id"]),
            int(candidate["span_start"]),
            int(candidate["span_end"]),
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _canonical_key(value: Any) -> str | None:
    text = str(value or "").strip().casefold()
    if not text:
        return None
    text = re.sub(r"[_-]+", " ", text)
    text = " ".join(text.split())
    return text[:300] or None


def _canonical_unit(value: Any) -> str | None:
    text = str(value or "").strip().casefold()
    if not text:
        return None
    if "$" in text or "usd" in text or "dollar" in text:
        return "usd"
    aliases = {
        "hours": "hour",
        "hrs": "hour",
        "minutes": "minute",
        "mins": "minute",
        "days": "day",
        "weeks": "week",
        "months": "month",
        "pages": "page",
        "projects": "project",
    }
    return aliases.get(text, text)[:100]


def _valid_epoch(value: Any) -> float | None:
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return timestamp if math.isfinite(timestamp) and timestamp > 0 else None


def _observation_time(
    row: Mapping[str, Any], session_date: str | None
) -> dict[str, Any]:
    ingested_at = _valid_epoch(row.get("ingested_at")) or _valid_epoch(
        row.get("timestamp")
    )
    source_observed_at = _valid_epoch(row.get("observed_at"))
    observed_at: float | None = source_observed_at
    observed_day: str | None = None
    source = (
        "host_message_timestamp" if source_observed_at is not None else "unavailable"
    )
    if session_date:
        match = _DATE_PREFIX_RE.match(str(session_date).strip())
        if match:
            try:
                observed_day = date(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                ).isoformat()
            except ValueError:
                observed_day = None
        if observed_day is not None:
            observed_at = datetime.fromisoformat(
                f"{observed_day}T00:00:00+00:00"
            ).timestamp()
            source = "benchmark_session_date"
    if observed_day is None and source_observed_at is not None:
        observed_day = (
            datetime.fromtimestamp(source_observed_at, tz=timezone.utc)
            .date()
            .isoformat()
        )
    if observed_day is None and ingested_at is not None:
        observed_day = (
            datetime.fromtimestamp(ingested_at, tz=timezone.utc).date().isoformat()
        )
        source = "ingest_fallback"
    return {
        "observed_at": observed_at,
        "ingested_at": ingested_at,
        "date": observed_day,
        "source": source,
    }


def _resolve_candidate(
    raw: Any,
    *,
    engine: Any,
    budgets: PackBudgets,
    plan: EvidencePlan | None,
) -> tuple[ResolvedCandidate | None, str | None]:
    if not isinstance(raw, Mapping):
        return None, "candidate_not_object"
    parsed = _parse_exact_ref(raw)
    if parsed is None:
        return None, "invalid_exact_ref"
    store_id, declared_start, declared_end = parsed
    row = engine._store.get(store_id)
    if row is None:
        return None, "missing_source_row"
    content = str(row.get("content") or "")
    if (
        declared_start < 0
        or declared_end <= declared_start
        or declared_end > len(content)
    ):
        return None, "exact_ref_out_of_bounds"

    proposed_quote = str(raw.get("quote") or "")
    if proposed_quote:
        if len(proposed_quote) > budgets.max_quote_chars:
            return None, "quote_budget_exceeded"
        window = content[declared_start:declared_end]
        first = window.find(proposed_quote)
        if first < 0:
            return None, "quote_not_in_exact_ref"
        if window.find(proposed_quote, first + 1) >= 0:
            return None, "ambiguous_quote_in_exact_ref"
        span_start = declared_start + first
        span_end = span_start + len(proposed_quote)
        quote = proposed_quote
    else:
        quote = content[declared_start:declared_end]
        if len(quote) > budgets.max_quote_chars:
            return None, "quote_budget_exceeded"
        span_start = declared_start
        span_end = declared_end

    session_id = str(row.get("session_id") or "")
    session_dates = getattr(engine, "_session_occurrence_dates", {}) or {}
    sidecar_session_date = str(session_dates.get(session_id) or "").strip() or None
    session_date = sidecar_session_date
    source_observed_at = _valid_epoch(row.get("observed_at"))
    if session_date is None and source_observed_at is not None:
        session_date = (
            datetime.fromtimestamp(source_observed_at, tz=timezone.utc)
            .date()
            .isoformat()
        )
    ingested_at = _valid_epoch(row.get("ingested_at")) or _valid_epoch(
        row.get("timestamp")
    )
    occurrence = resolve_occurrence_time(
        quote,
        observed_at=source_observed_at or 0.0,
        session_date=session_date,
    )
    # ground_evidence re-resolves this record from the exact quote.  Supplying
    # the source-session anchor makes unknown occurrence valid while keeping it
    # distinct from observation time.
    grounding_occurrence = dict(occurrence)
    if session_date:
        grounding_occurrence["session_date"] = session_date

    operand: dict[str, Any] = {
        "store_id": store_id,
        "span_start": span_start,
        "span_end": span_end,
        "quote": quote,
        "occurrence_time": grounding_occurrence,
    }
    value = raw.get("value")
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        operand["value"] = value
    elif value is None:
        operand["value"] = None
    raw_unit = str(raw.get("unit") or "").strip()
    raw_key = str(raw.get("key") or "").strip()
    raw_label = str(raw.get("label") or "").strip()
    if len(raw_unit) > 100:
        return None, "unit_budget_exceeded"
    if len(raw_key) > 300:
        return None, "key_budget_exceeded"
    if len(raw_label) > 300:
        return None, "label_budget_exceeded"
    unit = _canonical_unit(raw_unit)
    key = _canonical_key(raw_key)
    label = raw_label or None
    if label and label.casefold() not in quote.casefold():
        return None, "label_not_in_exact_quote"

    # Counts need identity, not a synthetic numeric "1".  Dropping that value
    # avoids pretending that an unattached unit was a measured quantity.
    if plan is not None and plan.operation == "count_distinct":
        operand["value"] = None
        unit = None
    if unit:
        operand["unit"] = unit
    if key:
        operand["key"] = key
    if label:
        operand["label"] = label
    if occurrence.get("event_date"):
        operand["date"] = occurrence["event_date"]

    exact_ref = f"lcm:{store_id}:{span_start}-{span_end}"
    public = {
        "exact_ref": exact_ref,
        "store_id": store_id,
        "span_start": span_start,
        "span_end": span_end,
        "quote": quote,
        "role": row.get("role") or "unknown",
        "source": row.get("source") or "",
        "session_id": session_id,
        "observation_time": _observation_time(row, sidecar_session_date),
        "occurrence_time": {
            "observed_at": _observation_time(row, sidecar_session_date).get(
                "observed_at"
            ),
            "stored_at": ingested_at,
            "occurred_at": occurrence.get("event_at"),
            "event_at": occurrence.get("event_at"),
            "event_date": occurrence.get("event_date"),
            "event_time_source": occurrence.get("event_time_source", "unknown"),
            "precision": occurrence.get("precision", "unknown"),
            "reason": occurrence.get("reason"),
        },
        "facets": {
            key_name: operand[key_name]
            for key_name in ("value", "unit", "key", "label", "date")
            if key_name in operand and operand[key_name] is not None
        },
    }
    return ResolvedCandidate(operand=operand, public=public), None


def _completeness(
    plan: EvidencePlan | None,
    *,
    grounded_count: int,
    rejected_count: int,
) -> dict[str, Any]:
    if plan is None:
        return {
            "state": "partial",
            "reason_code": "no_supported_operation",
            "product_verified": False,
        }
    if rejected_count:
        return {
            "state": "partial",
            "reason_code": "operand_grounding_incomplete",
            "product_verified": False,
        }
    if plan.exact_operands is not None and grounded_count == plan.exact_operands:
        return {
            "state": "closed",
            "reason_code": "fixed_cardinality_satisfied",
            "product_verified": True,
        }
    if not plan.requires_complete_evidence and grounded_count >= plan.minimum_operands:
        return {
            "state": "closed",
            "reason_code": "minimum_cardinality_satisfied",
            "product_verified": True,
        }
    return {
        "state": "partial",
        "reason_code": "open_cardinality_not_product_closed",
        "product_verified": False,
    }


def _diverse_evidence(
    candidates: Sequence[ResolvedCandidate],
    *,
    budgets: PackBudgets,
) -> tuple[list[ResolvedCandidate], int]:
    selected: list[ResolvedCandidate] = []
    session_counts: dict[str, int] = {}
    date_counts: dict[str, int] = {}
    dropped = 0
    for index, candidate in enumerate(candidates):
        session = str(candidate.public.get("session_id") or f"missing:{index}")
        observation = candidate.public.get("observation_time")
        observed_date = (
            str(observation.get("date") or f"unknown:{index}")
            if isinstance(observation, Mapping)
            else f"unknown:{index}"
        )
        if session_counts.get(session, 0) >= budgets.max_per_session:
            dropped += 1
            continue
        if date_counts.get(observed_date, 0) >= budgets.max_per_date:
            dropped += 1
            continue
        session_counts[session] = session_counts.get(session, 0) + 1
        date_counts[observed_date] = date_counts.get(observed_date, 0) + 1
        selected.append(candidate)
    return selected, dropped


def _run_retrieval_probe(
    question: str,
    *,
    seen_refs: Sequence[str],
    budgets: PackBudgets,
    retrieve: Callable[[dict[str, Any]], Any] | None,
) -> dict[str, Any]:
    if budgets.max_retrieval_calls == 0:
        return {
            "status": "disabled",
            "query_calls": 0,
            "novel_exact_refs": [],
            "usage": {},
        }
    if retrieve is None:
        return {
            "status": "unavailable",
            "query_calls": 0,
            "novel_exact_refs": [],
            "usage": {},
        }
    started = time.perf_counter()
    try:
        raw = retrieve(
            {
                "query": question,
                "include": "verbatim",
                "detail": "answer_ready",
                "limit": budgets.max_novel_refs,
                "scope_bias": 0.0,
                "seen_refs": list(seen_refs),
                "include_occurrence_time": True,
            }
        )
        payload = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(payload, Mapping):
            raise ValueError("retrieval result is not an object")
        if payload.get("error"):
            raise ValueError(str(payload.get("error"))[:300])
        novel: list[str] = []
        seen = set(seen_refs)
        for hit in payload.get("hits", []):
            if not isinstance(hit, Mapping):
                continue
            exact_ref = str(hit.get("exact_ref") or "").strip()
            if (
                exact_ref
                and _EXACT_REF_RE.fullmatch(exact_ref)
                and exact_ref not in seen
                and exact_ref not in novel
            ):
                novel.append(exact_ref)
                if len(novel) >= budgets.max_novel_refs:
                    break
        metrics = payload.get("metrics")
        metric_map = metrics if isinstance(metrics, Mapping) else {}
        usage = {
            key: metric_map[key]
            for key in (
                "embedding_query_calls",
                "embedding_query_tokens",
                "embedding_query_tokens_complete",
                "embedding_queries",
            )
            if key in metric_map
        }
        delta = payload.get("delta")
        no_novel = (
            not novel
            and isinstance(delta, Mapping)
            and delta.get("termination_reason") == "no_novel_exact_ref"
        )
        return {
            "status": (
                "novel_refs_available"
                if novel
                else "no_novel"
                if no_novel
                else "no_progress"
            ),
            "query_calls": 1,
            "novel_exact_refs": novel,
            "usage": usage,
            "coverage": payload.get("provenance", {}).get("coverage", {})
            if isinstance(payload.get("provenance"), Mapping)
            else {},
            "latency_ms": round((time.perf_counter() - started) * 1_000.0, 3),
        }
    except Exception as exc:
        return {
            "status": "error",
            "query_calls": 1,
            "novel_exact_refs": [],
            "usage": {},
            "reason": str(exc)[:300],
            "latency_ms": round((time.perf_counter() - started) * 1_000.0, 3),
        }


def _latest_selection(
    plan: EvidencePlan | None,
    candidates: Sequence[ResolvedCandidate],
) -> dict[str, Any] | None:
    if plan is None or plan.operation != "latest_fact" or not candidates:
        return None
    occurrence_dates = [
        str(candidate.public.get("occurrence_time", {}).get("event_date") or "")
        for candidate in candidates
    ]
    if all(occurrence_dates):
        basis = "occurrence_time"
        dates = occurrence_dates
    else:
        observations = [
            candidate.public.get("observation_time", {}) for candidate in candidates
        ]
        observation_dates = [
            str(item.get("date") or "") if isinstance(item, Mapping) else ""
            for item in observations
        ]
        observation_sources = [
            str(item.get("source") or "") if isinstance(item, Mapping) else ""
            for item in observations
        ]
        if not all(observation_dates) or any(
            source in {"", "unavailable", "ingest_fallback"}
            for source in observation_sources
        ):
            return {
                "status": "fallback",
                "basis": None,
                "exact_refs": [],
                "reason_code": "latest_candidates_lack_common_source_time_basis",
            }
        basis = "observation_time"
        dates = observation_dates
    latest = max(dates)
    refs = [
        candidate.public["exact_ref"]
        for candidate, candidate_date in zip(candidates, dates)
        if candidate_date == latest
    ]
    return {
        "status": "selected" if len(refs) == 1 else "fallback",
        "basis": basis,
        "exact_refs": refs if len(refs) == 1 else [],
        "reason_code": (
            "latest_unique_bounded_candidate"
            if len(refs) == 1
            else "latest_candidates_tied"
        ),
    }


def build_evidence_pack(
    args: Mapping[str, Any],
    *,
    engine: Any,
    retrieve: Callable[[dict[str, Any]], Any] | None = None,
) -> str:
    started = time.perf_counter()
    question = str(args.get("question") or "").strip()
    if not question:
        return json.dumps({"status": "fallback", "reason_code": "question_required"})
    question_date, date_error = normalize_question_date(args.get("question_date"))
    if date_error:
        return json.dumps(
            {
                "status": "fallback",
                "reason_code": date_error,
                "question_date": None,
            }
        )
    normalized_question_date = question_date.day.isoformat() if question_date else None
    plan_decision = compile_evidence_plan(question, normalized_question_date)
    plan = plan_decision.plan if plan_decision.status == "planned" else None
    budgets = _parse_budgets(args.get("budgets"))
    raw_refs = args.get("baseline_refs")
    if not isinstance(raw_refs, list) or not raw_refs:
        return json.dumps(
            {
                "status": "fallback",
                "reason_code": "baseline_refs_required",
                "question_date": question_date.public_dict() if question_date else None,
                "budgets": budgets.public_dict(),
            }
        )
    input_count = len(raw_refs)
    processed_refs = raw_refs[: budgets.max_refs]

    resolved: list[ResolvedCandidate] = []
    rejections: list[dict[str, Any]] = []
    seen_exact_refs: set[str] = set()
    deduplicated = 0
    for index, raw in enumerate(processed_refs):
        candidate, error = _resolve_candidate(
            raw,
            engine=engine,
            budgets=budgets,
            plan=plan,
        )
        if error:
            rejections.append({"index": index, "reason_code": error})
            continue
        exact_ref = candidate.public["exact_ref"]  # type: ignore[union-attr]
        if exact_ref in seen_exact_refs:
            deduplicated += 1
            continue
        seen_exact_refs.add(exact_ref)
        resolved.append(candidate)  # type: ignore[arg-type]
    all_resolved_refs = [item.public["exact_ref"] for item in resolved]

    as_of = (
        question_date_as_of_epoch(normalized_question_date)
        if normalized_question_date
        else None
    )
    exclusions: list[dict[str, Any]] = []
    if as_of is not None:
        as_of_eligible: list[ResolvedCandidate] = []
        for item in resolved:
            observation = item.public.get("observation_time")
            observed_at = (
                _valid_epoch(observation.get("observed_at"))
                if isinstance(observation, Mapping)
                else None
            )
            if observed_at is not None and observed_at > as_of:
                exclusions.append(
                    {
                        "exact_ref": item.public["exact_ref"],
                        "reason_code": "source_observed_after_question_as_of",
                    }
                )
                continue
            as_of_eligible.append(item)
        resolved = as_of_eligible
    grounding = (
        ground_evidence(
            [item.operand for item in resolved],
            messages=engine._store,
            assertions=getattr(engine, "_assertions", None),
            as_of=as_of,
            session_dates=getattr(engine, "_session_occurrence_dates", None),
        )
        if resolved
        else None
    )
    if grounding is None or grounding.status != "grounded":
        if resolved:
            rejections.append(
                {
                    "index": None,
                    "reason_code": "operand_grounding_failed",
                    "reason": grounding.reason
                    if grounding is not None
                    else "no evidence",
                }
            )
        grounded_operands: Sequence[Any] = ()
    else:
        grounded_operands = grounding.operands
    grounded_resolved = (
        resolved if grounding is not None and grounding.status == "grounded" else []
    )

    completeness = _completeness(
        plan,
        grounded_count=len(grounded_operands),
        rejected_count=len(rejections),
    )
    retrieval = (
        _run_retrieval_probe(
            question,
            seen_refs=all_resolved_refs,
            budgets=budgets,
            retrieve=retrieve,
        )
        if plan is not None
        and plan.requires_complete_evidence
        and completeness["state"] != "closed"
        else {
            "status": "not_needed",
            "query_calls": 0,
            "novel_exact_refs": [],
            "usage": {},
        }
    )
    computation: dict[str, Any] | None = None
    computation_reason: str | None = None
    if plan is not None and completeness["state"] == "closed":
        alignment_error = validate_selector_alignment(question, plan, grounded_operands)
        if alignment_error:
            computation_reason = alignment_error
        else:
            computed = execute_plan(plan, grounded_operands)
            if computed.status == "computed" and computed.trace is not None:
                computation = computed.trace.as_dict()
            else:
                computation_reason = computed.reason
    elif plan is not None:
        computation_reason = completeness["reason_code"]

    if computation is not None:
        status = "computed"
    elif grounded_resolved:
        status = "evidence_ready"
    else:
        status = "fallback"
    output_evidence, diversity_dropped = (
        _diverse_evidence(grounded_resolved, budgets=budgets)
        if plan is None or plan.requires_complete_evidence
        else (list(grounded_resolved), 0)
    )
    selection = _latest_selection(plan, output_evidence)
    response = {
        "status": status,
        "version": EVIDENCE_PACK_VERSION,
        "question_date": question_date.public_dict() if question_date else None,
        "intent": {
            "status": plan_decision.status,
            "reason": plan_decision.reason,
            "operation": plan.operation if plan else None,
            "plan": plan.as_dict() if plan else None,
        },
        "evidence": [item.public for item in output_evidence],
        "rejections": rejections,
        "exclusions": exclusions,
        "completeness": completeness,
        "computation": computation,
        "computation_fallback_reason": computation_reason,
        "selection": selection,
        "retrieval": retrieval,
        "budgets": budgets.public_dict(),
        "truncation": {
            "refs_truncated": input_count > len(processed_refs),
            "quotes_truncated": False,
            "response_truncated": False,
        },
        "provenance": {
            "runtime_inputs": [
                "question",
                "question_date",
                "bounded_baseline_refs",
                "budgets",
            ],
            "storage": "same_lcm_db",
            "provider": "none",
            "model": "none",
            "exact_span_policy": "unique_quote_within_declared_exact_ref",
            "occurrence_policy": "occurrence-time-v1",
            "open_cardinality_policy": "never_close_from_caller_assertion",
        },
        "metrics": {
            "input_ref_count": input_count,
            "processed_ref_count": len(processed_refs),
            "unique_evidence_count": len(resolved),
            "deduplicated_ref_count": deduplicated,
            "diversity_dropped_count": diversity_dropped,
            "rejected_ref_count": len(rejections),
            "latency_ms": round((time.perf_counter() - started) * 1_000.0, 3),
        },
    }
    encoded = json.dumps(response, ensure_ascii=False)
    if len(encoded) <= RESPONSE_CHAR_CAP:
        return encoded
    response["evidence"] = []
    response["truncation"]["response_truncated"] = True
    response["status"] = "fallback"
    response["completeness"] = {
        "state": "partial",
        "reason_code": "response_char_cap_exceeded",
        "product_verified": False,
    }
    response["computation"] = None
    response["computation_fallback_reason"] = "response_char_cap_exceeded"
    response["selection"] = None
    encoded = json.dumps(response, ensure_ascii=False)
    if len(encoded) <= RESPONSE_CHAR_CAP:
        return encoded
    response["rejections"] = response["rejections"][:10]
    response["exclusions"] = response["exclusions"][:10]
    response["retrieval"] = {
        "status": "truncated",
        "query_calls": response["retrieval"].get("query_calls", 0),
        "novel_exact_refs": [],
        "usage": {},
    }
    return json.dumps(response, ensure_ascii=False)

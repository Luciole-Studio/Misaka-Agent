"""Selective, baseline-first expansion of exact evidence from represented sessions.

Ordinary questions never read session history.  Routed questions may expand a
small number of sessions already present in the answer-ready baseline.  The
result is advisory evidence, not a claim of exhaustive coverage or final prose.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import time
from typing import Any, Mapping, Sequence


SELECTIVE_RECALL_VERSION = "selective-recall-v1"
_EXACT_REF_RE = re.compile(r"^lcm:(?P<store_id>[1-9]\d*):(?P<start>\d+)-(?P<end>\d+)$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class SessionBundleBudgets:
    max_sessions: int = 3
    max_messages_per_session: int = 4
    max_novel_refs: int = 4
    max_context_chars: int = 3_000
    max_quote_chars: int = 600

    def public_dict(self) -> dict[str, int]:
        return {
            "max_sessions": self.max_sessions,
            "max_messages_per_session": self.max_messages_per_session,
            "max_novel_refs": self.max_novel_refs,
            "max_context_chars": self.max_context_chars,
            "max_quote_chars": self.max_quote_chars,
        }


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(maximum, max(minimum, parsed))


def _budgets(raw: Any) -> SessionBundleBudgets:
    value = raw if isinstance(raw, Mapping) else {}
    return SessionBundleBudgets(
        max_sessions=_bounded_int(value.get("max_sessions"), default=3, minimum=1, maximum=5),
        max_messages_per_session=_bounded_int(
            value.get("max_messages_per_session"), default=4, minimum=1, maximum=12
        ),
        max_novel_refs=_bounded_int(
            value.get("max_novel_refs"), default=4, minimum=1, maximum=12
        ),
        max_context_chars=_bounded_int(
            value.get("max_context_chars"), default=3_000, minimum=512, maximum=16_000
        ),
        max_quote_chars=_bounded_int(
            value.get("max_quote_chars"), default=600, minimum=160, maximum=2_400
        ),
    )


def _sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _strict_date(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if not _ISO_DATE_RE.fullmatch(raw):
        raise ValueError("question date must be ISO YYYY-MM-DD")
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError("question date must be a real calendar date") from exc


def route_selective_recall(
    question: str, question_date: str | None = None
) -> dict[str, str]:
    """Route only generic temporal, multi-evidence, update, or preference cues."""
    text = " ".join(str(question or "").casefold().split())
    # The explicit date is an anchor, not by itself a reason to expand memory.
    cues = (
        r"\bwhen\b",
        r"\b(?:today|yesterday|tomorrow)\b",
        r"\b(?:day|days|week|weeks|month|months|year|years)\s+ago\b",
        r"\b(?:before|after|earlier|later|earliest|latest|previous|prior|first|last)\b",
        r"\b(?:order|sequence|timeline|chronological)\b",
        r"\b(?:all|both|each|combined|across|several|multiple)\b",
        r"\b(?:how many|how much|total|sum|difference)\b",
        r"\b(?:usual|normally|typically|preferences?|preferred)\b",
        r"\b(?:plan|planning|planned)\s+to\b",
        r"\b(?:used to|no longer|currently|now|moved|changed|updated)\b",
    )
    if text and any(re.search(pattern, text) for pattern in cues):
        return {
            "version": SELECTIVE_RECALL_VERSION,
            "route": "session_bundle",
            "reason_code": "generic_selective_cue",
        }
    return {
        "version": SELECTIVE_RECALL_VERSION,
        "route": "ordinary",
        "reason_code": "no_selective_cue",
    }


def _parse_ref(raw: Any, engine: Any) -> dict[str, Any] | None:
    candidate = {"exact_ref": raw} if isinstance(raw, str) else dict(raw) if isinstance(raw, Mapping) else {}
    exact_ref = str(candidate.get("exact_ref") or "").strip()
    match = _EXACT_REF_RE.fullmatch(exact_ref)
    if match is None:
        store_id_raw = candidate.get("store_id")
        try:
            store_id = int(store_id_raw)
        except (TypeError, ValueError, OverflowError):
            return None
        row = engine._store.get(store_id)
        if row is None:
            return None
        content = str(row.get("content") or "")
        start = _bounded_int(candidate.get("content_offset"), default=0, minimum=0, maximum=len(content))
        quote = str(candidate.get("quote") or candidate.get("content") or "")
        end = min(len(content), start + len(quote)) if quote else len(content)
        if end <= start:
            return None
        exact_ref = f"lcm:{store_id}:{start}-{end}"
        match = _EXACT_REF_RE.fullmatch(exact_ref)
    assert match is not None
    store_id = int(match.group("store_id"))
    start = int(match.group("start"))
    end = int(match.group("end"))
    row = engine._store.get(store_id)
    if row is None:
        return None
    content = str(row.get("content") or "")
    if start < 0 or end <= start or end > len(content):
        return None
    return {
        "exact_ref": exact_ref,
        "store_id": store_id,
        "start": start,
        "end": end,
        "session_id": str(row.get("session_id") or ""),
    }


def _base_result(
    *,
    route: str,
    status: str,
    reason_code: str,
    baseline: Sequence[Mapping[str, Any]],
    budgets: SessionBundleBudgets,
    started: float,
) -> dict[str, Any]:
    baseline_refs = [str(item["exact_ref"]) for item in baseline]
    return {
        "version": SELECTIVE_RECALL_VERSION,
        "route": route,
        "status": status,
        "reason_code": reason_code,
        "context": None,
        "baseline": {
            "exact_ref_count": len(baseline_refs),
            "exact_refs_sha256": _sha256(baseline_refs),
        },
        "selected_sessions": [],
        "novel_exact_refs": [],
        "budgets": budgets.public_dict(),
        "metrics": {
            "session_loads": 0,
            "context_chars": 0,
            "latency_ms": round((time.perf_counter() - started) * 1_000, 3),
        },
        "trace": {"context_sha256": None, "truncated": False},
        "provenance": {
            "storage": "same_lcm_db",
            "selector_calls": 0,
            "provider_calls": 0,
            "source_time": "observed_at_or_explicit_adapter_date_or_unknown",
            "unknown_source_time_valid": True,
            "finite_coverage_claimed": False,
            "final_prose_cached": False,
        },
    }


def _source_date(engine: Any, row: Mapping[str, Any]) -> tuple[str, str]:
    observed = row.get("observed_at")
    if isinstance(observed, (int, float)) and observed > 0:
        return datetime.fromtimestamp(float(observed), timezone.utc).date().isoformat(), "host_message_timestamp"
    session_dates = getattr(engine, "_session_occurrence_dates", {}) or {}
    raw = session_dates.get(str(row.get("session_id") or ""))
    if raw:
        try:
            return _strict_date(raw) or "unknown", "adapter_session_date"
        except ValueError:
            pass
    return "unknown", "unknown"


def _is_covered(store_id: int, start: int, end: int, baseline: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        int(item["store_id"]) == store_id
        and int(item["start"]) <= start
        and int(item["end"]) >= end
        for item in baseline
    )


def build_selective_session_bundle(
    question: str,
    *,
    engine: Any,
    baseline_refs: Sequence[Any] = (),
    question_date: str | None = None,
    enabled: bool = False,
    budgets: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return bounded novel exact evidence or an unchanged-baseline result."""
    started = time.perf_counter()
    limits = _budgets(budgets)
    baseline: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    for raw in list(baseline_refs)[:50]:
        parsed = _parse_ref(raw, engine)
        if parsed is not None and parsed["exact_ref"] not in seen_refs:
            baseline.append(parsed)
            seen_refs.add(parsed["exact_ref"])
    routed = route_selective_recall(question, question_date)
    if not enabled:
        return _base_result(
            route=routed["route"], status="no_augmentation", reason_code="disabled",
            baseline=baseline, budgets=limits, started=started,
        )
    if routed["route"] == "ordinary":
        return _base_result(
            route="ordinary", status="no_augmentation", reason_code="ordinary_baseline",
            baseline=baseline, budgets=limits, started=started,
        )
    try:
        _strict_date(question_date)
    except ValueError:
        return _base_result(
            route="session_bundle", status="no_augmentation", reason_code="question_date_invalid",
            baseline=baseline, budgets=limits, started=started,
        )
    all_session_ids: list[str] = []
    session_anchors: dict[str, list[int]] = {}
    for item in baseline:
        session_id = item["session_id"]
        if session_id and session_id not in all_session_ids:
            all_session_ids.append(session_id)
        if session_id:
            anchors = session_anchors.setdefault(session_id, [])
            store_id = int(item["store_id"])
            if store_id not in anchors:
                anchors.append(store_id)
    session_ids = all_session_ids[: limits.max_sessions]
    result = _base_result(
        route="session_bundle", status="no_augmentation", reason_code="no_novel_exact_evidence",
        baseline=baseline, budgets=limits, started=started,
    )
    result["selected_sessions"] = session_ids
    if not session_ids:
        result["reason_code"] = "no_represented_session"
        return result

    evidence: list[dict[str, Any]] = []
    truncated = len(all_session_ids) > len(session_ids)
    try:
        for session_id in session_ids:
            rows_by_id: dict[int, dict[str, Any]] = {}
            anchors = session_anchors.get(session_id, []) or [0]
            per_anchor = max(1, limits.max_messages_per_session // len(anchors))
            before = min(2, per_anchor // 2)
            after = max(0, per_anchor - before)
            for anchor in anchors:
                window = engine._store.load_session_window(
                    session_id,
                    anchor_store_id=anchor,
                    before=before,
                    after=after,
                )
                for row in window:
                    rows_by_id[int(row["store_id"])] = row
            rows = [rows_by_id[key] for key in sorted(rows_by_id)]
            result["metrics"]["session_loads"] += 1
            if len(rows_by_id) > limits.max_messages_per_session + len(anchors):
                truncated = True
            session_added = 0
            for row in rows:
                content = str(row.get("content") or "")
                if not content:
                    continue
                end = min(len(content), limits.max_quote_chars)
                store_id = int(row["store_id"])
                if _is_covered(store_id, 0, end, baseline):
                    continue
                exact_ref = f"lcm:{store_id}:0-{end}"
                if exact_ref in seen_refs:
                    continue
                date, date_source = _source_date(engine, row)
                evidence.append(
                    {
                        "exact_ref": exact_ref,
                        "session_id": session_id,
                        "role": str(row.get("role") or "unknown"),
                        "quote": content[:end],
                        "date": date,
                        "date_source": date_source,
                    }
                )
                session_added += 1
                seen_refs.add(exact_ref)
                if (
                    len(evidence) >= limits.max_novel_refs
                    or session_added >= limits.max_messages_per_session
                ):
                    truncated = True
                    break
            if len(evidence) >= limits.max_novel_refs:
                break
    except Exception:
        result["reason_code"] = "session_load_error"
        result["metrics"]["latency_ms"] = round((time.perf_counter() - started) * 1_000, 3)
        return result
    if not evidence:
        result["trace"]["truncated"] = truncated
        result["metrics"]["latency_ms"] = round((time.perf_counter() - started) * 1_000, 3)
        return result

    header = (
        "[Hermes-LCM selective session evidence; partial and non-exhaustive. "
        "Use only cited exact spans.]"
    )
    blocks = [header]
    admitted: list[dict[str, Any]] = []
    for item in evidence:
        block = (
            f"\n[{item['exact_ref']}; session={item['session_id']}; role={item['role']}; "
            f"date={item['date']}]\n{item['quote']}"
        )
        if len("".join(blocks)) + len(block) > limits.max_context_chars:
            truncated = True
            break
        blocks.append(block)
        admitted.append(item)
    if not admitted:
        result["trace"]["truncated"] = True
        return result
    context = "".join(blocks)
    result.update(
        {
            "status": "augmented",
            "reason_code": "novel_adjacent_exact_evidence",
            "context": context,
            "novel_exact_refs": [item["exact_ref"] for item in admitted],
            "evidence": admitted,
        }
    )
    result["metrics"].update(
        {
            "context_chars": len(context),
            "latency_ms": round((time.perf_counter() - started) * 1_000, 3),
        }
    )
    result["trace"] = {"context_sha256": hashlib.sha256(context.encode()).hexdigest(), "truncated": truncated}
    return result

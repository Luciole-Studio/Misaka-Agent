"""Provider-free answer-shape and evidence-requirement compilation.

The compiler is intentionally conservative.  It observes only a question and
an explicit as-of date, never benchmark identity or answer data.  A contract is
an evidence requirement, not an answer and not a completeness claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import re
from typing import Any, Literal

from .evidence_pack import normalize_question_date
from .reasoning import TemporalWindow, normalize_unit, resolve_temporal_window


ANSWER_CONTRACT_VERSION = "answer-contract-v1"

AnswerKind = Literal[
    "fact",
    "person",
    "place",
    "time",
    "duration",
    "quantity",
    "event",
    "advice",
    "state",
    "list",
]
ContractOperation = Literal[
    "none",
    "scalar",
    "sum",
    "difference",
    "date_interval",
    "date_filter",
    "count_distinct",
    "order",
    "latest",
    "previous",
]
CoveragePolicy = Literal[
    "source_asserted_fact",
    "source_asserted_or_finite_enumeration",
    "fixed_operands",
    "finite_enumeration",
]


@dataclass(frozen=True)
class EvidenceSlot:
    name: str
    anchor: str | None
    value_type: Literal["number", "date", "person", "place", "text", "event"]
    unit: str | None = None
    expected_role: Literal["any", "user", "assistant"] = "any"
    required: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "anchor": self.anchor,
            "value_type": self.value_type,
            "unit": self.unit,
            "expected_role": self.expected_role,
            "required": self.required,
        }


@dataclass(frozen=True)
class AnswerContract:
    answer_kind: AnswerKind
    operation: ContractOperation
    anchors: tuple[str, ...]
    slots: tuple[EvidenceSlot, ...]
    coverage_policy: CoveragePolicy
    confidence: Literal["high", "medium"]
    question_as_of: str | None = None
    temporal_window: TemporalWindow | None = None
    finite_cardinality: int | None = None
    requested_unit: str | None = None
    difference_direction: Literal[
        "absolute", "first_minus_second", "second_minus_first"
    ] | None = None
    order_direction: Literal["ascending", "descending"] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": ANSWER_CONTRACT_VERSION,
            "answer_kind": self.answer_kind,
            "operation": self.operation,
            "anchors": list(self.anchors),
            "slots": [slot.as_dict() for slot in self.slots],
            "coverage_policy": self.coverage_policy,
            "confidence": self.confidence,
            "question_as_of": self.question_as_of,
            "temporal_window": (
                self.temporal_window.as_dict() if self.temporal_window else None
            ),
            "finite_cardinality": self.finite_cardinality,
            "requested_unit": self.requested_unit,
            "difference_direction": self.difference_direction,
            "order_direction": self.order_direction,
        }


@dataclass(frozen=True)
class ContractDecision:
    status: Literal["planned", "not_applicable", "fallback"]
    contract: AnswerContract | None = None
    reason_code: str = ""


_WORD_NUMBERS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
_TEMPORAL_RELATIVE_RE = re.compile(
    r"\b(?:ago|yesterday|last\s+(?:week|month|year|monday|tuesday|wednesday|"
    r"thursday|friday|saturday|sunday)|this\s+(?:week|month|year))\b",
    re.IGNORECASE,
)
_UNSUPPORTED_RE = re.compile(
    r"\b(?:average|mean|median|percentage|percent|ratio|rate|multiply|divide)\b|"
    r"\bproduct\s+of\b",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_STOP = frozenset(
    {
        "a",
        "about",
        "ago",
        "all",
        "am",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "between",
        "both",
        "by",
        "can",
        "combined",
        "current",
        "currently",
        "day",
        "days",
        "did",
        "do",
        "does",
        "doing",
        "for",
        "from",
        "happened",
        "has",
        "have",
        "how",
        "i",
        "in",
        "instead",
        "is",
        "it",
        "latest",
        "long",
        "many",
        "me",
        "most",
        "much",
        "my",
        "need",
        "now",
        "of",
        "on",
        "or",
        "previous",
        "previously",
        "recent",
        "save",
        "should",
        "spent",
        "spend",
        "take",
        "taking",
        "than",
        "that",
        "the",
        "these",
        "this",
        "time",
        "to",
        "total",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "will",
        "with",
        "would",
        "year",
        "week",
        "weeks",
        "month",
        "months",
    }
    | set(_WORD_NUMBERS)
)


def _canonical_unit(raw: str | None) -> str | None:
    text = str(raw or "").strip().casefold()
    if not text:
        return None
    irregular = {
        "people": "person",
        "persons": "person",
        "properties": "property",
        "vacations": "vacation",
        "holidays": "holiday",
        "clothes": "clothing_item",
        "clothing": "clothing_item",
    }
    if text in irregular:
        return irregular[text]
    normalized = normalize_unit(text)
    if normalized != text:
        return normalized
    if text.endswith("ies") and len(text) > 4:
        return text[:-3] + "y"
    if text.endswith("s") and not text.endswith("ss") and len(text) > 3:
        return text[:-1]
    return text.replace(" ", "_")


def _requested_unit(text: str) -> str | None:
    normalized = text.casefold()
    has_time_dimension = bool(
        re.search(r"\b(?:time|minutes?|hours?|days?|weeks?|months?|years?)\b", normalized)
    )
    if re.search(
        r"(?:[$€£]|\b(?:usd|dollars?|money|cost|costs|fare|fares|fee|fees|"
        r"price|prices|savings)\b)",
        normalized,
    ) or (
        not has_time_dimension
        and re.search(r"\bhow much\b.{0,80}\b(?:spend|spent|save|saved)\b", normalized)
    ):
        return "usd"
    match = re.search(
        r"\bhow\s+many\s+(minutes?|hours?|days?|weeks?|months?|years?|points?|pages?)\b",
        normalized,
    )
    if match:
        return _canonical_unit(match.group(1))
    match = re.search(
        r"\bhow\s+many\s+(.{1,80}?)\s+"
        r"(?:do|did|have|has|am|is|are|was|were|will|would|can|could|should|need)\b",
        normalized,
    )
    if match:
        phrase = re.sub(r"^(?:different|total|number\s+of)\s+", "", match.group(1))
        if re.search(r"\bitems?\s+of\s+clothing\b", phrase):
            return "clothing_item"
        # Alternatives name a facet set, not a trustworthy scalar dimension.
        if not re.search(r"\b(?:and|or)\b", phrase):
            words = re.findall(r"[a-z][a-z-]*", phrase)
            if words:
                return _canonical_unit(words[-1])
    match = re.search(
        r"\b(?:in|measured in)\s+(minutes?|hours?|days?|weeks?|months?)\b",
        normalized,
    )
    if match:
        return _canonical_unit(match.group(1))
    match = re.search(
        r"\b(?:the\s+)?(?:one|two|three|four|five|six|seven|eight|nine|ten|"
        r"eleven|twelve|\d{1,2})\s+(?:named\s+)?([a-z][a-z-]*)s?\b",
        normalized,
    )
    return _canonical_unit(match.group(1)) if match else None


def _anchors(text: str, *, unit: str | None = None) -> tuple[str, ...]:
    quoted = [
        " ".join(item.split()).casefold()
        for item in re.findall(r"['\"]([^'\"]{1,160})['\"]", text)
    ]
    tokens = []
    for token in _TOKEN_RE.findall(text):
        normalized = token.casefold()
        if len(normalized) < 3 or normalized in _STOP:
            continue
        if unit and normalized in {unit, f"{unit}s", unit.replace("_", " ")}:
            continue
        if normalized not in tokens:
            tokens.append(normalized)
    return tuple(dict.fromkeys([*quoted, *tokens]))[:12]


def _explicit_cardinality(text: str) -> int | None:
    normalized = text.casefold()
    if re.search(r"\bboth\b", normalized):
        return 2
    match = re.search(
        r"\b(?:the\s+)?(one|two|three|four|five|six|seven|eight|nine|ten|"
        r"eleven|twelve|\d{1,2})\s+(?:named\s+)?[a-z][a-z-]*s?\b",
        normalized,
    )
    if not match:
        return None
    raw = match.group(1)
    value = int(raw) if raw.isdigit() else _WORD_NUMBERS.get(raw)
    return value if value is not None and 1 <= value <= 12 else None


def _clean_operand(value: str) -> str | None:
    text = " ".join(value.strip(" ,.?\"'").casefold().split())
    text = re.sub(r"^(?:a|an|the|doing|taking|using|on)\s+", "", text)
    text = re.sub(r"\s+(?:did|do|was|were|is|are)$", "", text)
    tokens = [token for token in _TOKEN_RE.findall(text) if token not in _STOP]
    if not tokens:
        return None
    return " ".join(tokens[-4:])


def _named_operands(text: str, operation: str) -> tuple[str, ...]:
    normalized = " ".join(text.split())
    quoted = tuple(
        value
        for raw in re.findall(r"['\"]([^'\"]{1,120})['\"]", normalized)
        if (value := _clean_operand(raw)) is not None
    )
    if 2 <= len(quoted) <= 8:
        return quoted
    if operation == "difference":
        instead = re.search(
            r"\b(?:taking|using|choosing|with)?\s*(?:the\s+)?(.{1,80}?)\s+"
            r"instead\s+of\s+(?:taking|using|choosing|with)?\s*(?:a|an|the)?\s*"
            r"(.{1,80}?)(?:\?|$)",
            normalized,
            re.IGNORECASE,
        )
        if instead:
            values = tuple(
                value
                for raw in instead.groups()
                if (value := _clean_operand(raw)) is not None
            )
            if len(values) == 2:
                return values
        versus = re.search(
            r"\b([A-Za-z][A-Za-z0-9 _-]{0,50}?)\s+(?:versus|vs\.?|than)\s+"
            r"([A-Za-z][A-Za-z0-9 _-]{0,50}?)(?:\?|$)",
            normalized,
            re.IGNORECASE,
        )
        if versus:
            values = tuple(
                value
                for raw in versus.groups()
                if (value := _clean_operand(raw)) is not None
            )
            if len(values) == 2:
                return values
    if operation == "sum":
        enumeration = re.search(
            r"\b(?:spend|spent)\s+(?:on\s+)?(.{1,240}?)(?:\?|$)|"
            r"\b(?:across|of|for|on)\s+(.{1,240}?)(?:\?|$)",
            normalized,
            re.IGNORECASE,
        )
        if enumeration:
            tail = next(group for group in enumeration.groups() if group is not None)
            parts = [part.strip() for part in tail.split(",")]
            if len(parts) >= 3:
                parts[-1] = re.sub(
                    r"^(?:and|plus)\s+", "", parts[-1], flags=re.IGNORECASE
                )
                values = tuple(
                    value
                    for raw in parts
                    if (value := _clean_operand(raw)) is not None
                )
                if len(values) == len(parts) and len(values) <= 8:
                    return values
                return ()
    pair = re.search(
        r"\b(?:spend|spent|across|between|from|of|for|doing|on)\s+(.{1,80}?)\s+"
        r"(?:and|plus)\s+(?:doing|on|the)?\s*(.{1,80}?)(?:\?|$)",
        normalized,
        re.IGNORECASE,
    )
    if pair:
        values = tuple(
            value
            for raw in pair.groups()
            if (value := _clean_operand(raw)) is not None
        )
        if len(values) == 2:
            return values
    return ()


def _answer_kind(text: str, operation: ContractOperation) -> AnswerKind:
    normalized = text.casefold()
    if operation in {"latest", "previous"}:
        if re.search(r"\bhow\s+(?:many|much)\b", normalized):
            return "quantity"
        if re.search(r"\b(?:how long|duration|personal best time|record time)\b", normalized):
            return "duration"
        if normalized.startswith("where"):
            return "place"
        if normalized.startswith("who"):
            return "person"
        return "state"
    if operation == "date_filter":
        if re.search(r"\bwhere\b", normalized):
            return "place"
        if re.search(r"\b(?:who|whom|which artist|what artist)\b", normalized):
            return "person"
        if re.search(r"\bwhen\b", normalized):
            return "time"
        return "event"
    if operation in {"sum", "difference", "count_distinct", "scalar"}:
        if re.search(r"\bhow long\b", normalized):
            return "duration"
        return "quantity"
    if operation == "date_interval" or normalized.startswith("when"):
        return "time"
    if operation == "order":
        return "list"
    if normalized.startswith("who"):
        return "person"
    if normalized.startswith("where"):
        return "place"
    if re.search(r"\b(?:advice|tips?|recommend|suggest|what should|how can)\b", normalized):
        return "advice"
    return "fact"


def _time_window(text: str, canonical_as_of: str | None) -> TemporalWindow | None:
    if canonical_as_of:
        relative = resolve_temporal_window(text, canonical_as_of)
        if relative is not None:
            return relative
        anchor = date.fromisoformat(canonical_as_of)
        normalized = text.casefold()
        rolling = re.search(
            r"\blast\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
            r"eleven|twelve)\s+(day|week|month)s?\b",
            normalized,
        )
        if rolling:
            raw = rolling.group(1)
            amount = int(raw) if raw.isdigit() else _WORD_NUMBERS[raw]
            if rolling.group(2) == "day":
                start = anchor - timedelta(days=amount - 1)
            elif rolling.group(2) == "week":
                start = anchor - timedelta(days=amount * 7 - 1)
            else:
                month_index = anchor.year * 12 + anchor.month - 1 - amount
                year, month_zero = divmod(month_index, 12)
                start = date(year, month_zero + 1, 1)
            return TemporalWindow(start, anchor + timedelta(days=1), rolling.group(0))
        month_names = {
            "january": 1,
            "february": 2,
            "march": 3,
            "april": 4,
            "may": 5,
            "june": 6,
            "july": 7,
            "august": 8,
            "september": 9,
            "october": 10,
            "november": 11,
            "december": 12,
        }
        named_month = re.search(
            r"\b(?:in|during)\s+(" + "|".join(month_names) + r")\b",
            normalized,
        )
        if named_month:
            month = month_names[named_month.group(1)]
            year = anchor.year if month <= anchor.month else anchor.year - 1
            start = date(year, month, 1)
            end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
            return TemporalWindow(start, end, f"{named_month.group(1)} {year}")
        if re.search(r"\bthis year\b", normalized):
            return TemporalWindow(
                date(anchor.year, 1, 1),
                date(anchor.year + 1, 1, 1),
                "the question-date calendar year",
            )
        if re.search(r"\blast year\b", normalized):
            return TemporalWindow(
                date(anchor.year - 1, 1, 1),
                date(anchor.year, 1, 1),
                "the calendar year before the question date",
            )
        if re.search(r"\bthis month\b", normalized):
            if anchor.month == 12:
                end = date(anchor.year + 1, 1, 1)
            else:
                end = date(anchor.year, anchor.month + 1, 1)
            return TemporalWindow(
                date(anchor.year, anchor.month, 1), end, "the question-date calendar month"
            )
        if re.search(r"\bthis week\b", normalized):
            start = anchor - timedelta(days=anchor.weekday())
            return TemporalWindow(start, start + timedelta(days=7), "the question-date week")
    return None


def compile_answer_contract(
    question: Any, question_as_of: Any = None
) -> ContractDecision:
    text = " ".join(str(question or "").split())
    if not text:
        return ContractDecision("fallback", reason_code="question_required")
    if len(text) > 4_000:
        return ContractDecision("fallback", reason_code="question_too_long")
    if _UNSUPPORTED_RE.search(text):
        return ContractDecision("fallback", reason_code="unsupported_operation")

    normalized_date, date_error = normalize_question_date(question_as_of)
    if date_error:
        return ContractDecision("fallback", reason_code="question_as_of_invalid")
    canonical_as_of = normalized_date.day.isoformat() if normalized_date else None
    normalized = text.casefold()
    relative = bool(_TEMPORAL_RELATIVE_RE.search(normalized))
    if relative and canonical_as_of is None:
        return ContractDecision("fallback", reason_code="question_as_of_required")
    window = _time_window(text, canonical_as_of)
    unit_hint = _requested_unit(text)

    operation: ContractOperation
    coverage: CoveragePolicy
    direction = None
    order_direction = None
    finite = _explicit_cardinality(text)
    named: tuple[str, ...] = ()

    if re.search(
        r"\b(?:how long|time between|how many\s+(?:calendar\s+)?(?:days?|weeks?|months?)\s+"
        r"(?:between|since|passed))\b",
        normalized,
    ) and re.search(r"\b(?:between|since|passed|from)\b", normalized):
        operation, coverage, finite = "date_interval", "fixed_operands", 2
    elif re.search(
        r"\b(?:difference|how much (?:more|less)|more than|less than|instead of|"
        r"increase|decrease|saved?|shorter|longer)\b",
        normalized,
    ):
        operation, coverage, finite = "difference", "fixed_operands", 2
        named = _named_operands(text, operation)
        if "instead of" in normalized:
            direction = "second_minus_first"
        elif re.search(r"\bincreas(?:e|ed)\s+from\b", normalized):
            direction = "second_minus_first"
        elif re.search(r"\bdecreas(?:e|ed)\s+from\b", normalized):
            direction = "first_minus_second"
        elif re.search(r"\b(?:how much less|less than|decrease|saved?|shorter)\b", normalized):
            direction = "second_minus_first"
        elif re.search(r"\b(?:how much more|more than|increase|longer)\b", normalized):
            direction = "first_minus_second"
        else:
            direction = "absolute"
    elif re.search(
        r"\b(?:total|combined|altogether|sum(?:med)?|add(?:ed)? together|in all)\b",
        normalized,
    ):
        operation, coverage = "sum", "fixed_operands"
        named = _named_operands(text, operation)
        finite = len(named) or finite or 2
    elif (
        unit_hint in {"minute", "hour", "day", "week", "month", "year"}
        and re.search(r"\bhow\s+many\b.{1,120}\b(?:and|plus)\b", normalized)
        and len(_named_operands(text, "sum")) >= 2
    ):
        operation, coverage = "sum", "fixed_operands"
        named = _named_operands(text, operation)
        finite = len(named)
    elif re.search(r"\b(?:chronolog|in order|order of|earliest to latest|latest to earliest)\b", normalized):
        operation, coverage = "order", "fixed_operands"
        order_direction = (
            "descending"
            if re.search(r"\b(?:latest first|newest first|descending|latest to earliest)\b", normalized)
            else "ascending"
        )
    elif re.search(
        r"\b(?:advice|tips?|recommend|suggest|what should|how can)\b", normalized
    ):
        operation, coverage, finite = "none", "source_asserted_fact", 1
    elif re.search(
        r"\b(?:how many|how much|how long|what (?:was|is) the (?:number|count|duration))\b",
        normalized,
    ):
        if re.fullmatch(r"how much (?:was )?it\??", normalized):
            return ContractDecision("fallback", reason_code="insufficient_anchors")
        operation = "scalar"
        source_asserted_only = bool(
            re.search(
                r"\b(?:need|required?|owe|have left|lead|manage|view|score|earn|"
                r"redeem|commute|duration)\b",
                normalized,
            )
        )
        coverage = (
            "source_asserted_or_finite_enumeration"
            if re.search(r"\b(?:how many|number|count)\b", normalized)
            and not source_asserted_only
            else "source_asserted_fact"
        )
        finite = None
    elif re.search(r"\b(?:current|currently|now|latest|most recent|these days)\b", normalized):
        operation, coverage, finite = "latest", "source_asserted_fact", 1
    elif re.search(r"\b(?:previous|previously|formerly|former|prior|before that|used to)\b", normalized):
        operation, coverage, finite = "previous", "source_asserted_fact", 1
    elif window is not None and re.search(r"\b(?:what|which|who|whom|where|when)\b", normalized):
        operation, coverage = "date_filter", "source_asserted_fact"
        finite = None
    elif re.match(r"^(?:who|where|when)\b", normalized):
        operation, coverage, finite = "none", "source_asserted_fact", 1
    elif re.match(r"^(?:what|which)\b", normalized):
        # Generic wh-words alone are not a retrieval contract.  Require a
        # content-bearing subject with enough lexical shape to avoid routing
        # vague prompts such as "What did we discuss?" or "What happened?".
        provisional_anchors = _anchors(text)
        if len(provisional_anchors) < 2:
            return ContractDecision(
                "not_applicable", reason_code="generic_question_low_confidence"
            )
        operation, coverage, finite = "none", "source_asserted_fact", 1
    else:
        return ContractDecision("not_applicable", reason_code="ordinary_or_low_confidence")

    unit = unit_hint
    anchors = _anchors(text, unit=unit)
    if not anchors and unit:
        anchors = (unit.replace("_", " "),)
    if not anchors and operation not in {"date_filter", "date_interval"}:
        return ContractDecision("fallback", reason_code="insufficient_anchors")

    kind = _answer_kind(text, operation)
    if named:
        slots = tuple(
            EvidenceSlot(
                name=f"operand_{index + 1}",
                anchor=anchor,
                value_type="date" if operation == "date_interval" else "number",
                unit=unit,
            )
            for index, anchor in enumerate(named)
        )
    elif operation in {"sum", "difference", "date_interval", "order"}:
        slot_count = finite or 2
        value_type = (
            "date"
            if operation in {"date_interval", "order"}
            else "number"
        )
        slots = tuple(
            EvidenceSlot(
                name=f"operand_{index + 1}",
                anchor=None,
                value_type=value_type,
                unit=unit,
            )
            for index in range(slot_count)
        )
    else:
        value_type = {
            "quantity": "number",
            "duration": "number",
            "time": "date",
            "person": "person",
            "place": "place",
            "event": "event",
        }.get(kind, "text")
        expected_role = "assistant" if kind == "advice" else "any"
        slots = (
            EvidenceSlot(
                name="answer",
                anchor=anchors[0] if anchors else None,
                value_type=value_type,  # type: ignore[arg-type]
                unit=unit,
                expected_role=expected_role,
            ),
        )

    contract = AnswerContract(
        answer_kind=kind,
        operation=operation,
        anchors=anchors,
        slots=slots,
        coverage_policy=coverage,
        confidence="high",
        question_as_of=canonical_as_of,
        temporal_window=window,
        finite_cardinality=finite,
        requested_unit=unit,
        difference_direction=direction,  # type: ignore[arg-type]
        order_direction=order_direction,  # type: ignore[arg-type]
    )
    return ContractDecision("planned", contract=contract, reason_code="high_confidence_contract")

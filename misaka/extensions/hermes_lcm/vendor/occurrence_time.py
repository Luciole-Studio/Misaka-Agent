"""Provider-neutral occurrence-time extraction for bounded exact evidence.

Observation time and occurrence time are deliberately separate.  The parser
only resolves dates supported by the evidence text itself, optionally anchored
to source/session metadata supplied by the host.  Failure and ambiguity are
valid ``unknown`` results; observation time is never reused as event time.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any


POLICY_VERSION = "occurrence-time-v1"

_ISO_DATE = re.compile(r"(?<!\d)(?P<year>\d{4})[-/](?P<month>\d{1,2})[-/](?P<day>\d{1,2})(?!\d)")
_RELATIVE = re.compile(
    r"\b(?:(?P<count>\d+)\s+(?P<unit>days?|weeks?|months?)\s+ago|(?P<simple>today|yesterday)|last\s+(?P<weekday>monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b",
    re.IGNORECASE,
)
_WEEKDAYS = {
    name: index
    for index, name in enumerate(
        ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    )
}


def _parse_anchor(value: Any) -> date | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10].replace("/", "-"))
    except ValueError:
        return None


def _epoch(day: date) -> float:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()


def _subtract_months(day: date, months: int) -> date:
    absolute = day.year * 12 + (day.month - 1) - months
    year, zero_month = divmod(absolute, 12)
    month = zero_month + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _unknown(
    observed_at: Any,
    *,
    session_date: Any = None,
    reason: str = "no_supported_occurrence_time",
) -> dict[str, Any]:
    try:
        observed = float(observed_at)
    except (TypeError, ValueError, OverflowError):
        observed = 0.0
    anchor = _parse_anchor(session_date)
    return {
        "observed_at": _epoch(anchor) if anchor else observed,
        "stored_at": observed,
        "event_at": None,
        "event_date": None,
        "event_time_source": "unknown",
        "session_date": anchor.isoformat() if anchor else None,
        "precision": "unknown",
        "policy_version": POLICY_VERSION,
        "reason": reason,
    }


def resolve_occurrence_time(
    text: Any,
    *,
    observed_at: Any,
    session_date: Any = None,
) -> dict[str, Any]:
    """Resolve one unambiguous day from exact evidence and source metadata."""
    content = str(text or "")
    explicit: list[tuple[date, re.Match[str]]] = []
    for match in _ISO_DATE.finditer(content):
        try:
            explicit.append(
                (
                    date(
                        int(match.group("year")),
                        int(match.group("month")),
                        int(match.group("day")),
                    ),
                    match,
                )
            )
        except ValueError:
            continue
    distinct_explicit = {item[0] for item in explicit}
    if len(distinct_explicit) > 1:
        return _unknown(
            observed_at,
            session_date=session_date,
            reason="ambiguous_multiple_explicit_dates",
        )
    if explicit:
        day, match = explicit[0]
        anchor = _parse_anchor(session_date)
        return {
            "observed_at": _epoch(anchor) if anchor else float(observed_at or 0.0),
            "stored_at": float(observed_at or 0.0),
            "event_at": _epoch(day),
            "event_date": day.isoformat(),
            "event_time_source": "explicit",
            "precision": "day",
            "policy_version": POLICY_VERSION,
            "session_date": anchor.isoformat() if anchor else None,
            "support": {
                "quote": match.group(0),
                "char_start": match.start(),
                "char_end": match.end(),
            },
        }

    matches = list(_RELATIVE.finditer(content))
    if len(matches) != 1:
        return _unknown(
            observed_at,
            session_date=session_date,
            reason="ambiguous_relative_expression" if matches else "no_supported_occurrence_time",
        )
    anchor = _parse_anchor(session_date)
    if anchor is None:
        return _unknown(
            observed_at,
            session_date=session_date,
            reason="relative_expression_without_session_date",
        )

    match = matches[0]
    simple = (match.group("simple") or "").casefold()
    if simple == "today":
        day = anchor
    elif simple == "yesterday":
        day = anchor - timedelta(days=1)
    elif match.group("weekday"):
        target = _WEEKDAYS[match.group("weekday").casefold()]
        delta = (anchor.weekday() - target) % 7
        day = anchor - timedelta(days=delta or 7)
    else:
        count = int(match.group("count"))
        unit = match.group("unit").casefold()
        if unit.startswith("day"):
            day = anchor - timedelta(days=count)
        elif unit.startswith("week"):
            day = anchor - timedelta(weeks=count)
        else:
            day = _subtract_months(anchor, count)

    return {
        "observed_at": _epoch(anchor),
        "stored_at": float(observed_at or 0.0),
        "event_at": _epoch(day),
        "event_date": day.isoformat(),
        "event_time_source": "relative_to_session",
        "session_date": anchor.isoformat(),
        "precision": "day",
        "policy_version": POLICY_VERSION,
        "support": {
            "quote": match.group(0),
            "char_start": match.start(),
            "char_end": match.end(),
        },
    }

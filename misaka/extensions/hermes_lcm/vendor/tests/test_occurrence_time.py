from datetime import datetime, timezone

from hermes_lcm.occurrence_time import resolve_occurrence_time


def _day(value):
    return datetime.fromtimestamp(value, tz=timezone.utc).date().isoformat()


def test_explicit_date_is_source_backed_and_not_observation_time():
    result = resolve_occurrence_time(
        "The launch happened on 2024-02-29.",
        observed_at=1_800_000_000,
        session_date="2026-07-19",
    )
    assert result["event_time_source"] == "explicit"
    assert _day(result["event_at"]) == "2024-02-29"
    assert result["event_at"] != result["observed_at"]
    assert result["stored_at"] == 1_800_000_000
    assert _day(result["observed_at"]) == "2026-07-19"
    assert result["support"]["quote"] == "2024-02-29"


def test_relative_occurrence_time_variants_use_session_date():
    cases = {
        "today": "2024-03-20",
        "yesterday": "2024-03-19",
        "5 days ago": "2024-03-15",
        "2 weeks ago": "2024-03-06",
        "1 month ago": "2024-02-20",
        "last monday": "2024-03-18",
    }
    for phrase, expected in cases.items():
        result = resolve_occurrence_time(
            f"It happened {phrase}.", observed_at=99, session_date="2024-03-20"
        )
        assert result["event_time_source"] == "relative_to_session"
        assert _day(result["event_at"]) == expected


def test_unknown_is_valid_without_aliasing_observation_time():
    for text, session_date in (("sometime recently", "2024-03-20"), ("yesterday", None)):
        result = resolve_occurrence_time(text, observed_at=123, session_date=session_date)
        assert result["event_time_source"] == "unknown"
        assert result["event_at"] is None
        assert result["observed_at"] == (
            datetime(2024, 3, 20, tzinfo=timezone.utc).timestamp()
            if session_date
            else 123
        )
        assert result["stored_at"] == 123


def test_conflicting_explicit_dates_remain_unknown():
    result = resolve_occurrence_time(
        "Either 2024-03-01 or 2024-03-02.", observed_at=123, session_date="2024-03-20"
    )
    assert result["event_time_source"] == "unknown"
    assert result["reason"] == "ambiguous_multiple_explicit_dates"

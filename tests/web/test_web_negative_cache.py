"""Negative cache: time is injected, never slept."""
import pytest

from misaka.core.tools._web import negative_cache as nc


@pytest.fixture(autouse=True)
def _fresh_cache():
    nc.clear()
    yield
    nc.clear()


def test_403_bans_on_the_first_failure_and_lifts_after_an_hour():
    url = "https://blocked.example/a"
    assert nc.skip_reason(url, now=0.0) is None
    nc.record_failure(url, 403, now=0.0)

    reason = nc.skip_reason(url, now=10.0)
    assert reason is not None
    assert "403" in reason and url in reason

    assert nc.skip_reason(url, now=3599.0) is not None
    assert nc.skip_reason(url, now=3600.0) is None
    # The expired entry is forgotten, so the next 403 gets a full window again.
    nc.record_failure(url, 403, now=3600.0)
    assert nc.skip_reason(url, now=7199.0) is not None


def test_422_needs_two_consecutive_failures_before_it_bans():
    url = "https://paywall.example/b"
    nc.record_failure(url, 422, now=0.0)
    assert nc.skip_reason(url, now=1.0) is None

    nc.record_failure(url, 422, now=1.0)
    reason = nc.skip_reason(url, now=2.0)
    assert reason is not None and "422" in reason
    assert nc.skip_reason(url, now=1800.0) is not None
    assert nc.skip_reason(url, now=1801.0) is None


def test_429_ban_is_short():
    url = "https://busy.example/c"
    nc.record_failure(url, 429, now=100.0)
    assert nc.skip_reason(url, now=399.0) is not None
    assert nc.skip_reason(url, now=400.0) is None


@pytest.mark.parametrize("status", [200, 404, 418, 500, 503])
def test_untracked_statuses_are_never_cached(status):
    url = "https://flaky.example/d"
    nc.record_failure(url, status, now=0.0)
    assert nc.skip_reason(url, now=0.0) is None


def test_a_success_clears_a_pending_failure_count():
    url = "https://recovered.example/e"
    nc.record_failure(url, 422, now=0.0)
    nc.record_success(url)
    nc.record_failure(url, 422, now=1.0)
    assert nc.skip_reason(url, now=2.0) is None


def test_a_racing_failure_cannot_shorten_an_active_ban():
    """Two parallel fetches clear the gate together; the second verdict must not re-time."""
    url = "https://mixed.example/f"
    nc.record_failure(url, 403, now=0.0)
    nc.record_failure(url, 429, now=1.0)  # would otherwise cut 1h down to 5 min
    reason = nc.skip_reason(url, now=400.0)
    assert reason is not None and "403" in reason
    assert nc.skip_reason(url, now=3599.0) is not None


def test_a_racing_422_cannot_lift_an_active_ban():
    """A lone 422 does not ban on its own, so it must not clear someone else's ban."""
    url = "https://mixed.example/g"
    nc.record_failure(url, 429, now=0.0)
    nc.record_failure(url, 422, now=1.0)
    assert nc.skip_reason(url, now=2.0) is not None
    assert nc.skip_reason(url, now=300.0) is None


def test_a_different_status_replaces_a_pending_count():
    """A bare 422 count is not a ban, so a later 429 takes the entry over completely."""
    url = "https://mixed.example/h"
    nc.record_failure(url, 422, now=0.0)
    nc.record_failure(url, 429, now=1.0)
    reason = nc.skip_reason(url, now=2.0)
    assert reason is not None and "429" in reason and "422" not in reason
    # The 429 window is served and forgotten; the old 422 count did not survive it.
    assert nc.skip_reason(url, now=301.0) is None
    nc.record_failure(url, 422, now=302.0)
    assert nc.skip_reason(url, now=303.0) is None


def test_a_success_lifts_an_active_ban():
    url = "https://recovered.example/i"
    nc.record_failure(url, 403, now=0.0)
    assert nc.skip_reason(url, now=1.0) is not None
    nc.record_success(url)
    assert nc.skip_reason(url, now=2.0) is None


def test_a_ban_is_per_url_not_per_host():
    """One paywalled article must not take its whole domain down."""
    nc.record_failure("https://news.example/paywalled", 403, now=0.0)
    assert nc.skip_reason("https://news.example/free", now=1.0) is None


def test_the_default_clock_is_the_one_production_uses():
    """The only test that exercises the now=None path; no sleeping, no wall clock."""
    url = "https://blocked.example/j"
    assert nc.skip_reason(url) is None
    nc.record_failure(url, 403)
    assert nc.skip_reason(url) is not None


def test_eviction_sacrifices_dead_entries_before_live_bans(monkeypatch):
    monkeypatch.setattr(nc, "_MAX_ENTRIES", 4)
    nc.record_failure("https://live1.example/", 403, now=0.0)  # banned until 3600
    nc.record_failure("https://live2.example/", 403, now=0.0)
    nc.record_failure("https://pending.example/", 422, now=0.0)  # count only, no ban
    nc.record_failure("https://served.example/", 429, now=0.0)  # ban over by t=1000

    nc.record_failure("https://new.example/", 403, now=1000.0)

    assert (nc.policy_key(), "https://pending.example/") not in nc.current_scope().negative_bans
    assert (nc.policy_key(), "https://served.example/") not in nc.current_scope().negative_bans
    assert nc.skip_reason("https://live1.example/", now=1001.0) is not None
    assert nc.skip_reason("https://live2.example/", now=1001.0) is not None
    assert nc.skip_reason("https://new.example/", now=1001.0) is not None


def test_the_table_stays_bounded(monkeypatch):
    monkeypatch.setattr(nc, "_MAX_ENTRIES", 4)
    for i in range(50):
        nc.record_failure(f"https://host{i}.example/", 403, now=float(i))
    assert len(nc.current_scope().negative_bans) <= 4
    # Eviction drops the ban closest to expiry, so the newest failure survives.
    assert nc.skip_reason("https://host49.example/", now=50.0) is not None

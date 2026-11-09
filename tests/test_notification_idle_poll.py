"""A subscriber polling an outbox with nothing in it writes nothing.

0.19.17 on a Mac (2026-10-13): every open Last Order window polls its workspace's outbox each
``PENDING_POLL_SECONDS``, and each poll that found nothing rewrote the subscription's ``updated_at``
-- the board took a write every few seconds while nothing happened. ``updated_at`` is how ``prune``
tells a subscriber still there from one that left, so a poll still refreshes it, once a day.
"""
from contextlib import closing

from misaka.core.platform import notifications, tasks


def _wal_frames(con):
    """Frames written to the WAL since the last checkpoint that emptied it."""
    return con.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()[1]


def _poll(con, workspace="/w"):
    """One pass of the delivery loop (``SisterRuntime.deliver_pending``) over an empty outbox."""
    sid = notifications.subscribe(con, "last-order", f"board-harness:{workspace}", "task", "*", "terminal")
    with tasks.write_txn(con):
        return sid, notifications.claim_next(con, sid)


def test_an_idle_poll_writes_nothing_to_the_board(tmp_path):
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        sid, event = _poll(con)
        assert event is None
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert _wal_frames(con) == 0
        for _ in range(50):
            assert _poll(con) == (sid, None)
        assert _wal_frames(con) == 0


def test_a_poll_still_says_the_subscriber_is_there_once_a_day(tmp_path):
    import time

    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        sid, _ = _poll(con)
        stale = int(time.time()) - notifications.HEARTBEAT_SECONDS - 1
        con.execute("UPDATE notification_subscriptions SET updated_at=? WHERE id=?", (stale, sid))
        _poll(con)
        refreshed = con.execute("SELECT updated_at FROM notification_subscriptions WHERE id=?", (sid,)).fetchone()[0]
        assert refreshed > stale
        # Polled daily, a subscriber that has seen nothing for longer than the idle limit is kept.
        notifications.init(con)
        assert con.execute("SELECT 1 FROM notification_subscriptions WHERE id=?", (sid,)).fetchone()


def test_an_expired_lease_with_nothing_behind_it_is_cleared(tmp_path):
    import time

    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        sid, _ = _poll(con)
        con.execute("UPDATE notification_subscriptions SET lease_token='nl_old',leased_event_id=999,lease_expires=? "
                    "WHERE id=?", (int(time.time()) - 1, sid))
        _poll(con)
        row = con.execute("SELECT lease_token,leased_event_id,lease_expires FROM notification_subscriptions WHERE id=?",
                          (sid,)).fetchone()
        assert tuple(row) == (None, None, None)


def test_an_event_is_still_leased_and_acknowledged(tmp_path):
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        sid, _ = _poll(con)
        notifications.publish(con, "task", "t_1", "terminal", {"status": "done"})
        _sid, event = _poll(con)
        assert event is not None and event["resource_id"] == "t_1"
        assert notifications.ack(con, sid, event["id"], event["lease_token"])
        assert _poll(con) == (sid, None)

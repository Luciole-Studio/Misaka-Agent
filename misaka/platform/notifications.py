"""Durable resource notifications with one leased cursor per subscriber."""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from contextlib import contextmanager, nullcontext

SCHEMA = """
CREATE TABLE IF NOT EXISTS notification_events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  resource_type TEXT NOT NULL,
  resource_id   TEXT NOT NULL,
  kind          TEXT NOT NULL,
  payload       TEXT,
  dedupe_key    TEXT UNIQUE,
  created_at    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS notification_subscriptions (
  id              TEXT PRIMARY KEY,
  owner           TEXT NOT NULL,
  channel         TEXT NOT NULL,
  resource_type   TEXT NOT NULL,
  resource_id     TEXT NOT NULL,
  kind            TEXT NOT NULL,
  cursor          INTEGER NOT NULL DEFAULT 0,
  lease_token     TEXT,
  leased_event_id INTEGER,
  lease_expires   INTEGER,
  failure_count   INTEGER NOT NULL DEFAULT 0,
  last_error      TEXT,
  created_at      INTEGER NOT NULL,
  updated_at      INTEGER NOT NULL,
  UNIQUE(owner,channel,resource_type,resource_id,kind)
);
CREATE INDEX IF NOT EXISTS idx_notification_events_resource
  ON notification_events(resource_type,resource_id,kind,id);
"""

TASK_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS task_terminal_notification_v3
AFTER UPDATE OF status,generation ON tasks
WHEN NEW.status IN ('done','failed','stopped','blocked','triage')
 AND (OLD.status IS NOT NEW.status OR OLD.generation IS NOT NEW.generation)
BEGIN
  INSERT OR IGNORE INTO notification_events
    (resource_type,resource_id,kind,payload,dedupe_key,created_at)
  VALUES
    ('task',NEW.id,'terminal',
     json_object('status',NEW.status,'generation',NEW.generation),
     NULL,unixepoch());
END;
"""

@contextmanager
def _txn(con):
    serialized = getattr(con, "serialized", None)
    with serialized() if serialized else nullcontext():
        owner = not con.in_transaction
        if owner:
            con.execute("BEGIN IMMEDIATE")
        try:
            yield
            if owner:
                con.commit()
        except BaseException:
            if owner:
                con.rollback()
            raise


NOTIFICATION_SCHEMA_VERSION = 1


def init(con):
    """Tables and trigger every connect; the one-off backfill only until it is recorded.

    The trigger has to match the code that reads its events, so repairing it is an invariant
    worth re-asserting on every connection. The backfill is not: it is a correlated scan of
    the whole task table, and it used to run from every ``subscribe`` as well.
    """
    con.executescript(SCHEMA)
    task_columns = {
        row[1] for row in con.execute("PRAGMA table_info(tasks)").fetchall()
    }
    if {"id", "status", "generation", "completed_at", "created_at"} <= task_columns:
        con.executescript(TASK_TRIGGER)           # install the replacement before retiring old triggers
        for obsolete in ("task_terminal_notification", "task_terminal_notification_v2"):
            if con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (obsolete,)
            ).fetchone():
                con.execute(f'DROP TRIGGER "{obsolete}"')
        if con.execute(
            "SELECT 1 FROM schema_migrations WHERE component='notifications' AND version=?",
            (NOTIFICATION_SCHEMA_VERSION,),
        ).fetchone():
            return
        con.execute(
            "INSERT OR IGNORE INTO notification_events"
            "(resource_type,resource_id,kind,payload,dedupe_key,created_at) "
            "SELECT 'task',id,'terminal',json_object('status',status,'generation',generation),"
            "'task:'||id||':'||generation||':'||status||':backfill',COALESCE(completed_at,created_at) "
            "FROM tasks t WHERE status IN ('done','failed','stopped','blocked','triage') "
            "AND NOT EXISTS (SELECT 1 FROM notification_events n WHERE n.resource_type='task' "
            "AND n.resource_id=t.id AND n.kind='terminal' "
            "AND n.payload=json_object('status',t.status,'generation',t.generation))"
        )
    con.execute(
        "INSERT OR IGNORE INTO schema_migrations(component,version,applied_at) VALUES(?,?,?)",
        ("notifications", NOTIFICATION_SCHEMA_VERSION, int(time.time())),
    )


def publish(con, resource_type, resource_id, kind, payload=None, *, dedupe_key=None):
    if isinstance(payload, (dict, list)):
        payload = json.dumps(payload, ensure_ascii=False)
    cur = con.execute(
        "INSERT OR IGNORE INTO notification_events"
        "(resource_type,resource_id,kind,payload,dedupe_key,created_at) VALUES(?,?,?,?,?,?)",
        (resource_type, resource_id, kind, payload, dedupe_key, int(time.time())),
    )
    return int(cur.lastrowid) if cur.rowcount == 1 else None


def subscribe(con, owner, channel, resource_type="*", resource_id="*", kind="*", *,
              from_now=False):
    signature = f"{owner}\x00{channel}\x00{resource_type}\x00{resource_id}\x00{kind}"
    subscription_id = "ns_" + hashlib.sha256(signature.encode()).hexdigest()[:16]
    now = int(time.time())
    cursor = con.execute("SELECT COALESCE(MAX(id),0) FROM notification_events").fetchone()[0]
    con.execute(
        "INSERT OR IGNORE INTO notification_subscriptions"
        "(id,owner,channel,resource_type,resource_id,kind,cursor,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (subscription_id, owner, channel, resource_type, resource_id, kind,
         int(cursor) if from_now else 0, now, now),
    )
    return subscription_id


def claim_next(con, subscription_id, *, token=None, ttl_seconds=60):
    """Lease the next matching event without advancing the subscriber cursor."""
    token = token or "nl_" + secrets.token_hex(6)
    now = int(time.time())
    with _txn(con):
        sub = con.execute(
            "SELECT * FROM notification_subscriptions WHERE id=?",
            (subscription_id,),
        ).fetchone()
        if sub is None:
            return None
        if (sub["lease_token"] == token and sub["leased_event_id"] is not None
                and int(sub["lease_expires"] or 0) >= now):
            return con.execute(
                "SELECT *,? AS lease_token FROM notification_events WHERE id=?",
                (token, sub["leased_event_id"]),
            ).fetchone()
        if sub["lease_token"] and int(sub["lease_expires"] or 0) >= now:
            return None
        event = con.execute(
            "SELECT * FROM notification_events WHERE id>? "
            "AND (?='*' OR resource_type=?) AND (?='*' OR resource_id=?) "
            "AND (?='*' OR kind=?) ORDER BY id LIMIT 1",
            (sub["cursor"], sub["resource_type"], sub["resource_type"],
             sub["resource_id"], sub["resource_id"], sub["kind"], sub["kind"]),
        ).fetchone()
        if event is None:
            con.execute(
                "UPDATE notification_subscriptions SET lease_token=NULL,leased_event_id=NULL,"
                "lease_expires=NULL,updated_at=? WHERE id=?",
                (now, subscription_id),
            )
            return None
        cur = con.execute(
            "UPDATE notification_subscriptions SET lease_token=?,leased_event_id=?,"
            "lease_expires=?,updated_at=? WHERE id=? "
            "AND (lease_token IS NULL OR lease_expires IS NULL OR lease_expires<?)",
            (token, event["id"], now + max(1, int(ttl_seconds)), now,
             subscription_id, now),
        )
        if cur.rowcount != 1:
            return None
        return con.execute(
            "SELECT *,? AS lease_token FROM notification_events WHERE id=?",
            (token, event["id"]),
        ).fetchone()


def ack(con, subscription_id, event_id, token):
    now = int(time.time())
    cur = con.execute(
        "UPDATE notification_subscriptions SET cursor=MAX(cursor,?),lease_token=NULL,"
        "leased_event_id=NULL,lease_expires=NULL,failure_count=0,last_error=NULL,updated_at=? "
        "WHERE id=? AND lease_token=? AND leased_event_id=?",
        (int(event_id), now, subscription_id, token, int(event_id)),
    )
    return cur.rowcount == 1


def nack(con, subscription_id, event_id, token, error):
    now = int(time.time())
    cur = con.execute(
        "UPDATE notification_subscriptions SET lease_token=NULL,leased_event_id=NULL,"
        "lease_expires=NULL,failure_count=failure_count+1,last_error=?,updated_at=? "
        "WHERE id=? AND lease_token=? AND leased_event_id=?",
        (str(error)[:1000], now, subscription_id, token, int(event_id)),
    )
    return cur.rowcount == 1

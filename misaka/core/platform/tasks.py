"""Durable SQLite storage for task cards, runs, and research workflows."""

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager, nullcontext
from functools import wraps
from pathlib import Path

from misaka.utils.redact import redact, redact_payload

logger = logging.getLogger(__name__)

# connect() runs per process and sometimes more than once; the WAL fallback is a property of
# the filesystem, not of the connection, so it is worth saying exactly once.
_WARNED_ROLLBACK_JOURNAL = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
 id TEXT PRIMARY KEY,
 title TEXT NOT NULL,
 body TEXT,
 assignee TEXT NOT NULL,
 reviewer TEXT,
 executor TEXT, -- NULL = MISAKA card shell; JSON argv = external ally CLI
 model TEXT,
 status TEXT NOT NULL DEFAULT 'ready',
 priority INTEGER NOT NULL DEFAULT 0,
 workspace TEXT NOT NULL,
 output_dir TEXT,
 origin_session TEXT, -- id of the Last Order conversation that created the card
 agent_id TEXT,
 session_file TEXT,
 session_dir TEXT, -- stable storage address; survives runtime resets and workspace changes
 claim_lock TEXT,
 claim_expires INTEGER,
 worker_pid INTEGER,
 worker_identity TEXT,
 current_run_id TEXT,
 generation INTEGER NOT NULL DEFAULT 1,
 notified_generation INTEGER NOT NULL DEFAULT 0, -- legacy cache; notification_events/subscriptions own delivery
 review_rounds INTEGER NOT NULL DEFAULT 0,
 review_feedback TEXT,
 review_lock TEXT,
 review_expires INTEGER,
 review_pid INTEGER,
 review_identity TEXT,
 block_kind TEXT,
 block_reason TEXT,
 block_fingerprint TEXT,
 block_recurrence INTEGER NOT NULL DEFAULT 0,
 blocked_at INTEGER,
 heartbeat_at INTEGER,
 next_attempt_at INTEGER,
 consecutive_failures INTEGER NOT NULL DEFAULT 0, -- failed attempts in a row; a settled or resumed card starts over
 last_failure_error TEXT, -- why the last attempt failed, shown to the next one
 created_at INTEGER NOT NULL,
 started_at INTEGER,
 completed_at INTEGER
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 task_id TEXT NOT NULL,
 kind TEXT NOT NULL,
 payload TEXT,
 generation INTEGER,
 run_id TEXT,
 created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS task_runs (
 id TEXT PRIMARY KEY,
 task_id TEXT NOT NULL,
 generation INTEGER NOT NULL,
 attempt INTEGER NOT NULL,
 phase TEXT NOT NULL,
 assignee TEXT,
 status TEXT NOT NULL,
 claim_lock TEXT,
 pid INTEGER,
 process_identity TEXT,
 workspace TEXT,
 session_file TEXT,
 started_at INTEGER NOT NULL,
 heartbeat_at INTEGER,
 completed_at INTEGER,
 exit_code INTEGER,
 failure_kind TEXT,
 failure_fingerprint TEXT,
 summary TEXT,
 usage_tokens INTEGER NOT NULL DEFAULT 0,
 UNIQUE(task_id, generation, attempt)
);
CREATE TABLE IF NOT EXISTS schema_migrations (
 component TEXT NOT NULL,
 version INTEGER NOT NULL,
 applied_at INTEGER NOT NULL,
 PRIMARY KEY(component, version)
);
CREATE TABLE IF NOT EXISTS scheduler_state (
 lane TEXT PRIMARY KEY,
 cursor TEXT,
 updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS budget_reservations (
 id TEXT PRIMARY KEY,
 task_id TEXT NOT NULL,
 generation INTEGER NOT NULL,
 tokens INTEGER NOT NULL,
 expires_at INTEGER NOT NULL,
 created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS todos (
 id TEXT PRIMARY KEY, -- td_xxxxxx
 task_id TEXT NOT NULL, -- Owning task card
 parent_id TEXT, -- Optional parent to-do
 text TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'open', -- open | doing | done | blocked
 owner TEXT, -- Sister ID, subagent name, or free-text owner
 note TEXT, -- Required reason when blocked
 generation INTEGER NOT NULL, -- Task generation audit stamp
 created_at INTEGER NOT NULL,
 updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_runs_task ON task_runs(task_id, generation, attempt);
CREATE INDEX IF NOT EXISTS idx_task_runs_status ON task_runs(status);
-- latest_payload asks for one card's newest event of a kind; without this it walks the whole
-- table backwards on every miss, and settle_done_tasks probes exactly that miss once per
-- unsettled done card. Every connect runs this script, so an older board gains the index on
-- its next open -- IF NOT EXISTS is the migration.
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, kind, id);
"""
TASK_SCHEMA_VERSION = 8
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
# One failure model for every way an attempt can end badly -- a crash, a timeout, a provider
# error, a turn that ended without settling the card. Each counts against the same limit and the
# card returns to ready with the error kept for the next attempt; at the limit it stays failed.
# A rate limit is the one exception: it only defers the card and counts nothing.
FAILURE_LIMIT = 3
# A retry cannot help these: no credentials or credit, or a board that names an assignee the
# installation does not have.
TERMINAL_FAILURE_KINDS = frozenset({"auth_or_quota", "configuration"})
RETRY_BASE_SECONDS = 30
RETRY_CAP_SECONDS = 900
# A running card whose worker is alive but has shown no progress for this long is wedged.
# Progress is what the worker's own session stamps (``heartbeat`` with ``progress=True``);
# a supervisor renewing the lease proves only that a process exists.
HEARTBEAT_STALE_SECONDS = 3600
UNSETTLED_REASON = ("the session ended without settling the card: no summary was produced, "
                    "so nothing was submitted")
ABANDONED_REASON = "worker exited before the card lifecycle settled"


class _SerializedCursor(sqlite3.Cursor):
    """Serialize every operation that can advance a SQLite statement."""

    @property
    def _lock(self):
        return self.connection._misaka_lock

    def execute(self, sql, parameters=()):
        with self._lock:
            return super().execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters):
        with self._lock:
            return super().executemany(sql, seq_of_parameters)

    def executescript(self, sql_script):
        with self._lock:
            return super().executescript(sql_script)

    def fetchone(self):
        with self._lock:
            return super().fetchone()

    def fetchmany(self, size=None):
        with self._lock:
            return super().fetchmany() if size is None else super().fetchmany(size)

    def fetchall(self):
        with self._lock:
            return super().fetchall()

    def __next__(self):
        with self._lock:
            return super().__next__()

    def close(self):
        with self._lock:
            return super().close()


class SerializedConnection(sqlite3.Connection):
    """One SQLite connection shared by the Last Order loop and its worker callback.

    ``check_same_thread=False`` only removes Python's ownership check; callers
    must still serialize writes themselves.  The re-entrant lock also lets one
    card operation call another (``create_task`` emits an event) without
    deadlocking.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._misaka_lock = threading.RLock()

    @contextmanager
    def serialized(self):
        with self._misaka_lock:
            yield self

    def cursor(self, factory=_SerializedCursor):
        with self._misaka_lock:
            return super().cursor(factory)

    def execute(self, sql, parameters=()):
        with self._misaka_lock:
            return self.cursor().execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters):
        with self._misaka_lock:
            return self.cursor().executemany(sql, seq_of_parameters)

    def executescript(self, sql_script):
        with self._misaka_lock:
            return self.cursor().executescript(sql_script)

    def commit(self):
        with self._misaka_lock:
            return super().commit()

    def rollback(self):
        with self._misaka_lock:
            return super().rollback()

    def close(self):
        with self._misaka_lock:
            return super().close()


def _serialized(operation):
    """Run a multi-statement card operation under the connection lock."""

    @wraps(operation)
    def call(con, *args, **kwargs):
        serialized = getattr(con, "serialized", None)
        if serialized is None:  # Preserve compatibility with caller-owned connections.
            return operation(con, *args, **kwargs)
        with serialized():
            return operation(con, *args, **kwargs)
    return call


def _columns(con, table):
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate(con):
    """Additively migrate databases created by older MISAKA builds."""
    task_columns = {
        "reviewer": "TEXT",
        "current_run_id": "TEXT",
        "block_kind": "TEXT",
        "block_reason": "TEXT",
        "block_fingerprint": "TEXT",
        "block_recurrence": "INTEGER NOT NULL DEFAULT 0",
        "blocked_at": "INTEGER",
        "heartbeat_at": "INTEGER",
        "next_attempt_at": "INTEGER",
        "review_rounds": "INTEGER NOT NULL DEFAULT 0",
        "review_feedback": "TEXT",
        "review_lock": "TEXT",
        "review_expires": "INTEGER",
        "review_pid": "INTEGER",
        "review_identity": "TEXT",
        "output_dir": "TEXT",
        "origin_session": "TEXT",
        "session_dir": "TEXT",
        "consecutive_failures": "INTEGER NOT NULL DEFAULT 0",
        "last_failure_error": "TEXT",
    }
    existing = _columns(con, "tasks")
    for name, definition in task_columns.items():
        if name not in existing:
            con.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
    if "session_dir" not in existing:
        # Preserve physical storage in place exactly once. New cards allocate by immutable
        # card id; runtime code never searches old layouts or derives storage from a worktree.
        from misaka.config import sessions
        for row in con.execute("SELECT id,session_file FROM tasks"):
            directory = (os.path.dirname(row["session_file"]) if row["session_file"] else
                         sessions.card_session_dir(row))
            con.execute("UPDATE tasks SET session_dir=? WHERE id=?", (directory, row["id"]))
    # Phases 2-3 moved comments, attachments and links into the card file / repo. Nothing copies
    # the old rows over, so a table that still holds any is renamed, not dropped: the data stays
    # reachable until someone migrates it by hand.
    stamp = time.strftime("%Y%m%d%H%M%S")
    for retired in ("task_comments", "task_attachments", "task_links"):
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (retired,)).fetchone():
            continue
        if con.execute(f"SELECT COUNT(*) FROM {retired}").fetchone()[0] == 0:
            con.execute(f"DROP TABLE {retired}")
        else:
            con.execute(f"ALTER TABLE {retired} RENAME TO {retired}_bak_{stamp}")
    # The verifier and its two statuses are gone; a card left there needs a human, not silence.
    con.execute(
        "UPDATE tasks SET status='triage', block_kind='needs_input', "
        "block_reason='left in a retired verifier state (verifying/finalizing); review the card and unblock it', "
        "blocked_at=?, claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, worker_identity=NULL "
        "WHERE status IN ('verifying','finalizing')",
        (int(time.time()),),
    )
    if "run_id" not in _columns(con, "events"):
        con.execute("ALTER TABLE events ADD COLUMN run_id TEXT")
    con.execute("CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id)")
    con.execute(
        "INSERT OR IGNORE INTO schema_migrations(component,version,applied_at) VALUES(?,?,?)",
        ("tasks", TASK_SCHEMA_VERSION, int(time.time())),
    )


def connect(path: str) -> sqlite3.Connection:
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(
        path,
        timeout=5,
        isolation_level=None,  # autocommit keeps each CAS update atomic
        check_same_thread=False,
        factory=SerializedConnection,
    )
    con.row_factory = sqlite3.Row
    # journal_mode=WAL is a request, not a promise: a filesystem without the shared-memory and
    # locking primitives WAL needs (NFS, SMB, some container overlays) leaves SQLite in
    # rollback-journal mode, and the pragma answers with the mode it kept. Read that row back --
    # what synchronous may safely be depends on which mode actually took.
    row = con.execute("PRAGMA journal_mode=WAL").fetchone()
    journal = str(row[0]).lower() if row else ""
    if journal == "wal":
        # WAL's canonical pairing. FULL fsyncs the WAL on every autocommit statement, and this
        # board is written a statement at a time (one UPDATE per reconciled card, one row per
        # event). Under WAL, NORMAL cannot corrupt the database; it risks only the last commits
        # before a power cut, and every durable decision here is a lease or a CAS that re-runs
        # when it is not observed: an un-fsynced claim is simply claimed again. The card file,
        # not this index, is the contract.
        con.execute("PRAGMA synchronous=NORMAL")
    else:
        # That whole tolerance argument was made *for* WAL. In rollback-journal mode NORMAL
        # risks the database file itself, not just the newest commits, and nothing in the
        # lease/CAS design compensates for a board that will not open. Keep SQLite's FULL.
        global _WARNED_ROLLBACK_JOURNAL
        if not _WARNED_ROLLBACK_JOURNAL:
            # Said once per process, through logging rather than a flag: nothing in this build
            # reports on the board's storage, so a flag would have no reader but its own test,
            # while a warning reaches whoever is running MISAKA on that filesystem. Silence is
            # the one option ruled out -- the durability the design assumes is not there.
            _WARNED_ROLLBACK_JOURNAL = True
            logger.warning(
                "Task board %s could not use WAL (journal_mode=%s): this filesystem does not "
                "support it. Keeping synchronous=FULL, which is slower but is the only safe "
                "setting for a rollback journal. A local disk is the supported home for it.",
                path, journal or "unknown",
            )
    con.executescript(SCHEMA)
    from misaka.core.platform import notifications
    newest = con.execute("SELECT MAX(version) FROM schema_migrations WHERE component='tasks'").fetchone()[0]
    if newest is not None and int(newest) > TASK_SCHEMA_VERSION:
        con.close()
        raise RuntimeError(f"This board was written by a newer MISAKA (task schema v{newest}; this build knows "
                           f"v{TASK_SCHEMA_VERSION}). Upgrade MISAKA rather than downgrading the data.")
    if newest != TASK_SCHEMA_VERSION:
        # Once per upgrade, not on every connection -- and once per *board*, not per process.
        # _migrate reads the column set and then ALTERs; two processes that both connect to the
        # same stale board right after an upgrade each read the pre-migration metadata, and the
        # loser's ALTER raises "duplicate column name" (or its DROP raises "no such table")
        # straight out of connect(), killing that process at startup. busy_timeout does not help:
        # it is stale metadata, not a lock conflict. BEGIN IMMEDIATE makes the loser queue, and
        # re-reading the version inside the transaction makes it skip work the winner committed.
        with _write_txn(con):
            newest = con.execute(
                "SELECT MAX(version) FROM schema_migrations WHERE component='tasks'"
            ).fetchone()[0]
            if newest != TASK_SCHEMA_VERSION:
                _migrate(con)
    notifications.init(con)
    return con


def canonical_workspace(path=None):
    return str(Path(path or os.getcwd()).expanduser().resolve())


def task_state_dir(task_id):
    from misaka.config import CFG
    return str(Path(CFG.get("tasks_root", "~/.misaka/tasks")).expanduser() / str(task_id))


def workspace_for(task):
    return canonical_workspace(task["workspace"])


def _card_dispatchable(con, task_id):
    """One card's file-side gate, checked per claim; the project-wide pass is per tick."""
    row = con.execute("SELECT workspace FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return False
    from misaka.core.platform import cards
    return cards.dispatchable(con, row["workspace"], task_id)


@_serialized
def create_task(con, title, body="", assignee="", model=None, priority=0,
                executor=None, reviewer=None, workspace=None, output_dir=None,
                origin_session=None):
    """Insert a card, record its ``created`` event, and return the new id.

    ``workspace`` is the project folder the card belongs to; there is no default.
    ``origin_session`` is the Last Order conversation that created it, so that
    conversation can bring the right Sisters back when it is resumed.
    """
    reviewer = str(reviewer or "").strip() or None
    if reviewer == assignee:
        raise ValueError("The reviewer must be different from the assignee.")
    if not workspace:
        raise ValueError("A task card needs a workspace folder.")
    workspace = canonical_workspace(workspace)
    output_dir = canonical_workspace(output_dir) if output_dir else None
    # Three bytes of id keep card names short enough to say aloud; a board of a few thousand
    # cards will see the odd collision, which costs a fresh draw, never a failed create.
    for draw in range(3):
        tid = "t_" + secrets.token_hex(3)
        try:
            con.execute(
                "INSERT INTO tasks (id,title,body,assignee,reviewer,executor,model,priority,"
                "workspace,output_dir,origin_session,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, title, body, assignee, reviewer, json.dumps(executor) if executor else None,
                 model, priority, workspace, output_dir, origin_session or None,
                 int(time.time())),
            )
        except sqlite3.IntegrityError:
            if draw == 2:
                raise
            continue
        break
    add_event(con, tid, "created", {"title": title, "assignee": assignee,
                                    "reviewer": reviewer,
                                    "workspace": workspace, "executor": executor})
    return tid


@_serialized
def add_event(
    con,
    task_id,
    kind,
    payload=None,
    generation=None,
    claim_lock=None,
    run_id=None,
):
    """Append an event, optionally fenced by generation and running owner.

    The conditional form is one SQLite statement, so a late supervisor cannot
    append generation-N output after another Last Order has resumed N+1.  An
    owner-fenced event additionally requires the still-live running lease, so
    an expired worker cannot publish output after an orphan takeover in the
    same generation.

    The ledger is kept for good, so whatever a tool echoed into a summary, a note or a
    failure message is masked here, at the one door every event goes through.
    """
    if isinstance(payload, (dict, list)):
        payload = json.dumps(redact_payload(payload), ensure_ascii=False)
    elif isinstance(payload, str):
        payload = redact(payload)
    now = int(time.time())
    if run_id is None:
        row = con.execute("SELECT current_run_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        run_id = row[0] if row else None
    if claim_lock is not None:
        if generation is None:
            raise ValueError("an owner-fenced event requires a generation")
        cur = con.execute(
            "INSERT INTO events (task_id, kind, payload, generation, run_id, created_at) "
            "SELECT ?,?,?,?,?,? WHERE EXISTS "
            "(SELECT 1 FROM tasks WHERE id=? AND generation=? "
            "AND status='running' AND claim_lock=? AND claim_expires>=?)",
            (
                task_id,
                kind,
                payload,
                generation,
                run_id,
                now,
                task_id,
                generation,
                claim_lock,
                now,
            ),
        )
    elif generation is None:
        cur = con.execute(
            "INSERT INTO events (task_id, kind, payload, generation, run_id, created_at) "
            "SELECT ?,?,?,generation,?,? FROM tasks WHERE id=?",
            (task_id, kind, payload, run_id, now, task_id),
        )
    else:
        cur = con.execute(
            "INSERT INTO events (task_id, kind, payload, generation, run_id, created_at) "
            "SELECT ?,?,?,?,?,? WHERE EXISTS "
            "(SELECT 1 FROM tasks WHERE id=? AND generation=?)",
            (task_id, kind, payload, generation, run_id, now, task_id, generation),
        )
    return cur.rowcount == 1


@contextmanager
def _write_txn(con):
    """Commit task-row and task-run changes as one crash-safe unit.

    The connection lock covers the *whole* transaction, not just individual SQL
    statements.  Otherwise another thread can see ``in_transaction`` and
    accidentally join a transaction that later rolls its successful work back.
    The lock is re-entrant, so nested use in the owning thread still joins the
    outer transaction.
    """
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


write_txn = _write_txn     # callers outside this module that must make several card writes one unit


def _start_run(
    con, task_id, generation, phase, claim_lock, pid=None, identity=None, assignee=None
):
    row = con.execute("SELECT assignee,workspace,session_file FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise ValueError(f"Card not found: {task_id}")
    attempt = int(con.execute(
        "SELECT COALESCE(MAX(attempt),0)+1 FROM task_runs WHERE task_id=? AND generation=?",
        (task_id, generation),
    ).fetchone()[0])
    run_id = "tr_" + secrets.token_hex(5)
    now = int(time.time())
    con.execute(
        "INSERT INTO task_runs "
        "(id,task_id,generation,attempt,phase,assignee,status,claim_lock,pid,"
        "process_identity,workspace,session_file,started_at,heartbeat_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, task_id, int(generation), attempt, phase, assignee or row["assignee"], "running",
         claim_lock, pid, identity, row["workspace"], row["session_file"], now, now),
    )
    con.execute("UPDATE tasks SET current_run_id=?,heartbeat_at=? WHERE id=?",
                (run_id, now, task_id))
    return run_id


def _clear_failures(con, task_id):
    """A settled, unblocked or resumed card starts its failure budget over."""
    con.execute(
        "UPDATE tasks SET consecutive_failures=0,last_failure_error=NULL WHERE id=?", (task_id,)
    )


def _finish_current_run(
    con,
    task_id,
    status,
    *,
    exit_code=None,
    failure_kind=None,
    failure_fingerprint=None,
    summary=None,
):
    now = int(time.time())
    con.execute(
        "UPDATE task_runs SET status=?,completed_at=?,exit_code=COALESCE(?,exit_code),"
        "failure_kind=COALESCE(?,failure_kind),"
        "failure_fingerprint=COALESCE(?,failure_fingerprint),summary=COALESCE(?,summary) "
        "WHERE id=(SELECT current_run_id FROM tasks WHERE id=?) AND status='running'",
        (status, now, exit_code, failure_kind, failure_fingerprint, redact(summary), task_id),
    )


_LAST_REFUSAL: dict[str, str] = {}


def _note_refusal(task_id, reason):
    _LAST_REFUSAL[str(task_id)] = reason


def claim_refusal(task_id):
    """Why the last ``claim`` of this card said no, when the reason is worth telling.

    ``claim`` answers a bare False for every reason -- taken by another dispatcher, wrong
    generation, a backoff, a full host, a Sister at her cap -- and its callers reported all
    of them as "claimed by another dispatcher". The admission limits and a retry backoff are
    the ones a person can do something about, so they are kept here for the message.
    Cleared on read.
    """
    return _LAST_REFUSAL.pop(str(task_id), None)


@_serialized
def claim(
    con,
    task_id,
    lock,
    ttl_seconds=1800,
    generation=None,
    pid=None,
    worker_identity=None,
    host_cap=None,
    assignee_cap=None,
) -> bool:
    now = int(time.time())
    if not _card_dispatchable(con, task_id):
        return False
    with _write_txn(con):
        row = con.execute(
            "SELECT generation,assignee,next_attempt_at FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if row is None or (generation is not None and int(row["generation"]) != int(generation)):
            return False
        if row["next_attempt_at"] is not None and int(row["next_attempt_at"]) > now:
            _note_refusal(task_id, f"Card {task_id} is waiting out a retry backoff for another "
                                   f"{int(row['next_attempt_at']) - now} s; it will be ready then")
            return False
        if host_cap is not None or assignee_cap is not None:
            from misaka.core.platform import admission
            occupied = admission.occupied(con)
        if host_cap is not None:
            running = len(occupied)
            if running >= max(0, int(host_cap)):
                _note_refusal(task_id, f"{running} cards are running on this host (limit {int(host_cap)}); waiting for a slot")
                return False
        if assignee_cap is not None:
            hers = sum(assignee == row["assignee"] for assignee in occupied.values())
            if hers >= max(0, int(assignee_cap)):
                _note_refusal(task_id, f"Sister {row['assignee']} is already running {hers} cards (limit {int(assignee_cap)}); waiting for a slot")
                return False
        actual_generation = int(row["generation"])
        cur = con.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, started_at=?, "
            "worker_pid=?, worker_identity=?, next_attempt_at=NULL "
            "WHERE id=? AND status='ready' AND claim_lock IS NULL AND generation=?",
            (lock, now + ttl_seconds, now, pid, worker_identity, task_id, actual_generation),
        )
        if cur.rowcount != 1:
            return False
        _start_run(con, task_id, actual_generation, "worker", lock, pid, worker_identity)
    return True


@_serialized
def get(con, task_id):
    return con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()


@_serialized
def insert_index_row(con, fields, body, *, workspace):
    """Restore one index row from a card file (misaka.core.platform.cards.rebuild). The file is
    the truth; this only re-derives the index and never overwrites an existing row."""
    cur = con.execute(
        "INSERT OR IGNORE INTO tasks (id,title,body,assignee,reviewer,executor,model,"
        "priority,workspace,origin_session,status,generation,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (str(fields.get("id")), str(fields.get("title") or ""), body,
         str(fields.get("assignee") or ""), fields.get("reviewer"),
         json.dumps(fields["executor"]) if fields.get("executor") else None,
         fields.get("model"), int(fields.get("priority") or 0),
         canonical_workspace(workspace),
         fields.get("origin_session"), str(fields.get("status") or "ready"),
         max(1, int(fields.get("generation") or 1)),
         int(time.time())),
    )
    return cur.rowcount == 1


@_serialized
def delete_task(con, task_id, *, allow_active=False):
    """Delete a card and everything attached to it; active cards are refused unless ``allow_active``."""
    row = con.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return False, f"Card not found: {task_id}"
    if not allow_active and row["status"] in ("running", "review"):
        return False, f"Card {task_id} is {row['status']}; stop it before deletion."
    con.execute("DELETE FROM budget_reservations WHERE task_id=?", (task_id,))
    con.execute("DELETE FROM events WHERE task_id=?", (task_id,))
    con.execute("DELETE FROM todos WHERE task_id=?", (task_id,))
    con.execute("DELETE FROM task_runs WHERE task_id=?", (task_id,))
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_run_tasks'").fetchone():
        con.execute("DELETE FROM research_run_tasks WHERE task_id=?", (task_id,))   # a link without its card is a trap for resume
    from misaka.core.platform import cards
    skipped = []
    for child in _children_of(con, task_id):
        # One child's accident is that child's alone. The handler lives inside the iteration on
        # purpose: hoisted around the loop, a single casualty (a card file deleted concurrently
        # between _children_of's walk and this rewrite) aborted the whole loop, and every
        # surviving sibling kept a needs entry pointing at the card being deleted -- held at
        # todo forever, waiting on a parent that no longer exists.
        try:
            remaining = [p for p in parent_ids(con, child, strict=True) if p != task_id]
        except UnreadableCard as error:
            skipped.append((child, error.reason))   # an empty read here would rewrite needs=None,
            continue                                # dropping the child's *other* dependencies;
                                                    # leave the file for its owner
        child_row = get(con, child)
        if child_row is None or not child_row["workspace"]:
            skipped.append((child, "no index row"))     # a card file with no row to name its project
            continue
        try:
            cards.set_fields(child_row["workspace"], child, needs=remaining or None)
            promote_task(con, child)        # mirrors the child's file too, so it races the same way
        except OSError as error:
            skipped.append((child, str(error)))
    for child, reason in skipped:
        # On the *child*, which survives this deletion -- the parent's own events go with it.
        add_event(con, child, "dependency_rewrite_skipped",
                  {"parent_id": task_id, "reason": reason})
    con.execute(
        "DELETE FROM notification_events WHERE resource_type='task' AND resource_id=?",
        (task_id,),
    )
    con.execute(
        "DELETE FROM notification_subscriptions WHERE resource_type='task' AND resource_id=?",
        (task_id,),
    )
    con.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    shutil.rmtree(task_state_dir(task_id), ignore_errors=True)
    # Nothing here touches the card *file*; only cards.remove does, and only it knows whether
    # git was ever given a copy. Claiming anything about the file from in here was a lie for
    # every card `cards.create` makes, because nothing commits cards/ on the way in.
    message = (f"Card {task_id} and its runs, dependencies, events, budget, and to-do items "
               "were deleted.")
    if skipped:
        message += (" These cards still name it in needs and could not be rewritten: "
                    + "; ".join(f"{child} ({reason})" for child, reason in skipped) + ".")
    return True, message


@_serialized
def by_status(con, status, *, workspace=None):
    sql = "SELECT * FROM tasks WHERE status=?"
    params = [status]
    if workspace is not None:
        sql += " AND workspace=?"
        params.append(canonical_workspace(workspace))
    return con.execute(sql + " ORDER BY priority DESC, created_at", params).fetchall()


@_serialized
def fair_ready(con, *, limit=None, lane="workers", advance=True, now=None, workspace=None):
    """Return runnable cards round-robin across assignees, keeping priority order within each assignee."""
    now = int(time.time()) if now is None else int(now)
    workspace = canonical_workspace(workspace) if workspace is not None else None
    from misaka.core.platform import cards
    workspaces = ([workspace] if workspace is not None else
                  [row[0] for row in con.execute("SELECT DISTINCT workspace FROM tasks")])
    valid = set().union(*(cards.reconcile(con, item) for item in workspaces if item))
    where = ""
    params = [now]
    if workspace is not None:
        where = "AND workspace=? "
        params.append(workspace)
    rows = [row for row in con.execute(
        "SELECT * FROM tasks WHERE status='ready' "
        "AND (next_attempt_at IS NULL OR next_attempt_at<=?) "
        + where +
        "ORDER BY assignee,priority DESC,created_at,id",
        params,
    ).fetchall() if row["id"] in valid]
    queues = {}
    for row in rows:
        queues.setdefault(row["assignee"], []).append(row)
    assignees = sorted(queues)
    state_lane = f"{lane}:{workspace}" if workspace is not None else lane
    state = con.execute("SELECT cursor FROM scheduler_state WHERE lane=?", (state_lane,)).fetchone()
    if state and state["cursor"] in assignees:
        pivot = assignees.index(state["cursor"]) + 1
        assignees = assignees[pivot:] + assignees[:pivot]
    selected = []
    while assignees and (limit is None or len(selected) < max(0, int(limit))):
        remaining = []
        for assignee in assignees:
            if limit is not None and len(selected) >= max(0, int(limit)):
                break
            selected.append(queues[assignee].pop(0))
            if queues[assignee]:
                remaining.append(assignee)
        assignees = remaining
    if advance and selected:
        con.execute(
            "INSERT INTO scheduler_state(lane,cursor,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(lane) DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at",
            (state_lane, selected[-1]["assignee"], now),
        )
    return selected


@_serialized
def link_tasks(con, parent_id, child_id) -> bool:
    return link_dependencies(con, [parent_id], child_id)


@_serialized
def link_dependencies(con, parents, child_id) -> bool:
    """Validate every new edge before publishing the child's complete needs list."""
    with _write_txn(con):
        parents = list(dict.fromkeys(parents))
        if not parents:
            return False
        needs = parent_ids(con, child_id, strict=True)
        additions = [pid for pid in parents if pid not in needs]
        if not additions:
            return False
        for parent_id in additions:
            _validate_dependency(con, parent_id, child_id)
        from misaka.core.platform import cards
        child = get(con, child_id)
        cards.set_fields(child["workspace"], child_id, needs=[*needs, *additions])
        con.execute("UPDATE tasks SET status='todo' WHERE id=? AND status='ready'", (child_id,))
        for parent_id in additions:
            add_event(con, child_id, "dependency_linked", {"parent_id": parent_id})
        promote_task(con, child_id)
        _mirror_status(con, child_id)
    return True


def _validate_dependency(con, parent_id, child_id):
    """Check one proposed edge without changing the card's published contract."""
    if parent_id == child_id:
        raise ValueError("A task cannot depend on itself.")
    parent, child = get(con, parent_id), get(con, child_id)
    if parent is None or child is None:
        raise ValueError("Both ends of a dependency must be existing tasks.")
    if parent["workspace"] != child["workspace"]:
        raise ValueError("A dependency must stay inside one project.")
    # Idempotency comes first, before every check that can refuse: an edge the child's file already
    # carries is not a change to a moving card, it is that card's own history being re-posted. A
    # resume replays a whole plan over cards that have since started, and the replay must be a
    # no-op rather than the reason the node fails again. Only a *new* edge is refused below.
    needs = parent_ids(con, child_id, strict=True)
    if parent_id in needs:
        return False
    if child["status"] in {"running", "review", "done"}:
        raise ValueError(f"Child task is {child['status']}; its dependencies cannot be changed.")
    # Cycle iff the child is already upstream of the parent. This walk is the only thing standing
    # between a typo and a cycle written into the card files, and a cycle is not repairable by
    # fixing the file that hid it: reconcile's fixed point then holds the whole chain at todo
    # forever, with nothing on the board saying why. So it reads strictly -- an ancestor whose
    # file will not parse means we do not know the graph, and an edge we cannot prove acyclic is
    # refused. Refusing one new edge costs nobody their running work; the cycle costs the chain.
    seen, frontier = set(), [parent_id]
    while frontier:
        current = frontier.pop()
        if current == child_id:
            raise ValueError("Task dependency creates a cycle.")
        if current not in seen:
            seen.add(current)
            try:
                frontier.extend(parent_ids(con, current, strict=True))
            except UnreadableCard as error:
                raise ValueError(
                    f"Dependency refused: {error} Its dependencies could decide whether this "
                    "link closes a cycle, so fix that card first."
                ) from error


def _mirror_status(con, task_id, *, commit=False):
    """Write the live transition's at-rest fields to its authoritative card.

    File-write failure aborts the SQLite transition. Git is only history: commit
    failure leaves the file dirty and records a retry hint instead of fabricating
    a rollback across stores. ``running`` remains lease-only and is never mirrored.
    """
    row = get(con, task_id)
    if row is None or not row["workspace"]:
        return
    from misaka.core.platform import cards, repo
    try:
        cards.set_fields(
            row["workspace"], task_id, status=row["status"], generation=int(row["generation"])
        )
    except FileNotFoundError:
        # The card file is not merely unwritable, it is gone -- the project directory was
        # deleted or moved. There is no at-rest truth left to protect, so aborting here only
        # means the lease outlives its worker: `reclaim_abandoned` mirrors, so a reconciler
        # could never release such a card and every card behind it in the loop kept its
        # expired lease too. `board()` already models this row as `missing_file`; let the
        # transition commit, and record that the mirror did not land.
        #
        # Every other write failure still aborts the transition, which is the rule this
        # function exists to enforce: a file that exists and could not be written means the
        # database must not claim a state the file does not show.
        add_event(con, task_id, "card_file_missing",
                  {"status": row["status"], "workspace": row["workspace"]},
                  generation=row["generation"])
        return
    if commit and repo.enabled(row["workspace"]) and not repo.commit(
            row["workspace"], [os.path.join("cards", f"{task_id}.md")], f"card {task_id}: {row['status']}"):
        add_event(
            con, task_id, "git_commit_pending", {"status": row["status"]},
            generation=row["generation"],
        )


class UnreadableCard(ValueError):
    """A card file had to be read to answer this, and could not be parsed."""

    def __init__(self, task_id, path, reason):
        super().__init__(f"Card {task_id}'s file cannot be read ({reason}): {path}")
        self.task_id, self.path, self.reason = task_id, path, reason


@_serialized
def parent_ids(con, task_id, *, strict=False):
    """The card's dependencies, from its file's frontmatter ``needs`` (the table is gone).

    An unreadable card answers ``[]`` for the walks that visit the whole project on one card's
    behalf: one hand-broken file may not stop every other card (the failure is recorded in
    ``cards.invalid_cards()``). ``strict=True`` raises :class:`UnreadableCard` instead, for the
    caller that cannot tell "declares no dependencies" from "we could not find out" -- reading
    the second as the first is how a cycle gets written into the files.

    A card with no file at all is not that case, and is ``[]`` even under ``strict``. Such a row
    is what ``cards.board`` shows as ``missing_file``: it has no frontmatter, so it declares no
    dependencies and can hide no cycle, and refusing over it would refuse forever -- there is no
    file to go and fix. Only bytes we hold and cannot parse make us say we do not know.
    """
    row = get(con, task_id)
    if row is None or not row["workspace"]:
        return []
    from misaka.core.platform import cards
    path = cards.card_path(row["workspace"], task_id)
    card = cards.try_read(path)
    if card is None:                        # unreadable: recorded in cards.invalid_cards()
        reason = cards.unreadable_reason(path)      # None when there simply is no file
        if strict and reason is not None:
            raise UnreadableCard(task_id, path, reason)
        return []
    return [str(x) for x in card["fields"].get("needs") or []]


def _children_of(con, parent_id):
    """Every card in the parent's project whose file names it in ``needs``."""
    row = get(con, parent_id)
    if row is None or not row["workspace"]:
        return []
    from misaka.core.platform import cards
    out = []
    for tid, path in cards.iter_cards(row["workspace"]):
        card = cards.try_read(path)
        if card is None:                    # one broken file is one card's problem, not the
            continue                        # whole project's: recorded in cards.invalid_cards()
        needs = card["fields"].get("needs") or []
        if parent_id in [str(x) for x in needs]:
            out.append(tid)
    return out


def dependency_state(con, task_id):
    parents = parent_ids(con, task_id)
    statuses = []
    for pid in parents:
        parent = get(con, pid)
        statuses.append(parent["status"] if parent is not None else "done")  # a deleted parent holds nobody
    if not parents or all(status == "done" for status in statuses):
        return "ready", parents
    if any(status in {"failed", "stopped"} for status in statuses):
        return "failed", parents
    return "waiting", parents


@_serialized
def promote_task(con, task_id) -> bool:
    state, parents = dependency_state(con, task_id)
    if state != "ready":
        return False
    with _write_txn(con):
        cur = con.execute(
            "UPDATE tasks SET status='ready',block_kind=NULL,block_reason=NULL,blocked_at=NULL "
            "WHERE id=? AND status='todo'",
            (task_id,),
        )
        if cur.rowcount == 1:
            add_event(con, task_id, "dependencies_satisfied", {"parents": parents})
            _mirror_status(con, task_id)
        return cur.rowcount == 1


@_serialized
def promote_dependents(con, parent_id):
    promoted = []
    for child in _children_of(con, parent_id):
        if promote_task(con, child):
            promoted.append(child)
    return promoted


def _descendant_ids(con, task_id):
    out, seen, frontier = [], {task_id}, [task_id]
    while frontier:
        current = frontier.pop(0)
        for child in _children_of(con, current):
            if child not in seen:
                seen.add(child)
                out.append(child)
                frontier.append(child)
    return out


def _invalidate_descendants(con, task_id):
    descendants = _descendant_ids(con, task_id)
    if not descendants:
        return []
    marks = ",".join("?" * len(descendants))
    active = con.execute(
        f"SELECT id FROM tasks WHERE id IN ({marks}) "
        "AND status IN ('running','review')",
        descendants,
    ).fetchall()
    if active:
        raise RuntimeError("Active child tasks must stop before this task can resume: " + ','.join(row[0] for row in active))
    with _write_txn(con):
        changed = [row[0] for row in con.execute(
            "UPDATE tasks SET status='todo',completed_at=NULL,claim_lock=NULL,claim_expires=NULL,"
            "worker_pid=NULL,worker_identity=NULL,review_lock=NULL,review_expires=NULL,"
            "review_pid=NULL,review_identity=NULL,generation=generation+1 WHERE id IN (" + marks + ") "
            "AND status<>'todo' RETURNING id",
            descendants,
        ).fetchall()]
        for child_id in changed:
            add_event(con, child_id, "dependency_invalidated", {"reopened_ancestor": task_id})
            _mirror_status(con, child_id)
    return changed


@_serialized
def set_workspace(con, task_id, workspace, generation=None, claim_lock=None):
    clauses = ["id=?"]
    values = [workspace, task_id]
    if generation is not None:
        clauses.append("generation=?")
        values.append(generation)
    if claim_lock is not None:
        clauses.extend(["status='running'", "claim_lock=?", "claim_expires>=?"])
        values.extend([claim_lock, int(time.time())])
    with _write_txn(con):
        cur = con.execute(
            f"UPDATE tasks SET workspace=? WHERE {' AND '.join(clauses)}",
            tuple(values),
        )
        if cur.rowcount == 1:
            con.execute(
                "UPDATE task_runs SET workspace=? WHERE id=(SELECT current_run_id FROM tasks WHERE id=?)",
                (workspace, task_id),
            )
        return cur.rowcount == 1


@_serialized
def set_runtime(con, task_id, agent_id, session_file, generation=None, claim_lock=None):
    """Record the Sister's agent id and session file on the card so it can be addressed later."""
    clauses = ["id=?"]
    values = [agent_id, session_file, os.path.dirname(session_file) if session_file else None, task_id]
    if generation is not None:
        clauses.append("generation=?")
        values.append(generation)
    if claim_lock is not None:   # Same fence as set_workspace/set_pid: the lease must still be live.
        clauses.extend(["status='running'", "claim_lock=?", "claim_expires>=?"])
        values.extend([claim_lock, int(time.time())])
    with _write_txn(con):
        cur = con.execute(
            f"UPDATE tasks SET agent_id=?, session_file=?, session_dir=COALESCE(session_dir,?) WHERE {' AND '.join(clauses)}",
            tuple(values),
        )
        if cur.rowcount == 1:
            con.execute(
                "UPDATE task_runs SET session_file=? WHERE id=(SELECT current_run_id FROM tasks WHERE id=?)",
                (session_file, task_id),
            )
        return cur.rowcount == 1


@_serialized
def clear_runtime(con, task_id, generation=None, claim_lock=None):
    clauses = ["id=?"]
    values = [task_id]
    if generation is not None:
        clauses.append("generation=?")
        values.append(generation)
    if claim_lock is not None:
        clauses.extend(["status='running'", "claim_lock=?"])
        values.append(claim_lock)
    cur = con.execute(
        f"UPDATE tasks SET agent_id=NULL, session_file=NULL WHERE {' AND '.join(clauses)}",
        tuple(values),
    )
    return cur.rowcount == 1


@_serialized
def set_pid(con, task_id, pid, worker_identity=None, generation=None, claim_lock=None):
    clauses = ["id=?"]
    values = [pid, worker_identity, task_id]
    if generation is not None:
        clauses.append("generation=?")
        values.append(generation)
    if claim_lock is not None:
        clauses.extend(["status='running'", "claim_lock=?", "claim_expires>=?"])
        values.extend([claim_lock, int(time.time())])
    with _write_txn(con):
        cur = con.execute(
            f"UPDATE tasks SET worker_pid=?, worker_identity=? WHERE {' AND '.join(clauses)}",
            tuple(values),
        )
        if cur.rowcount == 1:
            con.execute(
                "UPDATE task_runs SET pid=?,process_identity=? "
                "WHERE id=(SELECT current_run_id FROM tasks WHERE id=?)",
                (pid, worker_identity, task_id),
            )
        return cur.rowcount == 1


@_serialized
def configure_review(con, task_id, reviewer=None) -> bool:
    reviewer = str(reviewer or "").strip() or None
    row = get(con, task_id)
    if row is None:
        return False
    if reviewer == row["assignee"]:
        raise ValueError("The reviewer must be different from the assignee.")
    with _write_txn(con):
        cur = con.execute(
            "UPDATE tasks SET reviewer=?,review_feedback=NULL WHERE id=? "
            "AND status IN ('todo','ready')",
            (reviewer, task_id),
        )
        if cur.rowcount == 1:
            add_event(con, task_id, "review_configured", {"reviewer": reviewer})
            if row["workspace"]:
                from misaka.core.platform import cards
                cards.set_fields(row["workspace"], task_id, reviewer=reviewer)   # OSError rolls the change back
        return cur.rowcount == 1


def _terminal_transition(
    con, task_id, status, generation=None, claim_lock=None,
    failure_kind=None, failure_fingerprint=None, summary=None,
):
    now = int(time.time())
    clauses = ["id=?"]
    values = [task_id]
    if claim_lock is not None:
        clauses.extend(["status='running'", "claim_lock=?", "claim_expires>=?"])
        values.extend([claim_lock, now])
    if generation is not None:
        clauses.append("generation=?")
        values.append(generation)
    with _write_txn(con):
        cur = con.execute(
            f"UPDATE tasks SET status='{status}', completed_at=?, worker_pid=NULL, "
            "worker_identity=NULL, claim_lock=NULL, claim_expires=NULL, "
            "review_lock=NULL,review_expires=NULL,review_pid=NULL,review_identity=NULL "
            f"WHERE {' AND '.join(clauses)}",
            (now, *values),
        )
        if cur.rowcount == 1:
            _finish_current_run(
                con, task_id, status, failure_kind=failure_kind,
                failure_fingerprint=failure_fingerprint, summary=summary,
            )
            if status == "done":
                _clear_failures(con, task_id)
                con.execute(
                    "UPDATE tasks SET block_kind=NULL,block_reason=NULL,block_fingerprint=NULL,"
                    "block_recurrence=0,blocked_at=NULL WHERE id=?",
                    (task_id,),
                )
                promote_dependents(con, task_id)
            _mirror_status(con, task_id, commit=True)
        return cur.rowcount == 1


# Ordered: the first kind whose pattern matches wins. Quota and billing markers go before the
# rate-limit check because a provider reports an exhausted quota with the same 429 status.
# Every marker is a word or phrase; a bare status code is only trusted where nothing else uses
# that number, since the reason is often the tail of a crashed process's stderr.
_FAILURE_KINDS = (
    # Quota means an exhausted allowance, not a rate limit that mentions one: Gemini's 429
    # reads "Resource has been exhausted (e.g. check quota)" and must stay retryable.
    ("auth_or_quota", re.compile(
        r"authentication|unauthori[sz]ed|permission[ _]error|permissiondeniederror"
        r"|\b403 forbidden\b|invalid[ _-]?(?:api[ _-]?key|token)|api[ _-]?key"
        r"|insufficient[ _-]?quota|quota exceeded|exceeded your (?:current )?quota"
        r"|billing|payment required|credit balance|insufficient[ _-]?credit")),
    ("rate_limit", re.compile(r"rate[ _-]?limit|too many requests|\b429\b")),
    ("context_overflow", re.compile(
        r"context[ _-]?(?:length|window)|maximum context|too many tokens|prompt is too long"
        r"|input is too long|exceeds? the (?:context|token)")),
    ("timeout", re.compile(r"time(?:d[ -]?| )?out|deadline exceeded")),
    ("server_error", re.compile(
        r"overloaded|internal server error|service unavailable|bad gateway|\b(?:500|502|503|529)\b")),
    ("protocol_violation", re.compile(r"without settling|unparsable|bad schema|protocol")),
    ("crash", re.compile(r"reclaim|crash|supervisor|exited with code|killed by signal|segfault")),
)


def classify_failure(reason):
    text = str(reason or "").casefold()
    for kind, pattern in _FAILURE_KINDS:
        if pattern.search(text):
            return kind
    return "failure"


def retry_delay(kind, failures):
    """Seconds to hold a card back after its ``failures``-th consecutive failure of ``kind``."""
    if kind == "protocol_violation":
        return 0          # the model ended its turn early; there is nothing external to wait for
    return min(RETRY_CAP_SECONDS, RETRY_BASE_SECONDS * (2 ** max(0, int(failures) - 1)))


def _owner_clauses(task_id, generation, claim_lock, now):
    clauses, values = ["id=?"], [task_id]
    if claim_lock is not None:
        clauses.extend(["status='running'", "claim_lock=?", "claim_expires>=?"])
        values.extend([claim_lock, now])
    if generation is not None:
        clauses.append("generation=?")
        values.append(generation)
    return " AND ".join(clauses), values


@_serialized
def mark_failed(
    con, task_id, generation=None, claim_lock=None,
    *, failure_kind=None, reason=None,
):
    """Record one failed attempt under the card's ownership fence.

    A rate limit defers the card and counts nothing. Any other failure counts against
    ``FAILURE_LIMIT``: below it the card returns to ready after a backoff, keeping the error for
    the next attempt; at it, or for a kind a retry cannot fix, the card stays failed.
    """
    if reason is None:
        raw = latest_payload(con, task_id, "failed", generation=generation)
        try:
            reason = json.loads(raw or "{}").get("reason")
        except (ValueError, AttributeError, TypeError):
            reason = raw
    reason = redact(reason)        # kept as the next attempt's context and the run's summary
    kind = failure_kind or classify_failure(reason)
    fingerprint = (hashlib.sha256(str(reason).encode()).hexdigest()[:16]
                   if reason else None)
    summary = str(reason)[:1000] if reason else None
    now = int(time.time())
    where, values = _owner_clauses(task_id, generation, claim_lock, now)
    if kind == "rate_limit":
        previous = con.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=? AND failure_kind='rate_limit'",
            (task_id,),
        ).fetchone()[0]
        delay = min(RETRY_CAP_SECONDS, RETRY_BASE_SECONDS * (2 ** min(5, int(previous))))
        with _write_txn(con):
            cur = con.execute(
                "UPDATE tasks SET status='ready',next_attempt_at=?,completed_at=NULL,"
                "worker_pid=NULL,worker_identity=NULL,claim_lock=NULL,claim_expires=NULL "
                f"WHERE {where}",
                (now + delay, *values),
            )
            if cur.rowcount == 1:
                _finish_current_run(
                    con, task_id, "deferred", failure_kind=kind,
                    failure_fingerprint=fingerprint, summary=summary,
                )
                add_event(con, task_id, "cooldown", {
                    "failure_kind": kind, "seconds": delay, "until": now + delay,
                }, generation=generation)
                _mirror_status(con, task_id)
            return cur.rowcount == 1
    row = con.execute("SELECT consecutive_failures FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return False
    failures = int(row["consecutive_failures"]) + 1
    error = summary or kind
    tally = {"failure_kind": kind, "failures": failures, "limit": FAILURE_LIMIT, "reason": error[:500]}
    if kind in TERMINAL_FAILURE_KINDS or failures >= FAILURE_LIMIT:
        with _write_txn(con):
            if not _terminal_transition(
                con, task_id, "failed", generation, claim_lock,
                failure_kind=kind, failure_fingerprint=fingerprint, summary=summary,
            ):
                return False
            # The transition above matched the fence, so this row is ours to stamp.
            con.execute(
                "UPDATE tasks SET consecutive_failures=?,last_failure_error=? WHERE id=?",
                (failures, error, task_id),
            )
            add_event(con, task_id, "gave_up", tally, generation=generation)
        return True
    delay = retry_delay(kind, failures)
    with _write_txn(con):
        cur = con.execute(
            "UPDATE tasks SET status='ready',next_attempt_at=?,consecutive_failures=?,"
            "last_failure_error=?,completed_at=NULL,worker_pid=NULL,worker_identity=NULL,"
            "claim_lock=NULL,claim_expires=NULL,review_lock=NULL,review_expires=NULL,"
            "review_pid=NULL,review_identity=NULL "
            f"WHERE {where}",
            (now + delay if delay else None, failures, error, *values),
        )
        if cur.rowcount == 1:
            _finish_current_run(
                con, task_id, "failed", failure_kind=kind,
                failure_fingerprint=fingerprint, summary=summary,
            )
            add_event(con, task_id, "retry_scheduled",
                      {**tally, "seconds": delay, "until": now + delay}, generation=generation)
            _mirror_status(con, task_id)
        return cur.rowcount == 1


def mark_unsettled(con, task_id, *, generation, claim_lock, reason=UNSETTLED_REASON):
    """The worker's session ended cleanly but never settled the card: a protocol violation.

    Counted like any failure, so a model that keeps ending its turn without a summary runs out
    of attempts instead of being dispatched forever.
    """
    return mark_failed(
        con, task_id, generation=generation, claim_lock=claim_lock,
        failure_kind="protocol_violation", reason=reason,
    )


@_serialized
def mark_stopped(con, task_id, generation=None, claim_lock=None):
    clauses = ["id=?"]
    values = [int(time.time()), task_id]
    if generation is not None:
        clauses.append("generation=?")
        values.append(generation)
    if claim_lock is not None:
        clauses.extend(["status='running'", "claim_lock=?", "claim_expires>=?"])
        values.extend([claim_lock, int(time.time())])
        status_clause = ""
    else:
        status_clause = (" AND status IN ('triage','todo','ready','running',"
                         "'blocked','review')")
    with _write_txn(con):
        cur = con.execute(
            "UPDATE tasks SET status='stopped', completed_at=?, worker_pid=NULL, "
            "worker_identity=NULL, claim_lock=NULL, claim_expires=NULL, "
            "review_lock=NULL,review_expires=NULL,review_pid=NULL,review_identity=NULL "
            f"WHERE {' AND '.join(clauses)}{status_clause}",
            tuple(values),
        )
        if cur.rowcount == 1:
            _finish_current_run(con, task_id, "stopped")
            _mirror_status(con, task_id, commit=True)
        return cur.rowcount == 1








@_serialized
def submit_task(con, task_id, generation=None, claim_lock=None, *, commit=True):
    """Accept a running card, or hand its submitted payload to the named reviewer."""
    now = int(time.time())
    clauses = ["id=?"]
    values = [now, task_id]
    if generation is not None:
        clauses.append("generation=?")
        values.append(generation)
    if claim_lock is not None:
        clauses.extend(["status='running'", "claim_lock=?", "claim_expires>=?"])
        values.extend([claim_lock, now])
    with _write_txn(con):
        cur = con.execute(
            "UPDATE tasks SET status=CASE WHEN reviewer IS NULL THEN 'done' ELSE 'review' END, "
            "completed_at=CASE WHEN reviewer IS NULL THEN ? ELSE NULL END, "
            "worker_pid=NULL, claim_lock=NULL, worker_identity=NULL, claim_expires=NULL, "
            "review_lock=NULL,review_expires=NULL,review_pid=NULL,review_identity=NULL "
            f"WHERE {' AND '.join(clauses)}",
            tuple(values),
        )
        if cur.rowcount == 1:
            _settle_done(con, task_id)
            _mirror_status(con, task_id, commit=commit)
        return cur.rowcount == 1


def _settle_done(con, task_id):
    """After a submission or an approval: close the run; a done card sheds its block and wakes
    its dependents."""
    status = con.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
    _finish_current_run(con, task_id, "done" if status == "done" else "submitted")
    _clear_failures(con, task_id)
    if status == "done":
        con.execute(
            "UPDATE tasks SET block_kind=NULL,block_reason=NULL,block_fingerprint=NULL,"
            "block_recurrence=0,blocked_at=NULL WHERE id=?", (task_id,),
        )
        promote_dependents(con, task_id)


@_serialized
def claim_review(
    con, task_id, reviewer, lock, ttl_seconds=1800, generation=None,
    pid=None, reviewer_identity=None,
) -> bool:
    now = int(time.time())
    if not _card_dispatchable(con, task_id):
        return False
    generation_clause = " AND generation=?" if generation is not None else ""
    params = [lock, now + max(60, int(ttl_seconds)), pid, reviewer_identity,
              task_id, reviewer, now, lock]
    if generation is not None:
        params.append(generation)
    with _write_txn(con):
        row = con.execute(
            "SELECT t.generation,t.reviewer,r.phase,r.status AS run_status,r.claim_lock "
            "FROM tasks t LEFT JOIN task_runs r ON r.id=t.current_run_id WHERE t.id=?",
            (task_id,),
        ).fetchone()
        if row is None or row["reviewer"] != reviewer:
            return False
        if (row["phase"] == "reviewer" and row["run_status"] == "running"
                and row["claim_lock"] == lock):
            return True
        cur = con.execute(
            "UPDATE tasks SET review_lock=?,review_expires=?,review_pid=?,review_identity=?,"
            "review_rounds=review_rounds+1 WHERE id=? AND reviewer=? AND status='review' "
            "AND (review_lock IS NULL OR review_expires IS NULL OR review_expires<? "
            "OR review_lock=?)" + generation_clause,
            tuple(params),
        )
        if cur.rowcount != 1:
            return False
        if row["phase"] == "reviewer" and row["run_status"] == "running":
            _finish_current_run(
                con, task_id, "reclaimed", failure_kind="stale_reclaim"
            )
        _start_run(
            con, task_id, int(row["generation"]), "reviewer", lock, pid,
            reviewer_identity, assignee=reviewer,
        )
        return True


@_serialized
def approve_review(con, task_id, lock, *, generation=None, summary=None) -> bool:
    now = int(time.time())
    clauses = ["id=?", "status='review'", "review_lock=?", "review_expires>=?"]
    values = [now, task_id, lock, now]
    if generation is not None:
        clauses.append("generation=?")
        values.append(generation)
    with _write_txn(con):
        cur = con.execute(
            "UPDATE tasks SET status='done',completed_at=?,review_feedback=NULL,review_lock=NULL,"
            "review_expires=NULL,review_pid=NULL,review_identity=NULL "
            f"WHERE {' AND '.join(clauses)}",
            tuple(values),
        )
        if cur.rowcount == 1:
            _finish_current_run(con, task_id, "approved", summary=summary)
            _clear_failures(con, task_id)
            con.execute(
                "UPDATE tasks SET block_kind=NULL,block_reason=NULL,block_fingerprint=NULL,"
                "block_recurrence=0,blocked_at=NULL WHERE id=?", (task_id,),
            )
            promote_dependents(con, task_id)
            reviewer = con.execute(
                "SELECT reviewer FROM tasks WHERE id=?", (task_id,)
            ).fetchone()[0]
            add_event(con, task_id, "review_approved",
                      {"reviewer": reviewer, "summary": summary or ""},
                      generation=generation)
            _mirror_status(con, task_id, commit=True)
        return cur.rowcount == 1


@_serialized
def request_review_changes(
    con, task_id, lock, feedback, *, generation=None
) -> bool:
    feedback = redact(str(feedback or "").strip())
    if not feedback:
        raise ValueError("Review feedback cannot be empty.")
    now = int(time.time())
    state, _parents = dependency_state(con, task_id)
    reviewer_row = con.execute("SELECT reviewer FROM tasks WHERE id=?", (task_id,)).fetchone()
    target = "ready" if state == "ready" else "todo"
    clauses = ["id=?", "status='review'", "review_lock=?", "review_expires>=?"]
    values = [task_id, lock, now]
    if generation is not None:
        clauses.append("generation=?")
        values.append(generation)
    with _write_txn(con):
        cur = con.execute(
            "UPDATE tasks SET status=?,review_feedback=?,review_lock=NULL,review_expires=NULL,"
            "review_pid=NULL,review_identity=NULL,completed_at=NULL "
            f"WHERE {' AND '.join(clauses)}",
            (target, feedback[:4000], *values),
        )
        if cur.rowcount == 1:
            _finish_current_run(con, task_id, "changes_requested", summary=feedback[:1000])
            add_event(con, task_id, "review_changes", {"feedback": feedback[:4000]},
                      generation=generation)
            ws_row = con.execute("SELECT workspace FROM tasks WHERE id=?", (task_id,)).fetchone()
            if ws_row is not None:
                try:
                    from misaka.core.platform import cards
                    cards.append_log(ws_row["workspace"], task_id,
                                     f"reviewer:{reviewer_row[0] if reviewer_row else 'unknown'}",
                                     f"[review] {feedback[:2000]}")
                except OSError:
                    pass                       # a stray index row without a file: the feedback column still has it
            _mirror_status(con, task_id, commit=True)
        return cur.rowcount == 1


@_serialized
def release_review(con, task_id, lock, *, generation=None) -> bool:
    generation_clause = " AND generation=?" if generation is not None else ""
    params = [task_id, lock]
    if generation is not None:
        params.append(generation)
    with _write_txn(con):
        cur = con.execute(
            "UPDATE tasks SET review_lock=NULL,review_expires=NULL,review_pid=NULL,"
            "review_identity=NULL WHERE id=? AND status='review' AND review_lock=?"
            + generation_clause,
            tuple(params),
        )
        if cur.rowcount == 1:
            _finish_current_run(con, task_id, "released")
            _mirror_status(con, task_id)
        return cur.rowcount == 1


@_serialized
def latest_payload(con, task_id, kind, generation=None):
    generation_clause = " AND generation=?" if generation is not None else ""
    params = [task_id, kind]
    if generation is not None:
        params.append(generation)
    row = con.execute(
        "SELECT payload FROM events WHERE task_id=? AND kind=?"
        + generation_clause
        + " ORDER BY id DESC LIMIT 1",
        tuple(params),
    ).fetchone()
    return row["payload"] if row else None


@_serialized
def back_to_ready(con, task_id, generation=None, claim_lock=None):
    clauses = ["id=?"]
    values = [task_id]
    if claim_lock is not None:
        clauses.extend(["status='running'", "claim_lock=?", "claim_expires>=?"])
        values.extend([claim_lock, int(time.time())])
    if generation is not None:
        clauses.append("generation=?")
        values.append(generation)
    with _write_txn(con):
        cur = con.execute(
            "UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, "
            "worker_identity=NULL,review_lock=NULL,review_expires=NULL,review_pid=NULL,"
            "review_identity=NULL "
            f"WHERE {' AND '.join(clauses)}",
            tuple(values),
        )
        if cur.rowcount == 1:
            _finish_current_run(con, task_id, "released")
            _mirror_status(con, task_id)
        return cur.rowcount == 1


@_serialized
def reclaim_abandoned(
    con,
    task_id,
    *,
    generation,
    claim_lock,
    worker_pid,
    worker_identity,
    claim_expires,
    reason=ABANDONED_REASON,
) -> str | None:
    """Release a running card whose worker died, matching its exact ownership fence.

    The dead attempt counts against ``FAILURE_LIMIT`` like any other failure: the card returns
    to ready (``"ready"``) or, at the limit, stays failed (``"failed"``). ``None`` means the
    fence no longer matched. Acceptance only happens through a generation-scoped ``submitted``
    event.
    """
    ownership_where = (
        "WHERE id=? AND generation=? AND status='running' AND claim_lock IS ? "
        "AND worker_pid IS ? AND worker_identity IS ? AND claim_expires IS ?"
    )
    ownership = (task_id, generation, claim_lock, worker_pid, worker_identity, claim_expires)
    with _write_txn(con):
        row = con.execute(
            "SELECT consecutive_failures FROM tasks " + ownership_where, ownership
        ).fetchone()
        if row is None:
            return None
        failures = int(row["consecutive_failures"]) + 1
        gave_up = failures >= FAILURE_LIMIT
        status = "failed" if gave_up else "ready"
        error = str(reason)[:1000]
        cur = con.execute(
            f"UPDATE tasks SET status='{status}', completed_at=?, claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL, worker_identity=NULL, "
            "review_lock=NULL,review_expires=NULL,review_pid=NULL,review_identity=NULL, "
            "consecutive_failures=?, last_failure_error=? " + ownership_where,
            (int(time.time()) if gave_up else None, failures, error, *ownership),
        )
        if cur.rowcount != 1:
            return None
        _finish_current_run(
            con, task_id, "failed" if gave_up else "reclaimed",
            failure_kind="crash", failure_fingerprint="reclaim", summary=error,
        )
        if gave_up:
            add_event(con, task_id, "gave_up",
                      {"failure_kind": "crash", "failures": failures, "limit": FAILURE_LIMIT,
                       "reason": error[:500]},
                      generation=generation)
        _mirror_status(con, task_id, commit=True)
        return status


@_serialized
def claim_resume(
    con,
    task_id,
    lock,
    pid,
    ttl_seconds=1800,
    worker_identity=None,
    expected_generation=None,
    host_cap=None,
    assignee_cap=None,
) -> bool:
    """Atomically reopen a finished card as a new generation and claim it for continuation."""
    now = int(time.time())
    if not _card_dispatchable(con, task_id):
        return False
    generation_clause = (
        " AND generation=?" if expected_generation is not None else ""
    )
    with _write_txn(con):
        current = con.execute("SELECT assignee FROM tasks WHERE id=?", (task_id,)).fetchone()
        if current is None:
            return False
        if host_cap is not None and con.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='running' AND claim_lock IS NOT NULL"
        ).fetchone()[0] >= max(0, int(host_cap)):
            return False
        if assignee_cap is not None and con.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='running' AND claim_lock IS NOT NULL "
            "AND assignee=?", (current["assignee"],)
        ).fetchone()[0] >= max(0, int(assignee_cap)):
            return False
        row = con.execute(
            "UPDATE tasks SET status='running',review_rounds=0,"
            "review_feedback=NULL,completed_at=NULL, "
            "claim_lock=?, claim_expires=?, worker_pid=?, worker_identity=?, "
            "review_lock=NULL,review_expires=NULL,review_pid=NULL,review_identity=NULL,"
            "block_kind=NULL,block_reason=NULL,blocked_at=NULL,heartbeat_at=?,next_attempt_at=NULL, "
            "consecutive_failures=0,last_failure_error=NULL, "
            "generation=generation+1 WHERE id=? "
            "AND status IN ('done','failed','stopped','blocked','triage')"
            + generation_clause + " RETURNING generation",
            tuple([lock, now + ttl_seconds, pid, worker_identity, now, task_id]
                  + ([expected_generation] if expected_generation is not None else [])),
        ).fetchone()
        if row is None:
            return False
        _invalidate_descendants(con, task_id)     # work built on the old answer goes back to todo
        _start_run(con, task_id, int(row["generation"]), "worker", lock, pid, worker_identity)
        # Publish the new generation under the card lock before its worker can run.
        # Submission's Git tail holds that lock without a SQLite transaction, so an
        # old generation can never commit bytes written by this continuation.
        from misaka.core.platform import cards
        workspace = get(con, task_id)["workspace"]
        cards.set_fields(workspace, task_id, generation=int(row["generation"]))
    return True


@_serialized
def reopen_task(
    con,
    task_id,
    *,
    target_status=None,
    expected_generation=None,
    invalidate_descendants=False,
) -> bool:
    """Reopen a finished card as a new generation without claiming a worker yet."""
    with _write_txn(con):
        row = get(con, task_id)
        if row is None or row["status"] not in {"done", "failed", "stopped", "blocked", "triage"}:
            return False
        if expected_generation is not None and int(row["generation"]) != int(expected_generation):
            return False
        if invalidate_descendants:
            _invalidate_descendants(con, task_id)
        if target_status is None:
            # Lenient on purpose: an unreadable card cannot reach the end of this call at all
            # (``_mirror_status`` below rewrites its file and raises, rolling the row back), so
            # the empty answer here is never the one that decides a reopened card's state.
            target_status = "todo" if parent_ids(con, task_id) else "ready"
        if target_status not in {"todo", "ready"}:
            raise ValueError("Only todo or ready tasks can be reset.")
        generation = int(row["generation"]) + 1
        cur = con.execute(
            "UPDATE tasks SET status=?,generation=?,started_at=NULL,completed_at=NULL,"
            "agent_id=NULL,session_file=NULL,claim_lock=NULL,claim_expires=NULL,"
            "worker_pid=NULL,worker_identity=NULL,review_rounds=0,"
            "review_feedback=NULL,review_lock=NULL,review_expires=NULL,review_pid=NULL,"
            "review_identity=NULL,block_kind=NULL,"
            "block_reason=NULL,blocked_at=NULL,heartbeat_at=NULL,next_attempt_at=NULL,"
            "consecutive_failures=0,last_failure_error=NULL "
            "WHERE id=? AND generation=?",
            (target_status, generation, task_id, row["generation"]),
        )
        if cur.rowcount != 1:
            return False
        con.execute("DELETE FROM budget_reservations WHERE task_id=?", (task_id,))
        add_event(
            con, task_id, "reopened", {"from_generation": row["generation"]},
            generation=generation,
        )
        if target_status == "todo":
            promote_task(con, task_id)
        _mirror_status(con, task_id)
        return True


BLOCK_KINDS = frozenset({"dependency", "needs_input", "capability", "transient"})


@_serialized
def block_task(
    con,
    task_id,
    kind,
    reason,
    *,
    generation=None,
    claim_lock=None,
    message_id=None,
) -> str | None:
    """Record a block; the same cause reported twice in a row routes the card to triage."""
    kind = str(kind or "").strip()
    reason = redact(str(reason or "").strip())
    if kind not in BLOCK_KINDS:
        raise ValueError("Block kind must be dependency, needs_input, capability, or transient.")
    if not reason:
        raise ValueError("Block reason cannot be empty.")
    fingerprint = hashlib.sha256(f"{kind}\0{reason}".encode()).hexdigest()[:16]
    row = get(con, task_id)
    if row is None:
        return None
    recurrence = int(row["block_recurrence"] or 0) + 1 \
        if row["block_fingerprint"] == fingerprint else 1
    status = "triage" if recurrence >= 2 else "blocked"
    clauses = ["id=?"]
    values = [task_id]
    if generation is not None:
        clauses.append("generation=?")
        values.append(generation)
    if claim_lock is not None:
        clauses.extend(["status='running'", "claim_lock=?", "claim_expires>=?"])
        values.extend([claim_lock, int(time.time())])
    else:
        clauses.append("status IN ('todo','ready','running','blocked')")
    now = int(time.time())
    with _write_txn(con):
        cur = con.execute(
            "UPDATE tasks SET status=?,block_kind=?,block_reason=?,block_fingerprint=?,"
            "block_recurrence=?,blocked_at=?,completed_at=NULL,claim_lock=NULL,"
            "claim_expires=NULL,worker_pid=NULL,worker_identity=NULL,review_lock=NULL,"
            "review_expires=NULL,review_pid=NULL,review_identity=NULL WHERE "
            + " AND ".join(clauses),
            (status, kind, reason, fingerprint, recurrence, now, *values),
        )
        if cur.rowcount != 1:
            return None
        _finish_current_run(
            con, task_id, status, failure_kind=f"blocked:{kind}",
            failure_fingerprint=fingerprint, summary=reason,
        )
        payload = {"kind": kind, "reason": reason, "fingerprint": fingerprint,
                   "recurrence": recurrence}
        if message_id is not None:
            payload["message_id"] = int(message_id)
        add_event(con, task_id, status, payload, generation=generation)
        _mirror_status(con, task_id, commit=True)
        return status


@_serialized
def unblock_task(con, task_id) -> bool:
    row = get(con, task_id)
    if row is None or row["status"] not in {"blocked", "triage"}:
        return False
    # Lenient for the same reason as ``reopen_task``: the ``_mirror_status`` below cannot write
    # an unreadable card, so such a card's unblock raises and rolls back rather than resting on
    # this answer. Holding it at ``todo`` instead would only be undone by ``promote_task``.
    target = "todo" if parent_ids(con, task_id) else "ready"
    with _write_txn(con):
        cur = con.execute(
            "UPDATE tasks SET status=?,block_kind=NULL,block_reason=NULL,blocked_at=NULL,"
            "consecutive_failures=0,last_failure_error=NULL "
            "WHERE id=? AND status IN ('blocked','triage')",
            (target, task_id),
        )
        if cur.rowcount == 1:
            add_event(con, task_id, "unblocked", {"target": target})
            if target == "todo":
                promote_task(con, task_id)
            _mirror_status(con, task_id)
        return cur.rowcount == 1


@_serialized
def heartbeat(con, task_id, lock, *, generation=None, ttl_seconds=1800, progress=True) -> bool:
    """Renew whichever lease (worker or reviewer) ``lock`` holds.

    With ``progress`` the card's ``heartbeat_at`` is stamped too: the worker's own session
    calls it on activity, so the stamp means the model is getting somewhere. A supervisor
    that only keeps a lease alive renews with ``progress=False``, so a wedged worker still
    reads as stale for ``HEARTBEAT_STALE_SECONDS``.
    """
    now = int(time.time())
    stamp = ",heartbeat_at=?" if progress else ""
    stamped = [now] if progress else []
    generation_clause = " AND generation=?" if generation is not None else ""
    suffix = [generation] if generation is not None else []
    with _write_txn(con):
        cur = con.execute(
            f"UPDATE tasks SET claim_expires=?{stamp} WHERE id=? AND status='running' "
            "AND claim_lock=?" + generation_clause,
            (now + max(60, int(ttl_seconds)), *stamped, task_id, lock, *suffix),
        )
        if cur.rowcount != 1:
            cur = con.execute(
                f"UPDATE tasks SET review_expires=?{stamp} WHERE id=? "
                "AND status='review' AND review_lock=?" + generation_clause,
                (now + max(60, int(ttl_seconds)), *stamped, task_id, lock, *suffix),
            )
        if cur.rowcount == 1 and progress:
            con.execute(
                "UPDATE task_runs SET heartbeat_at=? "
                "WHERE id=(SELECT current_run_id FROM tasks WHERE id=?) AND status='running'",
                (now, task_id),
            )
        return cur.rowcount == 1

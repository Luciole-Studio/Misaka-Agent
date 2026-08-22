"""Durable SQLite storage for task cards, runs, and research workflows."""

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

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
 timeout_seconds INTEGER NOT NULL DEFAULT 900,
 workspace TEXT NOT NULL,
 output_dir TEXT,
 agent_id TEXT,
 session_file TEXT,
 claim_lock TEXT,
 claim_expires INTEGER,
 worker_pid INTEGER,
 worker_identity TEXT,
 current_run_id TEXT,
 generation INTEGER NOT NULL DEFAULT 1,
 notified_generation INTEGER NOT NULL DEFAULT 0,
 verify_rounds INTEGER NOT NULL DEFAULT 0,
 review_rounds INTEGER NOT NULL DEFAULT 0,
 review_feedback TEXT,
 review_lock TEXT,
 review_expires INTEGER,
 review_pid INTEGER,
 review_identity TEXT,
 verify_lock TEXT,
 verify_expires INTEGER,
 verify_pid INTEGER,
 verify_identity TEXT,
 block_kind TEXT,
 block_reason TEXT,
 block_fingerprint TEXT,
 block_recurrence INTEGER NOT NULL DEFAULT 0,
 blocked_at INTEGER,
 heartbeat_at INTEGER,
 next_attempt_at INTEGER,
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
CREATE TABLE IF NOT EXISTS task_links (
 parent_id TEXT NOT NULL,
 child_id TEXT NOT NULL,
 creator_run_id TEXT,
 created_at INTEGER NOT NULL,
 PRIMARY KEY(parent_id, child_id)
);
CREATE TABLE IF NOT EXISTS task_comments (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 task_id TEXT NOT NULL,
 run_id TEXT,
 author TEXT NOT NULL,
 body TEXT NOT NULL,
 kind TEXT NOT NULL DEFAULT 'comment',
 created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS task_attachments (
 id TEXT PRIMARY KEY,
 task_id TEXT NOT NULL,
 kind TEXT NOT NULL,
 name TEXT NOT NULL,
 source TEXT NOT NULL,
 stored_path TEXT,
 sha256 TEXT,
 size_bytes INTEGER,
 status TEXT NOT NULL,
 author TEXT NOT NULL,
 created_at INTEGER NOT NULL
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
CREATE INDEX IF NOT EXISTS idx_task_links_child ON task_links(child_id);
CREATE INDEX IF NOT EXISTS idx_task_links_parent ON task_links(parent_id);
CREATE INDEX IF NOT EXISTS idx_task_comments_task ON task_comments(task_id, id);
CREATE INDEX IF NOT EXISTS idx_task_attachments_task ON task_attachments(task_id, created_at);
"""
RECLAIM_CAP = 2
TASK_SCHEMA_VERSION = 5
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024


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
    }
    existing = _columns(con, "tasks")
    for name, definition in task_columns.items():
        if name not in existing:
            con.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
    if "run_id" not in _columns(con, "events"):
        con.execute("ALTER TABLE events ADD COLUMN run_id TEXT")
    con.execute("CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id)")
    # Older builds recorded a blocked report as a ``blocked`` event but left the
    # task row at failed.  Promote those rows now that a real blocked state exists.
    legacy = con.execute(
        "SELECT t.id,t.generation,e.payload,e.created_at FROM tasks t JOIN events e ON e.id=("
        " SELECT MAX(id) FROM events WHERE task_id=t.id AND generation=t.generation"
        ") WHERE t.status='failed' AND e.kind='blocked'"
    ).fetchall()
    for row in legacy:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        reason = str(payload.get("reason") or 'Waiting for external input')
        if reason.startswith("blocked:"):
            reason = reason[len("blocked:"):].strip()
        fingerprint = hashlib.sha256(f"needs_input\0{reason}".encode()).hexdigest()[:16]
        con.execute(
            "UPDATE tasks SET status='blocked',block_kind='needs_input',block_reason=?,"
            "block_fingerprint=?,block_recurrence=MAX(block_recurrence,1),blocked_at=? "
            "WHERE id=? AND status='failed' AND generation=?",
            (reason, fingerprint, row["created_at"], row["id"], row["generation"]),
        )
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
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    from misaka.platform import notifications
    notifications.init(con)
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


@_serialized
def create_task(con, title, body="", assignee="", model=None, priority=0, timeout_seconds=900,
                executor=None, reviewer=None, workspace=None, output_dir=None):
    """Insert a card, record its ``created`` event, and return the new id.

    ``workspace`` is the project folder the card belongs to; there is no default.
    """
    reviewer = str(reviewer or "").strip() or None
    if reviewer == assignee:
        raise ValueError("The reviewer must be different from the assignee.")
    if not workspace:
        raise ValueError("A task card needs a workspace folder.")
    tid = "t_" + secrets.token_hex(3)
    workspace = canonical_workspace(workspace)
    output_dir = canonical_workspace(output_dir) if output_dir else None
    con.execute(
        "INSERT INTO tasks (id,title,body,assignee,reviewer,executor,model,priority,"
        "timeout_seconds,workspace,output_dir,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, title, body, assignee, reviewer, json.dumps(executor) if executor else None,
         model, priority, timeout_seconds, workspace, output_dir, int(time.time())),
    )
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
    verify_lock=None,
    run_id=None,
):
    """Append an event, optionally fenced by generation and running owner.

    The conditional form is one SQLite statement, so a late supervisor cannot
    append generation-N output after another Last Order has resumed N+1.  An
    owner-fenced event additionally requires the still-live running lease, so
    an expired worker cannot publish output after an orphan takeover in the
    same generation.
    """
    if isinstance(payload, (dict, list)):
        payload = json.dumps(payload, ensure_ascii=False)
    now = int(time.time())
    if run_id is None:
        row = con.execute("SELECT current_run_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        run_id = row[0] if row else None
    if claim_lock is not None and verify_lock is not None:
        raise ValueError("an event can carry only one owner fence")
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
    elif verify_lock is not None:
        if generation is None:
            raise ValueError("a verifier-fenced event requires a generation")
        cur = con.execute(
            "INSERT INTO events (task_id, kind, payload, generation, run_id, created_at) "
            "SELECT ?,?,?,?,?,? WHERE EXISTS "
            "(SELECT 1 FROM tasks WHERE id=? AND generation=? "
            "AND status IN ('verifying','finalizing') AND verify_lock=? "
            "AND verify_expires>=?)",
            (
                task_id,
                kind,
                payload,
                generation,
                run_id,
                now,
                task_id,
                generation,
                verify_lock,
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
    """Commit task-row and task-run changes as one crash-safe unit."""
    owner = not con.in_transaction
    if owner:
        con.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if owner:
            con.rollback()
        raise
    else:
        if owner:
            con.commit()


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
        (status, now, exit_code, failure_kind, failure_fingerprint, summary, task_id),
    )


@_serialized
def runs_for(con, task_id):
    return con.execute(
        "SELECT * FROM task_runs WHERE task_id=? ORDER BY generation,attempt", (task_id,)
    ).fetchall()


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
    with _write_txn(con):
        row = con.execute(
            "SELECT generation,assignee,next_attempt_at FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if row is None or (generation is not None and int(row["generation"]) != int(generation)):
            return False
        if row["next_attempt_at"] is not None and int(row["next_attempt_at"]) > now:
            return False
        if host_cap is not None and con.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='running' AND claim_lock IS NOT NULL"
        ).fetchone()[0] >= max(0, int(host_cap)):
            return False
        if assignee_cap is not None and con.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='running' AND claim_lock IS NOT NULL "
            "AND assignee=?",
            (row["assignee"],),
        ).fetchone()[0] >= max(0, int(assignee_cap)):
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
def delete_task(con, task_id, *, allow_active=False):
    """Delete a card and everything attached to it; active cards are refused unless ``allow_active``."""
    row = con.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return False, f"Card not found: {task_id}"
    if not allow_active and row["status"] in ("running", "review", "verifying", "finalizing"):
        return False, f"Card {task_id} is {row['status']}; stop it before deletion."
    con.execute("DELETE FROM budget_reservations WHERE task_id=?", (task_id,))
    con.execute("DELETE FROM events WHERE task_id=?", (task_id,))
    con.execute("DELETE FROM todos WHERE task_id=?", (task_id,))
    con.execute("DELETE FROM task_runs WHERE task_id=?", (task_id,))
    con.execute("DELETE FROM task_links WHERE parent_id=? OR child_id=?", (task_id, task_id))
    con.execute("DELETE FROM task_comments WHERE task_id=?", (task_id,))
    attachments = con.execute(
        "SELECT stored_path FROM task_attachments WHERE task_id=?", (task_id,)
    ).fetchall()
    con.execute("DELETE FROM task_attachments WHERE task_id=?", (task_id,))
    con.execute(
        "DELETE FROM notification_events WHERE resource_type='task' AND resource_id=?",
        (task_id,),
    )
    con.execute(
        "DELETE FROM notification_subscriptions WHERE resource_type='task' AND resource_id=?",
        (task_id,),
    )
    con.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    for item in attachments:
        try:
            Path(item["stored_path"]).unlink(missing_ok=True) if item["stored_path"] else None
        except OSError:
            pass
    shutil.rmtree(task_state_dir(task_id), ignore_errors=True)
    return True, f"Card {task_id} and its runs, dependencies, comments, attachments, events, budget, and to-do items were deleted."


@_serialized
def by_status(con, status):
    return con.execute(
        "SELECT * FROM tasks WHERE status=? ORDER BY priority DESC, created_at", (status,)
    ).fetchall()


@_serialized
def fair_ready(con, *, limit=None, lane="workers", advance=True, now=None):
    """Return runnable cards round-robin across assignees, keeping priority order within each assignee."""
    now = int(time.time()) if now is None else int(now)
    rows = con.execute(
        "SELECT * FROM tasks WHERE status='ready' "
        "AND (next_attempt_at IS NULL OR next_attempt_at<=?) "
        "ORDER BY assignee,priority DESC,created_at,id",
        (now,),
    ).fetchall()
    queues = {}
    for row in rows:
        queues.setdefault(row["assignee"], []).append(row)
    assignees = sorted(queues)
    state = con.execute("SELECT cursor FROM scheduler_state WHERE lane=?", (lane,)).fetchone()
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
            (lane, selected[-1]["assignee"], now),
        )
    return selected


@_serialized
def link_tasks(con, parent_id, child_id, *, creator_run_id=None) -> bool:
    """Add a parent->child dependency (rejecting cycles) and hold the child until the parent is done."""
    if parent_id == child_id:
        raise ValueError("A task cannot depend on itself.")
    known = con.execute(
        "SELECT id,status FROM tasks WHERE id IN (?,?)", (parent_id, child_id)
    ).fetchall()
    if {row["id"] for row in known} != {parent_id, child_id}:
        raise ValueError("Both ends of a dependency must be existing tasks.")
    child = next(row for row in known if row["id"] == child_id)
    if child["status"] in {"running", "review", "verifying", "finalizing", "done", "archived"}:
        raise ValueError(f"Child task is {child['status']}; its dependencies cannot be changed.")
    cycle = con.execute(
        "WITH RECURSIVE reach(id) AS ("
        " SELECT child_id FROM task_links WHERE parent_id=?"
        " UNION SELECT l.child_id FROM task_links l JOIN reach r ON l.parent_id=r.id"
        ") SELECT 1 FROM reach WHERE id=? LIMIT 1",
        (child_id, parent_id),
    ).fetchone()
    if cycle:
        raise ValueError("Task dependency creates a cycle.")
    with _write_txn(con):
        cur = con.execute(
            "INSERT OR IGNORE INTO task_links(parent_id,child_id,creator_run_id,created_at) "
            "VALUES(?,?,?,?)",
            (parent_id, child_id, creator_run_id, int(time.time())),
        )
        if cur.rowcount == 1:
            con.execute(
                "UPDATE tasks SET status='todo' WHERE id=? AND status='ready'", (child_id,)
            )
            add_event(con, child_id, "dependency_linked", {"parent_id": parent_id})
            promote_task(con, child_id)
        return cur.rowcount == 1


@_serialized
def parent_ids(con, task_id):
    return [row[0] for row in con.execute(
        "SELECT parent_id FROM task_links WHERE child_id=? ORDER BY created_at,parent_id",
        (task_id,),
    ).fetchall()]


@_serialized
def add_comment(con, task_id, author, body, *, kind="comment") -> int:
    body = str(body or "").strip()
    author = str(author or "").strip()
    if not author or not body:
        raise ValueError("Comment author and text cannot be empty.")
    row = con.execute("SELECT current_run_id FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise ValueError(f"Card not found: {task_id}")
    cur = con.execute(
        "INSERT INTO task_comments(task_id,run_id,author,body,kind,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (task_id, row["current_run_id"], author, body, kind, int(time.time())),
    )
    add_event(con, task_id, "commented", {"comment_id": cur.lastrowid, "author": author})
    return int(cur.lastrowid)


@_serialized
def comments(con, task_id, *, after_id=0, limit=100):
    return con.execute(
        "SELECT * FROM task_comments WHERE task_id=? AND id>? ORDER BY id LIMIT ?",
        (task_id, int(after_id), max(1, min(500, int(limit)))),
    ).fetchall()


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@_serialized
def attach_file(con, task_id, source, *, author="last-order", root=None):
    if get(con, task_id) is None:
        raise ValueError(f"Card not found: {task_id}")
    original = Path(os.path.expanduser(str(source)))
    if original.is_symlink():
        raise ValueError("Attachments cannot be symbolic links.")
    try:
        source_path = original.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"Attachment does not exist: {source}") from error
    if not source_path.is_file():
        raise ValueError("Attachments must be regular files.")
    size = source_path.stat().st_size
    if size > MAX_ATTACHMENT_BYTES:
        raise ValueError("Attachments may not exceed 50 MiB.")
    attachment_id = "att_" + secrets.token_hex(5)
    safe_name = source_path.name.replace("\x00", "") or "attachment"
    destination_root = (Path(os.path.expanduser(root)) / task_id
                        if root else Path(task_state_dir(task_id)) / "attachments" / "source")
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / f"{attachment_id}-{safe_name}"
    temp = destination.with_suffix(destination.suffix + ".tmp")
    try:
        shutil.copyfile(source_path, temp)
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)
    digest = _file_sha256(destination)
    con.execute(
        "INSERT INTO task_attachments "
        "(id,task_id,kind,name,source,stored_path,sha256,size_bytes,status,author,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (attachment_id, task_id, "file", safe_name, str(source_path), str(destination),
         digest, size, "ready", author, int(time.time())),
    )
    add_event(con, task_id, "attachment_added", {"attachment_id": attachment_id,
                                                   "name": safe_name, "size": size})
    return attachment_id


@_serialized
def attach_url(con, task_id, url, *, author="last-order"):
    if get(con, task_id) is None:
        raise ValueError(f"Card not found: {task_id}")
    url = str(url or "").strip()
    if not (url.startswith("https://") or url.startswith("http://")):
        raise ValueError("Attachment URLs must use HTTP or HTTPS.")
    attachment_id = "att_" + secrets.token_hex(5)
    con.execute(
        "INSERT INTO task_attachments "
        "(id,task_id,kind,name,source,status,author,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (attachment_id, task_id, "url", url.rsplit("/", 1)[-1] or url, url,
         "reference", author, int(time.time())),
    )
    add_event(con, task_id, "attachment_added", {"attachment_id": attachment_id, "url": url})
    return attachment_id


@_serialized
def attachments(con, task_id):
    return con.execute(
        "SELECT * FROM task_attachments WHERE task_id=? ORDER BY created_at,id", (task_id,)
    ).fetchall()


@_serialized
def stage_attachments(con, task_id, workspace):
    root = Path(task_state_dir(task_id)) / "attachments" / "staged"
    staged = []
    for row in attachments(con, task_id):
        if row["kind"] == "url":
            staged.append({"kind": "url", "source": row["source"], "name": row["name"]})
            continue
        source = Path(row["stored_path"] or "")
        if not source.is_file() or source.is_symlink():
            continue
        root.mkdir(parents=True, exist_ok=True)
        destination = root / f"{row['id']}-{row['name']}"
        shutil.copyfile(source, destination)
        if _file_sha256(destination) != row["sha256"]:
            destination.unlink(missing_ok=True)
            raise RuntimeError(f"Attachment integrity check failed: {row['id']}")
        staged.append({"kind": "file", "path": str(destination),
                       "name": row["name"], "sha256": row["sha256"]})
    return staged


@_serialized
def dependency_state(con, task_id):
    rows = con.execute(
        "SELECT t.id,t.status FROM task_links l JOIN tasks t ON t.id=l.parent_id "
        "WHERE l.child_id=? ORDER BY l.created_at,t.id",
        (task_id,),
    ).fetchall()
    if not rows or all(row["status"] == "done" for row in rows):
        return "ready", [row["id"] for row in rows]
    if any(row["status"] in {"failed", "stopped", "archived"} for row in rows):
        return "failed", [row["id"] for row in rows]
    return "waiting", [row["id"] for row in rows]


@_serialized
def promote_task(con, task_id) -> bool:
    state, parents = dependency_state(con, task_id)
    if state != "ready":
        return False
    cur = con.execute(
        "UPDATE tasks SET status='ready',block_kind=NULL,block_reason=NULL,blocked_at=NULL "
        "WHERE id=? AND status='todo'",
        (task_id,),
    )
    if cur.rowcount == 1:
        add_event(con, task_id, "dependencies_satisfied", {"parents": parents})
    return cur.rowcount == 1


@_serialized
def promote_dependents(con, parent_id):
    promoted = []
    for row in con.execute(
        "SELECT child_id FROM task_links WHERE parent_id=? ORDER BY created_at,child_id",
        (parent_id,),
    ).fetchall():
        if promote_task(con, row["child_id"]):
            promoted.append(row["child_id"])
    return promoted


def _descendant_ids(con, task_id):
    return [row[0] for row in con.execute(
        "WITH RECURSIVE descendants(id) AS ("
        " SELECT child_id FROM task_links WHERE parent_id=?"
        " UNION SELECT l.child_id FROM task_links l JOIN descendants d ON l.parent_id=d.id"
        ") SELECT id FROM descendants",
        (task_id,),
    ).fetchall()]


def _invalidate_descendants(con, task_id):
    descendants = _descendant_ids(con, task_id)
    if not descendants:
        return []
    marks = ",".join("?" * len(descendants))
    active = con.execute(
        f"SELECT id FROM tasks WHERE id IN ({marks}) "
        "AND status IN ('running','review','verifying','finalizing')",
        descendants,
    ).fetchall()
    if active:
        raise RuntimeError("Active child tasks must stop before this task can resume: " + ','.join(row[0] for row in active))
    changed = [row[0] for row in con.execute(
        f"UPDATE tasks SET status='todo',completed_at=NULL,claim_lock=NULL,claim_expires=NULL,"
        "worker_pid=NULL,worker_identity=NULL,review_lock=NULL,review_expires=NULL,"
        "review_pid=NULL,review_identity=NULL,verify_lock=NULL,verify_expires=NULL,"
        "verify_pid=NULL,verify_identity=NULL WHERE id IN (" + marks + ") "
        "AND status NOT IN ('todo','archived') RETURNING id",
        descendants,
    ).fetchall()]
    for child_id in changed:
        add_event(con, child_id, "dependency_invalidated", {"reopened_ancestor": task_id})
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
    values = [agent_id, session_file, task_id]
    if generation is not None:
        clauses.append("generation=?")
        values.append(generation)
    if claim_lock is not None:   # Same fence as set_workspace/set_pid: the lease must still be live.
        clauses.extend(["status='running'", "claim_lock=?", "claim_expires>=?"])
        values.extend([claim_lock, int(time.time())])
    cur = con.execute(
        f"UPDATE tasks SET agent_id=?, session_file=? WHERE {' AND '.join(clauses)}",
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
    cur = con.execute(
        "UPDATE tasks SET reviewer=?,review_feedback=NULL WHERE id=? "
        "AND status IN ('todo','scheduled','ready')",
        (reviewer, task_id),
    )
    if cur.rowcount == 1:
        add_event(con, task_id, "review_configured", {"reviewer": reviewer})
    return cur.rowcount == 1


def _terminal_transition(
    con, task_id, status, verify_token=None, generation=None, claim_lock=None,
    failure_kind=None, failure_fingerprint=None, summary=None,
):
    if verify_token is not None and claim_lock is not None:
        raise ValueError("a terminal transition can carry only one owner fence")
    now = int(time.time())
    clauses = ["id=?"]
    values = [task_id]
    if verify_token is not None:
        clauses.extend([
            "status IN ('verifying','finalizing')",
            "verify_lock=?",
            "verify_expires>=?",
        ])
        values.extend([verify_token, now])
    elif claim_lock is not None:
        clauses.extend(["status='running'", "claim_lock=?", "claim_expires>=?"])
        values.extend([claim_lock, now])
    if generation is not None:
        clauses.append("generation=?")
        values.append(generation)
    with _write_txn(con):
        cur = con.execute(
            f"UPDATE tasks SET status='{status}', completed_at=?, worker_pid=NULL, "
            "worker_identity=NULL, claim_lock=NULL, claim_expires=NULL, "
            "review_lock=NULL,review_expires=NULL,review_pid=NULL,review_identity=NULL,"
            "verify_lock=NULL, verify_expires=NULL, verify_pid=NULL, verify_identity=NULL "
            f"WHERE {' AND '.join(clauses)}",
            (now, *values),
        )
        if cur.rowcount == 1:
            _finish_current_run(
                con, task_id, status, failure_kind=failure_kind,
                failure_fingerprint=failure_fingerprint, summary=summary,
            )
            if status == "done":
                con.execute(
                    "UPDATE tasks SET block_kind=NULL,block_reason=NULL,block_fingerprint=NULL,"
                    "block_recurrence=0,blocked_at=NULL WHERE id=?",
                    (task_id,),
                )
                promote_dependents(con, task_id)
        return cur.rowcount == 1


@_serialized
def mark_done(con, task_id, verify_token=None, generation=None, claim_lock=None):
    return _terminal_transition(
        con, task_id, "done", verify_token, generation, claim_lock
    )


def classify_failure(reason):
    text = str(reason or "").casefold()
    if "rate limit" in text or "429" in text:
        return "rate_limit"
    if any(marker in text for marker in ("quota", "authentication", "unauthorized", "forbidden")):
        return "auth_or_quota"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if any(marker in text for marker in ("no report.json", "unparsable", "bad schema", "protocol")):
        return "protocol_violation"
    if "verification" in text or "verify" in text or 'red team' in text:
        return "verification_failure"
    if "reclaim" in text or "crash" in text or "supervisor" in text:
        return "crash"
    return "failure"


@_serialized
def mark_failed(
    con, task_id, verify_token=None, generation=None, claim_lock=None,
    *, failure_kind=None, reason=None,
):
    if reason is None:
        raw = latest_payload(con, task_id, "failed", generation=generation)
        try:
            reason = json.loads(raw or "{}").get("reason")
        except (ValueError, AttributeError, TypeError):
            reason = raw
    kind = failure_kind or classify_failure(reason)
    fingerprint = (hashlib.sha256(str(reason).encode()).hexdigest()[:16]
                   if reason else None)
    if kind == "rate_limit":
        now = int(time.time())
        previous = con.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=? AND failure_kind='rate_limit'",
            (task_id,),
        ).fetchone()[0]
        delay = min(900, 30 * (2 ** min(5, int(previous))))
        clauses, values = ["id=?"], [task_id]
        if verify_token is not None:
            clauses.extend(["status IN ('verifying','finalizing')", "verify_lock=?",
                            "verify_expires>=?"])
            values.extend([verify_token, now])
        elif claim_lock is not None:
            clauses.extend(["status='running'", "claim_lock=?", "claim_expires>=?"])
            values.extend([claim_lock, now])
        if generation is not None:
            clauses.append("generation=?")
            values.append(generation)
        with _write_txn(con):
            cur = con.execute(
                "UPDATE tasks SET status='ready',next_attempt_at=?,completed_at=NULL,"
                "worker_pid=NULL,worker_identity=NULL,claim_lock=NULL,claim_expires=NULL,"
                "verify_lock=NULL,verify_expires=NULL,verify_pid=NULL,verify_identity=NULL "
                f"WHERE {' AND '.join(clauses)}",
                (now + delay, *values),
            )
            if cur.rowcount == 1:
                _finish_current_run(
                    con, task_id, "deferred", failure_kind=kind,
                    failure_fingerprint=fingerprint,
                    summary=str(reason)[:1000] if reason else None,
                )
                add_event(con, task_id, "cooldown", {
                    "failure_kind": kind, "seconds": delay, "until": now + delay,
                }, generation=generation)
            return cur.rowcount == 1
    return _terminal_transition(
        con, task_id, "failed", verify_token, generation, claim_lock,
        failure_kind=kind, failure_fingerprint=fingerprint,
        summary=str(reason)[:1000] if reason else None,
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
        status_clause = (" AND status IN ('triage','todo','scheduled','ready','running',"
                         "'blocked','review','verifying','finalizing')")
    with _write_txn(con):
        cur = con.execute(
            "UPDATE tasks SET status='stopped', completed_at=?, worker_pid=NULL, "
            "worker_identity=NULL, claim_lock=NULL, claim_expires=NULL, "
            "review_lock=NULL,review_expires=NULL,review_pid=NULL,review_identity=NULL, "
            "verify_lock=NULL, verify_expires=NULL, verify_pid=NULL, verify_identity=NULL "
            f"WHERE {' AND '.join(clauses)}{status_clause}",
            tuple(values),
        )
        if cur.rowcount == 1:
            _finish_current_run(con, task_id, "stopped")
        return cur.rowcount == 1


@_serialized
def stop_and_take_verifier(con, task_id, generation=None, claim_lock=None):
    """Atomically revoke a run and return the verifier process it owned.

    The verifier PID fields deliberately survive this UPDATE so the caller can
    signal the process.  A verifier that attached before the statement is
    returned; one that attaches after it sees ``status='stopped'`` and exits on
    its own.
    """
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
        status_clause = (" AND status IN ('triage','todo','scheduled','ready','running',"
                         "'blocked','review','verifying','finalizing')")
    with _write_txn(con):
        row = con.execute(
            "UPDATE tasks SET status='stopped', completed_at=?, worker_pid=NULL, "
            "worker_identity=NULL, claim_lock=NULL, claim_expires=NULL, "
            "review_lock=NULL,review_expires=NULL,review_pid=NULL,review_identity=NULL, "
            "verify_lock=NULL, verify_expires=NULL "
            f"WHERE {' AND '.join(clauses)}{status_clause} "
            "RETURNING verify_pid, verify_identity",
            tuple(values),
        ).fetchone()
        if row is not None:
            _finish_current_run(con, task_id, "stopped")
        return (row is not None, row["verify_pid"] if row else None,
                row["verify_identity"] if row else None)


@_serialized
def clear_verifier_process(
    con, task_id, pid, identity, generation=None
) -> bool:
    generation_clause = " AND generation=?" if generation is not None else ""
    params = [task_id, pid, identity]
    if generation is not None:
        params.append(generation)
    cur = con.execute(
        "UPDATE tasks SET verify_pid=NULL, verify_identity=NULL "
        "WHERE id=? AND verify_pid IS ? AND verify_identity IS ?"
        + generation_clause,
        tuple(params),
    )
    return cur.rowcount == 1


@_serialized
def submit_task(con, task_id, generation=None, claim_lock=None):
    """Hand finished work to the optional peer review, then to the mandatory red-team gate."""
    clauses = ["id=?"]
    values = [task_id]
    if generation is not None:
        clauses.append("generation=?")
        values.append(generation)
    if claim_lock is not None:
        clauses.extend(["status='running'", "claim_lock=?", "claim_expires>=?"])
        values.extend([claim_lock, int(time.time())])
    with _write_txn(con):
        cur = con.execute(
            "UPDATE tasks SET status=CASE WHEN reviewer IS NULL THEN 'verifying' ELSE 'review' END, "
            "worker_pid=NULL, claim_lock=NULL, "
            "worker_identity=NULL, claim_expires=NULL, verify_lock=NULL, verify_expires=NULL, "
            "verify_pid=NULL, verify_identity=NULL,review_lock=NULL,review_expires=NULL,"
            "review_pid=NULL,review_identity=NULL "
            f"WHERE {' AND '.join(clauses)}",
            tuple(values),
        )
        if cur.rowcount == 1:
            _finish_current_run(con, task_id, "submitted")
        return cur.rowcount == 1


def mark_verifying(con, task_id, generation=None, claim_lock=None):
    """Backwards-compatible alias for ``submit_task``; submission may now stop at the review gate first."""
    return submit_task(con, task_id, generation=generation, claim_lock=claim_lock)


@_serialized
def claim_review(
    con, task_id, reviewer, lock, ttl_seconds=1800, generation=None,
    pid=None, reviewer_identity=None,
) -> bool:
    now = int(time.time())
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
    values = [task_id, lock, now]
    if generation is not None:
        clauses.append("generation=?")
        values.append(generation)
    with _write_txn(con):
        cur = con.execute(
            "UPDATE tasks SET status='verifying',review_feedback=NULL,review_lock=NULL,"
            "review_expires=NULL,review_pid=NULL,review_identity=NULL "
            f"WHERE {' AND '.join(clauses)}",
            tuple(values),
        )
        if cur.rowcount == 1:
            _finish_current_run(con, task_id, "approved", summary=summary)
            reviewer = con.execute(
                "SELECT reviewer FROM tasks WHERE id=?", (task_id,)
            ).fetchone()[0]
            add_event(con, task_id, "review_approved",
                      {"reviewer": reviewer, "summary": summary or ""},
                      generation=generation)
        return cur.rowcount == 1


@_serialized
def request_review_changes(
    con, task_id, lock, feedback, *, generation=None
) -> bool:
    feedback = str(feedback or "").strip()
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
            add_comment(
                con, task_id, f"reviewer:{reviewer_row[0] if reviewer_row else 'unknown'}",
                feedback[:4000], kind="review",
            )
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
        return cur.rowcount == 1


@_serialized
def bump_verify(con, task_id, verify_token=None, generation=None) -> int | None:
    generation_clause = " AND generation=?" if generation is not None else ""
    if verify_token is None:
        params = (task_id, generation) if generation is not None else (task_id,)
        cur = con.execute(
            "UPDATE tasks SET verify_rounds=verify_rounds+1 WHERE id=?"
            + generation_clause,
            params,
        )
    else:
        params = [task_id, verify_token, int(time.time())]
        if generation is not None:
            params.append(generation)
        cur = con.execute(
            "UPDATE tasks SET verify_rounds=verify_rounds+1 "
            "WHERE id=? AND status='verifying' AND verify_lock=? AND verify_expires>=?"
            + generation_clause,
            tuple(params),
        )
    if cur.rowcount != 1:
        return None
    return con.execute("SELECT verify_rounds FROM tasks WHERE id=?", (task_id,)).fetchone()[0]


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
def back_to_ready(
    con, task_id, verify_token=None, generation=None, claim_lock=None
):
    if verify_token is not None and claim_lock is not None:
        raise ValueError("a ready transition can carry only one owner fence")
    clauses = ["id=?"]
    values = [task_id]
    if verify_token is not None:
        clauses.extend([
            "status IN ('verifying','finalizing')",
            "verify_lock=?",
            "verify_expires>=?",
        ])
        values.extend([verify_token, int(time.time())])
    elif claim_lock is not None:
        clauses.extend(["status='running'", "claim_lock=?", "claim_expires>=?"])
        values.extend([claim_lock, int(time.time())])
    if generation is not None:
        clauses.append("generation=?")
        values.append(generation)
    with _write_txn(con):
        cur = con.execute(
            "UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, "
            "worker_identity=NULL,review_lock=NULL,review_expires=NULL,review_pid=NULL,"
            "review_identity=NULL, verify_lock=NULL, verify_expires=NULL, "
            "verify_pid=NULL, verify_identity=NULL "
            f"WHERE {' AND '.join(clauses)}",
            tuple(values),
        )
        if cur.rowcount == 1:
            _finish_current_run(con, task_id, "released")
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
    submitted=False,
) -> bool:
    """Release a running card whose worker died, matching its exact ownership fence.

    Unsubmitted work goes back to ready, or to failed once ``RECLAIM_CAP``
    reclaims have been recorded; submitted work moves on to review/verifying.
    """
    ownership_where = (
        "WHERE id=? AND generation=? AND status='running' AND claim_lock IS ? "
        "AND worker_pid IS ? AND worker_identity IS ? AND claim_expires IS ?"
    )
    ownership = (task_id, generation, claim_lock, worker_pid, worker_identity, claim_expires)
    if not submitted:
        crashes = con.execute(
            "SELECT COUNT(*) FROM events WHERE task_id=? AND kind='reclaimed'",
            (task_id,),
        ).fetchone()[0]
        if crashes >= RECLAIM_CAP:
            with _write_txn(con):
                cur = con.execute(
                    "UPDATE tasks SET status='failed', completed_at=?, claim_lock=NULL, "
                    "claim_expires=NULL, worker_pid=NULL, worker_identity=NULL, "
                    "review_lock=NULL,review_expires=NULL,review_pid=NULL,review_identity=NULL, "
                    "verify_lock=NULL, verify_expires=NULL, verify_pid=NULL, verify_identity=NULL "
                    + ownership_where,
                    (int(time.time()), *ownership),
                )
                if cur.rowcount == 1:
                    _finish_current_run(
                        con, task_id, "failed", failure_kind="crash",
                        failure_fingerprint="reclaim-cap",
                    )
                    add_event(con, task_id, "crash_gave_up",
                              {"reclaimed_times": crashes, "cap": RECLAIM_CAP},
                              generation=generation)
                return False
    reviewer = con.execute("SELECT reviewer FROM tasks WHERE id=?", (task_id,)).fetchone()
    status = ("review" if reviewer and reviewer["reviewer"] else "verifying") if submitted else "ready"
    with _write_txn(con):
        cur = con.execute(
            f"UPDATE tasks SET status='{status}', claim_lock=NULL, claim_expires=NULL, "
            "worker_pid=NULL, worker_identity=NULL,review_lock=NULL,review_expires=NULL,"
            "review_pid=NULL,review_identity=NULL, verify_lock=NULL, verify_expires=NULL, "
            "verify_pid=NULL, verify_identity=NULL " + ownership_where,
            ownership,
        )
        if cur.rowcount == 1:
            _finish_current_run(
                con, task_id, "submitted" if submitted else "reclaimed",
                failure_kind=None if submitted else "stale_reclaim",
            )
        return cur.rowcount == 1


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
            "UPDATE tasks SET status='running',verify_rounds=0,review_rounds=0,"
            "review_feedback=NULL,completed_at=NULL, "
            "claim_lock=?, claim_expires=?, worker_pid=?, worker_identity=?, "
            "review_lock=NULL,review_expires=NULL,review_pid=NULL,review_identity=NULL,"
            "verify_lock=NULL, verify_expires=NULL, verify_pid=NULL, verify_identity=NULL, "
            "block_kind=NULL,block_reason=NULL,blocked_at=NULL,heartbeat_at=?,next_attempt_at=NULL, "
            "generation=generation+1 WHERE id=? "
            "AND status IN ('done','failed','stopped','blocked','triage')"
            + generation_clause + " RETURNING generation",
            tuple([lock, now + ttl_seconds, pid, worker_identity, now, task_id]
                  + ([expected_generation] if expected_generation is not None else [])),
        ).fetchone()
        if row is None:
            return False
        _start_run(con, task_id, int(row["generation"]), "worker", lock, pid, worker_identity)
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
            target_status = "todo" if parent_ids(con, task_id) else "ready"
        if target_status not in {"todo", "ready"}:
            raise ValueError("Only todo or ready tasks can be reset.")
        generation = int(row["generation"]) + 1
        cur = con.execute(
            "UPDATE tasks SET status=?,generation=?,started_at=NULL,completed_at=NULL,"
            "agent_id=NULL,session_file=NULL,claim_lock=NULL,claim_expires=NULL,"
            "worker_pid=NULL,worker_identity=NULL,verify_rounds=0,review_rounds=0,"
            "review_feedback=NULL,review_lock=NULL,review_expires=NULL,review_pid=NULL,"
            "review_identity=NULL,verify_lock=NULL,"
            "verify_expires=NULL,verify_pid=NULL,verify_identity=NULL,block_kind=NULL,"
            "block_reason=NULL,blocked_at=NULL,heartbeat_at=NULL,next_attempt_at=NULL "
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
) -> str | None:
    """Record a block; the same cause reported twice in a row routes the card to triage."""
    kind = str(kind or "").strip()
    reason = str(reason or "").strip()
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
        add_event(
            con, task_id, status,
            {"kind": kind, "reason": reason, "fingerprint": fingerprint,
             "recurrence": recurrence},
            generation=generation,
        )
        return status


@_serialized
def unblock_task(con, task_id) -> bool:
    row = get(con, task_id)
    if row is None or row["status"] not in {"blocked", "triage"}:
        return False
    target = "todo" if parent_ids(con, task_id) else "ready"
    cur = con.execute(
        "UPDATE tasks SET status=?,block_kind=NULL,block_reason=NULL,blocked_at=NULL "
        "WHERE id=? AND status IN ('blocked','triage')",
        (target, task_id),
    )
    if cur.rowcount == 1:
        add_event(con, task_id, "unblocked", {"target": target})
        if target == "todo":
            promote_task(con, task_id)
    return cur.rowcount == 1


@_serialized
def block_abandoned(
    con,
    task_id,
    kind,
    reason,
    *,
    generation,
    claim_lock,
    worker_pid,
    worker_identity,
    claim_expires,
) -> str | None:
    """Apply a blocked report recovered from a dead worker, matching its exact ownership fence."""
    if kind not in BLOCK_KINDS or not str(reason or "").strip():
        raise ValueError("invalid abandoned block")
    reason = str(reason).strip()
    fingerprint = hashlib.sha256(f"{kind}\0{reason}".encode()).hexdigest()[:16]
    row = get(con, task_id)
    if row is None:
        return None
    recurrence = int(row["block_recurrence"] or 0) + 1 \
        if row["block_fingerprint"] == fingerprint else 1
    status = "triage" if recurrence >= 2 else "blocked"
    with _write_txn(con):
        cur = con.execute(
            "UPDATE tasks SET status=?,block_kind=?,block_reason=?,block_fingerprint=?,"
            "block_recurrence=?,blocked_at=?,claim_lock=NULL,claim_expires=NULL,"
            "worker_pid=NULL,worker_identity=NULL,review_lock=NULL,review_expires=NULL,"
            "review_pid=NULL,review_identity=NULL WHERE id=? AND generation=? "
            "AND status='running' AND claim_lock IS ? AND worker_pid IS ? "
            "AND worker_identity IS ? AND claim_expires IS ?",
            (status, kind, reason, fingerprint, recurrence, int(time.time()), task_id,
             generation, claim_lock, worker_pid, worker_identity, claim_expires),
        )
        if cur.rowcount != 1:
            return None
        _finish_current_run(
            con, task_id, status, failure_kind=f"blocked:{kind}",
            failure_fingerprint=fingerprint, summary=reason,
        )
        add_event(
            con, task_id, status,
            {"kind": kind, "reason": reason, "fingerprint": fingerprint,
             "recurrence": recurrence, "reconciled": True},
            generation=generation,
        )
        return status


@_serialized
def claim_verification(con, task_id, lock, ttl_seconds=4500, generation=None) -> bool:
    """Acquire the cross-process verifier lease for a card in verifying/finalizing."""
    now = int(time.time())
    generation_clause = " AND generation=?" if generation is not None else ""
    params = [lock, now + ttl_seconds, task_id, now, lock]
    if generation is not None:
        params.append(generation)
    with _write_txn(con):
        current = con.execute(
            "SELECT t.generation,r.phase,r.status,r.claim_lock FROM tasks t "
            "LEFT JOIN task_runs r ON r.id=t.current_run_id WHERE t.id=?",
            (task_id,),
        ).fetchone()
        if (current and current["phase"] == "verifier" and current["status"] == "running"
                and current["claim_lock"] == lock):
            return True
        cur = con.execute(
            "UPDATE tasks SET verify_lock=?, verify_expires=?, verify_pid=NULL, verify_identity=NULL "
            "WHERE id=? AND status IN ('verifying','finalizing') "
            "AND (verify_lock IS NULL OR verify_expires IS NULL OR verify_expires<? OR verify_lock=?)"
            + generation_clause,
            tuple(params),
        )
        if cur.rowcount != 1:
            return False
        actual_generation = int(current["generation"] if current else generation)
        if current and current["phase"] == "verifier" and current["status"] == "running":
            _finish_current_run(
                con, task_id, "reclaimed", failure_kind="stale_reclaim"
            )
        _start_run(con, task_id, actual_generation, "verifier", lock)
        return True


@_serialized
def owns_verification(con, task_id, lock, generation=None) -> bool:
    generation_clause = " AND generation=?" if generation is not None else ""
    params = [task_id, lock, int(time.time())]
    if generation is not None:
        params.append(generation)
    row = con.execute(
        "SELECT 1 FROM tasks WHERE id=? AND status IN ('verifying','finalizing') "
        "AND verify_lock=? "
        "AND verify_expires>=?" + generation_clause,
        tuple(params),
    ).fetchone()
    return row is not None


@_serialized
def heartbeat(con, task_id, lock, *, generation=None, ttl_seconds=1800) -> bool:
    """Renew whichever lease (worker, reviewer, or verifier) ``lock`` holds and stamp the heartbeat."""
    now = int(time.time())
    generation_clause = " AND generation=?" if generation is not None else ""
    suffix = [generation] if generation is not None else []
    cur = con.execute(
        "UPDATE tasks SET claim_expires=?,heartbeat_at=? WHERE id=? AND status='running' "
        "AND claim_lock=?" + generation_clause,
        (now + max(60, int(ttl_seconds)), now, task_id, lock, *suffix),
    )
    if cur.rowcount != 1:
        cur = con.execute(
            "UPDATE tasks SET review_expires=?,heartbeat_at=? WHERE id=? "
            "AND status='review' AND review_lock=?" + generation_clause,
            (now + max(60, int(ttl_seconds)), now, task_id, lock, *suffix),
        )
    if cur.rowcount != 1:
        cur = con.execute(
            "UPDATE tasks SET verify_expires=?,heartbeat_at=? WHERE id=? "
            "AND status IN ('verifying','finalizing') AND verify_lock=?" + generation_clause,
            (now + max(60, int(ttl_seconds)), now, task_id, lock, *suffix),
        )
    if cur.rowcount == 1:
        con.execute(
            "UPDATE task_runs SET heartbeat_at=? "
            "WHERE id=(SELECT current_run_id FROM tasks WHERE id=?) AND status='running'",
            (now, task_id),
        )
    return cur.rowcount == 1


@_serialized
def set_verifier_process(
    con, task_id, lock, pid, identity, generation=None
) -> bool:
    generation_clause = " AND generation=?" if generation is not None else ""
    params = [pid, identity, task_id, lock, int(time.time())]
    if generation is not None:
        params.append(generation)
    cur = con.execute(
        "UPDATE tasks SET verify_pid=?, verify_identity=? "
        "WHERE id=? AND status IN ('verifying','finalizing') "
        "AND verify_lock=? AND verify_expires>=?"
        + generation_clause,
        tuple(params),
    )
    if cur.rowcount == 1:
        con.execute(
            "UPDATE task_runs SET pid=?,process_identity=? "
            "WHERE id=(SELECT current_run_id FROM tasks WHERE id=?)",
            (pid, identity, task_id),
        )
    return cur.rowcount == 1


@_serialized
def begin_finalize(con, task_id, lock, generation=None) -> bool:
    generation_clause = " AND generation=?" if generation is not None else ""
    params = [task_id, lock, int(time.time())]
    if generation is not None:
        params.append(generation)
    cur = con.execute(
        "UPDATE tasks SET status='finalizing' "
        "WHERE id=? AND status='verifying' AND verify_lock=? AND verify_expires>=?"
        + generation_clause,
        tuple(params),
    )
    return cur.rowcount == 1


@_serialized
def finish_finalize(con, task_id, lock, generation=None) -> bool:
    now = int(time.time())
    generation_clause = " AND generation=?" if generation is not None else ""
    params = [now, task_id, lock, now]
    if generation is not None:
        params.append(generation)
    with _write_txn(con):
        cur = con.execute(
            "UPDATE tasks SET status='done', completed_at=?, worker_pid=NULL, "
            "worker_identity=NULL, claim_lock=NULL, claim_expires=NULL, "
            "review_lock=NULL,review_expires=NULL,review_pid=NULL,review_identity=NULL, "
            "verify_lock=NULL, verify_expires=NULL, verify_pid=NULL, verify_identity=NULL "
            "WHERE id=? AND status='finalizing' AND verify_lock=? AND verify_expires>=?"
            + generation_clause,
            tuple(params),
        )
        if cur.rowcount == 1:
            _finish_current_run(con, task_id, "done")
            con.execute(
                "UPDATE tasks SET block_kind=NULL,block_reason=NULL,block_fingerprint=NULL,"
                "block_recurrence=0,blocked_at=NULL WHERE id=?",
                (task_id,),
            )
            promote_dependents(con, task_id)
        return cur.rowcount == 1


@_serialized
def release_verification(con, task_id, lock, generation=None) -> bool:
    generation_clause = " AND generation=?" if generation is not None else ""
    params = [task_id, lock]
    if generation is not None:
        params.append(generation)
    with _write_txn(con):
        cur = con.execute(
            "UPDATE tasks SET verify_lock=NULL, verify_expires=NULL, "
            "verify_pid=NULL, verify_identity=NULL "
            "WHERE id=? AND status IN ('verifying','finalizing') AND verify_lock=?"
            + generation_clause,
            tuple(params),
        )
        if cur.rowcount == 1:
            _finish_current_run(con, task_id, "released")
        return cur.rowcount == 1


def pending_completions(con):
    """Return finished cards whose current generation has not been reported yet."""
    return con.execute(
        "SELECT * FROM tasks WHERE status IN ('done','failed','stopped','blocked','triage') "
        "AND notified_generation<generation ORDER BY completed_at"
    ).fetchall()


@_serialized
def claim_notification(con, task_id, generation=None) -> bool:
    """Claim the single completion notification for this card generation."""
    generation_clause = " AND generation=?" if generation is not None else ""
    params = (task_id, generation) if generation is not None else (task_id,)
    cur = con.execute(
        "UPDATE tasks SET notified_generation=generation WHERE id=? "
        "AND status IN ('done','failed','stopped','blocked','triage') "
        "AND notified_generation<generation"
        + generation_clause,
        params,
    )
    return cur.rowcount == 1


@_serialized
def release_notification(con, task_id, *, generation) -> bool:
    """Give back a claimed notification so another session can deliver it."""
    cur = con.execute(
        "UPDATE tasks SET notified_generation=generation-1 WHERE id=? "
        "AND generation=? AND notified_generation=generation",
        (task_id, generation),
    )
    return cur.rowcount == 1

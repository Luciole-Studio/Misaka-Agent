"""LCM status, diagnostics, and backup operations."""
import sqlite3
import time
from pathlib import Path

from misaka.extensions.hermes_lcm.migrations import LATEST_SCHEMA_VERSION, migrate


def _connect_ro(db_path):
    return sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True, timeout=5.0)


def status(db_path):
    """Return database-wide LCM statistics without creating the database."""
    path = Path(db_path)
    out = {"db": str(path), "exists": path.is_file(),
           "size_bytes": path.stat().st_size if path.is_file() else 0,
           "sessions": 0, "messages": 0, "nodes": 0, "pending": 0,
           "schema_version": 0, "per_session": {}}
    if not out["exists"]:
        return out
    con = _connect_ro(path)
    try:
        out["messages"] = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        out["nodes"] = con.execute(
            "SELECT COUNT(*) FROM summary_nodes WHERE committed=1").fetchone()[0]
        try:
            out["pending"] = con.execute("SELECT COUNT(*) FROM compaction_attempts").fetchone()[0]
            row = con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            out["schema_version"] = int(row[0] or 0)
        except sqlite3.Error:
            pass
        rows = con.execute(
            """SELECT m.session_id, COUNT(*),
                      (SELECT COUNT(*) FROM summary_nodes n
                       WHERE n.session_id = m.session_id AND n.committed=1)
               FROM messages m GROUP BY m.session_id ORDER BY m.session_id"""
        ).fetchall()
        out["per_session"] = {r[0]: {"messages": r[1], "nodes": r[2]} for r in rows}
        out["sessions"] = len(out["per_session"])
    finally:
        con.close()
    return out


def doctor(db_path):
    """Run non-destructive LCM database diagnostics."""
    path = Path(db_path)
    checks = []

    def add(check, ok_status, detail, action="safe/ignore"):
        checks.append({"check": check, "status": ok_status,
                       "detail": detail, "action": action})

    if not path.is_file():
        add("database_exists", "pass", "The database does not exist yet; it is created on first compaction.")
        return checks
    try:
        con = _connect_ro(path)
    except sqlite3.Error as exc:
        add("database_open", "fail", f"Database could not be opened: {exc}", "backup-first repair")
        return checks
    try:
        verdict = con.execute("PRAGMA integrity_check").fetchone()[0]
        if verdict == "ok":
            add("database_integrity", "pass", "integrity_check ok")
        else:
            add("database_integrity", "fail", str(verdict)[:200],
                "backup-first repair (run `misaka lcm backup` first)")
    except sqlite3.Error as exc:
        add("database_integrity", "warn",
            f"Integrity check was unavailable ({exc}); this alone does not prove damage.", "safe/ignore")

    try:
        version = int(con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0)
        if version == LATEST_SCHEMA_VERSION:
            add("schema_version", "pass", f"v{version}")
        else:
            add("schema_version", "warn", f"v{version}; current version is v{LATEST_SCHEMA_VERSION}",
                "backup-first repair (run `misaka lcm repair`)")
    except sqlite3.Error:
        add("schema_version", "warn", "Migration history is unavailable for this database.",
            "backup-first repair (run `misaka lcm repair`)")

    for content, fts in (("messages", "messages_fts"), ("summary_nodes", "nodes_fts")):
        try:
            n_content = con.execute(f"SELECT COUNT(*) FROM {content}").fetchone()[0]
            n_fts = con.execute(f"SELECT COUNT(*) FROM {fts}").fetchone()[0]
            if n_content == n_fts:
                add(f"{fts}_sync", "pass", f"{n_content} rows synchronized")
            else:
                add(f"{fts}_sync", "warn",
                    f"{content}={n_content} vs {fts}={n_fts}; search results may be incomplete",
                    "inspect and rebuild the FTS table")
        except sqlite3.Error as exc:
            add(f"{fts}_sync", "warn",
                f"Synchronization check was unavailable ({exc}); this alone does not prove damage.",
                "safe/ignore")

    try:
        orphan = con.execute(
            """SELECT COUNT(*) FROM summary_nodes n, json_each(n.source_ids) j
               WHERE n.source_type='messages'
               AND CAST(j.value AS INTEGER) NOT IN (SELECT store_id FROM messages)""").fetchone()[0]
        if orphan:
            add("node_lineage", "warn",
                f"{orphan} summary source reference(s) point to missing messages; summaries remain readable",
                "inspect")
        else:
            add("node_lineage", "pass", "All summary source references resolve.")
    except sqlite3.Error as exc:
        add("node_lineage", "warn", f"Lineage check was unavailable ({exc}); this alone does not prove damage.",
            "safe/ignore")
    try:
        pending = con.execute("SELECT COUNT(*) FROM compaction_attempts").fetchone()[0]
        orphan_pending = con.execute(
            """SELECT COUNT(*) FROM summary_nodes n WHERE n.committed=0
               AND n.attempt_id NOT IN (SELECT attempt_id FROM compaction_attempts)"""
        ).fetchone()[0]
        if pending or orphan_pending:
            add("pending_attempts", "warn",
                f"{pending} pending attempt(s), {orphan_pending} orphan pending node(s)",
                "inspect; resume the session or run `misaka lcm repair`")
        else:
            add("pending_attempts", "pass", "No pending compaction attempts.")
    except sqlite3.Error as exc:
        add("pending_attempts", "warn", f"Pending-attempt check was unavailable ({exc}); this alone does not prove damage.",
            "safe/ignore")
    con.close()
    return checks


def backup(db_path, dest_dir=None):
    """Create a timestamped snapshot with SQLite's online backup API."""
    path = Path(db_path)
    if not path.is_file():
        return None, "The LCM database does not exist yet; there is nothing to back up."
    dest_root = Path(dest_dir) if dest_dir else path.parent / "backups" / "lcm"
    dest_root.mkdir(parents=True, exist_ok=True)
    stamp = f"{time.strftime('%Y%m%d_%H%M%S')}_{time.time_ns() % 1_000_000_000:09d}"
    dest = dest_root / f"{path.stem}-{stamp}.sqlite3"
    src = sqlite3.connect(str(path), timeout=5.0)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    except sqlite3.Error as exc:
        return None, f"Backup failed: {exc}"
    finally:
        src.close()
    return str(dest), None


def repair(db_path):
    """Back up, then migrate the schema, rebuild FTS indexes, and delete orphaned pending nodes."""
    path = Path(db_path)
    if not path.is_file():
        con = sqlite3.connect(str(path), timeout=5.0)
        try:
            migrate(con)
        finally:
            con.close()
        return {"backup": None, "messages_fts": 0, "nodes_fts": 0,
                "orphan_pending_deleted": 0}
    snapshot, error = backup(path)
    if error:
        raise RuntimeError(error)
    con = sqlite3.connect(str(path), timeout=5.0)
    try:
        migrate(con)
        with con:
            con.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
            con.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')")
            cur = con.execute(
                "DELETE FROM summary_nodes WHERE committed=0 AND attempt_id NOT IN "
                "(SELECT attempt_id FROM compaction_attempts)"
            )
            deleted = cur.rowcount or 0
        messages = con.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
        nodes = con.execute("SELECT COUNT(*) FROM nodes_fts").fetchone()[0]
    finally:
        con.close()
    return {"backup": snapshot, "messages_fts": messages, "nodes_fts": nodes,
            "orphan_pending_deleted": deleted}


def rebuild_from_session_file(db_path, session_file):
    """Rebuild one session's raw store and latest compaction lineage from JSONL."""
    from misaka.core.session_manager import load_entries_from_file
    from misaka.extensions.hermes_lcm.dag import SummaryDAG, SummaryNode
    from misaka.extensions.hermes_lcm.store import MessageStore
    from misaka.extensions.hermes_lcm.tokens import count_tokens

    snapshot = None
    if Path(db_path).is_file():
        snapshot, error = backup(db_path)
        if error:
            raise RuntimeError(error)
    entries = load_entries_from_file(str(session_file))
    header = next((entry for entry in entries if entry.get("type") == "session"), None)
    if not header or not header.get("id"):
        raise ValueError("session file has no valid header")
    session_id = str(header["id"])
    source = str(Path(session_file).resolve())
    branch_entries = [entry for entry in entries if entry.get("type") != "session"]
    by_id = {entry.get("id"): entry for entry in branch_entries if entry.get("id")}
    leaf = next((entry for entry in reversed(branch_entries) if entry.get("id")), None)
    branch = []
    while leaf:
        branch.insert(0, leaf)
        leaf = by_id.get(leaf.get("parentId"))

    # Reuse the adapter's exact normalization without inventing another message shape.
    from misaka.extensions.hermes_lcm.extension import _entry_message
    messages, host_ids = [], []
    for entry in branch_entries:
        msg = _entry_message(entry)
        if msg is not None and entry.get("id"):
            messages.append(msg)
            host_ids.append(str(entry["id"]))

    store, dag = MessageStore(db_path), SummaryDAG(db_path)
    try:
        dag.delete_session(session_id)
        store.delete_session(session_id)
        ids = store.append_batch(session_id, messages, source=source,
                                 host_entry_ids=host_ids)
        mapping = dict(zip(host_ids, ids))
        latest = next((entry for entry in reversed(branch)
                       if entry.get("type") == "compaction"), None)
        if latest and latest.get("summary"):
            first_kept = latest.get("firstKeptEntryId")
            first_index = next((i for i, entry in enumerate(branch)
                                if entry.get("id") == first_kept), len(branch))
            source_ids = [mapping[str(entry["id"])] for entry in branch[:first_index]
                          if str(entry.get("id")) in mapping]
            earliest, latest_at = store.get_time_bounds(source_ids)
            dag.add_node(SummaryNode(
                session_id=session_id, depth=0, summary=str(latest["summary"]),
                token_count=count_tokens(str(latest["summary"])),
                source_ids=source_ids, source_type="messages",
                earliest_at=earliest, latest_at=latest_at,
                expand_hint="Most recent compaction checkpoint rebuilt from the transcript.",
                host_compaction_id=latest.get("id")))
        return {"session_id": session_id, "messages": len(ids),
                "nodes": len(dag.get_session_nodes(session_id)), "backup": snapshot}
    finally:
        dag.close()
        store.close()

"""Directed acyclic graph helpers for hierarchical LCM summaries."""
import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from misaka.extensions.lcm.migrations import migrate

_NODE_COLUMNS = ("node_id, session_id, depth, summary, token_count, "
                 "source_token_count, source_ids, source_type, created_at, "
                 "earliest_at, latest_at, expand_hint, attempt_id, committed, "
                 "host_compaction_id")


@dataclass
class SummaryNode:
    node_id: int = 0
    session_id: str = ""
    depth: int = 0
    summary: str = ""
    token_count: int = 0
    source_token_count: int = 0
    source_ids: list = field(default_factory=list)
    source_type: str = "messages"      # "messages" | "nodes"
    created_at: float = 0.0
    earliest_at: float | None = None
    latest_at: float | None = None
    expand_hint: str = ""
    attempt_id: str | None = None
    committed: bool = True
    host_compaction_id: str | None = None


class SummaryDAG:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), timeout=5.0,
                                     check_same_thread=False)
        migrate(self._conn)

    def add_node(self, node, *, attempt_id=None):
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT INTO summary_nodes
                   (session_id, depth, summary, token_count, source_token_count,
                    source_ids, source_type, created_at, earliest_at, latest_at,
                    expand_hint, attempt_id, committed, host_compaction_id)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (node.session_id, node.depth, node.summary, node.token_count,
                 node.source_token_count, json.dumps(node.source_ids),
                 node.source_type, node.created_at or time.time(),
                 node.earliest_at, node.latest_at, node.expand_hint,
                 attempt_id or node.attempt_id,
                 int(not (attempt_id or node.attempt_id)),
                 node.host_compaction_id))
            node.node_id = cur.lastrowid
            node.attempt_id = attempt_id or node.attempt_id
            node.committed = not bool(node.attempt_id)
            return node.node_id

    def get_node(self, node_id):
        row = self._conn.execute(
            f"SELECT {_NODE_COLUMNS} FROM summary_nodes WHERE node_id=? AND committed=1",
            (node_id,)).fetchone()
        return self._row_to_node(row) if row else None

    def get_session_nodes(self, session_id, depth=None, limit=1000, *, attempt_id=None):
        visible = "(committed=1 OR attempt_id=?)" if attempt_id else "committed=1"
        args = [session_id]
        if attempt_id:
            args.append(attempt_id)
        if depth is not None:
            args.extend([depth, limit])
            rows = self._conn.execute(
                f"SELECT {_NODE_COLUMNS} FROM summary_nodes WHERE session_id=? "
                f"AND {visible} AND depth=? ORDER BY created_at LIMIT ?", args).fetchall()
        else:
            args.append(limit)
            rows = self._conn.execute(
                f"SELECT {_NODE_COLUMNS} FROM summary_nodes WHERE session_id=? "
                f"AND {visible} ORDER BY depth, created_at LIMIT ?", args).fetchall()
        return [self._row_to_node(r) for r in rows]

    def count_at_depth(self, session_id, depth):
        return self._conn.execute(
            "SELECT COUNT(*) FROM summary_nodes WHERE session_id=? AND depth=? AND committed=1",
            (session_id, depth)).fetchone()[0]

    def get_uncondensed_at_depth(self, session_id, depth, limit=100, *, attempt_id=None):
        """Return nodes at a depth that have not been condensed into a parent."""
        visible_n = "(n.committed=1 OR n.attempt_id=?)" if attempt_id else "n.committed=1"
        visible_p = "(p.committed=1 OR p.attempt_id=?)" if attempt_id else "p.committed=1"
        args = [session_id]
        if attempt_id:
            args.append(attempt_id)
        args.extend([depth, session_id])
        if attempt_id:
            args.append(attempt_id)
        args.extend([depth, limit])
        rows = self._conn.execute(
            f"""SELECT {', '.join('n.' + c.strip() for c in _NODE_COLUMNS.split(','))}
               FROM summary_nodes n WHERE n.session_id=? AND {visible_n} AND n.depth=?
               AND n.node_id NOT IN (
                   SELECT json_each.value FROM summary_nodes p, json_each(p.source_ids)
                   WHERE p.session_id=? AND {visible_p} AND p.depth>?
                     AND p.source_type='nodes')
               ORDER BY n.created_at LIMIT ?""", args).fetchall()
        return [self._row_to_node(r) for r in rows]

    def frontier_nodes(self, session_id, *, attempt_id=None):
        """Return the uncondensed nodes at every depth, deepest first (the visible summary frontier)."""
        depths = sorted({n.depth for n in self.get_session_nodes(
            session_id, attempt_id=attempt_id)},
                        reverse=True)
        out = []
        for d in depths:
            out.extend(self.get_uncondensed_at_depth(
                session_id, d, attempt_id=attempt_id))
        return out

    def source_message_ids(self, node_id, *, limit, offset=0):
        """Return distinct source-message IDs beneath a summary node."""
        if limit <= 0:
            return []
        rows = self._conn.execute(
            """WITH RECURSIVE source_walk(source_type, source_id) AS (
                   SELECT n.source_type, CAST(j.value AS INTEGER)
                   FROM summary_nodes n, json_each(n.source_ids) j
                   WHERE n.node_id = ?
                   UNION
                   SELECT child.source_type, CAST(j.value AS INTEGER)
                   FROM summary_nodes child
                   JOIN source_walk walk
                     ON walk.source_type = 'nodes' AND child.node_id = walk.source_id
                   JOIN json_each(child.source_ids) j)
               SELECT DISTINCT m.store_id FROM source_walk walk
               JOIN messages m
                 ON walk.source_type = 'messages' AND m.store_id = walk.source_id
               ORDER BY m.store_id LIMIT ? OFFSET ?""",
            (node_id, limit, max(0, offset))).fetchall()
        return [int(r[0]) for r in rows]

    def source_message_count(self, node_id):
        row = self._conn.execute(
            """WITH RECURSIVE source_walk(source_type, source_id) AS (
                   SELECT n.source_type, CAST(j.value AS INTEGER)
                   FROM summary_nodes n, json_each(n.source_ids) j
                   WHERE n.node_id = ? AND n.committed=1
                   UNION
                   SELECT child.source_type, CAST(j.value AS INTEGER)
                   FROM summary_nodes child
                   JOIN source_walk walk
                     ON walk.source_type='nodes' AND child.node_id=walk.source_id
                   JOIN json_each(child.source_ids) j)
               SELECT COUNT(DISTINCT m.store_id) FROM source_walk walk
               JOIN messages m
                 ON walk.source_type='messages' AND m.store_id=walk.source_id""",
            (node_id,),
        ).fetchone()
        return int(row[0] or 0)

    def search(self, query, session_id=None, limit=10, *, time_from=None, time_to=None):
        """Search summary nodes with FTS5 and a Unicode-safe LIKE fallback."""
        from misaka.extensions.lcm.search_query import (
            escape_like,
            extract_search_terms,
            requires_like_fallback,
            sanitize_fts5_query,
            sanitize_like_query,
        )
        safe = sanitize_fts5_query(query)
        if not requires_like_fallback(query, safe):
            where, args = ["nodes_fts MATCH ?", "n.committed=1"], [safe]
            if session_id is not None:
                where.append("n.session_id=?")
                args.append(session_id)
            if time_from is not None:
                where.append("COALESCE(n.latest_at,n.created_at)>=?")
                args.append(time_from)
            if time_to is not None:
                where.append("COALESCE(n.earliest_at,n.created_at)<=?")
                args.append(time_to)
            args.append(limit)
            try:
                rows = self._conn.execute(
                        f"""SELECT {', '.join('n.' + c.strip() for c in _NODE_COLUMNS.split(','))}
                        FROM nodes_fts fts
                        JOIN summary_nodes n ON n.node_id = fts.rowid
                        WHERE {' AND '.join(where)} ORDER BY rank LIMIT ?""",
                    args).fetchall()
                return [self._row_to_node(r) for r in rows]
            except sqlite3.Error:
                pass
        terms = extract_search_terms(sanitize_like_query(query))
        if not terms:
            return []
        where, args = ["committed=1"], []
        if session_id is not None:
            where.append("session_id=?")
            args.append(session_id)
        if time_from is not None:
            where.append("COALESCE(latest_at,created_at)>=?")
            args.append(time_from)
        if time_to is not None:
            where.append("COALESCE(earliest_at,created_at)<=?")
            args.append(time_to)
        where.append("(" + " OR ".join(["summary LIKE ? ESCAPE '\\'"] * len(terms)) + ")")
        args.extend(f"%{escape_like(t)}%" for t in terms)
        args.append(limit)
        rows = self._conn.execute(
            f"""SELECT {_NODE_COLUMNS} FROM summary_nodes WHERE {' AND '.join(where)}
                ORDER BY node_id DESC LIMIT ?""", args).fetchall()
        return [self._row_to_node(r) for r in rows]

    def describe_subtree(self, node_id, *, offset=0, limit=50):
        node = self.get_node(node_id)
        if not node:
            return {"error": f"Node {node_id} does not exist."}
        children = []
        if node.source_type == "nodes" and node.source_ids:
            ordered_ids = node.source_ids[max(0, offset):max(0, offset) + limit]
            if not ordered_ids:
                rows = []
            else:
                marks = ",".join("?" * len(ordered_ids))
                fetched = self._conn.execute(
                    f"SELECT {_NODE_COLUMNS} FROM summary_nodes WHERE committed=1 "
                    f"AND node_id IN ({marks})", ordered_ids).fetchall()
                by_id = {int(r[0]): r for r in fetched}
                rows = [by_id[i] for i in ordered_ids if i in by_id]
            for r in rows:
                c = self._row_to_node(r)
                children.append({"node_id": c.node_id, "depth": c.depth,
                                 "token_count": c.token_count,
                                 "expand_hint": c.expand_hint})
        return {"node_id": node.node_id, "depth": node.depth,
                "token_count": node.token_count,
                "source_token_count": node.source_token_count,
                "source_type": node.source_type,
                "num_sources": len(node.source_ids),
                "source_offset": max(0, offset),
                "next_offset": (max(0, offset) + len(children)
                                if max(0, offset) + len(children) < len(node.source_ids)
                                and node.source_type == "nodes" else None),
                "earliest_at": node.earliest_at, "latest_at": node.latest_at,
                "expand_hint": node.expand_hint, "children": children}

    def get_session_depth_stats(self, session_id):
        rows = self._conn.execute(
            """SELECT depth, COUNT(*), COALESCE(SUM(token_count), 0),
                      COALESCE(SUM(source_token_count), 0)
               FROM summary_nodes WHERE session_id=? AND committed=1
               GROUP BY depth ORDER BY depth""",
            (session_id,)).fetchall()
        return {int(r[0]): {"count": int(r[1]), "tokens": int(r[2]),
                            "source_tokens": int(r[3])} for r in rows}

    def reassign_session_nodes(self, old_session_id, new_session_id):
        """Move summary nodes to a new session while leaving raw messages intact."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE summary_nodes SET session_id=? WHERE session_id=?",
                (new_session_id, old_session_id))
            return cur.rowcount or 0

    def delete_below_depth(self, session_id, min_depth):
        """Delete nodes below a minimum depth, or all nodes when depth is None."""
        with self._lock, self._conn:
            if min_depth is None:
                cur = self._conn.execute(
                    "DELETE FROM summary_nodes WHERE session_id=?", (session_id,))
            else:
                cur = self._conn.execute(
                    "DELETE FROM summary_nodes WHERE session_id=? AND depth<?",
                    (session_id, min_depth))
            return cur.rowcount or 0

    def delete_session(self, session_id):
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM compaction_attempts WHERE session_id=?", (session_id,))
            cur = self._conn.execute("DELETE FROM summary_nodes WHERE session_id=?", (session_id,))
            return cur.rowcount or 0

    def stage_attempt(self, attempt_id, session_id, result_summary, first_kept_entry_id):
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO compaction_attempts"
                "(attempt_id,session_id,result_summary,first_kept_entry_id,created_at) "
                "VALUES(?,?,?,?,?)",
                (attempt_id, session_id, result_summary, first_kept_entry_id, time.time()),
            )

    def pending_attempts(self, session_id=None):
        where, args = (" WHERE session_id=?", [session_id]) if session_id else ("", [])
        rows = self._conn.execute(
            "SELECT attempt_id,session_id,result_summary,first_kept_entry_id,created_at "
            f"FROM compaction_attempts{where} ORDER BY created_at", args
        ).fetchall()
        return [dict(zip(("attempt_id", "session_id", "result_summary",
                         "first_kept_entry_id", "created_at"), row)) for row in rows]

    def commit_attempt(self, attempt_id, host_compaction_id=None):
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE summary_nodes SET committed=1, host_compaction_id=? "
                "WHERE attempt_id=? AND committed=0",
                (host_compaction_id, attempt_id),
            )
            self._conn.execute("DELETE FROM compaction_attempts WHERE attempt_id=?", (attempt_id,))
            return cur.rowcount or 0

    def discard_attempt(self, attempt_id):
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM summary_nodes WHERE attempt_id=? AND committed=0", (attempt_id,))
            self._conn.execute("DELETE FROM compaction_attempts WHERE attempt_id=?", (attempt_id,))
            return cur.rowcount or 0

    def reconcile_attempts(self, session_id, compaction_entries):
        """Commit attempts already present in JSONL; discard every other stale attempt."""
        committed = discarded = 0
        with self._lock, self._conn:
            orphan = self._conn.execute(
                "DELETE FROM summary_nodes WHERE session_id=? AND committed=0 "
                "AND attempt_id NOT IN (SELECT attempt_id FROM compaction_attempts)",
                (session_id,),
            )
            discarded += orphan.rowcount or 0
        for attempt in self.pending_attempts(session_id):
            match = next((entry for entry in compaction_entries
                          if entry.get("type") == "compaction"
                          and (entry.get("fromHook") or entry.get("fromExtension"))
                          and entry.get("summary") == attempt["result_summary"]
                          and entry.get("firstKeptEntryId") == attempt["first_kept_entry_id"]), None)
            if match:
                committed += self.commit_attempt(attempt["attempt_id"], match.get("id"))
            else:
                discarded += self.discard_attempt(attempt["attempt_id"])
        return {"committed": committed, "discarded": discarded}

    @staticmethod
    def _row_to_node(row):
        return SummaryNode(
            node_id=row[0], session_id=row[1], depth=row[2], summary=row[3],
            token_count=row[4], source_token_count=row[5],
            source_ids=json.loads(row[6]) if row[6] else [],
            source_type=row[7], created_at=row[8], earliest_at=row[9],
            latest_at=row[10], expand_hint=row[11] or "", attempt_id=row[12],
            committed=bool(row[13]), host_compaction_id=row[14])

    def close(self):
        if self._conn is not None:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error:
                pass
            self._conn.close()
            self._conn = None

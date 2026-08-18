"""摘要 DAG：层级压缩图（hermes-lcm dag.py 骨架移植，MIT）。

节点＝一段源材料的摘要（原始消息或更低层摘要）；边由 source_ids 指向源。
深度语义：D0 叶（分钟级）→ D1（小时）→ D2（天）→ D3+（周/月）。
未凝聚判定＝反查 json_each；血统下钻＝递归 CTE 一路走到叶子 store_id。
与消息库同一个 lcm.db（血统 CTE 要 JOIN messages）。
"""
import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS summary_nodes (
    node_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    depth INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL,
    token_count INTEGER DEFAULT 0,
    source_token_count INTEGER DEFAULT 0,
    source_ids TEXT NOT NULL DEFAULT '[]',
    source_type TEXT NOT NULL DEFAULT 'messages',
    created_at REAL NOT NULL,
    earliest_at REAL,
    latest_at REAL,
    expand_hint TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_nodes_session_depth
    ON summary_nodes(session_id, depth, created_at);
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    summary, content='summary_nodes', content_rowid='node_id');
CREATE TRIGGER IF NOT EXISTS nodes_fts_insert AFTER INSERT ON summary_nodes BEGIN
    INSERT INTO nodes_fts(rowid, summary) VALUES (new.node_id, new.summary);
END;
CREATE TRIGGER IF NOT EXISTS nodes_fts_delete AFTER DELETE ON summary_nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, summary)
        VALUES('delete', old.node_id, old.summary);
END;
"""


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


class SummaryDAG:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), timeout=5.0,
                                     check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def add_node(self, node):
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT INTO summary_nodes
                   (session_id, depth, summary, token_count, source_token_count,
                    source_ids, source_type, created_at, earliest_at, latest_at,
                    expand_hint) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (node.session_id, node.depth, node.summary, node.token_count,
                 node.source_token_count, json.dumps(node.source_ids),
                 node.source_type, node.created_at or time.time(),
                 node.earliest_at, node.latest_at, node.expand_hint))
            node.node_id = cur.lastrowid
            return node.node_id

    def get_node(self, node_id):
        row = self._conn.execute(
            "SELECT * FROM summary_nodes WHERE node_id=?", (node_id,)).fetchone()
        return self._row_to_node(row) if row else None

    def get_session_nodes(self, session_id, depth=None, limit=1000):
        if depth is not None:
            rows = self._conn.execute(
                """SELECT * FROM summary_nodes WHERE session_id=? AND depth=?
                   ORDER BY created_at LIMIT ?""",
                (session_id, depth, limit)).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT * FROM summary_nodes WHERE session_id=?
                   ORDER BY depth, created_at LIMIT ?""",
                (session_id, limit)).fetchall()
        return [self._row_to_node(r) for r in rows]

    def count_at_depth(self, session_id, depth):
        return self._conn.execute(
            "SELECT COUNT(*) FROM summary_nodes WHERE session_id=? AND depth=?",
            (session_id, depth)).fetchone()[0]

    def get_uncondensed_at_depth(self, session_id, depth, limit=100):
        """该层还没被更高层引用为源的节点（＝摘要前沿的该层成员）。"""
        rows = self._conn.execute(
            """SELECT n.* FROM summary_nodes n
               WHERE n.session_id=? AND n.depth=?
               AND n.node_id NOT IN (
                   SELECT json_each.value FROM summary_nodes p, json_each(p.source_ids)
                   WHERE p.session_id=? AND p.depth>? AND p.source_type='nodes')
               ORDER BY n.created_at LIMIT ?""",
            (session_id, depth, session_id, depth, limit)).fetchall()
        return [self._row_to_node(r) for r in rows]

    def frontier_nodes(self, session_id):
        """整个摘要前沿（各层未凝聚节点，高层在前）——装配二期直接吃它。"""
        depths = sorted({n.depth for n in self.get_session_nodes(session_id)},
                        reverse=True)
        out = []
        for d in depths:
            out.extend(self.get_uncondensed_at_depth(session_id, d))
        return out

    def source_message_ids(self, node_id, *, limit):
        """节点 → 其下全部叶子消息的 store_id（递归 CTE，有界有序）。"""
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
               ORDER BY m.store_id LIMIT ?""",
            (node_id, limit)).fetchall()
        return [int(r[0]) for r in rows]

    def search(self, query, session_id=None, limit=10):
        """摘要节点检索（FTS＋CJK/LIKE 降级，与消息库同套查询构建）。"""
        from misaka.orchestration.lcm.search_query import (
            escape_like,
            extract_search_terms,
            requires_like_fallback,
            sanitize_fts5_query,
            sanitize_like_query,
        )
        safe = sanitize_fts5_query(query)
        if not requires_like_fallback(query, safe):
            where, args = ["nodes_fts MATCH ?"], [safe]
            if session_id is not None:
                where.append("n.session_id=?")
                args.append(session_id)
            args.append(limit)
            try:
                rows = self._conn.execute(
                    f"""SELECT n.* FROM nodes_fts fts
                        JOIN summary_nodes n ON n.node_id = fts.rowid
                        WHERE {' AND '.join(where)} ORDER BY rank LIMIT ?""",
                    args).fetchall()
                return [self._row_to_node(r) for r in rows]
            except sqlite3.Error:
                pass
        terms = extract_search_terms(sanitize_like_query(query))
        if not terms:
            return []
        where, args = [], []
        if session_id is not None:
            where.append("session_id=?")
            args.append(session_id)
        where.append("(" + " OR ".join(["summary LIKE ? ESCAPE '\\'"] * len(terms)) + ")")
        args.extend(f"%{escape_like(t)}%" for t in terms)
        args.append(limit)
        rows = self._conn.execute(
            f"""SELECT * FROM summary_nodes WHERE {' AND '.join(where)}
                ORDER BY node_id DESC LIMIT ?""", args).fetchall()
        return [self._row_to_node(r) for r in rows]

    def describe_subtree(self, node_id):
        node = self.get_node(node_id)
        if not node:
            return {"error": f"没有节点 {node_id}"}
        children = []
        if node.source_type == "nodes" and node.source_ids:
            marks = ",".join("?" * len(node.source_ids))
            for r in self._conn.execute(
                    f"SELECT * FROM summary_nodes WHERE node_id IN ({marks})"
                    " ORDER BY created_at", node.source_ids).fetchall():
                c = self._row_to_node(r)
                children.append({"node_id": c.node_id, "depth": c.depth,
                                 "token_count": c.token_count,
                                 "expand_hint": c.expand_hint})
        return {"node_id": node.node_id, "depth": node.depth,
                "token_count": node.token_count,
                "source_token_count": node.source_token_count,
                "source_type": node.source_type,
                "num_sources": len(node.source_ids),
                "earliest_at": node.earliest_at, "latest_at": node.latest_at,
                "expand_hint": node.expand_hint, "children": children}

    def get_session_depth_stats(self, session_id):
        rows = self._conn.execute(
            """SELECT depth, COUNT(*), COALESCE(SUM(token_count), 0),
                      COALESCE(SUM(source_token_count), 0)
               FROM summary_nodes WHERE session_id=? GROUP BY depth ORDER BY depth""",
            (session_id,)).fetchall()
        return {int(r[0]): {"count": int(r[1]), "tokens": int(r[2]),
                            "source_tokens": int(r[3])} for r in rows}

    def reassign_session_nodes(self, old_session_id, new_session_id):
        """/new 接续：摘要搬去新会话（只搬 DAG 节点，原始消息归属不动——上游纪律）。"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE summary_nodes SET session_id=? WHERE session_id=?",
                (new_session_id, old_session_id))
            return cur.rowcount or 0

    def delete_below_depth(self, session_id, min_depth):
        """会话重置保高层：删 depth < min_depth 的节点。min_depth=None 全删。"""
        with self._lock, self._conn:
            if min_depth is None:
                cur = self._conn.execute(
                    "DELETE FROM summary_nodes WHERE session_id=?", (session_id,))
            else:
                cur = self._conn.execute(
                    "DELETE FROM summary_nodes WHERE session_id=? AND depth<?",
                    (session_id, min_depth))
            return cur.rowcount or 0

    @staticmethod
    def _row_to_node(row):
        return SummaryNode(
            node_id=row[0], session_id=row[1], depth=row[2], summary=row[3],
            token_count=row[4], source_token_count=row[5],
            source_ids=json.loads(row[6]) if row[6] else [],
            source_type=row[7], created_at=row[8], earliest_at=row[9],
            latest_at=row[10], expand_hint=row[11] or "")

    def close(self):
        if self._conn is not None:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error:
                pass
            self._conn.close()
            self._conn = None


if __name__ == "__main__":
    import tempfile

    from misaka.orchestration.lcm.store import MessageStore

    db = Path(tempfile.mkdtemp()) / "lcm.db"
    store = MessageStore(db)          # 同库：血统 CTE 要 JOIN messages
    dag = SummaryDAG(db)
    sid = "s1"
    mids = store.append_batch(sid, [
        {"role": "user", "content": f"消息{i}"} for i in range(6)])

    leaf_a = SummaryNode(session_id=sid, depth=0, summary="前三条的摘要",
                         source_ids=mids[:3], source_type="messages",
                         expand_hint="搬迁记录细节")
    leaf_b = SummaryNode(session_id=sid, depth=0, summary="后三条的摘要",
                         source_ids=mids[3:], source_type="messages")
    dag.add_node(leaf_a)
    dag.add_node(leaf_b)
    assert dag.count_at_depth(sid, 0) == 2
    assert len(dag.get_uncondensed_at_depth(sid, 0)) == 2, "都没被凝聚"

    d1 = SummaryNode(session_id=sid, depth=1, summary="全程弧线摘要",
                     source_ids=[leaf_a.node_id, leaf_b.node_id], source_type="nodes")
    dag.add_node(d1)
    assert dag.get_uncondensed_at_depth(sid, 0) == [], "叶被 D1 引用后不再是前沿"
    frontier = dag.frontier_nodes(sid)
    assert [n.node_id for n in frontier] == [d1.node_id], "前沿只剩 D1"

    walked = dag.source_message_ids(d1.node_id, limit=100)
    assert walked == sorted(mids), f"血统下钻要走到全部叶子: {walked}"
    assert dag.source_message_ids(d1.node_id, limit=2) == sorted(mids)[:2], "有界"

    desc = dag.describe_subtree(d1.node_id)
    assert desc["num_sources"] == 2 and len(desc["children"]) == 2
    assert dag.get_session_depth_stats(sid)[1]["count"] == 1
    assert "error" in dag.describe_subtree(999)

    assert dag.reassign_session_nodes(sid, "s2") == 3
    assert dag.get_session_nodes(sid) == [] and len(dag.get_session_nodes("s2")) == 3
    assert dag.delete_below_depth("s2", 1) == 2, "重置保高层：只删 D0"
    assert [n.depth for n in dag.get_session_nodes("s2")] == [1]
    dag.close()
    store.close()
    print("lcm dag selfcheck ok — 前沿判定/血统下钻/描述/搬迁/保高层重置 全对")

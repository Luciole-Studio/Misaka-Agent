"""LCM 消息库：不可变优先＋FTS5（hermes-lcm store.py 骨架移植，MIT）。

每条消息先落库再谈压缩——无损的地基。schema 忠实上游（含双时戳契约：
timestamp/ingested_at＝LCM 写入时刻，observed_at＝来源时刻，绝不互相冒充）。
搜索：FTS5 外容表；CJK/emoji 查询自动降级 LIKE 通道（unicode61 不切 CJK 词，
这对全中文库是主路径不是兜底）；directness 评分反堆砌。

ponytail: 上游的 early-stop 翻页扫描（可证明正确的提前停）简化为单次放大
fetch——misaka 库量级（万行内）全扫都快；破十万行再抄上游翻页环。
"""
import json
import math
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from misaka.orchestration.lcm.search_query import (
    AGE_DECAY_RATE,
    build_snippet,
    compute_directness_score,
    compute_search_fetch_limit,
    contains_risky_fts_ascii,
    count_term_matches,
    escape_like,
    extract_quoted_phrases,
    extract_search_terms,
    normalize_search_sort,
    requires_like_fallback,
    sanitize_fts5_query,
    sanitize_like_query,
)
from misaka.orchestration.lcm.tokens import count_message_tokens, normalize_content_value

_COLUMNS = ("store_id, session_id, source, role, content, tool_call_id, tool_calls,"
            " tool_name, timestamp, token_estimate, pinned, ingested_at,"
            " observed_at, observed_at_source")
_ROLE_BIAS = {"user": 0.0, "assistant": 1.0, "tool": 2.0}

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    store_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    source TEXT DEFAULT '',
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    token_estimate INTEGER DEFAULT 0,
    pinned INTEGER DEFAULT 0,
    ingested_at REAL,
    observed_at REAL,
    observed_at_source TEXT
);
CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id, store_id);
CREATE INDEX IF NOT EXISTS idx_msg_session_ts ON messages(session_id, timestamp);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content, content='messages', content_rowid='store_id');
CREATE TRIGGER IF NOT EXISTS msg_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.store_id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS msg_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
        VALUES('delete', old.store_id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS msg_fts_update AFTER UPDATE OF content ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
        VALUES('delete', old.store_id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.store_id, new.content);
END;
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
"""


def _normalize_observed_at(value):
    """来源时戳：数字 Unix 秒或带时区 ISO——可疑值一律 None，绝不用写入时冒充。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        observed = float(value)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            observed = float(raw)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                return None
            observed = parsed.timestamp()
    else:
        return None
    if not math.isfinite(observed) or observed <= 0:
        return None
    try:
        datetime.fromtimestamp(observed, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None
    return observed


class MessageStore:
    """SQLite 消息库。append-only（无删改口——删除只随运维面五期来）。"""

    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()   # 上游教训：跨线程写并发的纵深防御
        self._conn = sqlite3.connect(str(self.db_path), timeout=5.0,
                                     check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ── 写 ──────────────────────────────────────────────────────────────

    def append(self, session_id, msg, source=""):
        """落一条，返回 store_id。token 估算就地算（CJK 感知）。"""
        return self.append_batch(session_id, [msg], source=source)[0]

    def append_batch(self, session_id, messages, source=""):
        ids = []
        with self._write_lock, self._conn:
            for msg in messages:
                tc = msg.get("tool_calls")
                now = time.time()
                cur = self._conn.execute(
                    f"""INSERT INTO messages
                        (session_id, source, role, content, tool_call_id, tool_calls,
                         tool_name, timestamp, token_estimate, pinned, ingested_at,
                         observed_at, observed_at_source)
                        VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?)""",
                    (session_id, (source or "").strip(),
                     msg.get("role", "unknown"),
                     normalize_content_value(msg.get("content")),
                     msg.get("tool_call_id"),
                     json.dumps(tc, ensure_ascii=False) if tc else None,
                     msg.get("tool_name"), now, count_message_tokens(msg), now,
                     _normalize_observed_at(msg.get("timestamp")),
                     "host_message_timestamp"
                     if _normalize_observed_at(msg.get("timestamp")) is not None
                     else None))
                ids.append(cur.lastrowid)
        return ids

    # ── 读 ──────────────────────────────────────────────────────────────

    def get(self, store_id):
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM messages WHERE store_id=?", (store_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def get_batch(self, store_ids):
        if not store_ids:
            return {}
        marks = ",".join("?" * len(store_ids))
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM messages WHERE store_id IN ({marks})",
            list(store_ids)).fetchall()
        return {r[0]: self._row_to_dict(r) for r in rows}

    def get_session_messages_after(self, session_id, after_store_id=0, limit=10000):
        rows = self._conn.execute(
            f"""SELECT {_COLUMNS} FROM messages WHERE session_id=? AND store_id>?
                ORDER BY store_id LIMIT ?""",
            (session_id, after_store_id, limit)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_session_tail(self, session_id, limit=1000):
        if limit <= 0:
            return []
        rows = self._conn.execute(
            f"""SELECT {_COLUMNS} FROM (
                    SELECT {_COLUMNS} FROM messages WHERE session_id=?
                    ORDER BY store_id DESC LIMIT ?) ORDER BY store_id""",
            (session_id, limit)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_session_count(self, session_id):
        return self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=?",
            (session_id,)).fetchone()[0]

    def load_session_page(self, session_id, *, after_store_id=0, limit=100,
                          roles=None, time_from=None, time_to=None):
        """有序原文分页（after_store_id 排他，接上一页 next_cursor 不重行）。"""
        where, args = ["session_id=?", "store_id>?"], [session_id, after_store_id]
        if roles:
            where.append(f"role IN ({','.join('?' * len(roles))})")
            args.extend(roles)
        if time_from is not None:
            where.append("timestamp>=?")
            args.append(time_from)
        if time_to is not None:
            where.append("timestamp<=?")
            args.append(time_to)
        args.append(limit)
        rows = self._conn.execute(
            f"""SELECT {_COLUMNS} FROM messages WHERE {' AND '.join(where)}
                ORDER BY store_id LIMIT ?""", args).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_time_bounds(self, store_ids):
        if not store_ids:
            return None, None
        marks = ",".join("?" * len(store_ids))
        row = self._conn.execute(
            f"SELECT MIN(timestamp), MAX(timestamp) FROM messages WHERE store_id IN ({marks})",
            list(store_ids)).fetchone()
        return (row[0], row[1]) if row else (None, None)

    # ── 元数据 KV（cursor/账本用，二期消费）─────────────────────────────

    def read_metadata_json(self, key):
        row = self._conn.execute(
            "SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def write_metadata_json(self, key, value):
        with self._write_lock, self._conn:
            self._conn.execute(
                "INSERT INTO metadata(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False, sort_keys=True)))

    # ── 搜索 ────────────────────────────────────────────────────────────

    def search(self, query, session_id=None, limit=20, sort=None, role=None,
               time_from=None, time_to=None):
        """FTS5 检索；CJK/emoji/风险 ASCII 自动降级 LIKE。返回带 snippet 的行。"""
        safe = sanitize_fts5_query(query)
        if requires_like_fallback(query, safe):
            return self._search_like(query, session_id=session_id, limit=limit,
                                     sort=sort, role=role,
                                     time_from=time_from, time_to=time_to)
        terms = extract_search_terms(safe)
        phrases = extract_quoted_phrases(safe)
        where, args = ["messages_fts MATCH ?"], [safe]
        if session_id is not None:
            where.append("m.session_id=?")
            args.append(session_id)
        if role is not None:
            where.append("m.role=?")
            args.append(role)
        if time_from is not None:
            where.append("m.timestamp>=?")
            args.append(time_from)
        if time_to is not None:
            where.append("m.timestamp<=?")
            args.append(time_to)
        args.append(compute_search_fetch_limit(limit, terms, phrases))
        try:
            rows = self._conn.execute(
                f"""SELECT {', '.join('m.' + c.strip() for c in _COLUMNS.split(','))},
                           rank AS search_rank,
                           snippet(messages_fts, 0, '>>>', '<<<', '...', 40) AS snip
                    FROM messages_fts fts JOIN messages m ON m.store_id = fts.rowid
                    WHERE {' AND '.join(where)} ORDER BY rank LIMIT ?""",
                args).fetchall()
        except sqlite3.Error:
            return self._search_like(query, session_id=session_id, limit=limit,
                                     sort=sort, role=role,
                                     time_from=time_from, time_to=time_to)
        results = []
        for r in rows:
            d = self._row_to_dict(r[:14])
            d["search_rank"] = r[14]
            d["snippet"] = r[15]
            d["_directness"] = compute_directness_score(d.get("content") or "",
                                                        terms, phrases)
            results.append(d)
        results.sort(key=lambda d: self._sort_key(d, sort))
        for d in results:
            d.pop("_directness", None)
        return results[:limit]

    def _search_like(self, query, session_id=None, limit=20, sort=None, role=None,
                     time_from=None, time_to=None):
        safe = sanitize_like_query(query)
        terms = extract_search_terms(safe)
        phrases = extract_quoted_phrases(safe)
        if not terms:
            return []
        where, args = ["content IS NOT NULL"], []
        if session_id is not None:
            where.append("session_id=?")
            args.append(session_id)
        if role is not None:
            where.append("role=?")
            args.append(role)
        if time_from is not None:
            where.append("timestamp>=?")
            args.append(time_from)
        if time_to is not None:
            where.append("timestamp<=?")
            args.append(time_to)
        where.append("(" + " OR ".join(["content LIKE ? ESCAPE '\\'"] * len(terms)) + ")")
        args.extend(f"%{escape_like(t)}%" for t in terms)
        args.append(compute_search_fetch_limit(limit, terms, phrases) * 4)
        rows = self._conn.execute(
            f"""SELECT {_COLUMNS} FROM messages WHERE {' AND '.join(where)}
                ORDER BY store_id DESC LIMIT ?""", args).fetchall()
        collapse = contains_risky_fts_ascii(query)
        results = []
        for r in rows:
            d = self._row_to_dict(r)
            content = d.get("content") or ""
            score = sum(min(count_term_matches(content, t), 1) if collapse
                        else count_term_matches(content, t) for t in terms)
            if score <= 0:
                continue
            d["search_rank"] = -float(score)   # 上游同约定：负分对齐 FTS rank 越小越好
            d["snippet"] = build_snippet(content, terms)
            d["_directness"] = compute_directness_score(content, terms, phrases)
            results.append(d)
        results.sort(key=lambda d: self._sort_key(d, sort))
        for d in results:
            d.pop("_directness", None)
        return results[:limit]

    @staticmethod
    def _sort_key(d, sort):
        rank = d.get("search_rank")
        rank_v = float(rank) if rank is not None else float("inf")
        ts = float(d.get("timestamp") or 0.0)
        bias = _ROLE_BIAS.get(d.get("role"), 1.0)
        direct = float(d.get("_directness") or 0.0)
        normalized = normalize_search_sort(sort)
        if normalized == "relevance":
            return (rank_v, -direct, bias, -ts)
        if normalized == "hybrid":
            age_h = max(0.0, (time.time() - ts) / 3600.0)
            return (rank_v / (1 + age_h * AGE_DECAY_RATE), -direct, bias, -ts)
        return (-ts, bias, rank_v, -direct)

    # ── 杂项 ────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_dict(row):
        cols = [c.strip() for c in _COLUMNS.split(",")]
        d = dict(zip(cols, row[:len(cols)]))
        if d.get("tool_calls"):
            try:
                d["tool_calls"] = json.loads(d["tool_calls"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

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

    store = MessageStore(Path(tempfile.mkdtemp()) / "lcm.db")
    sid = "s1"
    ids = store.append_batch(sid, [
        {"role": "user", "content": "查明入藏簿里 1954 年的搬迁记录",
         "timestamp": "2026-08-01T10:00:00+00:00"},
        {"role": "assistant", "content": "档案显示 1954 年整体入库",
         "tool_calls": [{"function": {"name": "read", "arguments": {"path": "a.md"}}}]},
        {"role": "tool", "content": "入藏簿第 3 页：1954 年 3 月", "tool_call_id": "c1"},
        {"role": "user", "content": "the archive shows 1954 records"},
    ])
    assert len(ids) == 4 and store.get_session_count(sid) == 4
    row = store.get(ids[0])
    assert row["observed_at"] is not None and row["observed_at_source"] == "host_message_timestamp"
    assert store.get(ids[1])["observed_at"] is None, "无来源时戳绝不冒充"
    assert store.get(ids[1])["token_estimate"] > 0, "CJK 感知估算落库"

    hits = store.search("入藏簿", session_id=sid)
    assert len(hits) == 2 and all("入藏簿" in h["content"] for h in hits), \
        f"CJK 查询走 LIKE 通道命中: {[h['content'] for h in hits]}"
    assert hits[0]["snippet"], "LIKE 命中也带 snippet"
    hits_en = store.search("archive", session_id=sid)
    assert len(hits_en) == 1 and ">>>" in hits_en[0]["snippet"], "英文走 FTS 带高亮"
    assert store.search("入藏簿", session_id=sid, role="tool")[0]["role"] == "tool"
    assert store.search("找不到的词汇", session_id=sid) == []

    page = store.load_session_page(sid, limit=2)
    page2 = store.load_session_page(sid, after_store_id=page[-1]["store_id"], limit=10)
    assert [r["store_id"] for r in page + page2] == ids, "游标分页不重不漏"
    tail = store.get_session_tail(sid, limit=2)
    assert [r["store_id"] for r in tail] == ids[-2:], "尾巴按 store 序"
    lo, hi = store.get_time_bounds(ids)
    assert lo <= hi
    store.write_metadata_json("cursor:s1", {"n": 3})
    assert store.read_metadata_json("cursor:s1") == {"n": 3}
    store.close()
    print("lcm store selfcheck ok — 落库/双时戳/CJK-LIKE/FTS 高亮/分页/KV 全对")

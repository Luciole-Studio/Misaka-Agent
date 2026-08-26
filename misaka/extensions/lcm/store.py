"""Persistent LCM message and summary storage with full-text search."""
import base64
import binascii
import hashlib
import json
import math
import re
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from misaka.extensions.lcm.migrations import migrate
from misaka.extensions.lcm.search_query import (
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
from misaka.extensions.lcm.tokens import count_message_tokens, normalize_content_value

_COLUMNS = ("store_id, session_id, source, role, content, tool_call_id, tool_calls,"
            " tool_name, timestamp, token_estimate, pinned, ingested_at,"
            " observed_at, observed_at_source, host_entry_id")
_COLUMN_COUNT = len([c for c in _COLUMNS.split(",")])
_ROLE_BIAS = {"user": 0.0, "assistant": 1.0, "tool": 2.0}
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/\r\n]+={0,2}$")
_MEDIA_TYPES = {"image", "audio", "video", "file", "document"}


def _omitted_blob(value, kind="base64"):
    raw = value.encode("utf-8", "replace")
    return (f"[LCM omitted {kind}: sha256={hashlib.sha256(raw).hexdigest()} "
            f"encoded_bytes={len(raw)}; exact bytes remain in the session transcript]")


def storage_safe_content(value):
    """Keep media/base64 out of SQLite/FTS while preserving a stable transcript handle."""
    if isinstance(value, list):
        return [storage_safe_content(item) for item in value]
    if isinstance(value, dict):
        out = {str(k): storage_safe_content(v) for k, v in value.items()}
        kind = str(value.get("type") or "").lower()
        data = value.get("data")
        if kind in _MEDIA_TYPES and isinstance(data, str) and data:
            out["data"] = _omitted_blob(data, f"{kind}/{value.get('mimeType') or 'binary'}")
        return out
    if isinstance(value, str):
        if value.startswith("data:") and ";base64," in value[:256]:
            return _omitted_blob(value, "data-uri")
        compact = "".join(value.split())
        if len(compact) >= 8192 and len(compact) % 4 == 0 and _BASE64_RE.fullmatch(compact):
            try:
                base64.b64decode(compact[:4096], validate=True)
            except (ValueError, binascii.Error):
                return value
            return _omitted_blob(value)
    return value


def _normalize_observed_at(value):
    """Coerce a host timestamp (epoch seconds/millis or ISO-8601 with offset) to epoch seconds, or None."""
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
                parsed = datetime.fromisoformat(raw)
            except ValueError:
                return None
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                return None
            observed = parsed.timestamp()
    else:
        return None
    if observed > 10_000_000_000:  # host AgentMessage timestamps are milliseconds
        observed /= 1000.0
    if not math.isfinite(observed) or observed <= 0:
        return None
    try:
        datetime.fromtimestamp(observed, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None
    return observed


class MessageStore:
    """SQLite-backed store of raw session messages with FTS5 search and a metadata KV."""

    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), timeout=5.0,
                                     check_same_thread=False)
        migrate(self._conn)

    # ── Write ───────────────────────────────────────────────────────────────

    def append(self, session_id, msg, source="", host_entry_id=None):
        """Store one message and return its ID and estimated token count."""
        return self.append_batch(session_id, [msg], source=source,
                                 host_entry_ids=[host_entry_id])[0]

    def append_batch(self, session_id, messages, source="", host_entry_ids=None):
        """Append messages, or idempotently adopt/insert stable host entry ids."""
        if host_entry_ids is None:
            host_entry_ids = [None] * len(messages)
        if len(host_entry_ids) != len(messages):
            raise ValueError("host_entry_ids must align with messages")
        self._adopt_unkeyed(session_id, messages, host_entry_ids, source)
        existing = self.get_by_host_entry_ids(
            session_id, [entry_id for entry_id in host_entry_ids if entry_id])
        ids = []
        with self._write_lock, self._conn:
            for msg, host_entry_id in zip(messages, host_entry_ids):
                if host_entry_id in existing:
                    ids.append(existing[host_entry_id])
                    continue
                tc = msg.get("tool_calls")
                now = time.time()
                observed = _normalize_observed_at(msg.get("timestamp"))
                safe_content = normalize_content_value(storage_safe_content(msg.get("content")))
                cur = self._conn.execute(
                    """INSERT OR IGNORE INTO messages
                        (session_id, source, role, content, tool_call_id, tool_calls,
                         tool_name, timestamp, token_estimate, pinned, ingested_at,
                         observed_at, observed_at_source, host_entry_id)
                        VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?,?)""",
                    (session_id, (source or "").strip(),
                     msg.get("role", "unknown"),
                     safe_content,
                     msg.get("tool_call_id"),
                     json.dumps(tc, ensure_ascii=False) if tc else None,
                     msg.get("tool_name"), now, count_message_tokens(msg), now,
                     observed,
                     "host_message_timestamp"
                     if observed is not None else None,
                     host_entry_id))
                if cur.rowcount == 0 and host_entry_id:
                    row = self._conn.execute(
                        "SELECT store_id FROM messages WHERE session_id=? AND host_entry_id=?",
                        (session_id, host_entry_id),
                    ).fetchone()
                    if not row:
                        raise RuntimeError("idempotent message insert lost its host entry")
                    ids.append(int(row[0]))
                else:
                    ids.append(cur.lastrowid)
        return ids

    def _adopt_unkeyed(self, session_id, messages, host_entry_ids, source):
        """One-time upgrade: attach stable ids to old rows instead of duplicating them."""
        if not any(host_entry_ids):
            return
        marker = f"host_entry_adopted:{session_id}"
        if self.read_metadata_json(marker):
            return
        rows = self._conn.execute(
            "SELECT store_id, role, content, tool_call_id FROM messages "
            "WHERE session_id=? AND host_entry_id IS NULL ORDER BY store_id",
            (session_id,),
        ).fetchall()
        if not rows:
            self.write_metadata_json(marker, True)
            return
        candidates = [
            (entry_id, str(msg.get("role", "unknown")),
             normalize_content_value(storage_safe_content(msg.get("content"))),
             msg.get("tool_call_id"))
            for msg, entry_id in zip(messages, host_entry_ids) if entry_id
        ]
        cursor = 0
        with self._write_lock, self._conn:
            for store_id, role, content, tool_call_id in rows:
                match = next((i for i in range(cursor, len(candidates))
                              if candidates[i][1:] == (role, content, tool_call_id)), None)
                if match is None:
                    continue
                entry_id = candidates[match][0]
                try:
                    self._conn.execute(
                        "UPDATE messages SET host_entry_id=?, source=CASE WHEN source='' "
                        "THEN ? ELSE source END WHERE store_id=?",
                        (entry_id, (source or "").strip(), store_id),
                    )
                except sqlite3.IntegrityError:
                    pass
                cursor = match + 1
        self.write_metadata_json(marker, True)

    # ── Read ──────────────────────────────────────────────────────────────────

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

    def get_by_host_entry_ids(self, session_id, host_entry_ids):
        if not host_entry_ids:
            return {}
        out = {}
        for start in range(0, len(host_entry_ids), 900):
            chunk = host_entry_ids[start:start + 900]
            marks = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT host_entry_id, store_id FROM messages WHERE session_id=? "
                f"AND host_entry_id IN ({marks})", [session_id, *chunk]).fetchall()
            out.update({str(entry_id): int(store_id) for entry_id, store_id in rows})
        return out

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
                          roles=None, time_from=None, time_to=None, source=None):
        """Load an ordered page of source messages after a stable store ID."""
        where, args = ["session_id=?", "store_id>?"], [session_id, after_store_id]
        if roles:
            where.append(f"role IN ({','.join('?' * len(roles))})")
            args.extend(roles)
        if time_from is not None:
            where.append("COALESCE(observed_at,timestamp)>=?")
            args.append(time_from)
        if time_to is not None:
            where.append("COALESCE(observed_at,timestamp)<=?")
            args.append(time_to)
        if source is not None:
            where.append("source=?")
            args.append(source)
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
            f"SELECT MIN(COALESCE(observed_at,timestamp)), "
            f"MAX(COALESCE(observed_at,timestamp)) FROM messages "
            f"WHERE store_id IN ({marks})",
            list(store_ids)).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def recent(self, *, session_id=None, time_from=None, time_to=None, limit=20,
               source=None):
        where, args = [], []
        if session_id is not None:
            where.append("session_id=?")
            args.append(session_id)
        if source is not None:
            where.append("source=?")
            args.append(source)
        if time_from is not None:
            where.append("COALESCE(observed_at,timestamp)>=?")
            args.append(time_from)
        if time_to is not None:
            where.append("COALESCE(observed_at,timestamp)<=?")
            args.append(time_to)
        clause = " WHERE " + " AND ".join(where) if where else ""
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM messages{clause} "
            "ORDER BY COALESCE(observed_at,timestamp) DESC, store_id DESC LIMIT ?",
            [*args, limit],
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def delete_session(self, session_id):
        with self._write_lock, self._conn:
            cur = self._conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            return cur.rowcount or 0

    # ── Metadata KV (cursors and accounting) ──────────────────────────────────

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

    def record_summary_usage(self, *, input_tokens=0, output_tokens=0, failed=False):
        with self._write_lock, self._conn:
            row = self._conn.execute(
                "SELECT value FROM metadata WHERE key='summary_usage'"
            ).fetchone()
            try:
                usage = json.loads(row[0]) if row and row[0] else {}
            except (json.JSONDecodeError, TypeError):
                usage = {}
            usage["calls"] = int(usage.get("calls", 0)) + 1
            usage["failures"] = int(usage.get("failures", 0)) + int(bool(failed))
            usage["input_tokens_est"] = int(usage.get("input_tokens_est", 0)) + int(input_tokens)
            usage["output_tokens_est"] = int(usage.get("output_tokens_est", 0)) + int(output_tokens)
            self._conn.execute(
                "INSERT INTO metadata(key,value) VALUES('summary_usage',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(usage, sort_keys=True),),
            )

    # ── Search ────────────────────────────────────────────────────────────

    def search(self, query, session_id=None, limit=20, sort=None, role=None,
               time_from=None, time_to=None, source=None):
        """Search with FTS5, falling back to LIKE when sanitization loses meaning."""
        safe = sanitize_fts5_query(query)
        if requires_like_fallback(query, safe):
            return self._search_like(query, session_id=session_id, limit=limit,
                                     sort=sort, role=role,
                                     time_from=time_from, time_to=time_to,
                                     source=source)
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
            where.append("COALESCE(m.observed_at,m.timestamp)>=?")
            args.append(time_from)
        if time_to is not None:
            where.append("COALESCE(m.observed_at,m.timestamp)<=?")
            args.append(time_to)
        if source is not None:
            where.append("m.source=?")
            args.append(source)
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
                                     time_from=time_from, time_to=time_to,
                                     source=source)
        results = []
        for r in rows:
            d = self._row_to_dict(r[:_COLUMN_COUNT])
            d["search_rank"] = r[_COLUMN_COUNT]
            d["snippet"] = r[_COLUMN_COUNT + 1]
            d["_directness"] = compute_directness_score(d.get("content") or "",
                                                        terms, phrases)
            d["retrieval"] = "fts"
            results.append(d)
        results.sort(key=lambda d: self._sort_key(d, sort))
        for d in results:
            d.pop("_directness", None)
        return results[:limit]

    def _search_like(self, query, session_id=None, limit=20, sort=None, role=None,
                     time_from=None, time_to=None, source=None):
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
            where.append("COALESCE(observed_at,timestamp)>=?")
            args.append(time_from)
        if time_to is not None:
            where.append("COALESCE(observed_at,timestamp)<=?")
            args.append(time_to)
        if source is not None:
            where.append("source=?")
            args.append(source)
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
            d["search_rank"] = -float(score)  # Negate FTS rank so larger values sort first.
            d["snippet"] = build_snippet(content, terms)
            d["_directness"] = compute_directness_score(content, terms, phrases)
            d["retrieval"] = "like"
            results.append(d)
        results.sort(key=lambda d: self._sort_key(d, sort))
        for d in results:
            d.pop("_directness", None)
        return results[:limit]

    @staticmethod
    def _sort_key(d, sort):
        rank = d.get("search_rank")
        rank_v = float(rank) if rank is not None else float("inf")
        ts = float(d.get("observed_at") or d.get("timestamp") or 0.0)
        bias = _ROLE_BIAS.get(d.get("role"), 1.0)
        direct = float(d.get("_directness") or 0.0)
        normalized = normalize_search_sort(sort)
        if normalized == "relevance":
            return (rank_v, -direct, bias, -ts)
        if normalized == "hybrid":
            age_h = max(0.0, (time.time() - ts) / 3600.0)
            return (rank_v / (1 + age_h * AGE_DECAY_RATE), -direct, bias, -ts)
        return (-ts, bias, rank_v, -direct)

    # ── Miscellaneous ────────────────────────────────────────────────────────────

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

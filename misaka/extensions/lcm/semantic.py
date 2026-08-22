"""Optional summary-vector retrieval; stdlib SQLite scan, FastEmbed loaded lazily."""

import math
import sqlite3
import time
from array import array
from pathlib import Path

from misaka.extensions.lcm.migrations import migrate


class SemanticUnavailable(RuntimeError):
    pass


class FastEmbedder:
    def __init__(self, model):
        self.model_id = model
        self._model = None

    def embed(self, texts):
        if self._model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as exc:
                raise SemanticUnavailable(
                    "Semantic retrieval is not installed; run `uv sync --extra lcm-semantic`."
                ) from exc
            self._model = TextEmbedding(model_name=self.model_id)
        return [[float(x) for x in vector] for vector in self._model.embed(texts)]


def _pack(vector):
    return array("f", vector).tobytes()


def _unpack(blob):
    vector = array("f")
    vector.frombytes(blob)
    return vector


def _norm(vector):
    return math.sqrt(sum(value * value for value in vector))


class SemanticIndex:
    """Embeds committed summary nodes only; raw messages are not vectorized until the cost is measured."""

    def __init__(self, db_path, model, *, embedder=None):
        self.db_path = Path(db_path)
        self.model = str(model or "").strip()
        self.embedder = embedder or FastEmbedder(self.model)
        self._conn = sqlite3.connect(str(self.db_path), timeout=5.0,
                                     check_same_thread=False)
        migrate(self._conn)

    def backfill(self, limit=1000):
        rows = self._conn.execute(
            """SELECT n.node_id,n.summary FROM summary_nodes n
               LEFT JOIN summary_embeddings e
                 ON e.node_id=n.node_id AND e.model=?
               WHERE n.committed=1 AND e.node_id IS NULL
               ORDER BY n.node_id LIMIT ?""",
            (self.model, limit),
        ).fetchall()
        if not rows:
            return 0
        vectors = self.embedder.embed([row[1] for row in rows])
        if len(vectors) != len(rows):
            raise SemanticUnavailable("The embedding provider returned an unexpected vector count.")
        with self._conn:
            for (node_id, _), vector in zip(rows, vectors):
                norm = _norm(vector)
                if not vector or norm <= 0:
                    continue
                self._conn.execute(
                    "INSERT OR REPLACE INTO summary_embeddings"
                    "(node_id,model,dims,vector,norm,created_at) VALUES(?,?,?,?,?,?)",
                    (node_id, self.model, len(vector), _pack(vector), norm, time.time()),
                )
        return len(rows)

    def search(self, query, *, session_id=None, time_from=None, time_to=None,
               limit=10, backfill_limit=1000):
        if not self.model:
            raise SemanticUnavailable("MISAKA_LCM_EMBEDDING_MODEL is not configured.")
        self.backfill(backfill_limit)
        query_vector = self.embedder.embed([query])[0]
        query_norm = _norm(query_vector)
        if query_norm <= 0:
            return [], self.coverage(session_id=session_id)
        where, args = ["n.committed=1", "e.model=?", "e.dims=?"], [self.model, len(query_vector)]
        if session_id is not None:
            where.append("n.session_id=?")
            args.append(session_id)
        if time_from is not None:
            where.append("COALESCE(n.latest_at,n.created_at)>=?")
            args.append(time_from)
        if time_to is not None:
            where.append("COALESCE(n.earliest_at,n.created_at)<=?")
            args.append(time_to)
        rows = self._conn.execute(
            "SELECT n.node_id,e.vector,e.norm FROM summary_embeddings e "
            "JOIN summary_nodes n ON n.node_id=e.node_id WHERE " + " AND ".join(where),
            args,
        ).fetchall()
        scored = []
        for node_id, blob, norm in rows:
            vector = _unpack(blob)
            score = sum(a * b for a, b in zip(query_vector, vector)) / (query_norm * norm)
            scored.append((int(node_id), float(score)))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:limit], self.coverage(session_id=session_id)

    def coverage(self, *, session_id=None):
        where, args = ["committed=1"], []
        if session_id is not None:
            where.append("session_id=?")
            args.append(session_id)
        total = self._conn.execute(
            "SELECT COUNT(*) FROM summary_nodes WHERE " + " AND ".join(where), args
        ).fetchone()[0]
        indexed_where = ["n.committed=1", "e.model=?"]
        indexed_args = [self.model]
        if session_id is not None:
            indexed_where.append("n.session_id=?")
            indexed_args.append(session_id)
        indexed = self._conn.execute(
            "SELECT COUNT(*) FROM summary_embeddings e JOIN summary_nodes n "
            "ON n.node_id=e.node_id WHERE " + " AND ".join(indexed_where), indexed_args
        ).fetchone()[0]
        return {"indexed": int(indexed), "total": int(total),
                "complete": int(indexed) == int(total)}

    def close(self):
        self._conn.close()

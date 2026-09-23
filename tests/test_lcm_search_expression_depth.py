"""Long recall queries must not exceed SQLite's expression-tree depth."""
import json
import sqlite3
import time
from contextlib import closing
from types import SimpleNamespace

import pytest

from misaka.extensions.misaka_lcm.vendor.dag import SummaryDAG, SummaryNode
from misaka.extensions.misaka_lcm.vendor.search_query import balanced_sql_expr
from misaka.extensions.misaka_lcm.vendor.store import MessageStore
from misaka.extensions.misaka_lcm.vendor.tools import _lcm_grep_full_text_with_deadline


@pytest.mark.parametrize("sort", ["recency", "relevance", "hybrid"])
@pytest.mark.parametrize("quoted", [False, True])
def test_message_search_keeps_last_term_ranking_and_scope(tmp_path, sort, quoted):
    # More terms than SQLite's normal 1000-node expression depth, without
    # exceeding its independent bound on SQL variables.
    missing = " ".join(f"缺失{i:04d}" for i in range(1100))
    tail = '"tail phrase"' if quoted else "🚀tail_100%"
    text = "tail phrase" if quoted else tail
    with closing(MessageStore(tmp_path / "messages.db")) as store:
        first = store.append("s", {"role": "user", "content": text},
                             source="fixture", conversation_id="c")
        second = store.append("s", {"role": "user", "content": text + " " + text},
                              source="fixture", conversation_id="c")
        for sid, role, source, conversation in [
            ("other", "user", "fixture", "c"),
            ("s", "assistant", "fixture", "c"),
            ("s", "user", "other", "c"),
            ("s", "user", "fixture", "other"),
        ]:
            store.append(sid, {"role": role, "content": text * 5},
                         source=source, conversation_id=conversation)
        store._conn.execute("UPDATE messages SET timestamp = 1000")
        store._conn.commit()
        filters = {"session_id": "s", "role": "user", "source": "fixture",
                   "conversation_id": "c", "time_from": 999, "time_to": 1001,
                   "sort": sort, "limit": 1}
        # The small oracle uses the identical LIKE branch, not FTS ranking.
        oracle = store._search_like(tail, **filters)
        result = store.search(missing + " " + tail, **filters)
        assert len(result) == 1
        assert result[0]["store_id"] in {first, second}
        assert [(r["store_id"], r["search_rank"]) for r in result] == [
            (r["store_id"], r["search_rank"]) for r in oracle
        ]
        assert store.search(missing + " " + tail, **{**filters, "time_from": 1002}) == []


@pytest.mark.parametrize("sort", ["recency", "relevance", "hybrid"])
def test_summary_search_keeps_last_term_and_scope(tmp_path, sort):
    missing = " ".join(f"缺失{i:04d}" for i in range(1100))
    with closing(SummaryDAG(tmp_path / "summaries.db")) as dag:
        wanted = dag.add_node(SummaryNode(session_id="s", summary="🚀tail", token_count=2))
        dag.add_node(SummaryNode(session_id="other", summary="🚀tail", token_count=2))
        result = dag.search(missing + " 🚀tail", session_id="s", sort=sort, limit=1)
        assert [node.node_id for node in result] == [wanted]


def test_like_search_still_obeys_sqlite_cancellation(tmp_path):
    with closing(MessageStore(tmp_path / "cancel.db")) as store:
        store.append("s", {"role": "user", "content": "中文证据"})
        store._conn.set_progress_handler(lambda: 1, 1)
        try:
            with pytest.raises(sqlite3.OperationalError, match="interrupted"):
                store.search("中文证据", session_id="s")
        finally:
            store._conn.set_progress_handler(None, 0)


@pytest.mark.parametrize("operator", ["OR", "+"])
@pytest.mark.parametrize("count", [0, 1, 2, 1100])
def test_balanced_sql_respects_depth_and_parameter_order(operator, count):
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 64)
        # Unlike OR / + alone, this operand notices reordered parameters.
        parts = [f"(? = {i})" for i in range(count)]
        expression = balanced_sql_expr(parts, operator)
        value = conn.execute(f"SELECT {expression}", list(range(count))).fetchone()[0]
        assert value == (count if operator == "+" else int(count > 0))
        if count > 64:
            flat = f" {operator} ".join(parts)
            with pytest.raises(sqlite3.OperationalError, match="Expression tree is too large"):
                conn.execute(f"SELECT {flat}", list(range(count)))


@pytest.mark.parametrize("query", ["", "中文证据", "ascii_100%", "🚀"])
def test_like_empty_single_and_duplicate_terms_preserve_results(tmp_path, query):
    with closing(MessageStore(tmp_path / "edge.db")) as store:
        store.append("s", {"role": "user", "content": "中文证据 ascii_100% 🚀"})
        for sort in ("recency", "relevance", "hybrid"):
            single = store._search_like(query, session_id="s", sort=sort)
            repeated = store._search_like(" ".join([query] * 1200), session_id="s", sort=sort)
            assert repeated == single


def test_recall_read_connections_keep_long_query_and_deadline(tmp_path):
    path = tmp_path / "recall.db"
    with closing(MessageStore(path)) as store, closing(SummaryDAG(path)) as dag:
        wanted = store.append("other", {"role": "user", "content": "🚀last"})
        engine = SimpleNamespace(_store=store, _dag=dag, current_session_id="s")
        args = {"query": " ".join(f"缺失{i:04d}" for i in range(1100)) + " 🚀last",
                "session_scope": "all", "limit": 1}
        payload = _lcm_grep_full_text_with_deadline(
            args, engine=engine, deadline=time.monotonic() + 30)
        assert [hit["store_id"] for hit in payload["results"]] == [wanted]
        expired = _lcm_grep_full_text_with_deadline(
            args, engine=engine, deadline=time.monotonic() - 1)
        assert "deadline" in json.dumps(expired)

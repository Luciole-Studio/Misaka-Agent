"""A search reads tool calls too.

The store's search reads ``content``, and what an agent did sits in ``tool_calls``: the commands it
ran, the addresses it fetched, the files it wrote. In the real compacted card sessions on this machine that is
98% of what agents wrote, and 13 of 16 searches that found nothing were for a word in their own
earlier calls. So ``search`` looks through the calls as well, under the same filters.

The calls are read when a search asks, not indexed: LCM searches are rare (111 in 551 real
sessions), and reading leaves the schema and the cache as they are. The newest rows come first, so a
search stops as soon as it has enough. Terms are matched as written, because `dl.ndl.go.jp` and
`ndl_ctx.py` are what an agent looks for, and the store's query cleaning would take them apart.
"""
from __future__ import annotations

import contextlib
import json
import logging
import re
import sqlite3

from ..vendor import store as vendor_store
from ..vendor.search_query import build_snippet, normalize_search_sort

logger = logging.getLogger(__name__)

_TERM = re.compile(r'"([^"]+)"|(\S+)')
_OPERATORS = frozenset({"AND", "OR", "NOT", "NEAR"})


def _terms(query) -> list[str]:
    found = ((phrase or word) for phrase, word in _TERM.findall(str(query or "")))
    return [term.casefold() for term in found if term not in _OPERATORS]


def _values(value):
    """The values an agent wrote, not the names of the fields that hold them."""
    if isinstance(value, dict):
        for item in value.values():
            yield from _values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _values(item)
    elif value is not None:
        yield str(value)


def _call(call) -> tuple[str, str]:
    function = call.get("function") if isinstance(call, dict) else None
    if not isinstance(function, dict):
        return "", ""
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            pass
    return str(function.get("name") or ""), "\n".join(_values(arguments))


def _snippet(tool_calls, terms: list[str]) -> str | None:
    """The call holding every term, as ``tool: text around the match``; ``None`` if no call does."""
    for call in tool_calls if isinstance(tool_calls, list) else ():
        name, text = _call(call)
        written = f"{name}\n{text}".casefold()
        if all(term in written for term in terms):
            return f"{name}: {build_snippet(text, terms, 200)}"
    return None


def _hits(store, query, *, session_id, limit, source, conversation_id, role, time_from, time_to) -> list[dict]:
    """The newest rows with a tool call matching ``query``, shaped as the store shapes a hit."""
    terms = _terms(query)
    if not terms or limit <= 0 or role not in (None, "assistant") or store._is_memory_database:
        return []       # only an assistant row holds calls
    where, args = ["tool_calls IS NOT NULL", "role = 'assistant'"], []
    for clause, values in (vendor_store._source_filter_clause("source", source),
                           vendor_store._conversation_filter_clause("conversation_id", conversation_id)):
        if clause:
            where.append(clause)
            args.extend(values)
    for clause, value in (("session_id = ?", session_id), ("timestamp >= ?", time_from), ("timestamp <= ?", time_to)):
        if value is not None:
            where.append(clause)
            args.append(value)
    hits = []
    # A connection of its own: tools run in parallel, and two threads stepping one connection raise
    # "bad parameter or other API misuse". Rows come newest first, so the scan ends as soon as it can.
    with contextlib.closing(sqlite3.connect(f"{store.db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)) as conn:
        for row in conn.execute(f"SELECT {vendor_store._MESSAGE_SELECT_COLUMNS} FROM messages "
                                f"WHERE {' AND '.join(where)} ORDER BY store_id DESC", args):
            hit = store._row_to_dict(row)
            snippet = _snippet(hit.get("tool_calls"), terms)
            if snippet is not None:
                hits.append({**hit, "snippet": snippet, "search_rank": 0.0})
                if len(hits) == limit:
                    break
    return hits


def install() -> None:
    """Let ``MessageStore.search`` find tool calls. What it found in ``content`` stays as it was."""
    original = vendor_store.MessageStore.search
    if getattr(original, "misaka_calls", False):
        return

    def search(self, query, session_id=None, limit=20, sort=None, source=None, conversation_id=None,
               role=None, time_from=None, time_to=None, allow_operators=False):
        found = original(self, query, session_id=session_id, limit=limit, sort=sort, source=source,
                         conversation_id=conversation_id, role=role, time_from=time_from, time_to=time_to,
                         allow_operators=allow_operators)
        if allow_operators:         # a query the caller composed as FTS5 syntax, not one an agent wrote
            return found
        try:
            calls = _hits(self, query, session_id=session_id, limit=limit, source=source,
                          conversation_id=conversation_id, role=role, time_from=time_from, time_to=time_to)
        except sqlite3.Error:
            logger.warning("LCM could not search tool calls.", exc_info=True)
            return found
        seen = {hit["store_id"] for hit in found}
        merged = found + [hit for hit in calls if hit["store_id"] not in seen]
        if normalize_search_sort(sort) == "recency":
            merged.sort(key=lambda hit: hit.get("timestamp") or 0, reverse=True)
        return merged[:limit]

    search.misaka_calls = True
    vendor_store.MessageStore.search = search

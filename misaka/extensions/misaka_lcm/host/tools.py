"""Upstream's ``lcm_*`` tools, on misaka's tool seam.

Both ends go through the engine rather than through ``vendor/tools.py``: upstream's own
plugin entry point takes its schemas from ``get_tool_schemas()`` and registers every name
against ``handle_tool_call``, and that dispatch is not a lookup table -- it ingests the
live turn before the handler runs, so a tool asked about something said moments ago finds
it. Calling the module-level functions directly would skip that and answer from a store
that stops at the last compaction.

The handlers are synchronous and talk to SQLite, so each call goes to a worker thread. A
synchronous handler on the event loop would stall the whole session for the length of a
full-text scan; upstream opens its store, DAG and lifecycle connections with
``check_same_thread=False`` behind their own locks, which is what makes the thread safe.

Every result leaves through ``_deliver`` and then ``fence.refence``: one exit for all of
them, so "every result is bounded, readable and fenced" is true by construction rather than
by fifteen handlers each remembering. A tool that replays -- or summarises -- text misaka had
fenced as untrusted must hand it back fenced.

The definitions are built by ``platform.toolkit`` rather than by ``ToolDefinition``
directly, for the same reason: ``promptSnippet`` is optional, and a definition without one
is dropped from the system prompt's ``Available tools:`` inventory silently -- these
tools were fully callable and named nowhere in it, while being the largest block in the
``tools`` array. ``tool_definition`` derives the snippet from the description. The prose is
upstream's, with one exception: the sentences in ``_REWRITES``, which name a Hermes tool,
platform or memory model that misaka does not have. Each is matched by upstream's exact text
and a test fails when a pin bump rewords one, so a stale instruction cannot reach the model
in silence.
"""

from __future__ import annotations

import copy
import json
import logging
import sqlite3

from misaka.core.extensions.types import ToolDefinition
from misaka.core.platform.prompt_guard import untrusted
from misaka.core.platform.toolkit import tool_definition

from . import context_engine, execution, fence, storage

logger = logging.getLogger(__name__)

# Upstream sentences that are not true in misaka, and what misaka says instead ("" removes one).
_REWRITES = {
    # Hermes's session search is a host tool misaka does not have.
    " For Hermes-tracked session history outside the LCM database, use session_search.": "",
    " For Hermes-tracked session history outside the LCM database, prefer session_search.": "",
    "\n- Hermes-tracked history outside `lcm.db`: use the host's `session_search` when available.": "",
    "Use this after session_search or lcm_grep has identified a session_id":
        "Use this after lcm_grep or lcm_recall has identified a session_id",
    "For cross-session recall, use session_search first.":
        "For other sessions in this project, use lcm_recall or lcm_grep(session_scope='all') first.",
    "for cross-session recall, use session_search first.":
        "for other sessions in this project, use lcm_recall or lcm_grep(session_scope='all') first.",
    # Hermes's memory is one agent's, across all its chats; this cache is one project's sessions.
    "Search the agent's entire memory across ALL conversations and all time by meaning.":
        "Search every session loaded into this project's LCM cache -- Last Order's and her Sisters' -- "
        "by meaning.",
    # Hermes's sources are chat platforms; here a row's source is the session that wrote it.
    "Optional raw-message role filter. When supplied, lcm_grep returns raw message hits only.":
        "Optional raw-message role filter: `user` (the user, or a work order), `assistant`, `tool`, `system`, "
        "`notice` (a system notice), or `agent:<name>` (mail from that agent: `agent:10032`, `agent:last-order`). "
        "When supplied, lcm_grep returns raw message hits only.",
    "Optional source/platform filter (for example cli, discord, telegram).":
        "Optional filter by the session that wrote the rows, named by its exact address: `last-order`, a "
        "Sister's name, `name@<card id>` for a card session, `last-order@<node id>` for a research node's "
        "Last Order, `name/<agent id>` for a sub-agent.",
    "Optional gateway conversation/session key filter for lane-scoped retrieval. Use this to restrict "
    "Discord searches to one channel/thread/forum topic lane when rows carry metadata.":
        "Optional conversation filter. A conversation is a session and the sessions continued from it "
        "with /new --carry; its id is the first session's id.",
}

# One LCM result is at most this many tokens. Upstream bounds each part of a call -- rows, characters
# per row, hits -- but not their product: a default lcm_load_session page is up to 100 rows of 4,000
# characters and its hard caps allow 200 of 20,000, so one call could pour a Sister's whole research
# session into the caller's window. 25k tokens is under a tenth of misaka's smallest (272k) window.
RESULT_BUDGET_TOKENS = 25_000


def misaka_text(text: str) -> str:
    """Upstream prose, in misaka's terms."""
    for upstream, ours in _REWRITES.items():
        text = text.replace(upstream, ours)
    return text.replace("Hermes-LCM", "MISAKA LCM").replace("Hermes", "MISAKA")


def _args_of(raw) -> dict:
    """The handler's argument dict, copied so a handler cannot write into the tool call.

    The schemas are plain JSON Schema and these definitions set no ``prepareArguments``,
    so what arrives is the model's own JSON object -- and a pydantic instance, were one
    ever handed over, iterates into the same pairs.
    """
    return dict(raw or {})


def _snapshot(ctx) -> context_engine.Transcript | None:
    """Freeze archive and branch together before entering the worker queue."""
    try:
        return context_engine.snapshot(ctx)
    # A tool blind to the live turn still reads everything already stored.
    except Exception:
        logger.debug("LCM could not read the active context for a tool call.", exc_info=True)
        return None


def _result(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "details": {}}


def _json(payload) -> str:
    """``payload`` as JSON with its text as itself: upstream's ``json.dumps`` default writes every
    non-ASCII character as a ``\\uXXXX`` escape, six characters and several tokens for one CJK
    character. A lone surrogate cannot be sent as UTF-8, so a payload holding one keeps the escapes."""
    text = json.dumps(payload, ensure_ascii=False)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(payload)
    return text


def _tokens(name: str, payload) -> int:
    """The tokens ``payload`` costs the model, counted as ``fence.refence`` may hand it over: inside
    the untrusted-data fence, every marker in it lengthened by the defang. Fenced or not, the text
    the model receives is at most this, which is what makes the budget a bound."""
    text = untrusted(f"lcm:{name}", _json(payload))
    # A UTF-8 character is at most four bytes and a token at least one, so a text this short is
    # within budget without counting; ordinary results never reach the tokenizer.
    if len(text) * 4 <= RESULT_BUDGET_TOKENS:
        return 0
    from ..vendor.tokens import count_tokens
    return count_tokens(text)


def _within_budget(name: str, payload):
    """``payload`` if it fits one result; else, for a transcript page, the leading rows that fit
    with its cursor moved past them; else a request to ask for less."""
    if _tokens(name, payload) <= RESULT_BUDGET_TOKENS:
        return payload
    rows = payload.get("messages") if isinstance(payload, dict) else None
    if (name == "lcm_load_session" and isinstance(rows, list) and rows
            and all(isinstance(row, dict) and type(row.get("store_id")) is int for row in rows)):
        def page(count):
            # Upstream's own cursor contract: `next_cursor` is the last returned row's store_id,
            # passed back as `after_store_id`.
            return {**payload, "messages": rows[:count], "returned_messages": count, "has_more": True,
                    "next_cursor": rows[count - 1]["store_id"],
                    "trimmed_to_budget": {"budget_tokens": RESULT_BUDGET_TOKENS,
                                          "dropped_messages": len(rows) - count}}
        fits, low, high = 0, 1, len(rows)  # the most leading rows that fit; a page only grows with them
        while low <= high:
            middle = (low + high) // 2
            if _tokens(name, page(middle)) <= RESULT_BUDGET_TOKENS:
                fits, low = middle, middle + 1
            else:
                high = middle - 1
        if fits:
            return page(fits)
    return {"error": (f"{name}: this result is about {_tokens(name, payload)} tokens, over the "
                      f"{RESULT_BUDGET_TOKENS}-token limit for one LCM result. Ask for less -- a lower "
                      "limit, max_content_chars or max_tokens -- or take it in pages with after_store_id, "
                      "content_offset or source_offset.")}


def _attribute(engine, payload):
    """Name the session that wrote each item a result cites by ``session_id`` alone.

    Upstream's grep, expand and transcript rows carry ``source``; ``lcm_recall``'s hits and the
    other tools' items carry only ``session_id``, and recall is the tool an agent reaches for
    across sessions. A session's rows share one source -- one engine per session, bound with its
    address -- so its newest row names it.
    """
    unnamed = []

    def walk(value):
        if isinstance(value, dict):
            if isinstance(value.get("session_id"), str) and value["session_id"] and "source" not in value:
                unnamed.append(value)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    reader = fence._reader(engine) if unnamed else None
    if reader is None:
        return payload
    conn, lock = reader
    sources = {}
    try:
        with lock:
            for chunk in fence._chunks({item["session_id"] for item in unnamed}):
                sources.update(conn.execute(
                    "SELECT session_id, source FROM messages WHERE store_id IN (SELECT MAX(store_id) FROM "
                    f"messages WHERE session_id IN ({fence._marks(chunk)}) GROUP BY session_id)", chunk).fetchall())
    except sqlite3.Error:
        logger.debug("LCM could not name the sessions a result cites.", exc_info=True)
        return payload
    for item in unnamed:
        if item["session_id"] in sources:
            item["source"] = sources[item["session_id"]]
    return payload


def _deliver(name: str, output: str, engine=None) -> str:
    """The text one upstream result reaches the model as: every cited session named, within budget,
    readable."""
    try:
        payload = json.loads(output)
    except ValueError:
        return output  # not upstream's JSON: refence fences it unread
    if engine is not None:
        payload = _attribute(engine, payload)
    return _json(_within_budget(name, payload))


def _answer(name: str, args: dict, transcript, ctx) -> str:
    built = context_engine.bound_engine(ctx)
    messages = context_engine._messages(transcript, built) if transcript is not None else None
    output = str(built.handle_tool_call(name, context_engine.local_nodes(built, args), messages=messages))
    return fence.refence(_deliver(name, output, built), engine=built, tool_name=name)


def _execute(name: str, workspace=None, source=None):
    async def execute(tool_call_id, raw, signal, on_update, ctx):
        if workspace is not None:
            ctx = storage.context(ctx, workspace, source)
        try:
            answer = await execution.off_loop(
                _answer, name, _args_of(raw), _snapshot(ctx), ctx, ctx=ctx, signal=signal)
        except Exception as exc:
            logger.warning("LCM tool %s failed.", name, exc_info=True)
            return {**_result(_json({"error": f"{name} failed: {exc}"})), "isError": True}
        return _result(answer)
    return execute


def recall_guideline() -> str:
    """Upstream's recall policy, carried by every LCM tool's guidelines.

    Hermes appends this text to the current user message on every request (its plugin hook has
    no other seam, and it keeps the system prompt cacheable). Here it is a rule about tools, so it
    lives where the other tool rules live: the system prompt's guidelines, once (the builder
    dedupes it across the tools), a stable prefix that caches just as well. Appended to
    the user turn it swallowed short messages: a one-word go-ahead followed by two thousand
    characters of policy read, to the model, as a policy note with no instruction in it -- or as
    an injection to be distrusted."""
    from ..vendor import get_recall_policy
    return ("MISAKA LCM is this project's context cache, not a permanent memory. Its tools search the sessions "
            "loaded into the cache -- Last Order's and her Sisters' -- and nothing else: it stays between runs, "
            "but a session that was never opened here is not in it, and clearing the cache empties it. "
            "Native session files stay durable; resume and carry rebuild a session's searchable context from "
            "them. Numeric handles belong to the current cache: if one is not found, the session was rebuilt, "
            "so search again for its new handle.\n\n"
            "When asked about memory, distinguish tool capability from currently available evidence. "
            "Tool availability is not evidence that earlier conversations are loaded or remembered. "
            "In lcm_status, state_sessions_total and state_only_sessions refer to native session-catalog "
            "identifiers, not conversation content stored in LCM. Use the LCM session/content counts and actual "
            "retrieval results to establish what is available; do not claim prior-session recall from catalog-only "
            "entries. Status also does not establish which project files exist or have been read.\n\n"
            # misaka's rules, not upstream's: sessions here leave work products (delivered files, card
            # notes), and one cache holds Last Order and every Sister. Stated with the tools, so they come
            # and go with the plugin instead of being written into the prompts of whatever uses them.
            "Work products come before conversations. When another session's work left products -- files it "
            "delivered, notes and findings on its card -- read those first; open its conversation only where a "
            "product is unclear or needs the context it came from: a conversation supplements its products, "
            "never replaces them. Every stored row names the session that wrote it in `source`: `last-order`, a "
            "Sister's name, `name@<card id>` for a card session (`momo@t_3fa9c1`), `last-order@<node id>` for a "
            "research node's Last Order, `name/<agent id>` for a sub-agent. A row's `role` says who spoke: `user` "
            "is the user or a work order, `agent:<name>` is mail from that agent, `notice` a system notice. `lcm_grep(query, session_scope=\"all\", source=...)` searches one of them -- the value "
            "is exact, so take it from a hit or build it from a card -- and a hit's `session_id` opens that "
            "conversation with `lcm_load_session`.\n\n"
            + misaka_text(get_recall_policy().strip()))


def _parameters(schema: dict) -> dict:
    """Upstream's JSON Schema as it stands -- restating it as a pydantic model would be a second
    copy of the contract -- with each property's description in misaka's terms."""
    parameters = copy.deepcopy(schema.get("parameters") or {"type": "object", "properties": {}})
    for name, spec in (parameters.get("properties") or {}).items():
        if isinstance(spec, dict) and isinstance(spec.get("description"), str):
            spec["description"] = misaka_text(spec["description"])
            if name == "role":
                spec.pop("enum", None)      # `agent:<name>` is no closed list
    return parameters


def _definition(schema: dict, workspace=None, source=None) -> ToolDefinition:
    """One upstream OpenAI-shaped schema, as a misaka tool definition.

    The prompt snippet is left to ``tool_definition`` to derive from the description: hand-written
    lines would be a second copy in prose, and a derived one cannot fall out of step with what the
    description says. ``executionMode`` stays unset: recall is a read, and several of these tools
    are worth running beside each other.
    """
    name = str(schema["name"])
    return tool_definition(
        name=name,
        label="LCM " + name.removeprefix("lcm_").replace("_", " "),
        description=misaka_text(str(schema.get("description") or "")),
        parameters=_parameters(schema),
        execute=_execute(name, workspace, source),
        guidelines=[recall_guideline()],
    )


def _dormant(config) -> set[str]:
    """The tools whose handler can only answer ``disabled`` under this configuration.

    Upstream registers them regardless and lets that answer say so. Here the answer would
    cost every request of every session two schemas (5.5k characters of the ``tools``
    array) for calls the session cannot make. The flags are upstream's own; set, the tools
    are registered exactly as upstream registers them.
    """
    return {name for name, enabled in (("lcm_query_state", config.assertions_enabled),
                                       ("lcm_retrieve", config.adaptive_retrieval_enabled)) if not enabled}


def register(harn, *, workspace=None, source=None) -> None:
    """Expose the upstream schemas, less the tools this configuration leaves dormant."""
    from ..vendor.config import LCMConfig
    from ..vendor.engine import LCMEngine
    from . import config_bridge

    try:
        dormant = _dormant(LCMConfig.from_env(host_config=config_bridge.load_auxiliary_config()))
    except Exception:  # an unreadable configuration keeps upstream's whole surface
        logger.warning("LCM could not read its configuration; every tool is registered.", exc_info=True)
        dormant = set()
    for schema in LCMEngine.get_tool_schemas():
        if schema["name"] not in dormant:
            harn.registerTool(_definition(schema, workspace, source))

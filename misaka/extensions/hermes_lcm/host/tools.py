"""Upstream's fifteen ``lcm_*`` tools, on misaka's tool seam.

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

Every result leaves through ``fence.refence``. A tool that replays -- or summarises --
text misaka had fenced as untrusted must hand it back fenced, and putting that at the one
exit rather than inside fifteen handlers is what makes "all fifteen" true by construction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading

from misaka.core.extensions.types import ToolDefinition

from . import context_engine, fence, ingest

logger = logging.getLogger(__name__)


def _args_of(raw) -> dict:
    """The handler's argument dict, copied so a handler cannot write into the tool call.

    The schemas are plain JSON Schema and these definitions set no ``prepareArguments``,
    so what arrives is the model's own JSON object -- and a pydantic instance, were one
    ever handed over, iterates into the same pairs.
    """
    return dict(raw or {})


def _messages(ctx) -> list[dict] | None:
    """The active context in upstream's shape, so a tool can see the current turn."""
    try:
        return ingest.upstream_messages(ctx.sessionManager.buildSessionContext().messages)
    # A tool blind to the live turn still reads everything already stored.
    except Exception:
        logger.debug("LCM could not read the active context for a tool call.", exc_info=True)
        return None


def _result(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "details": {}}


# Upstream serialises its writes and states that its reads are unlocked (`store.py`
# around `_write_lock`), which holds for one calling thread. misaka runs a tool batch in
# parallel, so without this two worker threads step statements on the same sqlite3
# connection: that raises `InterfaceError: bad parameter or other API misuse` and, worse,
# hands one thread's row to the other's cursor. One caller at a time, off the loop either
# way -- the point of the worker thread is that the session keeps running, not that two
# recalls overlap.
_ENGINE_LOCK = threading.Lock()


def _answer(engine, name: str, args: dict, messages) -> str:
    """One handler call and the fence check on its result, both off the event loop.

    The check reads the same database the handler just did, so it belongs in the same
    worker thread rather than back on the loop.
    """
    with _ENGINE_LOCK:
        output = str(engine.handle_tool_call(name, args, messages=messages))
        return fence.refence(output, engine=engine, tool_name=name)


def _execute(name: str):
    """One tool's ``execute``, bound to the upstream name it dispatches."""

    async def execute(tool_call_id, raw, signal, on_update, ctx):
        try:
            engine = context_engine.engine()
            if engine is None:
                return _result(json.dumps({"error": "LCM engine not initialized"}))
            # One engine per database serves every session in this process, and the
            # handlers read whichever session it is bound to. A card asking about its
            # own history has to be that session first.
            session_id = ingest.session_id(ctx)
            if session_id and engine.current_session_id != session_id:
                context_engine.start(ctx)
            answer = await asyncio.to_thread(
                _answer, engine, name, _args_of(raw), _messages(ctx)
            )
        # A failed recall is an answer the model can work with; a raised one ends the
        # turn -- and an error carries no retrieved text, so nothing escapes the fence.
        except Exception as exc:
            logger.warning("LCM tool %s failed.", name, exc_info=True)
            return _result(json.dumps({"error": f"{name} failed: {exc}"}))
        return _result(answer)

    return execute


def _definition(schema: dict) -> ToolDefinition:
    """One upstream OpenAI-shaped schema, as a misaka tool definition.

    ``parameters`` takes the upstream dict as it stands -- it is already JSON Schema, and
    restating it as a pydantic model would be a second copy of the contract to keep in
    step with upstream. ``executionMode`` stays unset: recall is a read, and several of
    these tools are worth running beside each other.
    """
    name = str(schema["name"])
    return ToolDefinition(
        name=name,
        label="LCM " + name.removeprefix("lcm_").replace("_", " "),
        description=str(schema.get("description") or ""),
        parameters=schema.get("parameters") or {"type": "object", "properties": {}},
        execute=_execute(name),
    )


def register(harn) -> None:
    """Register whatever tools this engine build offers, or none if it has no engine."""
    engine = context_engine.engine()
    if engine is None:
        logger.warning("LCM has no usable engine; its tools are not registered.")
        return
    for schema in engine.get_tool_schemas():
        harn.registerTool(_definition(schema))

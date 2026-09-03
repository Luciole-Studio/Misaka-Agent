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

The definitions are built by ``platform.toolkit`` rather than by ``ToolDefinition``
directly, for the same reason: ``promptSnippet`` is optional, and a definition without one
is dropped from the system prompt's ``Available tools:`` inventory silently -- these
fifteen were fully callable and named nowhere in it, while being the largest block in the
``tools`` array. ``tool_definition`` derives the snippet from the description, which is
what keeps the fix here at zero restatements: this module says nothing about what any of
the fifteen do, so upstream can rename or re-word a tool without a second copy going stale.
"""

from __future__ import annotations

import asyncio
import json
import logging

from misaka.core.extensions.types import ToolDefinition
from misaka.platform.toolkit import tool_definition

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


# misaka runs a tool batch in parallel, so two recalls can be in flight at once. They
# queue on the one engine lock, which now also holds back the session-event handlers
# (`extension._off_loop`) -- the reason it lives in `context_engine` rather than here.
_ENGINE_LOCK = context_engine.ENGINE_LOCK


def _answer(engine, name: str, args: dict, messages, rebind=None) -> str:
    """One handler call and the fence check on its result, both off the event loop.

    The check reads the same database the handler just did, so it belongs in the same
    worker thread rather than back on the loop. So does the rebind: it is a write on the
    connections every other holder of this lock is using, and doing it on the loop meant
    it was the one engine call not covered by the lock.
    """
    with _ENGINE_LOCK:
        if rebind is not None:
            context_engine.start(rebind)
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
            rebind = ctx if session_id and engine.current_session_id != session_id else None
            answer = await asyncio.to_thread(
                _answer, engine, name, _args_of(raw), _messages(ctx), rebind
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
    step with upstream. The prompt snippet is left to ``tool_definition`` to derive from
    the description for the same reason: fifteen hand-written lines would be that second
    copy in prose, and a derived one cannot fall out of step with what upstream says.
    ``executionMode`` stays unset: recall is a read, and several of these tools are worth
    running beside each other.
    """
    name = str(schema["name"])
    return tool_definition(
        name=name,
        label="LCM " + name.removeprefix("lcm_").replace("_", " "),
        description=str(schema.get("description") or ""),
        parameters=schema.get("parameters") or {"type": "object", "properties": {}},
        execute=_execute(name),
    )


# Which of the fifteen a session is offered is a fact about the session, not about the
# engine, so it is decided in ``withheld`` and handed to ``register`` rather than read
# inside it: the adapter registers what it is given, and the tests that pin "all
# fifteen" keep meaning exactly that.

# Health, lineage and diagnostics of the store itself. They answer a person at the
# keyboard asking "is memory working"; a Sister on a card, or a role answering mail,
# has no use for a database check-up beside her research tools -- and every line in
# the ``tools`` array is paid for on every request.
_OPERATOR_TOOLS = frozenset({"lcm_status", "lcm_inspect", "lcm_doctor"})

# The one tool that cannot answer without the V4 assertion sidecar: it reads
# ``engine._assertions`` and returns "not enabled for this profile" when there is none
# (vendor/tools.py ``lcm_query_state``). ``lcm_compute`` reaches for the sidecar too but
# grounds on the message store without it, so it stays. A tool whose only possible
# answer is an error is worse offered than withheld.
_SIDECAR_TOOLS = frozenset({"lcm_query_state"})


def withheld(kind: str, engine) -> frozenset[str]:
    """The upstream tool names a session of ``kind`` is not offered.

    ``engine`` is what ``context_engine.engine()`` returned; ``None`` withholds nothing,
    since nothing will be registered either. The sidecar flag is read off the engine's
    own config the way ``context_engine.compact`` reads it -- upstream's plugin reaches
    it as ``_config`` and there is no other accessor.
    """
    if engine is None:
        return frozenset()
    names: set[str] = set()
    if kind != "foreground":
        names |= _OPERATOR_TOOLS
    if not bool(getattr(getattr(engine, "_config", None), "assertions_enabled", False)):
        names |= _SIDECAR_TOOLS
    return frozenset(names)


def register(harn, *, withhold: frozenset[str] = frozenset()) -> None:
    """Register the tools this engine build offers, less ``withhold``; none if it has no engine."""
    engine = context_engine.engine()
    if engine is None:
        logger.warning("LCM has no usable engine; its tools are not registered.")
        return
    for schema in engine.get_tool_schemas():
        if schema["name"] in withhold:
            continue
        harn.registerTool(_definition(schema))

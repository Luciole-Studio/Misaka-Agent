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

import json
import logging

from misaka.core.extensions.types import ToolDefinition
from misaka.core.platform.toolkit import tool_definition

from . import context_engine, execution, fence

logger = logging.getLogger(__name__)


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


def _answer(name: str, args: dict, transcript, ctx) -> str:
    built = context_engine.bound_engine(ctx)
    messages = context_engine._messages(transcript, built) if transcript is not None else None
    output = str(built.handle_tool_call(name, args, messages=messages))
    return fence.refence(output, engine=built, tool_name=name)


def _execute(name: str):
    async def execute(tool_call_id, raw, signal, on_update, ctx):
        try:
            answer = await execution.off_loop(
                _answer, name, _args_of(raw), _snapshot(ctx), ctx, ctx=ctx, signal=signal)
        except Exception as exc:
            logger.warning("LCM tool %s failed.", name, exc_info=True)
            return _result(json.dumps({"error": f"{name} failed: {exc}"}))
        return _result(answer)
    return execute


def recall_guideline() -> str:
    """Upstream's recall policy, carried by every LCM tool's guidelines.

    Hermes appends this text to the current user message on every request (its plugin hook has
    no other seam, and it keeps the system prompt cacheable). Here it is a rule about tools, so it
    lives where the other tool rules live: the system prompt's guidelines, once (the builder
    dedupes it across the fifteen tools), a stable prefix that caches just as well. Appended to
    the user turn it swallowed short messages: a one-word go-ahead followed by two thousand
    characters of policy read, to the model, as a policy note with no instruction in it -- or as
    an injection to be distrusted."""
    from ..vendor import get_recall_policy
    return get_recall_policy().strip()


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
        guidelines=[recall_guideline()],
    )


def register(harn) -> None:
    """Expose the same fifteen schemas as the upstream plugin, without local filtering."""
    from ..vendor.engine import LCMEngine

    for schema in LCMEngine.get_tool_schemas():
        harn.registerTool(_definition(schema))

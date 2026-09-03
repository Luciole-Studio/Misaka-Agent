"""One registration seam for MISAKA's own tools, so no tool can go un-advertised.

A tool reaches the model down two channels. The ``tools`` array carries
``description`` and the parameter schema, and both are required by the dataclass,
so a tool that reaches the request is always fully documented there. The system
prompt's ``Available tools:`` inventory carries ``promptSnippet``, and that field
is optional: ``build_system_prompt`` filters out every tool without one, and it
does so silently. A tool can therefore be completely callable and completely
absent from the inventory the model reads before it decides what to reach for.

That is not hypothetical. The fifteen ``lcm_*`` tools -- the most expensive block
in the request, half the ``tools`` array by bytes -- were registered straight from
upstream's JSON Schema with no snippet, and so were invisible in every session's
inventory while being fully callable.

The kernel's filter is Pi's and stays as it is. This seam closes the gap on our
side: ``register_tool`` always produces a snippet, deriving one from the
description when the caller does not write a better one, and
``tests/test_role_tool_consistency.py`` fails the build if any registered tool
still misses the inventory.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from misaka.core.extensions.types import ToolDefinition

# Inventory lines sit one per tool in every request; the existing hand-written ones
# run 40-90 characters. Past this a derived line is padding the prompt, not naming
# the tool, so it is cut at the last word that fits.
_MAX_SNIPPET_CHARS = 120

# A first sentence shorter than this is an abbreviation ("e.g.", "cf.") rather than
# a sentence, so the scan keeps going instead of advertising a fragment.
_MIN_SENTENCE_CHARS = 24

_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


def derive_snippet(description: str) -> str:
    """A one-line inventory entry derived from a tool's description.

    The first real sentence, unpunctuated and bounded. Returns ``""`` for an empty
    description -- a tool with nothing to say about itself gets no derived line,
    and the consistency test reports it rather than this quietly inventing one.
    """
    text = " ".join(str(description or "").split())
    if not text:
        return ""
    for match in _SENTENCE_END.finditer(text):
        candidate = text[: match.start()].strip()
        if len(candidate) >= _MIN_SENTENCE_CHARS:
            text = candidate
            break
    else:
        text = text.rstrip()
    text = text.rstrip(".")
    if len(text) > _MAX_SNIPPET_CHARS:
        head = text[: _MAX_SNIPPET_CHARS + 1]
        # A sentence too long to keep is cut at a clause boundary when one falls in
        # its back half, so the line ends on a whole thought ("...WITHOUT loading full
        # content...") rather than on whatever word the limit landed after ("...or
        # inspect an externalized"). Only a word boundary otherwise; the trailing dots
        # say the tool has more to it than this line.
        cut = max(head.rfind(", "), head.rfind("; "), head.rfind(": "))
        if cut < _MAX_SNIPPET_CHARS // 2:
            cut = head.rfind(" ")
        text = (head[:cut] if cut > 0 else text[:_MAX_SNIPPET_CHARS]).rstrip(" ,;:-") + "..."
    return text


def tool_definition(
    *,
    name: str,
    label: str,
    description: str,
    parameters: Any,
    execute: Callable[..., Any],
    snippet: str | None = None,
    guidelines: Iterable[str] | None = None,
) -> ToolDefinition:
    """A ``ToolDefinition`` that is guaranteed to reach the prompt inventory.

    For tool sources that build their definitions rather than decorating a
    handler -- the ``lcm_*`` adapter generates fifteen from upstream schemas.
    """
    return ToolDefinition(
        name=name,
        label=label,
        description=description,
        parameters=parameters,
        execute=execute,
        promptSnippet=snippet or derive_snippet(description) or None,
        promptGuidelines=list(guidelines or []),
    )


def register_tool(
    harn: Any,
    *,
    name: str,
    label: str,
    description: str,
    parameters: Any,
    snippet: str | None = None,
    guidelines: Sequence[str] | None = None,
    schema: Callable[[Any], Any] | None = None,
    before: Callable[[Any, Any], Any] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: register ``fn`` as a harness tool, parsing raw arguments first.

    ``parameters`` is a pydantic model; ``schema`` overrides how its JSON Schema is
    produced, and ``before(ctx, args)`` runs after parsing and before the handler
    for the callers that gate on session context.
    """
    to_schema = schema or (lambda model: model.model_json_schema())

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        async def execute(tool_call_id, raw, signal, on_update, ctx):
            args = raw if isinstance(raw, parameters) else parameters(**(raw or {}))
            if before is not None:
                before(ctx, args)
            return await fn(tool_call_id, args, signal, on_update, ctx)

        harn.registerTool(
            tool_definition(
                name=name,
                label=label,
                description=description,
                parameters=to_schema(parameters),
                execute=execute,
                snippet=snippet,
                guidelines=guidelines,
            )
        )
        return fn

    return deco


__all__ = ["derive_snippet", "register_tool", "tool_definition"]

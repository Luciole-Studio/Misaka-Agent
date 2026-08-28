"""Deferred tool splitting, translated from pi's ``utils/deferred-tools.ts``.

Tools added mid-conversation and never called still ride along in the request's tool list
on every call. Upstream hands those to whatever deferred-loading mechanism the provider
offers instead, minus the ones that already appear in a tool call
(``deferred-tools.ts:19-30``).

The split is a classification only; what the caller does with ``deferred`` is the
provider's business. In pi, ``api/openai-responses.ts`` puts only ``immediate`` in
``params.tools`` (``openai-responses.ts:312-313``) and injects the deferred definitions
into the transcript as ``additional_tools`` / ``tool_search_output`` items
(``openai-responses-shared.ts:321-345``), while ``api/anthropic-messages.ts`` sends both
sets in ``tools``, the deferred ones with ``defer_loading: true``
(``anthropic-messages.ts:1041-1057``, ``convertTools`` at 1332/1359), plus a
``tool_reference`` block in the transcript where each was added
(``anthropic-messages.ts:1127-1135``).

``normalize_name`` lets the name in a transcript be matched against the name in the tool
list under a single spelling. Of upstream's three callers
(``openai-responses.ts:277``, ``openai-codex-responses.ts:536``,
``anthropic-messages.ts:983-987``), it is ``anthropic-messages.ts`` that passes one: on an
OAuth token it folds tool names onto Claude Code's canonical casing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from misaka.ai.types import Context, Tool


@dataclass(slots=True)
class SplitTools:
    # Definitions sent with every request.
    immediate: list[Tool]
    # Definitions for the caller to hand to the provider's deferred-loading mechanism,
    # keyed by normalized name.
    deferred: dict[str, Tool]


def split_deferred_tools(
    context: Context,
    enabled: bool,
    normalize_name: Callable[[str], str] | None = None,
) -> SplitTools:
    """Split the current tools into prefix definitions and transcript-loaded ones."""
    normalize: Callable[[str], str] = normalize_name or (lambda name: name)

    unique: dict[str, Tool] = {}
    for tool in context.tools or []:
        unique[normalize(tool.name)] = tool
    if not enabled:
        return SplitTools(immediate=list(unique.values()), deferred={})

    deferred_names: set[str] = set()
    used_names: set[str] = set()
    for message in context.messages:
        role = getattr(message, "role", None)
        if role == "assistant":
            for block in message.content:
                if getattr(block, "type", None) == "toolCall":
                    used_names.add(normalize(block.name))
        elif role == "toolResult":
            for name in _added_tool_names(message):
                normalized = normalize(name)
                # A tool the model already called is not deferred.
                if normalized not in used_names:
                    deferred_names.add(normalized)

    immediate: list[Tool] = []
    deferred: dict[str, Tool] = {}
    for name, tool in unique.items():
        if name in deferred_names:
            deferred[name] = tool
        else:
            immediate.append(tool)
    return SplitTools(immediate=immediate, deferred=deferred)


def _added_tool_names(message: Any) -> list[str]:
    return getattr(message, "addedToolNames", None) or []


__all__ = ["SplitTools", "split_deferred_tools"]

"""MISAKA fork: the thinking a provider adapter requests beyond ``maxTokens`` (no upstream file; pi has no token cap).

The token-cap meter (``misaka.core.platform.metering``) budgets a request from it. The adapter modules are imported
inside the function, so loading this module does not load any SDK.
"""

from __future__ import annotations

from misaka.ai.api.simple_options import thinkingBudgetForLevel
from misaka.ai.types import Model, SimpleStreamOptions


def thinking_allowance(model: Model, options: SimpleStreamOptions | None) -> int:
    """Thinking a provider adapter requests *beyond* ``maxTokens`` for this request, in tokens.

    Two adapters add one: budget-thinking Claude (``anthropic-messages``, Bedrock), whose wire
    ``max_tokens`` is ``min(base + budget, model max)`` (``adjustMaxTokensForThinking``), and
    Gemini 2.5, whose ``thinkingBudget`` is not counted in ``maxOutputTokens`` (a dynamic budget,
    ``-1``, is taken as the model's whole output). Every other adapter -- adaptive Claude, the
    OpenAI APIs, DeepSeek (its ``max_tokens`` covers the reasoning, api-docs.deepseek.com),
    Mistral -- counts thinking inside ``maxTokens``. The one place these rules are written down.
    """
    from misaka.utils.values import read_field

    reasoning = read_field(options, "reasoning") if options is not None else None
    if not reasoning or reasoning == "off" or not getattr(model, "reasoning", False):
        return 0
    budgets = read_field(options, "thinkingBudgets")
    if model.api == "anthropic-messages":
        from misaka.ai.api._common import _compat_value

        if _compat_value(model, "forceAdaptiveThinking") is True:
            return 0
        return thinkingBudgetForLevel(reasoning, budgets)
    if model.api == "bedrock-converse-stream":
        from misaka.ai.api.bedrock_converse_stream import (
            _isAnthropicClaudeModel,
            _supportsAdaptiveThinking,
        )

        if _isAnthropicClaudeModel(model) and not _supportsAdaptiveThinking(model.id, model.name):
            return thinkingBudgetForLevel(reasoning, budgets)
        return 0
    if model.api in ("google-generative-ai", "google-vertex"):
        from misaka.ai.api.google_shared import (
            resolveGoogleThinkingLevel,
            usesGoogleThinkingLevel,
        )
        from misaka.ai.models import clampThinkingLevel

        # Each adapter keeps its own budget table (Vertex has no 2.5-flash-lite row).
        if model.api == "google-generative-ai":
            from misaka.ai.api.google_generative_ai import _getGoogleBudget
        else:
            from misaka.ai.api.google_vertex import _getGoogleBudget

        clamped = clampThinkingLevel(model, reasoning)
        if clamped == "off" or usesGoogleThinkingLevel(model):
            return 0
        budget = _getGoogleBudget(model, resolveGoogleThinkingLevel(model, clamped), budgets)
        return int(model.maxTokens or 0) if budget == -1 else max(0, int(budget))
    return 0


__all__ = ["thinking_allowance"]

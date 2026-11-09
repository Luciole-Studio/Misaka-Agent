"""The token-cap meter's lease covers the thinking an adapter adds beyond ``maxTokens``
(``_thinking_allowance.thinking_allowance``), on the ported provider adapters."""
import pytest

from misaka.ai.api._thinking_allowance import thinking_allowance
from misaka.ai.api.simple_options import thinkingBudgetForLevel
from misaka.ai.models_generated import MODELS
from misaka.ai.types import SimpleStreamOptions


@pytest.mark.parametrize("options", [SimpleStreamOptions(reasoning="high"), {"reasoning": "high"}])
def test_budget_thinking_claude_adds_its_budget(options):
    model = MODELS["anthropic"]["claude-sonnet-4-5"]
    assert thinking_allowance(model, options) == thinkingBudgetForLevel("high")


def test_adaptive_claude_counts_thinking_inside_max_tokens():
    model = MODELS["anthropic"]["claude-opus-4-6"]
    assert model.compat is not None and model.compat.forceAdaptiveThinking is True
    assert thinking_allowance(model, {"reasoning": "high"}) == 0


def test_gemini_2_5_adds_its_thinking_budget():
    assert thinking_allowance(MODELS["google"]["gemini-2.5-flash"], {"reasoning": "high"}) == 24576


@pytest.mark.parametrize("reasoning", [None, "off"])
def test_no_reasoning_adds_nothing(reasoning):
    assert thinking_allowance(MODELS["anthropic"]["claude-sonnet-4-5"], {"reasoning": reasoning}) == 0


def test_other_apis_count_thinking_inside_max_tokens():
    assert thinking_allowance(MODELS["openai"]["gpt-5"], {"reasoning": "high"}) == 0


def test_vertex_reads_its_own_budget_table():
    """Vertex has no 2.5-flash-lite row, so its minimal budget is the 2.5-flash one."""
    assert thinking_allowance(MODELS["google"]["gemini-2.5-flash-lite"], {"reasoning": "minimal"}) == 512
    assert thinking_allowance(MODELS["google-vertex"]["gemini-2.5-flash-lite"], {"reasoning": "minimal"}) == 128


def test_budget_thinking_bedrock_claude_adds_its_budget_and_adaptive_bedrock_claude_does_not():
    budget = MODELS["amazon-bedrock"]["global.anthropic.claude-sonnet-4-5-20250929-v1:0"]
    adaptive = MODELS["amazon-bedrock"]["global.anthropic.claude-opus-5-5"]
    assert thinking_allowance(budget, {"reasoning": "high"}) == thinkingBudgetForLevel("high")
    assert thinking_allowance(adaptive, {"reasoning": "high"}) == 0


def test_a_dynamic_gemini_budget_takes_the_models_whole_output():
    """A Gemini 2.5 model with no budget row asks for a dynamic budget (``-1``)."""
    model = MODELS["google"]["gemini-2.5-computer-use-preview-10-2025"]
    assert thinking_allowance(model, {"reasoning": "high"}) == model.maxTokens

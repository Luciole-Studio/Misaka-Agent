"""A summary asks for no output limit.

Upstream sizes the limit for the text, but adaptive thinking spends the same limit. On a real run
27 of 36 summary calls stopped at it: the cut text was accepted as a summary, and when nothing was
left for the text the route's circuit opened for five minutes.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from misaka.extensions.misaka_lcm.host import llm
from misaka.extensions.misaka_lcm.vendor import escalation, tools

THINKING = 4_000    # what the model spends before it writes a word


@pytest.fixture
def sent(monkeypatch):
    llm.install()
    calls = []

    def call_llm(**kwargs):
        calls.append(kwargs)
        limit = kwargs.get("max_tokens")
        text = "" if limit is not None and limit <= THINKING else "The whole summary."
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])

    monkeypatch.setattr(sys.modules["agent.auxiliary_client"], "call_llm", call_llm)
    return calls


def test_a_summary_sends_no_output_limit(sent):
    summary, level = escalation.summarize_with_escalation("source text " * 200, 800, 2000, timeout=5)
    assert (summary, level) == ("The whole summary.", 1)
    assert sent[0]["max_tokens"] is None
    assert sent[0]["timeout"] == 5


def test_thinking_does_not_open_the_route_s_circuit(sent):
    breaker = escalation.SummaryCircuitBreaker()
    for _ in range(3):
        escalation.summarize_with_escalation("source text " * 200, 800, 2000, circuit_breaker=breaker)
    assert breaker.allows("")
    assert len(sent) == 3


def test_an_expand_query_keeps_the_limit_its_caller_asked_for(sent):
    tools._synthesize_expansion_answer(prompt="q", context_blocks=[], model="", max_tokens=9_000, timeout=5)
    assert sent[0]["max_tokens"] == 9_000


def test_installing_twice_wraps_the_call_once():
    llm.install()
    first = escalation._call_llm_for_summary
    llm.install()
    assert escalation._call_llm_for_summary is first

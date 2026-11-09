"""The sub-agent small-fast tier knows pi 1.1.0's `azure` provider id (renamed from `azure-openai-responses`)."""

from misaka.ai.compat import getModel, getModels
from misaka.core.subagent.child import _small_fast_hook_model


def test_an_azure_parent_gets_the_azure_mini_model(monkeypatch):
    monkeypatch.delenv("OPENAI_SMALL_FAST_MODEL", raising=False)
    parent = getModel("azure", "gpt-5.4")
    picked = _small_fast_hook_model(parent, list(getModels("azure")))
    assert (picked.provider, picked.id) == ("azure", "gpt-5.4-mini")


def test_an_azure_parent_honours_the_openai_small_fast_override(monkeypatch):
    monkeypatch.setenv("OPENAI_SMALL_FAST_MODEL", "gpt-5-mini")
    parent = getModel("azure", "gpt-5.4")
    picked = _small_fast_hook_model(parent, list(getModels("azure")))
    assert (picked.provider, picked.id) == ("azure", "gpt-5-mini")

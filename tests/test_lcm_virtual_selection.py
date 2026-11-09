"""Under a virtual model selection, LCM keeps the window of the model the last request went to.

The live context's model is the virtual selection, which has no window; the request's routed model reaches LCM when
the request is prepared. Rebinding to the virtual model between hooks dropped the window to 0 and the usage anchor
with it."""

from types import SimpleNamespace

from misaka.ai.types import Model
from misaka.core.virtual_models import VirtualModelDefinition, createVirtualModel
from misaka.extensions.misaka_lcm.host import context_engine

PHYSICAL = Model.model_validate({
    "id": "large", "name": "large", "api": "openai-completions", "provider": "faux", "baseUrl": "https://x.test",
    "reasoning": False, "input": ["text"], "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    "contextWindow": 200_000, "maxTokens": 8000,
})
VIRTUAL = createVirtualModel(VirtualModelDefinition(provider="router", id="auto", name="Auto", route=lambda request: None))


def test_a_virtual_selection_keeps_the_routed_models_binding(monkeypatch):
    bound = []
    built = SimpleNamespace(model="large", raw_context_length=200_000, base_url="https://x.test", provider="faux",
                            api_mode="openai-completions", _usage_anchor="anchor",
                            update_model=lambda *args, **kwargs: bound.append(args))
    monkeypatch.setattr(context_engine, "engine", lambda ctx: built)

    context_engine.update_model(SimpleNamespace(model=VIRTUAL))
    assert bound == [] and built._usage_anchor == "anchor"

    context_engine.update_model(SimpleNamespace(model=PHYSICAL))
    assert bound == [] and built._usage_anchor == "anchor"   # unchanged: the same model

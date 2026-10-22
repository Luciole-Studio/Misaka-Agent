"""An OpenRouter model leaves MISAKA's catalog when the user's key may not use it (pi 1.2 per-key lists,
``withPersonalizedCatalog``). Every "this model is not there" error for a pinned OpenRouter model says so
(``personalizedCatalogHint``), not only the Sister, role and MoA ones."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from misaka.ai.compat import getModels
from misaka.core.subagent.runtime import SubagentManager


async def _resolve(reference: str) -> None:
    models = list(getModels("openrouter")) + list(getModels("anthropic"))
    manager = SimpleNamespace(role_context=SimpleNamespace(model_override=None), _current_permission_mode=lambda: None)
    context = SimpleNamespace(
        model=models[0],
        # The key's list no longer has the model: the registry does not find it.
        modelRegistry=SimpleNamespace(getAll=lambda: models, find=lambda _provider, _id: None,
                                      hasConfiguredAuth=lambda _model: True),
    )
    await SubagentManager._resolve_task_model(manager, context, reference, None)


async def test_an_unknown_openrouter_sub_agent_model_points_at_the_keys_model_restrictions():
    model = next(entry for entry in getModels("openrouter") if entry.type == "chat")
    with pytest.raises(ValueError, match=r"Unknown sub-agent model: openrouter/.*OpenRouter API key may use"):
        await _resolve(f"openrouter/{model.id}")


async def test_other_providers_get_no_openrouter_hint():
    model = getModels("anthropic")[0]
    with pytest.raises(ValueError) as raised:
        await _resolve(f"anthropic/{model.id}")
    assert str(raised.value) == f"Unknown sub-agent model: anthropic/{model.id}"

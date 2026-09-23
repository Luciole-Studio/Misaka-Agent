"""Pinning a model on a new Sister (`misaka create --model`, `/create`).

A pin is stored as ``provider/id``. The menu used to offer bare IDs and the writer used to
split whatever it got at the first slash, so every concrete menu choice failed after the
profile files were already on disk, and a gateway ID such as ``anthropic/claude-opus-4`` was
silently pinned to the ``anthropic`` provider. These tests pin the repaired contract.
"""

from __future__ import annotations

import json
import os

import pytest

from misaka.core.network import roster


@pytest.fixture
def product_provider(monkeypatch):
    """A product provider with configured auth so the menu has something to list."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture")
    monkeypatch.setattr(roster, "current_config", lambda: {"provider": "anthropic"})
    return "anthropic"


def _pin(root: str, sid: str) -> dict:
    with open(os.path.join(root, sid, "settings.json"), encoding="utf-8") as handle:
        data = json.load(handle)
    return {"defaultProvider": data.get("defaultProvider"), "defaultModel": data.get("defaultModel")}


def test_the_menu_offers_provider_qualified_references(product_provider):
    choices = roster.model_choices()
    assert choices[0] == roster.DEFAULT_CHOICE and choices[-1] == roster.CUSTOM_CHOICE
    concrete = choices[1:-1]
    assert concrete and all(item.startswith(f"{product_provider}/") for item in concrete)
    # Every menu entry is a valid pin as-is.
    for item in concrete:
        assert roster.resolve_pin(item) == item


def test_an_invalid_pin_creates_nothing(tmp_path, product_provider):
    ok, message = roster.create_sister("10077", root=str(tmp_path), specialty="x", model="no-such-model")
    assert ok is False and "Unknown model" in message
    assert not os.path.exists(tmp_path / "10077")
    # The ID is still free: a retry with a good pin succeeds.
    ok, message = roster.create_sister("10077", root=str(tmp_path), model="anthropic/claude-opus-5")
    assert ok is True and "Pinned model: anthropic/claude-opus-5" in message


def test_a_bare_id_pins_on_the_product_provider(tmp_path, product_provider):
    ok, _message = roster.create_sister("10078", root=str(tmp_path), model="claude-opus-5")
    assert ok is True
    assert _pin(str(tmp_path), "10078") == {"defaultProvider": "anthropic", "defaultModel": "claude-opus-5"}


def test_a_gateway_id_with_slashes_pins_on_the_gateway_not_the_vendor(tmp_path, product_provider):
    from misaka.ai.models import get_models, get_providers

    # A raw ID that exists on exactly one provider and whose first segment names another
    # real provider: the shape that used to be split and pinned to the vendor's account.
    providers = set(get_providers())
    owners: dict[str, set[str]] = {}
    for provider in providers:
        for model in get_models(provider):
            owners.setdefault(model.id, set()).add(provider)
    raw = next(
        model_id
        for model_id, holders in sorted(owners.items())
        if len(holders) == 1 and "/" in model_id and model_id.split("/", 1)[0] in providers
        and model_id.split("/", 1)[0] not in holders
    )
    gateway = next(iter(owners[raw]))
    ok, message = roster.create_sister("10079", root=str(tmp_path), model=raw)
    assert ok is True, message
    assert _pin(str(tmp_path), "10079") == {"defaultProvider": gateway, "defaultModel": raw}
    assert f"Pinned model: {gateway}/{raw}" in message


def test_an_ambiguous_raw_id_names_the_candidates_instead_of_guessing(tmp_path, product_provider):
    ok, message = roster.create_sister("10080", root=str(tmp_path), model="openai/gpt-oss-120b")
    assert ok is False and "Ambiguous model" in message and "groq/openai/gpt-oss-120b" in message
    assert not os.path.exists(tmp_path / "10080")

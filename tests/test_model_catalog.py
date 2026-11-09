"""The built-in model catalog is pi-ai's published data, read the way pi reads it."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from misaka.ai.compat import getModel
from misaka.ai.model_catalog import _flattenModelCatalog
from misaka.ai.models_generated import CLASSIFIER_MODELS, IMAGE_MODELS, MODELS
from misaka.ai.providers.all import _modelDataManifest as MODEL_DATA_MANIFEST
from misaka.ai.providers.all import getBuiltinModelDataGeneratedAt
from misaka.ai.types import Model
from misaka.core.model_resolver import defaultModelPerProvider

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "misaka" / "ai" / "providers" / "data"


def _script():
    spec = importlib.util.spec_from_file_location("refresh_model_catalog", ROOT / "scripts" / "refresh_model_catalog.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _provider_files():
    return sorted(path for path in DATA.glob("*.json") if not path.name.startswith("."))


def test_the_data_is_pi_s_as_published():
    script = _script()
    assert MODEL_DATA_MANIFEST["schemaVersion"] == script.SCHEMA_VERSION
    assert {path.name for path in _provider_files()} == set(MODEL_DATA_MANIFEST["files"])
    for name, digest in MODEL_DATA_MANIFEST["files"].items():
        assert hashlib.sha256((DATA / name).read_bytes()).hexdigest() == digest, name


def test_the_catalog_modules_are_what_the_script_writes():
    script = _script()
    rendered = script.render(MODEL_DATA_MANIFEST)
    for path, text in rendered.items():
        assert path.read_text(encoding="utf-8") == text, path.name
    assert set(script.generated_modules()) == set(rendered) - {script.TARGET}


@pytest.mark.parametrize(("table", "type"), [(MODELS, "chat"), (IMAGE_MODELS, "image"), (CLASSIFIER_MODELS, "classifier")])
def test_every_model_of_every_type_is_loaded_exactly_as_pi_ships_it(table, type):
    # Every provider is listed for every type, as pi's tables are (typesafe has no chat model).
    for path in _provider_files():
        raw_models = _flattenModelCatalog(json.loads(path.read_text(encoding="utf-8")), type)
        assert list(table[path.stem]) == list(raw_models), path.stem  # pi's file order
        for model_id, raw in raw_models.items():
            assert table[path.stem][model_id].model_dump(mode="json", exclude_none=True) == raw


def test_an_id_that_is_both_a_chat_and_an_image_model_stays_two_models():
    assert "google/gemini-3-pro-image" in MODELS["openrouter"]
    assert "google/gemini-3-pro-image" in IMAGE_MODELS["openrouter"]
    assert MODELS["typesafe"] == {} and list(CLASSIFIER_MODELS["typesafe"]) == ["jev-latest"]


def test_every_default_model_is_in_the_catalog():
    """pi drops models between releases; a default that went with them fails every sign-in."""
    missing = {provider: model_id for provider, model_id in defaultModelPerProvider.items()
               if getModel(provider, model_id) is None}
    assert missing == {}


def test_the_generation_stamp_comes_from_the_manifest():
    assert getBuiltinModelDataGeneratedAt() is not None


def test_a_model_s_type_is_chat_or_unset():
    base = next(iter(MODELS["anthropic"].values())).model_dump(exclude_none=True)
    assert Model.model_validate({**base, "type": "chat"}).type == "chat"
    assert Model.model_validate({key: value for key, value in base.items() if key != "type"}).type is None
    with pytest.raises(ValidationError):
        Model.model_validate({**base, "type": "image"})

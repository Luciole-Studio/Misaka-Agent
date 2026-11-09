"""models.json loading (``misaka/core/model_config.py``), after pi's ``model-config.ts`` (v1.3.0) and the runtime
cases of its ``config-schemas.test.ts`` (the generated schema's are in test_config_schemas.py)."""

from __future__ import annotations

import json

from misaka.core.model_config import ModelConfig
from misaka.utils.json import stripJsonComments


def _write(tmp_path, value) -> str:
    path = tmp_path / "models.json"
    path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
    return str(path)


async def test_missing_path_or_file_is_an_empty_config_without_error(tmp_path):
    for path in (None, "", str(tmp_path / "missing.json")):
        config = await ModelConfig.load(path)
        assert config.getProviderIds() == [] and config.getError() is None


async def test_accepts_comments_trailing_commas_and_a_bom(tmp_path):
    text = '﻿{\n  // providers\n  "providers": {"demo": {"baseUrl": "https://x", "models": [{"id": "m"},],},},\n}'
    config = await ModelConfig.load(_write(tmp_path, text))

    assert config.getError() is None
    assert config.getProviderIds() == ["demo"]
    assert config.getProvider("demo").models[0].id == "m"


async def test_thinking_level_map_takes_null_and_compat_keeps_custom_keys(tmp_path):
    config = await ModelConfig.load(_write(tmp_path, {"providers": {"demo": {
        "api": "openai-completions",
        "compat": {"supportsEagerToolInputStreaming": False, "notYetKnown": 1},
        "models": [{
            "id": "m",
            "thinkingLevelMap": {"minimal": None, "high": "max"},
            "compat": {"thinkingFormat": "chat-template", "chatTemplateKwargs": {"thinking": {"$var": "thinking.enabled"}}},
        }],
    }}}))

    assert config.getError() is None
    model = config.getProvider("demo").models[0]
    assert model.thinkingLevelMap.minimal is None and model.thinkingLevelMap.high == "max"
    assert config.getProvider("demo").compat == {"supportsEagerToolInputStreaming": False, "notYetKnown": 1}


async def test_any_schema_error_discards_the_whole_file_with_paths(tmp_path):
    path = _write(tmp_path, {"providers": {
        "good": {"baseUrl": "https://x"},
        "bad": {"baseUrl": None, "models": [{"name": "no id"}]},
    }})
    config = await ModelConfig.load(path)

    assert config.getProviderIds() == []
    error = config.getError()
    assert error.startswith("Invalid models.json schema:\n")
    assert "  - providers.bad.baseUrl: " in error and "  - providers.bad.models.0.id: " in error
    assert error.endswith(f"\n\nFile: {path}")


async def test_parse_errors_name_the_file(tmp_path):
    path = _write(tmp_path, "{bad")
    config = await ModelConfig.load(path)

    assert config.getError().startswith("Failed to parse models.json: ")
    assert config.getError().endswith(f"\n\nFile: {path}")


def test_strip_json_comments_leaves_strings_alone():
    assert stripJsonComments('{"a": "x // y", // z\n "b": [1,],}') == '{"a": "x // y", \n "b": [1]}'


async def test_values_are_never_coerced(tmp_path):
    # TypeBox `Check` does not convert: a string is not a boolean or a number.
    for model in ({"id": "m", "reasoning": "yes"}, {"id": "m", "maxTokens": "4096"},
                  {"id": "m", "cost": {"input": True, "output": 0, "cacheRead": 0, "cacheWrite": 0}}):
        config = await ModelConfig.load(_write(tmp_path, {"providers": {"p": {"models": [model]}}}))
        assert config.getProviderIds() == [] and "Invalid models.json schema" in config.getError()

    config = await ModelConfig.load(_write(tmp_path, {"providers": {"p": {"models": [
        {"id": "m", "contextWindow": 128000, "inputLimits": {"maxRequestBytes": 2.0}},
    ]}}}))
    assert config.getError() is None


async def test_compat_is_one_schema_that_type_checks_every_known_key(tmp_path):
    # pi 1.2: provider-level compat is API-agnostic, one merged schema (ai compat_schema.ProviderCompatSchema), so a
    # known key of any API is checked whatever the provider's API. It was a union of three object schemas, where a
    # block that any arm ignored passed: these four were valid, and now discard the file.
    for compat in ({"sessionAffinityFormat": "bogus"}, {"supportsMidConvoSystemMessages": 5},
                   {"supportsMidConvoEffort": "yes"}, {"thinkingTokenBudgetField": "budget"}):
        config = await ModelConfig.load(_write(tmp_path, {"providers": {"p": {"compat": compat}}}))
        assert config.getProviderIds() == [] and "Invalid models.json schema" in config.getError(), compat
        assert f"providers.p.compat.{next(iter(compat))}" in config.getError()

    # Unknown keys, nested ones included, are kept; a known key of another API is valid when well typed.
    for compat in ({"openRouterRouting": {"order": ["a"], "notYetKnown": 1}}, {"vllmPriority": 1.5},
                   {"supportsMidConvoSystemMessages": True, "zaiToolStream": True},
                   {"chatTemplateKwargs": {"budget": {"$var": "thinking.budget"}}}):
        config = await ModelConfig.load(_write(tmp_path, {"providers": {"p": {"compat": compat}}}))
        assert config.getError() is None, compat
        assert config.getProvider("p").compat == compat

    config = await ModelConfig.load(_write(tmp_path, {"providers": {"p": {"compat": {"supportsLongCacheRetention": None}}}}))
    assert config.getProviderIds() == [] and "compat" in config.getError()


async def test_accepts_custom_compatibility_settings_and_rejects_malformed_known_fields(tmp_path):
    compat = {"customOption": "value", "supportsStore": True, "supportsAdditionalTools": True}
    path = _write(tmp_path, {"providers": {"demo": {"api": "custom-api", "compat": compat}}})

    config = await ModelConfig.load(path)
    assert config.getError() is None
    assert config.getProvider("demo").compat == compat

    path = _write(tmp_path, {"providers": {"demo": {"compat": {"supportsLongCacheRetention": "yes"}}}})
    assert "Invalid models.json schema" in (await ModelConfig.load(path)).getError()


async def test_validates_schema_in_models_json_at_runtime(tmp_path):
    url = "https://raw.githubusercontent.com/Luciole-Studio/Misaka-Agent/v1.0.0/misaka/schemas/models.schema.json"
    path = _write(tmp_path, {"$schema": url, "providers": {"demo": {"baseUrl": "http://localhost:8080/v1", "models": [{"id": "demo"}]}}})

    config = await ModelConfig.load(path)
    assert config.getError() is None
    assert config.getProvider("demo").models[0].id == "demo"

    path = _write(tmp_path, {"$schema": url, "providers": {"demo": {"models": [{"id": ""}]}}})
    assert "Invalid models.json schema" in (await ModelConfig.load(path)).getError()

    path = _write(tmp_path, {"$schema": 42, "providers": {}})
    assert "$schema" in (await ModelConfig.load(path)).getError()


async def test_rejects_non_positive_model_token_limits(tmp_path):
    for field in ("contextWindow", "maxTokens"):
        for value in (0, -1):
            for provider in ({"models": [{"id": "model", field: value}]}, {"modelOverrides": {"model": {field: value}}}):
                config = await ModelConfig.load(_write(tmp_path, {"providers": {"local": provider}}))
                assert "Invalid models.json schema" in config.getError(), (provider, field, value)

    config = await ModelConfig.load(_write(tmp_path, {"providers": {"local": {
        "models": [{"id": "model", "contextWindow": 1, "maxTokens": 1}],
        "modelOverrides": {"model": {"contextWindow": 1, "maxTokens": 1}},
    }}}))
    assert config.getError() is None


async def test_override_cost_is_partial_and_typed(tmp_path):
    config = await ModelConfig.load(_write(tmp_path, {"providers": {"p": {"modelOverrides": {"m": {"cost": {"input": 99}}}}}}))
    assert config.getError() is None
    assert config.getProvider("p").modelOverrides["m"].cost.input == 99
    assert config.getProvider("p").modelOverrides["m"].cost.output is None

    config = await ModelConfig.load(_write(tmp_path, {"providers": {"p": {"modelOverrides": {"m": {"cost": {"input": "99"}}}}}}))
    assert "providers.p.modelOverrides.m.cost.input" in config.getError()


async def test_invalid_utf8_is_replaced_as_node_reads_it(tmp_path):
    path = tmp_path / "models.json"
    path.write_bytes(b'{"providers":{"p":{"name":"caf\xe9"}}}')

    config = await ModelConfig.load(str(path))

    assert config.getError() is None
    assert config.getProvider("p").name == "caf�"

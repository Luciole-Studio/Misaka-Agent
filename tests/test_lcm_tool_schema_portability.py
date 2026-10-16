"""LCM tool schemas must survive Google's independent union-branch validation."""
from copy import deepcopy

from jsonschema import Draft202012Validator
import pytest

from misaka.ai.types import Tool
from misaka.ai.providers.google_shared import convert_tools as google_tools
from misaka.ai.providers.openai_completions import convert_tools as openrouter_tools
from misaka.extensions.misaka_lcm.host.tools import _definition
from misaka.ai.providers.constrained_sampling import _portable_json_schema
from misaka.extensions.misaka_lcm.vendor.engine import LCMEngine
from misaka.extensions.misaka_lcm.vendor.schemas import LCM_RETRIEVE


def _operand(schema):
    return schema["properties"]["computation"]["properties"]["operands"]["items"]


def _check_google_required(node):
    if isinstance(node, list):
        for child in node:
            _check_google_required(child)
    elif isinstance(node, dict):
        if node.get("required"):
            assert node.get("type") == "object"
            assert set(node["required"]) <= set(node.get("properties", {}))
        # Conditionals are JSON-Schema-only fields, not Google Schema branches.
        for key, child in node.items():
            if key not in {"if", "then", "else"}:
                _check_google_required(child)


def test_registration_and_both_request_paths_have_independent_object_branches():
    before = deepcopy(LCMEngine.get_tool_schemas())
    tools = []
    for schema in LCMEngine.get_tool_schemas():
        definition = _definition(schema)
        tools.append(Tool(name=definition.name, description=definition.description, parameters=definition.parameters))
    converted = openrouter_tools(tools, {"supportsStrictMode": False})
    for declaration in converted:
        _check_google_required(declaration["function"]["parameters"])
    for declaration in google_tools(tools, use_parameters=True)[0]["functionDeclarations"]:
        _check_google_required(declaration["parameters"])
    assert LCMEngine.get_tool_schemas() == before
    adapted = _operand(next(item["function"]["parameters"] for item in converted if item["function"]["name"] == "lcm_retrieve"))
    assert adapted["anyOf"][0]["properties"]["assertion_id"] == {"type": "string"}
    assert set(adapted["anyOf"][1]["properties"]) == {"store_id", "span_start", "span_end"}


@pytest.mark.parametrize("operand", [
    {"quote": "evidence", "assertion_id": "exact-id"},
    {"quote": "evidence", "store_id": 7, "span_start": 0, "span_end": 8},
    {"quote": "evidence", "assertion_id": "exact-id", "store_id": 7, "span_start": 0, "span_end": 8},
    {"quote": "evidence"},
    {"assertion_id": "exact-id"},
    {"quote": "evidence", "store_id": 7, "span_start": 0},
    {"quote": "evidence", "assertion_id": 123},
    {"quote": "evidence", "assertion_id": "exact-id", "extra": True},
    "not-an-object",
])
def test_original_operand_acceptance_and_rejection_are_preserved(operand):
    original = _operand(LCM_RETRIEVE["parameters"])
    adapted = _operand(_portable_json_schema(LCM_RETRIEVE["parameters"]))
    assert Draft202012Validator(adapted).is_valid(operand) == Draft202012Validator(original).is_valid(operand)


def test_scalar_unions_and_other_tools_are_unchanged_and_copies_are_independent():
    source = LCM_RETRIEVE["parameters"]
    adapted = _portable_json_schema(source)
    assert _operand(adapted)["properties"]["value"] == _operand(source)["properties"]["value"]
    adapted["properties"]["action"]["enum"].append("changed")
    assert "changed" not in source["properties"]["action"]["enum"]
    for schema in LCMEngine.get_tool_schemas():
        if schema["name"] != "lcm_retrieve":
            assert _portable_json_schema(schema["parameters"]) == schema["parameters"]

"""The built-in tools give a model the schema pi's model gets: the fixture holds pi 1.1.0's TypeBox declarations of
each tool's parameters, as the real TypeBox serializes them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from misaka.ai.utils.typebox_helpers import typeboxSchema
from misaka.core.tools.bash import BashToolInput
from misaka.core.tools.edit import EditToolInput
from misaka.core.tools.find import FindToolInput
from misaka.core.tools.grep import GrepToolInput
from misaka.core.tools.ls import LsToolInput
from misaka.core.tools.read import ReadToolInput
from misaka.core.tools.write import WriteToolInput
from misaka.utils.js import jsonStringify

SCHEMAS = json.loads((Path(__file__).parent / "fixtures" / "typebox_tool_schemas.json").read_text(encoding="utf-8"))["schemas"]
MODELS = {
    "bash": BashToolInput,
    "read": ReadToolInput,
    "write": WriteToolInput,
    "edit": EditToolInput,
    "ls": LsToolInput,
    "find": FindToolInput,
    "grep": GrepToolInput,
}


@pytest.mark.parametrize("name", list(MODELS))
def test_built_in_tool_schemas_serialize_as_typebox_does(name):
    schema = typeboxSchema(MODELS[name])
    if name == "read":
        # MISAKA fork: read's spreadsheet range.
        del schema["properties"]["cell_range"]
    assert jsonStringify(schema) == SCHEMAS[name]


def test_optional_fields_are_left_out_of_required_rather_than_made_nullable():
    class Params(BaseModel):
        name: str = Field(description="who")
        count: int | None = Field(default=None, description="how many")
        flag: bool = Field(default=False)

    assert typeboxSchema(Params) == {
        "type": "object",
        "required": ["name"],
        "properties": {
            "name": {"type": "string", "description": "who"},
            "count": {"type": "integer", "description": "how many"},
            "flag": {"type": "boolean", "default": False},
        },
    }


# `JSON.stringify` of the `Type.Object(...)` literals in pi 1.1.0's codemode/tool.ts and tool-search/tool.ts,
# evaluated with typebox (scratchpad oracle110/tb_cm.mjs, tb_search.mjs).
_EXTENSION_SCHEMAS = {
    "codemode": '{"type":"object","required":["code"],"properties":{"code":{"type":"string",'
    '"description":"Raw JavaScript source."}}}',
    "tool_search": '{"type":"object","required":["query"],"properties":{"query":{"type":"string",'
    '"description":"Search query for deferred tools."},"limit":{"type":"number",'
    '"description":"Maximum number of tools to return. Defaults to 8."}}}',
}


def test_the_codemode_and_tool_search_schemas_are_typebox_s_bytes():
    from misaka.extensions.codemode.tool import codemodeSchema
    from misaka.extensions.tool_search.tool import toolSearchSchema

    assert jsonStringify(codemodeSchema) == _EXTENSION_SCHEMAS["codemode"]
    assert jsonStringify(toolSearchSchema) == _EXTENSION_SCHEMAS["tool_search"]

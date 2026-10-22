"""Translated from pi's ``packages/ai/test/json-parse.test.ts`` (v1.3.0).

MISAKA fork: the malformed-arguments cases also run through ``StreamingArgs.finish_into``, the path every adapter
finalizes a streamed tool call on (``Object.assign(block, parseToolCallArguments(partialJson))`` upstream).
"""

from __future__ import annotations

import pytest

from misaka.ai.types import ToolCall
from misaka.ai.utils._streaming_args import StreamingArgs
from misaka.ai.utils.json_parse import parseStreamingJson, parseToolCallArguments

# --- parseStreamingJson ---------------------------------------------------------------------------


# #10868
def test_keeps_invalid_u_escapes_as_literal_text_without_dropping_fields():
    assert parseStreamingJson(r'{"text":"\url{x}"}') == {"text": "\\url{x}"}
    assert parseStreamingJson(r'{"path":"a.tex","content":"\usepackage{x}","mode":"w"}') == {
        "path": "a.tex",
        "content": "\\usepackage{x}",
        "mode": "w",
    }


def test_decodes_valid_u_escapes():
    assert parseStreamingJson(r'{"text":"café \url"}') == {"text": "café \\url"}


def test_omits_a_u_escape_that_is_cut_off_mid_stream():
    assert parseStreamingJson(r'{"text":"ab\u00') == {"text": "ab"}


# --- parseToolCallArguments (#10897) --------------------------------------------------------------


def test_parses_complete_json_and_repairs_invalid_escapes():
    assert parseToolCallArguments(r'{"kind":"seek","note":"\url"}') == {"arguments": {"kind": "seek", "note": "\\url"}}


def test_uses_empty_arguments_for_empty_text():
    assert parseToolCallArguments("") == {"arguments": {}}


def test_does_not_keep_the_valid_prefix_of_malformed_json():
    raw = '{"action":"apply","actions":[{"kind":"seek","time":.5}]}'
    result = parseToolCallArguments(raw)
    assert result["arguments"] == {}
    assert "\n\nReceived arguments:\n" in result["argumentsError"]
    assert result["argumentsError"].endswith(raw)


def test_does_not_complete_cut_off_json():
    raw = '{"kind":"seek","clipId":"clip-fl'
    result = parseToolCallArguments(raw)
    assert result["arguments"] == {}
    assert result["argumentsError"].endswith(raw)


def test_rejects_json_that_is_not_an_object():
    assert parseToolCallArguments("[1,2]") == {
        "arguments": {},
        "argumentsError": "Tool arguments must be a JSON object\n\nReceived arguments:\n[1,2]",
    }


@pytest.mark.parametrize(
    "raw",
    ['{"action":"apply","actions":[{"kind":"seek","time":.5}]}', '{"kind":"seek","clipId":"clip-fl', "[1,2]"],
)
def test_a_streamed_call_finalizes_with_the_same_arguments_error(raw):
    block = ToolCall(id="call-1", name="seek", arguments={})
    args = StreamingArgs()
    args.append(raw)
    # The live preview may hold a repaired prefix; the final arguments never do.
    _preview = args.arguments

    args.finish_into(block)

    assert (block.arguments, block.argumentsError) == ({}, parseToolCallArguments(raw)["argumentsError"])


def test_a_streamed_call_with_valid_arguments_clears_the_error():
    block = ToolCall(id="call-1", name="seek", arguments={}, argumentsError="stale")

    StreamingArgs(r'{"note":"\url"}').finish_into(block)

    assert (block.arguments, block.argumentsError) == ({"note": "\\url"}, None)

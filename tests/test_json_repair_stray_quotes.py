"""Tool arguments with an unescaped quote inside a string value are repaired, not refused.

2026-09-23: through the sub2api gateway, Claude's `edit`/`office` arguments arrived with literal
``"`` around quoted terms in Chinese prose (``要求"新秩序"``) while every ``\\n`` was escaped;
strict parsing failed, pi's repair does not cover it, and the Sister retried the identical text
three times. The stray-quote pass runs only after both of those have failed."""
import json

import pytest

from misaka.ai.utils._streaming_args import StreamingArgs
from misaka.ai.utils.json_parse import _escape_stray_quotes, parseJsonWithRepair

BROKEN = ('{"path": "IE_ireland.md", "edits": [{"oldText": "## 附：跨卡接口", '
          '"newText": "## ⑯bis\\n\\n用户的总问题要求"新秩序"如何运作。以"爱尔兰中立化"换取承认——列为"永久中立国"而非"盟国"。\\n\\n---"}]}')


def test_the_real_payload_parses_with_the_quotes_kept_as_text():
    value = parseJsonWithRepair(BROKEN)
    new_text = value["edits"][0]["newText"]
    assert '要求"新秩序"如何运作' in new_text
    assert '以"爱尔兰中立化"换取' in new_text
    assert new_text.startswith("## ⑯bis\n\n")          # the escaped newlines were real newlines all along
    assert value["path"] == "IE_ireland.md"


def test_valid_json_is_never_touched():
    text = json.dumps({"a": "x \" y", "b": ["q", {"c": "d"}], "e": 1}, ensure_ascii=False)
    assert _escape_stray_quotes(text) == text
    assert parseJsonWithRepair(text) == json.loads(text)


@pytest.mark.parametrize("text, expected", [
    ('{"a": "he said "hi" to me"}', {"a": 'he said "hi" to me'}),
    ('{"a": "trailing quote""}', {"a": 'trailing quote"'}),
    ('{"a": "x", "b": "y "z""}', {"a": "x", "b": 'y "z"'}),
])
def test_stray_quote_shapes(text, expected):
    assert parseJsonWithRepair(text) == expected


def test_a_streamed_call_with_stray_quotes_now_executes():
    args = StreamingArgs()
    args.append(BROKEN)
    assert args.finish()["edits"][0]["oldText"] == "## 附：跨卡接口"


def test_genuinely_broken_json_still_raises():
    with pytest.raises(json.JSONDecodeError):
        parseJsonWithRepair('{"a": [1, 2')

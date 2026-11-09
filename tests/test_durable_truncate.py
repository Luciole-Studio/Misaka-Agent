"""pi durable's ``test/env-truncate.test.ts``. The runtime-without-``Buffer`` case has no Python counterpart (one
implementation counts the bytes); its inputs run against the same expected byte counts here."""

from __future__ import annotations

from misaka.durable.truncate import formatSize, truncateHead, utf8ByteLength


def byteLength(content: str) -> int:
    return len(content.encode("utf-8", "replace"))


def test_reports_utf_8_byte_counts_in_truncation_results():
    content = "aé🙂\nb"
    result = truncateHead(content, {"maxBytes": 100, "maxLines": 10})

    assert result["truncated"] is False
    assert result["totalBytes"] == byteLength(content)
    assert result["outputBytes"] == byteLength(content)
    assert result["totalBytes"] == 9


def test_counts_utf_8_bytes_the_way_a_text_encoder_does():
    # A lone surrogate counts as the three bytes of U+FFFD.
    inputs = ["", "ascii", "é", "中", "🙂", "\ud83d", "\ude42", "a🙂b", "߿ࠀ￿"]
    assert [utf8ByteLength(content) for content in inputs] == [0, 5, 2, 3, 4, 3, 3, 6, 8]
    head = truncateHead("aé🙂\nb", {"maxBytes": 7, "maxLines": 10})
    assert head["content"] == "aé🙂"
    assert head["outputBytes"] == 7
    assert head["truncatedBy"] == "bytes"


def test_does_not_count_a_trailing_newline_as_an_extra_line():
    content = "\n".join(["line"] * 3) + "\n"
    head = truncateHead(content, {"maxBytes": 100, "maxLines": 3})

    assert head["truncated"] is False
    assert head["totalLines"] == 3
    assert head["outputLines"] == 3


def test_truncates_head_by_line_limits():
    head = truncateHead("one\ntwo\nthree\nfour", {"maxBytes": 100, "maxLines": 2})
    assert head["content"] == "one\ntwo"
    assert head["truncated"] is True
    assert head["truncatedBy"] == "lines"
    assert head["totalLines"] == 4
    assert head["outputLines"] == 2


def test_reports_bytes_when_only_a_trailing_newline_exceeds_limits_at_the_line_cap():
    head = truncateHead("hello\nworld\n", {"maxBytes": 11, "maxLines": 2})
    assert head["content"] == "hello\nworld"
    assert head["truncated"] is True
    assert head["truncatedBy"] == "bytes"
    assert head["totalLines"] == 2
    assert head["outputLines"] == 2


def test_truncates_head_on_utf_8_byte_limits_without_partial_lines():
    result = truncateHead("éé\nabc", {"maxBytes": 4, "maxLines": 10})

    assert result["content"] == "éé"
    assert result["truncated"] is True
    assert result["truncatedBy"] == "bytes"
    assert result["outputBytes"] == 4
    assert result["firstLineExceedsLimit"] is False


def test_reports_head_truncation_when_the_first_line_exceeds_the_byte_limit():
    result = truncateHead("éé\nabc", {"maxBytes": 3, "maxLines": 10})

    assert result["content"] == ""
    assert result["truncated"] is True
    assert result["truncatedBy"] == "bytes"
    assert result["firstLineExceedsLimit"] is True


def test_formats_sizes():
    assert formatSize(1023) == "1023B"
    assert formatSize(1536) == "1.5KB"
    assert formatSize(3 * 1024 * 1024) == "3.0MB"


def test_uses_the_default_limits_and_reports_them():
    result = truncateHead("x" * 10)
    assert result["maxLines"] == 2000
    assert result["maxBytes"] == 50 * 1024
    assert result["truncated"] is False

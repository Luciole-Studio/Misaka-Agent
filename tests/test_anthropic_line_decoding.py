"""A multi-byte character split across two body chunks decodes as one character.

Upstream reads the SSE body through ``TextDecoder.decode(value, {stream: true})`` and flushes
it at the end; decoding each chunk on its own raised ``UnicodeDecodeError`` as soon as a CJK
character straddled a chunk boundary.
"""

from types import SimpleNamespace

from misaka.ai.api.anthropic_messages import _iterateSseMessages

FRAME = "event: message_stop\ndata: 数据と資料\n\n".encode()


def _body(chunks):
    async def aiter_bytes():
        for chunk in chunks:
            yield chunk

    return SimpleNamespace(aiter_bytes=aiter_bytes)


async def _collect(chunks):
    return [event async for event in _iterateSseMessages(_body(chunks))]


async def test_a_character_split_across_chunks_decodes_once():
    cut = FRAME.index("数".encode()) + 1  # one byte into a three-byte character
    [event] = await _collect([FRAME[:cut], FRAME[cut:]])

    assert event["event"] == "message_stop"
    assert event["data"] == "数据と資料"


async def test_a_crlf_split_between_chunks_reads_as_two_line_breaks_as_upstream_does():
    # Upstream's `consumeLine` takes a lone CR at the end of the buffer as a line break, so the LF that
    # arrives next is an empty line and dispatches the event early.
    frame = b"event: message_stop\r\ndata: x\r\n\r\n"
    cut = frame.index(b"\r\n") + 1  # between the CR and the LF
    events = await _collect([frame[:cut], frame[cut:]])

    assert events == [
        {"event": "message_stop", "data": "", "raw": ["event: message_stop"]},
        {"event": None, "data": "x", "raw": ["data: x"]},
    ]


async def test_a_trailing_event_without_a_blank_line_is_flushed():
    [event] = await _collect([b"event: message_stop\ndata: tail"])

    assert event["data"] == "tail"

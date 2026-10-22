"""Translated from pi-tui's ``test/stdin-buffer.test.ts`` (v1.3.0).

Tests for StdinBuffer

Based on code from OpenTUI (https://github.com/anomalyco/opentui)
MIT License - Copyright (c) 2025 opentui

The tests run on the event loop, as upstream's do: the buffer's timers are loop timers. ``t.mock.timers`` is the
editor tests' patched ``loop.call_later`` (``mockTimers``).
"""

import asyncio

import pytest

from misaka.ui.tui.keys import matchesKey
from misaka.ui.tui.stdin_buffer import StdinBuffer
from tests.test_tui_editor import mockTimers


class Harness:
    def __init__(self) -> None:
        self.buffer = StdinBuffer({"timeout": 10})
        # Collect emitted sequences
        self.emittedSequences: list[str] = []
        self.buffer.on("data", self.emittedSequences.append)
        # Collect paste events
        self.emittedPaste: list[str] = []
        self.buffer.on("paste", self.emittedPaste.append)

    # Helper to process data through the buffer
    def processInput(self, data: str | bytes) -> None:
        self.buffer.process(data)

    def replace(self, buffer: StdinBuffer) -> None:
        self.buffer = buffer
        self.emittedSequences = []
        self.buffer.on("data", self.emittedSequences.append)


@pytest.fixture
async def h():
    harness = Harness()
    yield harness
    harness.buffer.destroy()


# Helper to wait for async operations
async def wait(ms: int) -> None:
    await asyncio.sleep(ms / 1000)


# --- Regular Characters ---------------------------------------------------------------------------


async def test_should_pass_through_regular_characters_immediately(h):
    h.processInput("a")
    assert h.emittedSequences == ["a"]


async def test_should_pass_through_multiple_regular_characters(h):
    h.processInput("abc")
    assert h.emittedSequences == ["a", "b", "c"]


async def test_should_handle_unicode_characters(h):
    h.processInput("hello 世界")
    assert h.emittedSequences == ["h", "e", "l", "l", "o", " ", "世", "界"]


# --- Complete Escape Sequences --------------------------------------------------------------------


async def test_should_pass_through_complete_mouse_sgr_sequences(h):
    mouseSeq = "\x1b[<35;20;5m"
    h.processInput(mouseSeq)
    assert h.emittedSequences == [mouseSeq]


async def test_should_pass_through_complete_arrow_key_sequences(h):
    upArrow = "\x1b[A"
    h.processInput(upArrow)
    assert h.emittedSequences == [upArrow]


async def test_should_pass_through_complete_function_key_sequences(h):
    f1 = "\x1b[11~"
    h.processInput(f1)
    assert h.emittedSequences == [f1]


async def test_should_pass_through_meta_key_sequences(h):
    metaA = "\x1ba"
    h.processInput(metaA)
    assert h.emittedSequences == [metaA]


async def test_should_pass_through_ss3_sequences(h):
    ss3 = "\x1bOA"
    h.processInput(ss3)
    assert h.emittedSequences == [ss3]


# --- Partial Escape Sequences ---------------------------------------------------------------------


async def test_should_buffer_incomplete_mouse_sgr_sequence(h):
    h.processInput("\x1b")
    assert h.emittedSequences == []
    assert h.buffer.getBuffer() == "\x1b"

    h.processInput("[<35")
    assert h.emittedSequences == []
    assert h.buffer.getBuffer() == "\x1b[<35"

    h.processInput(";20;5m")
    assert h.emittedSequences == ["\x1b[<35;20;5m"]
    assert h.buffer.getBuffer() == ""


async def test_should_buffer_incomplete_csi_sequence(h):
    h.processInput("\x1b[")
    assert h.emittedSequences == []

    h.processInput("1;")
    assert h.emittedSequences == []

    h.processInput("5H")
    assert h.emittedSequences == ["\x1b[1;5H"]


async def test_should_buffer_split_across_many_chunks(h):
    h.processInput("\x1b")
    h.processInput("[")
    h.processInput("<")
    h.processInput("3")
    h.processInput("5")
    h.processInput(";")
    h.processInput("2")
    h.processInput("0")
    h.processInput(";")
    h.processInput("5")
    h.processInput("m")

    assert h.emittedSequences == ["\x1b[<35;20;5m"]


async def test_should_flush_incomplete_sequence_after_timeout(h):
    h.processInput("\x1b[<35")
    assert h.emittedSequences == []

    # Wait for timeout
    await wait(15)

    assert h.emittedSequences == ["\x1b[<35"]


async def test_should_flush_a_lone_esc_as_escape_when_cr_arrives_after_the_timeout(h):
    # Legacy-mode Alt+Enter is ESC + CR; when the terminal/transport splits
    # the bytes further apart than the timeout, ESC is flushed alone and the
    # host sees Escape (interrupt) instead of Alt+Enter. This locks in the
    # behavior so the configurable timeout in ProcessTerminal stays honest.
    h.processInput("\x1b")
    await wait(20)  # buffer timeout is 10ms in beforeEach
    h.processInput("\r")

    assert h.emittedSequences == ["\x1b", "\r"]
    assert matchesKey(h.emittedSequences[0] if h.emittedSequences else "", "escape") is True


async def test_should_merge_esc_cr_split_across_chunks_within_a_larger_timeout(h):
    h.replace(StdinBuffer({"escapeTimeout": 100}))

    h.processInput("\x1b")
    await wait(20)  # > 10ms default escapeTimeout, < 100ms configured escapeTimeout
    h.processInput("\r")

    assert h.emittedSequences == ["\x1b\r"]
    assert matchesKey(h.emittedSequences[0] if h.emittedSequences else "", "alt+enter") is True


async def test_does_not_apply_the_sequence_timeout_to_a_lone_esc(h):
    h.replace(StdinBuffer({"timeout": 100}))

    h.processInput("\x1b")
    await wait(20)
    h.processInput("\r")

    assert h.emittedSequences == ["\x1b", "\r"]
    assert matchesKey(h.emittedSequences[0] if h.emittedSequences else "", "escape") is True


async def test_keeps_fragmented_mouse_sequences_buffered_across_delayed_chunks_by_default():
    delayedBuffer = StdinBuffer()
    delayedSequences: list[str] = []
    delayedBuffer.on("data", delayedSequences.append)

    delayedBuffer.process("\x1b[")
    await wait(20)
    assert delayedSequences == []
    delayedBuffer.process("<65;48;39M")
    assert delayedSequences == ["\x1b[<65;48;39M"]
    delayedBuffer.destroy()


# --- Terminal Reply Strings -----------------------------------------------------------------------

replies = [
    {"header": "\x1b]4;1;", "reply": "\x1b]4;1;rgb:eaea/6c6c/6d6d\x07"},  # Ghostty's Ayu Light palette color 1.
    {"header": "\x1b]10;", "reply": "\x1b]10;rgb:ffff/ffff/ffff\x1b\\"},
    {"header": "\x1b]11;", "reply": "\x1b]11;rgb:0000/0000/0000\x07"},
    {"header": "\x1b]4;1;", "reply": "\x1b]4;1;rgb:6464/6464/4d4d\x07"},  # Another reported color-reply tail.
    {"header": "\x1bP>|", "reply": "\x1bP>|Ghostty 1.3.1\x1b\\"},
    {"header": "\x1b_Gi=1;", "reply": "\x1b_Gi=1;OK\x1b\\"},
    {"header": "\x1b]7501;?", "reply": "\x1b]7501;?\x1b\\"},
]


@pytest.mark.parametrize("header,reply", [(r["header"], r["reply"]) for r in replies])
async def test_keeps_delayed_reply_intact_at_every_split_after_its_reply_header(header, reply):
    with mockTimers() as timers:
        for split in range(len(header), len(reply)):
            delayedBuffer = StdinBuffer()
            sequences: list[str] = []
            delayedBuffer.on("data", sequences.append)
            try:
                delayedBuffer.process(reply[:split])
                timers.tick(5000)
                delayedBuffer.process(f"{reply[split:]}x")
                assert any(matchesKey(sequence, "ctrl+g") for sequence in sequences) is False
                assert sequences == [reply, "x"]
            finally:
                delayedBuffer.destroy()


async def test_keeps_an_osc_reply_buffered_across_multiple_expired_sequence_timeouts(h):
    with mockTimers() as timers:
        h.processInput("\x1b]4;1;rgb:eaea/6c")
        timers.tick(100)
        assert h.emittedSequences == []
        h.processInput("6c/6d6d")
        timers.tick(100)
        assert h.emittedSequences == []
        h.processInput("\x07")
        assert h.emittedSequences == [replies[0]["reply"]]


async def test_still_times_out_bare_string_introducers_as_alt_keys(h):
    with mockTimers() as timers:
        for prefix in ["\x1b]", "\x1bP", "\x1b_"]:
            h.processInput(prefix)
            timers.tick(10)
        assert h.emittedSequences == ["\x1b]", "\x1bP", "\x1b_"]


@pytest.mark.parametrize(
    "ambiguous",
    [
        "\x1b]a",
        "\x1b]1",
        "\x1b]10",
        "\x1b]0;title",
        "\x1b]4;",
        "\x1b]4;a;",
        "\x1bPa",
        "\x1bP>",
        "\x1b_a",
        "\x1b_G",
        "\x1b_Gi=1",
    ],
)
async def test_recovers_batched_legacy_alt_input_within_the_sequence_timeout(ambiguous):
    with mockTimers() as timers:
        legacyBuffer = StdinBuffer()
        sequences: list[str] = []
        legacyBuffer.on("data", sequences.append)
        try:
            legacyBuffer.process(ambiguous)
            timers.tick(20)
            legacyBuffer.process("x\r\x03\x04")
            timers.tick(49)
            assert sequences == []
            timers.tick(1)
            assert sequences == [
                ambiguous[:2],
                *ambiguous[2:],
                "x",
                "\r",
                "\x03",
                "\x04",
            ]
            assert legacyBuffer.getBuffer() == ""

            legacyBuffer.process("y\r\x03\x04")
            assert sequences[-4:] == ["y", "\r", "\x03", "\x04"]
        finally:
            legacyBuffer.destroy()


async def test_recognizes_a_reply_header_assembled_within_the_ambiguous_input_timeout(h):
    with mockTimers() as timers:
        h.processInput("\x1b]1")
        timers.tick(5)
        h.processInput("0;")
        timers.tick(5000)
        assert h.emittedSequences == []
        h.processInput("rgb:ffff/ffff/ffff\x1b\\")
        assert h.emittedSequences == [replies[1]["reply"]]


async def test_recovers_from_an_unterminated_reply_when_a_new_escape_sequence_arrives(h):
    with mockTimers() as timers:
        h.processInput("\x1b]4;1;rgb:eaea/6c")
        timers.tick(100)
        h.processInput("\x1b")
        timers.tick(100)
        h.processInput("[Ahello")
        assert h.emittedSequences == ["\x1b[A", "h", "e", "l", "l", "o"]


async def test_discards_a_canceled_reply_without_swallowing_subsequent_input(h):
    for cancel in ["\x18", "\x1a"]:
        h.processInput(f"\x1b]11;rgb:0000{cancel}x")
    assert h.emittedSequences == ["x", "x"]


# --- Mixed Content --------------------------------------------------------------------------------


async def test_should_handle_characters_followed_by_escape_sequence(h):
    h.processInput("abc\x1b[A")
    assert h.emittedSequences == ["a", "b", "c", "\x1b[A"]


async def test_should_handle_escape_sequence_followed_by_characters(h):
    h.processInput("\x1b[Aabc")
    assert h.emittedSequences == ["\x1b[A", "a", "b", "c"]


async def test_should_handle_multiple_complete_sequences(h):
    h.processInput("\x1b[A\x1b[B\x1b[C")
    assert h.emittedSequences == ["\x1b[A", "\x1b[B", "\x1b[C"]


async def test_should_handle_partial_sequence_with_preceding_characters(h):
    h.processInput("abc\x1b[<35")
    assert h.emittedSequences == ["a", "b", "c"]
    assert h.buffer.getBuffer() == "\x1b[<35"

    h.processInput(";20;5m")
    assert h.emittedSequences == ["a", "b", "c", "\x1b[<35;20;5m"]


# --- Kitty Keyboard Protocol ----------------------------------------------------------------------


async def test_should_handle_kitty_csi_u_press_events(h):
    # Press 'a' in Kitty protocol
    h.processInput("\x1b[97u")
    assert h.emittedSequences == ["\x1b[97u"]


async def test_should_handle_kitty_csi_u_release_events(h):
    # Release 'a' in Kitty protocol
    h.processInput("\x1b[97;1:3u")
    assert h.emittedSequences == ["\x1b[97;1:3u"]


async def test_should_handle_batched_kitty_press_and_release(h):
    # Press 'a', release 'a' batched together (common over SSH)
    h.processInput("\x1b[97u\x1b[97;1:3u")
    assert h.emittedSequences == ["\x1b[97u", "\x1b[97;1:3u"]


async def test_should_handle_multiple_batched_kitty_events(h):
    # Press 'a', release 'a', press 'b', release 'b'
    h.processInput("\x1b[97u\x1b[97;1:3u\x1b[98u\x1b[98;1:3u")
    assert h.emittedSequences == ["\x1b[97u", "\x1b[97;1:3u", "\x1b[98u", "\x1b[98;1:3u"]


async def test_should_handle_kitty_arrow_keys_with_event_type(h):
    # Up arrow press with event type
    h.processInput("\x1b[1;1:1A")
    assert h.emittedSequences == ["\x1b[1;1:1A"]


async def test_should_handle_kitty_functional_keys_with_event_type(h):
    # Delete key release
    h.processInput("\x1b[3;1:3~")
    assert h.emittedSequences == ["\x1b[3;1:3~"]


async def test_should_split_esc_esc_csi_into_standalone_esc_and_the_csi_sequence_wezterm_escape_key_regression(h):
    # WezTerm with enable_kitty_keyboard sends Escape key press as raw \x1b
    # and the release as a full Kitty CSI-u sequence, concatenated.
    # The buffer must not treat \x1b\x1b as a complete meta-key when the
    # following byte starts a new escape sequence.
    h.processInput("\x1b\x1b[27;129:3u")
    assert h.emittedSequences == ["\x1b", "\x1b[27;129:3u"]


async def test_should_split_esc_esc_csi_with_no_modifier_no_num_lock(h):
    h.processInput("\x1b\x1b[27;1:3u")
    assert h.emittedSequences == ["\x1b", "\x1b[27;1:3u"]


async def test_should_still_emit_esc_esc_as_a_single_sequence_when_not_followed_by_a_new_escape(h):
    # \x1b\x1b alone (no following CSI) stays as-is — e.g. ctrl+alt+[
    h.processInput("\x1b\x1b")
    assert h.emittedSequences == ["\x1b\x1b"]


async def test_should_handle_plain_characters_mixed_with_kitty_sequences(h):
    # Plain 'a' followed by Kitty release
    h.processInput("a\x1b[97;1:3u")
    assert h.emittedSequences == ["a", "\x1b[97;1:3u"]


async def test_should_drop_raw_duplicate_character_after_matching_kitty_printable_sequence(h):
    h.processInput("\x1b[224uà")
    assert h.emittedSequences == ["\x1b[224u"]


async def test_should_drop_raw_duplicate_character_after_matching_kitty_printable_sequence_across_chunks(h):
    h.processInput("\x1b[64u")
    h.processInput("@")
    assert h.emittedSequences == ["\x1b[64u"]


async def test_should_keep_non_matching_plain_character_after_kitty_printable_sequence(h):
    h.processInput("\x1b[97ub")
    assert h.emittedSequences == ["\x1b[97u", "b"]


async def test_should_keep_raw_character_after_modified_kitty_printable_sequence(h):
    h.processInput("\x1b[64;3u@")
    assert h.emittedSequences == ["\x1b[64;3u", "@"]


async def test_should_handle_rapid_typing_simulation_with_kitty_protocol(h):
    # Simulates typing "hi" quickly with releases interleaved
    h.processInput("\x1b[104u\x1b[104;1:3u\x1b[105u\x1b[105;1:3u")
    assert h.emittedSequences == ["\x1b[104u", "\x1b[104;1:3u", "\x1b[105u", "\x1b[105;1:3u"]


# --- Mouse Events ---------------------------------------------------------------------------------


async def test_should_handle_mouse_press_event(h):
    h.processInput("\x1b[<0;10;5M")
    assert h.emittedSequences == ["\x1b[<0;10;5M"]


async def test_should_handle_mouse_release_event(h):
    h.processInput("\x1b[<0;10;5m")
    assert h.emittedSequences == ["\x1b[<0;10;5m"]


async def test_should_handle_mouse_move_event(h):
    h.processInput("\x1b[<35;20;5m")
    assert h.emittedSequences == ["\x1b[<35;20;5m"]


async def test_should_handle_split_mouse_events(h):
    h.processInput("\x1b[<3")
    h.processInput("5;1")
    h.processInput("5;")
    h.processInput("10m")
    assert h.emittedSequences == ["\x1b[<35;15;10m"]


async def test_should_handle_multiple_mouse_events(h):
    h.processInput("\x1b[<35;1;1m\x1b[<35;2;2m\x1b[<35;3;3m")
    assert h.emittedSequences == ["\x1b[<35;1;1m", "\x1b[<35;2;2m", "\x1b[<35;3;3m"]


async def test_should_handle_old_style_mouse_sequence_esc_m_3_bytes(h):
    h.processInput("\x1b[M abc")
    assert h.emittedSequences == ["\x1b[M ab", "c"]


async def test_should_buffer_incomplete_old_style_mouse_sequence(h):
    h.processInput("\x1b[M")
    assert h.buffer.getBuffer() == "\x1b[M"

    h.processInput(" a")
    assert h.buffer.getBuffer() == "\x1b[M a"

    h.processInput("b")
    assert h.emittedSequences == ["\x1b[M ab"]


# --- Edge Cases -----------------------------------------------------------------------------------


async def test_should_handle_empty_input(h):
    h.processInput("")
    # Empty string emits an empty data event
    assert h.emittedSequences == [""]


async def test_should_handle_lone_escape_character_with_timeout(h):
    h.processInput("\x1b")
    assert h.emittedSequences == []

    # After timeout, should emit
    await wait(15)
    assert h.emittedSequences == ["\x1b"]


async def test_flushes_a_lone_escape_promptly_with_the_longer_default_sequence_timeout():
    defaultBuffer = StdinBuffer()
    defaultSequences: list[str] = []
    defaultBuffer.on("data", defaultSequences.append)

    defaultBuffer.process("\x1b")
    await wait(20)
    assert defaultSequences == ["\x1b"]
    defaultBuffer.destroy()


async def test_should_handle_lone_escape_character_with_explicit_flush(h):
    h.processInput("\x1b")
    assert h.emittedSequences == []

    flushed = h.buffer.flush()
    assert flushed == ["\x1b"]


async def test_should_handle_buffer_input(h):
    h.processInput(b"\x1b[A")
    assert h.emittedSequences == ["\x1b[A"]


async def test_should_handle_very_long_sequences(h):
    longSeq = f"\x1b[{'1;' * 50}H"
    h.processInput(longSeq)
    assert h.emittedSequences == [longSeq]


# --- Flush ----------------------------------------------------------------------------------------


async def test_should_flush_incomplete_sequences(h):
    h.processInput("\x1b[<35")
    flushed = h.buffer.flush()
    assert flushed == ["\x1b[<35"]
    assert h.buffer.getBuffer() == ""


async def test_should_return_empty_array_if_nothing_to_flush(h):
    flushed = h.buffer.flush()
    assert flushed == []


async def test_should_emit_flushed_data_via_timeout(h):
    h.processInput("\x1b[<35")
    assert h.emittedSequences == []

    # Wait for timeout to flush
    await wait(15)

    assert h.emittedSequences == ["\x1b[<35"]


# --- Clear ----------------------------------------------------------------------------------------


async def test_should_clear_buffered_content_without_emitting(h):
    h.processInput("\x1b[<35")
    assert h.buffer.getBuffer() == "\x1b[<35"

    h.buffer.clear()
    assert h.buffer.getBuffer() == ""
    assert h.emittedSequences == []


# --- Bracketed Paste ------------------------------------------------------------------------------


async def test_should_emit_paste_event_for_complete_bracketed_paste(h):
    pasteStart = "\x1b[200~"
    pasteEnd = "\x1b[201~"
    content = "hello world"

    h.processInput(pasteStart + content + pasteEnd)

    assert h.emittedPaste == ["hello world"]
    assert h.emittedSequences == []  # No data events during paste


async def test_should_handle_paste_arriving_in_chunks(h):
    h.processInput("\x1b[200~")
    assert h.emittedPaste == []

    h.processInput("hello ")
    assert h.emittedPaste == []

    h.processInput("world\x1b[201~")
    assert h.emittedPaste == ["hello world"]
    assert h.emittedSequences == []


async def test_should_handle_paste_with_input_before_and_after(h):
    h.processInput("a")
    h.processInput("\x1b[200~pasted\x1b[201~")
    h.processInput("b")

    assert h.emittedSequences == ["a", "b"]
    assert h.emittedPaste == ["pasted"]


async def test_should_handle_paste_with_newlines(h):
    h.processInput("\x1b[200~line1\nline2\nline3\x1b[201~")

    assert h.emittedPaste == ["line1\nline2\nline3"]
    assert h.emittedSequences == []


async def test_should_handle_paste_with_unicode(h):
    h.processInput("\x1b[200~Hello 世界 🎉\x1b[201~")

    assert h.emittedPaste == ["Hello 世界 🎉"]
    assert h.emittedSequences == []


# --- Destroy --------------------------------------------------------------------------------------


async def test_should_clear_buffer_on_destroy(h):
    h.processInput("\x1b[<35")
    assert h.buffer.getBuffer() == "\x1b[<35"

    h.buffer.destroy()
    assert h.buffer.getBuffer() == ""


async def test_should_clear_pending_timeouts_on_destroy(h):
    h.processInput("\x1b[<35")
    h.buffer.destroy()

    # Wait longer than timeout
    await wait(15)

    # Should not have emitted anything
    assert h.emittedSequences == []


# --- MISAKA: the forks ----------------------------------------------------------------------------


async def test_a_character_split_across_byte_chunks_arrives_whole(h):
    data = "世".encode()
    h.processInput(data[:1])
    h.processInput(data[1:])
    assert h.emittedSequences == ["世"]

    h.processInput(data[:2])
    h.processInput(data[2:])
    assert h.emittedSequences == ["世", "世"]


async def test_a_chunk_the_decoder_holds_back_does_not_reset_the_timer(h):
    h.processInput("\x1b[<35")
    h.processInput("世".encode()[:1])
    await wait(15)
    assert h.emittedSequences == ["\x1b[<35"]


async def test_a_character_outside_the_bmp_is_one_sequence(h):
    h.processInput("a\U0001f600b")
    assert h.emittedSequences == ["a", "\U0001f600", "b"]

    h.processInput("\x1b[128512u\U0001f600")
    assert h.emittedSequences == ["a", "\U0001f600", "b", "\x1b[128512u"]


async def test_sgr_mouse_digits_are_ascii(h):
    h.processInput("\x1b[<²;1;1m")
    assert h.emittedSequences == []
    assert h.buffer.getBuffer() == "\x1b[<²;1;1m"


async def test_an_ime_commit_batched_after_alt_bracket_is_reparsed_per_character():
    # Upstream's flush recovery with CJK text: the Alt+] key, then each committed character, not one swallowed event.
    with mockTimers() as timers:
        imeBuffer = StdinBuffer()
        sequences: list[str] = []
        imeBuffer.on("data", sequences.append)
        try:
            imeBuffer.process("\x1b]日本語\r".encode())
            timers.tick(50)
            assert sequences == ["\x1b]", "日", "本", "語", "\r"]
            assert imeBuffer.getBuffer() == ""
        finally:
            imeBuffer.destroy()


async def test_a_background_reply_split_inside_a_multibyte_read_still_waits_for_its_terminator():
    # Raw bytes go through the stream decoder first; the reply header then holds the buffer without a timer.
    with mockTimers() as timers:
        replyBuffer = StdinBuffer()
        sequences: list[str] = []
        replyBuffer.on("data", sequences.append)
        try:
            replyBuffer.process(b"\x1b]11;rgb:eaea/6c")
            timers.tick(5000)
            replyBuffer.process(b"6c/6d6d\x07" + "日".encode()[:2])
            assert sequences == ["\x1b]11;rgb:eaea/6c6c/6d6d\x07"]
            replyBuffer.process("日".encode()[2:])
            assert sequences[-1] == "日"
        finally:
            replyBuffer.destroy()

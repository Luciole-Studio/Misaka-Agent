"""Translated from pi-tui's ``test/program-status.test.ts`` (v1.1.0)."""

import base64

from misaka.ui.tui.program_status import formatProgramStatus, isProgramStatusReply
from misaka.utils.js_regexp import RegExp


def decodeMessage(sequence: str) -> str | None:
    match = RegExp(r":msg=([A-Za-z0-9+/=]*)").exec(sequence)
    message = match[1] if match else None
    return None if message is None else base64.b64decode(message).decode("utf-8")


# #10607
# --- formatProgramStatus --------------------------------------------------------------------------


def test_encodes_state_app_kind_and_a_base64_message():
    assert (
        formatProgramStatus({"state": "blocked", "app": "pi", "kind": "permission", "message": "Allow bash?"})
        == f"\x1b]7501;state=blocked:app=pi:kind=permission:msg={base64.b64encode(b'Allow bash?').decode()}\x1b\\"
    )
    assert formatProgramStatus({"state": "clear"}) == "\x1b]7501;state=clear\x1b\\"


def test_omits_kind_outside_blocked_invalid_app_names_and_empty_messages():
    assert (
        formatProgramStatus({"state": "working", "app": "my app", "kind": "auth", "message": " \n "})
        == "\x1b]7501;state=working\x1b\\"
    )
    assert RegExp(r":app=a{32}\x1b").test(formatProgramStatus({"state": "idle", "app": "a" * 32}))
    assert not RegExp(r"app=").test(formatProgramStatus({"state": "idle", "app": "a" * 33}))


def test_replaces_control_characters_which_make_terminals_discard_the_report():
    sequence = formatProgramStatus({"state": "error", "message": "first\nsecond\x1b[31m\u009bthird\t"})
    assert decodeMessage(sequence) == "first second [31m third"


def test_cuts_long_messages_at_a_utf_8_boundary_within_the_spec_limits():
    sequence = formatProgramStatus({"state": "working", "app": "pi", "message": "é" * 2000})
    message = decodeMessage(sequence)
    assert message == "é" * 1024
    assert len(message.encode("utf-8")) <= 2048
    assert len(sequence) <= 4096


# --- isProgramStatusReply -------------------------------------------------------------------------


def test_accepts_the_query_echo_with_either_terminator_and_future_pairs():
    assert isProgramStatusReply("\x1b]7501;?\x1b\\") is True
    assert isProgramStatusReply("\x1b]7501;?\x07") is True
    assert isProgramStatusReply("\x1b]7501;?version=2\x1b\\") is True
    assert isProgramStatusReply("\x1b]7501;state=idle\x1b\\") is False
    assert isProgramStatusReply("\x1b]11;rgb:0000/0000/0000\x07") is False

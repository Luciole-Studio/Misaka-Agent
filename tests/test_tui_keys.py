"""Translated from pi-tui's ``test/keys.test.ts`` (v1.3.0).

Tests for keyboard input handling
"""

from collections.abc import Callable, Iterator

import pytest

from misaka.ui.tui.keys import (
    Key,
    decodeKittyPrintable,
    decodePrintableKey,
    matchesKey,
    parseKey,
    setKittyProtocolActive,
)


@pytest.fixture(autouse=True)
def _kittyProtocolInactiveAfterEachTest() -> Iterator[None]:
    # The protocol state is a module global; a failing assertion must not leave it on for the next test.
    yield
    setKittyProtocolActive(False)


@pytest.fixture
def withEnvVars(monkeypatch: pytest.MonkeyPatch) -> Callable[[dict[str, str | None], Callable[[], None]], None]:
    def run(vars_: dict[str, str | None], fn: Callable[[], None]) -> None:
        with monkeypatch.context() as patch:
            for name, value in vars_.items():
                if value is None:
                    patch.delenv(name, raising=False)
                else:
                    patch.setenv(name, value)
            fn()

    return run


# --- matchesKey: Kitty protocol with alternate keys (non-Latin layouts) ---------------------------
# Kitty protocol flag 4 (Report alternate keys) sends:
# CSI codepoint:shifted:base ; modifier:event u
# Where base is the key in standard PC-101 layout


def test_should_match_ctrl_c_when_pressing_ctrl_cyrillic_es_with_base_layout_key():
    setKittyProtocolActive(True)
    # Cyrillic 'с' = codepoint 1089, Latin 'c' = codepoint 99
    # Format: CSI 1089::99;5u (codepoint::base;modifier with ctrl=4, +1=5)
    cyrillicCtrlC = "\x1b[1089::99;5u"
    assert matchesKey(cyrillicCtrlC, "ctrl+c") is True
    setKittyProtocolActive(False)


def test_should_match_ctrl_d_when_pressing_ctrl_cyrillic_ve_with_base_layout_key():
    setKittyProtocolActive(True)
    # Cyrillic 'в' = codepoint 1074, Latin 'd' = codepoint 100
    cyrillicCtrlD = "\x1b[1074::100;5u"
    assert matchesKey(cyrillicCtrlD, "ctrl+d") is True
    setKittyProtocolActive(False)


def test_should_match_ctrl_z_when_pressing_ctrl_cyrillic_ya_with_base_layout_key():
    setKittyProtocolActive(True)
    # Cyrillic 'я' = codepoint 1103, Latin 'z' = codepoint 122
    cyrillicCtrlZ = "\x1b[1103::122;5u"
    assert matchesKey(cyrillicCtrlZ, "ctrl+z") is True
    setKittyProtocolActive(False)


def test_should_match_ctrl_shift_p_with_base_layout_key():
    setKittyProtocolActive(True)
    # Cyrillic 'з' = codepoint 1079, Latin 'p' = codepoint 112
    # ctrl=4, shift=1, +1 = 6
    cyrillicCtrlShiftP = "\x1b[1079::112;6u"
    assert matchesKey(cyrillicCtrlShiftP, "ctrl+shift+p") is True
    setKittyProtocolActive(False)


def test_should_still_match_direct_codepoint_when_no_base_layout_key():
    setKittyProtocolActive(True)
    # Latin ctrl+c without base layout key (terminal doesn't support flag 4)
    latinCtrlC = "\x1b[99;5u"
    assert matchesKey(latinCtrlC, "ctrl+c") is True
    setKittyProtocolActive(False)


def test_should_match_super_modified_kitty_bindings_including_combined_modifiers():
    setKittyProtocolActive(True)
    assert matchesKey("\x1b[107;9u", "super+k") is True
    assert matchesKey("\x1b[13;9u", "super+enter") is True
    assert matchesKey("\x1b[107;13u", Key.ctrlSuper("k")) is True
    assert matchesKey("\x1b[107;13u", "ctrl+super+k") is True
    assert matchesKey("\x1b[107;14u", "ctrl+shift+super+k") is True
    assert matchesKey("\x1b[107;13u", "super+k") is False
    assert parseKey("\x1b[107;9u") == "super+k"
    assert parseKey("\x1b[13;9u") == "super+enter"
    assert parseKey("\x1b[107;13u") == "ctrl+super+k"
    assert parseKey("\x1b[107;14u") == "shift+ctrl+super+k"
    setKittyProtocolActive(False)


def test_should_match_digit_bindings_via_kitty_csi_u():
    setKittyProtocolActive(True)
    assert matchesKey("\x1b[49u", "1") is True
    assert matchesKey("\x1b[49;5u", "ctrl+1") is True
    assert matchesKey("\x1b[49;5u", "ctrl+2") is False
    assert parseKey("\x1b[49u") == "1"
    assert parseKey("\x1b[49;5u") == "ctrl+1"
    setKittyProtocolActive(False)


def test_should_normalize_kitty_keypad_functional_keys_to_logical_digits_symbols_and_navigation():
    setKittyProtocolActive(True)
    assert matchesKey("\x1b[57400u", "1") is True
    assert matchesKey("\x1b[57410u", "/") is True
    assert matchesKey("\x1b[57417u", "left") is True
    assert matchesKey("\x1b[57426u", "delete") is True
    assert parseKey("\x1b[57399u") == "0"
    assert parseKey("\x1b[57409u") == "."
    assert parseKey("\x1b[57413u") == "+"
    assert parseKey("\x1b[57416u") == ","
    assert parseKey("\x1b[57417u") == "left"
    assert parseKey("\x1b[57418u") == "right"
    assert parseKey("\x1b[57419u") == "up"
    assert parseKey("\x1b[57420u") == "down"
    assert parseKey("\x1b[57421u") == "pageUp"
    assert parseKey("\x1b[57422u") == "pageDown"
    assert parseKey("\x1b[57423u") == "home"
    assert parseKey("\x1b[57424u") == "end"
    assert parseKey("\x1b[57425u") == "insert"
    assert parseKey("\x1b[57426u") == "delete"
    setKittyProtocolActive(False)


def test_should_match_shifted_symbols_by_their_produced_character():
    setKittyProtocolActive(True)
    assert matchesKey("\x1b[61:43;2u", "+") is True
    assert matchesKey("\x1b[61:43;2u", "shift+=") is True
    assert parseKey("\x1b[61:43;2u") == "+"
    assert matchesKey("\x1b[61:43;6u", "ctrl++") is True
    assert parseKey("\x1b[61:43;6u") == "ctrl++"
    setKittyProtocolActive(False)


def test_should_handle_shifted_key_in_format():
    setKittyProtocolActive(True)
    # Format with shifted key: CSI codepoint:shifted:base;modifier u
    # Latin 'c' with shifted 'C' (67) and base 'c' (99)
    shiftedKey = "\x1b[99:67:99;2u"  # shift modifier = 1, +1 = 2
    assert matchesKey(shiftedKey, "shift+c") is True
    setKittyProtocolActive(False)


def test_should_handle_event_type_in_format():
    setKittyProtocolActive(True)
    # Format with event type: CSI codepoint::base;modifier:event u
    # Cyrillic ctrl+c release event (event type 3)
    releaseEvent = "\x1b[1089::99;5:3u"
    assert matchesKey(releaseEvent, "ctrl+c") is True
    setKittyProtocolActive(False)


def test_should_handle_full_format_with_shifted_key_base_key_and_event_type():
    setKittyProtocolActive(True)
    # Full format: CSI codepoint:shifted:base;modifier:event u
    # Cyrillic 'С' (shifted) with base 'c', Ctrl+Shift pressed, repeat event
    # Cyrillic 'с' = 1089, Cyrillic 'С' = 1057, Latin 'c' = 99
    # ctrl=4, shift=1, +1 = 6, repeat event = 2
    fullFormat = "\x1b[1089:1057:99;6:2u"
    assert matchesKey(fullFormat, "ctrl+shift+c") is True
    setKittyProtocolActive(False)


def test_should_prefer_codepoint_for_latin_letters_even_when_base_layout_differs():
    setKittyProtocolActive(True)
    # Dvorak Ctrl+K reports codepoint 'k' (107) and base layout 'v' (118)
    dvorakCtrlK = "\x1b[107::118;5u"
    assert matchesKey(dvorakCtrlK, "ctrl+k") is True
    assert matchesKey(dvorakCtrlK, "ctrl+v") is False
    setKittyProtocolActive(False)


def test_should_prefer_codepoint_for_symbol_keys_even_when_base_layout_differs():
    setKittyProtocolActive(True)
    # Dvorak Ctrl+/ reports codepoint '/' (47) and base layout '[' (91)
    dvorakCtrlSlash = "\x1b[47::91;5u"
    assert matchesKey(dvorakCtrlSlash, "ctrl+/") is True
    assert matchesKey(dvorakCtrlSlash, "ctrl+[") is False
    setKittyProtocolActive(False)


def test_should_not_match_wrong_key_even_with_base_layout():
    setKittyProtocolActive(True)
    # Cyrillic ctrl+с with base 'c' should NOT match ctrl+d
    cyrillicCtrlC = "\x1b[1089::99;5u"
    assert matchesKey(cyrillicCtrlC, "ctrl+d") is False
    setKittyProtocolActive(False)


def test_should_not_match_wrong_modifiers_even_with_base_layout():
    setKittyProtocolActive(True)
    # Cyrillic ctrl+с should NOT match ctrl+shift+c
    cyrillicCtrlC = "\x1b[1089::99;5u"
    assert matchesKey(cyrillicCtrlC, "ctrl+shift+c") is False
    setKittyProtocolActive(False)


# --- matchesKey: modifyOtherKeys matching ---------------------------------------------------------


def test_should_match_xterm_modifyotherkeys_ctrl_c():
    setKittyProtocolActive(False)
    assert matchesKey("\x1b[27;5;99~", "ctrl+c") is True
    assert parseKey("\x1b[27;5;99~") == "ctrl+c"


def test_should_match_xterm_modifyotherkeys_ctrl_d():
    setKittyProtocolActive(False)
    assert matchesKey("\x1b[27;5;100~", "ctrl+d") is True
    assert parseKey("\x1b[27;5;100~") == "ctrl+d"


def test_should_match_xterm_modifyotherkeys_ctrl_z():
    setKittyProtocolActive(False)
    assert matchesKey("\x1b[27;5;122~", "ctrl+z") is True
    assert parseKey("\x1b[27;5;122~") == "ctrl+z"


def test_should_match_xterm_modifyotherkeys_enter_variants():
    setKittyProtocolActive(False)
    assert matchesKey("\x1b[27;5;13~", "ctrl+enter") is True
    assert matchesKey("\x1b[27;2;13~", "shift+enter") is True
    assert matchesKey("\x1b[27;3;13~", "alt+enter") is True
    assert parseKey("\x1b[27;5;13~") == "ctrl+enter"
    assert parseKey("\x1b[27;2;13~") == "shift+enter"
    assert parseKey("\x1b[27;3;13~") == "alt+enter"


def test_should_match_xterm_modifyotherkeys_tab_variants():
    setKittyProtocolActive(False)
    assert matchesKey("\x1b[27;2;9~", "shift+tab") is True
    assert matchesKey("\x1b[27;5;9~", "ctrl+tab") is True
    assert matchesKey("\x1b[27;3;9~", "alt+tab") is True
    assert parseKey("\x1b[27;2;9~") == "shift+tab"
    assert parseKey("\x1b[27;5;9~") == "ctrl+tab"
    assert parseKey("\x1b[27;3;9~") == "alt+tab"


def test_should_match_xterm_modifyotherkeys_backspace_variants():
    setKittyProtocolActive(False)
    assert matchesKey("\x1b[27;1;127~", "backspace") is True
    assert matchesKey("\x1b[27;5;127~", "ctrl+backspace") is True
    assert matchesKey("\x1b[27;3;127~", "alt+backspace") is True
    assert parseKey("\x1b[27;1;127~") == "backspace"
    assert parseKey("\x1b[27;5;127~") == "ctrl+backspace"
    assert parseKey("\x1b[27;3;127~") == "alt+backspace"


def test_should_match_xterm_modifyotherkeys_escape():
    setKittyProtocolActive(False)
    assert matchesKey("\x1b[27;1;27~", "escape") is True
    assert parseKey("\x1b[27;1;27~") == "escape"


def test_should_match_xterm_modifyotherkeys_space_variants():
    setKittyProtocolActive(False)
    assert matchesKey("\x1b[27;1;32~", "space") is True
    assert matchesKey("\x1b[27;5;32~", "ctrl+space") is True
    assert parseKey("\x1b[27;1;32~") == "space"
    assert parseKey("\x1b[27;5;32~") == "ctrl+space"


def test_should_match_xterm_modifyotherkeys_symbol_combos():
    setKittyProtocolActive(False)
    assert matchesKey("\x1b[27;5;47~", "ctrl+/") is True
    assert parseKey("\x1b[27;5;47~") == "ctrl+/"
    assert matchesKey("\x1b[27;6;43~", "ctrl++") is True
    assert parseKey("\x1b[27;6;43~") == "ctrl++"


def test_should_match_xterm_modifyotherkeys_digit_combos():
    setKittyProtocolActive(False)
    assert matchesKey("\x1b[27;5;49~", "ctrl+1") is True
    assert matchesKey("\x1b[27;2;49~", "shift+1") is True
    assert parseKey("\x1b[27;5;49~") == "ctrl+1"
    assert parseKey("\x1b[27;2;49~") == "shift+1"


def test_should_match_xterm_modifyotherkeys_shifted_uppercase_letters():
    setKittyProtocolActive(False)
    assert matchesKey("\x1b[27;2;69~", "shift+e") is True
    assert matchesKey("\x1b[27;6;69~", "ctrl+shift+e") is True
    assert parseKey("\x1b[27;2;69~") == "shift+e"
    assert parseKey("\x1b[27;6;69~") == "shift+ctrl+e"


def test_should_match_ctrl_alt_letter_via_csi_u_when_kitty_inactive():
    setKittyProtocolActive(False)
    assert matchesKey("\x1b[104;7u", "ctrl+alt+h") is True
    assert parseKey("\x1b[104;7u") == "ctrl+alt+h"


def test_should_match_ctrl_alt_letter_via_xterm_modifyotherkeys():
    setKittyProtocolActive(False)
    assert matchesKey("\x1b[27;7;104~", "ctrl+alt+h") is True
    assert parseKey("\x1b[27;7;104~") == "ctrl+alt+h"


# --- matchesKey: Legacy key matching --------------------------------------------------------------


def test_should_match_legacy_ctrl_c():
    setKittyProtocolActive(False)
    # Ctrl+c sends ASCII 3 (ETX)
    assert matchesKey("\x03", "ctrl+c") is True


def test_should_match_legacy_ctrl_d():
    setKittyProtocolActive(False)
    # Ctrl+d sends ASCII 4 (EOT)
    assert matchesKey("\x04", "ctrl+d") is True


def test_should_match_escape_key():
    assert matchesKey("\x1b", "escape") is True


def test_should_match_legacy_linefeed_as_enter():
    setKittyProtocolActive(False)
    assert matchesKey("\n", "enter") is True
    assert parseKey("\n") == "enter"


def test_should_treat_linefeed_as_shift_enter_when_kitty_active():
    setKittyProtocolActive(True)
    assert matchesKey("\n", "shift+enter") is True
    assert matchesKey("\n", "enter") is False
    assert parseKey("\n") == "shift+enter"
    setKittyProtocolActive(False)


def test_should_parse_ctrl_space():
    setKittyProtocolActive(False)
    assert matchesKey("\x00", "ctrl+space") is True
    assert parseKey("\x00") == "ctrl+space"


def test_should_match_legacy_ctrl_symbol():
    setKittyProtocolActive(False)
    # Ctrl+\ sends ASCII 28 (File Separator) in legacy terminals
    assert matchesKey("\x1c", "ctrl+\\") is True
    assert parseKey("\x1c") == "ctrl+\\"
    # Ctrl+] sends ASCII 29 (Group Separator) in legacy terminals
    assert matchesKey("\x1d", "ctrl+]") is True
    assert parseKey("\x1d") == "ctrl+]"
    # Ctrl+_ sends ASCII 31 (Unit Separator) in legacy terminals
    # Ctrl+- is on the same physical key on US keyboards
    assert matchesKey("\x1f", "ctrl+_") is True
    assert matchesKey("\x1f", "ctrl+-") is True
    assert parseKey("\x1f") == "ctrl+-"


def test_should_match_legacy_ctrl_alt_symbol():
    setKittyProtocolActive(False)
    # Ctrl+Alt+[ sends ESC followed by ESC (Ctrl+[ = ESC)
    assert matchesKey("\x1b\x1b", "ctrl+alt+[") is True
    assert parseKey("\x1b\x1b") == "ctrl+alt+["
    # Ctrl+Alt+\ sends ESC followed by ASCII 28
    assert matchesKey("\x1b\x1c", "ctrl+alt+\\") is True
    assert parseKey("\x1b\x1c") == "ctrl+alt+\\"
    # Ctrl+Alt+] sends ESC followed by ASCII 29
    assert matchesKey("\x1b\x1d", "ctrl+alt+]") is True
    assert parseKey("\x1b\x1d") == "ctrl+alt+]"
    # Ctrl+_ sends ASCII 31 (Unit Separator) in legacy terminals
    # Ctrl+- is on the same physical key on US keyboards
    assert matchesKey("\x1b\x1f", "ctrl+alt+_") is True
    assert matchesKey("\x1b\x1f", "ctrl+alt+-") is True
    assert parseKey("\x1b\x1f") == "ctrl+alt+-"


def test_should_treat_raw_0x08_as_plain_backspace_outside_windows_terminal(withEnvVars):
    setKittyProtocolActive(False)

    def check() -> None:
        assert matchesKey("\x7f", "backspace") is True
        assert matchesKey("\x7f", "ctrl+backspace") is False
        assert parseKey("\x7f") == "backspace"
        assert matchesKey("\x08", "backspace") is True
        assert matchesKey("\x08", "ctrl+backspace") is False
        assert parseKey("\x08") == "backspace"
        assert matchesKey("\x08", "ctrl+h") is True

    withEnvVars({"WT_SESSION": None}, check)


def test_should_treat_raw_0x08_as_ctrl_backspace_in_local_windows_terminal(withEnvVars):
    setKittyProtocolActive(False)

    def check() -> None:
        assert matchesKey("\x08", "ctrl+backspace") is True
        assert matchesKey("\x08", "backspace") is False
        assert parseKey("\x08") == "ctrl+backspace"
        assert matchesKey("\x08", "ctrl+h") is True

    withEnvVars(
        {
            "WT_SESSION": "test-session",
            "SSH_CONNECTION": None,
            "SSH_CLIENT": None,
            "SSH_TTY": None,
        },
        check,
    )


def test_should_treat_raw_0x08_as_plain_backspace_in_windows_terminal_over_ssh(withEnvVars):
    setKittyProtocolActive(False)

    def check() -> None:
        assert matchesKey("\x08", "ctrl+backspace") is False
        assert matchesKey("\x08", "backspace") is True
        assert parseKey("\x08") == "backspace"
        assert matchesKey("\x08", "ctrl+h") is True

    withEnvVars(
        {
            "WT_SESSION": "test-session",
            "SSH_CONNECTION": "1 2 3 4",
            "SSH_CLIENT": "1 2 3",
            "SSH_TTY": "/dev/pts/1",
        },
        check,
    )


def test_should_parse_legacy_alt_prefixed_sequences_when_kitty_inactive():
    setKittyProtocolActive(False)
    assert matchesKey("\x1b ", "alt+space") is True
    assert parseKey("\x1b ") == "alt+space"
    assert matchesKey("\x1b\b", "alt+backspace") is True
    assert parseKey("\x1b\b") == "alt+backspace"
    assert matchesKey("\x1b\x03", "ctrl+alt+c") is True
    assert parseKey("\x1b\x03") == "ctrl+alt+c"
    assert matchesKey("\x1bB", "alt+left") is True
    assert parseKey("\x1bB") == "alt+left"
    assert matchesKey("\x1bF", "alt+right") is True
    assert parseKey("\x1bF") == "alt+right"
    assert matchesKey("\x1ba", "alt+a") is True
    assert parseKey("\x1ba") == "alt+a"
    assert matchesKey("\x1b1", "alt+1") is True
    assert parseKey("\x1b1") == "alt+1"
    assert matchesKey("\x1b,", "alt+,") is True
    assert parseKey("\x1b,") == "alt+,"
    assert matchesKey("\x1b.", "alt+.") is True
    assert parseKey("\x1b.") == "alt+."
    assert matchesKey("\x1by", "alt+y") is True
    assert parseKey("\x1by") == "alt+y"
    assert matchesKey("\x1bz", "alt+z") is True
    assert parseKey("\x1bz") == "alt+z"

    setKittyProtocolActive(True)
    assert matchesKey("\x1b ", "alt+space") is False
    assert parseKey("\x1b ") is None
    assert matchesKey("\x1b\b", "alt+backspace") is True
    assert parseKey("\x1b\b") == "alt+backspace"
    assert matchesKey("\x1b\x03", "ctrl+alt+c") is False
    assert parseKey("\x1b\x03") is None
    assert matchesKey("\x1bB", "alt+left") is False
    assert parseKey("\x1bB") is None
    assert matchesKey("\x1bF", "alt+right") is False
    assert parseKey("\x1bF") is None
    assert matchesKey("\x1ba", "alt+a") is False
    assert parseKey("\x1ba") is None
    assert matchesKey("\x1b1", "alt+1") is False
    assert parseKey("\x1b1") is None
    assert matchesKey("\x1b,", "alt+,") is False
    assert parseKey("\x1b,") is None
    assert matchesKey("\x1b.", "alt+.") is False
    assert parseKey("\x1b.") is None
    assert matchesKey("\x1by", "alt+y") is False
    assert parseKey("\x1by") is None
    setKittyProtocolActive(False)


def test_should_match_arrow_keys():
    assert matchesKey("\x1b[A", "up") is True
    assert matchesKey("\x1b[B", "down") is True
    assert matchesKey("\x1b[C", "right") is True
    assert matchesKey("\x1b[D", "left") is True


def test_should_match_ss3_arrows_and_home_end():
    assert matchesKey("\x1bOA", "up") is True
    assert matchesKey("\x1bOB", "down") is True
    assert matchesKey("\x1bOC", "right") is True
    assert matchesKey("\x1bOD", "left") is True
    assert matchesKey("\x1bOH", "home") is True
    assert matchesKey("\x1bOF", "end") is True


def test_should_match_xterm_ctrl_modified_viewport_navigation():
    assert matchesKey("\x1b[1;5H", "ctrl+home") is True
    assert matchesKey("\x1b[1;5F", "ctrl+end") is True
    assert matchesKey("\x1b[5;5~", "ctrl+pageUp") is True
    assert matchesKey("\x1b[6;5~", "ctrl+pageDown") is True
    assert parseKey("\x1b[1;5H") == "ctrl+home"
    assert parseKey("\x1b[1;5F") == "ctrl+end"
    assert parseKey("\x1b[5;5~") == "ctrl+pageUp"
    assert parseKey("\x1b[6;5~") == "ctrl+pageDown"


def test_should_match_legacy_function_keys_and_clear():
    assert matchesKey("\x1bOP", "f1") is True
    assert matchesKey("\x1b[24~", "f12") is True
    assert matchesKey("\x1b[E", "clear") is True


def test_should_match_alt_arrows():
    assert matchesKey("\x1bp", "alt+up") is True
    assert matchesKey("\x1bp", "up") is False


def test_should_match_rxvt_modifier_sequences():
    assert matchesKey("\x1b[a", "shift+up") is True
    assert matchesKey("\x1bOa", "ctrl+up") is True
    assert matchesKey("\x1b[2$", "shift+insert") is True
    assert matchesKey("\x1b[2^", "ctrl+insert") is True
    assert matchesKey("\x1b[7$", "shift+home") is True


# --- decodeKittyPrintable -------------------------------------------------------------------------


def test_should_decode_kitty_keypad_functional_keys_to_printable_characters():
    assert decodeKittyPrintable("\x1b[57399u") == "0"
    assert decodeKittyPrintable("\x1b[57400u") == "1"
    assert decodeKittyPrintable("\x1b[57409u") == "."
    assert decodeKittyPrintable("\x1b[57410u") == "/"
    assert decodeKittyPrintable("\x1b[57411u") == "*"
    assert decodeKittyPrintable("\x1b[57412u") == "-"
    assert decodeKittyPrintable("\x1b[57413u") == "+"
    assert decodeKittyPrintable("\x1b[57415u") == "="
    assert decodeKittyPrintable("\x1b[57416u") == ","
    assert decodeKittyPrintable("\x1b[57417u") is None


# --- decodePrintableKey ---------------------------------------------------------------------------


def test_should_decode_printable_xterm_modifyotherkeys_sequences():
    assert decodePrintableKey("\x1b[27;2;69~") == "E"
    assert decodePrintableKey("\x1b[27;2;196~") == "Ä"
    assert decodePrintableKey("\x1b[27;2;32~") == " "
    assert decodePrintableKey("\x1b[27;2;13~") is None
    assert decodePrintableKey("\x1b[27;6;69~") is None


# --- parseKey: Kitty protocol with alternate keys -------------------------------------------------


def test_should_return_latin_key_name_when_base_layout_key_is_present():
    setKittyProtocolActive(True)
    # Cyrillic ctrl+с with base layout 'c'
    cyrillicCtrlC = "\x1b[1089::99;5u"
    assert parseKey(cyrillicCtrlC) == "ctrl+c"
    setKittyProtocolActive(False)


def test_should_prefer_codepoint_for_latin_letters_when_base_layout_differs():
    setKittyProtocolActive(True)
    # Dvorak Ctrl+K reports codepoint 'k' (107) and base layout 'v' (118)
    dvorakCtrlK = "\x1b[107::118;5u"
    assert parseKey(dvorakCtrlK) == "ctrl+k"
    setKittyProtocolActive(False)


def test_should_prefer_codepoint_for_symbol_keys_when_base_layout_differs():
    setKittyProtocolActive(True)
    # Dvorak Ctrl+/ reports codepoint '/' (47) and base layout '[' (91)
    dvorakCtrlSlash = "\x1b[47::91;5u"
    assert parseKey(dvorakCtrlSlash) == "ctrl+/"
    setKittyProtocolActive(False)


def test_should_return_key_name_from_codepoint_when_no_base_layout():
    setKittyProtocolActive(True)
    latinCtrlC = "\x1b[99;5u"
    assert parseKey(latinCtrlC) == "ctrl+c"
    setKittyProtocolActive(False)


def test_should_parse_shifted_uppercase_csi_u_letters_as_shift_letter():
    setKittyProtocolActive(True)
    assert matchesKey("\x1b[69;2u", "shift+e") is True
    assert parseKey("\x1b[69;2u") == "shift+e"
    setKittyProtocolActive(False)


def test_should_ignore_kitty_csi_u_with_unsupported_modifiers():
    setKittyProtocolActive(True)
    assert parseKey("\x1b[99;17u") is None
    setKittyProtocolActive(False)


# --- parseKey: Legacy key parsing -----------------------------------------------------------------


def test_should_parse_legacy_ctrl_letter():
    setKittyProtocolActive(False)
    assert parseKey("\x03") == "ctrl+c"
    assert parseKey("\x04") == "ctrl+d"


def test_should_parse_special_keys():
    assert parseKey("\x1b") == "escape"
    assert parseKey("\t") == "tab"
    assert parseKey("\r") == "enter"
    assert parseKey("\n") == "enter"
    assert parseKey("\x00") == "ctrl+space"
    assert parseKey(" ") == "space"
    assert parseKey("1") == "1"
    assert matchesKey("1", "1") is True
    assert parseKey("+") == "+"
    assert matchesKey("+", "+") is True


def test_should_parse_arrow_keys():
    assert parseKey("\x1b[A") == "up"
    assert parseKey("\x1b[B") == "down"
    assert parseKey("\x1b[C") == "right"
    assert parseKey("\x1b[D") == "left"


def test_should_parse_ss3_arrows_and_home_end():
    assert parseKey("\x1bOA") == "up"
    assert parseKey("\x1bOB") == "down"
    assert parseKey("\x1bOC") == "right"
    assert parseKey("\x1bOD") == "left"
    assert parseKey("\x1bOH") == "home"
    assert parseKey("\x1bOF") == "end"


def test_should_parse_legacy_function_and_modifier_sequences():
    assert parseKey("\x1bOP") == "f1"
    assert parseKey("\x1b[24~") == "f12"
    assert parseKey("\x1b[E") == "clear"
    assert parseKey("\x1b[2^") == "ctrl+insert"
    assert parseKey("\x1bp") == "alt+up"


def test_should_parse_double_bracket_pageup():
    assert parseKey("\x1b[[5~") == "pageUp"


# --- MISAKA: JavaScript semantics the port keeps --------------------------------------------------


def test_a_sequence_with_a_trailing_newline_is_not_a_key():
    # `$` is the end of the input, as in JavaScript (Python's `$` also matches before a final newline).
    assert matchesKey("\x1b[99;5u\n", "ctrl+c") is False
    assert matchesKey("\x1b[27;5;99~\n", "ctrl+c") is False
    assert decodeKittyPrintable("\x1b[97u\n") is None


def test_only_ascii_digits_form_a_sequence():
    # `\d` is ASCII, as in JavaScript.
    assert matchesKey("\x1b[٩٩;5u", "ctrl+c") is False


def test_out_of_range_codepoints_do_not_raise():
    # `String.fromCharCode` truncates to a UTF-16 code unit where `chr` would raise.
    setKittyProtocolActive(True)
    assert matchesKey("\x1b[57417::99;5u", "ctrl+c") is True
    assert matchesKey("\x1b[1114112::99;5u", "ctrl+c") is True
    assert parseKey("\x1b[1114112u") is None
    assert decodeKittyPrintable("\x1b[1114112u") is None
    assert decodeKittyPrintable(f"\x1b[{'9' * 400}u") is None


def test_the_logical_shifted_symbol_match_leaves_cjk_and_fullwidth_input_alone():
    # An IME commit (日) is no key, and a fullwidth ＋ is not the ASCII symbol `+`: neither is consumed as Shift.
    setKittyProtocolActive(True)
    assert matchesKey("\x1b[26085u", "+") is False
    assert parseKey("\x1b[26085u") is None
    assert decodeKittyPrintable("\x1b[26085u") == "日"
    assert matchesKey("\x1b[61:65291;2u", "+") is False
    assert matchesKey("\x1b[61:65291;2u", "shift+=") is True
    assert parseKey("\x1b[61:65291;2u") == "shift+="
    setKittyProtocolActive(False)
    assert decodePrintableKey("\x1b[27;2;26085~") == "日"
    assert matchesKey("\x1b[27;2;26085~", "+") is False
    assert matchesKey("\x1b[27;2;65291~", "+") is False


def test_a_jis_layout_shifted_symbol_matches_the_symbol_it_produces():
    # On a JIS keyboard Shift+- types `=`: Kitty reports `-` with the shifted key `=`.
    setKittyProtocolActive(True)
    assert parseKey("\x1b[45:61;2u") == "="
    assert matchesKey("\x1b[45:61;2u", "=") is True
    assert matchesKey("\x1b[45:61;2u", "shift+-") is True
    # `"` (Shift+2) is not a bindable symbol, so it stays the physical key.
    assert parseKey("\x1b[50:34;2u") == "shift+2"
    setKittyProtocolActive(False)


def test_editing_keys_still_match_with_the_new_modifier_order():
    # The keys the editor's CJK backspace and IME paths rely on: the physical match comes first, unchanged.
    setKittyProtocolActive(True)
    assert matchesKey("\x1b[127u", "backspace") is True
    assert matchesKey("\x1b[127;2u", "shift+backspace") is True
    assert matchesKey("\x1b[127;2u", "backspace") is False
    assert matchesKey("\x1b[13u", "enter") is True
    setKittyProtocolActive(False)


def test_key_ids_parse_modifier_prefixes_only():
    # Unknown or repeated modifiers name no key (they used to fall back to the last `+` part); ids lowercase as before.
    assert matchesKey("x", "meta+x") is False
    assert matchesKey("\x03", "ctrl+ctrl+c") is False
    assert matchesKey("\x03", "Ctrl+C") is True

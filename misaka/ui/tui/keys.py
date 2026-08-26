"""Keyboard input handling for terminal applications."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

type KeyId = str
type KeyEventType = str

_kitty_protocol_active = False


def setKittyProtocolActive(active: bool) -> None:
    global _kitty_protocol_active
    _kitty_protocol_active = active


def isKittyProtocolActive() -> bool:
    return _kitty_protocol_active


class _KeyHelper:
    escape = "escape"
    esc = "esc"
    enter = "enter"
    tab = "tab"
    space = "space"
    backspace = "backspace"
    delete = "delete"
    insert = "insert"
    clear = "clear"
    home = "home"
    end = "end"
    pageUp = "pageUp"
    pageDown = "pageDown"
    up = "up"
    down = "down"
    left = "left"
    right = "right"
    f1 = "f1"
    f2 = "f2"
    f3 = "f3"
    f4 = "f4"
    f5 = "f5"
    f6 = "f6"
    f7 = "f7"
    f8 = "f8"
    f9 = "f9"
    f10 = "f10"
    f11 = "f11"
    f12 = "f12"
    backtick = "`"
    hyphen = "-"
    equals = "="
    leftbracket = "["
    rightbracket = "]"
    backslash = "\\"
    semicolon = ";"
    quote = "'"
    comma = ","
    period = "."
    slash = "/"
    exclamation = "!"
    at = "@"
    hash = "#"
    dollar = "$"
    percent = "%"
    caret = "^"
    ampersand = "&"
    asterisk = "*"
    leftparen = "("
    rightparen = ")"
    underscore = "_"
    plus = "+"
    pipe = "|"
    tilde = "~"
    leftbrace = "{"
    rightbrace = "}"
    colon = ":"
    lessthan = "<"
    greaterthan = ">"
    question = "?"

    @staticmethod
    def ctrl(key: str) -> str:
        return f"ctrl+{key}"

    @staticmethod
    def shift(key: str) -> str:
        return f"shift+{key}"

    @staticmethod
    def alt(key: str) -> str:
        return f"alt+{key}"

    @staticmethod
    def super(key: str) -> str:
        return f"super+{key}"

    @staticmethod
    def ctrlShift(key: str) -> str:
        return f"ctrl+shift+{key}"

    @staticmethod
    def shiftCtrl(key: str) -> str:
        return f"shift+ctrl+{key}"

    @staticmethod
    def ctrlAlt(key: str) -> str:
        return f"ctrl+alt+{key}"

    @staticmethod
    def altCtrl(key: str) -> str:
        return f"alt+ctrl+{key}"

    @staticmethod
    def shiftAlt(key: str) -> str:
        return f"shift+alt+{key}"

    @staticmethod
    def altShift(key: str) -> str:
        return f"alt+shift+{key}"

    @staticmethod
    def ctrlSuper(key: str) -> str:
        return f"ctrl+super+{key}"

    @staticmethod
    def superCtrl(key: str) -> str:
        return f"super+ctrl+{key}"

    @staticmethod
    def shiftSuper(key: str) -> str:
        return f"shift+super+{key}"

    @staticmethod
    def superShift(key: str) -> str:
        return f"super+shift+{key}"

    @staticmethod
    def altSuper(key: str) -> str:
        return f"alt+super+{key}"

    @staticmethod
    def superAlt(key: str) -> str:
        return f"super+alt+{key}"

    @staticmethod
    def ctrlShiftAlt(key: str) -> str:
        return f"ctrl+shift+alt+{key}"

    @staticmethod
    def ctrlShiftSuper(key: str) -> str:
        return f"ctrl+shift+super+{key}"


Key = _KeyHelper()
setattr(Key, "return", "return")

SYMBOL_KEYS = {
    "`",
    "-",
    "=",
    "[",
    "]",
    "\\",
    ";",
    "'",
    ",",
    ".",
    "/",
    "!",
    "@",
    "#",
    "$",
    "%",
    "^",
    "&",
    "*",
    "(",
    ")",
    "_",
    "+",
    "|",
    "~",
    "{",
    "}",
    ":",
    "<",
    ">",
    "?",
}

MODIFIERS = {
    "shift": 1,
    "alt": 2,
    "ctrl": 4,
    "super": 8,
}

LOCK_MASK = 64 + 128

CODEPOINTS = {
    "escape": 27,
    "tab": 9,
    "enter": 13,
    "space": 32,
    "backspace": 127,
    "kpEnter": 57414,
}

ARROW_CODEPOINTS = {
    "up": -1,
    "down": -2,
    "right": -3,
    "left": -4,
}

FUNCTIONAL_CODEPOINTS = {
    "delete": -10,
    "insert": -11,
    "pageUp": -12,
    "pageDown": -13,
    "home": -14,
    "end": -15,
}

KITTY_FUNCTIONAL_KEY_EQUIVALENTS = {
    57399: 48,
    57400: 49,
    57401: 50,
    57402: 51,
    57403: 52,
    57404: 53,
    57405: 54,
    57406: 55,
    57407: 56,
    57408: 57,
    57409: 46,
    57410: 47,
    57411: 42,
    57412: 45,
    57413: 43,
    57415: 61,
    57416: 44,
    57417: ARROW_CODEPOINTS["left"],
    57418: ARROW_CODEPOINTS["right"],
    57419: ARROW_CODEPOINTS["up"],
    57420: ARROW_CODEPOINTS["down"],
    57421: FUNCTIONAL_CODEPOINTS["pageUp"],
    57422: FUNCTIONAL_CODEPOINTS["pageDown"],
    57423: FUNCTIONAL_CODEPOINTS["home"],
    57424: FUNCTIONAL_CODEPOINTS["end"],
    57425: FUNCTIONAL_CODEPOINTS["insert"],
    57426: FUNCTIONAL_CODEPOINTS["delete"],
}

LEGACY_KEY_SEQUENCES = {
    "up": ["\x1b[A", "\x1bOA"],
    "down": ["\x1b[B", "\x1bOB"],
    "right": ["\x1b[C", "\x1bOC"],
    "left": ["\x1b[D", "\x1bOD"],
    "home": ["\x1b[H", "\x1bOH", "\x1b[1~", "\x1b[7~"],
    "end": ["\x1b[F", "\x1bOF", "\x1b[4~", "\x1b[8~"],
    "insert": ["\x1b[2~"],
    "delete": ["\x1b[3~"],
    "pageUp": ["\x1b[5~", "\x1b[[5~"],
    "pageDown": ["\x1b[6~", "\x1b[[6~"],
    "clear": ["\x1b[E", "\x1bOE"],
    "f1": ["\x1bOP", "\x1b[11~", "\x1b[[A"],
    "f2": ["\x1bOQ", "\x1b[12~", "\x1b[[B"],
    "f3": ["\x1bOR", "\x1b[13~", "\x1b[[C"],
    "f4": ["\x1bOS", "\x1b[14~", "\x1b[[D"],
    "f5": ["\x1b[15~", "\x1b[[E"],
    "f6": ["\x1b[17~"],
    "f7": ["\x1b[18~"],
    "f8": ["\x1b[19~"],
    "f9": ["\x1b[20~"],
    "f10": ["\x1b[21~"],
    "f11": ["\x1b[23~"],
    "f12": ["\x1b[24~"],
}

LEGACY_SHIFT_SEQUENCES = {
    "up": ["\x1b[a"],
    "down": ["\x1b[b"],
    "right": ["\x1b[c"],
    "left": ["\x1b[d"],
    "clear": ["\x1b[e"],
    "insert": ["\x1b[2$"],
    "delete": ["\x1b[3$"],
    "pageUp": ["\x1b[5$"],
    "pageDown": ["\x1b[6$"],
    "home": ["\x1b[7$"],
    "end": ["\x1b[8$"],
}

LEGACY_CTRL_SEQUENCES = {
    "up": ["\x1bOa"],
    "down": ["\x1bOb"],
    "right": ["\x1bOc"],
    "left": ["\x1bOd"],
    "clear": ["\x1bOe"],
    "insert": ["\x1b[2^"],
    "delete": ["\x1b[3^"],
    "pageUp": ["\x1b[5^"],
    "pageDown": ["\x1b[6^"],
    "home": ["\x1b[7^"],
    "end": ["\x1b[8^"],
}

LEGACY_SEQUENCE_KEY_IDS: dict[str, KeyId] = {
    "\x1bOA": "up",
    "\x1bOB": "down",
    "\x1bOC": "right",
    "\x1bOD": "left",
    "\x1bOH": "home",
    "\x1bOF": "end",
    "\x1b[E": "clear",
    "\x1bOE": "clear",
    "\x1bOe": "ctrl+clear",
    "\x1b[e": "shift+clear",
    "\x1b[2~": "insert",
    "\x1b[2$": "shift+insert",
    "\x1b[2^": "ctrl+insert",
    "\x1b[3$": "shift+delete",
    "\x1b[3^": "ctrl+delete",
    "\x1b[[5~": "pageUp",
    "\x1b[[6~": "pageDown",
    "\x1b[a": "shift+up",
    "\x1b[b": "shift+down",
    "\x1b[c": "shift+right",
    "\x1b[d": "shift+left",
    "\x1bOa": "ctrl+up",
    "\x1bOb": "ctrl+down",
    "\x1bOc": "ctrl+right",
    "\x1bOd": "ctrl+left",
    "\x1b[5$": "shift+pageUp",
    "\x1b[6$": "shift+pageDown",
    "\x1b[7$": "shift+home",
    "\x1b[8$": "shift+end",
    "\x1b[5^": "ctrl+pageUp",
    "\x1b[6^": "ctrl+pageDown",
    "\x1b[7^": "ctrl+home",
    "\x1b[8^": "ctrl+end",
    "\x1bOP": "f1",
    "\x1bOQ": "f2",
    "\x1bOR": "f3",
    "\x1bOS": "f4",
    "\x1b[11~": "f1",
    "\x1b[12~": "f2",
    "\x1b[13~": "f3",
    "\x1b[14~": "f4",
    "\x1b[[A": "f1",
    "\x1b[[B": "f2",
    "\x1b[[C": "f3",
    "\x1b[[D": "f4",
    "\x1b[[E": "f5",
    "\x1b[15~": "f5",
    "\x1b[17~": "f6",
    "\x1b[18~": "f7",
    "\x1b[19~": "f8",
    "\x1b[20~": "f9",
    "\x1b[21~": "f10",
    "\x1b[23~": "f11",
    "\x1b[24~": "f12",
    "\x1bb": "alt+left",
    "\x1bf": "alt+right",
    "\x1bp": "alt+up",
    "\x1bn": "alt+down",
}

KITTY_CSI_U_REGEX = re.compile(r"^\x1b\[(\d+)(?::(\d*))?(?::(\d+))?(?:;(\d+))?(?::(\d+))?u$")
KITTY_PRINTABLE_ALLOWED_MODIFIERS = MODIFIERS["shift"] | LOCK_MASK


@dataclass(slots=True)
class ParsedKittySequence:
    codepoint: int
    modifier: int
    eventType: KeyEventType
    shiftedKey: int | None = None
    baseLayoutKey: int | None = None


@dataclass(slots=True)
class ParsedModifyOtherKeysSequence:
    codepoint: int
    modifier: int


def normalize_kitty_functional_codepoint(codepoint: int) -> int:
    return KITTY_FUNCTIONAL_KEY_EQUIVALENTS.get(codepoint, codepoint)


def normalize_shifted_letter_identity_codepoint(codepoint: int, modifier: int) -> int:
    effective_modifier = modifier & ~LOCK_MASK
    if effective_modifier & MODIFIERS["shift"] and 65 <= codepoint <= 90:
        return codepoint + 32
    return codepoint


def matches_legacy_sequence(data: str, sequences: list[str]) -> bool:
    return data in sequences


def matches_legacy_modifier_sequence(data: str, key: str, modifier: int) -> bool:
    if modifier == MODIFIERS["shift"]:
        return matches_legacy_sequence(data, LEGACY_SHIFT_SEQUENCES[key])
    if modifier == MODIFIERS["ctrl"]:
        return matches_legacy_sequence(data, LEGACY_CTRL_SEQUENCES[key])
    return False


def isKeyRelease(data: str) -> bool:
    if "\x1b[200~" in data:
        return False
    return any(token in data for token in (":3u", ":3~", ":3A", ":3B", ":3C", ":3D", ":3H", ":3F"))


def isKeyRepeat(data: str) -> bool:
    if "\x1b[200~" in data:
        return False
    return any(token in data for token in (":2u", ":2~", ":2A", ":2B", ":2C", ":2D", ":2H", ":2F"))


def parse_event_type(event_type_str: str | None) -> KeyEventType:
    if event_type_str is None:
        return "press"
    if int(event_type_str) == 2:
        return "repeat"
    if int(event_type_str) == 3:
        return "release"
    return "press"


def parse_kitty_sequence(data: str) -> ParsedKittySequence | None:
    match = KITTY_CSI_U_REGEX.match(data)
    if match is not None:
        codepoint = int(match.group(1))
        shifted_key = int(match.group(2)) if match.group(2) else None
        base_layout_key = int(match.group(3)) if match.group(3) else None
        mod_value = int(match.group(4)) if match.group(4) else 1
        event_type = parse_event_type(match.group(5))
        return ParsedKittySequence(
            codepoint=codepoint,
            shiftedKey=shifted_key,
            baseLayoutKey=base_layout_key,
            modifier=mod_value - 1,
            eventType=event_type,
        )

    arrow_match = re.match(r"^\x1b\[1;(\d+)(?::(\d+))?([ABCD])$", data)
    if arrow_match is not None:
        arrow_codes = {
            "A": ARROW_CODEPOINTS["up"],
            "B": ARROW_CODEPOINTS["down"],
            "C": ARROW_CODEPOINTS["right"],
            "D": ARROW_CODEPOINTS["left"],
        }
        return ParsedKittySequence(
            codepoint=arrow_codes[arrow_match.group(3)],
            modifier=int(arrow_match.group(1)) - 1,
            eventType=parse_event_type(arrow_match.group(2)),
        )

    func_match = re.match(r"^\x1b\[(\d+)(?:;(\d+))?(?::(\d+))?~$", data)
    if func_match is not None:
        func_codes = {
            2: FUNCTIONAL_CODEPOINTS["insert"],
            3: FUNCTIONAL_CODEPOINTS["delete"],
            5: FUNCTIONAL_CODEPOINTS["pageUp"],
            6: FUNCTIONAL_CODEPOINTS["pageDown"],
            7: FUNCTIONAL_CODEPOINTS["home"],
            8: FUNCTIONAL_CODEPOINTS["end"],
        }
        key_num = int(func_match.group(1))
        codepoint = func_codes.get(key_num)
        if codepoint is not None:
            return ParsedKittySequence(
                codepoint=codepoint,
                modifier=(int(func_match.group(2)) if func_match.group(2) else 1) - 1,
                eventType=parse_event_type(func_match.group(3)),
            )

    home_end_match = re.match(r"^\x1b\[1;(\d+)(?::(\d+))?([HF])$", data)
    if home_end_match is not None:
        codepoint = (
            FUNCTIONAL_CODEPOINTS["home"]
            if home_end_match.group(3) == "H"
            else FUNCTIONAL_CODEPOINTS["end"]
        )
        return ParsedKittySequence(
            codepoint=codepoint,
            modifier=int(home_end_match.group(1)) - 1,
            eventType=parse_event_type(home_end_match.group(2)),
        )

    return None


def matches_kitty_sequence(data: str, expected_codepoint: int, expected_modifier: int) -> bool:
    parsed = parse_kitty_sequence(data)
    if parsed is None:
        return False
    actual_mod = parsed.modifier & ~LOCK_MASK
    expected_mod = expected_modifier & ~LOCK_MASK
    if actual_mod != expected_mod:
        return False

    normalized_codepoint = normalize_shifted_letter_identity_codepoint(
        normalize_kitty_functional_codepoint(parsed.codepoint),
        parsed.modifier,
    )
    normalized_expected = normalize_shifted_letter_identity_codepoint(
        normalize_kitty_functional_codepoint(expected_codepoint),
        expected_modifier,
    )
    if normalized_codepoint == normalized_expected:
        return True

    if parsed.baseLayoutKey is not None and parsed.baseLayoutKey == expected_codepoint:
        cp = normalized_codepoint
        is_latin_letter = 97 <= cp <= 122
        is_known_symbol = chr(cp) in SYMBOL_KEYS
        if not is_latin_letter and not is_known_symbol:
            return True

    return False


def parse_modify_other_keys_sequence(data: str) -> ParsedModifyOtherKeysSequence | None:
    match = re.match(r"^\x1b\[27;(\d+);(\d+)~$", data)
    if match is None:
        return None
    return ParsedModifyOtherKeysSequence(
        codepoint=int(match.group(2)),
        modifier=int(match.group(1)) - 1,
    )


def matches_modify_other_keys(data: str, expected_keycode: int, expected_modifier: int) -> bool:
    parsed = parse_modify_other_keys_sequence(data)
    return bool(
        parsed is not None and parsed.codepoint == expected_keycode and parsed.modifier == expected_modifier
    )


def is_windows_terminal_session() -> bool:
    return bool(
        os.environ.get("WT_SESSION")
        and not os.environ.get("SSH_CONNECTION")
        and not os.environ.get("SSH_CLIENT")
        and not os.environ.get("SSH_TTY")
    )


def matches_raw_backspace(data: str, expected_modifier: int) -> bool:
    if data == "\x7f":
        return expected_modifier == 0
    if data != "\x08":
        return False
    return expected_modifier == MODIFIERS["ctrl"] if is_windows_terminal_session() else expected_modifier == 0


def raw_ctrl_char(key: str) -> str | None:
    char = key.lower()
    code = ord(char)
    if 97 <= code <= 122 or char in {"[", "\\", "]", "_"}:
        return chr(code & 0x1F)
    if char == "-":
        return chr(31)
    return None


def is_digit_key(key: str) -> bool:
    return "0" <= key <= "9"


def matches_printable_modify_other_keys(data: str, expected_keycode: int, expected_modifier: int) -> bool:
    if expected_modifier == 0:
        return False
    parsed = parse_modify_other_keys_sequence(data)
    if parsed is None or parsed.modifier != expected_modifier:
        return False
    return normalize_shifted_letter_identity_codepoint(parsed.codepoint, parsed.modifier) == (
        normalize_shifted_letter_identity_codepoint(expected_keycode, expected_modifier)
    )


def format_key_name_with_modifiers(key_name: str, modifier: int) -> str | None:
    mods: list[str] = []
    effective_mod = modifier & ~LOCK_MASK
    supported_modifier_mask = MODIFIERS["shift"] | MODIFIERS["ctrl"] | MODIFIERS["alt"] | MODIFIERS["super"]
    if (effective_mod & ~supported_modifier_mask) != 0:
        return None
    if effective_mod & MODIFIERS["shift"]:
        mods.append("shift")
    if effective_mod & MODIFIERS["ctrl"]:
        mods.append("ctrl")
    if effective_mod & MODIFIERS["alt"]:
        mods.append("alt")
    if effective_mod & MODIFIERS["super"]:
        mods.append("super")
    return f"{'+'.join(mods)}+{key_name}" if mods else key_name


def parse_key_id(key_id: str) -> dict[str, bool | str] | None:
    parts = key_id.lower().split("+")
    key = parts[-1]
    if not key:
        return None
    return {
        "key": key,
        "ctrl": "ctrl" in parts,
        "shift": "shift" in parts,
        "alt": "alt" in parts,
        "super": "super" in parts,
    }


def matchesKey(data: str, key_id: KeyId) -> bool:
    parsed = parse_key_id(key_id)
    if parsed is None:
        return False

    key = str(parsed["key"])
    ctrl = bool(parsed["ctrl"])
    shift = bool(parsed["shift"])
    alt = bool(parsed["alt"])
    super_modifier = bool(parsed["super"])

    modifier = 0
    if shift:
        modifier |= MODIFIERS["shift"]
    if alt:
        modifier |= MODIFIERS["alt"]
    if ctrl:
        modifier |= MODIFIERS["ctrl"]
    if super_modifier:
        modifier |= MODIFIERS["super"]

    if key in {"escape", "esc"}:
        if modifier != 0:
            return False
        return (
            data == "\x1b"
            or matches_kitty_sequence(data, CODEPOINTS["escape"], 0)
            or matches_modify_other_keys(data, CODEPOINTS["escape"], 0)
        )

    if key == "space":
        if not _kitty_protocol_active:
            if modifier == MODIFIERS["ctrl"] and data == "\x00":
                return True
            if modifier == MODIFIERS["alt"] and data == "\x1b ":
                return True
        if modifier == 0:
            return (
                data == " "
                or matches_kitty_sequence(data, CODEPOINTS["space"], 0)
                or matches_modify_other_keys(data, CODEPOINTS["space"], 0)
            )
        return matches_kitty_sequence(data, CODEPOINTS["space"], modifier) or matches_modify_other_keys(
            data, CODEPOINTS["space"], modifier
        )

    if key == "tab":
        if modifier == MODIFIERS["shift"]:
            return (
                data == "\x1b[Z"
                or matches_kitty_sequence(data, CODEPOINTS["tab"], MODIFIERS["shift"])
                or matches_modify_other_keys(data, CODEPOINTS["tab"], MODIFIERS["shift"])
            )
        if modifier == 0:
            return data == "\t" or matches_kitty_sequence(data, CODEPOINTS["tab"], 0)
        return matches_kitty_sequence(data, CODEPOINTS["tab"], modifier) or matches_modify_other_keys(
            data, CODEPOINTS["tab"], modifier
        )

    if key in {"enter", "return"}:
        if modifier == MODIFIERS["shift"]:
            if matches_kitty_sequence(data, CODEPOINTS["enter"], MODIFIERS["shift"]) or matches_kitty_sequence(
                data, CODEPOINTS["kpEnter"], MODIFIERS["shift"]
            ):
                return True
            if matches_modify_other_keys(data, CODEPOINTS["enter"], MODIFIERS["shift"]):
                return True
            return data in {"\x1b\r", "\n"} if _kitty_protocol_active else False
        if modifier == MODIFIERS["alt"]:
            if matches_kitty_sequence(data, CODEPOINTS["enter"], MODIFIERS["alt"]) or matches_kitty_sequence(
                data, CODEPOINTS["kpEnter"], MODIFIERS["alt"]
            ):
                return True
            if matches_modify_other_keys(data, CODEPOINTS["enter"], MODIFIERS["alt"]):
                return True
            return data == "\x1b\r" if not _kitty_protocol_active else False
        if modifier == 0:
            return (
                data == "\r"
                or (not _kitty_protocol_active and data == "\n")
                or data == "\x1bOM"
                or matches_kitty_sequence(data, CODEPOINTS["enter"], 0)
                or matches_kitty_sequence(data, CODEPOINTS["kpEnter"], 0)
            )
        return matches_kitty_sequence(data, CODEPOINTS["enter"], modifier) or matches_kitty_sequence(
            data, CODEPOINTS["kpEnter"], modifier
        ) or matches_modify_other_keys(data, CODEPOINTS["enter"], modifier)

    if key == "backspace":
        if modifier == MODIFIERS["alt"]:
            if data in {"\x1b\x7f", "\x1b\b"}:
                return True
            return matches_kitty_sequence(data, CODEPOINTS["backspace"], MODIFIERS["alt"]) or (
                matches_modify_other_keys(data, CODEPOINTS["backspace"], MODIFIERS["alt"])
            )
        if modifier == MODIFIERS["ctrl"]:
            if matches_raw_backspace(data, MODIFIERS["ctrl"]):
                return True
            return matches_kitty_sequence(data, CODEPOINTS["backspace"], MODIFIERS["ctrl"]) or (
                matches_modify_other_keys(data, CODEPOINTS["backspace"], MODIFIERS["ctrl"])
            )
        if modifier == 0:
            return (
                matches_raw_backspace(data, 0)
                or matches_kitty_sequence(data, CODEPOINTS["backspace"], 0)
                or matches_modify_other_keys(data, CODEPOINTS["backspace"], 0)
            )
        return matches_kitty_sequence(data, CODEPOINTS["backspace"], modifier) or matches_modify_other_keys(
            data, CODEPOINTS["backspace"], modifier
        )

    if key in {"insert", "delete", "clear", "home", "end"}:
        legacy_key = key
        if modifier == 0:
            if key == "clear":
                return matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES["clear"])
            return matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES[legacy_key]) or matches_kitty_sequence(
                data,
                FUNCTIONAL_CODEPOINTS[key] if key != "insert" else FUNCTIONAL_CODEPOINTS["insert"],
                0,
            )
        if matches_legacy_modifier_sequence(data, legacy_key, modifier):
            return True
        if key == "clear":
            return False
        return matches_kitty_sequence(data, FUNCTIONAL_CODEPOINTS[key], modifier)

    if key in {"pageup", "pagedown"}:
        logical_key = "pageUp" if key == "pageup" else "pageDown"
        if modifier == 0:
            return matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES[logical_key]) or matches_kitty_sequence(
                data, FUNCTIONAL_CODEPOINTS[logical_key], 0
            )
        if matches_legacy_modifier_sequence(data, logical_key, modifier):
            return True
        return matches_kitty_sequence(data, FUNCTIONAL_CODEPOINTS[logical_key], modifier)

    if key in {"up", "down"}:
        if modifier == MODIFIERS["alt"]:
            return data == ("\x1bp" if key == "up" else "\x1bn") or matches_kitty_sequence(
                data, ARROW_CODEPOINTS[key], MODIFIERS["alt"]
            )
        if modifier == 0:
            return matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES[key]) or matches_kitty_sequence(
                data, ARROW_CODEPOINTS[key], 0
            )
        if matches_legacy_modifier_sequence(data, key, modifier):
            return True
        return matches_kitty_sequence(data, ARROW_CODEPOINTS[key], modifier)

    if key == "left":
        if modifier == MODIFIERS["alt"]:
            return (
                data == "\x1b[1;3D"
                or (not _kitty_protocol_active and data == "\x1bB")
                or data == "\x1bb"
                or matches_kitty_sequence(data, ARROW_CODEPOINTS["left"], MODIFIERS["alt"])
            )
        if modifier == MODIFIERS["ctrl"]:
            return (
                data == "\x1b[1;5D"
                or matches_legacy_modifier_sequence(data, "left", MODIFIERS["ctrl"])
                or matches_kitty_sequence(data, ARROW_CODEPOINTS["left"], MODIFIERS["ctrl"])
            )
        if modifier == 0:
            return matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES["left"]) or matches_kitty_sequence(
                data, ARROW_CODEPOINTS["left"], 0
            )
        if matches_legacy_modifier_sequence(data, "left", modifier):
            return True
        return matches_kitty_sequence(data, ARROW_CODEPOINTS["left"], modifier)

    if key == "right":
        if modifier == MODIFIERS["alt"]:
            return (
                data == "\x1b[1;3C"
                or (not _kitty_protocol_active and data == "\x1bF")
                or data == "\x1bf"
                or matches_kitty_sequence(data, ARROW_CODEPOINTS["right"], MODIFIERS["alt"])
            )
        if modifier == MODIFIERS["ctrl"]:
            return (
                data == "\x1b[1;5C"
                or matches_legacy_modifier_sequence(data, "right", MODIFIERS["ctrl"])
                or matches_kitty_sequence(data, ARROW_CODEPOINTS["right"], MODIFIERS["ctrl"])
            )
        if modifier == 0:
            return matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES["right"]) or matches_kitty_sequence(
                data, ARROW_CODEPOINTS["right"], 0
            )
        if matches_legacy_modifier_sequence(data, "right", modifier):
            return True
        return matches_kitty_sequence(data, ARROW_CODEPOINTS["right"], modifier)

    if key.startswith("f") and key[1:].isdigit():
        if modifier != 0 or key not in LEGACY_KEY_SEQUENCES:
            return False
        return matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES[key])

    if len(key) == 1 and (("a" <= key <= "z") or is_digit_key(key) or key in SYMBOL_KEYS):
        codepoint = ord(key)
        raw_ctrl = raw_ctrl_char(key)
        is_letter = "a" <= key <= "z"
        is_digit = is_digit_key(key)

        if (modifier == MODIFIERS["ctrl"] + MODIFIERS["alt"] and not _kitty_protocol_active and raw_ctrl
                and data == f"\x1b{raw_ctrl}"):
            return True
        if (modifier == MODIFIERS["alt"] and not _kitty_protocol_active and (is_letter or is_digit or key in SYMBOL_KEYS)
                and data == f"\x1b{key}"):
            return True
        if modifier == MODIFIERS["ctrl"]:
            if raw_ctrl and data == raw_ctrl:
                return True
            return matches_kitty_sequence(data, codepoint, MODIFIERS["ctrl"]) or matches_printable_modify_other_keys(
                data, codepoint, MODIFIERS["ctrl"]
            )
        if modifier == MODIFIERS["shift"] + MODIFIERS["ctrl"]:
            return matches_kitty_sequence(data, codepoint, modifier) or matches_printable_modify_other_keys(
                data, codepoint, modifier
            )
        if modifier == MODIFIERS["shift"]:
            if is_letter and data == key.upper():
                return True
            return matches_kitty_sequence(data, codepoint, MODIFIERS["shift"]) or matches_printable_modify_other_keys(
                data, codepoint, MODIFIERS["shift"]
            )
        if modifier != 0:
            return matches_kitty_sequence(data, codepoint, modifier) or matches_printable_modify_other_keys(
                data, codepoint, modifier
            )
        return data == key or matches_kitty_sequence(data, codepoint, 0)

    return False


def format_parsed_key(codepoint: int, modifier: int, base_layout_key: int | None = None) -> str | None:
    normalized_codepoint = normalize_kitty_functional_codepoint(codepoint)
    identity_codepoint = normalize_shifted_letter_identity_codepoint(normalized_codepoint, modifier)
    is_latin_letter = 97 <= identity_codepoint <= 122
    is_digit = 48 <= identity_codepoint <= 57
    is_known_symbol = chr(identity_codepoint) in SYMBOL_KEYS if identity_codepoint >= 0 else False
    effective_codepoint = (
        identity_codepoint
        if (is_latin_letter or is_digit or is_known_symbol)
        else (base_layout_key if base_layout_key is not None else identity_codepoint)
    )

    key_name: str | None = None
    if effective_codepoint == CODEPOINTS["escape"]:
        key_name = "escape"
    elif effective_codepoint == CODEPOINTS["tab"]:
        key_name = "tab"
    elif effective_codepoint in {CODEPOINTS["enter"], CODEPOINTS["kpEnter"]}:
        key_name = "enter"
    elif effective_codepoint == CODEPOINTS["space"]:
        key_name = "space"
    elif effective_codepoint == CODEPOINTS["backspace"]:
        key_name = "backspace"
    elif effective_codepoint == FUNCTIONAL_CODEPOINTS["delete"]:
        key_name = "delete"
    elif effective_codepoint == FUNCTIONAL_CODEPOINTS["insert"]:
        key_name = "insert"
    elif effective_codepoint == FUNCTIONAL_CODEPOINTS["home"]:
        key_name = "home"
    elif effective_codepoint == FUNCTIONAL_CODEPOINTS["end"]:
        key_name = "end"
    elif effective_codepoint == FUNCTIONAL_CODEPOINTS["pageUp"]:
        key_name = "pageUp"
    elif effective_codepoint == FUNCTIONAL_CODEPOINTS["pageDown"]:
        key_name = "pageDown"
    elif effective_codepoint == ARROW_CODEPOINTS["up"]:
        key_name = "up"
    elif effective_codepoint == ARROW_CODEPOINTS["down"]:
        key_name = "down"
    elif effective_codepoint == ARROW_CODEPOINTS["left"]:
        key_name = "left"
    elif effective_codepoint == ARROW_CODEPOINTS["right"]:
        key_name = "right"
    elif 48 <= effective_codepoint <= 57 or 97 <= effective_codepoint <= 122 or effective_codepoint >= 0 and chr(effective_codepoint) in SYMBOL_KEYS:
        key_name = chr(effective_codepoint)

    if key_name is None:
        return None
    return format_key_name_with_modifiers(key_name, modifier)


def parseKey(data: str) -> str | None:
    kitty = parse_kitty_sequence(data)
    if kitty is not None:
        return format_parsed_key(kitty.codepoint, kitty.modifier, kitty.baseLayoutKey)

    modify_other_keys = parse_modify_other_keys_sequence(data)
    if modify_other_keys is not None:
        return format_parsed_key(modify_other_keys.codepoint, modify_other_keys.modifier)

    if _kitty_protocol_active and data in {"\x1b\r", "\n"}:
        return "shift+enter"

    legacy_key_id = LEGACY_SEQUENCE_KEY_IDS.get(data)
    if legacy_key_id is not None:
        return legacy_key_id

    if data == "\x1b":
        return "escape"
    if data == "\x1c":
        return "ctrl+\\"
    if data == "\x1d":
        return "ctrl+]"
    if data == "\x1f":
        return "ctrl+-"
    if data == "\x1b\x1b":
        return "ctrl+alt+["
    if data == "\x1b\x1c":
        return "ctrl+alt+\\"
    if data == "\x1b\x1d":
        return "ctrl+alt+]"
    if data == "\x1b\x1f":
        return "ctrl+alt+-"
    if data == "\t":
        return "tab"
    if data == "\r" or (not _kitty_protocol_active and data == "\n") or data == "\x1bOM":
        return "enter"
    if data == "\x00":
        return "ctrl+space"
    if data == " ":
        return "space"
    if data == "\x7f":
        return "backspace"
    if data == "\x08":
        return "ctrl+backspace" if is_windows_terminal_session() else "backspace"
    if data == "\x1b[Z":
        return "shift+tab"
    if not _kitty_protocol_active and data == "\x1b\r":
        return "alt+enter"
    if not _kitty_protocol_active and data == "\x1b ":
        return "alt+space"
    if data in {"\x1b\x7f", "\x1b\b"}:
        return "alt+backspace"
    if not _kitty_protocol_active and data == "\x1bB":
        return "alt+left"
    if not _kitty_protocol_active and data == "\x1bF":
        return "alt+right"
    if not _kitty_protocol_active and len(data) == 2 and data[0] == "\x1b":
        code = ord(data[1])
        if 1 <= code <= 26:
            return f"ctrl+alt+{chr(code + 96)}"
        if 97 <= code <= 122 or 48 <= code <= 57 or chr(code) in SYMBOL_KEYS:
            return f"alt+{chr(code)}"
    if data == "\x1b[A":
        return "up"
    if data == "\x1b[B":
        return "down"
    if data == "\x1b[C":
        return "right"
    if data == "\x1b[D":
        return "left"
    if data in {"\x1b[H", "\x1bOH"}:
        return "home"
    if data in {"\x1b[F", "\x1bOF"}:
        return "end"
    if data == "\x1b[3~":
        return "delete"
    if data == "\x1b[5~":
        return "pageUp"
    if data == "\x1b[6~":
        return "pageDown"

    if len(data) == 1:
        code = ord(data)
        if 1 <= code <= 26:
            return f"ctrl+{chr(code + 96)}"
        if 32 <= code <= 126:
            return data

    return None


def decodeKittyPrintable(data: str) -> str | None:
    match = KITTY_CSI_U_REGEX.match(data)
    if match is None:
        return None

    codepoint = int(match.group(1))
    shifted_key = int(match.group(2)) if match.group(2) else None
    mod_value = int(match.group(4)) if match.group(4) else 1
    modifier = mod_value - 1

    if (modifier & ~KITTY_PRINTABLE_ALLOWED_MODIFIERS) != 0:
        return None
    if modifier & (MODIFIERS["alt"] | MODIFIERS["ctrl"]):
        return None

    effective_codepoint = codepoint
    if modifier & MODIFIERS["shift"] and shifted_key is not None:
        effective_codepoint = shifted_key
    effective_codepoint = normalize_kitty_functional_codepoint(effective_codepoint)
    if effective_codepoint < 32:
        return None
    try:
        return chr(effective_codepoint)
    except ValueError:
        return None


def decode_modify_other_keys_printable(data: str) -> str | None:
    parsed = parse_modify_other_keys_sequence(data)
    if parsed is None:
        return None
    modifier = parsed.modifier & ~LOCK_MASK
    if (modifier & ~MODIFIERS["shift"]) != 0 or parsed.codepoint < 32:
        return None
    try:
        return chr(parsed.codepoint)
    except ValueError:
        return None


def decodePrintableKey(data: str) -> str | None:
    return decodeKittyPrintable(data) or decode_modify_other_keys_printable(data)

__all__ = [
    "Key",
    "KeyEventType",
    "KeyId",
    "decodeKittyPrintable",
    "decodePrintableKey",
    "isKeyRelease",
    "isKeyRepeat",
    "isKittyProtocolActive",
    "matchesKey",
    "parseKey",
    "setKittyProtocolActive",
]

"""Translated from pi-tui's ``packages/tui/src/keys.ts`` (v1.3.0).

Keyboard input handling for terminal applications.

Supports both legacy terminal sequences and Kitty keyboard protocol.
See: https://sw.kovidgoyal.net/kitty/keyboard-protocol/
Reference: https://github.com/sst/opentui/blob/7da92b4088aebfe27b9f691c04163a48821e49fd/packages/core/src/lib/parse.keypress.ts

Symbol keys are also supported, however some ctrl+symbol combos
overlap with ASCII codes, e.g. ctrl+[ = ESC.
See: https://sw.kovidgoyal.net/kitty/keyboard-protocol/#legacy-ctrl-mapping-of-ascii-keys
Those can still be * used for ctrl+shift combos

API:
- matchesKey(data, keyId) - Check if input matches a key identifier
- parseKey(data) - Parse input and return the key identifier
- Key - Helper object for creating typed key identifiers
- setKittyProtocolActive(active) - Set global Kitty protocol state
- isKittyProtocolActive() - Query global Kitty protocol state

The regular expressions are JavaScript sources run through ``misaka.utils.js_regexp`` (``$`` is the end of the
input, ``\\d`` is ASCII), as upstream's literals behave.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

from misaka.utils import js_regexp
from misaka.utils.js_regexp import RegExp

# =============================================================================
# Global Kitty Protocol State
# =============================================================================

_kittyProtocolActive = False


def setKittyProtocolActive(active: bool) -> None:
    """Set the global Kitty keyboard protocol state.
    Called by ProcessTerminal after detecting protocol support.
    """
    global _kittyProtocolActive
    _kittyProtocolActive = active


def isKittyProtocolActive() -> bool:
    """Query whether Kitty keyboard protocol is currently active."""
    return _kittyProtocolActive


# =============================================================================
# Type-Safe Key Identifiers
# =============================================================================

type Letter = Literal[
    "a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m",
    "n", "o", "p", "q", "r", "s", "t", "u", "v", "w", "x", "y", "z",
]  # fmt: skip

type Digit = Literal["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"]

type SymbolKey = Literal[
    "`", "-", "=", "[", "]", "\\", ";", "'", ",", ".", "/", "!", "@", "#", "$", "%",
    "^", "&", "*", "(", ")", "_", "+", "|", "~", "{", "}", ":", "<", ">", "?",
]  # fmt: skip

type SpecialKey = Literal[
    "escape", "esc", "enter", "return", "tab", "space", "backspace", "delete", "insert", "clear", "home", "end",
    "pageUp", "pageDown", "up", "down", "left", "right",
    "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
]  # fmt: skip

type BaseKey = Letter | Digit | SymbolKey | SpecialKey
type ModifierName = Literal["ctrl", "shift", "alt", "super"]

# Union type of all valid key identifiers.
# Provides autocomplete and catches typos at compile time.
# MISAKA fork: upstream's `ModifiedKeyId<BaseKey>` builds every modifier permutation as a template literal type,
# which Python typing cannot express; a key identifier is any string here.
type KeyId = str


class Key:
    """Helper object for creating typed key identifiers with autocomplete.

    Usage:
    - Key.escape, Key.enter, Key.tab, etc. for special keys
    - Key.backtick, Key.comma, Key.period, etc. for symbol keys
    - Key.ctrl("c"), Key.alt("x"), Key.super("k") for single modifiers
    - Key.ctrlShift("p"), Key.ctrlAlt("x"), Key.ctrlSuper("k") for combined modifiers
    """

    # Special keys
    escape = "escape"
    esc = "esc"
    enter = "enter"
    # `return` is set after the class body (a Python keyword cannot be an attribute name in source).
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

    # Symbol keys
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

    # Single modifiers
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

    # Combined modifiers
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

    # Triple modifiers
    @staticmethod
    def ctrlShiftAlt(key: str) -> str:
        return f"ctrl+shift+alt+{key}"

    @staticmethod
    def ctrlShiftSuper(key: str) -> str:
        return f"ctrl+shift+super+{key}"


# MISAKA fork: `Key.return` as an attribute set from outside the class body; read it with `getattr(Key, "return")`.
setattr(Key, "return", "return")

# =============================================================================
# Constants
# =============================================================================

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

SHIFTED_SYMBOL_KEYS = frozenset("~!@#$%^&*()_+|{}:<>?")

MODIFIER_NAMES = ("shift", "alt", "ctrl", "super")


class MODIFIERS:
    shift = 1
    alt = 2
    ctrl = 4
    super = 8


LOCK_MASK = 64 + 128  # Caps Lock + Num Lock


class CODEPOINTS:
    escape = 27
    tab = 9
    enter = 13
    space = 32
    backspace = 127
    kpEnter = 57414  # Numpad Enter (Kitty protocol)


class ARROW_CODEPOINTS:
    up = -1
    down = -2
    right = -3
    left = -4


class FUNCTIONAL_CODEPOINTS:
    delete = -10
    insert = -11
    pageUp = -12
    pageDown = -13
    home = -14
    end = -15


KITTY_FUNCTIONAL_KEY_EQUIVALENTS: dict[int, int] = {
    57399: 48,  # KP_0 -> 0
    57400: 49,  # KP_1 -> 1
    57401: 50,  # KP_2 -> 2
    57402: 51,  # KP_3 -> 3
    57403: 52,  # KP_4 -> 4
    57404: 53,  # KP_5 -> 5
    57405: 54,  # KP_6 -> 6
    57406: 55,  # KP_7 -> 7
    57407: 56,  # KP_8 -> 8
    57408: 57,  # KP_9 -> 9
    57409: 46,  # KP_DECIMAL -> .
    57410: 47,  # KP_DIVIDE -> /
    57411: 42,  # KP_MULTIPLY -> *
    57412: 45,  # KP_SUBTRACT -> -
    57413: 43,  # KP_ADD -> +
    57415: 61,  # KP_EQUAL -> =
    57416: 44,  # KP_SEPARATOR -> ,
    57417: ARROW_CODEPOINTS.left,
    57418: ARROW_CODEPOINTS.right,
    57419: ARROW_CODEPOINTS.up,
    57420: ARROW_CODEPOINTS.down,
    57421: FUNCTIONAL_CODEPOINTS.pageUp,
    57422: FUNCTIONAL_CODEPOINTS.pageDown,
    57423: FUNCTIONAL_CODEPOINTS.home,
    57424: FUNCTIONAL_CODEPOINTS.end,
    57425: FUNCTIONAL_CODEPOINTS.insert,
    57426: FUNCTIONAL_CODEPOINTS.delete,
}


def normalizeKittyFunctionalCodepoint(codepoint: int) -> int:
    return KITTY_FUNCTIONAL_KEY_EQUIVALENTS.get(codepoint, codepoint)


def normalizeShiftedLetterIdentityCodepoint(codepoint: int, modifier: int) -> int:
    effectiveModifier = modifier & ~LOCK_MASK
    if (effectiveModifier & MODIFIERS.shift) != 0 and codepoint >= 65 and codepoint <= 90:
        return codepoint + 32
    return codepoint


def fromCharCode(code: int) -> str:
    """MISAKA fork: ``String.fromCharCode``: the UTF-16 code unit ``code`` names (ToUint16), so it never raises."""
    return chr(code & 0xFFFF)


@dataclass(slots=True, kw_only=True)
class LogicalShiftedSymbolCandidate:
    """MISAKA fork: upstream's ``{ codepoint, modifier }`` object."""

    codepoint: int
    modifier: int


def getLogicalShiftedSymbolCandidate(
    codepoint: int | None,
    modifier: int,
    allowedSymbols: set[str] | frozenset[str],
) -> LogicalShiftedSymbolCandidate | None:
    if codepoint is None or (modifier & MODIFIERS.shift) == 0:
        return None
    normalizedCodepoint = normalizeKittyFunctionalCodepoint(codepoint)
    if fromCharCode(normalizedCodepoint) not in allowedSymbols:
        return None
    return LogicalShiftedSymbolCandidate(codepoint=normalizedCodepoint, modifier=modifier & ~MODIFIERS.shift)


LEGACY_KEY_SEQUENCES: dict[str, tuple[str, ...]] = {
    "up": ("\x1b[A", "\x1bOA"),
    "down": ("\x1b[B", "\x1bOB"),
    "right": ("\x1b[C", "\x1bOC"),
    "left": ("\x1b[D", "\x1bOD"),
    "home": ("\x1b[H", "\x1bOH", "\x1b[1~", "\x1b[7~"),
    "end": ("\x1b[F", "\x1bOF", "\x1b[4~", "\x1b[8~"),
    "insert": ("\x1b[2~",),
    "delete": ("\x1b[3~",),
    "pageUp": ("\x1b[5~", "\x1b[[5~"),
    "pageDown": ("\x1b[6~", "\x1b[[6~"),
    "clear": ("\x1b[E", "\x1bOE"),
    "f1": ("\x1bOP", "\x1b[11~", "\x1b[[A"),
    "f2": ("\x1bOQ", "\x1b[12~", "\x1b[[B"),
    "f3": ("\x1bOR", "\x1b[13~", "\x1b[[C"),
    "f4": ("\x1bOS", "\x1b[14~", "\x1b[[D"),
    "f5": ("\x1b[15~", "\x1b[[E"),
    "f6": ("\x1b[17~",),
    "f7": ("\x1b[18~",),
    "f8": ("\x1b[19~",),
    "f9": ("\x1b[20~",),
    "f10": ("\x1b[21~",),
    "f11": ("\x1b[23~",),
    "f12": ("\x1b[24~",),
}

LEGACY_SHIFT_SEQUENCES: dict[str, tuple[str, ...]] = {
    "up": ("\x1b[a",),
    "down": ("\x1b[b",),
    "right": ("\x1b[c",),
    "left": ("\x1b[d",),
    "clear": ("\x1b[e",),
    "insert": ("\x1b[2$",),
    "delete": ("\x1b[3$",),
    "pageUp": ("\x1b[5$",),
    "pageDown": ("\x1b[6$",),
    "home": ("\x1b[7$",),
    "end": ("\x1b[8$",),
}

LEGACY_CTRL_SEQUENCES: dict[str, tuple[str, ...]] = {
    "up": ("\x1bOa",),
    "down": ("\x1bOb",),
    "right": ("\x1bOc",),
    "left": ("\x1bOd",),
    "clear": ("\x1bOe",),
    "insert": ("\x1b[2^",),
    "delete": ("\x1b[3^",),
    "pageUp": ("\x1b[5^",),
    "pageDown": ("\x1b[6^",),
    "home": ("\x1b[7^",),
    "end": ("\x1b[8^",),
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

type LegacyModifierKey = Literal[
    "up", "down", "right", "left", "clear", "insert", "delete", "pageUp", "pageDown", "home", "end"
]  # fmt: skip


def matchesLegacySequence(data: str, sequences: tuple[str, ...]) -> bool:
    return data in sequences


def matchesLegacyModifierSequence(data: str, key: LegacyModifierKey, modifier: int) -> bool:
    if modifier == MODIFIERS.shift:
        return matchesLegacySequence(data, LEGACY_SHIFT_SEQUENCES[key])
    if modifier == MODIFIERS.ctrl:
        return matchesLegacySequence(data, LEGACY_CTRL_SEQUENCES[key])
    return False


# =============================================================================
# Kitty Protocol Parsing
# =============================================================================

# Event types from Kitty keyboard protocol (flag 2)
# 1 = key press, 2 = key repeat, 3 = key release
type KeyEventType = Literal["press", "repeat", "release"]


@dataclass(slots=True, kw_only=True)
class ParsedKittySequence:
    codepoint: int
    shiftedKey: int | None = None  # Shifted version of the key (when shift is pressed)
    baseLayoutKey: int | None = None  # Key in standard PC-101 layout (for non-Latin layouts)
    modifier: int
    eventType: KeyEventType


@dataclass(slots=True, kw_only=True)
class ParsedModifyOtherKeysSequence:
    codepoint: int
    modifier: int


# Store the last parsed event type for isKeyRelease() to query
_lastEventType: KeyEventType = "press"


def isKeyRelease(data: str) -> bool:
    """Check if the last parsed key event was a key release.
    Only meaningful when Kitty keyboard protocol with flag 2 is active.
    """
    # Don't treat bracketed paste content as key release, even if it contains
    # patterns like ":3F" (e.g., bluetooth MAC addresses like "90:62:3F:A5").
    # Terminal.ts re-wraps paste content with bracketed paste markers before
    # passing to TUI, so pasted data will always contain \x1b[200~.
    if "\x1b[200~" in data:
        return False

    # Quick check: release events with flag 2 contain ":3"
    # Format: \x1b[<codepoint>;<modifier>:3u
    if (  # noqa: SIM103 - upstream returns true/false after the check
        ":3u" in data
        or ":3~" in data
        or ":3A" in data
        or ":3B" in data
        or ":3C" in data
        or ":3D" in data
        or ":3H" in data
        or ":3F" in data
    ):
        return True
    return False


def isKeyRepeat(data: str) -> bool:
    """Check if the last parsed key event was a key repeat.
    Only meaningful when Kitty keyboard protocol with flag 2 is active.
    """
    # Don't treat bracketed paste content as key repeat, even if it contains
    # patterns like ":2F". See isKeyRelease() for details.
    if "\x1b[200~" in data:
        return False

    if (  # noqa: SIM103 - upstream returns true/false after the check
        ":2u" in data
        or ":2~" in data
        or ":2A" in data
        or ":2B" in data
        or ":2C" in data
        or ":2D" in data
        or ":2H" in data
        or ":2F" in data
    ):
        return True
    return False


def parseEventType(eventTypeStr: str | None) -> KeyEventType:
    if not eventTypeStr:
        return "press"
    eventType = int(eventTypeStr, 10)
    if eventType == 2:
        return "repeat"
    if eventType == 3:
        return "release"
    return "press"


def parseKittySequence(data: str) -> ParsedKittySequence | None:
    global _lastEventType
    codepoint: int | None
    # CSI u format with alternate keys (flag 4):
    # \x1b[<codepoint>u
    # \x1b[<codepoint>;<mod>u
    # \x1b[<codepoint>;<mod>:<event>u
    # \x1b[<codepoint>:<shifted>;<mod>u
    # \x1b[<codepoint>:<shifted>:<base>;<mod>u
    # \x1b[<codepoint>::<base>;<mod>u (no shifted key, only base)
    #
    # With flag 2, event type is appended after modifier colon: 1=press, 2=repeat, 3=release
    # With flag 4, alternate keys are appended after codepoint with colons
    csiUMatch = js_regexp.match(data, RegExp(r"^\x1b\[(\d+)(?::(\d*))?(?::(\d+))?(?:;(\d+))?(?::(\d+))?u$"))
    if csiUMatch:
        codepoint = int(csiUMatch[1], 10)
        shiftedKey = int(csiUMatch[2], 10) if csiUMatch[2] and len(csiUMatch[2]) > 0 else None
        baseLayoutKey = int(csiUMatch[3], 10) if csiUMatch[3] else None
        modValue = int(csiUMatch[4], 10) if csiUMatch[4] else 1
        eventType = parseEventType(csiUMatch[5])
        _lastEventType = eventType
        return ParsedKittySequence(
            codepoint=codepoint,
            shiftedKey=shiftedKey,
            baseLayoutKey=baseLayoutKey,
            modifier=modValue - 1,
            eventType=eventType,
        )

    # Arrow keys with modifier: \x1b[1;<mod>A/B/C/D or \x1b[1;<mod>:<event>A/B/C/D
    arrowMatch = js_regexp.match(data, RegExp(r"^\x1b\[1;(\d+)(?::(\d+))?([ABCD])$"))
    if arrowMatch:
        modValue = int(arrowMatch[1], 10)
        eventType = parseEventType(arrowMatch[2])
        arrowCodes: dict[str, int] = {"A": -1, "B": -2, "C": -3, "D": -4}
        _lastEventType = eventType
        return ParsedKittySequence(codepoint=arrowCodes[arrowMatch[3]], modifier=modValue - 1, eventType=eventType)

    # Functional keys: \x1b[<num>~ or \x1b[<num>;<mod>~ or \x1b[<num>;<mod>:<event>~
    funcMatch = js_regexp.match(data, RegExp(r"^\x1b\[(\d+)(?:;(\d+))?(?::(\d+))?~$"))
    if funcMatch:
        keyNum = int(funcMatch[1], 10)
        modValue = int(funcMatch[2], 10) if funcMatch[2] else 1
        eventType = parseEventType(funcMatch[3])
        funcCodes: dict[int, int] = {
            2: FUNCTIONAL_CODEPOINTS.insert,
            3: FUNCTIONAL_CODEPOINTS.delete,
            5: FUNCTIONAL_CODEPOINTS.pageUp,
            6: FUNCTIONAL_CODEPOINTS.pageDown,
            7: FUNCTIONAL_CODEPOINTS.home,
            8: FUNCTIONAL_CODEPOINTS.end,
        }
        codepoint = funcCodes.get(keyNum)
        if codepoint is not None:
            _lastEventType = eventType
            return ParsedKittySequence(codepoint=codepoint, modifier=modValue - 1, eventType=eventType)

    # Home/End with modifier: \x1b[1;<mod>H/F or \x1b[1;<mod>:<event>H/F
    homeEndMatch = js_regexp.match(data, RegExp(r"^\x1b\[1;(\d+)(?::(\d+))?([HF])$"))
    if homeEndMatch:
        modValue = int(homeEndMatch[1], 10)
        eventType = parseEventType(homeEndMatch[2])
        codepoint = FUNCTIONAL_CODEPOINTS.home if homeEndMatch[3] == "H" else FUNCTIONAL_CODEPOINTS.end
        _lastEventType = eventType
        return ParsedKittySequence(codepoint=codepoint, modifier=modValue - 1, eventType=eventType)

    return None


def matchesKittySequence(data: str, expectedCodepoint: int, expectedModifier: int) -> bool:
    parsed = parseKittySequence(data)
    if not parsed:
        return False
    actualMod = parsed.modifier & ~LOCK_MASK
    expectedMod = expectedModifier & ~LOCK_MASK

    normalizedCodepoint = normalizeShiftedLetterIdentityCodepoint(
        normalizeKittyFunctionalCodepoint(parsed.codepoint),
        parsed.modifier,
    )
    normalizedExpectedCodepoint = normalizeShiftedLetterIdentityCodepoint(
        normalizeKittyFunctionalCodepoint(expectedCodepoint),
        expectedModifier,
    )

    # Physical match: primary codepoint and every reported modifier match.
    if actualMod == expectedMod and normalizedCodepoint == normalizedExpectedCodepoint:
        return True

    # Logical match: Kitty reports symbols such as "+" as Shift plus the physical "=" key.
    # Bindings name the produced symbol, so consume that implicit Shift while preserving
    # Ctrl, Alt, and Super. The physical "shift+=" spelling remains valid through the match above.
    logicalShiftedSymbol = getLogicalShiftedSymbolCandidate(parsed.shiftedKey, actualMod, SYMBOL_KEYS)
    if (
        logicalShiftedSymbol
        and (logicalShiftedSymbol.modifier & ~LOCK_MASK) == expectedMod
        and logicalShiftedSymbol.codepoint == normalizeKittyFunctionalCodepoint(expectedCodepoint)
    ):
        return True

    if actualMod != expectedMod:
        return False

    # Alternate match: use base layout key for non-Latin keyboard layouts.
    # This allows Ctrl+С (Cyrillic) to match Ctrl+c (Latin) when terminal reports
    # the base layout key (the key in standard PC-101 layout).
    #
    # Only fall back to base layout key when the codepoint is NOT already a
    # recognized Latin letter (a-z) or symbol (e.g., /, -, [, ;, etc.).
    # When the codepoint is a recognized key, it is authoritative regardless
    # of physical key position. This prevents remapped layouts (Dvorak, Colemak,
    # xremap, etc.) from causing false matches: both letters and symbols move
    # to different physical positions, so Ctrl+K could falsely match Ctrl+V
    # (letter remapping) and Ctrl+/ could falsely match Ctrl+[ (symbol remapping)
    # if the base layout key were always considered.
    if parsed.baseLayoutKey is not None and parsed.baseLayoutKey == expectedCodepoint:
        cp = normalizedCodepoint
        isLatinLetter = cp >= 97 and cp <= 122  # a-z
        isKnownSymbol = fromCharCode(cp) in SYMBOL_KEYS
        if not isLatinLetter and not isKnownSymbol:
            return True

    return False


def parseModifyOtherKeysSequence(data: str) -> ParsedModifyOtherKeysSequence | None:
    match = js_regexp.match(data, RegExp(r"^\x1b\[27;(\d+);(\d+)~$"))
    if not match:
        return None
    modValue = int(match[1], 10)
    codepoint = int(match[2], 10)
    return ParsedModifyOtherKeysSequence(codepoint=codepoint, modifier=modValue - 1)


def matchesModifyOtherKeys(data: str, expectedKeycode: int, expectedModifier: int) -> bool:
    """Match xterm modifyOtherKeys format: CSI 27 ; modifiers ; keycode ~
    This is used by terminals when Kitty protocol is not enabled.
    Modifier values are 1-indexed: 2=shift, 3=alt, 5=ctrl, etc.
    """
    parsed = parseModifyOtherKeysSequence(data)
    if not parsed:
        return False
    return parsed.codepoint == expectedKeycode and parsed.modifier == expectedModifier


def isWindowsTerminalSession() -> bool:
    return (
        bool(os.environ.get("WT_SESSION"))
        and not os.environ.get("SSH_CONNECTION")
        and not os.environ.get("SSH_CLIENT")
        and not os.environ.get("SSH_TTY")
    )


def matchesRawBackspace(data: str, expectedModifier: int) -> bool:
    """Raw 0x08 (BS) is ambiguous in legacy terminals.

    - Windows Terminal uses it for Ctrl+Backspace.
    - Some legacy terminals and tmux setups send it for plain Backspace.

    Prefer explicit Kitty / CSI-u / modifyOtherKeys sequences whenever they are
    available. Fall back to a Windows Terminal heuristic only for raw BS bytes.
    """
    if data == "\x7f":
        return expectedModifier == 0
    if data != "\x08":
        return False
    return expectedModifier == MODIFIERS.ctrl if isWindowsTerminalSession() else expectedModifier == 0


# =============================================================================
# Generic Key Matching
# =============================================================================


def rawCtrlChar(key: str) -> str | None:
    """Get the control character for a key.
    Uses the universal formula: code & 0x1f (mask to lower 5 bits)

    Works for:
    - Letters a-z → 1-26
    - Symbols [\\]_ → 27, 28, 29, 31
    - Also maps - to same as _ (same physical key on US keyboards)
    """
    char = key.lower()
    code = ord(char[0])
    if (code >= 97 and code <= 122) or char == "[" or char == "\\" or char == "]" or char == "_":
        return chr(code & 0x1F)
    # Handle - as _ (same physical key on US keyboards)
    if char == "-":
        return chr(31)  # Same as Ctrl+_
    return None


def isDigitKey(key: str) -> bool:
    return key >= "0" and key <= "9"


def matchesPrintableModifyOtherKeys(data: str, expectedKeycode: int, expectedModifier: int) -> bool:
    parsed = parseModifyOtherKeysSequence(data)
    if not parsed:
        return False
    physicalMatch = parsed.modifier == expectedModifier and normalizeShiftedLetterIdentityCodepoint(
        parsed.codepoint, parsed.modifier
    ) == normalizeShiftedLetterIdentityCodepoint(expectedKeycode, expectedModifier)
    if physicalMatch:
        return True
    logicalShiftedSymbol = getLogicalShiftedSymbolCandidate(
        parsed.codepoint,
        parsed.modifier,
        SHIFTED_SYMBOL_KEYS,
    )
    return (
        logicalShiftedSymbol is not None
        and logicalShiftedSymbol.modifier == expectedModifier
        and logicalShiftedSymbol.codepoint == expectedKeycode
    )


def formatKeyNameWithModifiers(keyName: str, modifier: int) -> str | None:
    mods: list[str] = []
    effectiveMod = modifier & ~LOCK_MASK
    supportedModifierMask = MODIFIERS.shift | MODIFIERS.ctrl | MODIFIERS.alt | MODIFIERS.super
    if (effectiveMod & ~supportedModifierMask) != 0:
        return None
    if effectiveMod & MODIFIERS.shift:
        mods.append("shift")
    if effectiveMod & MODIFIERS.ctrl:
        mods.append("ctrl")
    if effectiveMod & MODIFIERS.alt:
        mods.append("alt")
    if effectiveMod & MODIFIERS.super:
        mods.append("super")
    return f"{'+'.join(mods)}+{keyName}" if len(mods) > 0 else keyName


def parseKeyId(keyId: str) -> dict[str, Any] | None:
    """The key and its modifier flags: ``{key, ctrl, shift, alt, super}``."""
    key = keyId.lower()
    modifiers: set[str] = set()
    while True:
        modifier = next((name for name in MODIFIER_NAMES if key.startswith(f"{name}+")), None)
        if not modifier:
            break
        if modifier in modifiers:
            return None
        modifiers.add(modifier)
        key = key[len(modifier) + 1 :]
    if not key or ("+" in key and key != "+"):
        return None
    return {
        "key": key,
        "ctrl": "ctrl" in modifiers,
        "shift": "shift" in modifiers,
        "alt": "alt" in modifiers,
        "super": "super" in modifiers,
    }


def matchesKey(data: str, keyId: KeyId) -> bool:
    """Match input data against a key identifier string.

    Supported key identifiers:
    - Single keys: "escape", "tab", "enter", "backspace", "delete", "home", "end", "space"
    - Arrow keys: "up", "down", "left", "right"
    - Ctrl combinations: "ctrl+c", "ctrl+z", etc.
    - Shift combinations: "shift+tab", "shift+enter"
    - Alt combinations: "alt+enter", "alt+backspace"
    - Super combinations: "super+k", "super+enter"
    - Combined modifiers: "shift+ctrl+p", "ctrl+alt+x", "ctrl+super+k"

    Use the Key helper for autocomplete: Key.ctrl("c"), Key.escape, Key.ctrlShift("p"), Key.super("k")

    @param data - Raw input data from terminal
    @param keyId - Key identifier (e.g., "ctrl+c", "escape", Key.ctrl("c"))
    """
    parsed = parseKeyId(keyId)
    if not parsed:
        return False

    key: str = parsed["key"]
    ctrl: bool = parsed["ctrl"]
    shift: bool = parsed["shift"]
    alt: bool = parsed["alt"]
    superModifier: bool = parsed["super"]
    modifier = 0
    if shift:
        modifier |= MODIFIERS.shift
    if alt:
        modifier |= MODIFIERS.alt
    if ctrl:
        modifier |= MODIFIERS.ctrl
    if superModifier:
        modifier |= MODIFIERS.super

    match key:
        case "escape" | "esc":
            if modifier != 0:
                return False
            return (
                data == "\x1b"
                or matchesKittySequence(data, CODEPOINTS.escape, 0)
                or matchesModifyOtherKeys(data, CODEPOINTS.escape, 0)
            )

        case "space":
            if not _kittyProtocolActive:
                if modifier == MODIFIERS.ctrl and data == "\x00":
                    return True
                if modifier == MODIFIERS.alt and data == "\x1b ":
                    return True
            if modifier == 0:
                return (
                    data == " "
                    or matchesKittySequence(data, CODEPOINTS.space, 0)
                    or matchesModifyOtherKeys(data, CODEPOINTS.space, 0)
                )
            return matchesKittySequence(data, CODEPOINTS.space, modifier) or matchesModifyOtherKeys(
                data, CODEPOINTS.space, modifier
            )

        case "tab":
            if modifier == MODIFIERS.shift:
                return (
                    data == "\x1b[Z"
                    or matchesKittySequence(data, CODEPOINTS.tab, MODIFIERS.shift)
                    or matchesModifyOtherKeys(data, CODEPOINTS.tab, MODIFIERS.shift)
                )
            if modifier == 0:
                return data == "\t" or matchesKittySequence(data, CODEPOINTS.tab, 0)
            return matchesKittySequence(data, CODEPOINTS.tab, modifier) or matchesModifyOtherKeys(
                data, CODEPOINTS.tab, modifier
            )

        case "enter" | "return":
            if modifier == MODIFIERS.shift:
                # CSI u sequences (standard Kitty protocol)
                if matchesKittySequence(data, CODEPOINTS.enter, MODIFIERS.shift) or matchesKittySequence(
                    data, CODEPOINTS.kpEnter, MODIFIERS.shift
                ):
                    return True
                # xterm modifyOtherKeys format (fallback when Kitty protocol not enabled)
                if matchesModifyOtherKeys(data, CODEPOINTS.enter, MODIFIERS.shift):
                    return True
                # When Kitty protocol is active, legacy sequences are custom terminal mappings
                # \x1b\r = Kitty's "map shift+enter send_text all \e\r"
                # \n = Ghostty's "keybind = shift+enter=text:\n"
                if _kittyProtocolActive:
                    return data == "\x1b\r" or data == "\n"
                return False
            if modifier == MODIFIERS.alt:
                # CSI u sequences (standard Kitty protocol)
                if matchesKittySequence(data, CODEPOINTS.enter, MODIFIERS.alt) or matchesKittySequence(
                    data, CODEPOINTS.kpEnter, MODIFIERS.alt
                ):
                    return True
                # xterm modifyOtherKeys format (fallback when Kitty protocol not enabled)
                if matchesModifyOtherKeys(data, CODEPOINTS.enter, MODIFIERS.alt):
                    return True
                # \x1b\r is alt+enter only in legacy mode (no Kitty protocol)
                # When Kitty protocol is active, alt+enter comes as CSI u sequence
                if not _kittyProtocolActive:
                    return data == "\x1b\r"
                return False
            if modifier == 0:
                return (
                    data == "\r"
                    or (not _kittyProtocolActive and data == "\n")
                    or data == "\x1bOM"  # SS3 M (numpad enter in some terminals)
                    or matchesKittySequence(data, CODEPOINTS.enter, 0)
                    or matchesKittySequence(data, CODEPOINTS.kpEnter, 0)
                )
            return (
                matchesKittySequence(data, CODEPOINTS.enter, modifier)
                or matchesKittySequence(data, CODEPOINTS.kpEnter, modifier)
                or matchesModifyOtherKeys(data, CODEPOINTS.enter, modifier)
            )

        case "backspace":
            if modifier == MODIFIERS.alt:
                if data == "\x1b\x7f" or data == "\x1b\b":
                    return True
                return matchesKittySequence(data, CODEPOINTS.backspace, MODIFIERS.alt) or matchesModifyOtherKeys(
                    data, CODEPOINTS.backspace, MODIFIERS.alt
                )
            if modifier == MODIFIERS.ctrl:
                # Legacy raw 0x08 is ambiguous: it can be Ctrl+Backspace on Windows
                # Terminal or plain Backspace on other terminals, while also
                # overlapping with Ctrl+H.
                if matchesRawBackspace(data, MODIFIERS.ctrl):
                    return True
                return matchesKittySequence(data, CODEPOINTS.backspace, MODIFIERS.ctrl) or matchesModifyOtherKeys(
                    data, CODEPOINTS.backspace, MODIFIERS.ctrl
                )
            if modifier == 0:
                return (
                    matchesRawBackspace(data, 0)
                    or matchesKittySequence(data, CODEPOINTS.backspace, 0)
                    or matchesModifyOtherKeys(data, CODEPOINTS.backspace, 0)
                )
            return matchesKittySequence(data, CODEPOINTS.backspace, modifier) or matchesModifyOtherKeys(
                data, CODEPOINTS.backspace, modifier
            )

        case "insert":
            if modifier == 0:
                return matchesLegacySequence(data, LEGACY_KEY_SEQUENCES["insert"]) or matchesKittySequence(
                    data, FUNCTIONAL_CODEPOINTS.insert, 0
                )
            if matchesLegacyModifierSequence(data, "insert", modifier):
                return True
            return matchesKittySequence(data, FUNCTIONAL_CODEPOINTS.insert, modifier)

        case "delete":
            if modifier == 0:
                return matchesLegacySequence(data, LEGACY_KEY_SEQUENCES["delete"]) or matchesKittySequence(
                    data, FUNCTIONAL_CODEPOINTS.delete, 0
                )
            if matchesLegacyModifierSequence(data, "delete", modifier):
                return True
            return matchesKittySequence(data, FUNCTIONAL_CODEPOINTS.delete, modifier)

        case "clear":
            if modifier == 0:
                return matchesLegacySequence(data, LEGACY_KEY_SEQUENCES["clear"])
            return matchesLegacyModifierSequence(data, "clear", modifier)

        case "home":
            if modifier == 0:
                return matchesLegacySequence(data, LEGACY_KEY_SEQUENCES["home"]) or matchesKittySequence(
                    data, FUNCTIONAL_CODEPOINTS.home, 0
                )
            if matchesLegacyModifierSequence(data, "home", modifier):
                return True
            return matchesKittySequence(data, FUNCTIONAL_CODEPOINTS.home, modifier)

        case "end":
            if modifier == 0:
                return matchesLegacySequence(data, LEGACY_KEY_SEQUENCES["end"]) or matchesKittySequence(
                    data, FUNCTIONAL_CODEPOINTS.end, 0
                )
            if matchesLegacyModifierSequence(data, "end", modifier):
                return True
            return matchesKittySequence(data, FUNCTIONAL_CODEPOINTS.end, modifier)

        case "pageup":
            if modifier == 0:
                return matchesLegacySequence(data, LEGACY_KEY_SEQUENCES["pageUp"]) or matchesKittySequence(
                    data, FUNCTIONAL_CODEPOINTS.pageUp, 0
                )
            if matchesLegacyModifierSequence(data, "pageUp", modifier):
                return True
            return matchesKittySequence(data, FUNCTIONAL_CODEPOINTS.pageUp, modifier)

        case "pagedown":
            if modifier == 0:
                return matchesLegacySequence(data, LEGACY_KEY_SEQUENCES["pageDown"]) or matchesKittySequence(
                    data, FUNCTIONAL_CODEPOINTS.pageDown, 0
                )
            if matchesLegacyModifierSequence(data, "pageDown", modifier):
                return True
            return matchesKittySequence(data, FUNCTIONAL_CODEPOINTS.pageDown, modifier)

        case "up":
            if modifier == MODIFIERS.alt:
                return data == "\x1bp" or matchesKittySequence(data, ARROW_CODEPOINTS.up, MODIFIERS.alt)
            if modifier == 0:
                return matchesLegacySequence(data, LEGACY_KEY_SEQUENCES["up"]) or matchesKittySequence(
                    data, ARROW_CODEPOINTS.up, 0
                )
            if matchesLegacyModifierSequence(data, "up", modifier):
                return True
            return matchesKittySequence(data, ARROW_CODEPOINTS.up, modifier)

        case "down":
            if modifier == MODIFIERS.alt:
                return data == "\x1bn" or matchesKittySequence(data, ARROW_CODEPOINTS.down, MODIFIERS.alt)
            if modifier == 0:
                return matchesLegacySequence(data, LEGACY_KEY_SEQUENCES["down"]) or matchesKittySequence(
                    data, ARROW_CODEPOINTS.down, 0
                )
            if matchesLegacyModifierSequence(data, "down", modifier):
                return True
            return matchesKittySequence(data, ARROW_CODEPOINTS.down, modifier)

        case "left":
            if modifier == MODIFIERS.alt:
                return (
                    data == "\x1b[1;3D"
                    or (not _kittyProtocolActive and data == "\x1bB")
                    or data == "\x1bb"
                    or matchesKittySequence(data, ARROW_CODEPOINTS.left, MODIFIERS.alt)
                )
            if modifier == MODIFIERS.ctrl:
                return (
                    data == "\x1b[1;5D"
                    or matchesLegacyModifierSequence(data, "left", MODIFIERS.ctrl)
                    or matchesKittySequence(data, ARROW_CODEPOINTS.left, MODIFIERS.ctrl)
                )
            if modifier == 0:
                return matchesLegacySequence(data, LEGACY_KEY_SEQUENCES["left"]) or matchesKittySequence(
                    data, ARROW_CODEPOINTS.left, 0
                )
            if matchesLegacyModifierSequence(data, "left", modifier):
                return True
            return matchesKittySequence(data, ARROW_CODEPOINTS.left, modifier)

        case "right":
            if modifier == MODIFIERS.alt:
                return (
                    data == "\x1b[1;3C"
                    or (not _kittyProtocolActive and data == "\x1bF")
                    or data == "\x1bf"
                    or matchesKittySequence(data, ARROW_CODEPOINTS.right, MODIFIERS.alt)
                )
            if modifier == MODIFIERS.ctrl:
                return (
                    data == "\x1b[1;5C"
                    or matchesLegacyModifierSequence(data, "right", MODIFIERS.ctrl)
                    or matchesKittySequence(data, ARROW_CODEPOINTS.right, MODIFIERS.ctrl)
                )
            if modifier == 0:
                return matchesLegacySequence(data, LEGACY_KEY_SEQUENCES["right"]) or matchesKittySequence(
                    data, ARROW_CODEPOINTS.right, 0
                )
            if matchesLegacyModifierSequence(data, "right", modifier):
                return True
            return matchesKittySequence(data, ARROW_CODEPOINTS.right, modifier)

        case "f1" | "f2" | "f3" | "f4" | "f5" | "f6" | "f7" | "f8" | "f9" | "f10" | "f11" | "f12":
            if modifier != 0:
                return False
            functionKey = key
            return matchesLegacySequence(data, LEGACY_KEY_SEQUENCES[functionKey])

    # Handle single letter/digit keys and symbols
    if len(key) == 1 and ((key >= "a" and key <= "z") or isDigitKey(key) or key in SYMBOL_KEYS):
        codepoint = ord(key)
        rawCtrl = rawCtrlChar(key)
        isLetter = key >= "a" and key <= "z"
        isDigit = isDigitKey(key)

        if modifier == MODIFIERS.ctrl + MODIFIERS.alt and not _kittyProtocolActive and rawCtrl:  # noqa: SIM102
            # Legacy: ctrl+alt+key is ESC followed by the control character.
            # If that legacy form does not match, continue so CSI-u and
            # modifyOtherKeys sequences from tmux can still be recognized.
            if data == f"\x1b{rawCtrl}":
                return True

        if (  # noqa: SIM102 - upstream nests the legacy check under its comment
            modifier == MODIFIERS.alt and not _kittyProtocolActive and (isLetter or isDigit or key in SYMBOL_KEYS)
        ):
            # Legacy: alt+printable key is ESC followed by the key
            if data == f"\x1b{key}":
                return True

        if modifier == MODIFIERS.ctrl:
            # Legacy: ctrl+key sends the control character
            if rawCtrl and data == rawCtrl:
                return True
            return matchesKittySequence(data, codepoint, MODIFIERS.ctrl) or matchesPrintableModifyOtherKeys(
                data, codepoint, MODIFIERS.ctrl
            )

        if modifier == MODIFIERS.shift + MODIFIERS.ctrl:
            return matchesKittySequence(
                data, codepoint, MODIFIERS.shift + MODIFIERS.ctrl
            ) or matchesPrintableModifyOtherKeys(data, codepoint, MODIFIERS.shift + MODIFIERS.ctrl)

        if modifier == MODIFIERS.shift:
            # Legacy: shift+letter produces uppercase
            if isLetter and data == key.upper():
                return True
            return matchesKittySequence(data, codepoint, MODIFIERS.shift) or matchesPrintableModifyOtherKeys(
                data, codepoint, MODIFIERS.shift
            )

        if modifier != 0:
            return matchesKittySequence(data, codepoint, modifier) or matchesPrintableModifyOtherKeys(
                data, codepoint, modifier
            )

        # Check both raw char and Kitty sequence (needed for release events)
        return (
            data == key
            or matchesKittySequence(data, codepoint, 0)
            or matchesPrintableModifyOtherKeys(data, codepoint, 0)
        )

    return False


# Parse input data and return the key identifier if recognized.
#
# @param data - Raw input data from terminal
# @returns Key identifier string (e.g., "ctrl+c") or undefined
def formatParsedKey(codepoint: int, modifier: int, baseLayoutKey: int | None = None) -> str | None:
    normalizedCodepoint = normalizeKittyFunctionalCodepoint(codepoint)
    identityCodepoint = normalizeShiftedLetterIdentityCodepoint(normalizedCodepoint, modifier)

    # Use base layout key only when codepoint is not a recognized Latin
    # letter (a-z), digit (0-9), or symbol (/, -, [, ;, etc.). For those,
    # the codepoint is authoritative regardless of physical key position.
    # This prevents remapped layouts (Dvorak, Colemak, xremap, etc.) from
    # reporting the wrong key name based on the QWERTY physical position.
    isLatinLetter = identityCodepoint >= 97 and identityCodepoint <= 122  # a-z
    isDigit = identityCodepoint >= 48 and identityCodepoint <= 57  # 0-9
    isKnownSymbol = fromCharCode(identityCodepoint) in SYMBOL_KEYS
    effectiveCodepoint = (
        identityCodepoint
        if isLatinLetter or isDigit or isKnownSymbol
        else (baseLayoutKey if baseLayoutKey is not None else identityCodepoint)
    )

    keyName: str | None = None
    if effectiveCodepoint == CODEPOINTS.escape:
        keyName = "escape"
    elif effectiveCodepoint == CODEPOINTS.tab:
        keyName = "tab"
    elif effectiveCodepoint == CODEPOINTS.enter or effectiveCodepoint == CODEPOINTS.kpEnter:
        keyName = "enter"
    elif effectiveCodepoint == CODEPOINTS.space:
        keyName = "space"
    elif effectiveCodepoint == CODEPOINTS.backspace:
        keyName = "backspace"
    elif effectiveCodepoint == FUNCTIONAL_CODEPOINTS.delete:
        keyName = "delete"
    elif effectiveCodepoint == FUNCTIONAL_CODEPOINTS.insert:
        keyName = "insert"
    elif effectiveCodepoint == FUNCTIONAL_CODEPOINTS.home:
        keyName = "home"
    elif effectiveCodepoint == FUNCTIONAL_CODEPOINTS.end:
        keyName = "end"
    elif effectiveCodepoint == FUNCTIONAL_CODEPOINTS.pageUp:
        keyName = "pageUp"
    elif effectiveCodepoint == FUNCTIONAL_CODEPOINTS.pageDown:
        keyName = "pageDown"
    elif effectiveCodepoint == ARROW_CODEPOINTS.up:
        keyName = "up"
    elif effectiveCodepoint == ARROW_CODEPOINTS.down:
        keyName = "down"
    elif effectiveCodepoint == ARROW_CODEPOINTS.left:
        keyName = "left"
    elif effectiveCodepoint == ARROW_CODEPOINTS.right:
        keyName = "right"
    elif effectiveCodepoint >= 48 and effectiveCodepoint <= 57:  # noqa: SIM114 - upstream keeps digits apart
        keyName = fromCharCode(effectiveCodepoint)
    elif effectiveCodepoint >= 97 and effectiveCodepoint <= 122:  # noqa: SIM114 - upstream keeps letters apart
        keyName = fromCharCode(effectiveCodepoint)
    elif fromCharCode(effectiveCodepoint) in SYMBOL_KEYS:
        keyName = fromCharCode(effectiveCodepoint)

    if not keyName:
        return None
    return formatKeyNameWithModifiers(keyName, modifier)


def parseKey(data: str) -> str | None:
    kitty = parseKittySequence(data)
    if kitty:
        logicalShiftedSymbol = getLogicalShiftedSymbolCandidate(kitty.shiftedKey, kitty.modifier, SYMBOL_KEYS)
        return formatParsedKey(
            logicalShiftedSymbol.codepoint if logicalShiftedSymbol is not None else kitty.codepoint,
            logicalShiftedSymbol.modifier if logicalShiftedSymbol is not None else kitty.modifier,
            kitty.baseLayoutKey,
        )

    modifyOtherKeys = parseModifyOtherKeysSequence(data)
    if modifyOtherKeys:
        logicalShiftedSymbol = getLogicalShiftedSymbolCandidate(
            modifyOtherKeys.codepoint,
            modifyOtherKeys.modifier,
            SHIFTED_SYMBOL_KEYS,
        )
        return formatParsedKey(
            logicalShiftedSymbol.codepoint if logicalShiftedSymbol is not None else modifyOtherKeys.codepoint,
            logicalShiftedSymbol.modifier if logicalShiftedSymbol is not None else modifyOtherKeys.modifier,
        )

    # Mode-aware legacy sequences
    # When Kitty protocol is active, ambiguous sequences are interpreted as custom terminal mappings:
    # - \x1b\r = shift+enter (Kitty mapping), not alt+enter
    # - \n = shift+enter (Ghostty mapping)
    if _kittyProtocolActive:  # noqa: SIM102 - upstream nests the mapping check
        if data == "\x1b\r" or data == "\n":
            return "shift+enter"

    legacySequenceKeyId = LEGACY_SEQUENCE_KEY_IDS.get(data)
    if legacySequenceKeyId:
        return legacySequenceKeyId

    # Legacy sequences (used when Kitty protocol is not active, or for unambiguous sequences)
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
    if data == "\r" or (not _kittyProtocolActive and data == "\n") or data == "\x1bOM":
        return "enter"
    if data == "\x00":
        return "ctrl+space"
    if data == " ":
        return "space"
    if data == "\x7f":
        return "backspace"
    if data == "\x08":
        return "ctrl+backspace" if isWindowsTerminalSession() else "backspace"
    if data == "\x1b[Z":
        return "shift+tab"
    if not _kittyProtocolActive and data == "\x1b\r":
        return "alt+enter"
    if not _kittyProtocolActive and data == "\x1b ":
        return "alt+space"
    if data == "\x1b\x7f" or data == "\x1b\b":
        return "alt+backspace"
    if not _kittyProtocolActive and data == "\x1bB":
        return "alt+left"
    if not _kittyProtocolActive and data == "\x1bF":
        return "alt+right"
    if not _kittyProtocolActive and len(data) == 2 and data[0] == "\x1b":
        code = ord(data[1])
        if code >= 1 and code <= 26:
            return f"ctrl+alt+{chr(code + 96)}"
        # Legacy alt+letter/digit/symbol (ESC followed by the key)
        key = data[1]
        if (code >= 97 and code <= 122) or (code >= 48 and code <= 57) or key in SYMBOL_KEYS:
            return f"alt+{key}"
    if data == "\x1b[A":
        return "up"
    if data == "\x1b[B":
        return "down"
    if data == "\x1b[C":
        return "right"
    if data == "\x1b[D":
        return "left"
    if data == "\x1b[H" or data == "\x1bOH":
        return "home"
    if data == "\x1b[F" or data == "\x1bOF":
        return "end"
    if data == "\x1b[3~":
        return "delete"
    if data == "\x1b[5~":
        return "pageUp"
    if data == "\x1b[6~":
        return "pageDown"

    # Raw Ctrl+letter
    if len(data) == 1:
        code = ord(data[0])
        if code >= 1 and code <= 26:
            return f"ctrl+{chr(code + 96)}"
        if code >= 32 and code <= 126:
            return data

    return None


# =============================================================================
# Kitty CSI-u Printable Decoding
# =============================================================================

KITTY_CSI_U_REGEX = RegExp(r"^\x1b\[(\d+)(?::(\d*))?(?::(\d+))?(?:;(\d+))?(?::(\d+))?u$")
KITTY_PRINTABLE_ALLOWED_MODIFIERS = MODIFIERS.shift | LOCK_MASK


def decodeKittyPrintable(data: str) -> str | None:
    """Decode a Kitty CSI-u sequence into a printable character, if applicable.

    When Kitty keyboard protocol flag 1 (disambiguate) is active, terminals send
    CSI-u sequences for all keys, including plain printable characters. This
    function extracts the printable character from such sequences.

    Only accepts plain or Shift-modified keys. Rejects Ctrl, Alt, and unsupported
    modifier combinations (those are handled by keybinding matching instead).
    Prefers the shifted keycode when Shift is held and a shifted key is reported.

    @param data - Raw input data from terminal
    @returns The printable character, or undefined if not a printable CSI-u sequence
    """
    match = js_regexp.match(data, KITTY_CSI_U_REGEX)
    if not match:
        return None

    # CSI-u groups: <codepoint>[:<shifted>[:<base>]];<mod>[:<event>]u
    # (The digits always parse; upstream's `Number.isFinite` checks guard its float parse.)
    codepoint = int(match[1], 10)

    shiftedKey = int(match[2], 10) if match[2] and len(match[2]) > 0 else None
    modValue = int(match[4], 10) if match[4] else 1
    # Modifiers are 1-indexed in CSI-u; normalize to our bitmask.
    modifier = modValue - 1

    # Only accept printable CSI-u input for plain or Shift-modified text keys.
    # Reject unsupported modifier bits (e.g. Super/Meta) to avoid inserting
    # characters from modifier-only terminal events.
    if (modifier & ~KITTY_PRINTABLE_ALLOWED_MODIFIERS) != 0:
        return None
    if modifier & (MODIFIERS.alt | MODIFIERS.ctrl):
        return None

    # Prefer the shifted keycode when Shift is held.
    effectiveCodepoint = codepoint
    if modifier & MODIFIERS.shift and shiftedKey is not None:
        effectiveCodepoint = shiftedKey
    effectiveCodepoint = normalizeKittyFunctionalCodepoint(effectiveCodepoint)
    # Drop control characters or invalid codepoints.
    if effectiveCodepoint < 32:
        return None

    try:
        return chr(effectiveCodepoint)
    except (ValueError, OverflowError):
        return None


def decodeModifyOtherKeysPrintable(data: str) -> str | None:
    parsed = parseModifyOtherKeysSequence(data)
    if not parsed:
        return None
    modifier = parsed.modifier & ~LOCK_MASK
    if (modifier & ~MODIFIERS.shift) != 0:
        return None
    if parsed.codepoint < 32:
        return None

    try:
        return chr(parsed.codepoint)
    except (ValueError, OverflowError):
        return None


def decodePrintableKey(data: str) -> str | None:
    printable = decodeKittyPrintable(data)
    return printable if printable is not None else decodeModifyOtherKeysPrintable(data)


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

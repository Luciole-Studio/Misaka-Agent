"""Events -> bytes for the program in a pane, in the protocol that program negotiated.

Port of herdr ``src/input/encode.rs`` (keys: legacy, xterm modified specials, kitty CSI u)
and its mouse report encoder, plus the routing the pane runtime does around them
(``wheel_routing``, ``paste_payload``, ``try_send_focus_event``,
``plain_page_keys_use_host_scrollback`` in pane.rs / pane/terminal.rs). ``state`` is the
daemon's ``input_state`` dict for the pane.
"""
from misaka.ui.panel.host_input import (
    ALT,
    CTRL,
    HYPER,
    LOCK_MASK,
    META,
    SHIFT,
    SUPER,
    Key,
)

KITTY_DISAMBIGUATE = 1
KITTY_REPORT_EVENT_TYPES = 2
KITTY_REPORT_ALTERNATE_KEYS = 4
KITTY_REPORT_ALL_KEYS = 8
KITTY_REPORT_ASSOCIATED_TEXT = 16
# What the panel asks its own host for: the IME-compatible subset herdr pushes
# (input/model.rs ime_compatible_keyboard_enhancement_flags).
HOST_KITTY_FLAGS = KITTY_DISAMBIGUATE | KITTY_REPORT_EVENT_TYPES | KITTY_REPORT_ALTERNATE_KEYS

_SPECIAL_LEGACY = {
    "enter": b"\r", "backspace": b"\x7f", "tab": b"\t", "backtab": b"\x1b[Z", "esc": b"\x1b",
    "left": b"\x1b[D", "right": b"\x1b[C", "up": b"\x1b[A", "down": b"\x1b[B",
    "home": b"\x1b[H", "end": b"\x1b[F", "pageup": b"\x1b[5~", "pagedown": b"\x1b[6~",
    "delete": b"\x1b[3~", "insert": b"\x1b[2~",
    "f1": b"\x1bOP", "f2": b"\x1bOQ", "f3": b"\x1bOR", "f4": b"\x1bOS", "f5": b"\x1b[15~",
    "f6": b"\x1b[17~", "f7": b"\x1b[18~", "f8": b"\x1b[19~", "f9": b"\x1b[20~",
    "f10": b"\x1b[21~", "f11": b"\x1b[23~", "f12": b"\x1b[24~",
}
_APPLICATION_CURSOR = {"up": b"\x1bOA", "down": b"\x1bOB", "right": b"\x1bOC", "left": b"\x1bOD",
                       "home": b"\x1bOH", "end": b"\x1bOF"}
_MODIFIED_LETTER = {"up": "A", "down": "B", "right": "C", "left": "D", "home": "H", "end": "F",
                    "f1": "P", "f2": "Q", "f3": "R", "f4": "S"}
_MODIFIED_TILDE = {"insert": 2, "delete": 3, "pageup": 5, "pagedown": 6, "f5": 15, "f6": 17,
                   "f7": 18, "f8": 19, "f9": 20, "f10": 21, "f11": 23, "f12": 24}
_KITTY_NAMED = {"enter": 13, "tab": 9, "backspace": 127, "esc": 27, "left": 57417, "right": 57418,
                "up": 57419, "down": 57420, "pageup": 57421, "pagedown": 57422, "home": 57423,
                "end": 57424, "insert": 57425, "delete": 57426}
_CTRL_PUNCT = {" ": 0, "@": 0, "2": 0, "[": 27, "3": 27, "\\": 28, "4": 28, "]": 29, "5": 29,
               "^": 30, "6": 30, "_": 31, "/": 31, "7": 31, "-": 31}
_SHIFTED_PUNCTUATION = set("!@#$%^&*()_+{}|:\"<>?~")
_WHEEL_BUTTONS = {"wheel_up": 64, "wheel_down": 65, "wheel_left": 66, "wheel_right": 67}
_EVENT_SUFFIX = {"press": 1, "repeat": 2, "release": 3}


def _clean(mods):
    return mods & ~LOCK_MASK


def _xterm_modifier(mods):
    return 1 + (1 if mods & SHIFT else 0) + (2 if mods & ALT else 0) + (4 if mods & CTRL else 0)


def _kitty_modifier(mods):
    return (_xterm_modifier(mods) + (8 if mods & SUPER else 0) + (16 if mods & HYPER else 0)
            + (32 if mods & META else 0))


def _shifted_text(key):
    ch = key.code
    if key.shifted is not None:
        try:
            return chr(key.shifted)
        except ValueError:
            return None
    if ch.isupper() or ch in _SHIFTED_PUNCTUATION:
        return ch
    if ch.islower():
        return ch.upper()
    return None


def _text_char(key):
    """The text a key types by itself, or None when it is a chord / not a character."""
    if key.kind == "release" or not key.is_char:
        return None
    mods = _clean(key.mods)
    if mods == 0:
        return key.code
    if mods == SHIFT:
        return _shifted_text(key)
    return None


def _csi_u(key, flags):
    mods = _clean(key.mods)
    report_all = bool(flags & KITTY_REPORT_ALL_KEYS)
    suffix = _EVENT_SUFFIX[key.kind] if flags & KITTY_REPORT_EVENT_TYPES else None
    if not report_all and mods == 0 and key.code in ("enter", "tab", "backspace"):
        return None
    if mods == 0 and suffix is None and not report_all:
        return None
    if (key.code in _MODIFIED_LETTER or key.code in _MODIFIED_TILDE) and suffix is None and not report_all:
        return None              # xterm's modified forms are understood everywhere (herdr does the same)
    alternate = None
    if key.is_char:
        base = key.code.lower() if mods & SHIFT and key.code.isupper() else key.code
        codepoint = ord(base)
        if flags & KITTY_REPORT_ALTERNATE_KEYS:
            if key.shifted is not None:
                alternate = key.shifted
            elif mods & SHIFT and key.code.isupper():
                alternate = ord(key.code)
    elif key.code in _KITTY_NAMED:
        codepoint = _KITTY_NAMED[key.code]
    else:
        return None
    out = f"\x1b[{codepoint}"
    if alternate is not None:
        out += f":{alternate}"
    out += f";{_kitty_modifier(mods)}"
    if suffix is not None:
        out += f":{suffix}"
    if flags & KITTY_REPORT_ASSOCIATED_TEXT:
        text = _text_char(key)
        if text and ord(text) >= 32:
            out += f";{ord(text)}"
    return (out + "u").encode()


def _modified_special(code, mods):
    modifier = _xterm_modifier(mods)
    if modifier <= 1:
        return None
    if code in _MODIFIED_LETTER:
        return f"\x1b[1;{modifier}{_MODIFIED_LETTER[code]}".encode()
    if code in _MODIFIED_TILDE:
        return f"\x1b[{_MODIFIED_TILDE[code]};{modifier}~".encode()
    return None


def _legacy_inner(key, application_cursor):
    if key.is_char:
        ch = key.code
        if key.mods & CTRL:
            upper = ch.upper()
            if "A" <= upper <= "Z":
                return bytes([ord(upper) - 64])
            if ch in _CTRL_PUNCT:
                return bytes([_CTRL_PUNCT[ch]])
            return ch.encode()
        if _clean(key.mods) == SHIFT:
            ch = _shifted_text(key) or ch
        return ch.encode()
    if application_cursor and key.code in _APPLICATION_CURSOR:
        return _APPLICATION_CURSOR[key.code]
    return _SPECIAL_LEGACY.get(key.code, b"")


def _legacy(key, state):
    mods = _clean(key.mods)
    if mods:
        special = _modified_special(key.code, mods)
        if special is not None:
            return special
    if mods & ALT:
        inner = Key(key.code, mods & ~ALT, key.kind, key.shifted)
        return b"\x1b" + _legacy_inner(inner, state.get("application_cursor", False))
    return _legacy_inner(key, state.get("application_cursor", False))


def _modify_other_keys(key, state):
    """XTMODKEYS mode 2 (and mode 1 for shifted chords): ``CSI 27 ; mod ; codepoint ~``
    for character chords that have no distinct legacy byte."""
    mode = state.get("modify_other_keys", 0)
    mods = _clean(key.mods)
    if not mode or not key.is_char or not mods & (CTRL | ALT):
        return None
    if mode == 1 and not mods & SHIFT:
        return None
    return f"\x1b[27;{_xterm_modifier(mods)};{ord(key.code)}~".encode()


def encode_key(key, state):
    """herdr ``encode_terminal_key``: generated text first; releases only under kitty
    REPORT_EVENT_TYPES; CSI u for chords when the pane pushed kitty flags; else legacy."""
    flags = state.get("kitty_flags", 0)
    reports_events = bool(flags & KITTY_REPORT_EVENT_TYPES)
    if key.code in ("modifier", "media") and not flags:
        return b""
    if key.kind != "release" and key.text:
        return key.text.encode()
    if key.kind == "release" and not reports_events:
        return b""
    kitty_first = bool(flags & KITTY_REPORT_ALL_KEYS) or (key.kind == "release" and reports_events)
    if kitty_first and flags:
        encoded = _csi_u(key, flags)
        if encoded is not None:
            return encoded
    text = _text_char(key)
    if text is not None:
        return text.encode()
    if not kitty_first and flags:
        encoded = _csi_u(key, flags)
        if encoded is not None:
            return encoded
    if key.kind == "release":
        return b""
    if key.code in ("modifier", "media"):
        return b""
    encoded = _modify_other_keys(key, state)
    if encoded is not None:
        return encoded
    return _legacy(key, state)


def wants_mouse(state):
    return state.get("mouse_mode", "none") != "none"


def encode_mouse(event, col, row, state):
    """A mouse event at pane cell (col, row) as the pane's mouse report, or None when the
    pane did not ask for that kind of event (herdr encode_mouse_button/motion/wheel plus
    ghostty's mode gating)."""
    mode = state.get("mouse_mode", "none")
    if mode == "none":
        return None
    if event.kind in _WHEEL_BUTTONS:
        if mode == "x10":
            return None
        return _mouse_report(_WHEEL_BUTTONS[event.kind], False, col, row, event.mods, state)
    if event.kind == "move":
        if mode != "any_motion":
            return None
        return _mouse_report(35, False, col, row, event.mods, state)
    if event.kind == "drag":
        if mode not in ("button_motion", "any_motion"):
            return None
        return _mouse_report(32 + event.button, False, col, row, event.mods, state)
    if event.kind == "release" and mode == "x10":
        return None
    return _mouse_report(event.button, event.kind == "release", col, row, event.mods, state)


def _mouse_report(button, release, col, row, mods, state):
    encoding = state.get("mouse_encoding", "default")
    cb = button if (not release or encoding in ("sgr", "sgr_pixels")) else 3
    cb += (4 if mods & SHIFT else 0) + (8 if mods & ALT else 0) + (16 if mods & CTRL else 0)
    col, row = col + 1, row + 1
    if encoding in ("sgr", "sgr_pixels"):
        return f"\x1b[<{cb};{col};{row}{'m' if release else 'M'}".encode()
    if encoding == "utf8":
        return b"\x1b[M" + "".join(chr(value + 32) for value in (cb, col, row)).encode()
    if cb + 32 > 255 or col + 32 > 255 or row + 32 > 255:
        return None
    return bytes([0x1b, ord("["), ord("M"), cb + 32, col + 32, row + 32])


def wheel_routing(state):
    """mouse_report | alternate_scroll | host_scroll (herdr WheelRouting)."""
    if wants_mouse(state):
        return "mouse_report"
    if state.get("alternate_screen") and state.get("mouse_alternate_scroll", True):
        return "alternate_scroll"
    return "host_scroll"


def encode_alternate_scroll(event, state):
    """A wheel notch on the alternate screen as the arrow key the pane would get from a
    terminal (herdr encode_alternate_scroll); None when the routing is not alternate scroll."""
    if wheel_routing(state) != "alternate_scroll":
        return None
    code = {"wheel_up": "up", "wheel_down": "down"}.get(event.kind)
    return encode_key(Key(code), state) if code else None


def paste_bytes(text, state):
    """herdr paste_payload: bracketed when the pane asked for it, raw otherwise."""
    if state.get("bracketed_paste"):
        return ("\x1b[200~" + text + "\x1b[201~").encode()
    return text.encode()


def focus_bytes(gained, state):
    """The focus report a pane that enabled ``?1004`` expects; None otherwise."""
    if not state.get("focus_reporting"):
        return None
    return b"\x1b[I" if gained else b"\x1b[O"


def plain_page_keys_use_host_scrollback(state):
    """herdr InputState.plain_page_keys_use_host_scrollback: an unmodified PageUp/PageDown
    scrolls the pane's history when the pane looks like a shell transcript (bracketed paste
    marks a line editor; a pager keeps the primary screen but takes application cursor mode)."""
    return (not state.get("alternate_screen") and not wants_mouse(state)
            and (not state.get("application_cursor") or bool(state.get("bracketed_paste"))))


DEFAULT_STATE = {"alternate_screen": False, "application_cursor": False, "bracketed_paste": False,
                 "focus_reporting": False, "mouse_mode": "none", "mouse_encoding": "default",
                 "mouse_alternate_scroll": True, "modify_other_keys": 0, "kitty_flags": 0}

"""Host terminal bytes -> input events: keys, mouse, paste, focus.

Port of herdr ``src/input/parse.rs`` (key sequences: kitty CSI u, modifyOtherKeys, xterm
modified specials, legacy) on top of the sequence splitting crossterm does for herdr
(SGR/X10 mouse reports, bracketed paste, focus events, the lone-ESC timeout). The panel
never looks at raw bytes again: every mode handler receives ``Key``/``Mouse``/``Paste``/
``Focus`` values, and the pane encoder (``pane_input``) turns a ``Key`` back into bytes
for the protocol the pane negotiated.
"""
import codecs
import os
import re
import time
from dataclasses import dataclass

SHIFT, ALT, CTRL, SUPER, HYPER, META = 1, 2, 4, 8, 16, 32
LOCK_MASK = 64 | 128                 # caps / num lock bits (kitty): never part of a binding

ESCAPE_TIMEOUT = 0.010               # a lone ESC waits this long for an Alt+key second byte
SSH_ESCAPE_TIMEOUT = 0.100
SEQUENCE_TIMEOUT = 0.050             # a longer partial sequence gets the wider window


@dataclass(frozen=True, slots=True)
class Key:
    """One key event. ``code`` is the character itself for character keys, otherwise a
    name: enter, tab, backtab, backspace, esc, up, down, left, right, home, end, pageup,
    pagedown, insert, delete, f1..f35, modifier (a bare shift/ctrl/alt press under the kitty
    protocol), media. ``mods`` are the kitty modifier bits (SHIFT .. META, plus lock bits)."""
    code: str
    mods: int = 0
    kind: str = "press"              # press | repeat | release
    shifted: int | None = None       # kitty alternate (shifted) codepoint, when reported
    text: str | None = None          # kitty associated text, when reported

    @property
    def is_char(self):
        return len(self.code) == 1

    def matches(self, code, mods=0):
        """Binding match: the lock bits never count (herdr terminal_key_matches_combo)."""
        return self.code == code and (self.mods & ~LOCK_MASK) == mods


@dataclass(frozen=True, slots=True)
class Mouse:
    """``kind``: press, release, drag (motion with a button held), move, wheel_up,
    wheel_down, wheel_left, wheel_right. ``button``: 0 left, 1 middle, 2 right; 3 when the
    report carries no button (an X10 release). ``x``/``y`` are zero-based screen cells."""
    kind: str
    button: int
    x: int
    y: int
    mods: int = 0


@dataclass(frozen=True, slots=True)
class Paste:
    text: str


@dataclass(frozen=True, slots=True)
class Focus:
    gained: bool


_PASTE_START, _PASTE_END = "\x1b[200~", "\x1b[201~"
_SGR_MOUSE = re.compile(r"^\x1b\[<(\d+);(\d+);(\d+)([Mm])$")
_KITTY = re.compile(r"^\x1b\[(\d+)(?::(\d*))?(?::(\d+))?(?:;(\d*)(?::(\d+))?)?(?:;([\d:]+))?u$")
_MODIFY_OTHER = re.compile(r"^\x1b\[27;(\d+);(\d+)~$")
_XTERM_LETTER = re.compile(r"^\x1b\[1;(\d+)(?::(\d+))?([A-Z])$")
_XTERM_TILDE = re.compile(r"^\x1b\[(\d+);(\d+)(?::(\d+))?~$")

_LEGACY = {
    "\x1b[A": "up", "\x1bOA": "up", "\x1b[B": "down", "\x1bOB": "down",
    "\x1b[C": "right", "\x1bOC": "right", "\x1b[D": "left", "\x1bOD": "left",
    "\x1b[H": "home", "\x1bOH": "home", "\x1b[1~": "home", "\x1b[7~": "home",
    "\x1b[F": "end", "\x1bOF": "end", "\x1b[4~": "end", "\x1b[8~": "end",
    "\x1b[5~": "pageup", "\x1b[6~": "pagedown", "\x1b[2~": "insert", "\x1b[3~": "delete",
    "\x1bOM": "enter",
    "\x1bOP": "f1", "\x1b[11~": "f1", "\x1bOQ": "f2", "\x1b[12~": "f2",
    "\x1bOR": "f3", "\x1b[13~": "f3", "\x1bOS": "f4", "\x1b[14~": "f4",
    "\x1b[15~": "f5", "\x1b[17~": "f6", "\x1b[18~": "f7", "\x1b[19~": "f8",
    "\x1b[20~": "f9", "\x1b[21~": "f10", "\x1b[23~": "f11", "\x1b[24~": "f12",
}
_SS3_KEYPAD = {"p": "0", "q": "1", "r": "2", "s": "3", "t": "4", "u": "5", "v": "6",
               "w": "7", "x": "8", "y": "9", "n": ".", "l": ",", "m": "-", "k": "+",
               "j": "*", "o": "/"}
_XTERM_LETTERS = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end",
                  "P": "f1", "Q": "f2", "R": "f3", "S": "f4"}
_XTERM_TILDES = {"2": "insert", "3": "delete", "5": "pageup", "6": "pagedown", "15": "f5",
                 "17": "f6", "18": "f7", "19": "f8", "20": "f9", "21": "f10", "23": "f11",
                 "24": "f12"}
_KITTY_NAMED = {8: "backspace", 127: "backspace", 9: "tab", 13: "enter", 57414: "enter", 27: "esc",
                57417: "left", 57418: "right", 57419: "up", 57420: "down", 57421: "pageup",
                57422: "pagedown", 57423: "home", 57424: "end", 57425: "insert", 57426: "delete",
                57427: "kpbegin", 57358: "capslock", 57359: "scrolllock", 57360: "numlock",
                57361: "printscreen", 57362: "pause", 57363: "menu"}
_KITTY_KEYPAD = {57399: "0", 57400: "1", 57401: "2", 57402: "3", 57403: "4", 57404: "5",
                 57405: "6", 57406: "7", 57407: "8", 57408: "9", 57409: ".", 57410: "/",
                 57411: "*", 57412: "-", 57413: "+", 57415: "=", 57416: ","}
_EVENT_KINDS = {"1": "press", "2": "repeat", "3": "release"}
_CTRL_PUNCT = {0: " ", 27: "[", 28: "\\", 29: "]", 30: "^", 31: "_"}


def escape_timeout():
    """A lone ESC's disambiguation window; ssh adds enough latency to need a wider one."""
    return SSH_ESCAPE_TIMEOUT if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY") else ESCAPE_TIMEOUT


# ── One sequence -> one event ─────────────────────────────────────────────────────────

def _mods_from_kitty(value):
    return value - 1 if value >= 1 else 0


def _kitty_code(codepoint):
    if codepoint in _KITTY_NAMED:
        return _KITTY_NAMED[codepoint]
    if codepoint in _KITTY_KEYPAD:
        return _KITTY_KEYPAD[codepoint]
    if 57376 <= codepoint <= 57398:
        return f"f{codepoint - 57376 + 13}"
    if 57428 <= codepoint <= 57440:
        return "media"
    if 57441 <= codepoint <= 57454:
        return "modifier"
    if 57358 <= codepoint <= 57454:
        return None
    try:
        return chr(codepoint)
    except ValueError:
        return None


def _parse_kitty(seq):
    match = _KITTY.match(seq)
    if not match:
        return None
    codepoint = int(match.group(1))
    code = _kitty_code(codepoint)
    if code is None:
        return None
    shifted = int(match.group(2)) if match.group(2) else None
    mods = _mods_from_kitty(int(match.group(4))) if match.group(4) else 0
    kind = _EVENT_KINDS.get(match.group(5) or "1")
    if kind is None:
        return None
    text = None
    if match.group(6):
        try:
            text = "".join(chr(int(part)) for part in match.group(6).split(":"))
        except ValueError:
            return None
        if any(ch.isspace() is False and ord(ch) < 32 for ch in text):
            return None
    if len(code) == 1 and shifted is not None and shifted != codepoint:
        mods |= SHIFT           # kitty permits the shifted alternate only while Shift is down
    return Key(code, mods, kind, shifted, text)


def _parse_modify_other(seq):
    match = _MODIFY_OTHER.match(seq)
    if not match:
        return None
    code = _kitty_code(int(match.group(2)))
    return Key(code, _mods_from_kitty(int(match.group(1)))) if code else None


def _parse_xterm_modified(seq):
    match = _XTERM_LETTER.match(seq)
    if match and match.group(3) in _XTERM_LETTERS:
        kind = _EVENT_KINDS.get(match.group(2) or "1")
        return Key(_XTERM_LETTERS[match.group(3)], _mods_from_kitty(int(match.group(1))), kind) if kind else None
    match = _XTERM_TILDE.match(seq)
    if match and match.group(1) in _XTERM_TILDES:
        kind = _EVENT_KINDS.get(match.group(3) or "1")
        return Key(_XTERM_TILDES[match.group(1)], _mods_from_kitty(int(match.group(2))), kind) if kind else None
    return None


def _parse_ctrl_char(ch):
    value = ord(ch)
    if 1 <= value <= 26:
        return Key(chr(value + 96), CTRL)
    if value in _CTRL_PUNCT:
        return Key(_CTRL_PUNCT[value], CTRL)
    return None


def parse_key(seq):
    """One complete input sequence (str) to a Key, or None when it is not a key."""
    if seq in _LEGACY:
        return Key(_LEGACY[seq])
    if seq == "\x1b[Z":
        return Key("backtab", SHIFT)
    if len(seq) == 3 and seq.startswith("\x1bO") and seq[2] in _SS3_KEYPAD:
        return Key(_SS3_KEYPAD[seq[2]])
    for parser in (_parse_kitty, _parse_modify_other, _parse_xterm_modified):
        key = parser(seq)
        if key is not None:
            return key
    if seq == "\r":
        return Key("enter")
    if seq == "\t":
        return Key("tab")
    if seq == "\x1b":
        return Key("esc")
    if seq == "\x7f":
        return Key("backspace")
    if seq == "\x1b\x7f":
        return Key("backspace", ALT)
    if seq.startswith("\x1b") and len(seq) >= 2:
        inner = parse_key(seq[1:])          # Alt + key: ESC prefix (\x1b\x1b[A is Alt+Up)
        if inner is None or len(seq) > 2 and not seq[1:].startswith("\x1b["):
            return None
        return Key(inner.code, inner.mods | ALT, inner.kind, inner.shifted)
    if len(seq) == 1:
        ctrl = _parse_ctrl_char(seq)
        if ctrl is not None:
            return ctrl
        return Key(seq, SHIFT if seq.isupper() else 0)
    return None


def parse_mouse(seq):
    """An SGR (1006) or X10 mouse report to a Mouse, or None."""
    match = _SGR_MOUSE.match(seq)
    if match:
        raw, x, y, suffix = int(match.group(1)), int(match.group(2)) - 1, int(match.group(3)) - 1, match.group(4)
        release = suffix == "m"
    elif len(seq) == 6 and seq.startswith("\x1b[M"):
        raw = ord(seq[3]) - 32
        x, y = ord(seq[4]) - 33, ord(seq[5]) - 33
        release = raw & 3 == 3 and not raw & 64
    else:
        return None
    mods = (SHIFT if raw & 4 else 0) | (ALT if raw & 8 else 0) | (CTRL if raw & 16 else 0)
    button = raw & 3
    if raw & 64:
        kind = ("wheel_up", "wheel_down", "wheel_left", "wheel_right")[button]
        return Mouse(kind, button, x, y, mods)
    if raw & 32:
        return Mouse("move" if button == 3 else "drag", button, x, y, mods)
    return Mouse("release" if release else "press", button, x, y, mods)


def parse_sequence(seq):
    """One complete sequence to its event (Key, Mouse, Focus) or None for noise."""
    if seq == "\x1b[I":
        return Focus(True)
    if seq == "\x1b[O":
        return Focus(False)
    return parse_mouse(seq) or parse_key(seq)


# ── Byte stream -> complete sequences ─────────────────────────────────────────────────

def _complete(data):
    """'complete' | 'incomplete' for a buffer starting with ESC (crossterm's split rules)."""
    if len(data) == 1:
        return "incomplete"
    rest = data[1:]
    if rest.startswith("["):
        if rest.startswith("[M"):
            return "complete" if len(data) >= 6 else "incomplete"
        if len(data) < 3:
            return "incomplete"
        final = data[-1]
        if not 0x40 <= ord(final) <= 0x7E:
            return "incomplete"
        if rest.startswith("[<") and final not in "Mm":
            return "incomplete"          # an SGR mouse report ends only in M/m
        return "complete"
    if rest.startswith("]"):
        return "complete" if data.endswith(("\x1b\\", "\x07")) else "incomplete"
    if rest.startswith(("P", "_")):
        return "complete" if data.endswith("\x1b\\") else "incomplete"
    if rest.startswith("O"):
        return "complete" if len(rest) >= 2 else "incomplete"
    return "complete"                    # ESC + one character: Alt+key


def split_sequences(buffer):
    """Split decoded input into complete sequences; returns (sequences, remainder)."""
    out, pos = [], 0
    while pos < len(buffer):
        if buffer[pos] != "\x1b":
            out.append(buffer[pos])
            pos += 1
            continue
        end = pos + 1
        while end <= len(buffer):
            candidate = buffer[pos:end]
            if _complete(candidate) == "complete":
                if candidate == "\x1b\x1b" and end < len(buffer) and buffer[end] in "[]OP_":
                    out.append("\x1b")           # ESC, then a sequence: two events
                    pos += 1
                else:
                    out.append(candidate)
                    pos = end
                break
            end += 1
        else:
            return out, buffer[pos:]
    return out, ""


class HostInput:
    """Turns stdin chunks into events. A partial sequence waits for more bytes until
    ``deadline()`` passes, then ``flush()`` releases it as is (a lone ESC becomes the
    Escape key). Bracketed paste is collected across chunks into one ``Paste``."""

    def __init__(self):
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._pending = ""
        self._pending_since = None
        self._paste = None               # text collected since the paste opener, or None

    def deadline(self):
        """When ``flush`` should run for the buffered partial sequence, or None."""
        if not self._pending or self._pending_since is None:
            return None
        window = escape_timeout() if self._pending == "\x1b" else SEQUENCE_TIMEOUT
        return self._pending_since + window

    def feed(self, data, now=None):
        if len(data) == 1 and data[0] > 127 and not self._pending:
            text = "\x1b" + chr(data[0] - 128)   # 8-bit meta: Alt+key as one high byte
        else:
            text = self._decoder.decode(bytes(data))
        return self._consume(self._pending + text, now if now is not None else time.monotonic())

    def flush(self):
        """Release whatever is buffered; called when ``deadline`` has passed."""
        pending, self._pending, self._pending_since = self._pending, "", None
        events = []
        if pending:
            events.extend(self._events_for([pending]))
        return events

    def _consume(self, buffer, now):
        events = []
        while True:
            if self._paste is not None:
                cut = buffer.find(_PASTE_END)
                if cut < 0:
                    self._paste += buffer
                    self._pending, self._pending_since = "", None
                    return events
                events.append(Paste(self._paste + buffer[:cut]))
                self._paste, buffer = None, buffer[cut + len(_PASTE_END):]
                continue
            start = buffer.find(_PASTE_START)
            if start >= 0:
                head, remainder = split_sequences(buffer[:start])
                events.extend(self._events_for(head + ([remainder] if remainder else [])))
                self._paste, buffer = "", buffer[start + len(_PASTE_START):]
                continue
            sequences, remainder = split_sequences(buffer)
            events.extend(self._events_for(sequences))
            if remainder != self._pending:          # progress on a partial sequence restarts its window
                self._pending_since = now if remainder else None
            self._pending = remainder
            return events

    @staticmethod
    def _events_for(sequences):
        return [event for event in map(parse_sequence, sequences) if event is not None]

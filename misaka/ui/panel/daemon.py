"""Misaka Network daemon: the pseudo-terminal host where the Sisters live.

Process model (after herdr): this long-lived process owns every pane (PTY);
the panel and CLI are thin clients, and a disconnect only removes an observer.
The protocol is newline-delimited JSON (requests carry id/method/params).

Card panes: ``pane.run_card`` claims a lease and opens the worker.  The daemon
keeps that lease alive and reconciles the real process exit; the Sister's own
lifecycle hook submits her result.  External ally panes retain their exit
adapter.
"""
import asyncio
import base64
import fcntl
import json
import os
import re
import secrets
import signal
import socket
import struct
import subprocess
import sys
import termios
import time

import psutil

from misaka.config import CFG, home
from misaka.config import sessions as session_roots
from misaka.ui.panel import (
    geometry as hui,  # layout.rs port: split_at / remove_pane / pane_ids
)
from misaka.ui.panel import ghostty as vt
from misaka.utils.streams import STREAM_LIMIT

# Wire protocol version (strict equality, as in herdr). Bump it whenever *server
# behaviour* changes, not only method/event shapes: an unbumped behaviour change
# once let a stale daemon slip through the version gate.
PROTOCOL = 50   # 50: one home, one layout table -- a daemon from before it serves the old paths; 49: panes.list says which pane holds a card's claim, and a card is continued in the pane it already has; 48: pane.send/pane.input drain through a per-pane write queue and wait for delivery; 47: drag resizes are fire-and-forget (id=None, no reply) and the layout is persisted at drag end, so the divider never blocks on a busy daemon; 46: a drag resizes the program as fast as it repaints; 44: reflow held until repaint; 43: libghostty-vt; 42: panes.resize acknowledged first; 40: shown frame in a sync block; 38: input state, extract, kitty
RING_CAP = 256 * 1024          # output tail kept per pane
SEND_TIMEOUT = 8.0             # pane.send: how long a message may take to enter the pane before it counts as undelivered (under the client's 10 s request timeout)
INPUT_TIMEOUT = 1.0            # pane.input: keystrokes and pastes wait this long, then stay queued (typing must not block the panel)
WRITE_QUEUE_LIMIT = 1024 * 1024  # bytes queued per pane before further input is refused outright
FRAME_SECONDS = 0.008          # coalescing window for dirty-row broadcasts (~120 fps)
IDLE_QUIET_SECONDS = 1.0       # screen unchanged this long = idle (a static spinner glyph does not count)
RESIZE_HOLD_SECONDS = 1.5      # after a resize, keep the last complete frame until the program repaints (fallback ceiling)
RESIZE_MIN_INTERVAL = 0.016    # a drag resizes the program no faster than herdr's 16 ms render pace (MIN_RENDER_INTERVAL)
CARD_POLL_SECONDS = 5.0        # card-pane polling interval
DEFAULT_ROWS, DEFAULT_COLS = 32, 120
EXIT_GRACE_TRIES, EXIT_POLL_SECONDS = 40, 0.05   # 2 s for a program to save state, then SIGKILL
CARD_SHELL = [sys.executable, "-m", "misaka", "card-shell"]   # tests may override
SINGLETON_LOCK_TRIES = 100     # x 50 ms: how long a cold start waits for another one's probe+bind


def _expand(path):
    return os.path.expanduser(path)


def _write_some(fd, view):
    """One non-blocking write; 0 when the terminal's input queue is full right now."""
    try:
        return os.write(fd, view)
    except BlockingIOError:
        return 0


class _PendingWrite:
    """One message on a pane's write queue: what is left of it and who waits for it.

    A terminal's input queue holds about 1 KiB on macOS (1022 bytes measured), so any
    message longer than that -- a paste of 340 CJK characters, a Last Order note to a
    Sister -- only enters the pane as fast as the program reads. herdr hands input to a
    per-pane writer thread that blocks on the pty (``pty/actor.rs``: bounded channel,
    ``write_all``), so nothing is ever cut; the daemon has one event loop for every pane,
    so it drains on writability instead (``Daemon._drain_writes``). The earlier round that
    queued and answered ``{"sent": true}`` at once lost the failure signal (audit
    2026-09-02, ui-panel-01/10); ``Daemon._deliver`` therefore waits for the message to
    enter the pane, with a deadline, and reports what actually landed."""

    __slots__ = ("future", "total", "view")

    def __init__(self, data, future):
        self.view = memoryview(data)
        self.total = len(data)
        self.future = future

    @property
    def landed(self):
        return self.total - len(self.view)


# Synchronized output (DEC 2026): a program's repaint between `h` and `l` is one frame.
# ghostty holds the screen for herdr until the block closes; here the broadcast is held
# instead, so the panel never sees a half-drawn transcript (pi's TUI wraps every render in
# this pair). A block is abandoned once the program has been silent for SYNC_TIMEOUT,
# measured from the last byte: a long transcript's repaint arrives in many chunks.
SYNC_TIMEOUT = 1.0


def _colour_sgr(colour, base):
    """A ghostty colour (palette index or rgb) as SGR parameters for fg (30) or bg (40)."""
    if isinstance(colour, tuple):
        return f"{base + 8};2;{colour[0]};{colour[1]};{colour[2]}"
    if colour < 8:
        return str(base + colour)
    if colour < 16:
        return str(base + 60 + colour - 8)
    return f"{base + 8};5;{colour}"


def _sgr(style):
    """The SGR sequence that sets a cell style from scratch."""
    fg, bg, bold, faint, italic, underline, blink, inverse, strikethrough = style
    parts = ["0"]
    for on, code in ((bold, "1"), (faint, "2"), (italic, "3"), (underline, "4"),
                     (blink, "5"), (inverse, "7"), (strikethrough, "9")):
        if on:
            parts.append(code)
    if fg is not None:
        parts.append(_colour_sgr(fg, 30))
    if bg is not None:
        parts.append(_colour_sgr(bg, 40))
    return "\x1b[" + ";".join(parts) + "m"


def _render_cells(cells):
    """One viewport row as an ANSI string (SGR only on a style change, reset at the end).
    A wide character is written once and its tail cell skipped; a spacer head reads as a
    blank (herdr ``ghostty_blank_symbol_for_width``). Every attribute ghostty tracks
    reaches the host: bold, faint, italic, underline, blink, inverse, strikethrough."""
    out, last = [], None
    for text, width, style in cells:
        if width == 0:
            continue
        if style != last:
            out.append(_sgr(style))
            last = style
        out.append(text)
    out.append("\x1b[0m")
    return "".join(out)


def _screen_lines(pane):
    """The rows of the active area as text (herdr's detection text: what a shell or agent
    shows at the bottom, whatever the viewer has scrolled to)."""
    return pane.term.recent_text(pane.term.rows).split("\n")


# Spinner frames: braille and geometric dots only. Never add ASCII such as |/-\ --
# the / in paths and the ubiquitous - would make every screen look busy.
_SPINNER_CHARS = set("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⣻⢿⡿⣟⣯⣷◐◓◑◒◴◷◶◵")
_SHELLS = {"sh", "bash", "zsh", "fish", "dash", "ksh", "csh", "tcsh", "-zsh", "-bash"}
# A command typed by hand in a pane's shell counts as a transient ally only if it
# is on the allow-list; otherwise vim/htop would show up in the roster too. The list is
# ``"allies"`` in the global settings.json (no env knob, by user decision), read fresh on
# each ask so an edit takes effect without a daemon restart. Allies launched by Last
# Order are not subject to the list.
ALLY_SEED = ("claude", "codex")


def ally_commands():
    """The hand-launched ally allow-list: ``settings.json``'s ``allies``, else the seed."""
    from misaka.core.settings_manager import SettingsManager
    try:
        listed = SettingsManager.forRole(None).settings.get("allies")
    except Exception:  # noqa: BLE001 - an unreadable settings file must not block panes
        return frozenset(ALLY_SEED)
    if not isinstance(listed, list):
        return frozenset(ALLY_SEED)
    return frozenset(c.strip() for c in listed if isinstance(c, str) and c.strip())


def _looks_like_command(name):
    """Is this a command name? Pure digits/version strings ("2.1.228") are not; apps set those as their process name."""
    return bool(name) and not re.fullmatch(r"[\d.]+", name)


FOREGROUND_TTL = 0.5           # how long a pane's foreground reading stays good


def _foreground(pane):
    """Return what is running in the pane's foreground right now (name and command line).

    Last Order uses this to notice that the user started e.g. codex in a shell.
    No vendor list here: report the process name and let Last Order decide
    whether it is an agent.

    Cached per pane for FOREGROUND_TTL. Every `panes.list` asked twice per pane (once
    through `_ally_name`), each ask four psutil calls -- `cwd()` is a proc_pidinfo syscall
    on macOS -- and the panel calls `panes.list` on its poll, all on the daemon's single
    event loop (audit 2026-09-02, ui-panel-08). The reading is a sample of something that
    changes on its own anyway; half a second of it is not less true.
    """
    now = time.monotonic()
    if pane.fg_at is not None and now - pane.fg_at < FOREGROUND_TTL:
        return pane.fg_seen
    pane.fg_at, pane.fg_seen = now, None
    if not pane.alive() or pane.fd is None:
        return None
    try:
        fg = os.tcgetpgrp(pane.fd)
        if fg <= 0:
            return None
        proc = psutil.Process(fg)
        name = proc.name()
        try:
            argv = proc.cmdline()
        except psutil.Error:
            argv = []
        # The process name is not always the command name: Node apps like claude
        # set it to their version ("2.1.228"), which is meaningless as a label.
        # The first argv element is the name a human recognises.
        shown = name
        if argv and not _looks_like_command(name):
            shown = os.path.basename(argv[0])
            if shown in ("node", "python", "python3", "bun", "deno") and len(argv) > 1:
                shown = os.path.basename(argv[1]) or shown   # interpreter + script: use the script name
        try:
            cwd = proc.cwd()               # herdr foreground_cwd: where the foreground job really is
        except psutil.Error:
            cwd = None
        pane.fg_seen = {"name": shown, "proc_name": name, "pid": fg, "cwd": cwd,
                        "cmdline": " ".join(argv)[:200] if argv else name,
                        "is_shell": shown.lstrip("-") in _SHELLS or name.lstrip("-") in _SHELLS}
        return pane.fg_seen
    except (OSError, psutil.Error):
        return None


def _ally_name(pane):
    """Name of the third-party agent running in this pane, or None -- the test for a transient ally.

    Two sources: an ally launched by Last Order (``pane.ally`` is set; any
    command qualifies), or a command the user typed into a MISAKA pane's shell
    (only names on the ``allies`` allow-list, see ``ally_commands()``).
    Excluded: Sister card panes and MISAKA's own chat/card-shell sessions.
    """
    if pane.ally:
        return pane.ally
    if pane.card or not pane.alive():
        return None
    if "misaka" in " ".join(pane.argv or []):   # MISAKA's own sessions are not allies
        return None
    fg = _foreground(pane)
    if not fg or fg["is_shell"]:
        return None
    return fg["name"] if fg["name"] in ally_commands() else None


# herdr's agent-state detection for third-party agents, core rules from
# website/agent-detection/codex.toml and claude.toml. The OSC title is the strongest signal in
# both manifests (priority 1050-1100) because it is the agent's own announcement; the screen
# rules below it catch the prompts that are waiting for a person.
_ALLY_TITLE_BLOCKED = ("action required",)                 # codex osc_title_blocked (1100)
_ALLY_BLOCKERS = (                                         # codex live_strong_blocker/weak (900, 600)
    "press enter to confirm or esc to cancel", "enter to submit answer",
    "enter to submit all", "allow command?", "[y/n]", "yes (y)",
    "do you want to proceed?", "waiting for permission",   # claude 850-300
    "do you want to allow this connection?", "tab to amend", "ctrl+e to explain")
_ALLY_WEAK_BLOCKERS = ("do you want to", "would you like to")   # only when a yes/pointer is offered
_ALLY_WORKING_LINE = re.compile(r"^[•◦]\s+working \([^)]*esc to interrupt\)", re.MULTILINE)   # codex (500)
_ALLY_TAIL_ROWS = 8      # herdr reads its bottom_non_empty_lines(3..5) region; a few rows more here


def _ally_verdict(pane):
    """``(state, why)`` for the third-party agent in this pane: "working", "blocked", "idle",
    or "unknown" when nothing matches (herdr shows unknown rather than guessing), plus the rule
    that decided it -- ``pane.explain`` prints the reason.

    The shell heuristic in ``_pane_busy`` cannot speak for these panes: to it, codex sitting at
    its prompt is simply "a foreground process that is not the shell", i.e. permanently busy.
    """
    title = pane.term.title().strip()
    low = title.lower()
    if any(sign in low for sign in _ALLY_TITLE_BLOCKED):
        return "blocked", f"its terminal title asks for you ({title!r})"
    if any(ch in _SPINNER_CHARS for ch in title):
        return "working", f"a spinner is running in its terminal title ({title!r})"
    try:                     # herdr's bottom_non_empty_lines region: trailing blank rows say nothing
        lines = [row.strip() for row in _screen_lines(pane) if row.strip()]
        tail = "\n".join(lines[-_ALLY_TAIL_ROWS:]).lower()
    except Exception:  # noqa: BLE001 - a screen we cannot read says nothing
        tail = ""
    hit = next((sign for sign in _ALLY_BLOCKERS if sign in tail), None)
    if hit is None and any(sign in tail for sign in _ALLY_WEAK_BLOCKERS) and ("yes" in tail or "❯" in tail):
        hit = next(sign for sign in _ALLY_WEAK_BLOCKERS if sign in tail)
    if hit is not None:
        return "blocked", f"its screen is waiting on {hit!r}"
    if _ALLY_WORKING_LINE.search(tail):
        return "working", "its screen shows the working line"
    if title:                                  # a title with no spinner in it: waiting for you
        return "idle", f"its terminal title has no spinner in it ({title!r})"
    return "unknown", "it sets no terminal title and its screen says nothing"


def _ally_state(pane):
    return _ally_verdict(pane)[0]


def _pane_busy(pane, ally_state=None):
    """Is the pane actually busy right now? (herdr's agent-state detection, split by pane type.)

    Ally panes (codex, claude): whatever ``_ally_state`` read off the pane -- the shell rule
    below would call them busy forever.
    Shell panes: busy when the PTY foreground process is not the shell itself
    (comparing PIDs is not enough -- ``sh -c cmd`` execs over itself).
    Agent panes: busy when the screen shows a spinner frame *and* is still
    changing (the engine's spinner is braille, see tui/loader.py).
    """
    if not pane.alive():
        return False
    if ally_state is not None:
        return ally_state == "working"
    own = os.path.basename(pane.argv[0]) if pane.argv else ""
    if own.lstrip("-") in _SHELLS:
        try:
            fg = os.tcgetpgrp(pane.fd) if pane.fd is not None else -1
            if fg <= 0:
                return False
            name = psutil.Process(fg).name()
            return bool(name) and name.lstrip("-") not in _SHELLS
        except (OSError, psutil.Error):
            return False
    # Agent pane: a spinner frame counts only while the screen is still moving.
    # A static glyph is decoration and must not keep the busy light blinking.
    if time.time() - pane.last_output > IDLE_QUIET_SECONDS:
        return False
    try:
        for line in _screen_lines(pane):
            if any(ch in _SPINNER_CHARS for ch in line):
                return True
    except RuntimeError:                       # a screen the emulator cannot read says nothing
        pass
    return False


def _busy_reason(pane):
    """Explain why a pane is classified as busy or idle."""
    if not pane.alive():
        return "Pane process has exited."
    ally = _ally_name(pane)
    if ally:
        state, why = _ally_verdict(pane)
        return f"Ally pane ({ally}): {state}, because {why}."
    own = os.path.basename(pane.argv[0]) if pane.argv else ""
    if own.lstrip("-") in _SHELLS:
        try:
            fg = os.tcgetpgrp(pane.fd) if pane.fd is not None else -1
            name = psutil.Process(fg).name() if fg > 0 else "?"
        except (OSError, psutil.Error):
            return f"Shell pane ({own}): foreground process is unavailable; classified as idle."
        if name.lstrip("-") in _SHELLS:
            return f"Shell pane ({own}): foreground process is the shell itself ({name}); waiting for input."
        return f"Shell pane ({own}): foreground process is {name}; classified as busy."
    quiet = time.time() - pane.last_output
    if quiet > IDLE_QUIET_SECONDS:
        return f"Agent pane: no output for {quiet:.1f}s (threshold {IDLE_QUIET_SECONDS}s); classified as idle."
    spins = sorted({ch for line in _screen_lines(pane) for ch in line
                    if ch in _SPINNER_CHARS})
    if spins:
        return f"Agent pane: active spinner {''.join(spins)} detected; classified as busy."
    return f"Agent pane: last output was {quiet:.1f}s ago with no spinner; classified as idle."


def input_state(pane):
    """What the program in the pane asked its terminal for: the modes that decide how the
    panel must encode keys, mouse, paste and focus for it (herdr ``InputState``)."""
    term = pane.term
    if term.mode(vt.MODE_SGR_PIXELS_MOUSE):
        encoding = "sgr_pixels"
    elif term.mode(vt.MODE_SGR_MOUSE):
        encoding = "sgr"
    elif term.mode(vt.MODE_UTF8_MOUSE):
        encoding = "utf8"
    else:
        encoding = "default"
    return {
        "alternate_screen": term.alternate_screen(),
        "application_cursor": term.mode(vt.MODE_DECCKM),
        "bracketed_paste": term.mode(vt.MODE_BRACKETED_PASTE),
        "focus_reporting": term.mode(vt.MODE_FOCUS_EVENT),
        "mouse_mode": term.mouse_tracking(),
        "mouse_encoding": encoding,
        "mouse_alternate_scroll": term.mode(vt.MODE_ALT_SCROLL),
        "modify_other_keys": 2 if term.modify_other_keys() else 0,
        "kitty_flags": term.kitty_flags(),
    }


def extract_text(pane, start, end):
    """The text between two absolute ``(row, col)`` points, both inclusive, the way herdr's
    ``extract_selection`` reads it (``read_text_screen``: rows count from the top of the
    scrollback, soft wraps joined, trailing blanks dropped). Points past the screen read as
    empty."""
    (start_row, start_col), (end_row, end_col) = start, end
    if (end_row, end_col) < (start_row, start_col):
        (start_row, start_col), (end_row, end_col) = (end_row, end_col), (start_row, start_col)
    term = pane.term
    last_row, last_col = term.total_rows() - 1, term.cols - 1
    if last_row < 0 or start_row > last_row:
        return ""
    clamp = lambda value, high: max(0, min(int(value), high))
    return term.read_text((clamp(start_col, last_col), clamp(start_row, last_row)),
                          (clamp(end_col, last_col), clamp(end_row, last_row)))


def _scroll_metrics(pane):
    """Scrollback position, total scrollable lines, and viewport rows (herdr's ScrollMetrics)."""
    return pane.term.scroll_metrics()


def _scroll_pane(pane, delta=0, to=None):
    """Move the viewport by whole rows: negative ``delta`` goes back into history, "bottom"
    returns to the live screen. On the alternate screen ghostty keeps the viewport live."""
    if to == "bottom":
        pane.term.scroll_to_bottom()
    else:
        pane.term.scroll(int(delta))
    pane.sent_cursor = None             # visibility changes with the offset; resend it


def _rows_for(pane):
    """Every viewport row rendered (and marked clean: the caller is showing them)."""
    pane.render.update(pane.term)
    return [_render_cells(cells) for _index, cells in pane.render.rows(all_rows=True)]


def _display(pane, rows):
    """The pane as a viewer sees it right now: rows, cursor, metrics, modes."""
    x, y, visible = pane.render.cursor()
    return {"rows": rows,
            "cursor": [x, y],
            "cursor_hidden": not visible,
            "size": [pane.term.rows, pane.term.cols],
            "scroll": pane.term.scroll_metrics(),
            "alt_screen": pane.term.alternate_screen(),
            "input": input_state(pane)}


def _shown(pane, rendered=None):
    """Remember what the panel is showing (herdr's renderer holds its last frame while a
    synchronized block is open; the daemon answers ``pane.screen`` from this copy then).
    ``rendered`` maps the rows just sent; None means every row was."""
    shown = pane.shown
    if rendered is None or shown is None or len(shown["rows"]) != pane.term.rows:
        rows = _rows_for(pane)
    else:
        rows = list(shown["rows"])
        for row, line in rendered.items():
            if row < len(rows):
                rows[row] = line
    pane.shown = _display(pane, rows)
    return pane.shown


def _seated_pane_ids(spaces):
    """Every pane id a layout seats."""
    out = set()
    for space in spaces:
        for tab in space.get("tabs") or []:
            tree = hui.from_jsonable(tab["tree"]) if tab.get("tree") else None
            out.update(hui.pane_ids(tree) if tree else [])
    return out


class Pane:
    __slots__ = (
        "ally",
        "argv",
        "buf",
        "card",
        "claim_lock",
        "cwd",
        "exit_code",
        "fd",
        "fg_at",
        "fg_seen",
        "flush",
        "full_frame",
        "generation",
        "id",
        "last_heartbeat",
        "last_output",
        "proc",
        "render",
        "reported",
        "resize_applied_at",
        "resize_apply",
        "resize_hold",
        "resize_target",
        "seen_status",
        "sent_cursor",
        "sent_input",
        "shown",
        "started",
        "started_at",
        "sync_until",
        "term",
        "theme",
        "title",
        "writer_armed",
        "writes",
    )

    def __init__(self, pane_id, title, argv, cwd, card=None):
        self.id, self.title, self.argv, self.cwd, self.card = pane_id, title, argv, cwd, card
        self.reported = None          # the session's own word: {"state", "message", "seq"} (herdr hook authority); None = guess from the screen
        self.claim_lock = self.generation = None
        self.proc = self.fd = self.exit_code = None
        self.buf = bytearray()
        self.writes = []              # _PendingWrite queue, drained in order as the program reads (herdr's writer channel)
        self.writer_armed = False     # loop.add_writer(fd) is registered while the queue is non-empty and the pty is full
        self.started = None           # card-hosting start time (set by _host_card; None for plain panes)
        self.started_at = int(time.time())
        self.seen_status = None       # board status seen while focused ("finished but not yet looked at")
        # The pane's terminal is ghostty's (as herdr's): it parses, keeps the scrollback,
        # reflows on resize, answers queries, and tracks every mode the panel encodes for.
        self.term = vt.Terminal(DEFAULT_COLS, DEFAULT_ROWS, on_write_pty=self.reply)
        self.render = vt.RenderState()
        self.full_frame = True        # the next frame carries every row (first sight, resize, a client that fell behind)
        self.theme = "dark"           # theme variant (set from env at create; OSC 10/11 answers follow it)
        self.set_theme(self.theme)
        self.ally = None              # ally label (only for third-party agent panes started by Last Order)
        self.flush = None             # pending frame-coalescing timer
        self.sent_cursor = None       # last cursor broadcast (position + visibility)
        self.sent_input = None        # last input state broadcast (a mode change alone is a frame)
        self.sync_until = None        # inside a synchronized-output block: frames are held until this deadline
        self.shown = None             # the display as the panel last received it (rows, cursor, metrics)
        self.resize_target = None     # (rows, cols) the client wants; driven to the program as fast as it repaints
        self.resize_apply = None      # the pending _apply_resize timer (paces resizes at the render rate)
        self.resize_applied_at = 0.0  # loop time of the last applied resize (render-pace throttle)
        self.resize_hold = None       # after a resize: hold the last complete frame until the program repaints
        self.fg_at = None             # monotonic stamp of the cached foreground reading
        self.fg_seen = None           # cached _foreground() result
        self.last_output = 0.0        # time of the last output (is the screen still moving?)
        self.last_heartbeat = 0.0     # last lease heartbeat for a card pane

    def reply(self, data):
        """What the terminal answers the program (device attributes, cursor and size
        reports, the kitty keyboard query, OSC 10/11 colours) goes back down the pty."""
        if self.fd is not None:
            try:
                os.write(self.fd, data)
            except OSError:
                pass

    def set_theme(self, theme):
        """OSC 10/11 answers follow the pane's theme variant, so a full-screen app in a
        light terminal sees a light background."""
        self.theme = theme
        if theme == "light":
            self.term.set_colors((0x33, 0x20, 0x28), (0xFA, 0xF4, 0xF6))
        else:
            self.term.set_colors((0xE6, 0xE6, 0xE6), (0x1E, 0x1E, 0x1E))

    def size(self):
        return (self.term.rows, self.term.cols)

    def resize(self, rows, cols):
        """ghostty reflows the primary screen and keeps the offset from the bottom (herdr
        ``TerminalRuntime::resize``); every row is sent with the next frame."""
        self.term.resize(cols, rows)
        self.full_frame = True
        self.sent_cursor = None

    def close_terminal(self):
        self.render.close()
        self.term.close()

    def fresh_terminal(self):
        """A clean emulator at the current size, for a program replacing another in this pane:
        the modes, scrollback and alternate screen of the program that left are not inherited."""
        rows, cols = self.size()
        self.close_terminal()
        self.term = vt.Terminal(cols, rows, on_write_pty=self.reply)
        self.render = vt.RenderState()
        self.set_theme(self.theme)
        self.full_frame = True
        self.sent_cursor = self.sent_input = self.shown = None

    def alive(self):
        return self.proc is not None and self.proc.poll() is None


class Daemon:
    def __init__(self, sock_path=None, snapshot_path=None):
        self.sock_path = _expand(sock_path or CFG["net_sock"])
        self.snapshot_path = _expand(snapshot_path or CFG["net_snapshot"])
        self.panes: dict[str, Pane] = {}
        self._card_locks: dict[str, asyncio.Lock] = {}   # one attempt at a time per card
        self._reapers = set()          # background kill-escalation tasks (close never blocks)
        self._read_limit = STREAM_LIMIT   # request-reader limit; _read_line frames against it
        self._seq = 0
        # The layout, as in herdr: the server holds spaces -> tabs -> split trees and seats every
        # pane at creation; clients only draw it. {"id","folder","name","tabs":[{"name","tree"}]}
        self.spaces: list[dict] = []
        self._space_seq = 0
        self.layout_revision = 0    # bumps on every layout change; clients edit against it
        self._theme = "dark"        # session theme variant; updated when the panel creates a pane with MISAKA_THEME
        self._con = None            # board connection, opened on the first card run
        self._mcon = None           # mailbox connection, opened on the first panes.list with cards
        self._attached: dict[asyncio.StreamWriter, str] = {}   # subscribers: connection -> pane id
        self._panels: set[asyncio.StreamWriter] = set()        # panels (attach "*"): when the last one leaves, so do we
        self._clients: set[asyncio.StreamWriter] = set()
        self._stopping = asyncio.Event()
        self._socket_identity = None

    # ── Panes ─────────────────────────────────────────────

    def _spawn(self, pane: Pane, env=None):
        import pty

        def _become_session_leader():
            # A proper controlling terminal (as tmux/herdr do); without it ^C/^Z cannot reach the job.
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        master, slave = pty.openpty()
        # Popen fails on a missing argv[0], a cwd that is gone, or a preexec_fn error; without
        # this the pty pair leaked two descriptors per failure and a long-lived daemon walked
        # into EMFILE, after which no pane opened at all (audit 2026-09-02, ui-panel-02).
        try:
            rows, cols = pane.size()      # a fresh pane is still at the default size
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            child_env = {**os.environ, **(env or {}), "TERM": "xterm-256color",
                         "COLORTERM": "truecolor", "MISAKA_NET_PANE": pane.id}
            child_env.pop("MISAKA_DM_CARD_ALLOWLIST", None)  # contact-session capability
            pane.proc = subprocess.Popen(
                pane.argv, cwd=pane.cwd, stdin=slave, stdout=slave, stderr=slave,
                preexec_fn=_become_session_leader,  # noqa: PLW1509 - panes are spawned from the daemon's main thread only; setsid must run in the child
                # The pane's terminal is our ghostty relay, which passes 24-bit SGR through
                # untouched -- COLORTERM keeps the engine from pre-baking 24-bit colours.
                env=child_env,
            )
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        pane.fd = master
        asyncio.get_running_loop().add_reader(master, self._pump, pane)

    @staticmethod
    def _discard_output(pane: Pane, rounds=16):
        """Read and drop what a leaving program writes. Its pane is not listening any more, and
        a pty nobody drains fills up and holds the program open instead of letting it leave."""
        if pane.fd is None:
            return
        for _ in range(rounds):
            try:
                if not os.read(pane.fd, 65536):
                    return
            except OSError:                   # nothing to read, or the pty is gone
                return

    @staticmethod
    def _release_fd(pane: Pane):
        if pane.fd is not None:
            try:
                os.close(pane.fd)
            except OSError:
                pass
            pane.fd = None

    async def _stop_program(self, pane: Pane):
        """End the program running in a pane without taking the pane down.

        The reader is detached first, so the program's exit is not broadcast as the pane itself
        exiting -- but the pty stays open until it is gone. Closing the master would hang up its
        session (`_spawn` makes a pane's program a session leader owning that pty) and SIGHUP it
        before it had been asked to leave, which is exactly the grace period below."""
        loop = asyncio.get_running_loop()
        self._fail_writes(pane, "the program in the pane was replaced")   # unregisters the writer
        for timer in ("flush", "resize_apply"):
            pending = getattr(pane, timer)
            if pending is not None:
                pending.cancel()
                setattr(pane, timer, None)
        if pane.fd is not None:
            try:
                loop.remove_reader(pane.fd)
            except (OSError, ValueError):     # never registered, or already gone
                pass
        proc, pane.proc = pane.proc, None
        try:
            if proc is None or proc.poll() is not None:
                return
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:                   # already gone
                return
            for _ in range(EXIT_GRACE_TRIES):
                if proc.poll() is not None:
                    return
                self._discard_output(pane)
                await asyncio.sleep(EXIT_POLL_SECONDS)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            while proc.poll() is None:
                self._discard_output(pane)
                await asyncio.sleep(EXIT_POLL_SECONDS)
        finally:
            self._release_fd(pane)

    async def _await_exit(self, proc):
        """Wait for a process this daemon has already signalled (`close` escalates in its own
        task). True when it is gone; False when it outlived even the escalation."""
        if proc is None:
            return True
        for _ in range(EXIT_GRACE_TRIES * 3):
            if proc.poll() is not None:
                return True
            await asyncio.sleep(EXIT_POLL_SECONDS)
        return proc.poll() is not None

    async def _replace_program(self, pane: Pane, argv, *, cwd=None, env=None, title=None):
        """Run a different program in an existing pane. The pane keeps its id, its seat in the
        layout and its size, so a card continued after it finished comes back in the window the
        user already has open instead of a second one beside it (2026-09-18, B29/B14)."""
        await self._stop_program(pane)
        pane.argv = list(argv)
        pane.cwd = cwd or pane.cwd
        if title:
            pane.title = title
        del pane.buf[:]                  # the output tail belonged to the program that left
        pane.exit_code = None
        pane.reported = None             # a program that has not started has not reported
        pane.seen_status = None
        pane.started = pane.generation = None
        pane.claim_lock = None
        pane.ally = (env or {}).get("MISAKA_ALLY")   # as `create` reads it, per program
        pane.fg_at = pane.fg_seen = None
        pane.resize_target = pane.resize_hold = pane.sync_until = None
        pane.started_at = int(time.time())
        pane.fresh_terminal()
        self._spawn(pane, env=env)

    async def _deliver(self, pane: Pane, data, *, timeout, drop_on_timeout):
        """Queue ``data`` for the pane and wait until it has entered the program, or until
        ``timeout``. ``drop_on_timeout`` (pane.send): the unsent remainder is discarded and
        the caller gets the failure -- a note to a Sister must not arrive minutes later,
        after Last Order has already reacted to "not delivered". Without it (pane.input):
        keystrokes stay queued and deliver when the program reads, as a terminal would;
        the reply says how much is still pending."""
        if pane.fd is None:
            raise ValueError(f"Pane is missing or has exited: {pane.id}")
        queued = sum(len(item.view) for item in pane.writes)
        if queued + len(data) > WRITE_QUEUE_LIMIT:
            raise RuntimeError(
                f"Pane input queue is full: {queued} bytes are already waiting for the program "
                "in the pane to read them; it is not reading.")
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        # A pane that exits while an unwaited-for keystroke is queued would otherwise log
        # "exception was never retrieved" into the daemon log.
        future.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        item = _PendingWrite(bytes(data), future)
        pane.writes.append(item)
        self._drain_writes(pane)
        try:
            await asyncio.wait_for(asyncio.shield(future), timeout)
        except TimeoutError:
            pending = len(item.view)
            if not drop_on_timeout:
                return {"sent": True, "queued": pending}
            if item in pane.writes:
                pane.writes.remove(item)
            if not pane.writes and pane.writer_armed and pane.fd is not None:
                loop.remove_writer(pane.fd)
                pane.writer_armed = False
            future.cancel()
            # Say which way it failed: the bytes that entered the pane are in its input
            # queue and the program will read them; only the remainder was dropped.
            detail = (f"{item.landed} of {item.total} bytes entered the pane within {timeout:g}s and "
                      "the rest was dropped" if item.landed
                      else f"none of the {item.total} bytes entered the pane within {timeout:g}s")
            raise RuntimeError(
                f"Pane input buffer is full: {detail}; the program in the pane is not reading.") from None
        return {"sent": True}

    def _drain_writes(self, pane: Pane):
        """Write as much of the queue as the pty takes; arm the writer callback for the rest."""
        loop = asyncio.get_running_loop()
        while pane.writes and pane.fd is not None:
            item = pane.writes[0]
            try:
                written = _write_some(pane.fd, item.view)
            except OSError as error:
                self._fail_writes(pane, f"the pane's terminal refused input ({error})")
                return
            if not written:
                if not pane.writer_armed:
                    loop.add_writer(pane.fd, self._drain_writes, pane)
                    pane.writer_armed = True
                return
            item.view = item.view[written:]
            if not item.view:
                pane.writes.pop(0)
                if not item.future.done():
                    item.future.set_result(item.total)
        if pane.writer_armed and pane.fd is not None:
            loop.remove_writer(pane.fd)
            pane.writer_armed = False

    def _fail_writes(self, pane: Pane, reason):
        """The pane is going away: nothing queued will ever be read."""
        if pane.writer_armed and pane.fd is not None:
            try:
                asyncio.get_running_loop().remove_writer(pane.fd)
            except RuntimeError:
                pass
            pane.writer_armed = False
        pending, pane.writes = pane.writes, []
        for item in pending:
            if not item.future.done():
                item.future.set_exception(RuntimeError(
                    f"Pane input was not delivered: {item.landed} of {item.total} bytes entered the pane; {reason}."))

    def _pump(self, pane: Pane):
        try:
            chunk = os.read(pane.fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
        if not chunk:
            asyncio.get_running_loop().remove_reader(pane.fd)
            if pane.flush is not None:      # no more frames for a pane that has exited
                pane.flush.cancel()
                pane.flush = None
            self._fail_writes(pane, "the pane's program exited before reading it")
            os.close(pane.fd)
            pane.fd = None
            pane.exit_code = pane.proc.poll() if pane.proc else None
            pane.reported = None      # herdr: process exit is a generation fence; a dead session has no say
            self._broadcast(pane.id, {"event": "exited", "id": pane.id,
                                      "exit_code": pane.exit_code})
            return
        pane.buf += chunk
        pane.last_output = time.time()
        if len(pane.buf) > RING_CAP:
            del pane.buf[: len(pane.buf) - RING_CAP]
        pane.term.write(chunk)                # ghostty parses; its answers come back through Pane.reply
        # A synchronized block (DEC 2026) holds frames until it closes; the grace period
        # restarts with every chunk, so a slow repaint is never shown half-painted.
        pane.sync_until = time.monotonic() + SYNC_TIMEOUT if pane.term.mode(vt.MODE_SYNC_OUTPUT) else None
        if pane.resize_hold is not None:
            # The program answered the resize (a repaint, in practice a synchronized full
            # redraw). The hold has done its job; the sync logic governs from here.
            pane.resize_hold = None
        # Coalesce per frame instead of pushing every read. The PTY splits one repaint
        # into several chunks with the cursor parked mid-way (e.g. end of line); pushing
        # chunks makes clients place the cursor at those intermediate spots, so IME
        # candidate windows drift to the right edge. Sending a whole frame means clients
        # see the end-of-frame state.
        self._schedule_flush(pane, FRAME_SECONDS)

    def _schedule_flush(self, pane: Pane, delay):
        """Broadcast the pane's frame in ``delay`` seconds, or sooner if a flush is already
        due sooner. A later one is pulled in: the flush parked at a synchronized block's
        safety deadline must not hold the frame once the block has closed."""
        if not self._attached:
            return
        loop = asyncio.get_running_loop()
        if pane.flush is not None:
            if pane.flush.when() <= loop.time() + delay:
                return
            pane.flush.cancel()
        pane.flush = loop.call_later(delay, self._flush, pane)

    def _flush(self, pane: Pane):
        pane.flush = None
        if not self._attached:
            return
        if pane.resize_hold is not None:
            remaining = pane.resize_hold - time.monotonic()
            if remaining > 0:
                # The size changed and ghostty has reflowed the old content, but the program
                # has not repainted yet. Broadcasting ghostty's reflow here is the "排版重组
                # 失败" the user photographed: full-width rows spill their last cells onto a
                # continuation row, and bg-styled rows each gain a blank line. herdr never
                # shows it because pi (bun) repaints within a frame; pi in Python is slower,
                # so the last complete frame stays on screen until the repaint lands (_pump
                # lifts the hold on the program's first byte) or this deadline passes.
                self._schedule_flush(pane, remaining)
                return
            pane.resize_hold = None                    # the program ignored SIGWINCH: show what ghostty has
        if pane.sync_until is not None:
            remaining = pane.sync_until - time.monotonic()
            if remaining > 0 and pane.term.mode(vt.MODE_SYNC_OUTPUT):
                self._schedule_flush(pane, remaining)   # mid-frame: look again at the deadline
                return
            pane.sync_until = None                     # closed, or a block nobody closed: show what there is
        state = input_state(pane)
        pane.render.update(pane.term)
        rendered = {index: _render_cells(cells) for index, cells in pane.render.rows(all_rows=pane.full_frame)}
        pane.full_frame = False
        x, y, visible = pane.render.cursor()
        cursor = ([x, y], not visible)
        # Cursor moves and mode changes are frames too: neither dirties a row.
        if not rendered and cursor == pane.sent_cursor and state == pane.sent_input:
            return
        pane.sent_cursor = cursor
        pane.sent_input = state
        shown = _shown(pane, rendered)
        # Scroll metrics ride along: a clear wipes scrollback and the panel must collapse its scrollbar.
        self._broadcast(pane.id, {
            "event": "screen", "id": pane.id,
            "rows": {str(r): line for r, line in rendered.items()},
            "cursor": cursor[0],
            "cursor_hidden": cursor[1],
            "scroll": shown["scroll"],
            "alt_screen": shown["alt_screen"],
            "input": state,
        })
        # The program has finished this frame; if a drag moved on while it rendered, resize it
        # toward the size the client now wants (herdr resizes each render, as fast as pi keeps up).
        self._drive_resize(pane)

    def _request_resize(self, pane: Pane, rows, cols):
        """Record the size the client wants and drive it toward the program. The panel never
        waits on this: it acknowledges the size and tracks the divider from its own cache. A
        drag calls this on every step; the size is handed to the program as fast as the program
        can repaint it (see ``_drive_resize``). False when nothing changes (an unchanged size
        must not SIGWINCH the program into a pointless full repaint on every tab switch)."""
        current = pane.resize_target if pane.resize_target is not None else pane.size()
        if current == (rows, cols):
            return False
        pane.resize_target = (rows, cols)
        self._drive_resize(pane)
        return True

    def _drive_resize(self, pane: Pane):
        """Apply the pending size once the program is idle -- it has finished repainting the
        last size and is not mid-frame -- capped at herdr's render pace. herdr resizes during
        every render (16 ms) and bun-pi always finishes within it, so it is never resized
        mid-repaint; pi in Python takes longer, so a drag resizes it as fast as it actually
        repaints, showing each intermediate width cleanly instead of thrashing it with renders
        it would drop. Runs off the request (a scheduled apply), so the client is never blocked
        on the reflow, and again after each frame is broadcast, to pick up a drag that moved on
        while the program was rendering."""
        if pane.resize_target is None or pane.fd is None or pane.resize_apply is not None:
            return
        if pane.resize_hold is not None or pane.sync_until is not None:
            return                              # still repainting the last size: apply the next when it lands
        loop = asyncio.get_running_loop()
        delay = max(0.0, RESIZE_MIN_INTERVAL - (loop.time() - pane.resize_applied_at))
        pane.resize_apply = loop.call_later(delay, self._apply_resize, pane)

    def _apply_resize(self, pane: Pane):
        """The size a client asked for, in terminal order: pty first, emulator, then the
        program hears of it; the frame with every row follows at once."""
        pane.resize_apply = None
        target, pane.resize_target = pane.resize_target, None
        if target is None or pane.fd is None:
            return
        rows, cols = target
        if pane.size() == (rows, cols):
            return
        pane.resize_applied_at = asyncio.get_running_loop().time()
        try:
            fcntl.ioctl(pane.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass
        pane.resize(rows, cols)
        if pane.proc is not None:
            try:
                os.killpg(pane.proc.pid, signal.SIGWINCH)
            except OSError:
                pass
        # Keep the last complete frame on screen until the program repaints at the new size,
        # instead of broadcasting ghostty's reflow of the old content. _pump lifts the hold on
        # the program's first byte; the deadline is the fallback for one that never answers.
        pane.resize_hold = time.monotonic() + RESIZE_HOLD_SECONDS
        self._schedule_flush(pane, RESIZE_HOLD_SECONDS)

    def _leave_scrollback(self, pane: Pane):
        """A keystroke into the program returns the viewer to the live screen, as in a terminal."""
        if not pane.term.scroll_metrics()["offset_from_bottom"]:
            return
        _scroll_pane(pane, to="bottom")
        pane.full_frame = True
        self._schedule_flush(pane, FRAME_SECONDS)

    def _broadcast(self, pane_id, payload):
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
        behind = False
        for writer, wanted in list(self._attached.items()):
            if wanted not in (pane_id, "*") or writer.transport.is_closing():
                continue
            # Backpressure drops the FRAME, never the subscription. Cancelling it left the
            # panel connected, never told, and attached only once for its whole life, so
            # every pane froze for good with nothing on screen to say why (audit 2026-09-02,
            # ui-panel-14). Checked before the write, so a stalled reader cannot grow the
            # buffer further; the pane is marked fully dirty and the next frame resends it.
            try:
                if writer.transport.get_write_buffer_size() > 4 * 1024 * 1024:
                    behind = True
                    continue
                writer.write(line)
            except Exception:  # noqa: BLE001 - remove dead subscribers
                self._attached.pop(writer, None)
        pane = self.panes.get(pane_id) if behind else None
        if pane is not None and pane.fd is not None:
            pane.full_frame = True
            pane.sent_cursor = None

    def create(self, argv, cwd, *, title="", card=None, env=None, place=None) -> Pane:
        self._seq += 1
        pane = Pane(f"p{self._seq}", title or (argv[0] if argv else ""), list(argv),
                    cwd or os.getcwd(), card=card)
        if env and env.get("MISAKA_THEME") in ("dark", "light"):
            self._theme = env["MISAKA_THEME"]   # remember the session variant
            pane.set_theme(self._theme)
        if env and env.get("MISAKA_ALLY"):
            pane.ally = env["MISAKA_ALLY"]      # transient ally: in the roster until the process exits
        self._spawn(pane, env=env)
        self.panes[pane.id] = pane
        self._seat(pane, place or {})
        self._save_snapshot()
        return pane

    # ── Layout (herdr: a pane is created INTO a slot; nothing re-homes it later) ──

    def _tab_holding(self, pane_id):
        for space in self.spaces:
            for tab in space.get("tabs") or []:
                tree = hui.from_jsonable(tab.get("tree"))
                if tree and pane_id in hui.pane_ids(tree):
                    return space, tab
        return None

    def _seat(self, pane, place):
        """``{"split": pane_id[, "direction": "h"|"v"]}`` splits that pane in its own tab (herdr
        split_at, 50/50); ``{"grid": pane_id}`` adds the pane to that pane's tab and re-lays the
        tab as a balanced grid (a Last Order and the Sisters she summoned share one tab this
        way); ``{"tab": pane_id}`` opens a new tab in the space holding that pane; anything else
        (or an unknown pane) opens a new space in the pane's folder. ``name`` names a new tab
        (herdr custom_name); a new tab is otherwise named after its pane."""
        ref = place.get("split") or place.get("tab") or place.get("grid")
        at = self._tab_holding(ref) if ref else None
        name = place.get("name") or pane.title
        if at and place.get("grid"):
            tab = at[1]
            ids = hui.pane_ids(hui.from_jsonable(tab["tree"])) + [pane.id]
            tab["tree"] = hui.to_jsonable(hui.grid_tree(ids))
            tab["grid"] = True     # remembered so a later add or close re-balances (survives the client round-trip)
        elif at and place.get("split"):
            tab = at[1]
            tab["tree"] = hui.to_jsonable(hui.split_at(
                hui.from_jsonable(tab["tree"]), ref, place.get("direction") or "h", pane.id, 0.5))
        elif at:
            at[0]["tabs"].append({"name": name, "tree": ["pane", pane.id]})
        else:
            self._space_seq += 1
            self.spaces.append({"id": f"w{self._space_seq}", "folder": os.path.realpath(pane.cwd),
                                "name": None, "tabs": [{"name": name, "tree": ["pane", pane.id]}]})
        self.layout_revision += 1

    def _unseat(self, pane_id):
        """layout.rs close_pane: drop the leaf; a tab left empty goes, a space left without tabs goes."""
        for space in self.spaces:
            for tab in space.get("tabs") or []:
                tree = hui.from_jsonable(tab.get("tree"))
                if tree and pane_id in hui.pane_ids(tree):
                    tree = hui.remove_pane(tree, pane_id)
                    if tree and tab.get("grid"):
                        tree = hui.grid_tree(hui.pane_ids(tree))   # a grid closes its gap by re-balancing
                    tab["tree"] = hui.to_jsonable(tree) if tree else None
            space["tabs"] = [tab for tab in (space.get("tabs") or []) if tab.get("tree")]
        self.spaces = [space for space in self.spaces if space["tabs"]]
        self.layout_revision += 1

    def apply_layout(self, spaces, revision=None):
        """A client-side edit of the layout. It must be based on the revision the client last saw
        (an edit racing another client's is refused, not merged blindly), and it can never make
        a live pane disappear: a pane the edit omits is seated again."""
        if revision is not None and int(revision) != self.layout_revision:
            raise ValueError(f"stale layout (revision {revision}, current {self.layout_revision}); reload it first")
        incoming = [space for space in spaces if space.get("tabs")]
        # The shape is checked before it is adopted, not after. `_tab_holding` and `_unseat`
        # index `tab["tree"]` directly, so one tab without a usable tree made every later
        # pane.create and pane.close raise KeyError -- and close() pops the pane first, so
        # the daemon ended up unable to close anything (audit 2026-09-02, ui-panel-07).
        for space in incoming:
            for tab in space["tabs"]:
                if not isinstance(tab, dict) or hui.from_jsonable(tab.get("tree")) is None:
                    raise ValueError(f"Malformed layout: a tab of space {space.get('id')!r} "
                                     "carries no readable split tree.")
        seated = _seated_pane_ids(incoming)
        self.spaces = incoming
        for pane in list(self.panes.values()):
            if pane.id not in seated and pane.alive():
                self._seat(pane, {})
        self.layout_revision += 1
        return {"ok": True, "revision": self.layout_revision}

    async def _reap(self, proc):
        """SIGTERM was already sent: give the process a grace period, escalate to
        SIGKILL, and reap -- polling, so the event loop never blocks."""
        for _ in range(EXIT_GRACE_TRIES):        # 2s grace for engines to save state
            if proc.poll() is not None:
                break
            await asyncio.sleep(EXIT_POLL_SECONDS)
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            while proc.poll() is None:
                await asyncio.sleep(EXIT_POLL_SECONDS)
        await self._sweep_catalog()

    async def _sweep_catalog(self):
        """A session killed before it could retire itself leaves its record and its control
        socket behind; the daemon is the one that killed it, so it clears up after it
        (2026-09-18, B3)."""
        try:
            from misaka.core import session_catalog
            await asyncio.to_thread(session_catalog.sweep_dead)
        except Exception:  # noqa: BLE001, S110 - housekeeping must never take a pane close down
            pass

    @staticmethod
    def _check_generation(pane, expected_generation):
        if expected_generation is not None and pane.generation != int(expected_generation):
            raise ValueError(
                f"Pane {pane.id} is generation {pane.generation}; "
                f"expected generation {int(expected_generation)}."
            )

    def close(self, pane_id, *, expected_generation=None):
        pane = self.panes.get(pane_id)
        if pane is None:
            raise ValueError(f"Pane not found: {pane_id}")
        self._check_generation(pane, expected_generation)
        if pane.card and pane.claim_lock:
            # Explicit close (including daemon shutdown) must not orphan the claim.
            # Natural card exits stay in the pane registry for _watch_cards to settle.
            from misaka.core.platform import tasks as db
            con = self._board()
            db.add_event(con, pane.card, "stopped", {},
                         generation=pane.generation, claim_lock=pane.claim_lock)
            db.mark_stopped(con, pane.card,
                            generation=pane.generation, claim_lock=pane.claim_lock)
            pane.claim_lock = None
        self.panes.pop(pane_id)
        if pane.resize_apply is not None:      # a coalesced resize must not fire on a freed terminal
            pane.resize_apply.cancel()
            pane.resize_apply = None
        pane.close_terminal()
        if pane.alive():
            # herdr kills and moves on: waiting here froze every pane for up to 4s
            # per close (cascades serially longer). SIGTERM now; a background task
            # escalates to SIGKILL and reaps, off the event loop.
            try:
                os.killpg(pane.proc.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is None:      # no event loop (test teardown): the old synchronous ladder
                try:
                    pane.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(pane.proc.pid, signal.SIGKILL)
                    except OSError:
                        pass
                    try:
                        pane.proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
            else:
                reaper = loop.create_task(self._reap(pane.proc))
                self._reapers.add(reaper)
                reaper.add_done_callback(self._reapers.discard)
        if pane.fd is not None:
            try:
                asyncio.get_running_loop().remove_reader(pane.fd)
                self._fail_writes(pane, "the pane was closed before it was read")
            except RuntimeError:  # event loop already closed
                pass
            if pane.flush is not None:
                pane.flush.cancel()
                pane.flush = None
            try:
                os.close(pane.fd)
            except OSError:
                pass
            pane.fd = None
        self._unseat(pane_id)
        self._save_snapshot()
        return pane

    # ── Card panes (driven by the board state machine) ──────────

    def _board(self):
        if self._con is None:
            from misaka.core.platform import tasks as db
            self._con = db.connect(_expand(CFG["db"]))
        return self._con

    def _mailbox(self):
        """The mailbox connection, kept like the board's.

        `panes.list` used to open one per call -- connect, run the schema script, sweep old
        rows, GROUP BY, close -- and the panel calls `panes.list` on every poll, on the
        daemon's single event loop (audit 2026-09-02, ui-panel-08).
        """
        if self._mcon is None:
            from misaka.core.network import messages
            self._mcon = messages.connect()
        return self._mcon

    def _card_workspace(self, row):
        """The card's folder is its project: a pane always runs there, and a folder that is
        gone is refused rather than recreated somewhere the user deleted it."""
        from misaka.core.platform import tasks as db
        workspace = db.workspace_for(row)
        if not os.path.isdir(workspace):
            raise ValueError(f"Card {row['id']}: its folder {workspace} no longer exists.")
        return workspace

    def _card_env(self, row):
        from misaka.core.platform import tasks as db
        return {
            "MISAKA_THEME": self._theme,    # card panes follow the session theme
            # misaka commands inside the pane (e.g. an ally's `misaka tell`) must use
            # the daemon's home, or messages land in a different messages.db
            # and Last Order never sees them.
            home.ENV_HOME: str(home.home()),
            "MISAKA_TASK_OUTPUT_DIR": str(row["output_dir"] or db.workspace_for(row)),
        }

    def _settled_card_with_session(self, task_id, *, require_session=True):
        """A card whose session can be reopened: not live in a pane, with a saved transcript.
        Without ``require_session`` a card that never got as far as a transcript -- stopped
        seconds after its claim (2026-09-18, B16) -- is answered with ``transcript=None``."""
        from misaka.core.network.sister_runtime import ACTIVE_BOARD_STATUSES
        from misaka.core.platform import tasks as db
        from misaka.core.session_manager import find_most_recent_session
        row = db.get(self._board(), task_id)
        if row is None:
            raise ValueError(f"Card not found: {task_id}")
        if row["status"] in ACTIVE_BOARD_STATUSES:
            raise ValueError(f"Card {task_id} is still {row['status']}; steer its running pane instead.")
        transcript = find_most_recent_session(session_roots.card_session_dir(row))
        if transcript is None and require_session:
            raise ValueError(f"Card {task_id} has no saved session to reopen.")
        return row, transcript

    def _card_lock(self, task_id):
        """Hosting a card stops the program in its window before the board's claim decides who
        won the card, so two requests racing for the same card must not both get that far: one
        attempt at a time per card inside this daemon, and the loser meets an owned card."""
        lock = self._card_locks.get(task_id)
        if lock is None:
            lock = self._card_locks[task_id] = asyncio.Lock()
        return lock

    def _card_pane_to_reuse(self, task_id):
        """This card's window: the pane the user last opened for it, live ones first. A card
        has one window, whoever opened it -- the dispatcher, a continuation, or a look at its
        saved session -- so its next program takes this one over instead of adding another."""
        panes = [pane for pane in self.panes.values() if pane.card == task_id]
        return max(panes, key=lambda pane: (pane.alive(), pane.started_at)) if panes else None

    async def open_card_session(self, task_id, place=None) -> Pane:
        """Reopen a card's saved session to look at it: no claim, no contract, no model turn.
        The panel uses it for a click on a card session, Last Order to bring a Sister back into
        view.

        It opens the panel's own follower (``misaka chat --read-only``), which renders the
        transcript as it grows and assembles no engine, no session writer and no lifecycle at
        all. Looking at a card can therefore neither run a turn on it nor take its catalog
        record away from the process that owns it -- both of which a second ``card-shell
        --resume`` could do, and did (2026-09-18, B18/B19). A card already showing in a pane is
        answered with that pane rather than opened a second time (B14)."""
        row, transcript = self._settled_card_with_session(task_id)
        argv = [sys.executable, "-m", "misaka", "chat", "--read-only", "--session", transcript]
        title, workspace = f"{row['assignee']}·{task_id}", self._card_workspace(row)
        pane = self._card_pane_to_reuse(task_id)
        if pane is not None and pane.alive():
            return pane
        if pane is not None:
            # Its program exited; the window is still the card's, so the reader takes it over
            # rather than opening a second one next to a dark pane.
            await self._replace_program(pane, argv, cwd=workspace, env=self._card_env(row), title=title)
        else:
            pane = self.create(argv, workspace, title=title, card=task_id,
                               env=self._card_env(row), place=place)
        pane.generation = int(row["generation"])
        self._save_snapshot()
        return pane

    async def _vacate_card_panes(self, con, row, *, keep=None):
        """One card, one window, one writer. Every program still holding this card's session is
        stopped before its next attempt starts: ``keep`` -- the pane the attempt will run in --
        keeps its id, seat and size and only loses its program, and the card's other panes are
        closed. It runs after the board has granted the claim, so a refused attempt never
        empties the window it was not given."""
        leaving = []
        for pane in [p for p in self.panes.values() if p.card == row["id"] and p is not keep]:
            leaving.append(pane.proc)
            self.close(pane.id)          # SIGTERM now; `_reap` escalates off the request
        if keep is not None:
            await self._stop_program(keep)
        for proc in leaving:
            if not await self._await_exit(proc):
                raise ValueError(f"Card {row['id']}: a pane of an earlier attempt will not exit; try again.")

    def _verify_previous_dead(self, con, row):
        """Prove the recorded process of this generation gone before a blocked card is taken
        over, killing an orphan process group if that is what it is. It decides whether the
        takeover may happen at all, so it runs before the claim and never touches a pane."""
        from misaka.core.platform import processes as process_tree
        from misaka.core.subagent.child import PROCESS_GROUP_IDENTITY

        previous = con.execute(
            "SELECT pid,process_identity FROM task_runs WHERE id=? AND task_id=? "
            "AND generation=?",
            (row["current_run_id"], row["id"], row["generation"]),
        ).fetchone()
        if previous is None or previous["pid"] is None:
            return
        pid = int(previous["pid"])
        identity = str(previous["process_identity"] or "")
        if identity.startswith(PROCESS_GROUP_IDENTITY):
            drained = process_tree.terminate_orphaned_group(
                pid, identity[len(PROCESS_GROUP_IDENTITY):]
            )
        elif not identity:
            drained = not psutil.pid_exists(pid)
        else:
            drained = not process_tree.identity_is_alive(pid, identity)
        if not drained:
            raise ValueError(
                f"Card {row['id']}'s previous process is still running; try again."
            )

    async def continue_card(self, task_id, say, place=None, *, expected_generation=None) -> Pane:
        """Continue a settled card with a new model turn: the same ``claim_resume`` as the
        in-process Sister runtime (a new generation under our lock), after which the
        session lifecycle settles the card exactly like a first run."""
        from misaka.core.platform import admission
        from misaka.core.platform import tasks as db
        async with self._card_lock(task_id):
            return await self._continue_card(task_id, say, place, expected_generation,
                                             admission=admission, db=db)

    async def _continue_card(self, task_id, say, place, expected_generation, *, admission, db) -> Pane:
        row, transcript = self._settled_card_with_session(task_id, require_session=False)
        generation = int(row["generation"])
        if expected_generation is not None and generation != int(expected_generation):
            raise ValueError(
                f"Card {task_id} is generation {generation}; "
                f"expected generation {int(expected_generation)}."
            )
        con = self._board()
        reuse = self._card_pane_to_reuse(task_id)
        if row["status"] in {"blocked", "triage"}:
            self._verify_previous_dead(con, row)
        lock = f"net:{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"
        host_cap, assignee_cap = admission.limits()
        if not db.claim_resume(con, task_id, lock, os.getpid(),
                               expected_generation=generation,
                               host_cap=host_cap, assignee_cap=assignee_cap):
            raise ValueError(f"Card {task_id} could not be claimed for continuation; it changed "
                             "underneath, or the host is at capacity.")
        row = db.get(con, task_id)
        generation = int(row["generation"])
        if transcript is None:
            # Nothing to resume: this is the card's first attempt, run from its contract, and
            # the note travels by mail so the card's own inbox hands it over at the first
            # tool boundary (B16; the mail path is B8's card address).
            argv = [*CARD_SHELL, task_id]
            self._mail_card(row, say, generation)
        else:
            argv = [*CARD_SHELL, task_id, "--resume", "--say", say]
        return await self._host_card(
            con, row, lock, generation, argv, place,
            undo=lambda: db.block_task(con, task_id, "transient",
                                       "the pane could not be started; continue the card again",
                                       generation=generation, claim_lock=lock),
            event="continued" if transcript else "started", reuse=reuse)

    def _mail_card(self, row, text, generation):
        """A note for one attempt at a card, from its Last Order, read by the card's inbox."""
        from misaka.core.network import messages
        messages.send(self._mailbox(), row["assignee"], text, summary=text[:80].strip() or "note",
                      sender="last-order", to_task=row["id"], generation=generation,
                      workspace=row["workspace"])

    async def run_card(self, task_id, place=None) -> Pane:
        from misaka.core.platform import admission
        from misaka.core.platform import tasks as db
        async with self._card_lock(task_id):
            return await self._run_card(task_id, place, admission=admission, db=db)

    async def _run_card(self, task_id, place, *, admission, db) -> Pane:
        con = self._board()
        row = db.get(con, task_id)
        if row is None:
            raise ValueError(f"Card not found: {task_id}")
        if row["status"] != "ready":
            raise ValueError(f"Card {task_id} is not ready (current status: {row['status']}).")
        # A card requeued while its last window is still open runs in that window again.
        reuse = self._card_pane_to_reuse(task_id)
        # Who runs the card: no executor = a Sister (card-shell), otherwise an ally's
        # third-party CLI. This is the only fork; claim, lease, submission,
        # acceptance, and audit are shared (the board is the single bus).
        executor = json.loads(row["executor"]) if row["executor"] else None
        if executor is None:
            profile = os.path.join(_expand(CFG["profiles_root"]), row["assignee"])
            if not os.path.isdir(profile):
                raise ValueError(f"Sister {row['assignee']} is not in the roster.")
        generation = int(row["generation"])
        lock = f"net:{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"
        host_cap, assignee_cap = admission.limits()
        if not db.claim(con, task_id, lock,
                        generation=generation, pid=os.getpid(),
                        host_cap=host_cap, assignee_cap=assignee_cap):
            raise ValueError(db.claim_refusal(task_id) or f"Card {task_id} was claimed by another dispatcher.")
        undo = lambda: db.back_to_ready(con, task_id, generation=generation, claim_lock=lock)
        try:
            workspace = self._card_workspace(row)
            env = None
            if executor is None:
                argv = [*CARD_SHELL, task_id]
            else:  # ally: the card contract is the prompt, run one non-interactive turn
                from misaka.core.network.ally import runner as ally_runner
                from misaka.core.platform import cards
                task = dict(row)
                task["_attachments"] = cards.attachment_list(workspace, task_id, workspace=workspace)
                argv = ally_runner.build_argv(executor, ally_runner.card_prompt(task))
                env = {"MISAKA_ALLY": row["assignee"]}
        except BaseException:
            undo()
            raise
        return await self._host_card(con, row, lock, generation, argv, place,
                                     undo=undo, env=env, reuse=reuse)

    async def _host_card(self, con, row, lock, generation, argv, place, *, undo, env=None,
                         event="claimed", reuse=None):
        """Host a claimed card in a pane; ``undo`` releases its claim if no pane starts."""
        from misaka.core.platform import processes as process_tree
        from misaka.core.platform import tasks as db
        task_id = row["id"]
        pane = None
        # Everything below `create` is part of hosting too: `set_pid` writes the credential
        # reconcile needs to reclaim by process group, and a disk-full or locked board threw
        # right past `undo()`, leaving the pane running on a lease nobody could release
        # (audit 2026-09-02, ui-panel-06). One try covers the whole hand-over; the pane the
        # caller will never hear about is closed before the claim goes back.
        try:
            # Now that the card is ours: one window, one writer.
            await self._vacate_card_panes(con, row, keep=reuse)
            workspace = self._card_workspace(row)
            title = f"{row['assignee']}·{task_id}"
            hosting = {**self._card_env(row),
                       "MISAKA_USAGE_DB": _expand(CFG["db"]),
                       "MISAKA_USAGE_TASK_ID": task_id,
                       "MISAKA_USAGE_GENERATION": str(generation),
                       "MISAKA_USAGE_CLAIM_LOCK": lock,
                       "MISAKA_USAGE_TOKEN_CAP": str(int(CFG["token_cap"] or 0)),
                       **(env or {})}
            if reuse is not None and self.panes.get(reuse.id) is reuse:
                pane = reuse
                await self._replace_program(pane, argv, cwd=workspace, env=hosting, title=title)
                if self.panes.get(pane.id) is not pane:
                    # Closed while its program was being replaced: the new one has nobody to
                    # show it, so it goes the same way rather than running on unattached.
                    await self._stop_program(pane)
                    raise ValueError(f"Pane {pane.id} was closed while card {task_id} started in it.")
                pane.card = task_id
            else:
                pane = self.create(argv, workspace, title=title, card=task_id,
                                   env=hosting, place=place)
            pane.claim_lock, pane.generation = lock, generation
            pane.started = time.time()
            # Owner = the pane's process group: if the daemon dies, reconcile reclaims by group identity (same marker as child.py).
            identity = process_tree.identity(pane.proc.pid)
            db.set_pid(con, task_id, pane.proc.pid,
                       worker_identity=f"process-group|{identity}" if identity else None,
                       generation=generation, claim_lock=lock)
            db.add_event(con, task_id, event,
                         {"lock": lock, "workspace": workspace, "pane": pane.id},
                         generation=generation)
            self._save_snapshot()
        except BaseException:
            if pane is not None:
                pane.card = pane.claim_lock = None   # the watcher must not read this as a crash
                try:
                    self.close(pane.id)
                except Exception:  # noqa: BLE001, S110 - the claim must go back even if the pane will not close
                    pass
            undo()
            raise
        return pane

    async def _watch_cards(self):
        """Keep every card pane's lease alive and reconcile its process exit.

        A Sister's lifecycle hook owns submission; an external ally has no such hook, so
        its process exit is adapted into a submission.  Neither is interrupted for taking
        a long time.

        Everything slow here runs off the loop. This coroutine shares its thread with every
        pane's PTY reader and frame timer. The ally adapter import, `ally_runner.finish`,
        and `dispatch.accept` go through `asyncio.to_thread`; the board's own statements stay
        inline (single indexed CAS updates on a WAL connection, and keeping them here keeps
        every mutation of pane state on one thread). Each await is a suspension point, so the
        pane is re-checked afterwards -- `card.stop` or `pane.close` may have run meanwhile.
        """
        import importlib

        from misaka.core.platform import tasks as db

        def still_ours(pane):
            return self.panes.get(pane.id) is pane and pane.claim_lock is not None

        await self._sweep_catalog()   # records left behind by a previous panel (B3)
        while not self._stopping.is_set():
            if self._socket_identity is not None:
                try:
                    info = os.stat(self.sock_path, follow_symlinks=False)
                    owned = (info.st_dev, info.st_ino) == self._socket_identity
                except OSError:
                    owned = False
                if not owned:
                    self._stopping.set()    # no address remains through which this daemon can be managed
                    break
            cards = [p for p in self.panes.values() if p.card and p.claim_lock]
            for pane in cards:
                if not still_ours(pane):
                    continue
                con = self._board()
                row = db.get(con, pane.card)
                if row is None or int(row["generation"]) != pane.generation \
                        or row["claim_lock"] != pane.claim_lock:
                    pane.claim_lock = None          # ownership changed: observe only, never touch state
                    continue
                if not pane.alive():
                    exit_code = (
                        pane.exit_code
                        if pane.exit_code is not None
                        else pane.proc.poll()
                    )
                    if not pane.ally:
                        if exit_code:
                            tail = pane.buf.decode("utf-8", errors="replace")
                            tail = re.sub(
                                r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|[\r\x00]",
                                "",
                                tail,
                            ).strip()
                            reason = f"Sister process exited with code {exit_code}"
                            if tail:
                                reason += f": {tail[-500:]}"
                            if db.add_event(
                                con,
                                pane.card,
                                "failed",
                                {"reason": reason},
                                generation=pane.generation,
                                claim_lock=pane.claim_lock,
                            ):
                                db.mark_failed(
                                    con,
                                    pane.card,
                                    generation=pane.generation,
                                    claim_lock=pane.claim_lock,
                                    failure_kind="crash",
                                    reason=reason,
                                )
                        else:
                            db.mark_unsettled(
                                con,
                                pane.card,
                                generation=pane.generation,
                                claim_lock=pane.claim_lock,
                            )
                        pane.claim_lock = None
                        continue
                    # An ally has no lifecycle hook, so adapt its exit into the same
                    # submission object the board accepts for Sisters.
                    ally_runner = await asyncio.to_thread(
                        importlib.import_module,
                        "misaka.core.network.ally.runner")
                    # The tail is snapshotted here, on the loop's thread: decoding the
                    # live bytearray from the worker would race _pump's append.
                    tail = pane.buf.decode("utf-8", errors="replace")
                    submission, summary = await asyncio.to_thread(
                        ally_runner.finish,
                        pane.cwd,
                        exit_code if exit_code is not None else -1,
                        tail,
                        assignee=pane.ally, task_id=pane.card,
                        output_dir=row["output_dir"], generation=pane.generation,
                        since=getattr(pane, "started", None))
                    if not still_ours(pane):
                        continue
                    accepted = False
                    if submission is not None:
                        dispatch = await asyncio.to_thread(
                            importlib.import_module, "misaka.core.network.dispatch"
                        )
                        accepted = await asyncio.to_thread(
                            dispatch.accept,
                            con,
                            row,
                            submission,
                            generation=pane.generation,
                            claim_lock=pane.claim_lock,
                            workspace=pane.cwd,
                        )
                    else:
                        # No submission to adapt: a crash if the ally died, otherwise a turn
                        # that ended without one. Both count against the card's attempts.
                        db.mark_failed(
                            con, pane.card, generation=pane.generation,
                            claim_lock=pane.claim_lock,
                            failure_kind="protocol_violation" if exit_code == 0 else "crash",
                            reason=str(summary)[:500],
                        )
                    pane.claim_lock = None
                    head = "finished and submitted" if accepted else "could not submit"
                    try:
                        await asyncio.to_thread(
                            ally_runner.notify,
                            pane.card,
                            f"Ally {pane.ally} {head} (card {pane.card}):\n\n{summary}",
                            sender=pane.ally,
                        )
                    except Exception:  # noqa: BLE001, S110 - notification cannot change board state
                        pass
                else:
                    # The lease says the pane's process exists; progress is the Sister's own
                    # session to stamp.
                    if time.time() - pane.last_heartbeat >= 60 and db.heartbeat(
                        con, pane.card, pane.claim_lock,
                        generation=pane.generation, progress=False,
                    ):
                        pane.last_heartbeat = time.time()
            try:
                await asyncio.wait_for(self._stopping.wait(), CARD_POLL_SECONDS)
            except TimeoutError:
                pass

    # ── Snapshot (shape only: argv, cwd, card -- never process state) ──

    @staticmethod
    def _pane_reply(pane):
        return {"pane_id": pane.id, "pid": pane.proc.pid if pane.proc else None, "card": pane.card}

    @classmethod
    async def _hosted(cls, hosting):
        """The reply for a card the daemon is putting into a pane."""
        return cls._pane_reply(await hosting)

    def _save_snapshot(self):
        # Shape only, for the crash trace and the pending-card notice at the next start.
        # Nothing is recreated from it: since the daemon leaves with the panel, a restore
        # could only raise stale Last Orders as fresh sessions in folders the user left.
        data = {"version": 1, "panes": [
            {"id": p.id, "title": p.title, "argv": p.argv, "cwd": p.cwd,
             "card": p.card} for p in self.panes.values()]}
        tmp = self.snapshot_path + ".tmp"
        os.makedirs(os.path.dirname(self.snapshot_path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.snapshot_path)

    def restore_snapshot(self):
        """Nothing is restored (see _save_snapshot); this only reports the cards that were still
        running when the last daemon died, so the panel can say they need redispatching."""
        try:
            with open(self.snapshot_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return []
        return [item["card"] for item in data.get("panes", []) if item.get("card")]

    # ── Protocol ──────────────────────────────────────────────

    def _api(self, method, params):
        if method == "ping":
            return {"pong": True, "pid": os.getpid(), "panes": len(self.panes),
                    "panels": len(self._panels), "proto": PROTOCOL}
        if method == "panes.status":
            # Research waits on lifecycle only; no mailbox/roster/foreground/UI scans.
            return {"panes": [{"id": p.id, "card": p.card, "alive": p.alive(), "reported": p.reported}
                              for p in self.panes.values()]}
        if method == "panes.list":
            status, mail = {}, {}
            cards = [p.card for p in self.panes.values() if p.card]
            if cards:
                from misaka.core.platform import tasks as db
                con = self._board()
                for card in cards:
                    row = db.get(con, card)
                    status[card] = row["status"] if row else "?"
                try:
                    mail = dict(self._mailbox().execute(
                        "SELECT task_id, COUNT(*) FROM messages"
                        " WHERE delivered_at IS NULL AND task_id IS NOT NULL"
                        " GROUP BY task_id").fetchall())
                except Exception:  # noqa: BLE001 - a broken mailbox must not take the panel down
                    if self._mcon is not None:
                        try:
                            self._mcon.close()
                        except Exception:  # noqa: BLE001, S110 - it is already unusable
                            pass
                        self._mcon = None   # reopened on the next call
                    mail = {}
            def row(p):
                ally = _ally_name(p)             # non-empty = a third-party agent runs in this pane
                state = _ally_state(p) if ally and p.alive() else None
                return {"id": p.id, "title": p.title, "card": p.card, "cwd": p.cwd,
                        "generation": p.generation,
                        "reported": p.reported,
                        "alive": p.alive(), "exit_code": p.exit_code,
                        # Holding the card's claim: the attempt itself, as opposed to a pane
                        # that is only showing the card's session.
                        "claimed": bool(p.claim_lock),
                        "status": status.get(p.card),
                        "busy": _pane_busy(p, ally_state=state),
                        "foreground": _foreground(p),   # what runs in the foreground (for Last Order)
                        "ally": ally,
                        "agent_state": state,    # herdr's detection: working / blocked / idle / unknown
                        "mail": int(mail.get(p.card, 0)) if p.card else 0,
                        "unseen": bool(p.card
                                       and status.get(p.card) in {"done", "failed", "stopped"}
                                       and status.get(p.card) != p.seen_status),
                        # What the pane was launched on: the panel reads `--session <path>`
                        # from it to know which conversation a pane is opening before the
                        # session itself reports (the gap where a second click opened a
                        # second tab).
                        "argv": list(p.argv),
                        "pid": p.proc.pid if p.proc else None}
            return {"panes": [row(p) for p in self.panes.values()]}
        if method == "cards.list":
            # The board's cards with what the panel's sessions list needs: the folder each
            # belongs to, whether it has a session to reopen, and which Last Order conversation
            # created it. Given a workspace, only that folder's cards.
            from misaka.core.platform import tasks as db
            workspace = (db.canonical_workspace(params["workspace"])
                         if params.get("workspace") else None)
            from misaka.core.research import runs
            from misaka.core.session_manager import find_most_recent_session
            contexts = runs.task_contexts(self._board())
            cards = []
            for r in self._board().execute(
                    "SELECT id,status,title,assignee,workspace,origin_session,session_file,session_dir FROM tasks ORDER BY created_at"):
                context = contexts.get(r["id"])
                project = context["workspace"] if context else r["workspace"]
                if workspace and project != workspace:
                    continue
                sess = session_roots.card_session_dir(r)
                available = (os.path.isdir(r["workspace"] or "")
                             and os.path.isdir(project or ""))
                transcript = find_most_recent_session(sess) if available else None
                cards.append({"id": r["id"], "status": r["status"], "title": r["title"],
                              "assignee": r["assignee"], "workspace": r["workspace"], "project": project,
                              "origin_session": r["origin_session"], "research": context,
                              "has_session": transcript is not None, "session_file": transcript})
            from misaka.core import session_catalog
            paths = [(p.reported or {}).get("session") for p in getattr(self, "panes", {}).values()]
            entries = [entry for entry in session_catalog.list_entries(self._board(), extra_paths=paths)
                       if not workspace or entry["workspace"] == workspace]
            return {"cards": cards, "sessions": entries}
        if method == "card.delete":
            # A running card still occupies a pane: refuse and let the user stop it first (the panel uses card.stop).
            if any(p.card == params["task_id"] and p.alive()
                   for p in self.panes.values()):
                raise ValueError(f"Card {params['task_id']} is still running; stop it before deleting it.")
            from misaka.core.platform import cards as card_files
            from misaka.core.platform import tasks as db
            row = db.get(self._board(), params["task_id"])
            ok, msg = (card_files.remove(self._board(), row["workspace"], params["task_id"]) if row
                       else (False, f"Card not found: {params['task_id']}"))
            if not ok:
                raise ValueError(msg)
            return {"message": msg}
        if method == "pane.report_state":
            # The session's own word (herdr pane.report_agent): working / idle / blocked plus a
            # short message. Per-pane monotonic seq, so a report that arrives late cannot undo
            # a newer one. The panel trusts this over the screen heuristic.
            pane = self.panes.get(params["id"])
            if pane is None:
                raise ValueError(f"Pane not found: {params['id']}")
            state = params.get("state")
            if state not in ("working", "idle", "blocked"):
                raise ValueError(f"Unknown state: {state!r} (working, idle, or blocked)")
            seq = int(params.get("seq") or 0)
            if pane.reported and seq < pane.reported["seq"]:
                return {"ok": False, "stale": True}
            session_file = str(params.get("session") or "")
            if pane.card and session_file and os.path.isabs(session_file):
                from misaka.core.platform import tasks as db
                row = db.get(self._board(), pane.card)
                if row is not None and pane.generation is not None:
                    db.set_runtime(self._board(), pane.card, row["agent_id"], session_file,
                                   generation=pane.generation, claim_lock=pane.claim_lock)
            pane.reported = {"state": state, "message": str(params.get("message") or "")[:240],
                             "seq": seq,
                             # The session file this pane writes: the panel marks its list with it.
                             "session": str(params.get("session") or "")[:1024]}
            return {"ok": True}
        if method == "pane.explain":     # like herdr's `agent explain`: why the pane is busy/idle
            pane = self.panes.get(params["id"])
            if pane is None:
                raise ValueError(f"Pane not found: {params['id']}")
            return {"id": pane.id, "title": pane.title, "busy": _pane_busy(pane),
                    "why": _busy_reason(pane)}
        if method == "pane.focused":
            pane = self.panes.get(params["id"])
            if pane is None:
                raise ValueError(f"Pane not found: {params['id']}")
            if pane.card:
                from misaka.core.platform import tasks as db
                row = db.get(self._board(), pane.card)
                pane.seen_status = row["status"] if row else None
            return {"seen": True}
        if method == "card.stop":
            # The pane holding the claim is the attempt; another one is only showing the card.
            hosting = [p for p in self.panes.values() if p.card == params["task_id"]]
            pane = next((p for p in hosting if p.claim_lock), hosting[0] if hosting else None)
            if pane is None:
                if "expected_generation" in params:
                    return {"stopped": False, "missing": True}
                raise ValueError(f"Card {params['task_id']} is not running in a pane.")
            from misaka.core.platform import tasks as db
            with db.write_txn(self._board()):
                expected_owner = params.get("research_owner")
                if expected_owner is not None:
                    from misaka.core.research import runs
                    run = runs.get(self._board(), expected_owner["run_id"])
                    node = (runs.node(self._board(), expected_owner["node_id"])
                            if expected_owner.get("node_id") is not None else None)
                    if (run is None or run["driver_lock"] != expected_owner["driver_lock"]
                            or (expected_owner.get("node_id") is not None
                                and (node is None or node["runner_key"] != expected_owner["runner_key"]))):
                        return {"stopped": False, "stale": True}
                if "expected_generation" in params:
                    row = db.get(self._board(), pane.card)
                    expected = params["expected_generation"]
                    lock = params.get("expected_claim_lock")
                    if (row is None or row["generation"] != expected or pane.generation != expected
                            or row["claim_lock"] != lock or pane.claim_lock != lock):
                        return {"stopped": False, "stale": True}
                from misaka.core.platform import processes
                # Mid-takeover a pane has no program of its own; the stop still stands, and
                # the attempt that was starting finds its pane gone and puts the claim back.
                pid = pane.proc.pid if pane.proc else None
                identity = processes.identity(pid) if pid else None
                self.close(pane.id, expected_generation=params.get("expected_generation"))
                return {"stopped": True, "pid": pid, "identity": identity}
        if method == "layout.get":       # the daemon owns the layout (herdr server model)
            return {"spaces": self.spaces, "revision": self.layout_revision}
        if method == "layout.set":       # client-side edits: a dragged divider, a renamed tab or space
            return self.apply_layout(params.get("spaces") or [], params.get("revision"))
        if method == "pane.create":
            pane = self.create(params["argv"], params.get("cwd"),
                               title=params.get("title", ""),
                               env=params.get("env"),      # the panel passes through theme etc.
                               place=params.get("place"))
            return {"pane_id": pane.id, "pid": pane.proc.pid}
        if method == "pane.run_card":
            # Hosting a card waits for the pane it takes over; `_serve_client` awaits it.
            return self._hosted(self.run_card(params["task_id"], place=params.get("place")))
        if method == "pane.open_card_session":
            return self._hosted(self.open_card_session(params["task_id"], place=params.get("place")))
        if method == "pane.continue_card":
            return self._hosted(self.continue_card(
                params["task_id"], params["say"], place=params.get("place"),
                expected_generation=params.get("expected_generation"),
            ))
        if method == "pane.read":
            pane = self.panes.get(params["id"])
            if pane is None:
                raise ValueError(f"Pane not found: {params['id']}")
            text = pane.buf.decode("utf-8", errors="replace")
            if params.get("strip"):
                text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|[\r\x00]", "", text)
            lines = params.get("lines")
            if lines:
                text = "\n".join(text.splitlines()[-int(lines):])
            return {"text": text, "alive": pane.alive()}
        if method == "pane.send":
            pane = self.panes.get(params.get("id") or "") or next(
                (p for p in self.panes.values()
                 if p.card and p.card == params.get("card")), None)
            if pane is None or pane.fd is None:
                raise ValueError(f"Pane is missing or has exited: {params.get('id') or params.get('card')}")
            expected_generation = params.get("expected_generation")
            self._check_generation(pane, expected_generation)
            if expected_generation is not None and pane.card:
                from misaka.core.platform import tasks as db
                row = db.get(self._board(), pane.card)
                if row is None or int(row["generation"]) != int(expected_generation):
                    generation = row["generation"] if row is not None else "missing"
                    raise ValueError(
                        f"Card {pane.card} is generation {generation}; "
                        f"expected generation {int(expected_generation)}."
                    )
                if row["status"] != "running" or row["claim_lock"] != pane.claim_lock:
                    raise ValueError(
                        f"Pane {pane.id} no longer owns running card {pane.card} "
                        f"at expected generation {int(expected_generation)}."
                    )
            # Text and its Enter are one message: a cut between them would leave the text
            # sitting unsent in the program's editor (the 08:48 onboarding notes).
            data = params["text"].encode() + (b"\r" if params.get("enter") else b"")
            return self._deliver(pane, data, timeout=SEND_TIMEOUT, drop_on_timeout=True)
        if method == "pane.input":
            pane = self.panes.get(params["id"])
            if pane is None or pane.fd is None:
                raise ValueError(f"Pane is missing or has exited: {params['id']}")
            self._leave_scrollback(pane)   # a keystroke into the app returns to the live screen, as in a terminal
            return self._deliver(pane, base64.b64decode(params["data"]), timeout=INPUT_TIMEOUT, drop_on_timeout=False)
        if method == "pane.resize":
            pane = self.panes.get(params["id"])
            if pane is None or pane.fd is None:
                raise ValueError(f"Pane is missing or has exited: {params['id']}")
            return {"resized": self._request_resize(pane, int(params["rows"]), int(params["cols"]))}
        if method == "panes.resize":
            # One round trip for a whole layout (herdr's client hands the server the layout
            # and the server sizes every pane): {"sizes": {pane_id: [rows, cols]}}.
            resized = {}
            for pane_id, (rows, cols) in params["sizes"].items():
                pane = self.panes.get(pane_id)
                if pane is None or pane.fd is None:
                    continue
                resized[pane_id] = self._request_resize(pane, int(rows), int(cols))
            return {"resized": resized}
        if method == "pane.screen":
            pane = self.panes.get(params["id"])
            if pane is None:
                raise ValueError(f"Pane not found: {params['id']}")
            if (pane.sync_until is not None or pane.resize_hold is not None) and pane.shown is not None:
                # A frame is being painted, or a resize is waiting for the program to repaint:
                # the viewer keeps seeing the last complete frame (ghostty's renderer does not
                # draw inside a synchronized block, and its reflow of the old content is the
                # photographed spill/blank rows). Answering from the live buffer here showed
                # the transcript's first rows -- pi repaints from the top -- on every
                # divider-drag step, and cleared the dirty set so the rows painted before the
                # request never reached the panel afterwards.
                return dict(pane.shown, held=True)
            return _shown(pane)
        if method == "pane.extract":
            # The text between two absolute (row, col) points: the panel's selection lives in
            # screen-buffer coordinates (herdr selection.rs), so it survives scrolling and
            # can span rows that are no longer on screen.
            pane = self.panes.get(params["id"])
            if pane is None:
                raise ValueError(f"Pane not found: {params['id']}")
            start, end = params["start"], params["end"]
            return {"text": extract_text(pane, (int(start[0]), int(start[1])),
                                         (int(end[0]), int(end[1])))}
        if method == "pane.scroll":
            # Negative delta scrolls up; positive scrolls down; "bottom" jumps to the end.
            pane = self.panes.get(params["id"])
            if pane is None:
                raise ValueError(f"Pane not found: {params['id']}")
            _scroll_pane(pane, params.get("delta", 0), params.get("to"))
            if (pane.sync_until is not None or pane.resize_hold is not None) and pane.shown is not None:
                # Mid-frame, or awaiting a repaint after a resize: the new viewport is shown
                # when the frame completes.
                pane.full_frame = True
                return {"scroll": pane.shown["scroll"], "rows": pane.shown["rows"], "held": True}
            shown = _shown(pane)
            return {"scroll": shown["scroll"], "rows": shown["rows"]}
        if method == "pane.close":
            self.close(params["id"], expected_generation=params.get("expected_generation"))
            return {"closed": True}
        if method == "server.stop":
            self._stopping.set()
            return {"stopping": True}
        raise ValueError(f"Unknown method: {method}")

    async def _read_line(self, reader) -> bytes:
        """One newline-terminated request, ``b""`` at EOF, ValueError once per over-long line.

        `StreamReader.readline` cannot be used here. When a line outruns the reader's limit it
        clears the whole buffer *before* the newline has necessarily arrived and raises, so the
        tail of that one line kept coming and was parsed as line after line of junk: a single
        70 KB request was answered with N error frames (audit 2026-09-02, ui-panel-01). Going
        through `readuntil` keeps the accounting: every byte of the over-long line is dropped,
        up to and including its newline, and the requests queued behind it are left alone."""

        dropped = 0
        while True:
            try:
                line = await reader.readuntil(b"\n")
            except asyncio.IncompleteReadError as error:   # EOF with no newline in sight
                if dropped:
                    raise ValueError(
                        f"Request line too long: {dropped + len(error.partial)} bytes with no "
                        "newline before end of stream.") from None
                return error.partial
            except asyncio.LimitOverrunError as error:
                # `consumed` bytes are known to hold no newline. Zero means the newline *is*
                # buffered but sits past the limit, which by construction leaves the first
                # `_read_limit` bytes newline-free -- so that much is safe to drop too.
                dropped += len(await reader.readexactly(error.consumed or self._read_limit))
                continue
            if dropped:
                raise ValueError(
                    f"Request line too long: {dropped + len(line)} bytes discarded.") from None
            return line

    async def _serve_client(self, reader, writer):
        self._clients.add(writer)
        try:
            while not self._stopping.is_set():
                notify = False        # set once the line parses; a fire-and-forget request wants no reply
                request_id = None     # the id the reply answers to, error replies included
                try:
                    # The read belongs inside the try: on a line past the reader's limit it
                    # raises ValueError, and letting that out killed the connection without a
                    # reply -- for the panel's connection that meant the finally below read
                    # "the panel left" and run() SIGTERMed every pane. _read_line also leaves
                    # the stream framed, so one over-long request costs exactly one error frame.
                    line = await self._read_line(reader)
                    if not line:
                        break
                    req = json.loads(line)
                    request_id = req.get("id")
                    notify = request_id is None      # a fire-and-forget request expects no reply (drag resizes)
                    method = req.get("method", "")
                    if method == "pane.attach":   # subscription stream; "*" = screen events from every pane (tiled panel)
                        wanted = (req.get("params") or {}).get("id", "")
                        if wanted != "*" and wanted not in self.panes:
                            raise ValueError("Pane not found.")
                        if wanted == "*" and self._panels:
                            raise ValueError("A panel is already attached; one panel at a time.")
                        self._attached[writer] = wanted
                        if wanted == "*":
                            self._panels.add(writer)
                        result = {"attached": wanted}
                    else:
                        result = self._api(method, req.get("params") or {})
                        if asyncio.iscoroutine(result):   # delivery waits for the pane; nothing else does
                            result = await result
                    out = {"id": request_id, "result": result}
                except Exception as error:  # noqa: BLE001 - one failed request (or one unreadable line) must not drop the connection
                    out = {"id": request_id, "error": f"{type(error).__name__}: {error}"}
                if notify:                    # fire-and-forget (id=None): apply it, send nothing back
                    continue
                writer.write((json.dumps(out, ensure_ascii=False) + "\n").encode())
                try:
                    await writer.drain()
                except ConnectionError:
                    break
        finally:
            self._clients.discard(writer)
            self._attached.pop(writer, None)
            if writer in self._panels:
                # The panel left (detach key, closed terminal window, crash, kill): nothing keeps
                # running behind the user's back. Last Order and the Sisters shut down with it;
                # run() closes every pane on the way out. A daemon that never had a panel (pure
                # `misaka net ...` use) is unaffected and still ends with `misaka net stop`.
                self._panels.discard(writer)
                if not self._panels:
                    self._stopping.set()
            writer.close()

    async def run(self):
        # `pane.create` takes argv/cwd/env from whoever connects, so the socket is a
        # code-execution door: it must never be world-connectable, not even for the moment
        # between bind and chmod (audit 2026-09-02, ui-panel-05). umask covers the bind
        # itself; the directory is narrowed the way session_manager/auth_storage do it, and
        # the chmod stays as a backstop for a filesystem that ignores the mode bits.
        home.private_dir(os.path.dirname(self.sock_path))
        # Probe -> unlink -> bind is three steps, and two panels cold-starting in two
        # terminals interleave them: the loser either crashes on a FileNotFoundError from
        # `os.unlink` or unlinks the winner's live socket and binds over it, leaving a daemon
        # that believes it is serving and never sees another connection (audit 2026-09-02,
        # ui-panel-09). The flock serialises the whole sequence, so the second daemon runs its
        # probe against a socket that is already listening and exits with the honest message.
        # The kernel drops the lock when the process dies, so a crash cannot wedge startup.
        # Polled rather than blocking: the holder keeps it only across a probe and a bind, and
        # a blocking flock would freeze this event loop (and deadlock two daemons sharing one).
        lock_fd = os.open(self.sock_path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            for _ in range(SINGLETON_LOCK_TRIES):
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    await asyncio.sleep(0.05)
            # Waited it out: fall through and let bind decide, which is where this stood
            # before the lock existed. Better a loud "address already in use" than a hang.
            # Singleton: if the socket answers, a daemon is already running; a stale socket is removed and recreated.
            if os.path.exists(self.sock_path):
                # herdr ipc.rs prepare_socket_path: a live listener refuses a second server;
                # refused / missing / timed out means a stale file to reclaim.
                probe = socket.socket(socket.AF_UNIX)
                probe.settimeout(1.0)
                try:
                    probe.connect(self.sock_path)
                    raise SystemExit("The daemon is already running (its socket answered).")
                except (ConnectionRefusedError, FileNotFoundError, TimeoutError, OSError):
                    pass
                finally:
                    probe.close()
                try:
                    os.unlink(self.sock_path)
                except FileNotFoundError:
                    pass
            # A request line is one whole JSON object -- `pane.send` carries arbitrary user text,
            # `layout.set` a whole space tree -- so asyncio's 64 KiB default is far too small.
            old_umask = os.umask(0o177)
            try:
                server = await asyncio.start_unix_server(self._serve_client, self.sock_path,
                                                         limit=self._read_limit)
            finally:
                os.umask(old_umask)
            os.chmod(self.sock_path, 0o600)
            info = os.stat(self.sock_path, follow_symlinks=False)
            self._socket_identity = (info.st_dev, info.st_ino)
        finally:
            os.close(lock_fd)   # releases the flock
        skipped = self.restore_snapshot()
        if skipped:
            print(
                f"{len(skipped)} card(s) were running before shutdown ({', '.join(skipped)}); "
                "not restarted automatically -- redispatch from the panel.",
                flush=True,
            )
        watcher = asyncio.create_task(self._watch_cards())
        await self._stopping.wait()
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        for pane_id in list(self.panes):
            try:
                self.close(pane_id)
            except ValueError:
                pass
        if self._reapers:      # all grace periods run in parallel; worst case ~2.5s, not 4s x panes
            await asyncio.gather(*list(self._reapers), return_exceptions=True)
        server.close()
        for writer in list(self._clients):   # close lingering connections or wait_closed never returns
            writer.close()
        await server.wait_closed()
        for con in (self._con, self._mcon):     # the long-lived board/mailbox handles
            try:
                if con is not None:
                    con.close()
            except Exception:  # noqa: BLE001, S110 - shutting down; a failed close changes nothing
                pass
        self._con = self._mcon = None
        # herdr leaves the socket file at shutdown (ipc.rs reclaim_name(false)): the next
        # daemon's startup probe reclaims a stale file, so no unlink can ever race a
        # successor that has already bound the same path.


def main():
    from misaka.config import env as env_file
    from misaka.config.engine import configure_logging
    home.ensure()
    configure_logging()      # the daemon's warnings belong in the log file, not net.sock.log (B6)
    env_file.load()          # panes inherit it; a panel started outside a shell still has its keys
    asyncio.run(Daemon().run())


if __name__ == "__main__":
    main()

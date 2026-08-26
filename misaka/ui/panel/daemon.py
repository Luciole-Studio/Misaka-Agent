"""Misaka Network daemon: the pseudo-terminal host where the Sisters live.

Process model (after herdr): this long-lived process owns every pane (PTY);
the panel and CLI are thin clients, and a disconnect only removes an observer.
The protocol is newline-delimited JSON (requests carry id/method/params).

Card panes: ``pane.run_card`` drives the board state machine -- claim a lease,
open a pane running an interactive session, watch for submission/timeout/exit,
then accept the submission, send the card back, or fail it. The daemon only owns
the process and never judges
its own work.
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
import unicodedata

import psutil
import pyte

from misaka.config import CFG
from misaka.ui.panel import (
    geometry as hui,  # layout.rs port: split_at / remove_pane / pane_ids
)

# Wire protocol version (strict equality, as in herdr). Bump it whenever *server
# behaviour* changes, not only method/event shapes: an unbumped behaviour change
# once let a stale daemon slip through the version gate.
PROTOCOL = 31   # 31: the daemon owns the layout (spaces/tabs/trees, herdr server model); pane.create takes `place`; no pane parent
RING_CAP = 256 * 1024          # output tail kept per pane
FRAME_SECONDS = 0.008          # coalescing window for dirty-row broadcasts (~120 fps)
SCROLLBACK_LINES = 2000        # scrollback history per pane
IDLE_QUIET_SECONDS = 1.0       # screen unchanged this long = idle (a static spinner glyph does not count)
CARD_POLL_SECONDS = 5.0        # card-pane polling interval
DEFAULT_ROWS, DEFAULT_COLS = 32, 120
CARD_SHELL = [sys.executable, "-m", "misaka", "card-shell"]   # tests may override


def _expand(path):
    return os.path.expanduser(path)


# pyte does not understand these modern terminal sequences and would leak them
# as visible characters; strip them before emulation.
_UNSUPPORTED = re.compile(
    rb"\x1b\[[<>=?][0-9;]*u"          # Kitty keyboard protocol
    rb"|\x1b\[>[0-9;]*[mn]"           # XTMODKEYS / modifyOtherKeys
    rb"|\x1b\[\?2026[hl]"             # Synchronized output
    rb"|\x1b\]8;[^\x07\x1b]*(?:\x07|\x1b\\)"   # OSC 8 hyperlink
)
_ALTSCREEN = re.compile(rb"\x1b\[\?(?:1049|1047|47)[hl]")
# Applications in a pane query the "terminal" and wait for a reply (pi's kitty
# negotiation ends on a DA sentinel). herdr answers from its embedded ghostty;
# here the emulated screen answers, and the query itself never reaches the screen.
_QUERY = re.compile(
    rb"\x1b\[(?P<da1>0?c)"                       # Primary device attributes
    rb"|\x1b\[>(?P<da2>0?c)"                     # Secondary device attributes
    rb"|\x1b\[(?P<dsr>6n)"                       # Cursor position report
    rb"|\x1b\](?P<osc>1[01]);\?(?:\x07|\x1b\\)"  # OSC 10/11 color query
)


def _answer_queries(pane, data):
    """Answer terminal queries on behalf of the emulated screen; the queries themselves are dropped."""

    def reply(match):
        group = match.lastgroup
        if group == "da1":
            answer = b"\x1b[?62;22c"
        elif group == "da2":
            answer = b"\x1b[>1;10;0c"
        elif group == "dsr":
            answer = (f"\x1b[{pane.screen.cursor.y + 1};{pane.screen.cursor.x + 1}R").encode()
        else:
            # OSC 10/11 foreground/background: follow the pane's theme variant so
            # full-screen apps in a light terminal see a light background.
            light = pane.theme == "light"
            if match.group("osc") == b"11":
                color = b"faf4/f4f4/f6f6" if light else b"1e1e/1e1e/1e1e"
            else:
                color = b"3320/2020/2828" if light else b"e6e6/e6e6/e6e6"
            answer = b"\x1b]" + match.group("osc") + b";rgb:" + color + b"\x07"
        try:
            if pane.fd is not None:
                os.write(pane.fd, answer)
        except OSError:
            pass
        return b""

    return _QUERY.sub(reply, data)
# The tail may be a truncated escape sequence: carry it over to the next read
# (capped at 64 bytes).
_PARTIAL_ESC = re.compile(rb"\x1b(?:\[[0-9;<>=?]*|\][^\x07\x1b]*)?$")

_FG = {"black": 30, "red": 31, "green": 32, "brown": 33, "blue": 34, "magenta": 35,
       "cyan": 36, "white": 37, "brightblack": 90, "brightred": 91, "brightgreen": 92,
       "brightbrown": 93, "brightblue": 94, "brightmagenta": 95, "brightcyan": 96,
       "brightwhite": 97}


def _render_row(screen, row):
    """Render one emulated screen row as an ANSI string (SGR only on attribute change, reset at end of row)."""
    line = screen.buffer[row]
    out, last, skip_stub = [], None, False
    for col in range(screen.columns):
        if skip_stub:  # wide (CJK) characters span two columns; pyte leaves a placeholder cell after them
            skip_stub = False
            continue
        ch = line[col]
        attrs = (ch.fg, ch.bg, ch.bold, ch.reverse, ch.underscore)
        if attrs != last:
            last = attrs
            sgr = ["0"]
            if ch.bold:
                sgr.append("1")
            if ch.underscore:
                sgr.append("4")
            if ch.reverse:
                sgr.append("7")
            if ch.fg in _FG:
                sgr.append(str(_FG[ch.fg]))
            elif len(str(ch.fg)) == 6:      # hex truecolor
                try:
                    r, g, b = (int(str(ch.fg)[i:i + 2], 16) for i in (0, 2, 4))
                    sgr.append(f"38;2;{r};{g};{b}")
                except ValueError:
                    pass
            if ch.bg in _FG:
                sgr.append(str(_FG[ch.bg] + 10))
            elif len(str(ch.bg)) == 6:
                try:
                    r, g, b = (int(str(ch.bg)[i:i + 2], 16) for i in (0, 2, 4))
                    sgr.append(f"48;2;{r};{g};{b}")
                except ValueError:
                    pass
            out.append("\x1b[" + ";".join(sgr) + "m")
        data = ch.data or " "
        out.append(data)
        if data and unicodedata.east_asian_width(data[0]) in ("W", "F"):
            skip_stub = True
    out.append("\x1b[0m")
    return "".join(out)


# Spinner frames: braille and geometric dots only. Never add ASCII such as |/-\ --
# the / in paths and the ubiquitous - would make every screen look busy.
_SPINNER_CHARS = set("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⣻⢿⡿⣟⣯⣷◐◓◑◒◴◷◶◵")
_SHELLS = {"sh", "bash", "zsh", "fish", "dash", "ksh", "csh", "tcsh", "-zsh", "-bash"}
# A command typed by hand in a pane's shell counts as a transient ally only if it
# is on the allow-list; otherwise vim/htop would show up in the roster too. The
# single source of truth is ~/.misaka/allies.json (no env knob, by user decision):
# seeded on first use, edited in place, reloaded on mtime change without a daemon
# restart. Allies launched by Last Order are not subject to the list.
ALLY_SEED = ("claude", "codex")
_allies_cache = {"path": None, "mtime": None, "commands": frozenset(ALLY_SEED)}


def ally_commands():
    """Return the hand-launched ally allow-list.

    Missing file: write the seed and use it. Readable file: use it as-is (an
    empty list means nothing is recognised). Bad JSON: fall back to the seed
    but never overwrite the user's file -- it may be mid-edit.
    """
    path = _expand(CFG["allies"])
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.{secrets.token_hex(4)}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"commands": list(ALLY_SEED)}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            return frozenset(ALLY_SEED)   # cannot write (read-only disk etc.): use the seed, do not block panes
    if _allies_cache["path"] == path and _allies_cache["mtime"] == mtime:
        return _allies_cache["commands"]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        commands = frozenset(
            c.strip() for c in (data.get("commands") or [])
            if isinstance(c, str) and c.strip())
    except (OSError, ValueError, AttributeError):
        commands = frozenset(ALLY_SEED)
    _allies_cache.update(path=path, mtime=mtime, commands=commands)
    return commands


def _looks_like_command(name):
    """Is this a command name? Pure digits/version strings ("2.1.228") are not; apps set those as their process name."""
    return bool(name) and not re.fullmatch(r"[\d.]+", name)


def _foreground(pane):
    """Return what is running in the pane's foreground right now (name and command line).

    Last Order uses this to notice that the user started e.g. codex in a shell.
    No vendor list here: report the process name and let Last Order decide
    whether it is an agent.
    """
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
        return {"name": shown, "proc_name": name, "pid": fg, "cwd": cwd,
                "cmdline": " ".join(argv)[:200] if argv else name,
                "is_shell": shown.lstrip("-") in _SHELLS or name.lstrip("-") in _SHELLS}
    except (OSError, psutil.Error):
        return None


def _ally_name(pane):
    """Name of the third-party agent running in this pane, or None -- the test for a transient ally.

    Two sources: an ally launched by Last Order (``pane.ally`` is set; any
    command qualifies), or a command the user typed into a MISAKA pane's shell
    (only names on the allies.json allow-list, see ``ally_commands()``).
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
    title = (getattr(pane.screen, "title", "") or "").strip()
    low = title.lower()
    if any(sign in low for sign in _ALLY_TITLE_BLOCKED):
        return "blocked", f"its terminal title asks for you ({title!r})"
    if any(ch in _SPINNER_CHARS for ch in title):
        return "working", f"a spinner is running in its terminal title ({title!r})"
    try:                     # herdr's bottom_non_empty_lines region: trailing blank rows say nothing
        lines = [row.strip() for row in pane.screen.display if row.strip()]
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
        for line in pane.screen.display:
            if any(ch in _SPINNER_CHARS for ch in line):
                return True
    except Exception:  # noqa: BLE001
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
    spins = sorted({ch for line in pane.screen.display for ch in line
                    if ch in _SPINNER_CHARS})
    if spins:
        return f"Agent pane: active spinner {''.join(spins)} detected; classified as busy."
    return f"Agent pane: last output was {quiet:.1f}s ago with no spinner; classified as idle."


def _scroll_metrics(pane):
    """Scrollback position, total scrollable lines, and viewport rows (herdr's ScrollMetrics).

    pyte's HistoryScreen keeps history in two deques, ``top`` and ``bottom``;
    scrolling back moves lines from top to bottom, so the offset from the
    bottom is ``len(bottom)``.
    """
    screen = pane.screen
    top = len(getattr(screen, "history", None).top) if hasattr(screen, "history") else 0
    bottom = len(screen.history.bottom) if hasattr(screen, "history") else 0
    return {"offset_from_bottom": bottom,
            "max_offset_from_bottom": top + bottom,
            "viewport_rows": screen.lines}


def _scroll_pane(pane, delta=0, to=None):
    """Scroll by whole lines. pyte pages by ratio*lines; the screen is built with ratio=1/lines, so one page is one line."""
    screen = pane.screen
    if not hasattr(screen, "history"):
        return
    if to == "bottom":
        while screen.history.bottom:
            screen.next_page()
        return
    step = screen.next_page if delta > 0 else screen.prev_page
    for _ in range(abs(int(delta))):
        before = (len(screen.history.top), len(screen.history.bottom))
        step()
        if (len(screen.history.top), len(screen.history.bottom)) == before:
            break   # reached the end
    screen.dirty.update(range(screen.lines))


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
        "alt_screen",
        "argv",
        "buf",
        "card",
        "carry",
        "claim_lock",
        "cwd",
        "deadline",
        "exit_code",
        "fd",
        "flush",
        "generation",
        "id",
        "last_heartbeat",
        "last_output",
        "proc",
        "reported",
        "screen",
        "seen_status",
        "sent_cursor",
        "started_at",
        "stream",
        "submitted",
        "theme",
        "title",
    )

    def __init__(self, pane_id, title, argv, cwd, card=None):
        self.id, self.title, self.argv, self.cwd, self.card = pane_id, title, argv, cwd, card
        self.reported = None          # the session's own word: {"state", "message", "seq"} (herdr hook authority); None = guess from the screen
        self.claim_lock = self.generation = self.deadline = None
        self.proc = self.fd = self.exit_code = None
        self.buf = bytearray()
        self.started_at = int(time.time())
        self.submitted = False
        self.seen_status = None       # board status seen while focused ("finished but not yet looked at")
        # HistoryScreen keeps scrollback (the scrollbar needs it); ratio=1/rows makes paging line-granular
        self.screen = pyte.HistoryScreen(DEFAULT_COLS, DEFAULT_ROWS,
                                         history=SCROLLBACK_LINES, ratio=1 / DEFAULT_ROWS)
        self.stream = pyte.ByteStream(self.screen)
        self.carry = b""              # truncated escape sequence carried to the next read
        self.alt_screen = False       # in the alternate screen (full-screen app): no scrollbar
        self.theme = "dark"           # theme variant (set from env at create; OSC replies follow it)
        self.ally = None              # ally label (only for third-party agent panes started by Last Order)
        self.flush = None             # pending frame-coalescing timer
        self.sent_cursor = None       # last cursor broadcast (position + visibility)
        self.last_output = 0.0        # time of the last output (is the screen still moving?)
        self.last_heartbeat = 0.0     # last lease heartbeat for a card pane

    def alive(self):
        return self.proc is not None and self.proc.poll() is None


class Daemon:
    def __init__(self, sock_path=None, snapshot_path=None):
        self.sock_path = _expand(sock_path or CFG["net_sock"])
        self.snapshot_path = _expand(snapshot_path or CFG["net_snapshot"])
        self.panes: dict[str, Pane] = {}
        self._reapers = set()          # background kill-escalation tasks (close never blocks)
        self._seq = 0
        # The layout, as in herdr: the server holds spaces -> tabs -> split trees and seats every
        # pane at creation; clients only draw it. {"id","folder","name","tabs":[{"name","tree"}]}
        self.spaces: list[dict] = []
        self._space_seq = 0
        self.layout_revision = 0    # bumps on every layout change; clients edit against it
        self._theme = "dark"        # session theme variant; updated when the panel creates a pane with MISAKA_THEME
        self._con = None            # board connection, opened on the first card run
        self._attached: dict[asyncio.StreamWriter, str] = {}   # subscribers: connection -> pane id
        self._panels: set[asyncio.StreamWriter] = set()        # panels (attach "*"): when the last one leaves, so do we
        self._clients: set[asyncio.StreamWriter] = set()
        self._stopping = asyncio.Event()

    # ── Panes ─────────────────────────────────────────────

    def _spawn(self, pane: Pane, env=None):
        import pty

        def _become_session_leader():
            # A proper controlling terminal (as tmux/herdr do); without it ^C/^Z cannot reach the job.
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ,
                    struct.pack("HHHH", DEFAULT_ROWS, DEFAULT_COLS, 0, 0))
        pane.proc = subprocess.Popen(
            pane.argv, cwd=pane.cwd, stdin=slave, stdout=slave, stderr=slave,
            preexec_fn=_become_session_leader,
            env={**os.environ, **(env or {}), "TERM": "xterm-256color",
                 # The pane's terminal is OUR pyte relay, which passes 24-bit SGR through
                 # untouched -- without this hint the engine pre-bakes every theme colour
                 # down to the 256 palette (rose #cb3862 -> 167 salmon).
                 "COLORTERM": "truecolor",
                 "MISAKA_NET_PANE": pane.id},
        )
        os.close(slave)
        os.set_blocking(master, False)
        pane.fd = master
        asyncio.get_running_loop().add_reader(master, self._pump, pane)

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
        data = pane.carry + chunk
        pane.carry = b""
        tail = _PARTIAL_ESC.search(data)
        if tail and len(data) - tail.start() <= 64:
            pane.carry, data = data[tail.start():], data[: tail.start()]
        data = _answer_queries(pane, data)
        data = _UNSUPPORTED.sub(b"", data)
        try:
            # pyte does not know alternate-screen switches: treat each as a clear
            # (full-screen apps repaint anyway) and remember whether we are in the
            # alternate screen, where no scrollbar is shown.
            pieces = _ALTSCREEN.split(data)
            for match in _ALTSCREEN.finditer(data):
                pane.alt_screen = match.group().endswith(b"h")
            for index, piece in enumerate(pieces):
                if index:
                    pane.screen.reset()
                if piece:
                    pane.stream.feed(piece)
        except Exception:  # noqa: BLE001 - a sequence the emulator cannot digest must not take the pane down
            pass
        if self._attached and pane.flush is None:
            # Coalesce per frame instead of pushing every read. The PTY splits one
            # repaint into several chunks with the cursor parked mid-way (e.g. end
            # of line); pushing chunks makes clients place the cursor at those
            # intermediate spots, so IME candidate windows drift to the right edge.
            # Sending a whole frame means clients see the end-of-frame state.
            pane.flush = asyncio.get_running_loop().call_later(
                FRAME_SECONDS, self._flush, pane)

    def _flush(self, pane: Pane):
        pane.flush = None
        if not self._attached:
            pane.screen.dirty.clear()
            return
        dirty = sorted(pane.screen.dirty)
        pane.screen.dirty.clear()
        cursor = ([pane.screen.cursor.x, pane.screen.cursor.y],
                  bool(pane.screen.cursor.hidden))
        # Cursor moves must be broadcast too: pyte's dirty set only tracks
        # content, and moving the cursor does not dirty a row.
        if not dirty and cursor == pane.sent_cursor:
            return
        pane.sent_cursor = cursor
        # Include scroll metrics: a clear wipes scrollback and the panel must collapse its scrollbar.
        self._broadcast(pane.id, {
            "event": "screen", "id": pane.id,
            "rows": {str(r): _render_row(pane.screen, r)
                     for r in dirty if r < pane.screen.lines},
            "cursor": cursor[0],
            "cursor_hidden": cursor[1],
            "scroll": _scroll_metrics(pane),
            "alt_screen": pane.alt_screen,
        })

    def _broadcast(self, pane_id, payload):
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
        for writer, wanted in list(self._attached.items()):
            if wanted not in (pane_id, "*") or writer.transport.is_closing():
                continue
            try:
                writer.write(line)
                # Drop stalled subscribers before their write buffers consume unbounded memory.
                if writer.transport.get_write_buffer_size() > 4 * 1024 * 1024:
                    self._attached.pop(writer, None)
            except Exception:  # noqa: BLE001 - remove dead subscribers
                self._attached.pop(writer, None)

    def create(self, argv, cwd, *, title="", card=None, env=None, place=None) -> Pane:
        self._seq += 1
        pane = Pane(f"p{self._seq}", title or (argv[0] if argv else ""), list(argv),
                    cwd or os.getcwd(), card=card)
        if env and env.get("MISAKA_THEME") in ("dark", "light"):
            pane.theme = self._theme = env["MISAKA_THEME"]   # remember the session variant
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
            for tab in space["tabs"]:
                if pane_id in hui.pane_ids(hui.from_jsonable(tab["tree"])):
                    return space, tab
        return None

    def _seat(self, pane, place):
        """``{"split": pane_id[, "direction": "h"|"v"]}`` splits that pane in its own tab (herdr
        split_at, 50/50); ``{"tab": pane_id}`` opens a new tab in the space holding that pane;
        anything else (or an unknown pane) opens a new space in the pane's folder. ``name``
        names a new tab (herdr custom_name); a new tab is otherwise named after its pane."""
        ref = place.get("split") or place.get("tab")
        at = self._tab_holding(ref) if ref else None
        name = place.get("name") or pane.title
        if at and place.get("split"):
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
            for tab in space["tabs"]:
                tree = hui.from_jsonable(tab["tree"])
                if tree and pane_id in hui.pane_ids(tree):
                    tree = hui.remove_pane(tree, pane_id)
                    tab["tree"] = hui.to_jsonable(tree) if tree else None
            space["tabs"] = [tab for tab in space["tabs"] if tab["tree"]]
        self.spaces = [space for space in self.spaces if space["tabs"]]
        self.layout_revision += 1

    def apply_layout(self, spaces, revision=None):
        """A client-side edit of the layout. It must be based on the revision the client last saw
        (an edit racing another client's is refused, not merged blindly), and it can never make
        a live pane disappear: a pane the edit omits is seated again."""
        if revision is not None and int(revision) != self.layout_revision:
            raise ValueError(f"stale layout (revision {revision}, current {self.layout_revision}); reload it first")
        incoming = [space for space in spaces if space.get("tabs")]
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
        for _ in range(40):                      # 2s grace for engines to save state
            if proc.poll() is not None:
                return
            await asyncio.sleep(0.05)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        while proc.poll() is None:
            await asyncio.sleep(0.05)

    def close(self, pane_id):
        pane = self.panes.pop(pane_id, None)
        if pane is None:
            raise ValueError(f"Pane not found: {pane_id}")
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
            from misaka.platform import tasks as db
            self._con = db.connect(_expand(CFG["db"]))
        return self._con

    def _card_workspace(self, row):
        """The card's folder is its project: a pane always runs there, and a folder that is
        gone is refused rather than recreated somewhere the user deleted it."""
        from misaka.platform import tasks as db
        workspace = db.workspace_for(row)
        if not os.path.isdir(workspace):
            raise ValueError(f"Card {row['id']}: its folder {workspace} no longer exists.")
        return workspace

    def _card_env(self, row):
        from misaka.platform import tasks as db
        return {
            "MISAKA_THEME": self._theme,    # card panes follow the session theme
            # misaka commands inside the pane (e.g. an ally's `misaka tell`) must use
            # the daemon's databases, or messages land in a different messages.db
            # and Last Order never sees them.
            "MISAKA_DB": _expand(CFG["db"]),
            "MISAKA_MESSAGES": _expand(CFG["messages_db"]),
            "MISAKA_TASKS": _expand(CFG["tasks_root"]),
            "MISAKA_TASK_DIR": db.task_state_dir(row["id"]),
            "MISAKA_TASK_OUTPUT_DIR": str(row["output_dir"] or db.workspace_for(row)),
        }

    def _settled_card_with_session(self, task_id):
        """A card whose session can be reopened: not live in a pane, with a saved transcript."""
        from misaka.network.sister_runtime import ACTIVE_BOARD_STATUSES
        from misaka.platform import tasks as db
        row = db.get(self._board(), task_id)
        if row is None:
            raise ValueError(f"Card not found: {task_id}")
        if row["status"] in ACTIVE_BOARD_STATUSES:
            raise ValueError(f"Card {task_id} is still {row['status']}; steer its running pane instead.")
        session = os.path.join(db.task_state_dir(task_id), "session")
        if not (os.path.isdir(session) and any(n.endswith(".jsonl") for n in os.listdir(session))):
            raise ValueError(f"Card {task_id} has no saved session to reopen.")
        return row

    def open_card_session(self, task_id, place=None) -> Pane:
        """Reopen a card's saved session to look at it: no claim, no contract, no model turn.
        The panel uses it for a click on a card session, Last Order to bring a Sister back into
        view. A turn typed into such a pane is not an attempt: without a claim nothing settles
        the card, and a report it writes carries a stale generation and is ignored."""
        row = self._settled_card_with_session(task_id)
        pane = self.create([*CARD_SHELL, task_id, "--resume"], self._card_workspace(row),
                           title=f"{row['assignee']}·{task_id}", card=task_id,
                           env=self._card_env(row), place=place)
        self._save_snapshot()
        return pane

    def continue_card(self, task_id, say, place=None) -> Pane:
        """Continue a settled card with a new model turn: the same ``claim_resume`` as the
        in-process Sister runtime (a new generation under our lock), after which the card
        shell's Supervisor settles the card exactly like a first run."""
        from misaka.platform import admission
        from misaka.platform import tasks as db
        row = self._settled_card_with_session(task_id)
        con = self._board()
        lock = f"net:{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"
        host_cap, assignee_cap = admission.limits()
        if not db.claim_resume(con, task_id, lock, os.getpid(),
                               ttl_seconds=max(1800, int(row["timeout_seconds"]) + 60),
                               expected_generation=int(row["generation"]),
                               host_cap=host_cap, assignee_cap=assignee_cap):
            raise ValueError(f"Card {task_id} could not be claimed for continuation; it changed "
                             "underneath, or the host is at capacity.")
        row = db.get(con, task_id)
        generation = int(row["generation"])
        return self._host_card(
            con, row, lock, generation, [*CARD_SHELL, task_id, "--resume", "--say", say], place,
            undo=lambda: db.block_task(con, task_id, "transient",
                                       "the pane could not be started; continue the card again",
                                       generation=generation, claim_lock=lock),
            event="continued")

    def run_card(self, task_id, place=None) -> Pane:
        from misaka.platform import admission
        from misaka.platform import tasks as db

        con = self._board()
        row = db.get(con, task_id)
        if row is None:
            raise ValueError(f"Card not found: {task_id}")
        if row["status"] != "ready":
            raise ValueError(f"Card {task_id} is not ready (current status: {row['status']}).")
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
                        ttl_seconds=max(1800, int(row["timeout_seconds"]) + 60),
                        generation=generation, pid=os.getpid(),
                        host_cap=host_cap, assignee_cap=assignee_cap):
            raise ValueError(f"Card {task_id} was claimed by another dispatcher.")
        undo = lambda: db.back_to_ready(con, task_id, generation=generation, claim_lock=lock)
        try:
            workspace = self._card_workspace(row)
            env = None
            if executor is None:
                argv = [*CARD_SHELL, task_id]
            else:  # ally: the card contract is the prompt, run one non-interactive turn
                from misaka.extensions.last_order.ally import runner as ally_runner
                from misaka.platform import cards
                task = dict(row)
                task["_attachments"] = cards.attachment_list(workspace, task_id, workspace=workspace)
                argv = ally_runner.build_argv(executor, ally_runner.card_prompt(task))
                env = {"MISAKA_ALLY": row["assignee"]}
        except BaseException:
            undo()
            raise
        return self._host_card(con, row, lock, generation, argv, place, undo=undo, env=env)

    def _host_card(self, con, row, lock, generation, argv, place, *, undo, env=None, event="claimed"):
        """Host a claimed card in a pane. The pane's environment carries the claim so the card
        drives itself (card_shell.Supervisor); ``undo`` releases the claim when no pane starts."""
        from misaka.platform import processes as process_tree
        from misaka.platform import tasks as db
        task_id = row["id"]
        try:
            workspace = self._card_workspace(row)
            pane = self.create(argv, workspace, title=f"{row['assignee']}·{task_id}", card=task_id,
                               env={**self._card_env(row),
                                    "MISAKA_USAGE_DB": _expand(CFG["db"]),
                                    "MISAKA_USAGE_TASK_ID": task_id,
                                    "MISAKA_USAGE_GENERATION": str(generation),
                                    "MISAKA_USAGE_CLAIM_LOCK": lock,
                                    "MISAKA_USAGE_TOKEN_CAP": str(int(CFG.get("token_cap") or 0)),
                                    **(env or {})},
                               place=place)
        except BaseException:
            undo()
            raise
        pane.claim_lock, pane.generation = lock, generation
        pane.deadline = time.time() + int(row["timeout_seconds"])
        # Owner = the pane's process group: if the daemon dies, reconcile reclaims by group identity (same marker as child.py).
        identity = process_tree.identity(pane.proc.pid)
        db.set_pid(con, task_id, pane.proc.pid,
                   worker_identity=f"process-group|{identity}" if identity else None,
                   generation=generation, claim_lock=lock)
        db.add_event(con, task_id, event,
                     {"lock": lock, "workspace": workspace, "pane": pane.id},
                     generation=generation)
        self._save_snapshot()
        return pane

    async def _watch_cards(self):
        """Watch ALLY card panes only: an external CLI cannot supervise itself, so the daemon
        keeps its heartbeat, deadline, and exit reconciliation. A MISAKA card drives itself
        (card_shell.Supervisor, phase 2); the daemon just hosts its pane."""
        from misaka.platform import tasks as db

        worker = None
        while not self._stopping.is_set():
            allies = [p for p in self.panes.values() if p.card and p.claim_lock and p.ally]
            if allies and worker is None:
                # ~1s of engine imports: loaded only when an ally card pane actually
                # exists, never at daemon startup (it blocked the first ping for 0.7s).
                from misaka.network import worker
            for pane in allies:
                con = self._board()
                row = db.get(con, pane.card)
                if row is None or int(row["generation"]) != pane.generation \
                        or row["claim_lock"] != pane.claim_lock:
                    pane.claim_lock = None          # ownership changed: observe only, never touch state
                    continue
                if not pane.alive():
                    if pane.ally:
                        # An ally neither submits nor reports; do both on its behalf at
                        # exit so the artifact reconciliation below is identical for
                        # both kinds of executor (the board is the single bus).
                        from misaka.extensions.last_order.ally import (
                            runner as ally_runner,
                        )
                        ally_runner.finish(
                            pane.cwd,
                            pane.exit_code if pane.exit_code is not None else -1,
                            pane.buf.decode("utf-8", errors="replace"),
                            assignee=pane.ally, task_id=pane.card,
                            output_dir=row["output_dir"], generation=pane.generation)
                    ok, report = worker.check_report(pane.cwd, con=con, task_id=pane.card, generation=pane.generation)
                    blocked_reason = (str(report)[len("blocked:"):].strip()
                                      if not ok and str(report).startswith("blocked:") else None)
                    if blocked_reason:
                        db.block_abandoned(
                            con, pane.card, "needs_input", blocked_reason,
                            generation=pane.generation, claim_lock=row["claim_lock"],
                            worker_pid=row["worker_pid"],
                            worker_identity=row["worker_identity"],
                            claim_expires=row["claim_expires"],
                        )
                    elif db.reclaim_abandoned(
                        con, pane.card, generation=pane.generation,
                        claim_lock=row["claim_lock"], worker_pid=row["worker_pid"],
                        worker_identity=row["worker_identity"],
                        claim_expires=row["claim_expires"], submitted=bool(ok),
                    ):
                        db.add_event(con, pane.card,
                                     "submitted" if ok else "reclaimed",
                                     {"summary": report["summary"],
                                      "artifacts": report.get("artifacts", [])}
                                     if ok else {"reason": str(report)[:500]},
                                     generation=pane.generation)
                    pane.claim_lock = None
                elif not pane.submitted:
                    if time.time() - pane.last_heartbeat >= 60:
                        if db.heartbeat(
                            con, pane.card, pane.claim_lock,
                            generation=pane.generation,
                            ttl_seconds=max(1800, int(row["timeout_seconds"]) + 60),
                        ):
                            pane.last_heartbeat = time.time()
                    ok, report = worker.check_report(pane.cwd, con=con, task_id=pane.card, generation=pane.generation)
                    if ok:
                        from misaka.network import dispatch
                        dispatch.accept(con, row, report, generation=pane.generation,
                                        claim_lock=pane.claim_lock, workspace=pane.cwd)
                        pane.submitted = True   # keep the pane after submission so a person can continue the chat
                    elif str(report).startswith("blocked:"):
                        db.block_task(
                            con, pane.card, "needs_input",
                            str(report)[len("blocked:"):].strip(),
                            generation=pane.generation, claim_lock=pane.claim_lock,
                        )
                        pane.submitted = True
                        pane.claim_lock = None
                    elif pane.deadline and time.time() > pane.deadline:
                        if db.add_event(con, pane.card, "failed",
                                        {"reason": "Sister timeout"},
                                        generation=pane.generation,
                                        claim_lock=pane.claim_lock) and db.mark_failed(
                                con, pane.card, generation=pane.generation,
                                claim_lock=pane.claim_lock):
                            pass
                        pane.claim_lock = None
                        try:
                            self.close(pane.id)
                        except ValueError:
                            pass
            try:
                await asyncio.wait_for(self._stopping.wait(), CARD_POLL_SECONDS)
            except TimeoutError:
                pass

    # ── Snapshot (shape only: argv, cwd, card -- never process state) ──

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
        if method == "panes.list":
            status, mail = {}, {}
            cards = [p.card for p in self.panes.values() if p.card]
            if cards:
                from misaka.platform import tasks as db
                con = self._board()
                for card in cards:
                    row = db.get(con, card)
                    status[card] = row["status"] if row else "?"
                try:
                    from misaka.network import messages
                    mcon = messages.connect()
                    mail = dict(mcon.execute(
                        "SELECT task_id, COUNT(*) FROM messages"
                        " WHERE delivered_at IS NULL AND task_id IS NOT NULL"
                        " GROUP BY task_id").fetchall())
                    mcon.close()
                except Exception:  # noqa: BLE001 - a broken mailbox must not take the panel down
                    mail = {}
            def row(p):
                ally = _ally_name(p)             # non-empty = a third-party agent runs in this pane
                state = _ally_state(p) if ally and p.alive() else None
                return {"id": p.id, "title": p.title, "card": p.card, "cwd": p.cwd,
                        "reported": p.reported,
                        "alive": p.alive(), "exit_code": p.exit_code,
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
            from misaka.platform import tasks as db
            workspace = (db.canonical_workspace(params["workspace"])
                         if params.get("workspace") else None)
            sql = "SELECT id,status,title,assignee,workspace,origin_session FROM tasks"
            args = []
            if workspace:
                sql += " WHERE workspace=?"
                args.append(workspace)
            cards = []
            for r in self._board().execute(sql + " ORDER BY created_at", args):
                sess = os.path.join(db.task_state_dir(r["id"]), "session")
                cards.append({"id": r["id"], "status": r["status"], "title": r["title"],
                              "assignee": r["assignee"], "workspace": r["workspace"],
                              "origin_session": r["origin_session"],
                              "has_session": os.path.isdir(sess) and bool(os.listdir(sess))})
            return {"cards": cards}
        if method == "card.delete":
            # A running card still occupies a pane: refuse and let the user stop it first (the panel uses card.stop).
            if any(p.card == params["task_id"] and p.alive()
                   for p in self.panes.values()):
                raise ValueError(f"Card {params['task_id']} is still running; stop it before deleting it.")
            from misaka.platform import cards as card_files
            from misaka.platform import tasks as db
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
                from misaka.platform import tasks as db
                row = db.get(self._board(), pane.card)
                pane.seen_status = row["status"] if row else None
            return {"seen": True}
        if method == "card.stop":
            pane = next((p for p in self.panes.values()
                         if p.card == params["task_id"]), None)
            if pane is None:
                raise ValueError(f"Card {params['task_id']} is not running in a pane.")
            from misaka.platform import tasks as db
            con = self._board()
            if pane.claim_lock:
                db.add_event(con, pane.card, "stopped", {},
                             generation=pane.generation, claim_lock=pane.claim_lock)
                db.mark_stopped(con, pane.card,
                                generation=pane.generation, claim_lock=pane.claim_lock)
            pane.card = pane.claim_lock = None   # detach before closing so the watcher does not treat it as a crash
            self.close(pane.id)
            return {"stopped": True}
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
            pane = self.run_card(params["task_id"], place=params.get("place"))
            return {"pane_id": pane.id, "pid": pane.proc.pid, "card": pane.card}
        if method == "pane.open_card_session":
            pane = self.open_card_session(params["task_id"], place=params.get("place"))
            return {"pane_id": pane.id, "pid": pane.proc.pid, "card": pane.card}
        if method == "pane.continue_card":
            pane = self.continue_card(params["task_id"], params["say"], place=params.get("place"))
            return {"pane_id": pane.id, "pid": pane.proc.pid, "card": pane.card}
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
            os.write(pane.fd, params["text"].encode())
            if params.get("enter"):
                os.write(pane.fd, b"\r")
            return {"sent": True}
        if method == "pane.input":
            pane = self.panes.get(params["id"])
            if pane is None or pane.fd is None:
                raise ValueError(f"Pane is missing or has exited: {params['id']}")
            os.write(pane.fd, base64.b64decode(params["data"]))
            return {"sent": True}
        if method == "pane.resize":
            pane = self.panes.get(params["id"])
            if pane is None or pane.fd is None:
                raise ValueError(f"Pane is missing or has exited: {params['id']}")
            rows, cols = int(params["rows"]), int(params["cols"])
            if (pane.screen.lines, pane.screen.columns) == (rows, cols):
                # herdr resizes only on a real change: an unchanged size must not
                # SIGWINCH the app into a pointless full repaint on every tab switch.
                return {"resized": False}
            fcntl.ioctl(pane.fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
            pane.screen.resize(rows, cols)
            pane.screen.dirty.clear()
            try:
                os.killpg(pane.proc.pid, signal.SIGWINCH)
            except OSError:
                pass
            return {"resized": True}
        if method == "pane.screen":
            pane = self.panes.get(params["id"])
            if pane is None:
                raise ValueError(f"Pane not found: {params['id']}")
            pane.screen.dirty.clear()
            return {"rows": [_render_row(pane.screen, r)
                             for r in range(pane.screen.lines)],
                    "cursor": [pane.screen.cursor.x, pane.screen.cursor.y],
                    "cursor_hidden": bool(pane.screen.cursor.hidden),
                    "size": [pane.screen.lines, pane.screen.columns],
                    "scroll": _scroll_metrics(pane),
                    "alt_screen": pane.alt_screen}
        if method == "pane.scroll":
            # Negative delta scrolls up; positive scrolls down; "bottom" jumps to the end.
            pane = self.panes.get(params["id"])
            if pane is None:
                raise ValueError(f"Pane not found: {params['id']}")
            _scroll_pane(pane, params.get("delta", 0), params.get("to"))
            return {"scroll": _scroll_metrics(pane),
                    "rows": [_render_row(pane.screen, r)
                             for r in range(pane.screen.lines)]}
        if method == "pane.close":
            self.close(params["id"])
            return {"closed": True}
        if method == "server.stop":
            self._stopping.set()
            return {"stopping": True}
        raise ValueError(f"Unknown method: {method}")

    async def _serve_client(self, reader, writer):
        self._clients.add(writer)
        try:
            while not self._stopping.is_set():
                line = await reader.readline()
                if not line:
                    break
                try:
                    req = json.loads(line)
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
                    out = {"id": req.get("id"), "result": result}
                except Exception as error:  # noqa: BLE001 - one failed request must not drop the connection
                    out = {"id": None, "error": f"{type(error).__name__}: {error}"}
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
            os.unlink(self.sock_path)
        os.makedirs(os.path.dirname(self.sock_path), exist_ok=True)
        server = await asyncio.start_unix_server(self._serve_client, self.sock_path)
        os.chmod(self.sock_path, 0o600)
        ally_commands()   # seed allies.json on first run so the user can edit it at any time
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
        # herdr leaves the socket file at shutdown (ipc.rs reclaim_name(false)): the next
        # daemon's startup probe reclaims a stale file, so no unlink can ever race a
        # successor that has already bound the same path.


def main():
    asyncio.run(Daemon().run())


if __name__ == "__main__":
    main()

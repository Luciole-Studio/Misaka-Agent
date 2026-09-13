"""libghostty-vt, the terminal emulator herdr embeds, driven through ctypes.

herdr runs every pane on ghostty's VT implementation (its ``src/ghostty/mod.rs`` wraps the
C API in ``vendor/libghostty-vt/include/ghostty/vt``). The panel's daemon used pyte, a
pure-Python emulator, and every difference the user saw against herdr came from that
layer: no reflow on width change, a fixed 2000-row scrollback, a few MB/s of parsing, wide
characters cut at the edge. This module is the same wrapper for Python: one ``Terminal``
per pane, a ``RenderState`` to read the viewport the way herdr's renderer does, and the
formatter for text extraction.

The shared library is built from the source herdr vendors and shipped under ``lib/`` next to
this file, one per platform (``libghostty-vt-<os>-<arch>.<ext>``: macOS and Linux, arm64 and
x86_64). Anywhere else, build ghostty's libghostty-vt yourself and point ``MISAKA_GHOSTTY_VT``
at it; without one the panel is unavailable and ``misaka`` opens plain chat instead. ghostty is
MIT-licensed (Mitchell Hashimoto and contributors).
"""
import ctypes
import os
import platform
import sys
from ctypes import (
    CFUNCTYPE,
    POINTER,
    Structure,
    Union,
    byref,
    c_bool,
    c_char_p,
    c_int,
    c_size_t,
    c_ssize_t,
    c_uint8,
    c_uint16,
    c_uint32,
    c_uint64,
    c_void_p,
    sizeof,
)

SCROLLBACK_BYTES = 10_000_000       # ghostty's default scrollback-limit
CELL_SIZE_PX = (8, 16)              # what CSI 14/16 t reports; the panel draws no pixels

# ── C types (include/ghostty/vt/*.h) ──────────────────────────────────────────────────────


class TerminalOptions(Structure):
    _fields_ = [("cols", c_uint16), ("rows", c_uint16), ("max_scrollback", c_size_t)]


class Rgb(Structure):
    _fields_ = [("r", c_uint8), ("g", c_uint8), ("b", c_uint8)]


class _StyleColorValue(Union):
    _fields_ = [("palette", c_uint8), ("rgb", Rgb), ("_padding", c_uint64)]


class StyleColor(Structure):
    _fields_ = [("tag", c_int), ("value", _StyleColorValue)]


class Style(Structure):
    _fields_ = [("size", c_size_t), ("fg_color", StyleColor), ("bg_color", StyleColor),
                ("underline_color", StyleColor), ("bold", c_bool), ("italic", c_bool), ("faint", c_bool),
                ("blink", c_bool), ("inverse", c_bool), ("invisible", c_bool), ("strikethrough", c_bool),
                ("overline", c_bool), ("underline", c_int)]


class Buffer(Structure):
    _fields_ = [("ptr", c_void_p), ("cap", c_size_t), ("len", c_size_t)]


class Scrollbar(Structure):
    _fields_ = [("total", c_uint64), ("offset", c_uint64), ("len", c_uint64)]


class _ScrollValue(Union):
    _fields_ = [("delta", c_ssize_t), ("row", c_size_t), ("_padding", c_uint64 * 2)]


class ScrollViewport(Structure):
    _fields_ = [("tag", c_int), ("value", _ScrollValue)]


class Coordinate(Structure):
    _fields_ = [("x", c_uint16), ("y", c_uint32)]


class _PointValue(Union):
    _fields_ = [("coordinate", Coordinate), ("_padding", c_uint64 * 2)]


class Point(Structure):
    _fields_ = [("tag", c_int), ("value", _PointValue)]


class GridRef(Structure):
    _fields_ = [("size", c_size_t), ("node", c_void_p), ("x", c_uint16), ("y", c_uint16)]


class Selection(Structure):
    _fields_ = [("size", c_size_t), ("start", GridRef), ("end", GridRef), ("rectangle", c_bool)]


class _FormatterScreenExtra(Structure):
    _fields_ = [("size", c_size_t), ("cursor", c_bool), ("style", c_bool), ("hyperlink", c_bool),
                ("protection", c_bool), ("kitty_keyboard", c_bool), ("charsets", c_bool)]


class _FormatterTerminalExtra(Structure):
    _fields_ = [("size", c_size_t), ("palette", c_bool), ("modes", c_bool), ("scrolling_region", c_bool),
                ("tabstops", c_bool), ("pwd", c_bool), ("keyboard", c_bool), ("screen", _FormatterScreenExtra)]


class FormatterOptions(Structure):
    _fields_ = [("size", c_size_t), ("emit", c_int), ("unwrap", c_bool), ("trim", c_bool),
                ("extra", _FormatterTerminalExtra), ("selection", POINTER(Selection))]


class GString(Structure):
    _fields_ = [("ptr", c_void_p), ("len", c_size_t)]


class SizeReport(Structure):
    _fields_ = [("rows", c_uint16), ("columns", c_uint16), ("cell_width", c_uint32), ("cell_height", c_uint32)]


WritePtyFn = CFUNCTYPE(None, c_void_p, c_void_p, POINTER(c_uint8), c_size_t)
SizeFn = CFUNCTYPE(c_bool, c_void_p, c_void_p, POINTER(SizeReport))

# Enum values (the headers define them as plain C enums).
OPT_USERDATA, OPT_WRITE_PTY, OPT_SIZE, OPT_COLOR_FOREGROUND, OPT_COLOR_BACKGROUND = 0, 1, 6, 11, 12
DATA_TITLE = 12
DATA_CURSOR_X, DATA_CURSOR_Y, DATA_ACTIVE_SCREEN, DATA_CURSOR_VISIBLE = 3, 4, 6, 7
DATA_KITTY_KEYBOARD_FLAGS, DATA_SCROLLBAR = 8, 9
DATA_TOTAL_ROWS, DATA_VIEWPORT_ACTIVE, DATA_MODIFY_OTHER_KEYS = 14, 32, 33
RS_COLS, RS_ROWS, RS_DIRTY, RS_ROW_ITERATOR = 1, 2, 3, 4
RS_CURSOR_VISIBLE, RS_CURSOR_HAS_VALUE, RS_CURSOR_X, RS_CURSOR_Y = 11, 14, 15, 16
RS_OPTION_DIRTY, ROW_OPTION_DIRTY = 0, 0
ROW_DIRTY, ROW_CELLS = 1, 3
CELLS_RAW, CELLS_STYLE, CELLS_HAS_STYLING, CELLS_UTF8 = 1, 2, 8, 9
CELL_WIDE = 3
WIDE_NARROW, WIDE_WIDE, WIDE_SPACER_TAIL, WIDE_SPACER_HEAD = 0, 1, 2, 3
SCROLL_TOP, SCROLL_BOTTOM, SCROLL_DELTA, SCROLL_ROW = 0, 1, 2, 3
POINT_SCREEN = 2
DIRTY_FALSE, DIRTY_PARTIAL, DIRTY_FULL = 0, 1, 2
FORMAT_PLAIN = 0
COLOR_NONE, COLOR_PALETTE, COLOR_RGB = 0, 1, 2


def mode(number, ansi=False):
    """``ghostty_mode_new``: a mode number, ANSI (``CSI n h``) or DEC private (``CSI ? n h``)."""
    return (number & 0x7FFF) | (int(ansi) << 15)


MODE_DECCKM, MODE_ALT_SCREEN, MODE_ALT_SCREEN_SAVE, MODE_BRACKETED_PASTE = mode(1), mode(1047), mode(1049), mode(2004)
MODE_FOCUS_EVENT, MODE_ALT_SCROLL, MODE_SYNC_OUTPUT = mode(1004), mode(1007), mode(2026)
MODE_UTF8_MOUSE, MODE_SGR_MOUSE, MODE_SGR_PIXELS_MOUSE = mode(1005), mode(1006), mode(1016)
# The most demanding tracking mode wins, as in herdr's InputState (pane/terminal.rs).
MOUSE_MODES = ((mode(1003), "any_motion"), (mode(1002), "button_motion"), (mode(1000), "press_release"), (mode(9), "x10"))

_LIB = None


def library():
    """The loaded shared library (once per process)."""
    global _LIB
    if _LIB is None:
        _LIB = _load()
    return _LIB


def library_path():
    """Where the library is looked for: ``MISAKA_GHOSTTY_VT``, else the package's ``lib`` dir,
    which ships one build per platform (``libghostty-vt-<os>-<arch>.<ext>``: macOS and Linux,
    arm64 and x86_64). A build named without the platform is accepted as a fallback."""
    override = os.environ.get("MISAKA_GHOSTTY_VT")
    if override:
        return override
    folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
    system, suffix = ("darwin", ".dylib") if sys.platform == "darwin" else ("linux", ".so")
    machine = platform.machine().lower()
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x86_64", "amd64": "x86_64"}.get(machine, machine)
    specific = os.path.join(folder, f"libghostty-vt-{system}-{arch}{suffix}")
    generic = os.path.join(folder, "libghostty-vt" + suffix)
    return generic if not os.path.isfile(specific) and os.path.isfile(generic) else specific


def _load():
    path = library_path()
    try:
        lib = ctypes.CDLL(path)
    except OSError as error:
        raise RuntimeError(
            f"libghostty-vt is not available at {path}: {error}. "
            "No build is shipped for this platform; build one from herdr's vendored libghostty-vt "
            "(zig build -Demit-lib-vt=true) and point MISAKA_GHOSTTY_VT at it."
        ) from error
    vp = c_void_p
    signatures = {
        "ghostty_terminal_new": (c_int, [vp, POINTER(vp), TerminalOptions]),
        "ghostty_terminal_free": (None, [vp]),
        "ghostty_terminal_vt_write": (None, [vp, c_char_p, c_size_t]),
        "ghostty_terminal_resize": (c_int, [vp, c_uint16, c_uint16, c_uint32, c_uint32]),
        "ghostty_terminal_set": (c_int, [vp, c_int, vp]),
        "ghostty_terminal_get": (c_int, [vp, c_int, vp]),
        "ghostty_terminal_mode_get": (c_int, [vp, c_uint16, POINTER(c_bool)]),
        "ghostty_terminal_scroll_viewport": (None, [vp, ScrollViewport]),
        "ghostty_terminal_grid_ref": (c_int, [vp, Point, POINTER(GridRef)]),
        "ghostty_render_state_new": (c_int, [vp, POINTER(vp)]),
        "ghostty_render_state_free": (None, [vp]),
        "ghostty_render_state_update": (c_int, [vp, vp]),
        "ghostty_render_state_get": (c_int, [vp, c_int, vp]),
        "ghostty_render_state_set": (c_int, [vp, c_int, vp]),
        "ghostty_render_state_row_iterator_new": (c_int, [vp, POINTER(vp)]),
        "ghostty_render_state_row_iterator_free": (None, [vp]),
        "ghostty_render_state_row_iterator_next": (c_bool, [vp]),
        "ghostty_render_state_row_get": (c_int, [vp, c_int, vp]),
        "ghostty_render_state_row_set": (c_int, [vp, c_int, vp]),
        "ghostty_render_state_row_cells_new": (c_int, [vp, POINTER(vp)]),
        "ghostty_render_state_row_cells_free": (None, [vp]),
        "ghostty_render_state_row_cells_next": (c_bool, [vp]),
        "ghostty_render_state_row_cells_get": (c_int, [vp, c_int, vp]),
        "ghostty_cell_get": (c_int, [c_uint64, c_int, vp]),
        "ghostty_formatter_terminal_new": (c_int, [vp, POINTER(vp), vp, FormatterOptions]),
        "ghostty_formatter_format_alloc": (c_int, [vp, vp, POINTER(POINTER(c_uint8)), POINTER(c_size_t)]),
        "ghostty_formatter_free": (None, [vp]),
        "ghostty_free": (None, [vp, POINTER(c_uint8), c_size_t]),
    }
    for name, (restype, argtypes) in signatures.items():
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = restype, argtypes
    return lib


def _check(result, what):
    if result != 0:
        raise RuntimeError(f"libghostty-vt {what} failed with {result}")


def _colour(style_colour):
    """A ``GhosttyStyleColor`` as None, a palette index, or an ``(r, g, b)`` triple."""
    if style_colour.tag == COLOR_PALETTE:
        return style_colour.value.palette
    if style_colour.tag == COLOR_RGB:
        rgb = style_colour.value.rgb
        return (rgb.r, rgb.g, rgb.b)
    return None


PLAIN = (None, None, False, False, False, False, False, False, False)


def _style_tuple(style):
    """``(fg, bg, bold, faint, italic, underline, blink, inverse, strikethrough)``."""
    return (_colour(style.fg_color), _colour(style.bg_color), style.bold, style.faint, style.italic,
            style.underline != 0, style.blink, style.inverse, style.strikethrough)


class Terminal:
    """One ghostty terminal: herdr's ``ghostty::Terminal``.

    Bytes from the pty go in through ``write``; whatever the terminal answers (device
    attributes, cursor reports, the kitty keyboard query, size reports) comes out through
    ``on_write_pty``, which the owner sends back to the program.
    """

    def __init__(self, cols, rows, max_scrollback=SCROLLBACK_BYTES, on_write_pty=None):
        self._lib = library()
        self.raw = c_void_p()
        _check(self._lib.ghostty_terminal_new(None, byref(self.raw), TerminalOptions(cols, rows, max_scrollback)),
               "terminal_new")
        self.cols, self.rows = cols, rows
        self.on_write_pty = on_write_pty
        self._write_pty = WritePtyFn(self._pty_output)      # kept alive with the terminal
        self._size = SizeFn(self._size_report)
        _check(self._lib.ghostty_terminal_set(self.raw, OPT_WRITE_PTY, ctypes.cast(self._write_pty, c_void_p)),
               "set write_pty")
        _check(self._lib.ghostty_terminal_set(self.raw, OPT_SIZE, ctypes.cast(self._size, c_void_p)), "set size")

    def close(self):
        if self.raw:
            self._lib.ghostty_terminal_free(self.raw)
            self.raw = c_void_p()

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001, S110 - interpreter shutdown
            pass

    def _pty_output(self, _terminal, _userdata, data, length):
        if self.on_write_pty is not None and length:
            self.on_write_pty(ctypes.string_at(data, length))

    def _size_report(self, _terminal, _userdata, out):
        out[0].rows, out[0].columns = self.rows, self.cols
        out[0].cell_width, out[0].cell_height = CELL_SIZE_PX
        return True

    # ── input ─────────────────────────────────────────────────────────────────────────────

    def write(self, data):
        self._lib.ghostty_terminal_vt_write(self.raw, data, len(data))

    def resize(self, cols, rows):
        """herdr ``TerminalRuntime::resize``: the offset from the bottom is kept across the
        resize, clamped to the new maximum (ghostty reflows the primary screen itself)."""
        cols, rows = max(4, cols), max(2, rows)
        total, offset, length = self.scrollbar()
        from_bottom = total - (offset + length)
        _check(self._lib.ghostty_terminal_resize(self.raw, cols, rows, *CELL_SIZE_PX), "resize")
        self.cols, self.rows = cols, rows
        if from_bottom > 0:
            self.scroll_to_offset_from_bottom(from_bottom)

    def set_colors(self, foreground, background):
        """Default foreground and background as ``(r, g, b)``: what OSC 10/11 queries answer."""
        fg, bg = Rgb(*foreground), Rgb(*background)
        _check(self._lib.ghostty_terminal_set(self.raw, OPT_COLOR_FOREGROUND, byref(fg)), "set foreground")
        _check(self._lib.ghostty_terminal_set(self.raw, OPT_COLOR_BACKGROUND, byref(bg)), "set background")

    # ── state ─────────────────────────────────────────────────────────────────────────────

    def _get(self, kind, ctype):
        out = ctype()
        _check(self._lib.ghostty_terminal_get(self.raw, kind, byref(out)), f"get {kind}")
        return out

    def mode(self, which):
        out = c_bool()
        _check(self._lib.ghostty_terminal_mode_get(self.raw, which, byref(out)), "mode_get")
        return bool(out.value)

    def alternate_screen(self):
        return self._get(DATA_ACTIVE_SCREEN, c_int).value == 1

    def kitty_flags(self):
        return self._get(DATA_KITTY_KEYBOARD_FLAGS, c_uint8).value

    def mouse_tracking(self):
        """"none", "x10", "press_release", "button_motion" or "any_motion"."""
        return next((name for which, name in MOUSE_MODES if self.mode(which)), "none")

    def modify_other_keys(self):
        return bool(self._get(DATA_MODIFY_OTHER_KEYS, c_bool).value)

    def title(self):
        """The window title the program set (OSC 0/2), or an empty string."""
        out = self._get(DATA_TITLE, GString)
        return ctypes.string_at(out.ptr, out.len).decode("utf-8", "replace") if out.len else ""

    def cursor(self):
        """``(x, y)`` in the active area."""
        return (self._get(DATA_CURSOR_X, c_uint16).value, self._get(DATA_CURSOR_Y, c_uint16).value)

    def cursor_visible(self):
        return bool(self._get(DATA_CURSOR_VISIBLE, c_bool).value)

    def scrollbar(self):
        """``(total rows, first visible row, viewport rows)``."""
        bar = self._get(DATA_SCROLLBAR, Scrollbar)
        return (bar.total, bar.offset, bar.len)

    def scroll_metrics(self):
        """herdr ``scroll_metrics``: ``offset_from_bottom``, ``max_offset_from_bottom``, ``viewport_rows``."""
        total, offset, length = self.scrollbar()
        return {"offset_from_bottom": max(0, total - (offset + length)),
                "max_offset_from_bottom": max(0, total - length), "viewport_rows": length}

    def total_rows(self):
        return self._get(DATA_TOTAL_ROWS, c_size_t).value

    # ── viewport ──────────────────────────────────────────────────────────────────────────

    def _scroll(self, tag, **value):
        behaviour = ScrollViewport()
        behaviour.tag = tag
        for name, number in value.items():
            setattr(behaviour.value, name, number)
        self._lib.ghostty_terminal_scroll_viewport(self.raw, behaviour)

    def scroll(self, delta):
        """Move the viewport by rows: negative goes back into history (herdr ``scroll_up``)."""
        self._scroll(SCROLL_DELTA, delta=delta)

    def scroll_to_bottom(self):
        self._scroll(SCROLL_BOTTOM)

    def scroll_to_offset_from_bottom(self, offset_from_bottom):
        """herdr ``ghostty_set_scroll_offset_from_bottom``."""
        total, _offset, length = self.scrollbar()
        max_offset = max(0, total - length)
        offset_from_bottom = min(max(0, offset_from_bottom), max_offset)
        if offset_from_bottom == 0:
            self.scroll_to_bottom()
        else:
            self._scroll(SCROLL_ROW, row=max_offset - offset_from_bottom)

    # ── text ──────────────────────────────────────────────────────────────────────────────

    def _grid_ref(self, x, y):
        point = Point()
        point.tag = POINT_SCREEN
        point.value.coordinate.x, point.value.coordinate.y = x, y
        ref = GridRef()
        ref.size = sizeof(GridRef)
        _check(self._lib.ghostty_terminal_grid_ref(self.raw, point, byref(ref)), f"grid_ref {(x, y)}")
        return ref

    def read_text(self, start, end, *, unwrap=True, trim=True):
        """The text between two screen points ``(x, y)`` (rows count from the top of the
        scrollback), both inclusive, as herdr's ``read_text_screen``: soft-wrapped rows
        joined, trailing blanks dropped."""
        selection = Selection()
        selection.size = sizeof(Selection)
        selection.start, selection.end = self._grid_ref(*start), self._grid_ref(*end)
        selection.rectangle = False
        options = FormatterOptions()
        options.size, options.emit, options.unwrap, options.trim = sizeof(FormatterOptions), FORMAT_PLAIN, unwrap, trim
        options.extra.size = sizeof(_FormatterTerminalExtra)
        options.extra.screen.size = sizeof(_FormatterScreenExtra)
        options.selection = ctypes.pointer(selection)
        formatter = c_void_p()
        _check(self._lib.ghostty_formatter_terminal_new(None, byref(formatter), self.raw, options), "formatter_new")
        try:
            ptr, length = POINTER(c_uint8)(), c_size_t()
            _check(self._lib.ghostty_formatter_format_alloc(formatter, None, byref(ptr), byref(length)), "format")
            try:
                return ctypes.string_at(ptr, length.value).decode("utf-8", "replace") if length.value else ""
            finally:
                if length.value:
                    self._lib.ghostty_free(None, ptr, length.value)
        finally:
            self._lib.ghostty_formatter_free(formatter)

    def recent_text(self, rows):
        """The last ``rows`` rows of the screen as text (herdr ``recent_text``)."""
        total = self.total_rows()
        if not total:
            return ""
        first = max(0, total - rows)
        return self.read_text((0, first), (max(0, self.cols - 1), total - 1), unwrap=False)


class RenderState:
    """herdr's per-pane render state: what the viewport shows, row by row, with the
    dirty tracking ghostty maintains for renderers."""

    def __init__(self):
        self._lib = library()
        self.raw = c_void_p()
        _check(self._lib.ghostty_render_state_new(None, byref(self.raw)), "render_state_new")
        self._rows = c_void_p()
        _check(self._lib.ghostty_render_state_row_iterator_new(None, byref(self._rows)), "row_iterator_new")
        self._cells = c_void_p()
        _check(self._lib.ghostty_render_state_row_cells_new(None, byref(self._cells)), "row_cells_new")
        self._buffer = ctypes.create_string_buffer(256)
        self._utf8 = Buffer(ctypes.cast(self._buffer, c_void_p), 256, 0)

    def close(self):
        if self._cells:
            self._lib.ghostty_render_state_row_cells_free(self._cells)
            self._cells = c_void_p()
        if self._rows:
            self._lib.ghostty_render_state_row_iterator_free(self._rows)
            self._rows = c_void_p()
        if self.raw:
            self._lib.ghostty_render_state_free(self.raw)
            self.raw = c_void_p()

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001, S110
            pass

    def update(self, terminal):
        """Take the terminal's current state. Returns "none", "partial" or "full"."""
        _check(self._lib.ghostty_render_state_update(self.raw, terminal.raw), "render_state_update")
        dirty = c_int()
        _check(self._lib.ghostty_render_state_get(self.raw, RS_DIRTY, byref(dirty)), "get dirty")
        return {DIRTY_FALSE: "none", DIRTY_PARTIAL: "partial"}.get(dirty.value, "full")

    def _get(self, kind, ctype):
        out = ctype()
        _check(self._lib.ghostty_render_state_get(self.raw, kind, byref(out)), f"render get {kind}")
        return out

    def size(self):
        return (self._get(RS_COLS, c_uint16).value, self._get(RS_ROWS, c_uint16).value)

    def cursor(self):
        """``(x, y, visible)`` in viewport cells. Scrolled away from the cursor's row the
        viewport has no cursor: ``visible`` is False and the position is ``(0, 0)``."""
        if not self._get(RS_CURSOR_HAS_VALUE, c_bool).value:
            return (0, 0, False)
        visible = bool(self._get(RS_CURSOR_VISIBLE, c_bool).value)
        return (self._get(RS_CURSOR_X, c_uint16).value, self._get(RS_CURSOR_Y, c_uint16).value, visible)

    def rows(self, all_rows=False):
        """Yield ``(index, cells)`` for each dirty row (every row when ``all_rows``), where
        ``cells`` is a list of ``(text, width, style)``: ``width`` 0 marks a wide
        character's tail (or a spacer head, which reads as a blank), ``style`` a tuple from
        ``_style_tuple``. Rows are marked clean as they are read."""
        lib = self._lib
        _check(lib.ghostty_render_state_get(self.raw, RS_ROW_ITERATOR, byref(self._rows)), "get row iterator")
        rows, cells = self._rows, self._cells
        raw, wide, style, styled, utf8, buffer = c_uint64(), c_int(), Style(), c_bool(), self._utf8, self._buffer
        style.size = sizeof(Style)
        clean = c_bool(False)
        index = 0
        while lib.ghostty_render_state_row_iterator_next(rows):
            dirty = c_bool()
            lib.ghostty_render_state_row_get(rows, ROW_DIRTY, byref(dirty))
            if all_rows or dirty.value:
                _check(lib.ghostty_render_state_row_get(rows, ROW_CELLS, byref(cells)), "get row cells")
                out = []
                while lib.ghostty_render_state_row_cells_next(cells):
                    lib.ghostty_render_state_row_cells_get(cells, CELLS_RAW, byref(raw))
                    lib.ghostty_cell_get(raw.value, CELL_WIDE, byref(wide))
                    if wide.value == WIDE_SPACER_TAIL:
                        out.append(("", 0, PLAIN))
                        continue
                    utf8.len = 0
                    text = " "
                    if wide.value != WIDE_SPACER_HEAD and lib.ghostty_render_state_row_cells_get(cells, CELLS_UTF8, byref(utf8)) == 0 and utf8.len:
                        text = buffer.raw[:utf8.len].decode("utf-8", "replace")
                    lib.ghostty_render_state_row_cells_get(cells, CELLS_HAS_STYLING, byref(styled))
                    if styled.value and lib.ghostty_render_state_row_cells_get(cells, CELLS_STYLE, byref(style)) == 0:
                        cell_style = _style_tuple(style)
                    else:
                        cell_style = PLAIN
                    out.append((text, 2 if wide.value == WIDE_WIDE else 1, cell_style))
                if dirty.value:
                    lib.ghostty_render_state_row_set(rows, ROW_OPTION_DIRTY, byref(clean))
                yield index, out
            index += 1
        lib.ghostty_render_state_set(self.raw, RS_OPTION_DIRTY, byref(c_int(DIRTY_FALSE)))

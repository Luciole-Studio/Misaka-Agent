"""Multi-line editor component and layout helpers."""

from __future__ import annotations

import asyncio
import json
import os
import unicodedata
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from misaka.tui.autocomplete import AutocompleteItem, AutocompleteProvider, AutocompleteSuggestions
from misaka.tui.components.select_list import SelectItem, SelectList, SelectListLayoutOptions, SelectListTheme
from misaka.tui.keybindings import getKeybindings
from misaka.tui.keys import decodePrintableKey, matchesKey
from misaka.tui.kill_ring import KillRing
from misaka.tui.tui import CURSOR_MARKER, TUI
from misaka.tui.undo_stack import UndoStack
from misaka.tui.utils import (
    _CJK_BREAK_RE,
    SegmentData,
    getSegmenter,
    isPunctuationChar,
    isWhitespaceChar,
    truncateToWidth,
    visibleWidth,
)

_BASE_SEGMENTER = getSegmenter()

def _is_word_nav_punct(segment: str) -> bool:
    """Punctuation for word navigation: ASCII punctuation plus Unicode P* classes.

    PORT-NOTE: upstream uses Intl.Segmenter word granularity (with a CJK dictionary).
    Python has no dictionary segmentation, so approximate with "each CJK character is a
    word, Unicode punctuation is a boundary": slightly finer jumps than ICU words, but it
    fixes alt+backspace swallowing a whole Chinese sentence.
    """
    if not segment:
        return False
    if isPunctuationChar(segment):
        return True
    # P* = punctuation; S* = symbols (emoji, currency, ±). Upstream's ASCII table already puts
    # +-=$#@~ in S, so the equivalent class is P ∪ S
    return unicodedata.category(segment[0])[0] in ("P", "S")


PASTE_MARKER_REGEX = re.compile(r"\[paste #(\d+)( (\+\d+ lines|\d+ chars))?\]")
PASTE_MARKER_SINGLE = re.compile(r"^\[paste #(\d+)( (\+\d+ lines|\d+ chars))?\]$")
SLASH_COMMAND_SELECT_LIST_LAYOUT = SelectListLayoutOptions(minPrimaryColumnWidth=12, maxPrimaryColumnWidth=32)
ATTACHMENT_AUTOCOMPLETE_DEBOUNCE_MS = 20


class _AutocompleteAbortSignal:
    def __init__(self) -> None:
        self.aborted = False
        self._listeners: list[Callable[[], None]] = []

    def addEventListener(
        self,
        event: str,
        callback: Callable[[], None],
        _options: dict[str, object] | None = None,
    ) -> None:
        if event != "abort":
            return
        if self.aborted:
            callback()
            return
        self._listeners.append(callback)

    def removeEventListener(self, event: str, callback: Callable[[], None]) -> None:
        if event != "abort":
            return
        self._listeners = [listener for listener in self._listeners if listener is not callback]

    def dispatch(self) -> None:
        if self.aborted:
            return
        self.aborted = True
        listeners = list(self._listeners)
        self._listeners.clear()
        for callback in listeners:
            callback()


class _AutocompleteAbortController:
    def __init__(self) -> None:
        self.signal = _AutocompleteAbortSignal()

    def abort(self) -> None:
        self.signal.dispatch()


def _get_running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def is_paste_marker(segment: str) -> bool:
    return len(segment) >= 10 and PASTE_MARKER_SINGLE.fullmatch(segment) is not None


def segment_with_markers(text: str, valid_ids: set[int]) -> list[SegmentData]:
    if not valid_ids or "[paste #" not in text:
        return list(_BASE_SEGMENTER.segment(text))

    markers: list[tuple[int, int]] = []
    for match in PASTE_MARKER_REGEX.finditer(text):
        marker_id = int(match.group(1))
        if marker_id not in valid_ids:
            continue
        markers.append((match.start(), match.end()))

    if not markers:
        return list(_BASE_SEGMENTER.segment(text))

    base_segments = list(_BASE_SEGMENTER.segment(text))
    result: list[SegmentData] = []
    marker_index = 0

    for segment in base_segments:
        while marker_index < len(markers) and markers[marker_index][1] <= segment.index:
            marker_index += 1

        marker = markers[marker_index] if marker_index < len(markers) else None
        if marker is not None and marker[0] <= segment.index < marker[1]:
            if segment.index == marker[0]:
                result.append(SegmentData(segment=text[marker[0] : marker[1]], index=marker[0], input=text))
            continue

        result.append(segment)

    return result


@dataclass(slots=True)
class TextChunk:
    text: str
    startIndex: int
    endIndex: int


@dataclass(slots=True)
class EditorState:
    lines: list[str] = field(default_factory=lambda: [""])
    cursorLine: int = 0
    cursorCol: int = 0


@dataclass(slots=True)
class LayoutLine:
    text: str
    hasCursor: bool
    cursorPos: int | None = None


@dataclass(slots=True)
class EditorTheme:
    borderColor: Callable[[str], str]
    selectList: SelectListTheme


@dataclass(slots=True)
class EditorOptions:
    paddingX: int | None = None
    autocompleteMaxVisible: int | None = None


def word_wrap_line(
    line: str,
    max_width: int,
    pre_segmented: Iterable[SegmentData] | None = None,
) -> list[TextChunk]:
    if not line or max_width <= 0:
        return [TextChunk(text="", startIndex=0, endIndex=0)]

    if visibleWidth(line) <= max_width:
        return [TextChunk(text=line, startIndex=0, endIndex=len(line))]

    chunks: list[TextChunk] = []
    segments = list(pre_segmented) if pre_segmented is not None else list(_BASE_SEGMENTER.segment(line))
    current_width = 0
    chunk_start = 0
    wrap_opp_index = -1
    wrap_opp_width = 0

    for index, segment in enumerate(segments):
        grapheme = segment.segment
        grapheme_width = visibleWidth(grapheme)
        char_index = segment.index
        is_whitespace = not is_paste_marker(grapheme) and isWhitespaceChar(grapheme)

        if current_width + grapheme_width > max_width:
            if wrap_opp_index >= 0 and current_width - wrap_opp_width + grapheme_width <= max_width:
                chunks.append(
                    TextChunk(
                        text=line[chunk_start:wrap_opp_index],
                        startIndex=chunk_start,
                        endIndex=wrap_opp_index,
                    )
                )
                chunk_start = wrap_opp_index
                current_width -= wrap_opp_width
            elif chunk_start < char_index:
                chunks.append(TextChunk(text=line[chunk_start:char_index], startIndex=chunk_start, endIndex=char_index))
                chunk_start = char_index
                current_width = 0
            wrap_opp_index = -1

        if grapheme_width > max_width:
            sub_chunks = word_wrap_line(grapheme, max_width)
            for sub_chunk in sub_chunks[:-1]:
                chunks.append(
                    TextChunk(
                        text=sub_chunk.text,
                        startIndex=char_index + sub_chunk.startIndex,
                        endIndex=char_index + sub_chunk.endIndex,
                    )
                )
            last_chunk = sub_chunks[-1]
            chunk_start = char_index + last_chunk.startIndex
            current_width = visibleWidth(last_chunk.text)
            wrap_opp_index = -1
            continue

        current_width += grapheme_width
        next_segment = segments[index + 1] if index + 1 < len(segments) else None
        if (
            is_whitespace
            and next_segment is not None
            and (is_paste_marker(next_segment.segment) or not isWhitespaceChar(next_segment.segment))
        ):
            wrap_opp_index = next_segment.index
            wrap_opp_width = current_width
        elif not is_whitespace and next_segment is not None and not isWhitespaceChar(next_segment.segment):
            # any two adjacent CJK characters are a break opportunity (TS cjkBreakRegex branch)
            is_cjk = not is_paste_marker(grapheme) and _CJK_BREAK_RE.search(grapheme)
            next_is_cjk = (not is_paste_marker(next_segment.segment)
                           and _CJK_BREAK_RE.search(next_segment.segment))
            if is_cjk or next_is_cjk:
                wrap_opp_index = next_segment.index
                wrap_opp_width = current_width

    chunks.append(TextChunk(text=line[chunk_start:], startIndex=chunk_start, endIndex=len(line)))
    return chunks


class Editor:
    wantsKeyRelease = False

    def __init__(self, tui: TUI, theme: EditorTheme, options: EditorOptions | None = None) -> None:
        self.state = EditorState()
        self.focused = False
        self.tui = tui
        self.theme = theme
        self.paddingX = 0
        self.lastWidth = 80
        self.scrollOffset = 0
        self.borderColor = theme.borderColor
        self.autocompleteProvider: AutocompleteProvider | None = None
        self.autocompleteList: SelectList | None = None
        self.autocompleteState: str | None = None
        self.autocompletePrefix = ""
        self.autocompleteMaxVisible = 5
        self.autocompleteAbort: _AutocompleteAbortController | None = None
        self.autocompleteDebounceTimer: asyncio.TimerHandle | None = None
        self.autocompleteRequestTask: asyncio.Future[None] | None = None
        self.autocompleteStartToken = 0
        self.autocompleteRequestId = 0
        self.pastes: dict[int, str] = {}
        self.pasteCounter = 0
        self.pasteBuffer = ""
        self.isInPaste = False
        self.history: list[str] = []
        self.historyIndex = -1
        self.historyDraft: EditorState | None = None
        # MISAKA: input history persists across sessions (as in Claude Code). Enabled only when MISAKA_INPUT_HISTORY is set.
        self.historyFile = os.environ.get("MISAKA_INPUT_HISTORY") or None
        if self.historyFile:
            try:
                with open(self.historyFile, encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, list):
                    self.history = [x for x in loaded if isinstance(x, str)][:100]
            except (OSError, ValueError):
                pass
        self.killRing = KillRing()
        self.lastAction: str | None = None
        self.jumpMode: str | None = None
        self.preferredVisualCol: int | None = None
        self.snappedFromCursorCol: int | None = None
        self.undoStack: UndoStack[EditorState] = UndoStack()
        self.onSubmit: Callable[[str], None] | None = None
        self.onChange: Callable[[str], None] | None = None
        self.disableSubmit = False

        resolved_options = options or EditorOptions()
        self.paddingX = self._normalizePadding(
            resolved_options.paddingX if resolved_options.paddingX is not None else 0
        )
        self.autocompleteMaxVisible = self._normalizeAutocompleteMaxVisible(
            resolved_options.autocompleteMaxVisible if resolved_options.autocompleteMaxVisible is not None else 5
        )

    def validPasteIds(self) -> set[int]:
        return set(self.pastes)

    def segment(self, text: str) -> list[SegmentData]:
        return segment_with_markers(text, self.validPasteIds())

    def _normalizePadding(self, padding: int | float) -> int:
        return max(0, int(padding)) if isinstance(padding, (int, float)) else 0

    def _normalizeAutocompleteMaxVisible(self, max_visible: int | float) -> int:
        return max(3, min(20, int(max_visible))) if isinstance(max_visible, (int, float)) else 5

    def getPaddingX(self) -> int:
        return self.paddingX

    def setPaddingX(self, padding: int) -> None:
        new_padding = self._normalizePadding(padding)
        if self.paddingX != new_padding:
            self.paddingX = new_padding
            self.tui.requestRender()

    def getAutocompleteMaxVisible(self) -> int:
        return self.autocompleteMaxVisible

    def setAutocompleteMaxVisible(self, maxVisible: int) -> None:
        new_max_visible = self._normalizeAutocompleteMaxVisible(maxVisible)
        if self.autocompleteMaxVisible != new_max_visible:
            self.autocompleteMaxVisible = new_max_visible
            self.tui.requestRender()

    def setAutocompleteProvider(self, provider: AutocompleteProvider) -> None:
        self.cancelAutocomplete()
        self.autocompleteProvider = provider

    def addToHistory(self, text: str) -> None:
        trimmed = text.strip()
        if not trimmed:
            return
        if self.history and self.history[0] == trimmed:
            return
        self.history.insert(0, trimmed)
        if len(self.history) > 100:
            self.history.pop()
        if self.historyFile:
            try:  # atomic write; a failed history save must not block the submit
                os.makedirs(os.path.dirname(self.historyFile), exist_ok=True)
                tmp = self.historyFile + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.history, f, ensure_ascii=False)
                os.replace(tmp, self.historyFile)
            except OSError:
                pass

    def isEditorEmpty(self) -> bool:
        return len(self.state.lines) == 1 and self.state.lines[0] == ""

    def isOnFirstVisualLine(self) -> bool:
        visual_lines = self.buildVisualLineMap(self.lastWidth)
        return self.findCurrentVisualLine(visual_lines) == 0

    def isOnLastVisualLine(self) -> bool:
        visual_lines = self.buildVisualLineMap(self.lastWidth)
        return self.findCurrentVisualLine(visual_lines) == len(visual_lines) - 1

    def navigateHistory(self, direction: int) -> None:
        self.lastAction = None
        if not self.history:
            return

        new_index = self.historyIndex - direction
        if new_index < -1 or new_index >= len(self.history):
            return

        if self.historyIndex == -1 and new_index >= 0:
            self.pushUndoSnapshot()
            from copy import deepcopy as _dc
            self.historyDraft = _dc(self.state)  # save the draft before entering history so Down returns it intact

        self.historyIndex = new_index
        if self.historyIndex == -1:
            draft, self.historyDraft = self.historyDraft, None
            if draft is not None:
                self.state = draft
                self.preferredVisualCol = None
                self.snappedFromCursorCol = None
                self.scrollOffset = 0
                if self.onChange is not None:
                    self.onChange(self.getText())
            else:
                self.setTextInternal("")
        else:
            # Going up (-1) puts the cursor at the start: on a multi-line entry, Up again moves
            # straight to the previous entry instead of climbing through the whole text
            self.setTextInternal(self.history[self.historyIndex],
                                 "start" if direction == -1 else "end")

    def exitHistoryBrowsing(self) -> None:
        self.historyIndex = -1
        self.historyDraft = None

    def setTextInternal(self, text: str, cursor_placement: str = "end") -> None:
        lines = text.split("\n")
        self.state.lines = lines if lines else [""]
        if cursor_placement == "start":
            self.state.cursorLine = 0
            self.setCursorCol(0)
        else:
            self.state.cursorLine = len(self.state.lines) - 1
            self.setCursorCol(len(self.state.lines[self.state.cursorLine] if self.state.lines else ""))
        self.scrollOffset = 0
        if self.onChange is not None:
            self.onChange(self.getText())

    def invalidate(self) -> None:
        return None

    def render(self, width: int) -> list[str]:
        max_padding = max(0, (width - 1) // 2)
        padding_x = min(self.paddingX, max_padding)
        content_width = max(1, width - padding_x * 2)
        layout_width = max(1, content_width - (0 if padding_x else 1))
        self.lastWidth = layout_width

        horizontal = self.borderColor("─")
        layout_lines = self.layoutText(layout_width)
        terminal_rows = self.tui.terminal.rows
        max_visible_lines = max(5, int(terminal_rows * 0.3))

        cursor_line_index = next((index for index, line in enumerate(layout_lines) if line.hasCursor), 0)
        if cursor_line_index < self.scrollOffset:
            self.scrollOffset = cursor_line_index
        elif cursor_line_index >= self.scrollOffset + max_visible_lines:
            self.scrollOffset = cursor_line_index - max_visible_lines + 1

        max_scroll_offset = max(0, len(layout_lines) - max_visible_lines)
        self.scrollOffset = max(0, min(self.scrollOffset, max_scroll_offset))

        visible_lines = layout_lines[self.scrollOffset : self.scrollOffset + max_visible_lines]
        result: list[str] = []
        left_padding = " " * padding_x
        right_padding = left_padding

        if self.scrollOffset > 0:
            indicator = f"─── ↑ {self.scrollOffset} more "
            remaining = width - visibleWidth(indicator)
            result.append(
                self.borderColor(indicator + ("─" * remaining))
                if remaining >= 0
                else self.borderColor(truncateToWidth(indicator, width))
            )
        else:
            result.append(horizontal * width)

        emit_cursor_marker = self.focused  # emit even with the completion menu open: the IME candidate window is positioned by it
        for layout_line in visible_lines:
            display_text = layout_line.text
            line_visible_width = visibleWidth(layout_line.text)
            cursor_in_padding = False

            if layout_line.hasCursor and layout_line.cursorPos is not None:
                before = display_text[: layout_line.cursorPos]
                after = display_text[layout_line.cursorPos :]
                marker = CURSOR_MARKER if emit_cursor_marker else ""

                if after:
                    after_graphemes = self.segment(after)
                    first_grapheme = after_graphemes[0].segment if after_graphemes else ""
                    rest_after = after[len(first_grapheme) :]
                    cursor = f"\x1b[7m{first_grapheme}\x1b[0m"
                    display_text = before + marker + cursor + rest_after
                else:
                    cursor = "\x1b[7m \x1b[0m"
                    display_text = before + marker + cursor
                    line_visible_width += 1
                    if line_visible_width > content_width and padding_x > 0:
                        cursor_in_padding = True

            padding = " " * max(0, content_width - line_visible_width)
            line_right_padding = right_padding[1:] if cursor_in_padding else right_padding
            result.append(f"{left_padding}{display_text}{padding}{line_right_padding}")

        lines_below = len(layout_lines) - (self.scrollOffset + len(visible_lines))
        left = f"─── ↓ {lines_below} more " if lines_below > 0 else ""
        # MISAKA: while browsing history, show a position indicator on the right of the bottom border (Claude Code style "History N/M")
        label = (f" History {self.historyIndex + 1}/{len(self.history)} "
                 if self.historyIndex > -1 and self.history else "")
        if left and visibleWidth(left) > width:
            result.append(self.borderColor(truncateToWidth(left, width)))
        else:
            avail = width - visibleWidth(left)
            if label and visibleWidth(label) + 1 <= avail:
                bottom = left + "─" * (avail - visibleWidth(label) - 1) + label + "─"
            else:
                bottom = left + "─" * avail
            result.append(self.borderColor(bottom))

        if self.autocompleteState and self.autocompleteList is not None:
            autocomplete_result = self.autocompleteList.render(content_width)
            for line in autocomplete_result:
                line_width = visibleWidth(line)
                line_padding = " " * max(0, content_width - line_width)
                result.append(f"{left_padding}{line}{line_padding}{right_padding}")

        return result

    def handleInput(self, data: str) -> None:
        kb = getKeybindings()

        if self.jumpMode is not None:
            if kb.matches(data, "tui.editor.jumpForward") or kb.matches(data, "tui.editor.jumpBackward"):
                self.jumpMode = None
                return

            printable = (
                decodePrintableKey(data)
                if decodePrintableKey(data) is not None
                else (data if data and ord(data[0]) >= 32 else None)
            )
            if printable is not None:
                direction = self.jumpMode
                self.jumpMode = None
                self.jumpToChar(printable, direction)
                return

            self.jumpMode = None

        if "\x1b[200~" in data:
            self.isInPaste = True
            self.pasteBuffer = ""
            data = data.replace("\x1b[200~", "", 1)

        if self.isInPaste:
            self.pasteBuffer += data
            end_index = self.pasteBuffer.find("\x1b[201~")
            if end_index != -1:
                paste_content = self.pasteBuffer[:end_index]
                if paste_content:
                    self.handlePaste(paste_content)
                self.isInPaste = False
                remaining = self.pasteBuffer[end_index + 6 :]
                self.pasteBuffer = ""
                if remaining:
                    self.handleInput(remaining)
            return

        if kb.matches(data, "tui.input.copy"):
            return

        if kb.matches(data, "tui.editor.undo"):
            self.undo()
            return

        if self.autocompleteState and self.autocompleteList is not None:
            if kb.matches(data, "tui.select.cancel"):
                self.cancelAutocomplete()
                return
            if kb.matches(data, "tui.select.up") or kb.matches(data, "tui.select.down"):
                self.autocompleteList.handleInput(data)
                return
            if kb.matches(data, "tui.input.tab"):
                selected = self.autocompleteList.getSelectedItem()
                if selected is not None and self.autocompleteProvider is not None:
                    self.pushUndoSnapshot()
                    self.lastAction = None
                    self.applyAutocompleteSelection(selected)
                    self.cancelAutocomplete()
                    if self.onChange is not None:
                        self.onChange(self.getText())
                return
            if kb.matches(data, "tui.select.confirm"):
                selected = self.autocompleteList.getSelectedItem()
                if selected is not None and self.autocompleteProvider is not None:
                    self.pushUndoSnapshot()
                    self.lastAction = None
                    self.applyAutocompleteSelection(selected)
                    if self.autocompletePrefix.startswith("/"):
                        self.cancelAutocomplete()
                    else:
                        self.cancelAutocomplete()
                        if self.onChange is not None:
                            self.onChange(self.getText())
                        return

        if kb.matches(data, "tui.input.tab") and not self.autocompleteState:
            self.handleTabCompletion()
            return

        if kb.matches(data, "tui.editor.deleteToLineEnd"):
            self.deleteToEndOfLine()
            return
        if kb.matches(data, "tui.editor.deleteToLineStart"):
            self.deleteToStartOfLine()
            return
        if kb.matches(data, "tui.editor.deleteWordBackward"):
            self.deleteWordBackwards()
            return
        if kb.matches(data, "tui.editor.deleteWordForward"):
            self.deleteWordForward()
            return
        if kb.matches(data, "tui.editor.deleteCharBackward") or matchesKey(data, "shift+backspace"):
            self.handleBackspace()
            return
        if kb.matches(data, "tui.editor.deleteCharForward") or matchesKey(data, "shift+delete"):
            self.handleForwardDelete()
            return

        if kb.matches(data, "tui.editor.yank"):
            self.yank()
            return
        if kb.matches(data, "tui.editor.yankPop"):
            self.yankPop()
            return

        if kb.matches(data, "tui.editor.cursorLineStart"):
            self.moveToLineStart()
            return
        if kb.matches(data, "tui.editor.cursorLineEnd"):
            self.moveToLineEnd()
            return
        if kb.matches(data, "tui.editor.cursorWordLeft"):
            self.moveWordBackwards()
            return
        if kb.matches(data, "tui.editor.cursorWordRight"):
            self.moveWordForwards()
            return

        if (
            kb.matches(data, "tui.input.newLine")
            or (len(data) > 1 and ord(data[0]) == 10)
            or data == "\x1b\r"
            or data == "\x1b[13;2~"
            or (len(data) > 1 and "\x1b" in data and "\r" in data)
            or data == "\n"
        ):
            if self.shouldSubmitOnBackslashEnter(data, kb):
                self.handleBackspace()
                self.submitValue()
                return
            self.addNewLine()
            return

        if kb.matches(data, "tui.input.submit"):
            if self.disableSubmit:
                return
            current_line = (
                self.state.lines[self.state.cursorLine] if self.state.cursorLine < len(self.state.lines) else ""
            )
            if self.state.cursorCol > 0 and current_line[self.state.cursorCol - 1] == "\\":
                self.handleBackspace()
                self.addNewLine()
                return
            self.submitValue()
            return

        # Dedicated history actions always browse entries instead of moving the cursor.  (pi 16ad96a)
        if kb.matches(data, "tui.editor.historyPrevious"):
            self.cancelAutocomplete()
            self.navigateHistory(-1)
            return
        if kb.matches(data, "tui.editor.historyNext"):
            self.cancelAutocomplete()
            self.navigateHistory(1)
            return

        if kb.matches(data, "tui.editor.cursorUp"):
            if self.isOnFirstVisualLine() and (
                    self.isEditorEmpty() or self.historyIndex > -1 or self.state.cursorCol == 0):
                self.navigateHistory(-1)
            elif self.isOnFirstVisualLine():
                self.moveToLineStart()
            else:
                self.moveCursor(-1, 0)
            return

        if kb.matches(data, "tui.editor.cursorDown"):
            if self.historyIndex > -1 and self.isOnLastVisualLine():
                self.navigateHistory(1)
            elif self.isOnLastVisualLine():
                self.moveToLineEnd()
            else:
                self.moveCursor(1, 0)
            return

        if kb.matches(data, "tui.editor.cursorRight"):
            self.moveCursor(0, 1)
            return
        if kb.matches(data, "tui.editor.cursorLeft"):
            self.moveCursor(0, -1)
            return

        if kb.matches(data, "tui.editor.pageUp"):
            self.pageScroll(-1)
            return
        if kb.matches(data, "tui.editor.pageDown"):
            self.pageScroll(1)
            return

        if kb.matches(data, "tui.editor.jumpForward"):
            self.jumpMode = "forward"
            return
        if kb.matches(data, "tui.editor.jumpBackward"):
            self.jumpMode = "backward"
            return

        if matchesKey(data, "shift+space"):
            self.insertCharacter(" ")
            return

        printable = decodePrintableKey(data)
        if printable is not None:
            self.insertCharacter(printable)
            return

        if data and ord(data[0]) >= 32:
            self.insertCharacter(data)

    def layoutText(self, contentWidth: int) -> list[LayoutLine]:
        layout_lines: list[LayoutLine] = []
        if not self.state.lines or (len(self.state.lines) == 1 and self.state.lines[0] == ""):
            return [LayoutLine(text="", hasCursor=True, cursorPos=0)]

        for line_index, line in enumerate(self.state.lines):
            is_current_line = line_index == self.state.cursorLine
            if visibleWidth(line) <= contentWidth:
                layout_lines.append(
                    LayoutLine(
                        text=line,
                        hasCursor=is_current_line,
                        cursorPos=self.state.cursorCol if is_current_line else None,
                    )
                )
                continue

            chunks = word_wrap_line(line, contentWidth, self.segment(line))
            for chunk_index, chunk in enumerate(chunks):
                cursor_pos = self.state.cursorCol
                is_last_chunk = chunk_index == len(chunks) - 1
                has_cursor_in_chunk = False
                adjusted_cursor_pos = 0

                if is_current_line:
                    if is_last_chunk:
                        has_cursor_in_chunk = cursor_pos >= chunk.startIndex
                        adjusted_cursor_pos = cursor_pos - chunk.startIndex
                    else:
                        has_cursor_in_chunk = chunk.startIndex <= cursor_pos < chunk.endIndex
                        if has_cursor_in_chunk:
                            adjusted_cursor_pos = min(cursor_pos - chunk.startIndex, len(chunk.text))

                layout_lines.append(
                    LayoutLine(
                        text=chunk.text,
                        hasCursor=has_cursor_in_chunk,
                        cursorPos=adjusted_cursor_pos if has_cursor_in_chunk else None,
                    )
                )

        return layout_lines

    def getText(self) -> str:
        return "\n".join(self.state.lines)

    def expandPasteMarkers(self, text: str) -> str:
        result = text
        for paste_id, paste_content in self.pastes.items():
            marker_regex = re.compile(rf"\[paste #{paste_id}( (\+\d+ lines|\d+ chars))?\]")
            result = marker_regex.sub(lambda _match, content=paste_content: content, result)
        return result

    def getExpandedText(self) -> str:
        return self.expandPasteMarkers(self.getText())

    def getLines(self) -> list[str]:
        return list(self.state.lines)

    def getCursor(self) -> dict[str, int]:
        return {"line": self.state.cursorLine, "col": self.state.cursorCol}

    def setText(self, text: str) -> None:
        self.cancelAutocomplete()
        self.lastAction = None
        self.historyIndex = -1
        normalized = self.normalizeText(text)
        if self.getText() != normalized:
            self.pushUndoSnapshot()
        self.pastes.clear()
        self.pasteCounter = 0
        self.setTextInternal(normalized)

    def insertTextAtCursor(self, text: str) -> None:
        if not text:
            return
        self.cancelAutocomplete()
        self.pushUndoSnapshot()
        self.lastAction = None
        self.historyIndex = -1
        self.insertTextAtCursorInternal(text)

    def normalizeText(self, text: str) -> str:
        return text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")

    def insertTextAtCursorInternal(self, text: str) -> None:
        if not text:
            return
        normalized = self.normalizeText(text)
        inserted_lines = normalized.split("\n")
        current_line = self.state.lines[self.state.cursorLine]
        before_cursor = current_line[: self.state.cursorCol]
        after_cursor = current_line[self.state.cursorCol :]

        if len(inserted_lines) == 1:
            self.state.lines[self.state.cursorLine] = before_cursor + normalized + after_cursor
            self.setCursorCol(self.state.cursorCol + len(normalized))
        else:
            self.state.lines = [
                *self.state.lines[: self.state.cursorLine],
                before_cursor + inserted_lines[0],
                *inserted_lines[1:-1],
                inserted_lines[-1] + after_cursor,
                *self.state.lines[self.state.cursorLine + 1 :],
            ]
            self.state.cursorLine += len(inserted_lines) - 1
            self.setCursorCol(len(inserted_lines[-1]))

        if self.onChange is not None:
            self.onChange(self.getText())

    def insertCharacter(self, char: str, skipUndoCoalescing: bool = False) -> None:
        self.historyIndex = -1
        if not skipUndoCoalescing:
            if isWhitespaceChar(char) or self.lastAction != "type-word":
                self.pushUndoSnapshot()
            self.lastAction = "type-word"

        line = self.state.lines[self.state.cursorLine]
        before = line[: self.state.cursorCol]
        after = line[self.state.cursorCol :]
        self.state.lines[self.state.cursorLine] = before + char + after
        self.setCursorCol(self.state.cursorCol + len(char))

        if self.onChange is not None:
            self.onChange(self.getText())

        if not self.autocompleteState:
            if char == "/" and self.isAtStartOfMessage():
                self.tryTriggerAutocomplete()
            elif char in {"@", "#"}:
                current_line = self.state.lines[self.state.cursorLine]
                text_before_cursor = current_line[: self.state.cursorCol]
                char_before_symbol = text_before_cursor[-2] if len(text_before_cursor) > 1 else None
                if len(text_before_cursor) == 1 or char_before_symbol in {" ", "\t"}:
                    self.tryTriggerAutocomplete()
            elif re.fullmatch(r"[A-Za-z0-9._-]", char):
                current_line = self.state.lines[self.state.cursorLine]
                text_before_cursor = current_line[: self.state.cursorCol]
                if self.isInSlashCommandContext(text_before_cursor):
                    self.tryTriggerAutocomplete()
                elif re.search(r"(?:^|[\s])[@#][^\s]*$", text_before_cursor):
                    self.tryTriggerAutocomplete()
        else:
            self.updateAutocomplete()

    def handlePaste(self, pastedText: str) -> None:
        self.cancelAutocomplete()
        self.historyIndex = -1
        self.lastAction = None
        self.pushUndoSnapshot()

        decoded_text = re.sub(
            r"\x1b\[(\d+);5u",
            lambda match: self._decodePasteControlSequence(match.group(0), int(match.group(1))),
            pastedText,
        )
        clean_text = self.normalizeText(decoded_text)
        filtered_text = "".join(char for char in clean_text if char == "\n" or ord(char) >= 32)

        if re.match(r"^[/~.]", filtered_text):
            current_line = self.state.lines[self.state.cursorLine]
            char_before_cursor = current_line[self.state.cursorCol - 1] if self.state.cursorCol > 0 else ""
            if char_before_cursor and re.match(r"\w", char_before_cursor):
                filtered_text = f" {filtered_text}"

        pasted_lines = filtered_text.split("\n")
        total_chars = len(filtered_text)
        if len(pasted_lines) > 10 or total_chars > 1000:
            self.pasteCounter += 1
            paste_id = self.pasteCounter
            self.pastes[paste_id] = filtered_text
            marker = (
                f"[paste #{paste_id} +{len(pasted_lines)} lines]"
                if len(pasted_lines) > 10
                else f"[paste #{paste_id} {total_chars} chars]"
            )
            self.insertTextAtCursorInternal(marker)
            return

        self.insertTextAtCursorInternal(filtered_text)

    def _decodePasteControlSequence(self, original: str, codepoint: int) -> str:
        if 97 <= codepoint <= 122:
            return chr(codepoint - 96)
        if 65 <= codepoint <= 90:
            return chr(codepoint - 64)
        return original

    def addNewLine(self) -> None:
        self.cancelAutocomplete()
        self.historyIndex = -1
        self.lastAction = None
        self.pushUndoSnapshot()

        current_line = self.state.lines[self.state.cursorLine]
        before = current_line[: self.state.cursorCol]
        after = current_line[self.state.cursorCol :]
        self.state.lines[self.state.cursorLine] = before
        self.state.lines.insert(self.state.cursorLine + 1, after)
        self.state.cursorLine += 1
        self.setCursorCol(0)

        if self.onChange is not None:
            self.onChange(self.getText())

    def shouldSubmitOnBackslashEnter(self, data: str, kb: Any) -> bool:
        if self.disableSubmit or not matchesKey(data, "enter"):
            return False
        submit_keys = kb.getKeys("tui.input.submit")
        has_shift_enter = "shift+enter" in submit_keys or "shift+return" in submit_keys
        if not has_shift_enter:
            return False
        current_line = self.state.lines[self.state.cursorLine]
        return self.state.cursorCol > 0 and current_line[self.state.cursorCol - 1] == "\\"

    def submitValue(self) -> None:
        self.cancelAutocomplete()
        result = self.expandPasteMarkers(self.getText()).strip()
        self.state = EditorState()
        self.pastes.clear()
        self.pasteCounter = 0
        self.historyIndex = -1
        self.scrollOffset = 0
        self.undoStack.clear()
        self.lastAction = None

        if self.onChange is not None:
            self.onChange("")
        if self.onSubmit is not None:
            self.onSubmit(result)

    def handleBackspace(self) -> None:
        self.historyIndex = -1
        self.lastAction = None

        if self.state.cursorCol > 0:
            self.pushUndoSnapshot()
            line = self.state.lines[self.state.cursorLine]
            before_cursor = line[: self.state.cursorCol]
            graphemes = self.segment(before_cursor)
            last_grapheme = graphemes[-1] if graphemes else None
            grapheme_length = len(last_grapheme.segment) if last_grapheme is not None else 1
            marker = PASTE_MARKER_SINGLE.match(last_grapheme.segment) if last_grapheme else None
            if marker:  # deleting a whole [paste #N …] block: drop it from the registry and renumber higher ids down by one
                target_id = int(marker.group(1))
                self.pastes.pop(target_id, None)
                self.pasteCounter -= 1
                for pid in sorted(k for k in self.pastes if k > target_id):
                    self.pastes[pid - 1] = self.pastes.pop(pid)
                self.state.lines = [
                    PASTE_MARKER_REGEX.sub(
                        lambda m: m.group(0) if int(m.group(1)) <= target_id
                        else f"[paste #{int(m.group(1)) - 1}{m.group(2) or ''}]",
                        ln)
                    for ln in self.state.lines
                ]
                line = self.state.lines[self.state.cursorLine]
            before = line[: self.state.cursorCol - grapheme_length]
            after = line[self.state.cursorCol :]
            self.state.lines[self.state.cursorLine] = before + after
            self.setCursorCol(self.state.cursorCol - grapheme_length)
        elif self.state.cursorLine > 0:
            self.pushUndoSnapshot()
            current_line = self.state.lines[self.state.cursorLine]
            previous_line = self.state.lines[self.state.cursorLine - 1]
            self.state.lines[self.state.cursorLine - 1] = previous_line + current_line
            del self.state.lines[self.state.cursorLine]
            self.state.cursorLine -= 1
            self.setCursorCol(len(previous_line))

        if self.onChange is not None:
            self.onChange(self.getText())

        if self.autocompleteState:
            self.updateAutocomplete()
        else:
            current_line = self.state.lines[self.state.cursorLine]
            text_before_cursor = current_line[: self.state.cursorCol]
            if self.isInSlashCommandContext(text_before_cursor):
                self.tryTriggerAutocomplete()
            elif re.search(r"(?:^|[\s])[@#][^\s]*$", text_before_cursor):
                self.tryTriggerAutocomplete()

    def setCursorCol(self, col: int) -> None:
        self.state.cursorCol = col
        self.preferredVisualCol = None
        self.snappedFromCursorCol = None

    def moveToVisualLine(
        self,
        visualLines: list[dict[str, int]],
        currentVisualLine: int,
        targetVisualLine: int,
    ) -> None:
        current_vl = visualLines[currentVisualLine] if 0 <= currentVisualLine < len(visualLines) else None
        target_vl = visualLines[targetVisualLine] if 0 <= targetVisualLine < len(visualLines) else None
        if current_vl is None or target_vl is None:
            return

        if self.snappedFromCursorCol is not None:
            visual_line_index = self.findVisualLineAt(
                visualLines,
                current_vl["logicalLine"],
                self.snappedFromCursorCol,
            )
            current_visual_col = self.snappedFromCursorCol - visualLines[visual_line_index]["startCol"]
        else:
            current_visual_col = self.state.cursorCol - current_vl["startCol"]

        is_last_source_segment = (
            currentVisualLine == len(visualLines) - 1
            or visualLines[currentVisualLine + 1]["logicalLine"] != current_vl["logicalLine"]
        )
        source_max_visual_col = current_vl["length"] if is_last_source_segment else max(0, current_vl["length"] - 1)

        is_last_target_segment = (
            targetVisualLine == len(visualLines) - 1
            or visualLines[targetVisualLine + 1]["logicalLine"] != target_vl["logicalLine"]
        )
        target_max_visual_col = target_vl["length"] if is_last_target_segment else max(0, target_vl["length"] - 1)

        move_to_visual_col = self.computeVerticalMoveColumn(
            current_visual_col,
            source_max_visual_col,
            target_max_visual_col,
        )
        self.state.cursorLine = target_vl["logicalLine"]
        target_col = target_vl["startCol"] + move_to_visual_col
        logical_line = self.state.lines[target_vl["logicalLine"]]
        self.state.cursorCol = min(target_col, len(logical_line))

        segments = self.segment(logical_line)
        for segment in segments:
            if segment.index > self.state.cursorCol:
                break
            if len(segment.segment) <= 1:
                continue
            if self.state.cursorCol < segment.index + len(segment.segment):
                is_continuation = segment.index < target_vl["startCol"]
                is_moving_down = targetVisualLine > currentVisualLine

                if is_continuation and is_moving_down:
                    segment_end = segment.index + len(segment.segment)
                    next_index = targetVisualLine + 1
                    while (
                        next_index < len(visualLines)
                        and visualLines[next_index]["logicalLine"] == target_vl["logicalLine"]
                        and visualLines[next_index]["startCol"] < segment_end
                    ):
                        next_index += 1
                    if next_index < len(visualLines):
                        self.moveToVisualLine(visualLines, currentVisualLine, next_index)
                        return

                self.snappedFromCursorCol = self.state.cursorCol
                self.state.cursorCol = segment.index
                return

        self.snappedFromCursorCol = None

    def computeVerticalMoveColumn(self, currentVisualCol: int, sourceMaxVisualCol: int, targetMaxVisualCol: int) -> int:
        has_preferred = self.preferredVisualCol is not None
        cursor_in_middle = currentVisualCol < sourceMaxVisualCol
        target_too_short = targetMaxVisualCol < currentVisualCol

        if not has_preferred or cursor_in_middle:
            if target_too_short:
                self.preferredVisualCol = currentVisualCol
                return targetMaxVisualCol
            self.preferredVisualCol = None
            return currentVisualCol

        target_cant_fit_preferred = targetMaxVisualCol < (self.preferredVisualCol or 0)
        if target_too_short or target_cant_fit_preferred:
            return targetMaxVisualCol

        result = self.preferredVisualCol or 0
        self.preferredVisualCol = None
        return result

    def moveToLineStart(self) -> None:
        self.lastAction = None
        self.setCursorCol(0)

    def moveToLineEnd(self) -> None:
        self.lastAction = None
        self.setCursorCol(len(self.state.lines[self.state.cursorLine]))

    def deleteToStartOfLine(self) -> None:
        self.historyIndex = -1
        current_line = self.state.lines[self.state.cursorLine]
        if self.state.cursorCol > 0:
            self.pushUndoSnapshot()
            deleted_text = current_line[: self.state.cursorCol]
            self.killRing.push(deleted_text, {"prepend": True, "accumulate": self.lastAction == "kill"})
            self.lastAction = "kill"
            self.state.lines[self.state.cursorLine] = current_line[self.state.cursorCol :]
            self.setCursorCol(0)
        elif self.state.cursorLine > 0:
            self.pushUndoSnapshot()
            self.killRing.push("\n", {"prepend": True, "accumulate": self.lastAction == "kill"})
            self.lastAction = "kill"
            previous_line = self.state.lines[self.state.cursorLine - 1]
            self.state.lines[self.state.cursorLine - 1] = previous_line + current_line
            del self.state.lines[self.state.cursorLine]
            self.state.cursorLine -= 1
            self.setCursorCol(len(previous_line))

        if self.onChange is not None:
            self.onChange(self.getText())

    def deleteToEndOfLine(self) -> None:
        self.historyIndex = -1
        current_line = self.state.lines[self.state.cursorLine]
        if self.state.cursorCol < len(current_line):
            self.pushUndoSnapshot()
            deleted_text = current_line[self.state.cursorCol :]
            self.killRing.push(deleted_text, {"prepend": False, "accumulate": self.lastAction == "kill"})
            self.lastAction = "kill"
            self.state.lines[self.state.cursorLine] = current_line[: self.state.cursorCol]
        elif self.state.cursorLine < len(self.state.lines) - 1:
            self.pushUndoSnapshot()
            self.killRing.push("\n", {"prepend": False, "accumulate": self.lastAction == "kill"})
            self.lastAction = "kill"
            next_line = self.state.lines[self.state.cursorLine + 1]
            self.state.lines[self.state.cursorLine] = current_line + next_line
            del self.state.lines[self.state.cursorLine + 1]

        if self.onChange is not None:
            self.onChange(self.getText())

    def deleteWordBackwards(self) -> None:
        self.historyIndex = -1
        current_line = self.state.lines[self.state.cursorLine]

        if self.state.cursorCol == 0:
            if self.state.cursorLine > 0:
                self.pushUndoSnapshot()
                self.killRing.push("\n", {"prepend": True, "accumulate": self.lastAction == "kill"})
                self.lastAction = "kill"
                previous_line = self.state.lines[self.state.cursorLine - 1]
                self.state.lines[self.state.cursorLine - 1] = previous_line + current_line
                del self.state.lines[self.state.cursorLine]
                self.state.cursorLine -= 1
                self.setCursorCol(len(previous_line))
        else:
            self.pushUndoSnapshot()
            was_kill = self.lastAction == "kill"
            old_cursor_col = self.state.cursorCol
            self.moveWordBackwards()
            delete_from = self.state.cursorCol
            self.setCursorCol(old_cursor_col)
            deleted_text = current_line[delete_from : self.state.cursorCol]
            self.killRing.push(deleted_text, {"prepend": True, "accumulate": was_kill})
            self.lastAction = "kill"
            self.state.lines[self.state.cursorLine] = current_line[:delete_from] + current_line[self.state.cursorCol :]
            self.setCursorCol(delete_from)

        if self.onChange is not None:
            self.onChange(self.getText())

    def deleteWordForward(self) -> None:
        self.historyIndex = -1
        current_line = self.state.lines[self.state.cursorLine]

        if self.state.cursorCol >= len(current_line):
            if self.state.cursorLine < len(self.state.lines) - 1:
                self.pushUndoSnapshot()
                self.killRing.push("\n", {"prepend": False, "accumulate": self.lastAction == "kill"})
                self.lastAction = "kill"
                next_line = self.state.lines[self.state.cursorLine + 1]
                self.state.lines[self.state.cursorLine] = current_line + next_line
                del self.state.lines[self.state.cursorLine + 1]
        else:
            self.pushUndoSnapshot()
            was_kill = self.lastAction == "kill"
            old_cursor_col = self.state.cursorCol
            self.moveWordForwards()
            delete_to = self.state.cursorCol
            self.setCursorCol(old_cursor_col)
            deleted_text = current_line[self.state.cursorCol : delete_to]
            self.killRing.push(deleted_text, {"prepend": False, "accumulate": was_kill})
            self.lastAction = "kill"
            self.state.lines[self.state.cursorLine] = current_line[: self.state.cursorCol] + current_line[delete_to:]

        if self.onChange is not None:
            self.onChange(self.getText())

    def handleForwardDelete(self) -> None:
        self.historyIndex = -1
        self.lastAction = None
        current_line = self.state.lines[self.state.cursorLine]

        if self.state.cursorCol < len(current_line):
            self.pushUndoSnapshot()
            after_cursor = current_line[self.state.cursorCol :]
            graphemes = self.segment(after_cursor)
            first_grapheme = graphemes[0] if graphemes else None
            grapheme_length = len(first_grapheme.segment) if first_grapheme is not None else 1
            before = current_line[: self.state.cursorCol]
            after = current_line[self.state.cursorCol + grapheme_length :]
            self.state.lines[self.state.cursorLine] = before + after
        elif self.state.cursorLine < len(self.state.lines) - 1:
            self.pushUndoSnapshot()
            next_line = self.state.lines[self.state.cursorLine + 1]
            self.state.lines[self.state.cursorLine] = current_line + next_line
            del self.state.lines[self.state.cursorLine + 1]

        if self.onChange is not None:
            self.onChange(self.getText())

        if self.autocompleteState:
            self.updateAutocomplete()
        else:
            current_line = self.state.lines[self.state.cursorLine]
            text_before_cursor = current_line[: self.state.cursorCol]
            if self.isInSlashCommandContext(text_before_cursor):
                self.tryTriggerAutocomplete()
            elif re.search(r"(?:^|[\s])[@#][^\s]*$", text_before_cursor):
                self.tryTriggerAutocomplete()

    def buildVisualLineMap(self, width: int) -> list[dict[str, int]]:
        visual_lines: list[dict[str, int]] = []
        for logical_line, line in enumerate(self.state.lines):
            if line == "":
                visual_lines.append({"logicalLine": logical_line, "startCol": 0, "length": 0})
            elif visibleWidth(line) <= width:
                visual_lines.append({"logicalLine": logical_line, "startCol": 0, "length": len(line)})
            else:
                chunks = word_wrap_line(line, width, self.segment(line))
                for chunk in chunks:
                    visual_lines.append(
                        {
                            "logicalLine": logical_line,
                            "startCol": chunk.startIndex,
                            "length": chunk.endIndex - chunk.startIndex,
                        }
                    )
        return visual_lines

    def findVisualLineAt(self, visualLines: list[dict[str, int]], line: int, col: int) -> int:
        for index, visual_line in enumerate(visualLines):
            if visual_line["logicalLine"] != line:
                continue
            offset = col - visual_line["startCol"]
            is_last_segment_of_line = (
                index == len(visualLines) - 1 or visualLines[index + 1]["logicalLine"] != visual_line["logicalLine"]
            )
            if offset >= 0 and (
                offset < visual_line["length"] or (is_last_segment_of_line and offset == visual_line["length"])
            ):
                return index
        return len(visualLines) - 1

    def findCurrentVisualLine(self, visualLines: list[dict[str, int]]) -> int:
        return self.findVisualLineAt(visualLines, self.state.cursorLine, self.state.cursorCol)

    def moveCursor(self, deltaLine: int, deltaCol: int) -> None:
        self.lastAction = None
        visual_lines = self.buildVisualLineMap(self.lastWidth)
        current_visual_line = self.findCurrentVisualLine(visual_lines)

        if deltaLine != 0:
            target_visual_line = current_visual_line + deltaLine
            if 0 <= target_visual_line < len(visual_lines):
                self.moveToVisualLine(visual_lines, current_visual_line, target_visual_line)

        if deltaCol != 0:
            current_line = self.state.lines[self.state.cursorLine]
            if deltaCol > 0:
                if self.state.cursorCol < len(current_line):
                    after_cursor = current_line[self.state.cursorCol :]
                    graphemes = self.segment(after_cursor)
                    first_grapheme = graphemes[0] if graphemes else None
                    self.setCursorCol(self.state.cursorCol + (len(first_grapheme.segment) if first_grapheme else 1))
                elif self.state.cursorLine < len(self.state.lines) - 1:
                    self.state.cursorLine += 1
                    self.setCursorCol(0)
                else:
                    current_visual = visual_lines[current_visual_line]
                    self.preferredVisualCol = self.state.cursorCol - current_visual["startCol"]
            else:
                if self.state.cursorCol > 0:
                    before_cursor = current_line[: self.state.cursorCol]
                    graphemes = self.segment(before_cursor)
                    last_grapheme = graphemes[-1] if graphemes else None
                    self.setCursorCol(self.state.cursorCol - (len(last_grapheme.segment) if last_grapheme else 1))
                elif self.state.cursorLine > 0:
                    self.state.cursorLine -= 1
                    self.setCursorCol(len(self.state.lines[self.state.cursorLine]))

        if self.autocompleteState:
            self.updateAutocomplete()  # cursor moved: re-query, or close a stale menu (Tab completing a stale candidate is a real hazard)

    def pageScroll(self, direction: int) -> None:
        self.lastAction = None
        terminal_rows = self.tui.terminal.rows
        page_size = max(5, int(terminal_rows * 0.3))
        visual_lines = self.buildVisualLineMap(self.lastWidth)
        current_visual_line = self.findCurrentVisualLine(visual_lines)
        target_visual_line = max(0, min(len(visual_lines) - 1, current_visual_line + direction * page_size))
        self.moveToVisualLine(visual_lines, current_visual_line, target_visual_line)

    def moveWordBackwards(self) -> None:
        self.lastAction = None
        current_line = self.state.lines[self.state.cursorLine]
        if self.state.cursorCol == 0:
            if self.state.cursorLine > 0:
                self.state.cursorLine -= 1
                self.setCursorCol(len(self.state.lines[self.state.cursorLine]))
            return

        text_before_cursor = current_line[: self.state.cursorCol]
        graphemes = self.segment(text_before_cursor)
        new_col = self.state.cursorCol

        while graphemes and not is_paste_marker(graphemes[-1].segment) and isWhitespaceChar(graphemes[-1].segment):
            new_col -= len(graphemes.pop().segment)

        if graphemes:
            last_grapheme = graphemes[-1].segment
            if is_paste_marker(last_grapheme):
                new_col -= len(graphemes.pop().segment)
            elif _is_word_nav_punct(last_grapheme):
                while (
                    graphemes
                    and _is_word_nav_punct(graphemes[-1].segment)
                    and not is_paste_marker(graphemes[-1].segment)
                ):
                    new_col -= len(graphemes.pop().segment)
            elif _CJK_BREAK_RE.search(last_grapheme):
                new_col -= len(graphemes.pop().segment)   # each CJK character is a word
            else:
                while (
                    graphemes
                    and not isWhitespaceChar(graphemes[-1].segment)
                    and not _is_word_nav_punct(graphemes[-1].segment)
                    and not is_paste_marker(graphemes[-1].segment)
                    and not _CJK_BREAK_RE.search(graphemes[-1].segment)
                ):
                    new_col -= len(graphemes.pop().segment)

        self.setCursorCol(new_col)

    def yank(self) -> None:
        if self.killRing.length == 0:
            return
        self.pushUndoSnapshot()
        text = self.killRing.peek()
        if text is None:
            return
        self.insertYankedText(text)
        self.lastAction = "yank"

    def yankPop(self) -> None:
        if self.lastAction != "yank" or self.killRing.length <= 1:
            return
        self.pushUndoSnapshot()
        self.deleteYankedText()
        self.killRing.rotate()
        text = self.killRing.peek()
        if text is None:
            return
        self.insertYankedText(text)
        self.lastAction = "yank"

    def insertYankedText(self, text: str) -> None:
        self.historyIndex = -1
        lines = text.split("\n")
        if len(lines) == 1:
            current_line = self.state.lines[self.state.cursorLine]
            before = current_line[: self.state.cursorCol]
            after = current_line[self.state.cursorCol :]
            self.state.lines[self.state.cursorLine] = before + text + after
            self.setCursorCol(self.state.cursorCol + len(text))
        else:
            current_line = self.state.lines[self.state.cursorLine]
            before = current_line[: self.state.cursorCol]
            after = current_line[self.state.cursorCol :]
            self.state.lines[self.state.cursorLine] = before + lines[0]
            for index, line in enumerate(lines[1:-1], start=1):
                self.state.lines.insert(self.state.cursorLine + index, line)
            last_line_index = self.state.cursorLine + len(lines) - 1
            self.state.lines.insert(last_line_index, lines[-1] + after)
            self.state.cursorLine = last_line_index
            self.setCursorCol(len(lines[-1]))

        if self.onChange is not None:
            self.onChange(self.getText())

    def deleteYankedText(self) -> None:
        yanked_text = self.killRing.peek()
        if not yanked_text:
            return
        yank_lines = yanked_text.split("\n")

        if len(yank_lines) == 1:
            current_line = self.state.lines[self.state.cursorLine]
            delete_len = len(yanked_text)
            before = current_line[: self.state.cursorCol - delete_len]
            after = current_line[self.state.cursorCol :]
            self.state.lines[self.state.cursorLine] = before + after
            self.setCursorCol(self.state.cursorCol - delete_len)
        else:
            start_line = self.state.cursorLine - (len(yank_lines) - 1)
            start_col = len(self.state.lines[start_line]) - len(yank_lines[0])
            after_cursor = self.state.lines[self.state.cursorLine][self.state.cursorCol :]
            before_yank = self.state.lines[start_line][:start_col]
            self.state.lines[start_line : self.state.cursorLine + 1] = [before_yank + after_cursor]
            self.state.cursorLine = start_line
            self.setCursorCol(start_col)

        if self.onChange is not None:
            self.onChange(self.getText())

    def pushUndoSnapshot(self) -> None:
        self.undoStack.push({
            "state": EditorState(lines=self.state.lines, cursorLine=self.state.cursorLine,
                                 cursorCol=self.state.cursorCol),
            "pastes": self.pastes,
            "pasteCounter": self.pasteCounter,
        })

    def undo(self) -> None:
        self.historyIndex = -1
        snapshot = self.undoStack.pop()
        if snapshot is None:
            return
        self.state = snapshot["state"]
        self.pastes = snapshot["pastes"]
        self.pasteCounter = snapshot["pasteCounter"]
        self.lastAction = None
        self.preferredVisualCol = None
        if self.onChange is not None:
            self.onChange(self.getText())

    def jumpToChar(self, char: str, direction: str) -> None:
        self.lastAction = None
        is_forward = direction == "forward"
        end = len(self.state.lines) if is_forward else -1
        step = 1 if is_forward else -1

        line_idx = self.state.cursorLine
        while line_idx != end:
            line = self.state.lines[line_idx]
            is_current_line = line_idx == self.state.cursorLine
            search_from = (
                self.state.cursorCol + 1
                if is_current_line and is_forward
                else self.state.cursorCol - 1
                if is_current_line
                else None
            )
            found_index = (
                line.find(char, search_from)
                if is_forward
                else (line.rfind(char) if search_from is None else line.rfind(char, 0, search_from + 1))
            )
            if found_index != -1:
                self.state.cursorLine = line_idx
                self.setCursorCol(found_index)
                return
            line_idx += step

    def moveWordForwards(self) -> None:
        self.lastAction = None
        current_line = self.state.lines[self.state.cursorLine]
        if self.state.cursorCol >= len(current_line):
            if self.state.cursorLine < len(self.state.lines) - 1:
                self.state.cursorLine += 1
                self.setCursorCol(0)
            return

        segments = iter(self.segment(current_line[self.state.cursorCol :]))
        next_segment = next(segments, None)
        new_col = self.state.cursorCol

        while (
            next_segment is not None
            and not is_paste_marker(next_segment.segment)
            and isWhitespaceChar(next_segment.segment)
        ):
            new_col += len(next_segment.segment)
            next_segment = next(segments, None)

        if next_segment is not None:
            first_grapheme = next_segment.segment
            if is_paste_marker(first_grapheme):
                new_col += len(first_grapheme)
            elif _is_word_nav_punct(first_grapheme):
                while (
                    next_segment is not None
                    and _is_word_nav_punct(next_segment.segment)
                    and not is_paste_marker(next_segment.segment)
                ):
                    new_col += len(next_segment.segment)
                    next_segment = next(segments, None)
            elif _CJK_BREAK_RE.search(first_grapheme):
                new_col += len(first_grapheme)             # each CJK character is a word
            else:
                while (
                    next_segment is not None
                    and not isWhitespaceChar(next_segment.segment)
                    and not _is_word_nav_punct(next_segment.segment)
                    and not is_paste_marker(next_segment.segment)
                    and not _CJK_BREAK_RE.search(next_segment.segment)
                ):
                    new_col += len(next_segment.segment)
                    next_segment = next(segments, None)

        self.setCursorCol(new_col)

    def isSlashMenuAllowed(self) -> bool:
        return self.state.cursorLine == 0

    def isAtStartOfMessage(self) -> bool:
        if not self.isSlashMenuAllowed():
            return False
        current_line = self.state.lines[self.state.cursorLine]
        before_cursor = current_line[: self.state.cursorCol]
        return before_cursor.strip() in {"", "/"}

    def isInSlashCommandContext(self, textBeforeCursor: str) -> bool:
        return self.isSlashMenuAllowed() and textBeforeCursor.lstrip().startswith("/")

    def handleTabCompletion(self) -> None:
        if self.autocompleteProvider is None:
            return
        current_line = self.state.lines[self.state.cursorLine]
        before_cursor = current_line[: self.state.cursorCol]

        if self.isInSlashCommandContext(before_cursor) and " " not in before_cursor.lstrip():
            self.handleSlashCommandCompletion()
        else:
            self.forceFileAutocomplete(True)

    def handleSlashCommandCompletion(self) -> None:
        if self.autocompleteProvider is None:
            return
        self.requestAutocomplete(force=False, explicitTab=True)

    def forceFileAutocomplete(self, explicitTab: bool = False) -> None:
        if self.autocompleteProvider is None:
            return
        self.requestAutocomplete(force=True, explicitTab=explicitTab)

    def getBestAutocompleteMatchIndex(self, items: list[AutocompleteItem], prefix: str) -> int:
        if not prefix:
            return -1

        first_prefix_index = -1
        for index, item in enumerate(items):
            value = item.value
            if value == prefix:
                return index
            if first_prefix_index == -1 and value.startswith(prefix):
                first_prefix_index = index
        return first_prefix_index

    def createAutocompleteList(self, prefix: str, items: list[AutocompleteItem]) -> SelectList:
        layout = SLASH_COMMAND_SELECT_LIST_LAYOUT if prefix.startswith("/") else None
        select_items = [SelectItem(value=item.value, label=item.label, description=item.description) for item in items]
        return SelectList(select_items, self.autocompleteMaxVisible, self.theme.selectList, layout)

    def tryTriggerAutocomplete(self, explicitTab: bool = False) -> None:
        if self.autocompleteProvider is None:
            return
        self.requestAutocomplete(force=False, explicitTab=explicitTab)

    def requestAutocomplete(self, *, force: bool, explicitTab: bool) -> None:
        if self.autocompleteProvider is None:
            return

        if force:
            should_trigger = getattr(self.autocompleteProvider, "shouldTriggerFileCompletion", None)
            if callable(should_trigger) and not should_trigger(
                self.state.lines,
                self.state.cursorLine,
                self.state.cursorCol,
            ):
                return

        self.cancelAutocompleteRequest()
        start_token = self.autocompleteStartToken + 1
        self.autocompleteStartToken = start_token
        debounce_ms = self.getAutocompleteDebounceMs(force=force, explicitTab=explicitTab)
        loop = _get_running_loop()
        if loop is None:
            return

        if debounce_ms > 0:
            self.autocompleteDebounceTimer = loop.call_later(
                debounce_ms / 1000,
                self.startAutocompleteRequest,
                start_token,
                force,
                explicitTab,
            )
            return

        self.startAutocompleteRequest(start_token, force, explicitTab)

    def startAutocompleteRequest(self, startToken: int, force: bool, explicitTab: bool) -> None:
        if startToken != self.autocompleteStartToken or self.autocompleteProvider is None:
            return

        loop = _get_running_loop()
        if loop is None:
            return

        previous_task = self.autocompleteRequestTask

        async def run() -> None:
            if previous_task is not None:
                await previous_task

            if startToken != self.autocompleteStartToken or self.autocompleteProvider is None:
                return

            controller = _AutocompleteAbortController()
            self.autocompleteAbort = controller
            request_id = self.autocompleteRequestId + 1
            self.autocompleteRequestId = request_id
            snapshot_text = self.getText()
            snapshot_line = self.state.cursorLine
            snapshot_col = self.state.cursorCol

            await self.runAutocompleteRequest(
                request_id=request_id,
                controller=controller,
                snapshotText=snapshot_text,
                snapshotLine=snapshot_line,
                snapshotCol=snapshot_col,
                force=force,
                explicitTab=explicitTab,
            )

        task = loop.create_task(run())
        self.autocompleteRequestTask = task

    def getAutocompleteDebounceMs(self, *, force: bool, explicitTab: bool) -> int:
        if explicitTab or force:
            return 0
        current_line = self.state.lines[self.state.cursorLine]
        text_before_cursor = current_line[: self.state.cursorCol]
        is_symbol_autocomplete = (
            re.search(r'(?:^|[ \t])(?:@(?:"[^"]*|[^\s]*)|#[^\s]*)$', text_before_cursor) is not None
        )
        return ATTACHMENT_AUTOCOMPLETE_DEBOUNCE_MS if is_symbol_autocomplete else 0

    async def runAutocompleteRequest(
        self,
        *,
        request_id: int,
        controller: _AutocompleteAbortController,
        snapshotText: str,
        snapshotLine: int,
        snapshotCol: int,
        force: bool,
        explicitTab: bool,
    ) -> None:
        if self.autocompleteProvider is None:
            return

        suggestions = await self.autocompleteProvider.getSuggestions(
            self.state.lines,
            self.state.cursorLine,
            self.state.cursorCol,
            {"signal": controller.signal, "force": force},
        )

        if not self.isAutocompleteRequestCurrent(
            requestId=request_id,
            controller=controller,
            snapshotText=snapshotText,
            snapshotLine=snapshotLine,
            snapshotCol=snapshotCol,
        ):
            return

        self.autocompleteAbort = None

        if suggestions is None or not isinstance(suggestions.items, list) or not suggestions.items:
            self.cancelAutocomplete()
            self.tui.requestRender()
            return

        if force and explicitTab and len(suggestions.items) == 1:
            self.pushUndoSnapshot()
            self.lastAction = None
            self.applyAutocompleteSelection(suggestions.items[0], suggestions.prefix)
            if self.onChange is not None:
                self.onChange(self.getText())
            self.tui.requestRender()
            return

        self.applyAutocompleteSuggestions(suggestions, "force" if force else "regular")
        self.tui.requestRender()

    def isAutocompleteRequestCurrent(
        self,
        *,
        requestId: int,
        controller: _AutocompleteAbortController,
        snapshotText: str,
        snapshotLine: int,
        snapshotCol: int,
    ) -> bool:
        return (
            not controller.signal.aborted
            and requestId == self.autocompleteRequestId
            and self.getText() == snapshotText
            and self.state.cursorLine == snapshotLine
            and self.state.cursorCol == snapshotCol
        )

    def applyAutocompleteSelection(self, item: AutocompleteItem | SelectItem, prefix: str | None = None) -> None:
        if self.autocompleteProvider is None:
            return
        result = self.autocompleteProvider.applyCompletion(
            self.state.lines,
            self.state.cursorLine,
            self.state.cursorCol,
            item,
            self.autocompletePrefix if prefix is None else prefix,
        )
        self.state.lines = list(result["lines"])  # type: ignore[index]
        self.state.cursorLine = int(result["cursorLine"])  # type: ignore[index]
        self.setCursorCol(int(result["cursorCol"]))  # type: ignore[index]

    def applyAutocompleteSuggestions(self, suggestions: AutocompleteSuggestions, state: str) -> None:
        self.autocompletePrefix = suggestions.prefix
        self.autocompleteList = self.createAutocompleteList(suggestions.prefix, suggestions.items)
        best_match_index = self.getBestAutocompleteMatchIndex(suggestions.items, suggestions.prefix)
        if best_match_index >= 0 and self.autocompleteList is not None:
            self.autocompleteList.setSelectedIndex(best_match_index)
        self.autocompleteState = state

    def clearAutocompleteUi(self) -> None:
        self.autocompleteState = None
        self.autocompleteList = None
        self.autocompletePrefix = ""

    def cancelAutocomplete(self) -> None:
        self.cancelAutocompleteRequest()
        self.clearAutocompleteUi()

    def cancelAutocompleteRequest(self) -> None:
        self.autocompleteStartToken += 1
        if self.autocompleteDebounceTimer is not None:
            self.autocompleteDebounceTimer.cancel()
            self.autocompleteDebounceTimer = None
        if self.autocompleteAbort is not None:
            self.autocompleteAbort.abort()
            self.autocompleteAbort = None

    def isShowingAutocomplete(self) -> bool:
        return self.autocompleteState is not None

    def updateAutocomplete(self) -> None:
        if self.autocompleteProvider is None or self.autocompleteState is None:
            return
        self.requestAutocomplete(force=self.autocompleteState == "force", explicitTab=False)


segmentWithMarkers = segment_with_markers
isPasteMarker = is_paste_marker
wordWrapLine = word_wrap_line

__all__ = [
    "Editor",
    "EditorOptions",
    "EditorTheme",
    "TextChunk",
    "wordWrapLine",
]

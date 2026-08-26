"""Single-line text input with kill-ring and undo support."""

from __future__ import annotations

from dataclasses import dataclass

from misaka.ui.tui.keybindings import getKeybindings
from misaka.ui.tui.keys import decodeKittyPrintable
from misaka.ui.tui.kill_ring import KillRing
from misaka.ui.tui.tui import CURSOR_MARKER
from misaka.ui.tui.undo_stack import UndoStack
from misaka.ui.tui.components.editor import _is_word_nav_punct as _nav_punct
from misaka.ui.tui.utils import _CJK_BREAK_RE
from misaka.ui.tui.utils import getSegmenter, isPunctuationChar, isWhitespaceChar, sliceByColumn, visibleWidth

segmenter = getSegmenter()


@dataclass(slots=True)
class InputState:
    value: str
    cursor: int


class Input:
    def __init__(self) -> None:
        self.value = ""
        self.cursor = 0
        self.onSubmit = None
        self.onEscape = None
        self.focused = False
        self.pasteBuffer = ""
        self.isInPaste = False
        self.killRing = KillRing()
        self.lastAction: str | None = None
        self.undoStack: UndoStack[InputState] = UndoStack()

    def getValue(self) -> str:
        return self.value

    def setValue(self, value: str) -> None:
        self.value = value
        self.cursor = min(self.cursor, len(value))

    def handleInput(self, data: str) -> None:
        if "\x1b[200~" in data:
            self.isInPaste = True
            self.pasteBuffer = ""
            data = data.replace("\x1b[200~", "", 1)

        if self.isInPaste:
            self.pasteBuffer += data
            end_index = self.pasteBuffer.find("\x1b[201~")
            if end_index != -1:
                paste_content = self.pasteBuffer[:end_index]
                self.handlePaste(paste_content)
                self.isInPaste = False
                remaining = self.pasteBuffer[end_index + 6 :]
                self.pasteBuffer = ""
                if remaining:
                    self.handleInput(remaining)
            return

        kb = getKeybindings()

        if kb.matches(data, "tui.select.cancel"):
            if callable(self.onEscape):
                self.onEscape()
            return

        if kb.matches(data, "tui.editor.undo"):
            self.undo()
            return

        if kb.matches(data, "tui.input.submit") or data == "\n":
            if callable(self.onSubmit):
                self.onSubmit(self.value)
            return

        if kb.matches(data, "tui.editor.deleteCharBackward"):
            self.handleBackspace()
            return
        if kb.matches(data, "tui.editor.deleteCharForward"):
            self.handleForwardDelete()
            return
        if kb.matches(data, "tui.editor.deleteWordBackward"):
            self.deleteWordBackwards()
            return
        if kb.matches(data, "tui.editor.deleteWordForward"):
            self.deleteWordForward()
            return
        if kb.matches(data, "tui.editor.deleteToLineStart"):
            self.deleteToLineStart()
            return
        if kb.matches(data, "tui.editor.deleteToLineEnd"):
            self.deleteToLineEnd()
            return
        if kb.matches(data, "tui.editor.yank"):
            self.yank()
            return
        if kb.matches(data, "tui.editor.yankPop"):
            self.yankPop()
            return

        if kb.matches(data, "tui.editor.cursorLeft"):
            self.lastAction = None
            if self.cursor > 0:
                before_cursor = self.value[: self.cursor]
                graphemes = list(segmenter.segment(before_cursor))
                last_grapheme = graphemes[-1] if graphemes else None
                self.cursor -= len(last_grapheme.segment) if last_grapheme is not None else 1
            return

        if kb.matches(data, "tui.editor.cursorRight"):
            self.lastAction = None
            if self.cursor < len(self.value):
                after_cursor = self.value[self.cursor :]
                graphemes = list(segmenter.segment(after_cursor))
                first_grapheme = graphemes[0] if graphemes else None
                self.cursor += len(first_grapheme.segment) if first_grapheme is not None else 1
            return

        if kb.matches(data, "tui.editor.cursorLineStart"):
            self.lastAction = None
            self.cursor = 0
            return

        if kb.matches(data, "tui.editor.cursorLineEnd"):
            self.lastAction = None
            self.cursor = len(self.value)
            return

        if kb.matches(data, "tui.editor.cursorWordLeft"):
            self.moveWordBackwards()
            return

        if kb.matches(data, "tui.editor.cursorWordRight"):
            self.moveWordForwards()
            return

        kitty_printable = decodeKittyPrintable(data)
        if kitty_printable is not None:
            self.insertCharacter(kitty_printable)
            return

        has_control_chars = any(
            (ord(char) < 32 or ord(char) == 0x7F or 0x80 <= ord(char) <= 0x9F) for char in data
        )
        if not has_control_chars:
            self.insertCharacter(data)

    def insertCharacter(self, char: str) -> None:
        if isWhitespaceChar(char) or self.lastAction != "type-word":
            self.pushUndo()
        self.lastAction = "type-word"
        self.value = self.value[: self.cursor] + char + self.value[self.cursor :]
        self.cursor += len(char)

    def handleBackspace(self) -> None:
        self.lastAction = None
        if self.cursor > 0:
            self.pushUndo()
            before_cursor = self.value[: self.cursor]
            graphemes = list(segmenter.segment(before_cursor))
            last_grapheme = graphemes[-1] if graphemes else None
            grapheme_length = len(last_grapheme.segment) if last_grapheme is not None else 1
            self.value = self.value[: self.cursor - grapheme_length] + self.value[self.cursor :]
            self.cursor -= grapheme_length

    def handleForwardDelete(self) -> None:
        self.lastAction = None
        if self.cursor < len(self.value):
            self.pushUndo()
            after_cursor = self.value[self.cursor :]
            graphemes = list(segmenter.segment(after_cursor))
            first_grapheme = graphemes[0] if graphemes else None
            grapheme_length = len(first_grapheme.segment) if first_grapheme is not None else 1
            self.value = self.value[: self.cursor] + self.value[self.cursor + grapheme_length :]

    def deleteToLineStart(self) -> None:
        if self.cursor == 0:
            return
        self.pushUndo()
        deleted_text = self.value[: self.cursor]
        self.killRing.push(
            deleted_text,
            {"prepend": True, "accumulate": self.lastAction == "kill"},
        )
        self.lastAction = "kill"
        self.value = self.value[self.cursor :]
        self.cursor = 0

    def deleteToLineEnd(self) -> None:
        if self.cursor >= len(self.value):
            return
        self.pushUndo()
        deleted_text = self.value[self.cursor :]
        self.killRing.push(
            deleted_text,
            {"prepend": False, "accumulate": self.lastAction == "kill"},
        )
        self.lastAction = "kill"
        self.value = self.value[: self.cursor]

    def deleteWordBackwards(self) -> None:
        if self.cursor == 0:
            return
        was_kill = self.lastAction == "kill"
        self.pushUndo()
        old_cursor = self.cursor
        self.moveWordBackwards()
        delete_from = self.cursor
        self.cursor = old_cursor

        deleted_text = self.value[delete_from : self.cursor]
        self.killRing.push(deleted_text, {"prepend": True, "accumulate": was_kill})
        self.lastAction = "kill"
        self.value = self.value[:delete_from] + self.value[self.cursor :]
        self.cursor = delete_from

    def deleteWordForward(self) -> None:
        if self.cursor >= len(self.value):
            return
        was_kill = self.lastAction == "kill"
        self.pushUndo()
        old_cursor = self.cursor
        self.moveWordForwards()
        delete_to = self.cursor
        self.cursor = old_cursor

        deleted_text = self.value[self.cursor : delete_to]
        self.killRing.push(deleted_text, {"prepend": False, "accumulate": was_kill})
        self.lastAction = "kill"
        self.value = self.value[: self.cursor] + self.value[delete_to:]

    def yank(self) -> None:
        text = self.killRing.peek()
        if not text:
            return
        self.pushUndo()
        self.value = self.value[: self.cursor] + text + self.value[self.cursor :]
        self.cursor += len(text)
        self.lastAction = "yank"

    def yankPop(self) -> None:
        if self.lastAction != "yank" or self.killRing.length <= 1:
            return

        self.pushUndo()
        previous_text = self.killRing.peek() or ""
        self.value = self.value[: self.cursor - len(previous_text)] + self.value[self.cursor :]
        self.cursor -= len(previous_text)

        self.killRing.rotate()
        text = self.killRing.peek() or ""
        self.value = self.value[: self.cursor] + text + self.value[self.cursor :]
        self.cursor += len(text)
        self.lastAction = "yank"

    def pushUndo(self) -> None:
        self.undoStack.push(InputState(value=self.value, cursor=self.cursor))

    def undo(self) -> None:
        snapshot = self.undoStack.pop()
        if snapshot is None:
            return
        self.value = snapshot.value
        self.cursor = snapshot.cursor
        self.lastAction = None

    def moveWordBackwards(self) -> None:
        if self.cursor == 0:
            return
        self.lastAction = None
        text_before_cursor = self.value[: self.cursor]
        graphemes = list(segmenter.segment(text_before_cursor))

        while graphemes and isWhitespaceChar(graphemes[-1].segment):
            self.cursor -= len(graphemes.pop().segment)

        if graphemes:
            last_grapheme = graphemes[-1].segment
            if _nav_punct(last_grapheme):
                while graphemes and _nav_punct(graphemes[-1].segment):
                    self.cursor -= len(graphemes.pop().segment)
            elif _CJK_BREAK_RE.search(last_grapheme):
                self.cursor -= len(graphemes.pop().segment)   # each CJK character is a word
            else:
                while (
                    graphemes
                    and not isWhitespaceChar(graphemes[-1].segment)
                    and not _nav_punct(graphemes[-1].segment)
                    and not _CJK_BREAK_RE.search(graphemes[-1].segment)
                ):
                    self.cursor -= len(graphemes.pop().segment)

    def moveWordForwards(self) -> None:
        if self.cursor >= len(self.value):
            return
        self.lastAction = None
        segments = iter(segmenter.segment(self.value[self.cursor :]))

        next_segment = next(segments, None)
        while next_segment is not None and isWhitespaceChar(next_segment.segment):
            self.cursor += len(next_segment.segment)
            next_segment = next(segments, None)

        if next_segment is not None:
            first_grapheme = next_segment.segment
            if _nav_punct(first_grapheme):
                while next_segment is not None and _nav_punct(next_segment.segment):
                    self.cursor += len(next_segment.segment)
                    next_segment = next(segments, None)
            elif _CJK_BREAK_RE.search(first_grapheme):
                self.cursor += len(first_grapheme)             # each CJK character is a word
            else:
                while (
                    next_segment is not None
                    and not isWhitespaceChar(next_segment.segment)
                    and not _nav_punct(next_segment.segment)
                    and not _CJK_BREAK_RE.search(next_segment.segment)
                ):
                    self.cursor += len(next_segment.segment)
                    next_segment = next(segments, None)

    def handlePaste(self, pastedText: str) -> None:
        self.lastAction = None
        self.pushUndo()
        clean_text = (
            pastedText.replace("\r\n", "").replace("\r", "").replace("\n", "").replace("\t", "    ")
        )
        self.value = self.value[: self.cursor] + clean_text + self.value[self.cursor :]
        self.cursor += len(clean_text)

    def invalidate(self) -> None:
        return None

    def render(self, width: int) -> list[str]:
        prompt = "> "
        available_width = width - len(prompt)
        if available_width <= 0:
            return [prompt]

        visible_text = ""
        cursor_display = self.cursor
        total_width = visibleWidth(self.value)

        if total_width < available_width:
            visible_text = self.value
        else:
            scroll_width = available_width - 1 if self.cursor == len(self.value) else available_width
            cursor_col = visibleWidth(self.value[: self.cursor])

            if scroll_width > 0:
                half_width = scroll_width // 2
                if cursor_col < half_width:
                    start_col = 0
                elif cursor_col > total_width - half_width:
                    start_col = max(0, total_width - scroll_width)
                else:
                    start_col = max(0, cursor_col - half_width)

                visible_text = sliceByColumn(self.value, start_col, scroll_width, True)
                before_cursor = sliceByColumn(self.value, start_col, max(0, cursor_col - start_col), True)
                cursor_display = len(before_cursor)
            else:
                visible_text = ""
                cursor_display = 0

        graphemes = list(segmenter.segment(visible_text[cursor_display:]))
        cursor_grapheme = graphemes[0] if graphemes else None

        before_cursor = visible_text[:cursor_display]
        at_cursor = cursor_grapheme.segment if cursor_grapheme is not None else " "
        after_cursor = visible_text[cursor_display + len(at_cursor) :]
        marker = CURSOR_MARKER if self.focused else ""
        cursor_char = f"\x1b[7m{at_cursor}\x1b[27m"
        text_with_cursor = before_cursor + marker + cursor_char + after_cursor
        visual_length = visibleWidth(text_with_cursor)
        padding = " " * max(0, available_width - visual_length)
        return [prompt + text_with_cursor + padding]


__all__ = ["Input"]

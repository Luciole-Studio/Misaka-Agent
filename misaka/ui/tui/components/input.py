"""Translated from pi-tui's ``components/input.ts`` (v1.3.0).

MISAKA fork: no alt screen (D1), so ``handleMouse`` and the render geometry only it reads (``renderedStartColumn``)
are not ported; only the alt screen produces mouse events. String offsets count code points (Python strings), where
upstream counts UTF-16 code units.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from misaka.ui.tui.keybindings import getKeybindings
from misaka.ui.tui.keys import decodeKittyPrintable
from misaka.ui.tui.kill_ring import KillRing
from misaka.ui.tui.tui import CURSOR_MARKER, Component, renderFakeCursor
from misaka.ui.tui.undo_stack import UndoStack
from misaka.ui.tui.utils import (
    getGraphemeSegmenter,
    isWhitespaceChar,
    sliceByColumn,
    truncateToWidth,
    visibleWidth,
)
from misaka.ui.tui.word_navigation import findWordBackward, findWordForward
from misaka.utils.js import jsIndex

segmenter = getGraphemeSegmenter()


@dataclass(slots=True)
class InputState:
    value: str
    cursor: int


@dataclass(slots=True)
class InputOptions:
    prompt: str | None = None
    placeholder: str | None = None
    placeholderStyle: Callable[[str], str] | None = None


class Input(Component):
    """Input component - single-line text input with horizontal scrolling"""

    def __init__(self, options: InputOptions | None = None) -> None:
        if options is None:
            options = InputOptions()
        self.value: str = ""
        self.cursor: int = 0  # Cursor position in the value
        # MISAKA fork (D1): upstream's `renderedStartColumn` follows here.
        self.onSubmit: Callable[[str], None] | None = None
        self.onEscape: Callable[[], None] | None = None

        self.focused: bool = False
        """Focusable interface - set by TUI when focus changes"""

        # Bracketed paste mode buffering
        self.pasteBuffer: str = ""
        self.isInPaste: bool = False

        # Kill ring for Emacs-style kill/yank operations
        self.killRing = KillRing()
        self.lastAction: Literal["kill", "yank", "type-word"] | None = None

        # Undo support
        self.undoStack: UndoStack[InputState] = UndoStack()

        self.prompt = options.prompt if options.prompt is not None else "> "
        self.placeholder = options.placeholder if options.placeholder is not None else ""
        self.placeholderStyle: Callable[[str], str] = (
            options.placeholderStyle if options.placeholderStyle is not None else (lambda text: text)
        )

    def getValue(self) -> str:
        return self.value

    def setValue(self, value: str) -> None:
        self.value = value
        self.cursor = min(self.cursor, len(value))

    def handleInput(self, data: str) -> None:
        # Handle bracketed paste mode
        # Start of paste: \x1b[200~
        # End of paste: \x1b[201~

        # Check if we're starting a bracketed paste
        if "\x1b[200~" in data:
            self.isInPaste = True
            self.pasteBuffer = ""
            data = data.replace("\x1b[200~", "", 1)

        # If we're in a paste, buffer the data
        if self.isInPaste:
            # Check if this chunk contains the end marker
            self.pasteBuffer += data

            endIndex = self.pasteBuffer.find("\x1b[201~")
            if endIndex != -1:
                # Extract the pasted content
                pasteContent = self.pasteBuffer[:endIndex]

                # Process the complete paste
                self.handlePaste(pasteContent)

                # Reset paste state
                self.isInPaste = False

                # Handle any remaining input after the paste marker
                remaining = self.pasteBuffer[endIndex + 6 :]  # 6 = length of \x1b[201~
                self.pasteBuffer = ""
                if remaining:
                    self.handleInput(remaining)
            return

        kb = getKeybindings()

        # Escape/Cancel
        if kb.matches(data, "tui.select.cancel"):
            if self.onEscape:
                self.onEscape()
            return

        # Undo
        if kb.matches(data, "tui.editor.undo"):
            self.undo()
            return

        # Submit
        if kb.matches(data, "tui.input.submit") or data == "\n":
            if self.onSubmit:
                self.onSubmit(self.value)
            return

        # Deletion
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

        # Kill ring actions
        if kb.matches(data, "tui.editor.yank"):
            self.yank()
            return
        if kb.matches(data, "tui.editor.yankPop"):
            self.yankPop()
            return

        # Cursor movement
        if kb.matches(data, "tui.editor.cursorLeft"):
            self.lastAction = None
            if self.cursor > 0:
                beforeCursor = self.value[: self.cursor]
                graphemes = [*segmenter.segment(beforeCursor)]
                lastGrapheme = jsIndex(graphemes, len(graphemes) - 1)
                self.cursor -= len(lastGrapheme.segment) if lastGrapheme else 1
            return

        if kb.matches(data, "tui.editor.cursorRight"):
            self.lastAction = None
            if self.cursor < len(self.value):
                afterCursor = self.value[self.cursor :]
                graphemes = [*segmenter.segment(afterCursor)]
                firstGrapheme = jsIndex(graphemes, 0)
                self.cursor += len(firstGrapheme.segment) if firstGrapheme else 1
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

        # Kitty CSI-u printable character (e.g. \x1b[97u for 'a').
        # Terminals with Kitty protocol flag 1 (disambiguate) send CSI-u for all keys,
        # including plain printable characters. Decode before the control-char check
        # since CSI-u sequences contain \x1b which would be rejected.
        kittyPrintable = decodeKittyPrintable(data)
        if kittyPrintable is not None:
            self.insertCharacter(kittyPrintable)
            return

        # Regular character input - accept printable characters including Unicode,
        # but reject control characters (C0: 0x00-0x1F, DEL: 0x7F, C1: 0x80-0x9F)
        hasControlChars = any(ord(ch) < 32 or ord(ch) == 0x7F or (ord(ch) >= 0x80 and ord(ch) <= 0x9F) for ch in data)
        if not hasControlChars:
            self.insertCharacter(data)

    # MISAKA fork (D1): upstream's `handleMouse` follows here.

    def insertCharacter(self, char: str) -> None:
        # Undo coalescing: consecutive word chars coalesce into one undo unit
        if isWhitespaceChar(char) or self.lastAction != "type-word":
            self.pushUndo()
        self.lastAction = "type-word"

        self.value = self.value[: self.cursor] + char + self.value[self.cursor :]
        self.cursor += len(char)

    def handleBackspace(self) -> None:
        self.lastAction = None
        if self.cursor > 0:
            self.pushUndo()
            beforeCursor = self.value[: self.cursor]
            graphemes = [*segmenter.segment(beforeCursor)]
            lastGrapheme = jsIndex(graphemes, len(graphemes) - 1)
            graphemeLength = len(lastGrapheme.segment) if lastGrapheme else 1
            self.value = self.value[: self.cursor - graphemeLength] + self.value[self.cursor :]
            self.cursor -= graphemeLength

    def handleForwardDelete(self) -> None:
        self.lastAction = None
        if self.cursor < len(self.value):
            self.pushUndo()
            afterCursor = self.value[self.cursor :]
            graphemes = [*segmenter.segment(afterCursor)]
            firstGrapheme = jsIndex(graphemes, 0)
            graphemeLength = len(firstGrapheme.segment) if firstGrapheme else 1
            self.value = self.value[: self.cursor] + self.value[self.cursor + graphemeLength :]

    def deleteToLineStart(self) -> None:
        if self.cursor == 0:
            return
        self.pushUndo()
        deletedText = self.value[: self.cursor]
        self.killRing.push(deletedText, {"prepend": True, "accumulate": self.lastAction == "kill"})
        self.lastAction = "kill"
        self.value = self.value[self.cursor :]
        self.cursor = 0

    def deleteToLineEnd(self) -> None:
        if self.cursor >= len(self.value):
            return
        self.pushUndo()
        deletedText = self.value[self.cursor :]
        self.killRing.push(deletedText, {"prepend": False, "accumulate": self.lastAction == "kill"})
        self.lastAction = "kill"
        self.value = self.value[: self.cursor]

    def deleteWordBackwards(self) -> None:
        if self.cursor == 0:
            return

        # Save lastAction before cursor movement (moveWordBackwards resets it)
        wasKill = self.lastAction == "kill"

        self.pushUndo()

        oldCursor = self.cursor
        self.moveWordBackwards()
        deleteFrom = self.cursor
        self.cursor = oldCursor

        deletedText = self.value[deleteFrom : self.cursor]
        self.killRing.push(deletedText, {"prepend": True, "accumulate": wasKill})
        self.lastAction = "kill"

        self.value = self.value[:deleteFrom] + self.value[self.cursor :]
        self.cursor = deleteFrom

    def deleteWordForward(self) -> None:
        if self.cursor >= len(self.value):
            return

        # Save lastAction before cursor movement (moveWordForwards resets it)
        wasKill = self.lastAction == "kill"

        self.pushUndo()

        oldCursor = self.cursor
        self.moveWordForwards()
        deleteTo = self.cursor
        self.cursor = oldCursor

        deletedText = self.value[self.cursor : deleteTo]
        self.killRing.push(deletedText, {"prepend": False, "accumulate": wasKill})
        self.lastAction = "kill"

        self.value = self.value[: self.cursor] + self.value[deleteTo:]

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

        # Delete the previously yanked text (still at end of ring before rotation)
        prevText = self.killRing.peek() or ""
        self.value = self.value[: self.cursor - len(prevText)] + self.value[self.cursor :]
        self.cursor -= len(prevText)

        # Rotate and insert new entry
        self.killRing.rotate()
        text = self.killRing.peek() or ""
        self.value = self.value[: self.cursor] + text + self.value[self.cursor :]
        self.cursor += len(text)
        self.lastAction = "yank"

    def pushUndo(self) -> None:
        self.undoStack.push(InputState(value=self.value, cursor=self.cursor))

    def undo(self) -> None:
        snapshot = self.undoStack.pop()
        if not snapshot:
            return
        self.value = snapshot.value
        self.cursor = snapshot.cursor
        self.lastAction = None

    def moveWordBackwards(self) -> None:
        if self.cursor == 0:
            return
        self.lastAction = None
        self.cursor = findWordBackward(self.value, self.cursor)

    def moveWordForwards(self) -> None:
        if self.cursor >= len(self.value):
            return
        self.lastAction = None
        self.cursor = findWordForward(self.value, self.cursor)

    def handlePaste(self, pastedText: str) -> None:
        self.lastAction = None
        self.pushUndo()

        # Clean the pasted text - remove newlines and carriage returns
        cleanText = pastedText.replace("\r\n", "").replace("\r", "").replace("\n", "").replace("\t", "    ")
        # MISAKA fork: control characters go too, as `handleInput` refuses them from the keyboard; upstream inserts
        # them, and a pasted "\x1b[2J" then runs as a control sequence when `render` writes the value out.
        cleanText = "".join(
            char for char in cleanText if not (ord(char) < 32 or ord(char) == 0x7F or 0x80 <= ord(char) <= 0x9F)
        )

        # Insert at cursor position
        self.value = self.value[: self.cursor] + cleanText + self.value[self.cursor :]
        self.cursor += len(cleanText)

    def invalidate(self) -> None:
        # No cached state to invalidate currently
        pass

    def render(self, width: int) -> list[str]:
        # Calculate visible window
        availableWidth = width - visibleWidth(self.prompt)

        if availableWidth <= 0:
            return [truncateToWidth(self.prompt, width, "")]

        if len(self.value) == 0 and self.placeholder:
            placeholder = truncateToWidth(self.placeholder, availableWidth, "")
            graphemes = [*segmenter.segment(placeholder)]
            atCursor = graphemes[0].segment if graphemes else " "
            afterCursor = placeholder[len(atCursor) :]
            marker = CURSOR_MARKER if self.focused else ""
            cursorChar = renderFakeCursor(self.placeholderStyle(atCursor))
            textWithCursor = marker + cursorChar + self.placeholderStyle(afterCursor)
            padding = " " * max(0, availableWidth - visibleWidth(textWithCursor))
            return [self.prompt + textWithCursor + padding]

        visibleText = ""
        cursorDisplay = self.cursor
        totalWidth = visibleWidth(self.value)

        if totalWidth < availableWidth:
            # Everything fits (leave room for cursor at end)
            visibleText = self.value
        else:
            # Need horizontal scrolling
            # Reserve one column for cursor if it's at the end
            scrollWidth = availableWidth - 1 if self.cursor == len(self.value) else availableWidth
            cursorCol = visibleWidth(self.value[: self.cursor])

            if scrollWidth > 0:
                halfWidth = scrollWidth // 2
                startCol = 0

                if cursorCol < halfWidth:
                    # Cursor near start
                    startCol = 0
                elif cursorCol > totalWidth - halfWidth:
                    # Cursor near end
                    startCol = max(0, totalWidth - scrollWidth)
                else:
                    # Cursor in middle
                    startCol = max(0, cursorCol - halfWidth)

                visibleText = sliceByColumn(self.value, startCol, scrollWidth, True)
                beforeCursor = sliceByColumn(self.value, startCol, max(0, cursorCol - startCol), True)
                cursorDisplay = len(beforeCursor)
            else:
                visibleText = ""
                cursorDisplay = 0

        # Build line with fake cursor
        # Insert cursor character at cursor position
        graphemes = [*segmenter.segment(visibleText[cursorDisplay:])]
        cursorGrapheme = jsIndex(graphemes, 0)

        beforeCursor = visibleText[:cursorDisplay]
        atCursor = cursorGrapheme.segment if cursorGrapheme is not None else " "  # Character at cursor, or space if at end
        afterCursor = visibleText[cursorDisplay + len(atCursor) :]

        # Hardware cursor marker (zero-width, emitted before fake cursor for IME positioning)
        marker = CURSOR_MARKER if self.focused else ""

        # TUI draws the fake cursor in reverse video, or omits it when the hardware cursor is shown
        cursorChar = renderFakeCursor(atCursor)
        textWithCursor = beforeCursor + marker + cursorChar + afterCursor

        # Calculate visual width
        visualLength = visibleWidth(textWithCursor)
        padding = " " * max(0, availableWidth - visualLength)
        line = self.prompt + textWithCursor + padding

        return [line]


__all__ = ["Input", "InputOptions"]

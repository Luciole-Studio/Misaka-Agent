"""Translated from pi-tui's ``components/editor.ts`` (v1.3.0).

MISAKA forks, each marked where it happens:

- No alt screen (D1): ``handleMouse``, the render geometry only it reads (``renderedVisibleLineCount``,
  ``renderedAutocompleteHeight``) and the autocomplete list's ``onSelect`` (reached only by a mouse click) are not
  ported; only the alt screen produces mouse events.
- Prompt history persists in a file when ``MISAKA_INPUT_HISTORY`` names one.
- String offsets count code points (Python strings), where upstream counts UTF-16 code units.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict, cast

from misaka.ui.tui.autocomplete import (
    AutocompleteItem,
    AutocompleteProvider,
    AutocompleteSuggestions,
)
from misaka.ui.tui.keybindings import KeybindingsManager, getKeybindings
from misaka.ui.tui.keys import decodePrintableKey, matchesKey
from misaka.ui.tui.kill_ring import KillRing
from misaka.ui.tui.tui import CURSOR_MARKER, TUI, Component, renderFakeCursor
from misaka.ui.tui.undo_stack import UndoStack
from misaka.ui.tui.utils import (
    SegmentData,
    Segmenter,
    autocompleteBoundaryRegex,
    autocompleteSeparatorRegex,
    cjkBreakRegex,
    getGraphemeSegmenter,
    getWordSegmenter,
    isWhitespaceChar,
    sliceByColumn,
    visibleWidth,
)
from misaka.ui.tui.word_navigation import (
    WordNavigationOptions,
    findWordBackward,
    findWordForward,
)
from misaka.utils import js_regexp
from misaka.utils.js import (
    AbortController,
    eager,
    jsIndex,
    jsTrim,
    jsTrimStart,
    startTask,
)
from misaka.utils.js_regexp import RegExp

from .select_list import (
    SelectItem,
    SelectList,
    SelectListLayoutOptions,
    SelectListTheme,
)

graphemeSegmenter = getGraphemeSegmenter()
wordSegmenter = getWordSegmenter()

PASTE_MARKER_REGEX = RegExp(r"\[paste #(\d+)( (\+\d+ lines|\d+ chars))?\]", "g")
"""Regex matching paste markers like `[paste #1 +123 lines]` or `[paste #2 1234 chars]`."""

PASTE_MARKER_SINGLE = RegExp(r"^\[paste #(\d+)( (\+\d+ lines|\d+ chars))?\]$")
"""Non-global version for single-segment testing."""


def isPasteMarker(segment: str) -> bool:
    """Check if a segment is a paste marker (i.e. was merged by segmentWithMarkers)."""
    return len(segment) >= 10 and PASTE_MARKER_SINGLE.test(segment)


def segmentWithMarkers(text: str, baseSegmenter: Segmenter, validIds: set[int]) -> Iterable[SegmentData]:
    """A segmenter that wraps Intl.Segmenter and merges graphemes that fall
    within paste markers into single atomic segments.  This makes cursor
    movement, deletion, word-wrap, etc. treat paste markers as single units.

    Only markers whose numeric ID exists in `validIds` are merged.
    """
    # Fast path: no paste markers in the text or no valid IDs.
    if len(validIds) == 0 or "[paste #" not in text:
        return baseSegmenter.segment(text)

    # Find all marker spans with valid IDs.
    markers: list[tuple[int, int]] = []
    for m in js_regexp.matchAll(text, PASTE_MARKER_REGEX):
        id = int(m[1], 10)
        if id not in validIds:
            continue
        markers.append((m.index, m.index + len(m[0])))
    if len(markers) == 0:
        return baseSegmenter.segment(text)

    # Build merged segment list.
    baseSegments = baseSegmenter.segment(text)
    result: list[SegmentData] = []
    markerIdx = 0

    for seg in baseSegments:
        # Skip past markers that are entirely before this segment.
        while markerIdx < len(markers) and markers[markerIdx][1] <= seg.index:
            markerIdx += 1

        marker = markers[markerIdx] if markerIdx < len(markers) else None

        if marker and seg.index >= marker[0] and seg.index < marker[1]:
            # This segment falls inside a marker.
            # If this is the first segment of the marker, emit a merged segment.
            if seg.index == marker[0]:
                markerText = text[marker[0] : marker[1]]
                result.append(SegmentData(segment=markerText, index=marker[0], input=text))
            # Otherwise skip (already merged into the first segment).
        else:
            result.append(seg)

    return result


@dataclass(slots=True)
class TextChunk:
    """Represents a chunk of text for word-wrap layout.
    Tracks both the text content and its position in the original line.
    """

    text: str
    startIndex: int
    endIndex: int


def wordWrapLine(line: str, maxWidth: int, preSegmented: Sequence[SegmentData] | None = None) -> list[TextChunk]:
    """Split a line into word-wrapped chunks.
    Wraps at word boundaries when possible, falling back to character-level
    wrapping for words longer than the available width.

    @param line - The text line to wrap
    @param maxWidth - Maximum visible width per chunk
    @param preSegmented - Optional pre-segmented graphemes (e.g. with paste-marker awareness).
                          When omitted the default Intl.Segmenter is used.
    @returns Array of chunks with text and position information
    """
    if not line or maxWidth <= 0:
        return [TextChunk(text="", startIndex=0, endIndex=0)]

    lineWidth = visibleWidth(line)
    if lineWidth <= maxWidth:
        return [TextChunk(text=line, startIndex=0, endIndex=len(line))]

    chunks: list[TextChunk] = []
    segments = preSegmented if preSegmented is not None else [*graphemeSegmenter.segment(line)]

    # MISAKA fork: a line that is one grapheme wider than `maxWidth` (a wide character at width 1) cannot be split;
    # upstream recurses on that same grapheme below until the stack overflows. Overflow the column by a cell instead.
    # The base segmenter decides, so a line holding exactly one paste marker (one segment under the marker-aware
    # segmenter, many graphemes under the base one) still re-wraps at grapheme granularity.
    if len(segments) <= 1 and len(graphemeSegmenter.segment(line)) <= 1:
        return [TextChunk(text=line, startIndex=0, endIndex=len(line))]

    currentWidth = 0
    chunkStart = 0

    # Wrap opportunity: the position after the last whitespace before a non-whitespace
    # grapheme, i.e. where a line break is allowed.
    wrapOppIndex = -1
    wrapOppWidth = 0

    for i in range(len(segments)):
        seg = segments[i]
        grapheme = seg.segment
        gWidth = visibleWidth(grapheme)
        charIndex = seg.index
        isWs = not isPasteMarker(grapheme) and isWhitespaceChar(grapheme)

        # Overflow check before advancing.
        if currentWidth + gWidth > maxWidth:
            if wrapOppIndex >= 0 and currentWidth - wrapOppWidth + gWidth <= maxWidth:
                # Backtrack to last wrap opportunity (the remaining content
                # plus the current grapheme still fits within maxWidth).
                chunks.append(TextChunk(text=line[chunkStart:wrapOppIndex], startIndex=chunkStart, endIndex=wrapOppIndex))
                chunkStart = wrapOppIndex
                currentWidth -= wrapOppWidth
            elif chunkStart < charIndex:
                # No viable wrap opportunity: force-break at current position.
                # This also handles the case where backtracking to a word
                # boundary wouldn't help because the remaining content plus
                # the current grapheme (e.g. a wide character) still exceeds
                # maxWidth.
                chunks.append(TextChunk(text=line[chunkStart:charIndex], startIndex=chunkStart, endIndex=charIndex))
                chunkStart = charIndex
                currentWidth = 0
            wrapOppIndex = -1

        if gWidth > maxWidth:
            # Single atomic segment wider than maxWidth (e.g. paste marker
            # in a narrow terminal). Re-wrap it at grapheme granularity.

            # The segment remains logically atomic for cursor
            # movement / editing — the split is purely visual for word-wrap layout.
            subChunks = wordWrapLine(grapheme, maxWidth)
            for j in range(len(subChunks) - 1):
                sc = subChunks[j]
                chunks.append(
                    TextChunk(text=sc.text, startIndex=charIndex + sc.startIndex, endIndex=charIndex + sc.endIndex)
                )
            last = subChunks[len(subChunks) - 1]
            chunkStart = charIndex + last.startIndex
            currentWidth = visibleWidth(last.text)
            wrapOppIndex = -1
            continue

        # Advance.
        currentWidth += gWidth

        # Record wrap opportunity: whitespace followed by non-whitespace
        # (multiple spaces join; the break point is after the last space),
        # or at a boundary where either side is CJK (CJK allows breaking
        # between any adjacent characters).
        next = jsIndex(segments, i + 1)
        if isWs and next and (isPasteMarker(next.segment) or not isWhitespaceChar(next.segment)):
            wrapOppIndex = next.index
            wrapOppWidth = currentWidth
        elif not isWs and next and not isWhitespaceChar(next.segment):
            isCjk = not isPasteMarker(grapheme) and cjkBreakRegex.test(grapheme)
            nextIsCjk = not isPasteMarker(next.segment) and cjkBreakRegex.test(next.segment)
            if isCjk or nextIsCjk:
                wrapOppIndex = next.index
                wrapOppWidth = currentWidth

    # Push final chunk.
    chunks.append(TextChunk(text=line[chunkStart:], startIndex=chunkStart, endIndex=len(line)))

    return chunks


# Kitty CSI-u sequences for printable keys, including optional shifted/base codepoints.
@dataclass(slots=True)
class EditorState:
    lines: list[str] = field(default_factory=lambda: [""])
    cursorLine: int = 0
    cursorCol: int = 0


@dataclass(slots=True)
class EditorSnapshot:
    """Undo snapshot: editor text state plus the paste registry."""

    state: EditorState
    pastes: dict[int, str]
    pasteCounter: int


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
    paddingX: float | None = None
    autocompleteMaxVisible: float | None = None


class _VisualLine(TypedDict):
    """MISAKA fork: a name for upstream's inline ``{ logicalLine; startCol; length }`` type."""

    logicalLine: int
    startCol: int
    length: int


class _AutocompleteRequestOptions(TypedDict):
    """MISAKA fork: a name for upstream's inline ``{ force; explicitTab }`` type."""

    force: bool
    explicitTab: bool


SLASH_COMMAND_SELECT_LIST_LAYOUT = SelectListLayoutOptions(
    minPrimaryColumnWidth=12,
    maxPrimaryColumnWidth=32,
)

ATTACHMENT_AUTOCOMPLETE_DEBOUNCE_MS = 20
DEFAULT_AUTOCOMPLETE_TRIGGER_CHARACTERS = ["@", "#"]
# Unquoted completions end at whitespace or CJK punctuation; quoted paths may contain either.
unquotedAutocompleteSuffixRegex = RegExp(f"(?:(?!{autocompleteSeparatorRegex.source}).)*", "u")
# Trigger tokens may be wrapped in prose, e.g. "(@src/foo" or "`@src/foo".
autocompleteTokenStartSource = f"{autocompleteBoundaryRegex.source}[([{{<`]*"


def escapeCharacterClass(value: str) -> str:
    return js_regexp.replace(value, RegExp(r"[\\^$.*+?()[\]{}|-]", "g"), "\\$&")


def buildTriggerPattern(triggerCharacters: list[str]) -> RegExp:
    return RegExp(
        f'{autocompleteTokenStartSource}(?:@"[^"]*|[{"".join(map(escapeCharacterClass, triggerCharacters))}]'
        f"{unquotedAutocompleteSuffixRegex.source})$",
        "u",
    )


def buildDebouncePattern(triggerCharacters: list[str]) -> RegExp:
    escapedWithoutAt = [escapeCharacterClass(character) for character in triggerCharacters if character != "@"]
    return RegExp(
        f'{autocompleteTokenStartSource}(?:@(?:"[^"]*|{unquotedAutocompleteSuffixRegex.source})'
        f"|[{''.join(escapedWithoutAt)}]{unquotedAutocompleteSuffixRegex.source})$",
        "u",
    )


def createScrollBorder(direction: Literal["↑", "↓"], hiddenLineCount: int, width: int) -> str:
    availableWidth = max(0, width)
    label = f" {direction} {hiddenLineCount} more "
    labelWidth = visibleWidth(label)
    if labelWidth + 2 <= availableWidth:
        leftWidth = (availableWidth - labelWidth) // 2
        return "─" * leftWidth + label + "─" * (availableWidth - leftWidth - labelWidth)

    indicator = f"─── {direction} {hiddenLineCount} more "
    remaining = availableWidth - visibleWidth(indicator)
    if remaining >= 0:
        return indicator + "─" * remaining

    ellipsis = "..."[:availableWidth]
    indicatorWidth = availableWidth - visibleWidth(ellipsis)
    return sliceByColumn(indicator, 0, indicatorWidth, True) + ellipsis


def _finitePadding(value: float) -> int:
    """MISAKA fork: ``Number.isFinite(value) ? Math.max(0, Math.floor(value)) : 0``."""
    return max(0, math.floor(value)) if math.isfinite(value) else 0


def _finiteMaxVisible(value: float) -> int:
    """MISAKA fork: ``Number.isFinite(value) ? Math.max(3, Math.min(20, Math.floor(value))) : 5``."""
    return max(3, min(20, math.floor(value))) if math.isfinite(value) else 5


class Editor(Component):
    def __init__(self, tui: TUI, theme: EditorTheme, options: EditorOptions | None = None) -> None:
        if options is None:
            options = EditorOptions()
        self.state = EditorState(lines=[""], cursorLine=0, cursorCol=0)

        self.focused: bool = False
        """Focusable interface - set by TUI when focus changes"""

        self.paddingX: int = 0

        # Store last render geometry for cursor navigation and mouse hit-testing.
        self.lastWidth: int = 80
        # MISAKA fork (D1): upstream's `renderedVisibleLineCount` and `renderedAutocompleteHeight` follow here.

        # Vertical scrolling support
        self.scrollOffset: int = 0

        # Autocomplete support
        self.autocompleteProvider: AutocompleteProvider | None = None
        self.autocompleteTriggerCharacters = [*DEFAULT_AUTOCOMPLETE_TRIGGER_CHARACTERS]
        self.autocompleteTriggerPattern = buildTriggerPattern(self.autocompleteTriggerCharacters)
        self.autocompleteDebouncePattern = buildDebouncePattern(self.autocompleteTriggerCharacters)
        self.autocompleteList: SelectList | None = None
        self.autocompleteState: Literal["regular", "force"] | None = None
        self.autocompletePrefix: str = ""
        self.autocompleteMaxVisible: int = 5
        self.autocompleteAbort: AbortController | None = None
        self.autocompleteDebounceTimer: asyncio.TimerHandle | None = None
        # MISAKA fork: upstream starts from `Promise.resolve()`; a future needs a running loop, so `None` stands in.
        self.autocompleteRequestTask: asyncio.Future[None] | None = None
        self.autocompleteStartToken: int = 0
        self.autocompleteRequestId: int = 0

        # Paste tracking for large pastes
        self.pastes: dict[int, str] = {}
        self.pasteCounter: int = 0

        # Bracketed paste mode buffering
        self.pasteBuffer: str = ""
        self.isInPaste: bool = False

        # Prompt history for up/down navigation
        self.history: list[str] = []
        self.historyIndex: int = -1  # -1 = not browsing, 0 = most recent, 1 = older, etc.
        self.historyDraft: EditorState | None = None
        # MISAKA fork: prompt history persists across sessions when MISAKA_INPUT_HISTORY names a file.
        self.historyFile = os.environ.get("MISAKA_INPUT_HISTORY") or None
        if self.historyFile:
            self.history = self._readHistoryFile() or []

        # Kill ring for Emacs-style kill/yank operations
        self.killRing = KillRing()
        self.lastAction: Literal["kill", "yank", "type-word"] | None = None

        # Character jump mode
        self.jumpMode: Literal["forward", "backward"] | None = None

        # Preferred visual column for vertical cursor movement (sticky column)
        self.preferredVisualCol: int | None = None

        # When the cursor is snapped to the start of an atomic segment, e.g. a
        # paste marker, cursorCol no longer reflects where the cursor would have
        # landed. This field stores the pre-snap cursorCol so that the next
        # vertical move can resolve it to a visual column on whatever VL it belongs
        # to.
        self.snappedFromCursorCol: int | None = None

        # Undo support
        self.undoStack: UndoStack[EditorSnapshot] = UndoStack()

        self.onSubmit: Callable[[str], None] | None = None
        self.onChange: Callable[[str], None] | None = None
        self.disableSubmit: bool = False

        self.tui = tui
        self.theme = theme
        # Border color (can be changed dynamically)
        self.borderColor: Callable[[str], str] = theme.borderColor
        paddingX = options.paddingX if options.paddingX is not None else 0
        self.paddingX = _finitePadding(paddingX)
        maxVisible = options.autocompleteMaxVisible if options.autocompleteMaxVisible is not None else 5
        self.autocompleteMaxVisible = _finiteMaxVisible(maxVisible)

    def validPasteIds(self) -> set[int]:
        """Set of currently valid paste IDs, for marker-aware segmentation."""
        return set(self.pastes.keys())

    def segment(self, text: str, mode: Literal["word", "grapheme"]) -> Iterable[SegmentData]:
        """Segment text with paste-marker awareness, only merging markers with valid IDs."""
        return segmentWithMarkers(text, wordSegmenter if mode == "word" else graphemeSegmenter, self.validPasteIds())

    def getPaddingX(self) -> int:
        return self.paddingX

    def setPaddingX(self, padding: float) -> None:
        newPadding = _finitePadding(padding)
        if self.paddingX != newPadding:
            self.paddingX = newPadding
            self.tui.requestRender()

    def getAutocompleteMaxVisible(self) -> int:
        return self.autocompleteMaxVisible

    def setAutocompleteMaxVisible(self, maxVisible: float) -> None:
        newMaxVisible = _finiteMaxVisible(maxVisible)
        if self.autocompleteMaxVisible != newMaxVisible:
            self.autocompleteMaxVisible = newMaxVisible
            self.tui.requestRender()

    def setAutocompleteProvider(self, provider: AutocompleteProvider) -> None:
        self.cancelAutocomplete()
        self.autocompleteProvider = provider
        triggerCharacters = getattr(provider, "triggerCharacters", None)
        self.setAutocompleteTriggerCharacters(triggerCharacters if triggerCharacters is not None else [])

    def _readHistoryFile(self) -> list[str] | None:
        """MISAKA fork: the persisted history, newest first; None when there is none to read."""
        assert self.historyFile is not None
        try:
            with open(self.historyFile, encoding="utf-8") as f:
                loaded = json.load(f)
        except (OSError, ValueError, TypeError):
            return None
        return [x for x in loaded if isinstance(x, str)][:100] if isinstance(loaded, list) else None

    def addToHistory(self, text: str) -> None:
        """Add a prompt to history for up/down arrow navigation.
        Called after successful submission.
        """
        trimmed = jsTrim(text)
        if not trimmed:
            return
        # MISAKA fork: several windows share one role's history file (a root Last Order and her fork nodes): start
        # from what is on disk, or this window's copy from its own start overwrites what the others have added since.
        if self.historyFile and self.historyIndex == -1 and (latest := self._readHistoryFile()) is not None:
            self.history = latest
        # Don't add consecutive duplicates
        if len(self.history) > 0 and self.history[0] == trimmed:
            return
        self.history.insert(0, trimmed)
        # Limit history size
        if len(self.history) > 100:
            self.history.pop()
        # MISAKA fork: written atomically; a failed history save must not block the submit.
        if self.historyFile:
            try:
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
        visualLines = self.buildVisualLineMap(self.lastWidth)
        currentVisualLine = self.findCurrentVisualLine(visualLines)
        return currentVisualLine == 0

    def isOnLastVisualLine(self) -> bool:
        visualLines = self.buildVisualLineMap(self.lastWidth)
        currentVisualLine = self.findCurrentVisualLine(visualLines)
        return currentVisualLine == len(visualLines) - 1

    def navigateHistory(self, direction: Literal[1, -1]) -> None:
        self.lastAction = None
        if len(self.history) == 0:
            return

        newIndex = self.historyIndex - direction  # Up(-1) increases index, Down(1) decreases
        if newIndex < -1 or newIndex >= len(self.history):
            return

        # Capture state when first entering history browsing mode
        if self.historyIndex == -1 and newIndex >= 0:
            self.pushUndoSnapshot()
            self.historyDraft = copy.deepcopy(self.state)

        self.historyIndex = newIndex

        if self.historyIndex == -1:
            draft = self.historyDraft
            self.historyDraft = None
            if draft:
                self.state = draft
                self.preferredVisualCol = None
                self.snappedFromCursorCol = None
                self.scrollOffset = 0
                if self.onChange:
                    self.onChange(self.getText())
            else:
                self.setTextInternal("")
        else:
            self.setTextInternal(self.history[self.historyIndex] or "", "start" if direction == -1 else "end")

    def exitHistoryBrowsing(self) -> None:
        self.historyIndex = -1
        self.historyDraft = None

    def setTextInternal(self, text: str, cursorPlacement: Literal["start", "end"] = "end") -> None:
        """Internal setText that doesn't reset history state - used by navigateHistory"""
        lines = text.split("\n")
        self.state.lines = [""] if len(lines) == 0 else lines
        self.state.cursorLine = 0 if cursorPlacement == "start" else len(self.state.lines) - 1
        cursorLineText = jsIndex(self.state.lines, self.state.cursorLine)
        self.setCursorCol(0 if cursorPlacement == "start" else len(cursorLineText) if cursorLineText else 0)
        # Reset scroll - render() will adjust to show cursor
        self.scrollOffset = 0

        if self.onChange:
            self.onChange(self.getText())

    def invalidate(self) -> None:
        # No cached state to invalidate currently
        pass

    def renderTopBorder(self, width: int, hiddenLineCount: int) -> str:
        border = createScrollBorder("↑", hiddenLineCount, width) if hiddenLineCount > 0 else "─" * width
        return self.borderColor(border)

    def renderBottomBorder(self, width: int, hiddenLineCount: int) -> str:
        border = createScrollBorder("↓", hiddenLineCount, width) if hiddenLineCount > 0 else "─" * width
        return self.borderColor(border)

    def render(self, width: int) -> list[str]:
        maxPadding = max(0, (width - 1) // 2)
        paddingX = min(self.paddingX, maxPadding)
        contentWidth = max(1, width - paddingX * 2)

        # Layout width: with padding the cursor can overflow into it,
        # without padding we reserve 1 column for the cursor.
        layoutWidth = max(1, contentWidth - (0 if paddingX else 1))

        # Store for cursor navigation (must match wrapping width)
        self.lastWidth = layoutWidth

        # Layout the text
        layoutLines = self.layoutText(layoutWidth)

        # Calculate max visible lines: 30% of terminal height, minimum 5 lines
        terminalRows = self.tui.terminal.rows
        maxVisibleLines = max(5, math.floor(terminalRows * 0.3))

        # Find the cursor line index in layoutLines
        cursorLineIndex = next((index for index, line in enumerate(layoutLines) if line.hasCursor), -1)
        if cursorLineIndex == -1:
            cursorLineIndex = 0

        # Adjust scroll offset to keep cursor visible
        if cursorLineIndex < self.scrollOffset:
            self.scrollOffset = cursorLineIndex
        elif cursorLineIndex >= self.scrollOffset + maxVisibleLines:
            self.scrollOffset = cursorLineIndex - maxVisibleLines + 1

        # Clamp scroll offset to valid range
        maxScrollOffset = max(0, len(layoutLines) - maxVisibleLines)
        self.scrollOffset = max(0, min(self.scrollOffset, maxScrollOffset))

        # Get visible lines slice
        visibleLines = layoutLines[self.scrollOffset : self.scrollOffset + maxVisibleLines]

        result: list[str] = []
        leftPadding = " " * paddingX
        rightPadding = leftPadding

        # Render top border (with scroll indicator if scrolled down)
        result.append(self.renderTopBorder(width, self.scrollOffset))

        # Render each visible layout line
        # Emit hardware cursor marker when focused so TUI can position the
        # hardware cursor for IME candidate-window placement even while
        # autocomplete (e.g. slash-command menu) is visible.
        emitCursorMarker = self.focused

        for layoutLine in visibleLines:
            displayText = layoutLine.text
            lineVisibleWidth = visibleWidth(layoutLine.text)
            cursorInPadding = False

            # Add cursor if this line has it
            if layoutLine.hasCursor and layoutLine.cursorPos is not None:
                before = displayText[: layoutLine.cursorPos]
                after = displayText[layoutLine.cursorPos :]

                # Hardware cursor marker (zero-width, emitted before fake cursor for IME positioning)
                marker = CURSOR_MARKER if emitCursorMarker else ""

                if len(after) > 0:
                    # Cursor is on a character (grapheme) - replace it with highlighted version
                    # Get the first grapheme from 'after'
                    afterGraphemes = [*self.segment(after, "grapheme")]
                    firstGrapheme = afterGraphemes[0].segment if afterGraphemes else ""
                    restAfter = after[len(firstGrapheme) :]
                    cursor = renderFakeCursor(firstGrapheme)
                    displayText = before + marker + cursor + restAfter
                    # lineVisibleWidth stays the same - we're replacing, not adding
                else:
                    # Cursor is at the end - add highlighted space
                    cursor = renderFakeCursor(" ")
                    displayText = before + marker + cursor
                    lineVisibleWidth = lineVisibleWidth + 1
                    # If cursor overflows content width into the padding, flag it
                    if lineVisibleWidth > contentWidth and paddingX > 0:
                        cursorInPadding = True

            # Calculate padding based on actual visible width
            padding = " " * max(0, contentWidth - lineVisibleWidth)
            lineRightPadding = rightPadding[1:] if cursorInPadding else rightPadding

            # Render the line (no side borders, just horizontal lines above and below)
            result.append(f"{leftPadding}{displayText}{padding}{lineRightPadding}")

        # Render bottom border (with scroll indicator if more content below)
        linesBelow = len(layoutLines) - (self.scrollOffset + len(visibleLines))
        result.append(self.renderBottomBorder(width, linesBelow))

        # Add autocomplete list if active
        if self.autocompleteState and self.autocompleteList:
            autocompleteResult = self.autocompleteList.render(contentWidth)
            for line in autocompleteResult:
                lineWidth = visibleWidth(line)
                linePadding = " " * max(0, contentWidth - lineWidth)
                result.append(f"{leftPadding}{line}{linePadding}{rightPadding}")

        return result

    # MISAKA fork (D1): upstream's `handleMouse` follows here.

    def handleInput(self, data: str) -> None:
        kb = getKeybindings()

        # Handle character jump mode (awaiting next character to jump to)
        if self.jumpMode is not None:
            # Cancel if the hotkey is pressed again
            if kb.matches(data, "tui.editor.jumpForward") or kb.matches(data, "tui.editor.jumpBackward"):
                self.jumpMode = None
                return

            printable = decodePrintableKey(data)
            if printable is None:
                printable = data if len(data) > 0 and ord(data[0]) >= 32 else None
            if printable is not None:
                # Printable character - perform the jump
                direction = self.jumpMode
                self.jumpMode = None
                self.jumpToChar(printable, direction)
                return

            # Control character - cancel and fall through to normal handling
            self.jumpMode = None

        # Handle bracketed paste mode
        if "\x1b[200~" in data:
            self.isInPaste = True
            self.pasteBuffer = ""
            data = data.replace("\x1b[200~", "", 1)

        if self.isInPaste:
            self.pasteBuffer += data
            endIndex = self.pasteBuffer.find("\x1b[201~")
            if endIndex != -1:
                pasteContent = self.pasteBuffer[:endIndex]
                if len(pasteContent) > 0:
                    self.handlePaste(pasteContent)
                self.isInPaste = False
                remaining = self.pasteBuffer[endIndex + 6 :]
                self.pasteBuffer = ""
                if len(remaining) > 0:
                    self.handleInput(remaining)
                return
            return

        # Ctrl+C - let parent handle (exit/clear)
        if kb.matches(data, "tui.input.copy"):
            return

        # Undo
        if kb.matches(data, "tui.editor.undo"):
            self.undo()
            return

        # Handle autocomplete mode
        if self.autocompleteState and self.autocompleteList:
            if kb.matches(data, "tui.select.cancel"):
                self.cancelAutocomplete()
                return

            if kb.matches(data, "tui.select.up") or kb.matches(data, "tui.select.down"):
                self.autocompleteList.handleInput(data)
                return

            if kb.matches(data, "tui.input.tab"):
                selected = cast("AutocompleteItem | None", self.autocompleteList.getSelectedItem())
                if selected and self.autocompleteProvider:
                    self.pushUndoSnapshot()
                    self.lastAction = None
                    result = self.autocompleteProvider.applyCompletion(
                        self.state.lines,
                        self.state.cursorLine,
                        self.state.cursorCol,
                        selected,
                        self.autocompletePrefix,
                    )
                    self.state.lines = result["lines"]
                    self.state.cursorLine = result["cursorLine"]
                    self.setCursorCol(result["cursorCol"])
                    self.cancelAutocomplete()
                    if self.onChange:
                        self.onChange(self.getText())
                return

            if kb.matches(data, "tui.select.confirm"):
                selected = cast("AutocompleteItem | None", self.autocompleteList.getSelectedItem())
                if selected and self.autocompleteProvider:
                    self.pushUndoSnapshot()
                    self.lastAction = None
                    result = self.autocompleteProvider.applyCompletion(
                        self.state.lines,
                        self.state.cursorLine,
                        self.state.cursorCol,
                        selected,
                        self.autocompletePrefix,
                    )
                    self.state.lines = result["lines"]
                    self.state.cursorLine = result["cursorLine"]
                    self.setCursorCol(result["cursorCol"])

                    if self.autocompletePrefix.startswith("/"):
                        self.cancelAutocomplete()
                        # Fall through to submit
                    else:
                        self.cancelAutocomplete()
                        if self.onChange:
                            self.onChange(self.getText())
                        return

        # Tab - trigger completion
        if kb.matches(data, "tui.input.tab") and not self.autocompleteState:
            self.handleTabCompletion()
            return

        # Deletion actions
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

        # Kill ring actions
        if kb.matches(data, "tui.editor.yank"):
            self.yank()
            return
        if kb.matches(data, "tui.editor.yankPop"):
            self.yankPop()
            return

        # Dedicated history actions always browse entries instead of moving the cursor.
        if kb.matches(data, "tui.editor.historyPrevious"):
            self.cancelAutocomplete()
            self.navigateHistory(-1)
            return
        if kb.matches(data, "tui.editor.historyNext"):
            self.cancelAutocomplete()
            self.navigateHistory(1)
            return

        # Cursor movement actions
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

        # New line
        if (
            kb.matches(data, "tui.input.newLine")
            or (len(data) > 1 and ord(data[0]) == 10)
            or data == "\x1b\r"
            or data == "\x1b[13;2~"
            or (len(data) > 1 and "\x1b" in data and "\r" in data)
            or (data == "\n" and len(data) == 1)
        ):
            if self.shouldSubmitOnBackslashEnter(data, kb):
                self.handleBackspace()
                self.submitValue()
                return
            self.addNewLine()
            return

        # Submit (Enter)
        if kb.matches(data, "tui.input.submit"):
            if self.disableSubmit:
                return

            # Workaround for terminals without Shift+Enter support:
            # If char before cursor is \, delete it and insert newline instead of submitting.
            currentLine = jsIndex(self.state.lines, self.state.cursorLine) or ""
            if self.state.cursorCol > 0 and jsIndex(currentLine, self.state.cursorCol - 1) == "\\":
                self.handleBackspace()
                self.addNewLine()
                return

            self.submitValue()
            return

        # Arrow key navigation (with history support)
        if kb.matches(data, "tui.editor.cursorUp"):
            if self.isOnFirstVisualLine() and (
                self.isEditorEmpty() or self.historyIndex > -1 or self.state.cursorCol == 0
            ):
                self.navigateHistory(-1)
            elif self.isOnFirstVisualLine():
                # Already at top - jump to start of line
                self.moveToLineStart()
            else:
                self.moveCursor(-1, 0)
            return
        if kb.matches(data, "tui.editor.cursorDown"):
            if self.historyIndex > -1 and self.isOnLastVisualLine():
                self.navigateHistory(1)
            elif self.isOnLastVisualLine():
                # Already at bottom - jump to end of line
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

        # Page up/down - scroll by page and move cursor
        if kb.matches(data, "tui.editor.pageUp"):
            self.pageScroll(-1)
            return
        if kb.matches(data, "tui.editor.pageDown"):
            self.pageScroll(1)
            return

        # Character jump mode triggers
        if kb.matches(data, "tui.editor.jumpForward"):
            self.jumpMode = "forward"
            return
        if kb.matches(data, "tui.editor.jumpBackward"):
            self.jumpMode = "backward"
            return

        # Shift+Space - insert regular space
        if matchesKey(data, "shift+space"):
            self.insertCharacter(" ")
            return

        printable = decodePrintableKey(data)
        if printable is not None:
            self.insertCharacter(printable)
            return

        # Regular characters
        if len(data) > 0 and ord(data[0]) >= 32:
            self.insertCharacter(data)

    def layoutText(self, contentWidth: int) -> list[LayoutLine]:
        layoutLines: list[LayoutLine] = []

        if len(self.state.lines) == 0 or (len(self.state.lines) == 1 and self.state.lines[0] == ""):
            # Empty editor
            layoutLines.append(LayoutLine(text="", hasCursor=True, cursorPos=0))
            return layoutLines

        # Process each logical line
        for i in range(len(self.state.lines)):
            line = self.state.lines[i] or ""
            isCurrentLine = i == self.state.cursorLine
            lineVisibleWidth = visibleWidth(line)

            if lineVisibleWidth <= contentWidth:
                # Line fits in one layout line
                if isCurrentLine:
                    layoutLines.append(LayoutLine(text=line, hasCursor=True, cursorPos=self.state.cursorCol))
                else:
                    layoutLines.append(LayoutLine(text=line, hasCursor=False))
            else:
                # Line needs wrapping - use word-aware wrapping
                chunks = wordWrapLine(line, contentWidth, [*self.segment(line, "grapheme")])

                for chunkIndex in range(len(chunks)):
                    chunk = chunks[chunkIndex]

                    cursorPos = self.state.cursorCol
                    isLastChunk = chunkIndex == len(chunks) - 1

                    # Determine if cursor is in this chunk
                    # For word-wrapped chunks, we need to handle the case where
                    # cursor might be in trimmed whitespace at end of chunk
                    hasCursorInChunk = False
                    adjustedCursorPos = 0

                    if isCurrentLine:
                        if isLastChunk:
                            # Last chunk: cursor belongs here if >= startIndex
                            hasCursorInChunk = cursorPos >= chunk.startIndex
                            adjustedCursorPos = cursorPos - chunk.startIndex
                        else:
                            # Non-last chunk: cursor belongs here if in range [startIndex, endIndex)
                            # But we need to handle the visual position in the trimmed text
                            hasCursorInChunk = cursorPos >= chunk.startIndex and cursorPos < chunk.endIndex
                            if hasCursorInChunk:
                                adjustedCursorPos = cursorPos - chunk.startIndex
                                # Clamp to text length (in case cursor was in trimmed whitespace)
                                if adjustedCursorPos > len(chunk.text):  # noqa: PLR1730 - upstream if
                                    adjustedCursorPos = len(chunk.text)

                    if hasCursorInChunk:
                        layoutLines.append(LayoutLine(text=chunk.text, hasCursor=True, cursorPos=adjustedCursorPos))
                    else:
                        layoutLines.append(LayoutLine(text=chunk.text, hasCursor=False))

        return layoutLines

    def getText(self) -> str:
        return "\n".join(self.state.lines)

    def expandPasteMarkers(self, text: str) -> str:
        result = text
        for pasteId, pasteContent in self.pastes.items():
            markerRegex = RegExp(f"\\[paste #{pasteId}( (\\+\\d+ lines|\\d+ chars))?\\]", "g")
            result = js_regexp.replace(result, markerRegex, lambda *_match, content=pasteContent: content)
        return result

    def getExpandedText(self) -> str:
        """Get text with paste markers expanded to their actual content.
        Use this when you need the full content (e.g., for external editor).
        """
        return self.expandPasteMarkers("\n".join(self.state.lines))

    def getLines(self) -> list[str]:
        return [*self.state.lines]

    def getCursor(self) -> dict[str, int]:
        return {"line": self.state.cursorLine, "col": self.state.cursorCol}

    def setText(self, text: str) -> None:
        self.cancelAutocomplete()
        self.lastAction = None
        self.exitHistoryBrowsing()
        normalized = self.normalizeText(text)
        # Push undo snapshot if content differs (makes programmatic changes undoable)
        if self.getText() != normalized:
            self.pushUndoSnapshot()
        self.pastes.clear()
        self.pasteCounter = 0
        self.setTextInternal(normalized)

    def insertTextAtCursor(self, text: str) -> None:
        """Insert text at the current cursor position.
        Used for programmatic insertion (e.g., clipboard image markers).
        This is atomic for undo - single undo restores entire pre-insert state.
        """
        if not text:
            return
        self.cancelAutocomplete()
        self.pushUndoSnapshot()
        self.lastAction = None
        self.exitHistoryBrowsing()
        self.insertTextAtCursorInternal(text)

    def normalizeText(self, text: str) -> str:
        """Normalize text for editor storage:
        - Normalize line endings (\\r\\n and \\r -> \\n)
        - Expand tabs to 4 spaces
        """
        return text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")

    def insertTextAtCursorInternal(self, text: str) -> None:
        """Internal text insertion at cursor. Handles single and multi-line text.
        Does not push undo snapshots or trigger autocomplete - caller is responsible.
        Normalizes line endings and calls onChange once at the end.
        """
        if not text:
            return

        # Normalize line endings and tabs
        normalized = self.normalizeText(text)
        insertedLines = normalized.split("\n")

        currentLine = jsIndex(self.state.lines, self.state.cursorLine) or ""
        beforeCursor = currentLine[: self.state.cursorCol]
        afterCursor = currentLine[self.state.cursorCol :]

        if len(insertedLines) == 1:
            # Single line - insert at cursor position
            self.state.lines[self.state.cursorLine] = beforeCursor + normalized + afterCursor
            self.setCursorCol(self.state.cursorCol + len(normalized))
        else:
            # Multi-line insertion
            self.state.lines = [
                # All lines before current line
                *self.state.lines[: self.state.cursorLine],
                # The first inserted line merged with text before cursor
                beforeCursor + insertedLines[0],
                # All middle inserted lines
                *insertedLines[1:-1],
                # The last inserted line with text after cursor
                insertedLines[len(insertedLines) - 1] + afterCursor,
                # All lines after current line
                *self.state.lines[self.state.cursorLine + 1 :],
            ]

            self.state.cursorLine += len(insertedLines) - 1
            self.setCursorCol(len(insertedLines[len(insertedLines) - 1] or ""))

        if self.onChange:
            self.onChange(self.getText())

    # All the editor methods from before...
    def insertCharacter(self, char: str, skipUndoCoalescing: bool = False) -> None:
        self.exitHistoryBrowsing()

        # Undo coalescing (fish-style):
        # - Consecutive word chars coalesce into one undo unit
        # - Space captures state before itself (so undo removes space+following word together)
        # - Each space is separately undoable
        # Skip coalescing when called from atomic operations (e.g., handlePaste)
        if not skipUndoCoalescing:
            if isWhitespaceChar(char) or self.lastAction != "type-word":
                self.pushUndoSnapshot()
            self.lastAction = "type-word"

        line = jsIndex(self.state.lines, self.state.cursorLine) or ""

        before = line[: self.state.cursorCol]
        after = line[self.state.cursorCol :]

        self.state.lines[self.state.cursorLine] = before + char + after
        self.setCursorCol(self.state.cursorCol + len(char))

        if self.onChange:
            self.onChange(self.getText())

        # Check if we should trigger or update autocomplete
        if not self.autocompleteState:
            # Auto-trigger for "/" at the start of a line (slash commands)
            if char == "/" and self.isAtStartOfMessage():
                self.tryTriggerAutocomplete()
            # Auto-trigger for symbol-based completion like @, #, or provider triggers at token boundaries
            elif char in self.autocompleteTriggerCharacters:
                currentLine = jsIndex(self.state.lines, self.state.cursorLine) or ""
                textBeforeCursor = currentLine[: self.state.cursorCol]
                if self.autocompleteTriggerPattern.test(textBeforeCursor):
                    self.tryTriggerAutocomplete()
            # Also auto-trigger when typing letters in a slash command or symbol completion context
            elif RegExp(r"[a-zA-Z0-9.\-_]").test(char) or cjkBreakRegex.test(char):
                currentLine = jsIndex(self.state.lines, self.state.cursorLine) or ""
                textBeforeCursor = currentLine[: self.state.cursorCol]
                # Check if we're in a slash command (with or without space for arguments)
                if self.isInSlashCommandContext(textBeforeCursor):  # noqa: SIM114 - upstream branches
                    self.tryTriggerAutocomplete()
                # Check if we're in a symbol-based completion context like @, #, or provider triggers
                elif self.autocompleteTriggerPattern.test(textBeforeCursor):
                    self.tryTriggerAutocomplete()
        else:
            self.updateAutocomplete()

    def handlePaste(self, pastedText: str) -> None:
        self.cancelAutocomplete()
        self.exitHistoryBrowsing()
        self.lastAction = None

        self.pushUndoSnapshot()

        # Some terminals (e.g. tmux popups with extended-keys-format=csi-u) re-encode
        # control bytes inside bracketed paste as CSI-u Ctrl+<letter> sequences
        # (ESC [ <codepoint> ; 5 u). Decode those back to their literal byte so the
        # per-char filter below preserves newlines instead of stripping ESC and
        # leaking the printable tail (e.g. "[106;5u") into the editor.
        def decode(match: str, code: str) -> str:
            cp = int(code)
            if cp >= 97 and cp <= 122:
                return chr(cp - 96)
            if cp >= 65 and cp <= 90:
                return chr(cp - 64)
            return match

        decodedText = js_regexp.replace(pastedText, RegExp(r"\x1b\[(\d+);5u", "g"), decode)

        # Clean the pasted text: normalize line endings, expand tabs
        cleanText = self.normalizeText(decodedText)

        # Filter out non-printable characters except newlines
        filteredText = "".join(char for char in cleanText if char == "\n" or ord(char) >= 32)

        # If pasting a file path (starts with /, ~, or .) and the character before
        # the cursor is a word character, prepend a space for better readability
        if RegExp(r"^[/~.]").test(filteredText):
            currentLine = jsIndex(self.state.lines, self.state.cursorLine) or ""
            charBeforeCursor = (jsIndex(currentLine, self.state.cursorCol - 1) or "") if self.state.cursorCol > 0 else ""
            if charBeforeCursor and RegExp(r"\w").test(charBeforeCursor):
                filteredText = f" {filteredText}"

        # Split into lines to check for large paste
        pastedLines = filteredText.split("\n")

        # Check if this is a large paste (> 10 lines or > 1000 characters)
        totalChars = len(filteredText)
        if len(pastedLines) > 10 or totalChars > 1000:
            # Store the paste and insert a marker
            self.pasteCounter += 1
            pasteId = self.pasteCounter
            self.pastes[pasteId] = filteredText

            # Insert marker like "[paste #1 +123 lines]" or "[paste #1 1234 chars]"
            marker = (
                f"[paste #{pasteId} +{len(pastedLines)} lines]"
                if len(pastedLines) > 10
                else f"[paste #{pasteId} {totalChars} chars]"
            )
            self.insertTextAtCursorInternal(marker)
            return

        if len(pastedLines) == 1:
            # Single line - insert atomically (do not trigger autocomplete during paste)
            self.insertTextAtCursorInternal(filteredText)
            return

        # Multi-line paste - use direct state manipulation
        self.insertTextAtCursorInternal(filteredText)

    def addNewLine(self) -> None:
        self.cancelAutocomplete()
        self.exitHistoryBrowsing()
        self.lastAction = None

        self.pushUndoSnapshot()

        currentLine = jsIndex(self.state.lines, self.state.cursorLine) or ""

        before = currentLine[: self.state.cursorCol]
        after = currentLine[self.state.cursorCol :]

        # Split current line
        self.state.lines[self.state.cursorLine] = before
        self.state.lines.insert(self.state.cursorLine + 1, after)

        # Move cursor to start of new line
        self.state.cursorLine += 1
        self.setCursorCol(0)

        if self.onChange:
            self.onChange(self.getText())

    def shouldSubmitOnBackslashEnter(self, data: str, kb: KeybindingsManager) -> bool:
        if self.disableSubmit:
            return False
        if not matchesKey(data, "enter"):
            return False
        submitKeys = kb.getKeys("tui.input.submit")
        hasShiftEnter = "shift+enter" in submitKeys or "shift+return" in submitKeys
        if not hasShiftEnter:
            return False

        currentLine = jsIndex(self.state.lines, self.state.cursorLine) or ""
        return self.state.cursorCol > 0 and jsIndex(currentLine, self.state.cursorCol - 1) == "\\"

    def submitValue(self) -> None:
        self.cancelAutocomplete()
        result = jsTrim(self.expandPasteMarkers("\n".join(self.state.lines)))

        self.state = EditorState(lines=[""], cursorLine=0, cursorCol=0)
        self.pastes.clear()
        self.pasteCounter = 0
        self.exitHistoryBrowsing()
        self.scrollOffset = 0
        self.undoStack.clear()
        self.lastAction = None

        if self.onChange:
            self.onChange("")
        if self.onSubmit:
            self.onSubmit(result)

    def handleBackspace(self) -> None:
        self.exitHistoryBrowsing()
        self.lastAction = None

        if self.state.cursorCol > 0:
            self.pushUndoSnapshot()

            # Delete grapheme before cursor (handles emojis, combining characters, etc.)
            line = jsIndex(self.state.lines, self.state.cursorLine) or ""
            beforeCursor = line[: self.state.cursorCol]

            # Find the last grapheme in the text before cursor
            graphemes = [*self.segment(beforeCursor, "grapheme")]
            lastGrapheme = graphemes[len(graphemes) - 1] if graphemes else None
            graphemeLength = len(lastGrapheme.segment) if lastGrapheme else 1
            isPastedSegmented = PASTE_MARKER_SINGLE.exec(lastGrapheme.segment) if lastGrapheme else None

            if isPastedSegmented:
                # This contains the id part e.g 4 from [paste #4 +123 lines]
                targetId = int(isPastedSegmented[1])
                self.pastes.pop(targetId, None)
                self.pasteCounter -= 1

                # Shift registry entries down in ascending id order, independent
                # of marker order in the text ([paste #3] becomes [paste #2] when
                # [paste #1] is removed).
                higherIds = sorted(id for id in self.pastes if id > targetId)
                for id in higherIds:
                    self.pastes[id - 1] = self.pastes[id]
                    del self.pastes[id]

                # Renumber markers with ids greater than the removed one.
                def renumber(text: str) -> str:
                    def replacer(fullMatch: str, idGroup: str, suffixGroup: str | None) -> str:
                        x = int(idGroup)
                        if x <= targetId:
                            return fullMatch
                        return f"[paste #{x - 1}{suffixGroup or ''}]"

                    return js_regexp.replace(text, PASTE_MARKER_REGEX, replacer)

                # MISAKA fork: cut the marker out at the old coordinates and renumber both halves. Upstream renumbers
                # every line first and then slices at the old cursor column; a renumbered marker left of the cursor
                # ("[paste #10 ...]" becoming "[paste #9 ...]") is shorter by then, so the slice leaves part of the
                # deleted marker behind.
                before = renumber(line[: self.state.cursorCol - graphemeLength])
                after = renumber(line[self.state.cursorCol :])
                self.state.lines = [renumber(line) for line in self.state.lines]
                self.state.lines[self.state.cursorLine] = before + after
                self.setCursorCol(len(before))
            else:
                line = jsIndex(self.state.lines, self.state.cursorLine) or ""

                before = line[: self.state.cursorCol - graphemeLength]
                after = line[self.state.cursorCol :]

                self.state.lines[self.state.cursorLine] = before + after
                self.setCursorCol(self.state.cursorCol - graphemeLength)
        elif self.state.cursorLine > 0:
            self.pushUndoSnapshot()

            # Merge with previous line
            currentLine = jsIndex(self.state.lines, self.state.cursorLine) or ""
            previousLine = jsIndex(self.state.lines, self.state.cursorLine - 1) or ""

            self.state.lines[self.state.cursorLine - 1] = previousLine + currentLine
            del self.state.lines[self.state.cursorLine]

            self.state.cursorLine -= 1
            self.setCursorCol(len(previousLine))

        if self.onChange:
            self.onChange(self.getText())

        # Update or re-trigger autocomplete after backspace
        if self.autocompleteState:
            self.updateAutocomplete()
        else:
            # If autocomplete was cancelled (no matches), re-trigger if we're in a completable context
            currentLine = jsIndex(self.state.lines, self.state.cursorLine) or ""
            textBeforeCursor = currentLine[: self.state.cursorCol]
            # Slash command context
            if self.isInSlashCommandContext(textBeforeCursor):  # noqa: SIM114 - upstream branches
                self.tryTriggerAutocomplete()
            # Symbol-based completion context like @, #, or provider triggers
            elif self.autocompleteTriggerPattern.test(textBeforeCursor):
                self.tryTriggerAutocomplete()

    def setCursorCol(self, col: int) -> None:
        """Set cursor column and clear preferredVisualCol.
        Use this for all non-vertical cursor movements to reset sticky column behavior.
        """
        self.state.cursorCol = col
        self.preferredVisualCol = None
        self.snappedFromCursorCol = None

    def moveToVisualLine(
        self,
        visualLines: list[_VisualLine],
        currentVisualLine: int,
        targetVisualLine: int,
    ) -> None:
        """Move cursor to a target visual line, applying sticky column logic.
        Shared by moveCursor() and pageScroll().
        """
        currentVL = jsIndex(visualLines, currentVisualLine)
        targetVL = jsIndex(visualLines, targetVisualLine)
        if not (currentVL and targetVL):
            return

        # When the cursor was snapped to a segment start, resolve the pre-snap
        # position against the VL it belongs to. This gives the correct visual
        # column even after a resize reshuffles VLs.
        currentVisualCol: int
        if self.snappedFromCursorCol is not None:
            vlIndex = self.findVisualLineAt(visualLines, currentVL["logicalLine"], self.snappedFromCursorCol)
            currentVisualCol = self.snappedFromCursorCol - visualLines[vlIndex]["startCol"]
        else:
            currentVisualCol = self.state.cursorCol - currentVL["startCol"]

        # For non-last segments, clamp to length-1 to stay within the segment
        nextSource = jsIndex(visualLines, currentVisualLine + 1)
        isLastSourceSegment = currentVisualLine == len(visualLines) - 1 or (
            nextSource is None or nextSource["logicalLine"] != currentVL["logicalLine"]
        )
        sourceMaxVisualCol = currentVL["length"] if isLastSourceSegment else max(0, currentVL["length"] - 1)

        nextTarget = jsIndex(visualLines, targetVisualLine + 1)
        isLastTargetSegment = targetVisualLine == len(visualLines) - 1 or (
            nextTarget is None or nextTarget["logicalLine"] != targetVL["logicalLine"]
        )
        targetMaxVisualCol = targetVL["length"] if isLastTargetSegment else max(0, targetVL["length"] - 1)

        moveToVisualCol = self.computeVerticalMoveColumn(currentVisualCol, sourceMaxVisualCol, targetMaxVisualCol)

        # Set cursor position
        self.state.cursorLine = targetVL["logicalLine"]
        targetCol = targetVL["startCol"] + moveToVisualCol
        logicalLine = jsIndex(self.state.lines, targetVL["logicalLine"]) or ""
        self.state.cursorCol = min(targetCol, len(logicalLine))

        # Snap cursor to atomic segment boundary (e.g. paste markers)
        # so the cursor never lands in the middle of a multi-grapheme unit.
        # Single-grapheme segments don't need snapping.
        segments = [*self.segment(logicalLine, "grapheme")]
        for seg in segments:
            if seg.index > self.state.cursorCol:
                break
            if len(seg.segment) <= 1:
                continue
            if self.state.cursorCol < seg.index + len(seg.segment):
                isContinuation = seg.index < targetVL["startCol"]
                isMovingDown = targetVisualLine > currentVisualLine

                if isContinuation and isMovingDown:
                    # The segment started on a previous visual line, and we
                    # already visited it on the way down. Skip all remaining
                    # continuation VLs and land on the first VL past it.
                    segEnd = seg.index + len(seg.segment)
                    next = targetVisualLine + 1
                    while (
                        next < len(visualLines)
                        and visualLines[next]["logicalLine"] == targetVL["logicalLine"]
                        and visualLines[next]["startCol"] < segEnd
                    ):
                        next += 1
                    if next < len(visualLines):
                        self.moveToVisualLine(visualLines, currentVisualLine, next)
                        return

                # Snap to the start of the segment so it gets highlighted.
                # Store the pre-snap position so the next vertical move can
                # resolve it to the correct visual column.
                self.snappedFromCursorCol = self.state.cursorCol
                self.state.cursorCol = seg.index
                return

        # No snap occurred – we moved out of the atomic segment.
        self.snappedFromCursorCol = None

    def computeVerticalMoveColumn(
        self,
        currentVisualCol: int,
        sourceMaxVisualCol: int,
        targetMaxVisualCol: int,
    ) -> int:
        """Compute the target visual column for vertical cursor movement.
        Implements the sticky column decision table:

        | P | S | T | U | Scenario                                             | Set Preferred | Move To     |
        |---|---|---|---| ---------------------------------------------------- |---------------|-------------|
        | 0 | * | 0 | - | Start nav, target fits                               | null          | current     |
        | 0 | * | 1 | - | Start nav, target shorter                            | current       | target end  |
        | 1 | 0 | 0 | 0 | Clamped, target fits preferred                       | null          | preferred   |
        | 1 | 0 | 0 | 1 | Clamped, target longer but still can't fit preferred | keep          | target end  |
        | 1 | 0 | 1 | - | Clamped, target even shorter                         | keep          | target end  |
        | 1 | 1 | 0 | - | Rewrapped, target fits current                       | null          | current     |
        | 1 | 1 | 1 | - | Rewrapped, target shorter than current               | current       | target end  |

        Where:
        - P = preferred col is set
        - S = cursor in middle of source line (not clamped to end)
        - T = target line shorter than current visual col
        - U = target line shorter than preferred col
        """
        hasPreferred = self.preferredVisualCol is not None  # P
        cursorInMiddle = currentVisualCol < sourceMaxVisualCol  # S
        targetTooShort = targetMaxVisualCol < currentVisualCol  # T

        if not hasPreferred or cursorInMiddle:
            if targetTooShort:
                # Cases 2 and 7
                self.preferredVisualCol = currentVisualCol
                return targetMaxVisualCol

            # Cases 1 and 6
            self.preferredVisualCol = None
            return currentVisualCol

        assert self.preferredVisualCol is not None
        targetCantFitPreferred = targetMaxVisualCol < self.preferredVisualCol  # U
        if targetTooShort or targetCantFitPreferred:
            # Cases 4 and 5
            return targetMaxVisualCol

        # Case 3
        result = self.preferredVisualCol
        self.preferredVisualCol = None
        return result

    def moveToLineStart(self) -> None:
        self.lastAction = None
        self.setCursorCol(0)

    def moveToLineEnd(self) -> None:
        self.lastAction = None
        currentLine = jsIndex(self.state.lines, self.state.cursorLine) or ""
        self.setCursorCol(len(currentLine))

    def deleteToStartOfLine(self) -> None:
        self.exitHistoryBrowsing()

        currentLine = jsIndex(self.state.lines, self.state.cursorLine) or ""

        if self.state.cursorCol > 0:
            self.pushUndoSnapshot()

            # Calculate text to be deleted and save to kill ring (backward deletion = prepend)
            deletedText = currentLine[: self.state.cursorCol]
            self.killRing.push(deletedText, {"prepend": True, "accumulate": self.lastAction == "kill"})
            self.lastAction = "kill"

            # Delete from start of line up to cursor
            self.state.lines[self.state.cursorLine] = currentLine[self.state.cursorCol :]
            self.setCursorCol(0)
        elif self.state.cursorLine > 0:
            self.pushUndoSnapshot()

            # At start of line - merge with previous line, treating newline as deleted text
            self.killRing.push("\n", {"prepend": True, "accumulate": self.lastAction == "kill"})
            self.lastAction = "kill"

            previousLine = jsIndex(self.state.lines, self.state.cursorLine - 1) or ""
            self.state.lines[self.state.cursorLine - 1] = previousLine + currentLine
            del self.state.lines[self.state.cursorLine]
            self.state.cursorLine -= 1
            self.setCursorCol(len(previousLine))

        if self.onChange:
            self.onChange(self.getText())

    def deleteToEndOfLine(self) -> None:
        self.exitHistoryBrowsing()

        currentLine = jsIndex(self.state.lines, self.state.cursorLine) or ""

        if self.state.cursorCol < len(currentLine):
            self.pushUndoSnapshot()

            # Calculate text to be deleted and save to kill ring (forward deletion = append)
            deletedText = currentLine[self.state.cursorCol :]
            self.killRing.push(deletedText, {"prepend": False, "accumulate": self.lastAction == "kill"})
            self.lastAction = "kill"

            # Delete from cursor to end of line
            self.state.lines[self.state.cursorLine] = currentLine[: self.state.cursorCol]
        elif self.state.cursorLine < len(self.state.lines) - 1:
            self.pushUndoSnapshot()

            # At end of line - merge with next line, treating newline as deleted text
            self.killRing.push("\n", {"prepend": False, "accumulate": self.lastAction == "kill"})
            self.lastAction = "kill"

            nextLine = jsIndex(self.state.lines, self.state.cursorLine + 1) or ""
            self.state.lines[self.state.cursorLine] = currentLine + nextLine
            del self.state.lines[self.state.cursorLine + 1]

        if self.onChange:
            self.onChange(self.getText())

    def deleteWordBackwards(self) -> None:
        self.exitHistoryBrowsing()

        currentLine = jsIndex(self.state.lines, self.state.cursorLine) or ""

        # If at start of line, behave like backspace at column 0 (merge with previous line)
        if self.state.cursorCol == 0:
            if self.state.cursorLine > 0:
                self.pushUndoSnapshot()

                # Treat newline as deleted text (backward deletion = prepend)
                self.killRing.push("\n", {"prepend": True, "accumulate": self.lastAction == "kill"})
                self.lastAction = "kill"

                previousLine = jsIndex(self.state.lines, self.state.cursorLine - 1) or ""
                self.state.lines[self.state.cursorLine - 1] = previousLine + currentLine
                del self.state.lines[self.state.cursorLine]
                self.state.cursorLine -= 1
                self.setCursorCol(len(previousLine))
        else:
            self.pushUndoSnapshot()

            # Save lastAction before cursor movement (moveWordBackwards resets it)
            wasKill = self.lastAction == "kill"

            oldCursorCol = self.state.cursorCol
            self.moveWordBackwards()
            deleteFrom = self.state.cursorCol
            self.setCursorCol(oldCursorCol)

            deletedText = currentLine[deleteFrom : self.state.cursorCol]
            self.killRing.push(deletedText, {"prepend": True, "accumulate": wasKill})
            self.lastAction = "kill"

            self.state.lines[self.state.cursorLine] = currentLine[:deleteFrom] + currentLine[self.state.cursorCol :]
            self.setCursorCol(deleteFrom)

        if self.onChange:
            self.onChange(self.getText())

    def deleteWordForward(self) -> None:
        self.exitHistoryBrowsing()

        currentLine = jsIndex(self.state.lines, self.state.cursorLine) or ""

        # If at end of line, merge with next line (delete the newline)
        if self.state.cursorCol >= len(currentLine):
            if self.state.cursorLine < len(self.state.lines) - 1:
                self.pushUndoSnapshot()

                # Treat newline as deleted text (forward deletion = append)
                self.killRing.push("\n", {"prepend": False, "accumulate": self.lastAction == "kill"})
                self.lastAction = "kill"

                nextLine = jsIndex(self.state.lines, self.state.cursorLine + 1) or ""
                self.state.lines[self.state.cursorLine] = currentLine + nextLine
                del self.state.lines[self.state.cursorLine + 1]
        else:
            self.pushUndoSnapshot()

            # Save lastAction before cursor movement (moveWordForwards resets it)
            wasKill = self.lastAction == "kill"

            oldCursorCol = self.state.cursorCol
            self.moveWordForwards()
            deleteTo = self.state.cursorCol
            self.setCursorCol(oldCursorCol)

            deletedText = currentLine[self.state.cursorCol : deleteTo]
            self.killRing.push(deletedText, {"prepend": False, "accumulate": wasKill})
            self.lastAction = "kill"

            self.state.lines[self.state.cursorLine] = currentLine[: self.state.cursorCol] + currentLine[deleteTo:]

        if self.onChange:
            self.onChange(self.getText())

    def handleForwardDelete(self) -> None:
        self.exitHistoryBrowsing()
        self.lastAction = None

        currentLine = jsIndex(self.state.lines, self.state.cursorLine) or ""

        if self.state.cursorCol < len(currentLine):
            self.pushUndoSnapshot()

            # Delete grapheme at cursor position (handles emojis, combining characters, etc.)
            afterCursor = currentLine[self.state.cursorCol :]

            # Find the first grapheme at cursor
            graphemes = [*self.segment(afterCursor, "grapheme")]
            firstGrapheme = jsIndex(graphemes, 0)
            graphemeLength = len(firstGrapheme.segment) if firstGrapheme else 1

            before = currentLine[: self.state.cursorCol]
            after = currentLine[self.state.cursorCol + graphemeLength :]
            self.state.lines[self.state.cursorLine] = before + after
        elif self.state.cursorLine < len(self.state.lines) - 1:
            self.pushUndoSnapshot()

            # At end of line - merge with next line
            nextLine = jsIndex(self.state.lines, self.state.cursorLine + 1) or ""
            self.state.lines[self.state.cursorLine] = currentLine + nextLine
            del self.state.lines[self.state.cursorLine + 1]

        if self.onChange:
            self.onChange(self.getText())

        # Update or re-trigger autocomplete after forward delete
        if self.autocompleteState:
            self.updateAutocomplete()
        else:
            currentLine = jsIndex(self.state.lines, self.state.cursorLine) or ""
            textBeforeCursor = currentLine[: self.state.cursorCol]
            # Slash command context
            if self.isInSlashCommandContext(textBeforeCursor):  # noqa: SIM114 - upstream branches
                self.tryTriggerAutocomplete()
            # Symbol-based completion context like @, #, or provider triggers
            elif self.autocompleteTriggerPattern.test(textBeforeCursor):
                self.tryTriggerAutocomplete()

    def buildVisualLineMap(self, width: int) -> list[_VisualLine]:
        """Build a mapping from visual lines to logical positions.
        Returns an array where each element represents a visual line with:
        - logicalLine: index into this.state.lines
        - startCol: starting column in the logical line
        - length: length of this visual line segment
        """
        visualLines: list[_VisualLine] = []

        for i in range(len(self.state.lines)):
            line = self.state.lines[i] or ""
            lineVisWidth = visibleWidth(line)
            if len(line) == 0:
                # Empty line still takes one visual line
                visualLines.append({"logicalLine": i, "startCol": 0, "length": 0})
            elif lineVisWidth <= width:
                visualLines.append({"logicalLine": i, "startCol": 0, "length": len(line)})
            else:
                # Line needs wrapping - use word-aware wrapping
                chunks = wordWrapLine(line, width, [*self.segment(line, "grapheme")])
                for chunk in chunks:
                    visualLines.append(
                        {
                            "logicalLine": i,
                            "startCol": chunk.startIndex,
                            "length": chunk.endIndex - chunk.startIndex,
                        }
                    )

        return visualLines

    def findVisualLineAt(self, visualLines: list[_VisualLine], line: int, col: int) -> int:
        """Find the visual line index that contains the given logical position."""
        for i in range(len(visualLines)):
            vl = visualLines[i]
            if not vl or vl["logicalLine"] != line:
                continue
            offset = col - vl["startCol"]
            # Cursor is in this segment if it's within range. For the last
            # segment of a logical line, cursor can be at length (end position)
            nextVl = jsIndex(visualLines, i + 1)
            isLastSegmentOfLine = i == len(visualLines) - 1 or (
                nextVl is None or nextVl["logicalLine"] != vl["logicalLine"]
            )
            if offset >= 0 and (offset < vl["length"] or (isLastSegmentOfLine and offset == vl["length"])):
                return i
        return len(visualLines) - 1

    def findCurrentVisualLine(self, visualLines: list[_VisualLine]) -> int:
        """Find the visual line index for the current cursor position."""
        return self.findVisualLineAt(visualLines, self.state.cursorLine, self.state.cursorCol)

    def moveCursor(self, deltaLine: int, deltaCol: int) -> None:
        self.lastAction = None
        visualLines = self.buildVisualLineMap(self.lastWidth)
        currentVisualLine = self.findCurrentVisualLine(visualLines)

        if deltaLine != 0:
            targetVisualLine = currentVisualLine + deltaLine

            if targetVisualLine >= 0 and targetVisualLine < len(visualLines):
                self.moveToVisualLine(visualLines, currentVisualLine, targetVisualLine)

        if deltaCol != 0:
            currentLine = jsIndex(self.state.lines, self.state.cursorLine) or ""

            if deltaCol > 0:
                # Moving right - move by one grapheme (handles emojis, combining characters, etc.)
                if self.state.cursorCol < len(currentLine):
                    afterCursor = currentLine[self.state.cursorCol :]
                    graphemes = [*self.segment(afterCursor, "grapheme")]
                    firstGrapheme = jsIndex(graphemes, 0)
                    self.setCursorCol(self.state.cursorCol + (len(firstGrapheme.segment) if firstGrapheme else 1))
                elif self.state.cursorLine < len(self.state.lines) - 1:
                    # Wrap to start of next logical line
                    self.state.cursorLine += 1
                    self.setCursorCol(0)
                else:
                    # At end of last line - can't move, but set preferredVisualCol for up/down navigation
                    currentVL = jsIndex(visualLines, currentVisualLine)
                    if currentVL:
                        self.preferredVisualCol = self.state.cursorCol - currentVL["startCol"]
            else:
                # Moving left - move by one grapheme (handles emojis, combining characters, etc.)
                if self.state.cursorCol > 0:
                    beforeCursor = currentLine[: self.state.cursorCol]
                    graphemes = [*self.segment(beforeCursor, "grapheme")]
                    lastGrapheme = jsIndex(graphemes, len(graphemes) - 1)
                    self.setCursorCol(self.state.cursorCol - (len(lastGrapheme.segment) if lastGrapheme else 1))
                elif self.state.cursorLine > 0:
                    # Wrap to end of previous logical line
                    self.state.cursorLine -= 1
                    prevLine = jsIndex(self.state.lines, self.state.cursorLine) or ""
                    self.setCursorCol(len(prevLine))

        # Keep an open autocomplete picker in sync with the new cursor
        # position: cursor movement changes the text before the cursor, so a
        # picker computed for the old position is stale. Re-query so it
        # refreshes — or closes when the new position yields no suggestions —
        # mirroring insertCharacter()/handleBackspace(). Without this, arrowing
        # left from `/cmd ` back into the command name leaves the argument
        # picker showing against a `/cmd` prefix (and a Tab there would
        # concatenate the stale suggestion onto the partial command name).
        if self.autocompleteState:
            self.updateAutocomplete()

    def pageScroll(self, direction: Literal[-1, 1]) -> None:
        """Scroll by a page (direction: -1 for up, 1 for down).
        Moves cursor by the page size while keeping it in bounds.
        """
        self.lastAction = None
        terminalRows = self.tui.terminal.rows
        pageSize = max(5, math.floor(terminalRows * 0.3))

        visualLines = self.buildVisualLineMap(self.lastWidth)
        currentVisualLine = self.findCurrentVisualLine(visualLines)
        targetVisualLine = max(0, min(len(visualLines) - 1, currentVisualLine + direction * pageSize))

        self.moveToVisualLine(visualLines, currentVisualLine, targetVisualLine)

    def moveWordBackwards(self) -> None:
        self.lastAction = None
        currentLine = jsIndex(self.state.lines, self.state.cursorLine) or ""

        # If at start of line, move to end of previous line
        if self.state.cursorCol == 0:
            if self.state.cursorLine > 0:
                self.state.cursorLine -= 1
                prevLine = jsIndex(self.state.lines, self.state.cursorLine) or ""
                self.setCursorCol(len(prevLine))
            return

        self.setCursorCol(
            findWordBackward(
                currentLine,
                self.state.cursorCol,
                WordNavigationOptions(
                    segment=lambda text: self.segment(text, "word"),
                    isAtomicSegment=isPasteMarker,
                ),
            )
        )

    def yank(self) -> None:
        """Yank (paste) the most recent kill ring entry at cursor position."""
        if self.killRing.length == 0:
            return

        self.pushUndoSnapshot()

        text = self.killRing.peek()
        assert text is not None
        self.insertYankedText(text)

        self.lastAction = "yank"

    def yankPop(self) -> None:
        """Cycle through kill ring (only works immediately after yank or yank-pop).
        Replaces the last yanked text with the previous entry in the ring.
        """
        # Only works if we just yanked and have more than one entry
        if self.lastAction != "yank" or self.killRing.length <= 1:
            return

        self.pushUndoSnapshot()

        # Delete the previously yanked text (still at end of ring before rotation)
        self.deleteYankedText()

        # Rotate the ring: move end to front
        self.killRing.rotate()

        # Insert the new most recent entry (now at end after rotation)
        text = self.killRing.peek()
        assert text is not None
        self.insertYankedText(text)

        self.lastAction = "yank"

    def insertYankedText(self, text: str) -> None:
        """Insert text at cursor position (used by yank operations)."""
        self.exitHistoryBrowsing()
        lines = text.split("\n")

        if len(lines) == 1:
            # Single line - insert at cursor
            currentLine = jsIndex(self.state.lines, self.state.cursorLine) or ""
            before = currentLine[: self.state.cursorCol]
            after = currentLine[self.state.cursorCol :]
            self.state.lines[self.state.cursorLine] = before + text + after
            self.setCursorCol(self.state.cursorCol + len(text))
        else:
            # Multi-line insert
            currentLine = jsIndex(self.state.lines, self.state.cursorLine) or ""
            before = currentLine[: self.state.cursorCol]
            after = currentLine[self.state.cursorCol :]

            # First line merges with text before cursor
            self.state.lines[self.state.cursorLine] = before + (lines[0] or "")

            # Insert middle lines
            for i in range(1, len(lines) - 1):
                self.state.lines.insert(self.state.cursorLine + i, lines[i] or "")

            # Last line merges with text after cursor
            lastLineIndex = self.state.cursorLine + len(lines) - 1
            self.state.lines.insert(lastLineIndex, (lines[len(lines) - 1] or "") + after)

            # Update cursor position
            self.state.cursorLine = lastLineIndex
            self.setCursorCol(len(lines[len(lines) - 1] or ""))

        if self.onChange:
            self.onChange(self.getText())

    def deleteYankedText(self) -> None:
        """Delete the previously yanked text (used by yank-pop).
        The yanked text is derived from killRing[end] since it hasn't been rotated yet.
        """
        yankedText = self.killRing.peek()
        if not yankedText:
            return

        yankLines = yankedText.split("\n")

        if len(yankLines) == 1:
            # Single line - delete backward from cursor
            currentLine = jsIndex(self.state.lines, self.state.cursorLine) or ""
            deleteLen = len(yankedText)
            before = currentLine[: self.state.cursorCol - deleteLen]
            after = currentLine[self.state.cursorCol :]
            self.state.lines[self.state.cursorLine] = before + after
            self.setCursorCol(self.state.cursorCol - deleteLen)
        else:
            # Multi-line delete - cursor is at end of last yanked line
            startLine = self.state.cursorLine - (len(yankLines) - 1)
            startCol = len(jsIndex(self.state.lines, startLine) or "") - len(yankLines[0] or "")

            # Get text after cursor on current line
            afterCursor = (jsIndex(self.state.lines, self.state.cursorLine) or "")[self.state.cursorCol :]

            # Get text before yank start position
            beforeYank = (jsIndex(self.state.lines, startLine) or "")[:startCol]

            # Remove all lines from startLine to cursorLine and replace with merged line
            self.state.lines[startLine : startLine + len(yankLines)] = [beforeYank + afterCursor]

            # Update cursor
            self.state.cursorLine = startLine
            self.setCursorCol(startCol)

        if self.onChange:
            self.onChange(self.getText())

    def pushUndoSnapshot(self) -> None:
        self.undoStack.push(EditorSnapshot(state=self.state, pastes=self.pastes, pasteCounter=self.pasteCounter))

    def undo(self) -> None:
        self.exitHistoryBrowsing()
        snapshot = self.undoStack.pop()
        if not snapshot:
            return
        # `Object.assign(this.state, snapshot.state)`
        self.state.lines = snapshot.state.lines
        self.state.cursorLine = snapshot.state.cursorLine
        self.state.cursorCol = snapshot.state.cursorCol
        self.pastes = snapshot.pastes
        self.pasteCounter = snapshot.pasteCounter
        self.lastAction = None
        self.preferredVisualCol = None
        if self.onChange:
            self.onChange(self.getText())

    def jumpToChar(self, char: str, direction: Literal["forward", "backward"]) -> None:
        """Jump to the first occurrence of a character in the specified direction.
        Multi-line search. Case-sensitive. Skips the current cursor position.
        """
        self.lastAction = None
        isForward = direction == "forward"
        lines = self.state.lines

        end = len(lines) if isForward else -1
        step = 1 if isForward else -1

        lineIdx = self.state.cursorLine
        while lineIdx != end:
            line = jsIndex(lines, lineIdx) or ""
            isCurrentLine = lineIdx == self.state.cursorLine

            # Current line: start after/before cursor; other lines: search full line
            searchFrom = (
                (self.state.cursorCol + 1 if isForward else self.state.cursorCol - 1) if isCurrentLine else None
            )

            if isForward:
                idx = line.find(char, searchFrom if searchFrom is not None else 0)
            else:
                # `lastIndexOf(char, searchFrom)`: the last match starting at or before `searchFrom` (clamped to the
                # string), the whole string when it is undefined.
                position = len(line) if searchFrom is None else min(max(searchFrom, 0), len(line))
                idx = line.rfind(char, 0, position + len(char))

            if idx != -1:
                self.state.cursorLine = lineIdx
                self.setCursorCol(idx)
                return
            lineIdx += step
        # No match found - cursor stays in place

    def moveWordForwards(self) -> None:
        self.lastAction = None
        currentLine = jsIndex(self.state.lines, self.state.cursorLine) or ""

        # If at end of line, move to start of next line
        if self.state.cursorCol >= len(currentLine):
            if self.state.cursorLine < len(self.state.lines) - 1:
                self.state.cursorLine += 1
                self.setCursorCol(0)
            return

        self.setCursorCol(
            findWordForward(
                currentLine,
                self.state.cursorCol,
                WordNavigationOptions(
                    segment=lambda text: self.segment(text, "word"),
                    isAtomicSegment=isPasteMarker,
                ),
            )
        )

    # Slash menu only allowed on the first line of the editor
    def isSlashMenuAllowed(self) -> bool:
        return self.state.cursorLine == 0

    # Helper method to check if cursor is at start of message (for slash command detection)
    def isAtStartOfMessage(self) -> bool:
        if not self.isSlashMenuAllowed():
            return False
        currentLine = jsIndex(self.state.lines, self.state.cursorLine) or ""
        beforeCursor = currentLine[: self.state.cursorCol]
        return jsTrim(beforeCursor) == "" or jsTrim(beforeCursor) == "/"

    def isInSlashCommandContext(self, textBeforeCursor: str) -> bool:
        return self.isSlashMenuAllowed() and jsTrimStart(textBeforeCursor).startswith("/")

    # Autocomplete methods
    def getBestAutocompleteMatchIndex(self, items: Sequence[Any], prefix: str) -> int:
        """Find the best autocomplete item index for the given prefix.
        Returns -1 if no match is found.

        Match priority:
        1. Exact match (prefix === item.value) -> always selected
        2. Prefix match -> first item whose value starts with prefix
        3. No match -> -1 (keep default highlight)

        Matching is case-sensitive and checks item.value only.
        """
        if not prefix:
            return -1

        firstPrefixIndex = -1

        for i in range(len(items)):
            value = items[i].value
            if value == prefix:
                return i  # Exact match always wins
            if firstPrefixIndex == -1 and value.startswith(prefix):
                firstPrefixIndex = i

        return firstPrefixIndex

    def createAutocompleteList(self, prefix: str, items: Sequence[Any]) -> SelectList:
        layout = SLASH_COMMAND_SELECT_LIST_LAYOUT if prefix.startswith("/") else None
        # The autocomplete items go in as they are, as upstream passes them: they have a select item's fields.
        selectList = SelectList(cast("list[SelectItem]", list(items)), self.autocompleteMaxVisible, self.theme.selectList, layout)
        # MISAKA fork (D1): upstream's `list.onSelect` follows here; the list calls it only on a mouse click (the
        # editor handles confirm itself).
        return selectList

    def tryTriggerAutocomplete(self, explicitTab: bool = False) -> None:
        self.requestAutocomplete({"force": False, "explicitTab": explicitTab})

    def handleTabCompletion(self) -> None:
        if not self.autocompleteProvider:
            return

        currentLine = jsIndex(self.state.lines, self.state.cursorLine) or ""
        beforeCursor = currentLine[: self.state.cursorCol]

        if self.isInSlashCommandContext(beforeCursor) and " " not in jsTrimStart(beforeCursor):
            self.handleSlashCommandCompletion()
        else:
            self.forceFileAutocomplete(True)

    def handleSlashCommandCompletion(self) -> None:
        self.requestAutocomplete({"force": False, "explicitTab": True})

    def forceFileAutocomplete(self, explicitTab: bool = False) -> None:
        self.requestAutocomplete({"force": True, "explicitTab": explicitTab})

    def requestAutocomplete(self, options: _AutocompleteRequestOptions) -> None:
        if not self.autocompleteProvider:
            return

        if options["force"]:
            shouldTriggerFileCompletion = getattr(self.autocompleteProvider, "shouldTriggerFileCompletion", None)
            shouldTrigger = not shouldTriggerFileCompletion or shouldTriggerFileCompletion(
                self.state.lines,
                self.state.cursorLine,
                self.state.cursorCol,
            )
            if not shouldTrigger:
                return

        self.cancelAutocompleteRequest()
        self.autocompleteStartToken += 1
        startToken = self.autocompleteStartToken

        # MISAKA fork: `setTimeout` and an async function's start need an asyncio loop; without one (an editor driven
        # from synchronous code) nothing is requested.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        debounceMs = self.getAutocompleteDebounceMs(options)
        if debounceMs > 0:

            def onTimeout() -> None:
                self.autocompleteDebounceTimer = None
                self.startAutocompleteRequest(startToken, options)

            self.autocompleteDebounceTimer = loop.call_later(debounceMs / 1000, onTimeout)
            return

        self.startAutocompleteRequest(startToken, options)

    @eager
    async def startAutocompleteRequest(self, startToken: int, options: _AutocompleteRequestOptions) -> None:
        previousTask = self.autocompleteRequestTask

        async def task() -> None:
            if previousTask is not None:
                await previousTask
            if startToken != self.autocompleteStartToken or not self.autocompleteProvider:
                return

            controller = AbortController()
            self.autocompleteAbort = controller
            self.autocompleteRequestId += 1
            requestId = self.autocompleteRequestId
            snapshotText = self.getText()
            snapshotLine = self.state.cursorLine
            snapshotCol = self.state.cursorCol

            await self.runAutocompleteRequest(requestId, controller, snapshotText, snapshotLine, snapshotCol, options)

        self.autocompleteRequestTask = startTask(task())
        await self.autocompleteRequestTask

    def setAutocompleteTriggerCharacters(self, triggerCharacters: list[str]) -> None:
        next = [*DEFAULT_AUTOCOMPLETE_TRIGGER_CHARACTERS]
        for character in triggerCharacters:
            if len(character) != 1 or character == "/" or isWhitespaceChar(character) or character in next:
                continue
            next.append(character)
        self.autocompleteTriggerCharacters = next
        self.autocompleteTriggerPattern = buildTriggerPattern(next)
        self.autocompleteDebouncePattern = buildDebouncePattern(next)

    def getAutocompleteDebounceMs(self, options: _AutocompleteRequestOptions) -> int:
        if options["explicitTab"] or options["force"]:
            return 0

        currentLine = jsIndex(self.state.lines, self.state.cursorLine) or ""
        textBeforeCursor = currentLine[: self.state.cursorCol]
        return ATTACHMENT_AUTOCOMPLETE_DEBOUNCE_MS if self.autocompleteDebouncePattern.test(textBeforeCursor) else 0

    async def runAutocompleteRequest(
        self,
        requestId: int,
        controller: AbortController,
        snapshotText: str,
        snapshotLine: int,
        snapshotCol: int,
        options: _AutocompleteRequestOptions,
    ) -> None:
        if not self.autocompleteProvider:
            return

        suggestions = await self.autocompleteProvider.getSuggestions(
            self.state.lines,
            self.state.cursorLine,
            self.state.cursorCol,
            {"signal": controller.signal, "force": options["force"]},
        )

        if not self.isAutocompleteRequestCurrent(requestId, controller, snapshotText, snapshotLine, snapshotCol):
            return

        self.autocompleteAbort = None

        if not suggestions or not isinstance(suggestions.items, list) or len(suggestions.items) == 0:
            self.cancelAutocomplete()
            self.tui.requestRender()
            return

        if options["force"] and options["explicitTab"] and len(suggestions.items) == 1:
            item = suggestions.items[0]
            self.pushUndoSnapshot()
            self.lastAction = None
            result = self.autocompleteProvider.applyCompletion(
                self.state.lines,
                self.state.cursorLine,
                self.state.cursorCol,
                item,
                suggestions.prefix,
            )
            self.state.lines = result["lines"]
            self.state.cursorLine = result["cursorLine"]
            self.setCursorCol(result["cursorCol"])
            if self.onChange:
                self.onChange(self.getText())
            self.tui.requestRender()
            return

        self.applyAutocompleteSuggestions(suggestions, "force" if options["force"] else "regular")
        self.tui.requestRender()

    def isAutocompleteRequestCurrent(
        self,
        requestId: int,
        controller: AbortController,
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

    def applyAutocompleteSuggestions(
        self, suggestions: AutocompleteSuggestions, state: Literal["regular", "force"]
    ) -> None:
        self.autocompletePrefix = suggestions.prefix
        self.autocompleteList = self.createAutocompleteList(suggestions.prefix, suggestions.items)

        bestMatchIndex = self.getBestAutocompleteMatchIndex(suggestions.items, suggestions.prefix)
        if bestMatchIndex >= 0:
            self.autocompleteList.setSelectedIndex(bestMatchIndex)

        self.autocompleteState = state

    def cancelAutocompleteRequest(self) -> None:
        self.autocompleteStartToken += 1
        if self.autocompleteDebounceTimer:
            self.autocompleteDebounceTimer.cancel()
            self.autocompleteDebounceTimer = None
        if self.autocompleteAbort is not None:
            self.autocompleteAbort.abort()
        self.autocompleteAbort = None

    def clearAutocompleteUi(self) -> None:
        self.autocompleteState = None
        self.autocompleteList = None
        self.autocompletePrefix = ""

    def cancelAutocomplete(self) -> None:
        self.cancelAutocompleteRequest()
        self.clearAutocompleteUi()

    def isShowingAutocomplete(self) -> bool:
        return self.autocompleteState is not None

    def updateAutocomplete(self) -> None:
        if not self.autocompleteState or not self.autocompleteProvider:
            return
        self.requestAutocomplete({"force": self.autocompleteState == "force", "explicitTab": False})


__all__ = [
    "Editor",
    "EditorOptions",
    "EditorTheme",
    "TextChunk",
    "wordWrapLine",
]

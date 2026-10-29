"""Translated from pi's ``modes/interactive/components/session-selector.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio
import math
import os
import re
import subprocess
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, TypedDict

from misaka.config import sessions as session_paths
from misaka.core.keybindings import KeybindingsManager
from misaka.core.session_manager import SessionInfo, SessionListProgress
from misaka.modes.interactive.components.footer import formatCwdForFooter
from misaka.modes.interactive.theme.theme import theme
from misaka.ui.tui import (
    Container,
    Input,
    Spacer,
    Text,
    getKeybindings,
    truncateToWidth,
    visibleWidth,
)
from misaka.utils.js import (
    AbortController,
    Timeout,
    clearTimeout,
    errorMessage,
    jsIndex,
    jsSlice,
    jsTrim,
    setTimeout,
    spawn,
)
from misaka.utils.paths import canonicalizePath as _canonicalizePath

from .dynamic_border import DynamicBorder
from .keybinding_hints import keyHint, keyText
from .session_selector_search import (
    NameFilter,
    SortMode,
    filterAndSortSessions,
    hasSessionName,
)

type SessionScope = Literal["current", "all"]


def shortenPath(path: str) -> str:
    # MISAKA fork: the footer's component-wise home collapse; pi's `startsWith(home)` turns /home/abc/x into "~c/x"
    # when HOME is /home/ab (audit 2026-09-02).
    home = os.path.expanduser("~")
    if not path:
        return path
    return formatCwdForFooter(path, home)


def formatSessionDate(date: datetime) -> str:
    diffMs = time.time() * 1000 - date.timestamp() * 1000
    diffMins = math.floor(diffMs / 60000)
    diffHours = math.floor(diffMs / 3600000)
    diffDays = math.floor(diffMs / 86400000)

    if diffMins < 1:
        return "now"
    if diffMins < 60:
        return f"{diffMins}m"
    if diffHours < 24:
        return f"{diffHours}h"
    if diffDays < 7:
        return f"{diffDays}d"
    if diffDays < 30:
        return f"{math.floor(diffDays / 7)}w"
    if diffDays < 365:
        return f"{math.floor(diffDays / 30)}mo"
    return f"{math.floor(diffDays / 365)}y"


def canonicalizePath(path: str | None) -> str | None:
    if not path:
        return path
    return _canonicalizePath(path)


class SessionStatusMessage(TypedDict):
    """MISAKA fork: a name for upstream's inline ``{ type; message }`` status type."""

    type: Literal["info", "error"]
    message: str


class _LoadProgress(TypedDict):
    loaded: int
    total: int


class SessionSelectorHeader:
    def __init__(
        self,
        scope: SessionScope,
        sortMode: SortMode,
        nameFilter: NameFilter,
        requestRender: Callable[[], None],
    ) -> None:
        self.loading = False
        self.loadProgress: _LoadProgress | None = None
        self.showPath = False
        self.confirmingDeletePath: str | None = None
        self.statusMessage: SessionStatusMessage | None = None
        self.statusTimeout: Timeout | None = None
        self.showRenameHint = False

        self.scope = scope
        self.sortMode = sortMode
        self.nameFilter = nameFilter
        self.requestRender = requestRender

    def setScope(self, scope: SessionScope) -> None:
        self.scope = scope

    def setSortMode(self, sortMode: SortMode) -> None:
        self.sortMode = sortMode

    def setNameFilter(self, nameFilter: NameFilter) -> None:
        self.nameFilter = nameFilter

    def setLoading(self, loading: bool) -> None:
        self.loading = loading
        # Progress is scoped to the current load; clear whenever the loading state is set
        self.loadProgress = None

    def setProgress(self, loaded: int, total: int) -> None:
        self.loadProgress = {"loaded": loaded, "total": total}

    def setShowPath(self, showPath: bool) -> None:
        self.showPath = showPath

    def setShowRenameHint(self, show: bool) -> None:
        self.showRenameHint = show

    def setConfirmingDeletePath(self, path: str | None) -> None:
        self.confirmingDeletePath = path

    def clearStatusTimeout(self) -> None:
        if not self.statusTimeout:
            return
        clearTimeout(self.statusTimeout)
        self.statusTimeout = None

    def setStatusMessage(self, msg: SessionStatusMessage | None, autoHideMs: float | None = None) -> None:
        self.clearStatusTimeout()
        self.statusMessage = msg
        if not msg or not autoHideMs:
            return

        def hide() -> None:
            self.statusMessage = None
            self.statusTimeout = None
            self.requestRender()

        self.statusTimeout = setTimeout(hide, autoHideMs)

    def invalidate(self) -> None:
        pass

    def render(self, width: int) -> list[str]:
        title = "Resume Session (Current Folder)" if self.scope == "current" else "Resume Session (All)"
        leftText = theme.bold(title)

        sortLabel = "Threaded" if self.sortMode == "threaded" else "Recent" if self.sortMode == "recent" else "Fuzzy"
        sortText = theme.fg("muted", "Sort: ") + theme.fg("accent", sortLabel)

        nameLabel = "All" if self.nameFilter == "all" else "Named"
        nameText = theme.fg("muted", "Name: ") + theme.fg("accent", nameLabel)

        scopeText: str
        if self.loading:
            progressText = (
                f"{self.loadProgress['loaded']}/{self.loadProgress['total']}" if self.loadProgress else "..."
            )
            scopeText = f"{theme.fg('muted', '○ Current Folder | ')}{theme.fg('accent', f'Loading {progressText}')}"
        elif self.scope == "current":
            scopeText = f"{theme.fg('accent', '◉ Current Folder')}{theme.fg('muted', ' | ○ All')}"
        else:
            scopeText = f"{theme.fg('muted', '○ Current Folder | ')}{theme.fg('accent', '◉ All')}"

        rightText = truncateToWidth(f"{scopeText}  {nameText}  {sortText}", width, "")
        availableLeft = max(0, width - visibleWidth(rightText) - 1)
        left = truncateToWidth(leftText, availableLeft, "")
        spacing = max(0, width - visibleWidth(left) - visibleWidth(rightText))

        # Build hint lines - changes based on state (all branches truncate to width)
        hintLine1: str
        hintLine2: str
        if self.confirmingDeletePath is not None:
            confirmHint = (
                f"Delete session? {keyHint('tui.select.confirm', 'confirm')} · {keyHint('tui.select.cancel', 'cancel')}"
            )
            hintLine1 = theme.fg("error", truncateToWidth(confirmHint, width, "…"))
            hintLine2 = ""
        elif self.statusMessage:
            color: Literal["error", "accent"] = "error" if self.statusMessage["type"] == "error" else "accent"
            hintLine1 = theme.fg(color, truncateToWidth(self.statusMessage["message"], width, "…"))
            hintLine2 = ""
        else:
            pathState = "(on)" if self.showPath else "(off)"
            sep = theme.fg("muted", " · ")
            hint1 = keyHint("tui.input.tab", "scope") + sep + theme.fg("muted", 're:<pattern> regex · "phrase" exact')
            hint2Parts = [
                keyHint("app.session.toggleSort", "sort"),
                keyHint("app.session.toggleNamedFilter", "named"),
                keyHint("app.session.delete", "delete"),
                keyHint("app.session.togglePath", f"path {pathState}"),
            ]
            if self.showRenameHint:
                hint2Parts.append(keyHint("app.session.rename", "rename"))
            hint2 = sep.join(hint2Parts)
            hintLine1 = truncateToWidth(hint1, width, "…")
            hintLine2 = truncateToWidth(hint2, width, "…")

        return [f"{left}{' ' * spacing}{rightText}", hintLine1, hintLine2]


@dataclass(slots=True)
class SessionTreeNode:
    """A session tree node for hierarchical display"""

    session: SessionInfo
    children: list[SessionTreeNode]
    latestActivity: float


@dataclass(slots=True)
class FlatSessionNode:
    """Flattened node for display with tree structure info"""

    session: SessionInfo
    depth: int
    isLast: bool
    # For each ancestor level, whether there are more siblings after it
    ancestorContinues: list[bool]


def buildSessionTree(sessions: list[SessionInfo]) -> list[SessionTreeNode]:
    """Build a tree structure from sessions based on parentSessionPath.
    Returns root nodes sorted by modified date (descending)."""
    byPath: dict[str, SessionTreeNode] = {}

    for session in sessions:
        sessionPath = canonicalizePath(session.path) or session.path
        byPath[sessionPath] = SessionTreeNode(
            session=session, children=[], latestActivity=session.modified.timestamp() * 1000
        )

    roots: list[SessionTreeNode] = []

    for session in sessions:
        sessionPath = canonicalizePath(session.path) or session.path
        node = byPath[sessionPath]
        parentPath = canonicalizePath(session.parentSessionPath)

        if parentPath and parentPath in byPath:
            byPath[parentPath].children.append(node)
        else:
            roots.append(node)

    def updateLatestActivity(node: SessionTreeNode) -> float:
        latestActivity = node.session.modified.timestamp() * 1000
        for child in node.children:
            latestActivity = max(latestActivity, updateLatestActivity(child))
        node.latestActivity = latestActivity
        return latestActivity

    for root in roots:
        updateLatestActivity(root)

    # Sort children and roots by latest activity in each subtree (descending)
    def sortNodes(nodes: list[SessionTreeNode]) -> None:
        nodes.sort(key=lambda node: node.latestActivity, reverse=True)
        for node in nodes:
            sortNodes(node.children)

    sortNodes(roots)

    return roots


def flattenSessionTree(roots: list[SessionTreeNode]) -> list[FlatSessionNode]:
    """Flatten tree into display list with tree structure metadata."""
    result: list[FlatSessionNode] = []

    def walk(node: SessionTreeNode, depth: int, ancestorContinues: list[bool], isLast: bool) -> None:
        result.append(FlatSessionNode(session=node.session, depth=depth, isLast=isLast, ancestorContinues=ancestorContinues))

        for i in range(len(node.children)):
            childIsLast = i == len(node.children) - 1
            # Only show continuation line for non-root ancestors
            continues = not isLast if depth > 0 else False
            walk(node.children[i], depth + 1, [*ancestorContinues, continues], childIsLast)

    for i in range(len(roots)):
        walk(roots[i], 0, [], i == len(roots) - 1)

    return result


_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class SessionList:
    """Custom session list component with multi-line items and search"""

    def getSelectedSessionPath(self) -> str | None:
        selected = jsIndex(self.filteredSessions, self.selectedIndex)
        return selected.session.path if selected else None

    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        self.searchInput.focused = value

    def __init__(
        self,
        sessions: list[SessionInfo],
        showCwd: bool,
        sortMode: SortMode,
        nameFilter: NameFilter,
        keybindings: KeybindingsManager,
        currentSessionFilePath: str | None = None,
    ) -> None:
        self.selectedIndex: int = 0
        self.selectionTouched = False
        self.showPath = False
        self.confirmingDeletePath: str | None = None
        self.onSelect: Callable[[str], None] | None = None
        self.onCancel: Callable[[], None] | None = None
        self.onExit: Callable[[], None] = lambda: None
        self.onToggleScope: Callable[[], None] | None = None
        self.onToggleSort: Callable[[], None] | None = None
        self.onToggleNameFilter: Callable[[], None] | None = None
        self.onTogglePath: Callable[[bool], None] | None = None
        self.onDeleteConfirmationChange: Callable[[str | None], None] | None = None
        self.onDeleteSession: Callable[[str], Coroutine[Any, Any, None]] | None = None
        self.onRenameSession: Callable[[str], None] | None = None
        self.onError: Callable[[str], None] | None = None
        self.maxVisible: int = 10  # Max sessions visible (one line each)
        # MISAKA fork: holds the running `void this.onDeleteSession(...)` (asyncio holds tasks weakly).
        self._tasks: set[asyncio.Task[Any]] = set()

        # Focusable implementation - propagate to searchInput for IME cursor positioning
        self._focused = False

        self.allSessions: list[SessionInfo] = sessions
        self.filteredSessions: list[FlatSessionNode] = []
        self.searchInput = Input()
        self.showCwd = showCwd
        self.sortMode: SortMode = sortMode
        self.nameFilter: NameFilter = nameFilter
        self.keybindings = keybindings
        self.currentSessionCanonicalPath = canonicalizePath(currentSessionFilePath)
        self.filterSessions("")

        # Handle Enter in search input - select current item
        def onSubmit(_value: str) -> None:
            selected = jsIndex(self.filteredSessions, self.selectedIndex)
            if selected and self.onSelect:
                self.onSelect(selected.session.path)

        self.searchInput.onSubmit = onSubmit

    def setSortMode(self, sortMode: SortMode) -> None:
        self.sortMode = sortMode
        self.filterSessions(self.searchInput.getValue())

    def setNameFilter(self, nameFilter: NameFilter) -> None:
        self.nameFilter = nameFilter
        self.filterSessions(self.searchInput.getValue())

    def setSessions(self, sessions: list[SessionInfo], showCwd: bool) -> None:
        selectedPath = self.getSelectedSessionPath() if self.selectionTouched else None
        self.allSessions = sessions
        self.showCwd = showCwd
        self.filterSessions(self.searchInput.getValue())
        if not self.selectionTouched:
            self.selectedIndex = 0
        elif selectedPath:
            selectedIndex = next(
                (index for index, node in enumerate(self.filteredSessions) if node.session.path == selectedPath), -1
            )
            if selectedIndex >= 0:
                self.selectedIndex = selectedIndex

    def filterSessions(self, query: str) -> None:
        trimmed = jsTrim(query)
        nameFiltered = (
            self.allSessions
            if self.nameFilter == "all"
            else [session for session in self.allSessions if hasSessionName(session)]
        )

        if self.sortMode == "threaded" and not trimmed:
            # Threaded mode without search: show tree structure
            roots = buildSessionTree(nameFiltered)
            self.filteredSessions = flattenSessionTree(roots)
        else:
            # Other modes or with search: flat list
            filtered = filterAndSortSessions(nameFiltered, query, self.sortMode, "all")
            self.filteredSessions = [
                FlatSessionNode(session=session, depth=0, isLast=True, ancestorContinues=[]) for session in filtered
            ]
        self.selectedIndex = min(self.selectedIndex, max(0, len(self.filteredSessions) - 1))

    def setConfirmingDeletePath(self, path: str | None) -> None:
        self.confirmingDeletePath = path
        if self.onDeleteConfirmationChange:
            self.onDeleteConfirmationChange(path)

    def startDeleteConfirmationForSelectedSession(self) -> None:
        selected = jsIndex(self.filteredSessions, self.selectedIndex)
        if not selected:
            return

        # Prevent deleting current session
        if self.isCurrentSessionPath(selected.session.path):
            if self.onError:
                self.onError("Cannot delete the currently active session")
            return

        self.setConfirmingDeletePath(selected.session.path)

    def isCurrentSessionPath(self, path: str) -> bool:
        if not self.currentSessionCanonicalPath:
            return False
        return (canonicalizePath(path) or path) == self.currentSessionCanonicalPath

    def invalidate(self) -> None:
        pass

    def render(self, width: int) -> list[str]:
        lines: list[str] = []

        # Render search input
        lines.extend(self.searchInput.render(width))
        lines.append("")  # Blank line after search

        if len(self.filteredSessions) == 0:
            emptyMessage: str
            if self.nameFilter == "named":
                toggleKey = keyText("app.session.toggleNamedFilter")
                if self.showCwd:
                    emptyMessage = f"  No named sessions found. Press {toggleKey} to show all."
                else:
                    emptyMessage = (
                        f"  No named sessions in current folder. Press {toggleKey} to show all, or Tab to view all."
                    )
            elif self.showCwd:
                # "All" scope - no sessions anywhere that match filter
                emptyMessage = "  No sessions found"
            else:
                # "Current folder" scope - hint to try "all"
                emptyMessage = "  No sessions in current folder. Press Tab to view all."
            lines.append(theme.fg("muted", truncateToWidth(emptyMessage, width, "…")))
            return lines

        # Calculate visible range with scrolling
        startIndex = max(
            0,
            min(
                self.selectedIndex - math.floor(self.maxVisible / 2),
                len(self.filteredSessions) - self.maxVisible,
            ),
        )
        endIndex = min(startIndex + self.maxVisible, len(self.filteredSessions))

        # Render visible sessions (one line each with tree structure)
        for i in range(startIndex, endIndex):
            node = self.filteredSessions[i]
            session = node.session
            isSelected = i == self.selectedIndex
            isConfirmingDelete = session.path == self.confirmingDeletePath
            isCurrent = self.isCurrentSessionPath(session.path)

            # Build tree prefix
            prefix = self.buildTreePrefix(node)

            # Session display text (name or first message)
            hasName = bool(session.name)
            displayText = session.name if session.name is not None else session.firstMessage
            normalizedMessage = jsTrim(_CONTROL_CHARACTERS.sub(" ", displayText))

            # Right side: message count and age
            age = formatSessionDate(session.modified)
            msgCount = str(session.messageCount)
            rightPart = f"{msgCount} {age}"
            if self.showCwd and session.cwd:
                rightPart = f"{shortenPath(session.cwd)} {rightPart}"
            if self.showPath:
                rightPart = f"{shortenPath(session.path)} {rightPart}"

            # Cursor
            cursor = theme.fg("accent", "› ") if isSelected else "  "

            # Calculate available width for message
            prefixWidth = visibleWidth(prefix)
            rightWidth = visibleWidth(rightPart) + 2  # +2 for spacing
            availableForMsg = width - 2 - prefixWidth - rightWidth  # -2 for cursor

            truncatedMsg = truncateToWidth(normalizedMessage, max(10, availableForMsg), "…")

            # Style message
            messageColor: Literal["error", "warning", "accent"] | None = None
            if isConfirmingDelete:
                messageColor = "error"
            elif isCurrent:
                messageColor = "accent"
            elif hasName:
                messageColor = "warning"
            styledMsg = theme.fg(messageColor, truncatedMsg) if messageColor else truncatedMsg
            if isSelected:
                styledMsg = theme.bold(styledMsg)

            # Build line
            leftPart = cursor + theme.fg("dim", prefix) + styledMsg
            leftWidth = visibleWidth(leftPart)
            spacing = max(1, width - leftWidth - visibleWidth(rightPart))
            styledRight = theme.fg("error" if isConfirmingDelete else "dim", rightPart)

            line = leftPart + " " * spacing + styledRight
            if isSelected:
                line = theme.bg("selectedBg", line)
            lines.append(truncateToWidth(line, width))

        # Add scroll indicator if needed
        if startIndex > 0 or endIndex < len(self.filteredSessions):
            scrollText = f"  ({self.selectedIndex + 1}/{len(self.filteredSessions)})"
            scrollInfo = theme.fg("muted", truncateToWidth(scrollText, width, ""))
            lines.append(scrollInfo)

        return lines

    def buildTreePrefix(self, node: FlatSessionNode) -> str:
        if node.depth == 0:
            return ""

        parts = ["│  " if continues else "   " for continues in node.ancestorContinues]
        branch = "└─ " if node.isLast else "├─ "
        return "".join(parts) + branch

    def handleInput(self, keyData: str) -> None:
        kb = getKeybindings()

        # Handle delete confirmation state first - intercept all keys
        if self.confirmingDeletePath is not None:
            if kb.matches(keyData, "tui.select.confirm"):
                pathToDelete = self.confirmingDeletePath
                self.setConfirmingDeletePath(None)
                if self.onDeleteSession:
                    spawn(self.onDeleteSession(pathToDelete), self._tasks)
                return
            if kb.matches(keyData, "tui.select.cancel"):
                self.setConfirmingDeletePath(None)
                return
            # Ignore all other keys while confirming
            return

        if kb.matches(keyData, "tui.input.tab"):
            if self.onToggleScope:
                self.onToggleScope()
            return

        if kb.matches(keyData, "app.session.toggleSort"):
            if self.onToggleSort:
                self.onToggleSort()
            return

        if self.keybindings.matches(keyData, "app.session.toggleNamedFilter"):
            if self.onToggleNameFilter:
                self.onToggleNameFilter()
            return

        # Ctrl+P: toggle path display
        if kb.matches(keyData, "app.session.togglePath"):
            self.showPath = not self.showPath
            if self.onTogglePath:
                self.onTogglePath(self.showPath)
            return

        # Ctrl+D: initiate delete confirmation (useful on terminals that don't distinguish Ctrl+Backspace from
        # Backspace)
        if kb.matches(keyData, "app.session.delete"):
            self.startDeleteConfirmationForSelectedSession()
            return

        # Rename selected session
        if kb.matches(keyData, "app.session.rename"):
            selected = jsIndex(self.filteredSessions, self.selectedIndex)
            if selected and self.onRenameSession:
                # MISAKA: Hermes offers no Rename on a bot's canonical Bot Chat (session-actions-menu).
                info = selected.session
                role = session_paths.canonical_contact(info.path, info.name, info.parentSessionPath)
                if role is not None:
                    if self.onError:
                        self.onError(session_paths.contact_rename_refusal(role))
                    return
                self.onRenameSession(selected.session.path)
            return

        # Ctrl+Backspace: non-invasive convenience alias for delete
        # Only triggers deletion when the query is empty; otherwise it is forwarded to the input
        if kb.matches(keyData, "app.session.deleteNoninvasive"):
            if len(self.searchInput.getValue()) > 0:
                self.searchInput.handleInput(keyData)
                self.filterSessions(self.searchInput.getValue())
                return

            self.startDeleteConfirmationForSelectedSession()
            return

        self.selectionTouched = True
        # Up arrow
        if kb.matches(keyData, "tui.select.up"):
            self.selectedIndex = max(0, self.selectedIndex - 1)
        # Down arrow
        elif kb.matches(keyData, "tui.select.down"):
            self.selectedIndex = min(len(self.filteredSessions) - 1, self.selectedIndex + 1)
        # Page up - jump up by maxVisible items
        elif kb.matches(keyData, "tui.select.pageUp"):
            self.selectedIndex = max(0, self.selectedIndex - self.maxVisible)
        # Page down - jump down by maxVisible items
        elif kb.matches(keyData, "tui.select.pageDown"):
            self.selectedIndex = min(len(self.filteredSessions) - 1, self.selectedIndex + self.maxVisible)
        # Enter
        elif kb.matches(keyData, "tui.select.confirm"):
            selected = jsIndex(self.filteredSessions, self.selectedIndex)
            if selected and self.onSelect:
                self.onSelect(selected.session.path)
        # Escape - cancel
        elif kb.matches(keyData, "tui.select.cancel"):
            if self.onCancel:
                self.onCancel()
        # Pass everything else to search input
        else:
            self.searchInput.handleInput(keyData)
            self.filterSessions(self.searchInput.getValue())


type SessionsLoader = Callable[[SessionListProgress | None, Any], Awaitable[list[SessionInfo]]]


class DeleteSessionResult(TypedDict, total=False):
    ok: bool
    method: Literal["trash", "unlink"]
    error: str


async def deleteSessionFile(sessionPath: str) -> DeleteSessionResult:
    """Delete a session file, trying the `trash` CLI first, then falling back to unlink"""
    # Try `trash` first (if installed)
    trashArgs = ["--", sessionPath] if sessionPath.startswith("-") else [sessionPath]
    # MISAKA fork: `spawnSync`'s result fields (`status`, `stderr`, `error`) as three variables.
    trashError: OSError | None = None
    trashStatus: int | None = None
    trashStderr: str | None = None
    try:
        trashResult = subprocess.run(  # noqa: ASYNC221 - upstream spawnSync
            ["trash", *trashArgs], check=False, capture_output=True, encoding="utf-8", errors="replace"
        )
        trashStatus = trashResult.returncode
        trashStderr = trashResult.stderr
    except OSError as spawnError:
        trashError = spawnError

    def getTrashErrorHint() -> str | None:
        parts: list[str] = []
        if trashError:
            parts.append(str(trashError))
        stderr = jsTrim(trashStderr) if trashStderr is not None else None
        if stderr:
            parts.append(stderr.split("\n")[0])
        if len(parts) == 0:
            return None
        return f"trash: {jsSlice(' · '.join(parts), 0, 200)}"

    # If trash reports success, or the file is gone afterwards, treat it as successful
    if trashStatus == 0 or not os.path.exists(sessionPath):
        return {"ok": True, "method": "trash"}

    # Fallback to permanent deletion
    try:
        os.unlink(sessionPath)
        return {"ok": True, "method": "unlink"}
    except OSError as err:
        unlinkError = errorMessage(err)
        trashErrorHint = getTrashErrorHint()
        error = f"{unlinkError} ({trashErrorHint})" if trashErrorHint else unlinkError
        return {"ok": False, "method": "unlink", "error": error}


class SessionSelectorOptions(TypedDict, total=False):
    renameSession: Callable[[str, str | None], Awaitable[None]]
    showRenameHint: bool
    keybindings: KeybindingsManager


class SessionSelectorComponent(Container):
    """Component that renders a session selector"""

    def handleInput(self, data: str) -> None:
        if self.mode == "rename":
            kb = getKeybindings()
            if kb.matches(data, "tui.select.cancel"):
                self.exitRenameMode()
                return
            self.renameInput.handleInput(data)
            return

        self.sessionList.handleInput(data)

    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        self.sessionList.focused = value
        self.renameInput.focused = value
        if value and self.mode == "rename":
            self.renameInput.focused = True

    def buildBaseLayout(self, content: Any, options: dict[str, bool] | None = None) -> None:
        self.clear()
        self.addChild(Spacer(1))
        self.addChild(DynamicBorder(lambda s: theme.fg("accent", s)))
        self.addChild(Spacer(1))
        showHeader = options.get("showHeader") if options else None
        if showHeader if showHeader is not None else True:
            self.addChild(self.header)
            self.addChild(Spacer(1))
        self.addChild(content)
        self.addChild(Spacer(1))
        self.addChild(DynamicBorder(lambda s: theme.fg("accent", s)))

    def __init__(
        self,
        currentSessionsLoader: SessionsLoader,
        allSessionsLoader: SessionsLoader,
        onSelect: Callable[[str], Any],
        onCancel: Callable[[], None],
        onExit: Callable[[], None],
        requestRender: Callable[[], None],
        options: SessionSelectorOptions | None = None,
        currentSessionFilePath: str | None = None,
    ) -> None:
        super().__init__()
        self.canRename = True
        self.scope: SessionScope = "current"
        self.sortMode: SortMode = "threaded"
        self.nameFilter: NameFilter = "all"
        self.currentSessions: list[SessionInfo] | None = None
        self.allSessions: list[SessionInfo] | None = None
        self.currentLoad: AbortController | None = None
        self.allLoad: AbortController | None = None

        self.mode: Literal["list", "rename"] = "list"
        self.renameInput = Input()
        self.renameTargetPath: str | None = None

        # Focusable implementation - propagate to sessionList for IME cursor positioning
        self._focused = False
        # MISAKA fork: holds the running `void this.loadScope(...)`/`void this.confirmRename(...)` (asyncio holds
        # tasks weakly).
        self._tasks: set[asyncio.Task[Any]] = set()

        keybindings = options.get("keybindings") if options else None
        self.keybindings = keybindings if keybindings is not None else KeybindingsManager.create()
        self.currentSessionsLoader = currentSessionsLoader
        self.allSessionsLoader = allSessionsLoader
        self.requestRender = requestRender
        self.header = SessionSelectorHeader(self.scope, self.sortMode, self.nameFilter, self.requestRender)
        renameSession = options.get("renameSession") if options else None
        self.renameSession = renameSession
        self.canRename = bool(renameSession)
        showRenameHint = options.get("showRenameHint") if options else None
        self.header.setShowRenameHint(showRenameHint if showRenameHint is not None else self.canRename)

        # Create session list (starts empty, will be populated after load)
        self.sessionList = SessionList(
            [],
            False,
            self.sortMode,
            self.nameFilter,
            self.keybindings,
            currentSessionFilePath,
        )

        self.buildBaseLayout(self.sessionList)

        def onRenameSubmit(value: str) -> None:
            spawn(self.confirmRename(value), self._tasks)

        self.renameInput.onSubmit = onRenameSubmit

        # Ensure header status timeouts are cleared when leaving the selector
        def clearStatusMessage() -> None:
            self.header.setStatusMessage(None)

        def listSelect(sessionPath: str) -> None:
            clearStatusMessage()
            self.cancelLoads()
            onSelect(sessionPath)

        def listCancel() -> None:
            clearStatusMessage()
            self.cancelLoads()
            onCancel()

        def listExit() -> None:
            clearStatusMessage()
            self.cancelLoads()
            onExit()

        self.sessionList.onSelect = listSelect
        self.sessionList.onCancel = listCancel
        self.sessionList.onExit = listExit
        self.sessionList.onToggleScope = lambda: self.toggleScope()
        self.sessionList.onToggleSort = lambda: self.toggleSortMode()
        self.sessionList.onToggleNameFilter = lambda: self.toggleNameFilter()

        def onRenameSession(sessionPath: str) -> None:
            if not renameSession:
                return
            if self.currentLoad if self.scope == "current" else self.allLoad:
                return

            sessions = (
                (self.allSessions if self.allSessions is not None else [])
                if self.scope == "all"
                else (self.currentSessions if self.currentSessions is not None else [])
            )
            session = next((s for s in sessions if s.path == sessionPath), None)
            self.enterRenameMode(sessionPath, session.name if session else None)

        self.sessionList.onRenameSession = onRenameSession

        # Sync list events to header
        def onTogglePath(showPath: bool) -> None:
            self.header.setShowPath(showPath)
            self.requestRender()

        self.sessionList.onTogglePath = onTogglePath

        def onDeleteConfirmationChange(path: str | None) -> None:
            self.header.setConfirmingDeletePath(path)
            self.requestRender()

        self.sessionList.onDeleteConfirmationChange = onDeleteConfirmationChange

        def onError(msg: str) -> None:
            self.header.setStatusMessage({"type": "error", "message": msg}, 3000)
            self.requestRender()

        self.sessionList.onError = onError

        # Handle session deletion
        async def onDeleteSession(sessionPath: str) -> None:
            from misaka.core import session_catalog

            with session_catalog.deletable(sessionPath) as refusal:
                if refusal is not None:
                    self.header.setStatusMessage({"type": "error", "message": refusal}, 3000)
                    self.requestRender()
                    return
                result = await deleteSessionFile(sessionPath)

            if result.get("ok"):
                if self.currentSessions is not None:
                    self.currentSessions = [s for s in self.currentSessions if s.path != sessionPath]
                if self.allSessions is not None:
                    self.allSessions = [s for s in self.allSessions if s.path != sessionPath]

                sessions = (
                    (self.allSessions if self.allSessions is not None else [])
                    if self.scope == "all"
                    else (self.currentSessions if self.currentSessions is not None else [])
                )
                showCwd = self.scope == "all"
                self.sessionList.setSessions(sessions, showCwd)

                msg = "Session moved to trash" if result.get("method") == "trash" else "Session deleted"
                self.header.setStatusMessage({"type": "info", "message": msg}, 2000)
                await self.refreshSessionsAfterMutation()
            else:
                errorMessage = result.get("error") or "Unknown error"
                self.header.setStatusMessage({"type": "error", "message": f"Failed to delete: {errorMessage}"}, 3000)

            self.requestRender()

        self.sessionList.onDeleteSession = onDeleteSession

        # Start loading current sessions immediately
        spawn(self.loadScope("current"), self._tasks)

    def cancelLoads(self) -> None:
        if self.currentLoad:
            self.currentLoad.abort()
            self.currentLoad = None
            self.currentSessions = None
        if self.allLoad:
            self.allLoad.abort()
            self.allLoad = None
            self.allSessions = None

    def enterRenameMode(self, sessionPath: str, currentName: str | None) -> None:
        self.mode = "rename"
        self.renameTargetPath = sessionPath
        self.renameInput.setValue(currentName if currentName is not None else "")
        self.renameInput.focused = True

        panel = Container()
        panel.addChild(Text(theme.bold("Rename Session"), 1, 0))
        panel.addChild(Spacer(1))
        panel.addChild(self.renameInput)
        panel.addChild(Spacer(1))
        panel.addChild(
            Text(
                theme.fg("muted", f"{keyText('tui.select.confirm')} to save · {keyText('tui.select.cancel')} to cancel"),
                1,
                0,
            )
        )

        self.buildBaseLayout(panel, {"showHeader": False})
        self.requestRender()

    def exitRenameMode(self) -> None:
        self.mode = "list"
        self.renameTargetPath = None

        self.buildBaseLayout(self.sessionList)

        self.requestRender()

    async def confirmRename(self, value: str) -> None:
        next = jsTrim(value)
        if not next:
            return
        target = self.renameTargetPath
        if not target:
            self.exitRenameMode()
            return

        # Find current name for callback
        renameSession = self.renameSession
        if not renameSession:
            self.exitRenameMode()
            return

        try:
            await renameSession(target, next)
            await self.refreshSessionsAfterMutation()
        except ValueError as refusal:   # MISAKA: a role's canonical Bot Chat keeps its name
            self.header.setStatusMessage({"type": "error", "message": str(refusal)}, 3000)
        finally:
            self.exitRenameMode()

    async def loadScope(self, scope: SessionScope) -> None:
        if self.currentLoad if scope == "current" else self.allLoad:
            return

        showCwd = scope == "all"
        controller = AbortController()
        if scope == "current":
            self.currentLoad = controller
        else:
            self.allLoad = controller
        self.header.setScope(scope)
        self.header.setLoading(True)
        self.requestRender()

        def isActive() -> bool:
            return (self.currentLoad if scope == "current" else self.allLoad) is controller

        def onProgress(loaded: int, total: int, partialSessions: Any = None) -> None:
            if not isActive():
                return
            if partialSessions is not None:
                sessions = [*partialSessions]
                if scope == "current":
                    self.currentSessions = sessions
                else:
                    self.allSessions = sessions
                if scope == self.scope:
                    self.sessionList.setSessions(sessions, showCwd)
            if scope != self.scope:
                return
            self.header.setProgress(loaded, total)
            self.requestRender()

        try:
            sessions = await (
                self.currentSessionsLoader(onProgress, controller.signal)
                if scope == "current"
                else self.allSessionsLoader(onProgress, controller.signal)
            )
            if not isActive():
                return

            if scope == "current":
                self.currentSessions = sessions
                self.currentLoad = None
            else:
                self.allSessions = sessions
                self.allLoad = None

            if scope != self.scope:
                return
            self.header.setLoading(False)
            self.sessionList.setSessions(sessions, showCwd)
            self.requestRender()
        except Exception as err:  # noqa: BLE001 - upstream `catch (err)` shows any load failure in the picker
            if not isActive():
                return
            if scope == "current":
                self.currentLoad = None
                self.currentSessions = None
            else:
                self.allLoad = None
                self.allSessions = None
            if scope != self.scope:
                return

            message = errorMessage(err)
            self.header.setLoading(False)
            self.header.setStatusMessage({"type": "error", "message": f"Failed to load sessions: {message}"}, 4000)
            self.sessionList.setSessions([], showCwd)
            self.requestRender()

    def toggleSortMode(self) -> None:
        # Cycle: threaded -> recent -> relevance -> threaded
        self.sortMode = (
            "recent" if self.sortMode == "threaded" else "relevance" if self.sortMode == "recent" else "threaded"
        )
        self.header.setSortMode(self.sortMode)
        self.sessionList.setSortMode(self.sortMode)
        self.requestRender()

    def toggleNameFilter(self) -> None:
        self.nameFilter = "named" if self.nameFilter == "all" else "all"
        self.header.setNameFilter(self.nameFilter)
        self.sessionList.setNameFilter(self.nameFilter)
        self.requestRender()

    async def refreshSessionsAfterMutation(self) -> None:
        self.cancelLoads()
        self.currentSessions = None
        self.allSessions = None
        await self.loadScope(self.scope)

    def toggleScope(self) -> None:
        self.scope = "all" if self.scope == "current" else "current"
        sessions = self.currentSessions if self.scope == "current" else self.allSessions
        loading = (self.currentLoad if self.scope == "current" else self.allLoad) is not None
        self.header.setScope(self.scope)
        self.header.setLoading(loading)
        self.sessionList.setSessions(sessions if sessions is not None else [], self.scope == "all")
        self.requestRender()
        if sessions is None and not loading:
            spawn(self.loadScope(self.scope), self._tasks)

    def getSessionList(self) -> SessionList:
        return self.sessionList


__all__ = [
    "SessionSelectorComponent",
    "SessionSelectorOptions",
    "SessionsLoader",
]

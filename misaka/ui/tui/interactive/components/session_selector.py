"""Interactive session selector with threaded and searchable session views."""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from misaka.core.keybindings import KeybindingsManager
from misaka.core.session_manager import SessionInfo, SessionListProgress
from misaka.ui.tui import (
    Component,
    Container,
    Focusable,
    Input,
    Spacer,
    Text,
    getKeybindings,
    truncateToWidth,
    visibleWidth,
)
from misaka.ui.tui.interactive.theme.theme import theme
from misaka.utils.paths import canonicalize_path

from .dynamic_border import DynamicBorder
from .keybinding_hints import key_hint, key_text
from .session_selector_search import (
    NameFilter,
    SortMode,
    filter_and_sort_sessions,
    has_session_name,
)

type SessionScope = Literal["current", "all"]
type SessionsLoader = Callable[[SessionListProgress | None], Awaitable[list[SessionInfo]]]


def _get_running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _invoke_callback(callback_result: object) -> None:
    if not inspect.isawaitable(callback_result):
        return
    loop = _get_running_loop()
    if loop is None:
        asyncio.run(callback_result)
    else:
        loop.create_task(callback_result)


def _run_or_schedule(awaitable: Awaitable[object]) -> None:
    loop = _get_running_loop()
    if loop is None:
        asyncio.run(awaitable)
    else:
        loop.create_task(awaitable)


def shorten_path(path: str) -> str:
    home_dir = str(Path.home())
    if not path:
        return path
    if path.startswith(home_dir):
        return f"~{path[len(home_dir):]}"
    return path


def format_session_date(date: datetime) -> str:
    diff_seconds = max(0, int((datetime.now(date.tzinfo) - date).total_seconds()))
    diff_minutes = diff_seconds // 60
    diff_hours = diff_seconds // 3600
    diff_days = diff_seconds // 86_400

    if diff_minutes < 1:
        return "now"
    if diff_minutes < 60:
        return f"{diff_minutes}m"
    if diff_hours < 24:
        return f"{diff_hours}h"
    if diff_days < 7:
        return f"{diff_days}d"
    if diff_days < 30:
        return f"{diff_days // 7}w"
    if diff_days < 365:
        return f"{diff_days // 30}mo"
    return f"{diff_days // 365}y"


def canonicalize_optional_path(path: str | None) -> str | None:
    return canonicalize_path(path) if path else path


@dataclass(slots=True)
class SessionTreeNode:
    session: SessionInfo
    children: list[SessionTreeNode]


@dataclass(slots=True)
class FlatSessionNode:
    session: SessionInfo
    depth: int
    isLast: bool
    ancestorContinues: list[bool]


def build_session_tree(sessions: list[SessionInfo]) -> list[SessionTreeNode]:
    by_path: dict[str, SessionTreeNode] = {}
    for session in sessions:
        session_path = canonicalize_path(session.path)
        by_path[session_path] = SessionTreeNode(session=session, children=[])

    roots: list[SessionTreeNode] = []
    for session in sessions:
        session_path = canonicalize_path(session.path)
        node = by_path[session_path]
        parent_path = canonicalize_optional_path(session.parentSessionPath)
        if parent_path and parent_path in by_path:
            by_path[parent_path].children.append(node)
        else:
            roots.append(node)

    def sort_nodes(nodes: list[SessionTreeNode]) -> None:
        nodes.sort(key=lambda entry: entry.session.modified, reverse=True)
        for node in nodes:
            sort_nodes(node.children)

    sort_nodes(roots)
    return roots


def flatten_session_tree(roots: list[SessionTreeNode]) -> list[FlatSessionNode]:
    flattened: list[FlatSessionNode] = []

    def walk(node: SessionTreeNode, depth: int, ancestor_continues: list[bool], is_last: bool) -> None:
        flattened.append(
            FlatSessionNode(
                session=node.session,
                depth=depth,
                isLast=is_last,
                ancestorContinues=ancestor_continues,
            )
        )
        for index, child in enumerate(node.children):
            child_is_last = index == len(node.children) - 1
            continues = [*ancestor_continues, not is_last] if depth > 0 else ancestor_continues
            walk(child, depth + 1, continues, child_is_last)

    for index, root in enumerate(roots):
        walk(root, 0, [], index == len(roots) - 1)
    return flattened


class SessionSelectorHeader(Component):
    def __init__(
        self,
        scope: SessionScope,
        sortMode: SortMode,
        nameFilter: NameFilter,
        requestRender: Callable[[], None],
    ) -> None:
        self.scope = scope
        self.sortMode = sortMode
        self.nameFilter = nameFilter
        self.requestRender = requestRender
        self.loading = False
        self.loadProgress: tuple[int, int] | None = None
        self.showPath = False
        self.confirmingDeletePath: str | None = None
        self.statusMessage: tuple[Literal["info", "error"], str] | None = None
        self.showRenameHint = False
        self._status_timer_handle: asyncio.TimerHandle | None = None

    def setScope(self, scope: SessionScope) -> None:
        self.scope = scope

    def setSortMode(self, sortMode: SortMode) -> None:
        self.sortMode = sortMode

    def setNameFilter(self, nameFilter: NameFilter) -> None:
        self.nameFilter = nameFilter

    def setLoading(self, loading: bool) -> None:
        self.loading = loading
        self.loadProgress = None

    def setProgress(self, loaded: int, total: int) -> None:
        self.loadProgress = (loaded, total)

    def setShowPath(self, showPath: bool) -> None:
        self.showPath = showPath

    def setShowRenameHint(self, show: bool) -> None:
        self.showRenameHint = show

    def setConfirmingDeletePath(self, path: str | None) -> None:
        self.confirmingDeletePath = path

    def _clear_status_timeout(self) -> None:
        if self._status_timer_handle is not None:
            self._status_timer_handle.cancel()
            self._status_timer_handle = None

    def setStatusMessage(
        self,
        message: tuple[Literal["info", "error"], str] | None,
        autoHideMs: int | None = None,
    ) -> None:
        self._clear_status_timeout()
        self.statusMessage = message
        if not message or not autoHideMs:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        def clear_status() -> None:
            self.statusMessage = None
            self._status_timer_handle = None
            self.requestRender()

        self._status_timer_handle = loop.call_later(autoHideMs / 1000, clear_status)

    def invalidate(self) -> None:
        return None

    def handleInput(self, data: str) -> None:
        del data

    def render(self, width: int) -> list[str]:
        title = "Resume Session (Current Folder)" if self.scope == "current" else "Resume Session (All)"
        left = theme.bold(title)

        sort_label = "Threaded" if self.sortMode == "threaded" else "Recent" if self.sortMode == "recent" else "Fuzzy"
        name_label = "All" if self.nameFilter == "all" else "Named"
        if self.loading:
            progress_text = (
                f"{self.loadProgress[0]}/{self.loadProgress[1]}"
                if self.loadProgress is not None
                else "..."
            )
            scope_text = f"{theme.fg('muted', '○ Current Folder | ')}{theme.fg('accent', f'Loading {progress_text}')}"
        elif self.scope == "current":
            scope_text = f"{theme.fg('accent', '◉ Current Folder')}{theme.fg('muted', ' | ○ All')}"
        else:
            scope_text = f"{theme.fg('muted', '○ Current Folder | ')}{theme.fg('accent', '◉ All')}"

        right = truncateToWidth(
            f"{scope_text}  {theme.fg('muted', 'Name: ')}{theme.fg('accent', name_label)}  "
            f"{theme.fg('muted', 'Sort: ')}{theme.fg('accent', sort_label)}",
            width,
            "",
        )
        available_left = max(0, width - visibleWidth(right) - 1)
        left_text = truncateToWidth(left, available_left, "")
        spacing = max(0, width - visibleWidth(left_text) - visibleWidth(right))

        if self.confirmingDeletePath is not None:
            hint_line_1 = theme.fg(
                "error",
                truncateToWidth(
                    f"Delete session? {key_hint('tui.select.confirm', 'confirm')} · "
                    f"{key_hint('tui.select.cancel', 'cancel')}",
                    width,
                    "…",
                ),
            )
            hint_line_2 = ""
        elif self.statusMessage is not None:
            color = "error" if self.statusMessage[0] == "error" else "accent"
            hint_line_1 = theme.fg(color, truncateToWidth(self.statusMessage[1], width, "…"))
            hint_line_2 = ""
        else:
            path_state = "(on)" if self.showPath else "(off)"
            separator = theme.fg("muted", " · ")
            hint_line_1 = truncateToWidth(
                key_hint("tui.input.tab", "scope")
                + separator
                + theme.fg("muted", 're:<pattern> regex · "phrase" exact'),
                width,
                "…",
            )
            hint_parts = [
                key_hint("app.session.toggleSort", "sort"),
                key_hint("app.session.toggleNamedFilter", "named"),
                key_hint("app.session.delete", "delete"),
                key_hint("app.session.togglePath", f"path {path_state}"),
            ]
            if self.showRenameHint:
                hint_parts.append(key_hint("app.session.rename", "rename"))
            hint_line_2 = truncateToWidth(separator.join(hint_parts), width, "…")

        return [f"{left_text}{' ' * spacing}{right}", hint_line_1, hint_line_2]


class SessionList(Component, Focusable):
    def __init__(
        self,
        sessions: list[SessionInfo],
        showCwd: bool,
        sortMode: SortMode,
        nameFilter: NameFilter,
        keybindings: KeybindingsManager,
        currentSessionFilePath: str | None = None,
    ) -> None:
        self.allSessions = list(sessions)
        self.filteredSessions: list[FlatSessionNode] = []
        self.selectedIndex = 0
        self.searchInput = Input()
        self.showCwd = showCwd
        self.sortMode = sortMode
        self.nameFilter = nameFilter
        self.keybindings = keybindings
        self.showPath = False
        self.confirmingDeletePath: str | None = None
        self.currentSessionCanonicalPath = canonicalize_optional_path(currentSessionFilePath)
        self.maxVisible = 10
        self.onSelect: Callable[[str], None] | None = None
        self.onCancel: Callable[[], None] | None = None
        self.onExit: Callable[[], None] = lambda: None
        self.onToggleScope: Callable[[], None] | None = None
        self.onToggleSort: Callable[[], None] | None = None
        self.onToggleNameFilter: Callable[[], None] | None = None
        self.onTogglePath: Callable[[bool], None] | None = None
        self.onDeleteConfirmationChange: Callable[[str | None], None] | None = None
        self.onDeleteSession: Callable[[str], object] | None = None
        self.onRenameSession: Callable[[str], None] | None = None
        self.onError: Callable[[str], None] | None = None
        self._focused = False

        self.searchInput.onSubmit = self._submit_current
        self.filterSessions("")

    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        self.searchInput.focused = value

    def _submit_current(self, value: str) -> None:
        del value
        if not self.filteredSessions:
            return
        selected = self.filteredSessions[self.selectedIndex]
        if self.onSelect is not None:
            self.onSelect(selected.session.path)

    def getSelectedSessionPath(self) -> str | None:
        if not self.filteredSessions:
            return None
        return self.filteredSessions[self.selectedIndex].session.path

    def setSortMode(self, sortMode: SortMode) -> None:
        self.sortMode = sortMode
        self.filterSessions(self.searchInput.getValue())

    def setNameFilter(self, nameFilter: NameFilter) -> None:
        self.nameFilter = nameFilter
        self.filterSessions(self.searchInput.getValue())

    def setSessions(self, sessions: list[SessionInfo], showCwd: bool) -> None:
        self.allSessions = list(sessions)
        self.showCwd = showCwd
        self.filterSessions(self.searchInput.getValue())

    def filterSessions(self, query: str) -> None:
        name_filtered = (
            self.allSessions
            if self.nameFilter == "all"
            else [session for session in self.allSessions if has_session_name(session)]
        )

        if self.sortMode == "threaded" and not query.strip():
            self.filteredSessions = flatten_session_tree(build_session_tree(name_filtered))
        else:
            filtered = filter_and_sort_sessions(name_filtered, query, self.sortMode, "all")
            self.filteredSessions = [
                FlatSessionNode(session=session, depth=0, isLast=True, ancestorContinues=[])
                for session in filtered
            ]
        self.selectedIndex = min(self.selectedIndex, max(0, len(self.filteredSessions) - 1))

    def setConfirmingDeletePath(self, path: str | None) -> None:
        self.confirmingDeletePath = path
        if self.onDeleteConfirmationChange is not None:
            self.onDeleteConfirmationChange(path)

    def isCurrentSessionPath(self, path: str) -> bool:
        if self.currentSessionCanonicalPath is None:
            return False
        return canonicalize_path(path) == self.currentSessionCanonicalPath

    def startDeleteConfirmationForSelectedSession(self) -> None:
        selected = self.filteredSessions[self.selectedIndex] if self.filteredSessions else None
        if selected is None:
            return
        if self.isCurrentSessionPath(selected.session.path):
            if self.onError is not None:
                self.onError("Cannot delete the currently active session")
            return
        self.setConfirmingDeletePath(selected.session.path)

    def invalidate(self) -> None:
        return None

    def buildTreePrefix(self, node: FlatSessionNode) -> str:
        if node.depth == 0:
            return ""
        parts = ["│  " if continues else "   " for continues in node.ancestorContinues]
        return "".join(parts) + ("└─ " if node.isLast else "├─ ")

    def render(self, width: int) -> list[str]:
        lines = [*self.searchInput.render(width), ""]
        if not self.filteredSessions:
            if self.nameFilter == "named":
                toggle_key = key_text("app.session.toggleNamedFilter")
                message = (
                    f"  No named sessions found. Press {toggle_key} to show all."
                    if self.showCwd
                    else f"  No named sessions in current folder. Press {toggle_key} to show all, or Tab to view all."
                )
            elif self.showCwd:
                message = "  No sessions found"
            else:
                message = "  No sessions in current folder. Press Tab to view all."
            return [*lines, theme.fg("muted", truncateToWidth(message, width, "…"))]

        start_index = max(
            0,
            min(self.selectedIndex - (self.maxVisible // 2), len(self.filteredSessions) - self.maxVisible),
        )
        end_index = min(start_index + self.maxVisible, len(self.filteredSessions))

        for index in range(start_index, end_index):
            node = self.filteredSessions[index]
            session = node.session
            is_selected = index == self.selectedIndex
            is_confirming_delete = session.path == self.confirmingDeletePath
            is_current = self.isCurrentSessionPath(session.path)

            prefix = self.buildTreePrefix(node)
            display_text = re.sub(r"[\x00-\x1f\x7f]", " ", session.name or session.firstMessage).strip()

            right_part = f"{session.messageCount} {format_session_date(session.modified)}"
            if self.showCwd and session.cwd:
                right_part = f"{shorten_path(session.cwd)} {right_part}"
            if self.showPath:
                right_part = f"{shorten_path(session.path)} {right_part}"

            cursor = theme.fg("accent", "› ") if is_selected else "  "
            available_for_message = width - visibleWidth(cursor) - visibleWidth(prefix) - visibleWidth(right_part) - 2
            truncated_message = truncateToWidth(display_text, max(10, available_for_message), "…")

            if is_confirming_delete:
                styled_message = theme.fg("error", truncated_message)
            elif is_current:
                styled_message = theme.fg("accent", truncated_message)
            elif session.name:
                styled_message = theme.fg("warning", truncated_message)
            else:
                styled_message = truncated_message
            if is_selected:
                styled_message = theme.bold(styled_message)

            left_part = cursor + theme.fg("dim", prefix) + styled_message
            spacing = max(1, width - visibleWidth(left_part) - visibleWidth(right_part))
            line = left_part + (" " * spacing) + theme.fg("dim" if not is_confirming_delete else "error", right_part)
            if is_selected:
                line = theme.bg("selectedBg", line)
            lines.append(truncateToWidth(line, width))

        if start_index > 0 or end_index < len(self.filteredSessions):
            scroll_text = f"  ({self.selectedIndex + 1}/{len(self.filteredSessions)})"
            lines.append(theme.fg("muted", truncateToWidth(scroll_text, width, "")))
        return lines

    def handleInput(self, keyData: str) -> None:
        kb = getKeybindings()

        if self.confirmingDeletePath is not None:
            if kb.matches(keyData, "tui.select.confirm"):
                path_to_delete = self.confirmingDeletePath
                self.setConfirmingDeletePath(None)
                if self.onDeleteSession is not None:
                    _invoke_callback(self.onDeleteSession(path_to_delete))
                return
            if kb.matches(keyData, "tui.select.cancel"):
                self.setConfirmingDeletePath(None)
                return
            return

        if kb.matches(keyData, "tui.input.tab"):
            if self.onToggleScope is not None:
                self.onToggleScope()
            return
        if kb.matches(keyData, "app.session.toggleSort"):
            if self.onToggleSort is not None:
                self.onToggleSort()
            return
        if self.keybindings.matches(keyData, "app.session.toggleNamedFilter"):
            if self.onToggleNameFilter is not None:
                self.onToggleNameFilter()
            return
        if kb.matches(keyData, "app.session.togglePath"):
            self.showPath = not self.showPath
            if self.onTogglePath is not None:
                self.onTogglePath(self.showPath)
            return
        if kb.matches(keyData, "app.session.delete"):
            self.startDeleteConfirmationForSelectedSession()
            return
        if kb.matches(keyData, "app.session.rename"):
            selected = self.filteredSessions[self.selectedIndex] if self.filteredSessions else None
            if selected is not None and self.onRenameSession is not None:
                self.onRenameSession(selected.session.path)
            return
        if kb.matches(keyData, "app.session.deleteNoninvasive"):
            if self.searchInput.getValue():
                self.searchInput.handleInput(keyData)
                self.filterSessions(self.searchInput.getValue())
                return
            self.startDeleteConfirmationForSelectedSession()
            return
        if kb.matches(keyData, "tui.select.up"):
            self.selectedIndex = max(0, self.selectedIndex - 1)
            return
        if kb.matches(keyData, "tui.select.down"):
            self.selectedIndex = min(len(self.filteredSessions) - 1, self.selectedIndex + 1)
            return
        if kb.matches(keyData, "tui.select.pageUp"):
            self.selectedIndex = max(0, self.selectedIndex - self.maxVisible)
            return
        if kb.matches(keyData, "tui.select.pageDown"):
            self.selectedIndex = min(len(self.filteredSessions) - 1, self.selectedIndex + self.maxVisible)
            return
        if kb.matches(keyData, "tui.select.confirm"):
            if self.filteredSessions and self.onSelect is not None:
                self.onSelect(self.filteredSessions[self.selectedIndex].session.path)
            return
        if kb.matches(keyData, "tui.select.cancel"):
            if self.onCancel is not None:
                self.onCancel()
            return

        self.searchInput.handleInput(keyData)
        self.filterSessions(self.searchInput.getValue())


async def delete_session_file(sessionPath: str) -> dict[str, str | bool]:
    trash_binary = shutil.which("trash")
    if trash_binary is not None:
        trash_args = [trash_binary, "--", sessionPath] if sessionPath.startswith("-") else [trash_binary, sessionPath]
        result = subprocess.run(  # noqa: ASYNC221 - one bounded git call while the picker is open
            trash_args,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 or not os.path.exists(sessionPath):
            return {"ok": True, "method": "trash"}

    try:
        Path(sessionPath).unlink()
    except OSError as error:
        return {"ok": False, "method": "unlink", "error": str(error)}
    return {"ok": True, "method": "unlink"}


class SessionSelectorComponent(Container, Focusable):
    def __init__(
        self,
        currentSessionsLoader: SessionsLoader,
        allSessionsLoader: SessionsLoader,
        onSelect: Callable[[str], None],
        onCancel: Callable[[], None],
        onExit: Callable[[], None],
        requestRender: Callable[[], None],
        options: dict[str, Any] | None = None,
        currentSessionFilePath: str | None = None,
    ) -> None:
        super().__init__()
        self.keybindings = options.get("keybindings") if isinstance(options, dict) else None
        if not isinstance(self.keybindings, KeybindingsManager):
            self.keybindings = KeybindingsManager.create()
        self.currentSessionsLoader = currentSessionsLoader
        self.allSessionsLoader = allSessionsLoader
        self.onCancel = onCancel
        self.requestRender = requestRender
        self.scope: SessionScope = "current"
        self.sortMode: SortMode = "threaded"
        self.nameFilter: NameFilter = "all"
        self.currentSessions: list[SessionInfo] | None = None
        self.allSessions: list[SessionInfo] | None = None
        self.currentLoading = False
        self.allLoading = False
        self.mode: Literal["list", "rename"] = "list"
        self.renameInput = Input()
        self.renameTargetPath: str | None = None
        self.renameSession = options.get("renameSession") if isinstance(options, dict) else None
        self.canRename = callable(self.renameSession)
        self._focused = False
        self._all_load_seq = 0

        self.header = SessionSelectorHeader(self.scope, self.sortMode, self.nameFilter, requestRender)
        self.header.setShowRenameHint(
            bool(options.get("showRenameHint", self.canRename)) if isinstance(options, dict) else self.canRename
        )
        self.sessionList = SessionList(
            [],
            False,
            self.sortMode,
            self.nameFilter,
            self.keybindings,
            currentSessionFilePath,
        )
        self.buildBaseLayout(self.sessionList)

        self.renameInput.onSubmit = lambda value: _run_or_schedule(self.confirmRename(value))

        self.sessionList.onSelect = lambda sessionPath: self._select(sessionPath, onSelect)
        self.sessionList.onCancel = self._cancel
        self.sessionList.onExit = self._exit_wrapper(onExit)
        self.sessionList.onToggleScope = self.toggleScope
        self.sessionList.onToggleSort = self.toggleSortMode
        self.sessionList.onToggleNameFilter = self.toggleNameFilter
        self.sessionList.onRenameSession = self._rename_from_path
        self.sessionList.onTogglePath = self._toggle_path
        self.sessionList.onDeleteConfirmationChange = self._delete_confirmation_change
        self.sessionList.onError = self._show_error
        self.sessionList.onDeleteSession = self._delete_session

        _run_or_schedule(self.loadScope("current", "initial"))

    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        self.sessionList.focused = value
        self.renameInput.focused = value and self.mode == "rename"

    def _select(self, sessionPath: str, onSelect: Callable[[str], None]) -> None:
        self.header.setStatusMessage(None)
        onSelect(sessionPath)

    def _cancel(self) -> None:
        self.header.setStatusMessage(None)
        self.onCancel()

    def _exit_wrapper(self, onExit: Callable[[], None]) -> Callable[[], None]:
        def _wrapped() -> None:
            self.header.setStatusMessage(None)
            onExit()

        return _wrapped

    def _toggle_path(self, showPath: bool) -> None:
        self.header.setShowPath(showPath)
        self.requestRender()

    def _delete_confirmation_change(self, path: str | None) -> None:
        self.header.setConfirmingDeletePath(path)
        self.requestRender()

    def _show_error(self, message: str) -> None:
        self.header.setStatusMessage(("error", message), 3000)
        self.requestRender()

    def _rename_from_path(self, sessionPath: str) -> None:
        if not self.canRename:
            return
        if self.scope == "current" and self.currentLoading:
            return
        if self.scope == "all" and self.allLoading:
            return
        sessions = self.allSessions if self.scope == "all" else self.currentSessions
        session = next((entry for entry in (sessions or []) if entry.path == sessionPath), None)
        self.enterRenameMode(sessionPath, session.name if session is not None else None)

    async def _delete_session(self, sessionPath: str) -> None:
        result = await delete_session_file(sessionPath)
        if result.get("ok"):
            if self.currentSessions is not None:
                self.currentSessions = [session for session in self.currentSessions if session.path != sessionPath]
            if self.allSessions is not None:
                self.allSessions = [session for session in self.allSessions if session.path != sessionPath]
            active_sessions = self.allSessions if self.scope == "all" else self.currentSessions
            self.sessionList.setSessions(active_sessions or [], self.scope == "all")
            message = "Session moved to trash" if result.get("method") == "trash" else "Session deleted"
            self.header.setStatusMessage(("info", message), 2000)
            await self.loadScope(self.scope, "refresh")
        else:
            self.header.setStatusMessage(("error", f"Failed to delete: {result.get('error', 'Unknown error')}"), 3000)
        self.requestRender()

    def buildBaseLayout(self, content: Component, *, showHeader: bool = True) -> None:
        self.clear()
        self.addChild(Spacer(1))
        self.addChild(DynamicBorder(lambda text: theme.fg("accent", text)))
        self.addChild(Spacer(1))
        if showHeader:
            self.addChild(self.header)
            self.addChild(Spacer(1))
        self.addChild(content)
        self.addChild(Spacer(1))
        self.addChild(DynamicBorder(lambda text: theme.fg("accent", text)))

    def enterRenameMode(self, sessionPath: str, currentName: str | None) -> None:
        self.mode = "rename"
        self.renameTargetPath = sessionPath
        self.renameInput.setValue(currentName or "")
        self.renameInput.cursor = len(self.renameInput.getValue())
        self.renameInput.focused = True

        panel = Container()
        panel.addChild(Text(theme.bold("Rename Session"), 1, 0))
        panel.addChild(Spacer(1))
        panel.addChild(self.renameInput)
        panel.addChild(Spacer(1))
        panel.addChild(
            Text(
                theme.fg(
                    "muted",
                    f"{key_text('tui.select.confirm')} to save · {key_text('tui.select.cancel')} to cancel",
                ),
                1,
                0,
            )
        )
        self.buildBaseLayout(panel, showHeader=False)
        self.requestRender()

    def exitRenameMode(self) -> None:
        self.mode = "list"
        self.renameTargetPath = None
        self.renameInput.focused = False
        self.buildBaseLayout(self.sessionList)
        self.requestRender()

    async def confirmRename(self, value: str) -> None:
        next_value = value.strip()
        target = self.renameTargetPath
        rename_session = self.renameSession
        if not next_value:
            return
        if not target or not callable(rename_session):
            self.exitRenameMode()
            return
        try:
            result = rename_session(target, next_value)
            if inspect.isawaitable(result):
                await result
            await self.loadScope(self.scope, "refresh")
        finally:
            self.exitRenameMode()

    async def loadScope(self, scope: SessionScope, reason: Literal["initial", "refresh", "toggle"]) -> None:
        show_cwd = scope == "all"
        seq = None
        if scope == "all":
            self._all_load_seq += 1
            seq = self._all_load_seq
        if scope == "current":
            self.currentLoading = True
        else:
            self.allLoading = True

        self.header.setScope(scope)
        self.header.setLoading(True)
        self.requestRender()

        def on_progress(loaded: int, total: int) -> None:
            if scope != self.scope:
                return
            if seq is not None and seq != self._all_load_seq:
                return
            self.header.setProgress(loaded, total)
            self.requestRender()

        try:
            loader = self.currentSessionsLoader if scope == "current" else self.allSessionsLoader
            sessions = list(await loader(on_progress))
            if scope == "current":
                self.currentSessions = sessions
                self.currentLoading = False
            else:
                self.allSessions = sessions
                self.allLoading = False

            if scope != self.scope:
                return
            if seq is not None and seq != self._all_load_seq:
                return

            self.header.setLoading(False)
            self.sessionList.setSessions(sessions, show_cwd)
            self.requestRender()

            if scope == "all" and not sessions and not (self.currentSessions or []):
                self.onCancel()
        except Exception as error:  # noqa: BLE001 - any load failure is shown in the picker
            if scope == "current":
                self.currentLoading = False
            else:
                self.allLoading = False

            if scope != self.scope:
                return
            if seq is not None and seq != self._all_load_seq:
                return

            self.header.setLoading(False)
            self.header.setStatusMessage(("error", f"Failed to load sessions: {error}"), 4000)
            if reason == "initial":
                self.sessionList.setSessions([], show_cwd)
            self.requestRender()

    def toggleSortMode(self) -> None:
        self.sortMode = (
            "recent"
            if self.sortMode == "threaded"
            else "relevance" if self.sortMode == "recent" else "threaded"
        )
        self.header.setSortMode(self.sortMode)
        self.sessionList.setSortMode(self.sortMode)
        self.requestRender()

    def toggleNameFilter(self) -> None:
        self.nameFilter = "named" if self.nameFilter == "all" else "all"
        self.header.setNameFilter(self.nameFilter)
        self.sessionList.setNameFilter(self.nameFilter)
        self.requestRender()

    def toggleScope(self) -> None:
        if self.scope == "current":
            self.scope = "all"
            self.header.setScope(self.scope)
            if self.allSessions is not None:
                self.header.setLoading(False)
                self.sessionList.setSessions(self.allSessions, True)
                self.requestRender()
                return
            if not self.allLoading:
                _run_or_schedule(self.loadScope("all", "toggle"))
            return

        self.scope = "current"
        self.header.setScope(self.scope)
        self.header.setLoading(self.currentLoading)
        self.sessionList.setSessions(self.currentSessions or [], False)
        self.requestRender()

    def handleInput(self, data: str) -> None:
        if self.mode == "rename":
            kb = getKeybindings()
            if kb.matches(data, "tui.select.cancel"):
                self.exitRenameMode()
                return
            self.renameInput.handleInput(data)
            return
        self.sessionList.handleInput(data)

    def getSessionList(self) -> SessionList:
        return self.sessionList


shortenPath = shorten_path

__all__ = [
    "SessionSelectorComponent",
]

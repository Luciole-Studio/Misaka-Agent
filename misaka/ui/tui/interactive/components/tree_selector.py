"""Interactive session tree selector with filtering, folding, and labels."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from misaka.core.session_manager import SessionTreeNode
from misaka.ui.tui import (
    Component,
    Container,
    Focusable,
    Input,
    Spacer,
    Text,
    TruncatedText,
    getKeybindings,
    truncateToWidth,
)
from misaka.ui.tui.interactive.theme.theme import theme

from .dynamic_border import DynamicBorder
from .keybinding_hints import key_hint, key_text

type FilterMode = Literal["default", "no-tools", "user-only", "labeled-only", "all"]


@dataclass(slots=True)
class GutterInfo:
    position: int
    show: bool


@dataclass(slots=True)
class ToolCallInfo:
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class FlatNode:
    node: SessionTreeNode
    indent: int
    showConnector: bool
    isLast: bool
    gutters: list[GutterInfo]
    isVirtualRootChild: bool


class TreeList(Component):
    def __init__(
        self,
        tree: list[SessionTreeNode],
        currentLeafId: str | None,
        maxVisibleLines: int,
        initialSelectedId: str | None = None,
        initialFilterMode: FilterMode | None = None,
    ) -> None:
        self.flatNodes: list[FlatNode] = []
        self.filteredNodes: list[FlatNode] = []
        self.selectedIndex = 0
        self.currentLeafId = currentLeafId
        self.maxVisibleLines = maxVisibleLines
        self.filterMode: FilterMode = initialFilterMode or "default"
        self.searchQuery = ""
        self.toolCallMap: dict[str, ToolCallInfo] = {}
        self.multipleRoots = False
        self.showLabelTimestamps = False
        self.activePathIds: set[str] = set()
        self.visibleParentMap: dict[str, str | None] = {}
        self.visibleChildrenMap: dict[str | None, list[str]] = {}
        self.lastSelectedId: str | None = None
        self.foldedNodes: set[str] = set()

        self.onSelect: callable | None = None
        self.onCancel: callable | None = None
        self.onCopy: callable | None = None
        self.onLabelEdit: callable | None = None

        self.multipleRoots = len(tree) > 1
        self.flatNodes = self.flattenTree(tree)
        self.buildActivePath()
        self.applyFilter()

        target_id = initialSelectedId or currentLeafId
        self.selectedIndex = self.findNearestVisibleIndex(target_id)
        if self.filteredNodes:
            self.lastSelectedId = self.filteredNodes[self.selectedIndex].node.entry.get("id")

    def findNearestVisibleIndex(self, entryId: str | None) -> int:
        if not self.filteredNodes:
            return 0

        entry_map = {node.node.entry.get("id"): node for node in self.flatNodes}
        visible_index = {
            flat_node.node.entry.get("id"): index
            for index, flat_node in enumerate(self.filteredNodes)
        }

        current_id = entryId
        while current_id is not None:
            if current_id in visible_index:
                return visible_index[current_id]
            parent = entry_map.get(current_id)
            if parent is None:
                break
            next_id = parent.node.entry.get("parentId")
            current_id = next_id if isinstance(next_id, str) else None
        return len(self.filteredNodes) - 1

    def buildActivePath(self) -> None:
        self.activePathIds.clear()
        if self.currentLeafId is None:
            return

        entry_map = {flat_node.node.entry.get("id"): flat_node for flat_node in self.flatNodes}
        current_id: str | None = self.currentLeafId
        while current_id is not None:
            self.activePathIds.add(current_id)
            node = entry_map.get(current_id)
            if node is None:
                break
            parent_id = node.node.entry.get("parentId")
            current_id = parent_id if isinstance(parent_id, str) else None

    def flattenTree(self, roots: list[SessionTreeNode]) -> list[FlatNode]:
        result: list[FlatNode] = []
        self.toolCallMap.clear()

        stack: list[tuple[SessionTreeNode, int, bool, bool, bool, list[GutterInfo], bool]] = []

        contains_active: dict[int, bool] = {}
        leaf_id = self.currentLeafId
        all_nodes: list[SessionTreeNode] = []
        preorder_stack = list(roots)
        while preorder_stack:
            node = preorder_stack.pop()
            all_nodes.append(node)
            for child in reversed(node.children):
                preorder_stack.append(child)
        for node in reversed(all_nodes):
            entry_id = node.entry.get("id")
            has_active = leaf_id is not None and entry_id == leaf_id
            for child in node.children:
                if contains_active.get(id(child), False):
                    has_active = True
            contains_active[id(node)] = has_active

        multiple_roots = len(roots) > 1
        ordered_roots = sorted(
            roots,
            key=lambda node: int(contains_active.get(id(node), False)),
            reverse=True,
        )
        for index in range(len(ordered_roots) - 1, -1, -1):
            is_last = index == len(ordered_roots) - 1
            stack.append(
                (
                    ordered_roots[index],
                    1 if multiple_roots else 0,
                    multiple_roots,
                    multiple_roots,
                    is_last,
                    [],
                    multiple_roots,
                )
            )

        while stack:
            node, indent, just_branched, show_connector, is_last, gutters, is_virtual_root_child = stack.pop()
            entry = node.entry
            if entry.get("type") == "message":
                message = entry.get("message")
                if isinstance(message, dict) and message.get("role") == "assistant":
                    content = message.get("content")
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "toolCall":
                                call_id = block.get("id")
                                name = block.get("name")
                                arguments = block.get("arguments")
                                if isinstance(call_id, str) and isinstance(name, str):
                                    self.toolCallMap[call_id] = ToolCallInfo(
                                        name=name,
                                        arguments=arguments if isinstance(arguments, dict) else {},
                                    )

            result.append(
                FlatNode(
                    node=node,
                    indent=indent,
                    showConnector=show_connector,
                    isLast=is_last,
                    gutters=list(gutters),
                    isVirtualRootChild=is_virtual_root_child,
                )
            )

            children = node.children
            multiple_children = len(children) > 1
            ordered_children = [
                *[child for child in children if contains_active.get(id(child), False)],
                *[child for child in children if not contains_active.get(id(child), False)],
            ]

            if multiple_children or just_branched and indent > 0:
                child_indent = indent + 1
            else:
                child_indent = indent

            connector_displayed = show_connector and not is_virtual_root_child
            current_display_indent = max(0, indent - 1) if self.multipleRoots else indent
            connector_position = max(0, current_display_indent - 1)
            child_gutters = (
                [*gutters, GutterInfo(position=connector_position, show=not is_last)]
                if connector_displayed
                else list(gutters)
            )

            for index in range(len(ordered_children) - 1, -1, -1):
                child_is_last = index == len(ordered_children) - 1
                stack.append(
                    (
                        ordered_children[index],
                        child_indent,
                        multiple_children,
                        multiple_children,
                        child_is_last,
                        list(child_gutters),
                        False,
                    )
                )

        return result

    def applyFilter(self) -> None:
        if self.filteredNodes:
            entry = self.filteredNodes[self.selectedIndex].node.entry
            self.lastSelectedId = entry.get("id") if isinstance(entry.get("id"), str) else self.lastSelectedId

        search_tokens = [token for token in self.searchQuery.lower().split() if token]
        filtered: list[FlatNode] = []

        for flat_node in self.flatNodes:
            entry = flat_node.node.entry
            entry_id = entry.get("id")
            is_current_leaf = entry_id == self.currentLeafId

            if entry.get("type") == "message":
                message = entry.get("message")
                if (
                    isinstance(message, dict)
                    and message.get("role") == "assistant"
                    and not is_current_leaf
                ):
                    has_text = self.hasTextContent(message.get("content"))
                    stop_reason = message.get("stopReason")
                    is_error_or_aborted = isinstance(stop_reason, str) and stop_reason not in {"stop", "toolUse"}
                    if not has_text and not is_error_or_aborted:
                        continue

            is_settings_entry = entry.get("type") in {
                "label",
                "custom",
                "model_change",
                "thinking_level_change",
                "session_info",
            }
            passes_filter = True
            if self.filterMode == "user-only":
                message = entry.get("message")
                passes_filter = (
                    entry.get("type") == "message"
                    and isinstance(message, dict)
                    and message.get("role") == "user"
                )
            elif self.filterMode == "no-tools":
                message = entry.get("message")
                passes_filter = not is_settings_entry and not (
                    entry.get("type") == "message"
                    and isinstance(message, dict)
                    and message.get("role") == "toolResult"
                )
            elif self.filterMode == "labeled-only":
                passes_filter = flat_node.node.label is not None
            elif self.filterMode == "all":
                passes_filter = True
            else:
                passes_filter = not is_settings_entry

            if not passes_filter:
                continue

            if search_tokens:
                node_text = self.getSearchableText(flat_node.node).lower()
                if not all(token in node_text for token in search_tokens):
                    continue

            filtered.append(flat_node)

        if self.foldedNodes:
            skip_set: set[str] = set()
            for flat_node in self.flatNodes:
                entry = flat_node.node.entry
                node_id = entry.get("id")
                parent_id = entry.get("parentId")
                if (
                    isinstance(node_id, str)
                    and isinstance(parent_id, str)
                    and (parent_id in self.foldedNodes or parent_id in skip_set)
                ):
                    skip_set.add(node_id)
            filtered = [
                flat_node
                for flat_node in filtered
                if isinstance(flat_node.node.entry.get("id"), str)
                and flat_node.node.entry.get("id") not in skip_set
            ]

        self.filteredNodes = filtered
        self.recalculateVisualStructure()

        if self.lastSelectedId:
            self.selectedIndex = self.findNearestVisibleIndex(self.lastSelectedId)
        elif self.selectedIndex >= len(self.filteredNodes):
            self.selectedIndex = max(0, len(self.filteredNodes) - 1)

        if self.filteredNodes:
            entry = self.filteredNodes[self.selectedIndex].node.entry
            self.lastSelectedId = entry.get("id") if isinstance(entry.get("id"), str) else self.lastSelectedId
        else:
            self.selectedIndex = 0

    def recalculateVisualStructure(self) -> None:
        if not self.filteredNodes:
            self.visibleParentMap = {}
            self.visibleChildrenMap = {}
            return

        visible_ids = {
            entry_id
            for flat_node in self.filteredNodes
            if isinstance((entry_id := flat_node.node.entry.get("id")), str)
        }
        entry_map = {
            entry_id: flat_node
            for flat_node in self.flatNodes
            if isinstance((entry_id := flat_node.node.entry.get("id")), str)
        }

        def find_visible_ancestor(node_id: str) -> str | None:
            current_id = entry_map.get(node_id).node.entry.get("parentId") if node_id in entry_map else None
            while isinstance(current_id, str):
                if current_id in visible_ids:
                    return current_id
                parent = entry_map.get(current_id)
                if parent is None:
                    break
                current_id = parent.node.entry.get("parentId")
            return None

        visible_parent: dict[str, str | None] = {}
        visible_children: dict[str | None, list[str]] = {None: []}
        for flat_node in self.filteredNodes:
            node_id = flat_node.node.entry.get("id")
            if not isinstance(node_id, str):
                continue
            ancestor_id = find_visible_ancestor(node_id)
            visible_parent[node_id] = ancestor_id
            visible_children.setdefault(ancestor_id, []).append(node_id)

        visible_root_ids = visible_children.get(None, [])
        self.multipleRoots = len(visible_root_ids) > 1
        filtered_node_map = {
            entry_id: flat_node
            for flat_node in self.filteredNodes
            if isinstance((entry_id := flat_node.node.entry.get("id")), str)
        }

        stack: list[tuple[str, int, bool, bool, bool, list[GutterInfo], bool]] = []
        for index in range(len(visible_root_ids) - 1, -1, -1):
            is_last = index == len(visible_root_ids) - 1
            stack.append(
                (
                    visible_root_ids[index],
                    1 if self.multipleRoots else 0,
                    self.multipleRoots,
                    self.multipleRoots,
                    is_last,
                    [],
                    self.multipleRoots,
                )
            )

        while stack:
            node_id, indent, just_branched, show_connector, is_last, gutters, is_virtual_root_child = stack.pop()
            flat_node = filtered_node_map.get(node_id)
            if flat_node is None:
                continue

            flat_node.indent = indent
            flat_node.showConnector = show_connector
            flat_node.isLast = is_last
            flat_node.gutters = list(gutters)
            flat_node.isVirtualRootChild = is_virtual_root_child

            children = visible_children.get(node_id, [])
            multiple_children = len(children) > 1
            if multiple_children or just_branched and indent > 0:
                child_indent = indent + 1
            else:
                child_indent = indent

            connector_displayed = show_connector and not is_virtual_root_child
            current_display_indent = max(0, indent - 1) if self.multipleRoots else indent
            connector_position = max(0, current_display_indent - 1)
            child_gutters = (
                [*gutters, GutterInfo(position=connector_position, show=not is_last)]
                if connector_displayed
                else list(gutters)
            )

            for index in range(len(children) - 1, -1, -1):
                child_is_last = index == len(children) - 1
                stack.append(
                    (
                        children[index],
                        child_indent,
                        multiple_children,
                        multiple_children,
                        child_is_last,
                        list(child_gutters),
                        False,
                    )
                )

        self.visibleParentMap = visible_parent
        self.visibleChildrenMap = visible_children

    def getSearchableText(self, node: SessionTreeNode) -> str:
        entry = node.entry
        parts: list[str] = []

        if node.label:
            parts.append(node.label)

        entry_type = entry.get("type")
        if entry_type == "message":
            message = entry.get("message")
            if isinstance(message, dict):
                role = message.get("role")
                if isinstance(role, str):
                    parts.append(role)
                content = message.get("content")
                if content:
                    parts.append(self.extractContent(content))
                if role == "bashExecution":
                    command = message.get("command")
                    if isinstance(command, str):
                        parts.append(command)
        elif entry_type == "custom_message":
            custom_type = entry.get("customType")
            if isinstance(custom_type, str):
                parts.append(custom_type)
            content = entry.get("content")
            if isinstance(content, str):
                parts.append(content)
            else:
                parts.append(self.extractContent(content))
        elif entry_type == "compaction":
            parts.append("compaction")
        elif entry_type == "branch_summary":
            parts.extend(["branch summary", str(entry.get("summary", ""))])
        elif entry_type == "session_info":
            parts.append("title")
            name = entry.get("name")
            if isinstance(name, str):
                parts.append(name)
        elif entry_type == "model_change":
            parts.extend(["model", str(entry.get("modelId", ""))])
        elif entry_type == "thinking_level_change":
            parts.extend(["thinking", str(entry.get("thinkingLevel", ""))])
        elif entry_type == "custom":
            parts.extend(["custom", str(entry.get("customType", ""))])
        elif entry_type == "label":
            parts.extend(["label", str(entry.get("label", ""))])

        return " ".join(parts)

    def invalidate(self) -> None:
        return None

    def getSearchQuery(self) -> str:
        return self.searchQuery

    def copySelected(self) -> None:
        node = self.getSelectedNode()
        if callable(self.onCopy):
            self.onCopy(self.getEntryCopyText(node) if node is not None else None)

    def getSelectedNode(self) -> SessionTreeNode | None:
        return self.filteredNodes[self.selectedIndex].node if self.filteredNodes else None

    def updateNodeLabel(self, entryId: str, label: str | None, labelTimestamp: str | None = None) -> None:
        for flat_node in self.flatNodes:
            if flat_node.node.entry.get("id") == entryId:
                flat_node.node.label = label
                flat_node.node.labelTimestamp = (
                    labelTimestamp
                    if label
                    else None
                )
                if label and labelTimestamp is None:
                    flat_node.node.labelTimestamp = (
                        datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                    )
                break

    def getStatusLabels(self) -> str:
        labels = ""
        if self.filterMode == "no-tools":
            labels += " [no-tools]"
        elif self.filterMode == "user-only":
            labels += " [user]"
        elif self.filterMode == "labeled-only":
            labels += " [labeled]"
        elif self.filterMode == "all":
            labels += " [all]"
        if self.showLabelTimestamps:
            labels += " [+label time]"
        return labels

    def render(self, width: int) -> list[str]:
        if not self.filteredNodes:
            return [
                truncateToWidth(theme.fg("muted", "  No entries found"), width),
                truncateToWidth(theme.fg("muted", f"  (0/0){self.getStatusLabels()}"), width),
            ]

        lines: list[str] = []
        start_index = max(
            0,
            min(
                self.selectedIndex - (self.maxVisibleLines // 2),
                len(self.filteredNodes) - self.maxVisibleLines,
            ),
        )
        end_index = min(start_index + self.maxVisibleLines, len(self.filteredNodes))

        for index in range(start_index, end_index):
            flat_node = self.filteredNodes[index]
            entry = flat_node.node.entry
            entry_id = entry.get("id")
            is_selected = index == self.selectedIndex

            cursor = theme.fg("accent", "› ") if is_selected else "  "
            display_indent = max(0, flat_node.indent - 1) if self.multipleRoots else flat_node.indent
            connector = (
                ("└─ " if flat_node.isLast else "├─ ")
                if flat_node.showConnector and not flat_node.isVirtualRootChild
                else ""
            )
            connector_position = display_indent - 1 if connector else -1
            total_chars = display_indent * 3
            prefix_chars: list[str] = []
            is_folded = isinstance(entry_id, str) and entry_id in self.foldedNodes
            for offset in range(total_chars):
                level = offset // 3
                pos_in_level = offset % 3
                gutter = next((item for item in flat_node.gutters if item.position == level), None)
                if gutter is not None:
                    prefix_chars.append("│" if pos_in_level == 0 and gutter.show else " ")
                elif connector and level == connector_position:
                    if pos_in_level == 0:
                        prefix_chars.append("└" if flat_node.isLast else "├")
                    elif pos_in_level == 1:
                        foldable = isinstance(entry_id, str) and self.isFoldable(entry_id)
                        prefix_chars.append("⊞" if is_folded else "⊟" if foldable else "─")
                    else:
                        prefix_chars.append(" ")
                else:
                    prefix_chars.append(" ")

            prefix = "".join(prefix_chars)
            shows_fold_in_connector = flat_node.showConnector and not flat_node.isVirtualRootChild
            fold_marker = theme.fg("accent", "⊞ ") if is_folded and not shows_fold_in_connector else ""
            path_marker = (
                theme.fg("accent", "• ")
                if isinstance(entry_id, str) and entry_id in self.activePathIds
                else ""
            )
            label = theme.fg("warning", f"[{flat_node.node.label}] ") if flat_node.node.label else ""
            label_timestamp = (
                theme.fg("muted", f"{self.formatLabelTimestamp(flat_node.node.labelTimestamp)} ")
                if self.showLabelTimestamps and flat_node.node.label and flat_node.node.labelTimestamp
                else ""
            )
            content = self.getEntryDisplayText(flat_node.node, is_selected)

            line = cursor + theme.fg("dim", prefix) + fold_marker + path_marker + label + label_timestamp + content
            if is_selected:
                line = theme.bg("selectedBg", line)
            lines.append(truncateToWidth(line, width))

        lines.append(
            truncateToWidth(
                theme.fg(
                    "muted",
                    f"  ({self.selectedIndex + 1}/{len(self.filteredNodes)}){self.getStatusLabels()}",
                ),
                width,
            )
        )
        return lines

    def getEntryDisplayText(self, node: SessionTreeNode, isSelected: bool) -> str:
        entry = node.entry
        entry_type = entry.get("type")

        def normalize(value: str) -> str:
            return value.replace("\n", " ").replace("\t", " ").strip()

        if entry_type == "message":
            message = entry.get("message")
            if isinstance(message, dict):
                role = message.get("role")
                if role == "user":
                    content = normalize(self.extractContent(message.get("content")))
                    result = theme.fg("accent", "user: ") + content
                elif role == "assistant":
                    text_content = normalize(self.extractContent(message.get("content")))
                    if text_content:
                        result = theme.fg("success", "assistant: ") + text_content
                    elif message.get("stopReason") == "aborted":
                        result = theme.fg("success", "assistant: ") + theme.fg("muted", "(aborted)")
                    elif isinstance(message.get("errorMessage"), str):
                        err_msg = normalize(str(message["errorMessage"]))[:80]
                        result = theme.fg("success", "assistant: ") + theme.fg("error", err_msg)
                    else:
                        result = theme.fg("success", "assistant: ") + theme.fg("muted", "(no content)")
                elif role == "toolResult":
                    call_id = message.get("toolCallId")
                    tool_name = message.get("toolName")
                    tool_call = self.toolCallMap.get(call_id) if isinstance(call_id, str) else None
                    if tool_call is not None:
                        result = theme.fg("muted", self.formatToolCall(tool_call.name, tool_call.arguments))
                    else:
                        result = theme.fg("muted", f"[{tool_name or 'tool'}]")
                elif role == "bashExecution":
                    result = theme.fg("dim", f"[bash]: {normalize(str(message.get('command', '')))}")
                else:
                    result = theme.fg("dim", f"[{role}]")
            else:
                result = ""
        elif entry_type == "custom_message":
            content = entry.get("content")
            if isinstance(content, str):
                value = content
            elif isinstance(content, list):
                value = "".join(
                    str(item.get("text", ""))
                    for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                )
            else:
                value = ""
            result = theme.fg("customMessageLabel", f"[{entry.get('customType')}]: ") + normalize(value)
        elif entry_type == "compaction":
            tokens = round(int(entry.get("tokensBefore", 0)) / 1000)
            result = theme.fg("borderAccent", f"[compaction: {tokens}k tokens]")
        elif entry_type == "branch_summary":
            result = theme.fg("warning", "[branch summary]: ") + normalize(str(entry.get("summary", "")))
        elif entry_type == "model_change":
            result = theme.fg("dim", f"[model: {entry.get('modelId', '')}]")
        elif entry_type == "thinking_level_change":
            result = theme.fg("dim", f"[thinking: {entry.get('thinkingLevel', '')}]")
        elif entry_type == "custom":
            result = theme.fg("dim", f"[custom: {entry.get('customType', '')}]")
        elif entry_type == "label":
            result = theme.fg("dim", f"[label: {entry.get('label', '(cleared)')}]")
        elif entry_type == "session_info":
            name = entry.get("name")
            if isinstance(name, str) and name:
                result = theme.fg("dim", "[title: ") + theme.fg("dim", name) + theme.fg("dim", "]")
            else:
                result = theme.fg("dim", "[title: ") + theme.italic(theme.fg("dim", "empty")) + theme.fg("dim", "]")
        else:
            result = ""

        return theme.bold(result) if isSelected else result

    def formatLabelTimestamp(self, timestamp: str) -> str:
        date = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone()
        now = datetime.now().astimezone()
        time = f"{date.hour:02d}:{date.minute:02d}"
        if date.year == now.year and date.month == now.month and date.day == now.day:
            return time
        if date.year == now.year:
            return f"{date.month}/{date.day} {time}"
        return f"{str(date.year)[2:]}/{date.month}/{date.day} {time}"

    def extractContent(self, content: Any) -> str:
        return self.extractFullContent(content)[:200]

    def extractFullContent(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        result = ""
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                result += str(item.get("text", ""))
        return result

    def getEntryCopyText(self, node: SessionTreeNode) -> str | None:
        entry = node.entry
        text: str | None = None
        entry_type = entry.get("type")
        if entry_type == "message":
            message = entry.get("message") or {}
            if message.get("role") == "bashExecution":
                text = message.get("command")
            elif "content" in message:
                text = self.extractFullContent(message.get("content"))
                if not text and message.get("role") == "assistant":
                    text = message.get("errorMessage")
        elif entry_type == "custom_message":
            text = self.extractFullContent(entry.get("content"))
        elif entry_type in ("compaction", "branch_summary"):
            text = entry.get("summary")
        return text if isinstance(text, str) and text.strip() else None

    def hasTextContent(self, content: Any) -> bool:
        if isinstance(content, str):
            return bool(content.strip())
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text" and str(item.get("text", "")).strip():
                    return True
        return False

    def formatToolCall(self, name: str, args: dict[str, Any]) -> str:
        home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or ""

        def shorten_path(path: str) -> str:
            return f"~{path[len(home):]}" if home and path.startswith(home) else path

        if name == "read":
            path = shorten_path(str(args.get("path") or args.get("file_path") or ""))
            offset = args.get("offset")
            limit = args.get("limit")
            display = path
            if offset is not None or limit is not None:
                start = 1 if offset is None else int(offset)
                end = start + int(limit) - 1 if limit is not None else ""
                display += f":{start}{f'-{end}' if end else ''}"
            return f"[read: {display}]"
        if name == "write":
            return f"[write: {shorten_path(str(args.get('path') or args.get('file_path') or ''))}]"
        if name == "edit":
            return f"[edit: {shorten_path(str(args.get('path') or args.get('file_path') or ''))}]"
        if name == "bash":
            raw_command = str(args.get("command") or "")
            command = raw_command.replace("\n", " ").replace("\t", " ").strip()[:50]
            return f"[bash: {command}{'...' if len(raw_command) > 50 else ''}]"
        if name == "grep":
            pattern = str(args.get("pattern") or "")
            path = shorten_path(str(args.get("path") or "."))
            return f"[grep: /{pattern}/ in {path}]"
        if name == "find":
            pattern = str(args.get("pattern") or "")
            path = shorten_path(str(args.get("path") or "."))
            return f"[find: {pattern} in {path}]"
        if name == "ls":
            return f"[ls: {shorten_path(str(args.get('path') or '.'))}]"
        args_json = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
        display = args_json[:40]
        return f"[{name}: {display}{'...' if len(args_json) > 40 else ''}]"

    def handleInput(self, keyData: str) -> None:
        kb = getKeybindings()
        if kb.matches(keyData, "tui.select.up"):
            if self.filteredNodes:
                self.selectedIndex = len(self.filteredNodes) - 1 if self.selectedIndex == 0 else self.selectedIndex - 1
            return
        if kb.matches(keyData, "tui.select.down"):
            if self.filteredNodes:
                self.selectedIndex = 0 if self.selectedIndex == len(self.filteredNodes) - 1 else self.selectedIndex + 1
            return
        if kb.matches(keyData, "app.tree.foldOrUp"):
            selected = self.filteredNodes[self.selectedIndex] if self.filteredNodes else None
            current_id = selected.node.entry.get("id") if selected is not None else None
            if isinstance(current_id, str) and self.isFoldable(current_id) and current_id not in self.foldedNodes:
                self.foldedNodes.add(current_id)
                self.applyFilter()
            elif self.filteredNodes:
                self.selectedIndex = self.findBranchSegmentStart("up")
            return
        if kb.matches(keyData, "app.tree.unfoldOrDown"):
            selected = self.filteredNodes[self.selectedIndex] if self.filteredNodes else None
            current_id = selected.node.entry.get("id") if selected is not None else None
            if isinstance(current_id, str) and current_id in self.foldedNodes:
                self.foldedNodes.remove(current_id)
                self.applyFilter()
            elif self.filteredNodes:
                self.selectedIndex = self.findBranchSegmentStart("down")
            return
        if kb.matches(keyData, "tui.editor.cursorLeft") or kb.matches(keyData, "tui.select.pageUp"):
            if self.filteredNodes:
                self.selectedIndex = max(0, self.selectedIndex - self.maxVisibleLines)
            return
        if kb.matches(keyData, "tui.editor.cursorRight") or kb.matches(keyData, "tui.select.pageDown"):
            if self.filteredNodes:
                self.selectedIndex = min(len(self.filteredNodes) - 1, self.selectedIndex + self.maxVisibleLines)
            return
        if kb.matches(keyData, "tui.select.confirm"):
            selected = self.filteredNodes[self.selectedIndex] if self.filteredNodes else None
            if selected is not None and callable(self.onSelect):
                self.onSelect(selected.node.entry.get("id"))
            return
        if kb.matches(keyData, "app.message.copy"):
            self.copySelected()
            return
        if kb.matches(keyData, "tui.select.cancel"):
            if self.searchQuery:
                self.searchQuery = ""
                self.foldedNodes.clear()
                self.applyFilter()
            elif callable(self.onCancel):
                self.onCancel()
            return
        if kb.matches(keyData, "app.tree.filter.default"):
            self.filterMode = "default"
            self.foldedNodes.clear()
            self.applyFilter()
            return
        if kb.matches(keyData, "app.tree.filter.noTools"):
            self.filterMode = "default" if self.filterMode == "no-tools" else "no-tools"
            self.foldedNodes.clear()
            self.applyFilter()
            return
        if kb.matches(keyData, "app.tree.filter.userOnly"):
            self.filterMode = "default" if self.filterMode == "user-only" else "user-only"
            self.foldedNodes.clear()
            self.applyFilter()
            return
        if kb.matches(keyData, "app.tree.filter.labeledOnly"):
            self.filterMode = "default" if self.filterMode == "labeled-only" else "labeled-only"
            self.foldedNodes.clear()
            self.applyFilter()
            return
        if kb.matches(keyData, "app.tree.filter.all"):
            self.filterMode = "default" if self.filterMode == "all" else "all"
            self.foldedNodes.clear()
            self.applyFilter()
            return
        if kb.matches(keyData, "app.tree.filter.cycleBackward"):
            modes: list[FilterMode] = ["default", "no-tools", "user-only", "labeled-only", "all"]
            self.filterMode = modes[(modes.index(self.filterMode) - 1) % len(modes)]
            self.foldedNodes.clear()
            self.applyFilter()
            return
        if kb.matches(keyData, "app.tree.filter.cycleForward"):
            modes = ["default", "no-tools", "user-only", "labeled-only", "all"]
            self.filterMode = modes[(modes.index(self.filterMode) + 1) % len(modes)]
            self.foldedNodes.clear()
            self.applyFilter()
            return
        if kb.matches(keyData, "tui.editor.deleteCharBackward"):
            if self.searchQuery:
                self.searchQuery = self.searchQuery[:-1]
                self.foldedNodes.clear()
                self.applyFilter()
            return
        if kb.matches(keyData, "app.tree.editLabel"):
            selected = self.filteredNodes[self.selectedIndex] if self.filteredNodes else None
            if selected is not None and callable(self.onLabelEdit):
                self.onLabelEdit(selected.node.entry.get("id"), selected.node.label)
            return
        if kb.matches(keyData, "app.tree.toggleLabelTimestamp"):
            self.showLabelTimestamps = not self.showLabelTimestamps
            return

        has_control_chars = any(
            ord(char) < 32 or ord(char) == 0x7F or 0x80 <= ord(char) <= 0x9F
            for char in keyData
        )
        if not has_control_chars and keyData:
            self.searchQuery += keyData
            self.foldedNodes.clear()
            self.applyFilter()

    def isFoldable(self, entryId: str) -> bool:
        children = self.visibleChildrenMap.get(entryId)
        if not children:
            return False
        parent_id = self.visibleParentMap.get(entryId)
        if parent_id is None:
            return True
        siblings = self.visibleChildrenMap.get(parent_id)
        return siblings is not None and len(siblings) > 1

    def findBranchSegmentStart(self, direction: Literal["up", "down"]) -> int:
        selected = self.filteredNodes[self.selectedIndex] if self.filteredNodes else None
        selected_id = selected.node.entry.get("id") if selected is not None else None
        if not isinstance(selected_id, str):
            return self.selectedIndex

        index_by_id = {
            flat_node.node.entry.get("id"): index
            for index, flat_node in enumerate(self.filteredNodes)
        }
        current_id = selected_id
        if direction == "down":
            while True:
                children = self.visibleChildrenMap.get(current_id, [])
                if not children:
                    return index_by_id[current_id]
                if len(children) > 1:
                    return index_by_id[children[0]]
                current_id = children[0]

        while True:
            parent_id = self.visibleParentMap.get(current_id)
            if parent_id is None:
                return index_by_id[current_id]
            siblings = self.visibleChildrenMap.get(parent_id, [])
            if len(siblings) > 1:
                segment_start = index_by_id[current_id]
                if segment_start < self.selectedIndex:
                    return segment_start
            current_id = parent_id


class SearchLine(Component):
    def __init__(self, treeList: TreeList) -> None:
        self.treeList = treeList

    def invalidate(self) -> None:
        return None

    def render(self, width: int) -> list[str]:
        query = self.treeList.getSearchQuery()
        if query:
            return [
                truncateToWidth(
                    f"  {theme.fg('muted', 'Type to search:')} {theme.fg('accent', query)}",
                    width,
                )
            ]
        return [truncateToWidth(f"  {theme.fg('muted', 'Type to search:')}", width)]

    def handleInput(self, keyData: str) -> None:
        del keyData


class LabelInput(Component, Focusable):
    def __init__(self, entryId: str, currentLabel: str | None) -> None:
        self.entryId = entryId
        self.input = Input()
        self._focused = False
        self.onSubmit: callable | None = None
        self.onCancel: callable | None = None
        if currentLabel:
            self.input.setValue(currentLabel)

    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        self.input.focused = value

    def invalidate(self) -> None:
        return None

    def render(self, width: int) -> list[str]:
        indent = "  "
        available_width = max(1, width - len(indent))
        lines = [truncateToWidth(f"{indent}{theme.fg('muted', 'Label (empty to remove):')}", width)]
        lines.extend(
            truncateToWidth(f"{indent}{line}", width)
            for line in self.input.render(available_width)
        )
        lines.append(
            truncateToWidth(
                f"{indent}{key_hint('tui.select.confirm', 'save')}  {key_hint('tui.select.cancel', 'cancel')}",
                width,
            )
        )
        return lines

    def handleInput(self, keyData: str) -> None:
        kb = getKeybindings()
        if kb.matches(keyData, "tui.select.confirm"):
            if callable(self.onSubmit):
                self.onSubmit(self.entryId, self.input.getValue().strip() or None)
        elif kb.matches(keyData, "tui.select.cancel"):
            if callable(self.onCancel):
                self.onCancel()
        else:
            self.input.handleInput(keyData)


class TreeSelectorComponent(Container, Focusable):
    def __init__(
        self,
        tree: list[SessionTreeNode],
        currentLeafId: str | None,
        terminalHeight: int,
        onSelect: callable,
        onCancel: callable,
        onLabelChange: callable | None = None,
        initialSelectedId: str | None = None,
        initialFilterMode: FilterMode | None = None,
    ) -> None:
        super().__init__()
        self._focused = False
        self.onLabelChangeCallback = onLabelChange
        self.treeList = TreeList(
            tree,
            currentLeafId,
            max(5, terminalHeight // 2),
            initialSelectedId,
            initialFilterMode,
        )
        self.onCopy: callable | None = None
        self.treeList.onSelect = onSelect
        self.treeList.onCancel = onCancel
        self.treeList.onCopy = lambda text: self.onCopy(text) if callable(self.onCopy) else None
        self.treeList.onLabelEdit = lambda entryId, currentLabel: self.showLabelInput(entryId, currentLabel)
        self.labelInput: LabelInput | None = None
        self.treeContainer = Container()
        self.labelInputContainer = Container()
        self.treeContainer.addChild(self.treeList)

        filter_keys = "/".join(
            [
                key_text("app.tree.filter.default"),
                key_text("app.tree.filter.noTools"),
                key_text("app.tree.filter.userOnly"),
                key_text("app.tree.filter.labeledOnly"),
                key_text("app.tree.filter.all"),
            ]
        )
        cycle_keys = f"{key_text('app.tree.filter.cycleForward')}/{key_text('app.tree.filter.cycleBackward')}"
        branch_keys = f"{key_text('app.tree.foldOrUp')}/{key_text('app.tree.unfoldOrDown')}"

        self.addChild(Spacer(1))
        self.addChild(DynamicBorder())
        self.addChild(Text(theme.bold("  Session Tree"), 1, 0))
        self.addChild(
            TruncatedText(
                theme.fg(
                    "muted",
                    "  ↑/↓: move. ←/→: page. "
                    f"{branch_keys}: fold/branch. "
                    f"{key_text('app.tree.editLabel')}: label. "
                    f"{filter_keys}: filters ({cycle_keys} cycle). "
                    f"{key_text('app.tree.toggleLabelTimestamp')}: label time",
                ),
                0,
                0,
            )
        )
        self.addChild(SearchLine(self.treeList))
        self.addChild(DynamicBorder())
        self.addChild(Spacer(1))
        self.addChild(self.treeContainer)
        self.addChild(self.labelInputContainer)
        self.addChild(Spacer(1))
        self.addChild(DynamicBorder())

        if not tree:
            timer = threading.Timer(0.1, onCancel)
            timer.daemon = True
            timer.start()

    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        if self.labelInput is not None:
            self.labelInput.focused = value

    def showLabelInput(self, entryId: str, currentLabel: str | None) -> None:
        self.labelInput = LabelInput(entryId, currentLabel)
        self.labelInput.onSubmit = self._saveLabel
        self.labelInput.onCancel = self.hideLabelInput
        self.labelInput.focused = self._focused

        self.treeContainer.clear()
        self.labelInputContainer.clear()
        self.labelInputContainer.addChild(self.labelInput)

    def _saveLabel(self, entryId: str, label: str | None) -> None:
        self.treeList.updateNodeLabel(entryId, label)
        if callable(self.onLabelChangeCallback):
            self.onLabelChangeCallback(entryId, label)
        self.hideLabelInput()

    def hideLabelInput(self) -> None:
        self.labelInput = None
        self.labelInputContainer.clear()
        self.treeContainer.clear()
        self.treeContainer.addChild(self.treeList)

    def handleInput(self, keyData: str) -> None:
        if self.labelInput is not None:
            self.labelInput.handleInput(keyData)
        else:
            self.treeList.handleInput(keyData)

    def getTreeList(self) -> TreeList:
        return self.treeList


__all__ = [
    "FilterMode",
    "TreeSelectorComponent",
]

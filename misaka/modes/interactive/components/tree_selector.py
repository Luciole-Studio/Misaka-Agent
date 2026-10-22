"""Translated from pi's ``modes/interactive/components/tree-selector.ts`` (v1.3.0)."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from misaka.core.session_manager import SessionTreeNode
from misaka.core.tools.render_utils import formatReadLineRange
from misaka.modes.interactive.theme.theme import theme
from misaka.ui.tui import (
    Container,
    Input,
    Keybinding,
    Spacer,
    Text,
    getKeybindings,
    sliceByColumn,
    truncateToWidth,
    visibleWidth,
    wrapTextWithAnsi,
)
from misaka.utils import js_regexp
from misaka.utils.js import (
    UNDEFINED,
    jsIndex,
    jsLength,
    jsonStringify,
    jsRound,
    jsSlice,
    jsString,
    jsTrim,
    jsTrimEnd,
    setTimeout,
)
from misaka.utils.js_regexp import RegExp

from .dynamic_border import DynamicBorder
from .keybinding_hints import formatKeyText, keyHint


@dataclass(slots=True)
class GutterInfo:
    """Gutter info: position (displayIndent where connector was) and whether to show │"""

    position: int  # displayIndent level where the connector was shown
    show: bool  # true = show │, false = show spaces


@dataclass(slots=True)
class FlatNode:
    """Flattened tree node for navigation"""

    node: SessionTreeNode
    # Indentation level (each level = 3 chars)
    indent: int
    # Whether to show connector (├─ or └─) - true if parent has multiple children
    showConnector: bool
    # If showConnector, true = last sibling (└─), false = not last (├─)
    isLast: bool
    # Gutter info for each ancestor branch point
    gutters: list[GutterInfo]
    # True if this node is a root under a virtual branching root (multiple roots)
    isVirtualRootChild: bool


@dataclass(slots=True)
class HorizontalViewportRow:
    gutter: str
    body: str
    anchorCol: int
    bodyWidth: int
    isSelected: bool


TREE_GUTTER_WIDTH = 2
MIN_VISIBLE_ANCHOR_CONTENT_WIDTH = 4
MAX_VISIBLE_ANCHOR_CONTENT_WIDTH = 20
MIN_ANCHOR_CONTEXT_WIDTH = 2
MAX_ANCHOR_CONTEXT_WIDTH = 12


def renderHorizontalViewport(rows: list[HorizontalViewportRow], width: int) -> list[str]:
    """Render tree rows into a horizontally clipped viewport.

    The tree gutter is always kept visible. The row bodies are shifted left only
    when the selected row's anchor (the start of its entry text after tree
    indentation/markers) would otherwise be too far right to see useful content."""
    viewportWidth = max(0, width - TREE_GUTTER_WIDTH)
    maxBodyWidth = max([0, *(row.bodyWidth for row in rows)])
    maxHorizontalScroll = max(0, maxBodyWidth - viewportWidth)
    selectedRow = next((row for row in rows if row.isSelected), None)

    # Only pan horizontally when needed to keep enough selected-row content visible after its anchor.
    horizontalScroll = 0
    if selectedRow and maxHorizontalScroll > 0:
        minVisibleAnchorContentWidth = min(
            MAX_VISIBLE_ANCHOR_CONTENT_WIDTH,
            max(MIN_VISIBLE_ANCHOR_CONTENT_WIDTH, math.floor(viewportWidth / 3)),
        )
        if selectedRow.anchorCol > viewportWidth - minVisibleAnchorContentWidth:
            anchorContextWidth = min(
                MAX_ANCHOR_CONTEXT_WIDTH,
                max(MIN_ANCHOR_CONTEXT_WIDTH, math.floor(viewportWidth / 4)),
            )
            horizontalScroll = min(maxHorizontalScroll, selectedRow.anchorCol - anchorContextWidth)

    # Clip only the body; the fixed-width gutter remains visible as navigation context.
    def clip(row: HorizontalViewportRow) -> str:
        line = (
            f"{row.gutter}{sliceByColumn(row.body, horizontalScroll, viewportWidth, True)}\x1b[0m"
            if horizontalScroll > 0
            else row.gutter + row.body
        )
        return truncateToWidth(line, width, "")

    return [clip(row) for row in rows]


# Filter mode for tree display
type FilterMode = Literal["default", "no-tools", "user-only", "labeled-only", "all"]


@dataclass(slots=True)
class ToolCallInfo:
    """Tool call info for lookup"""

    name: str
    arguments: dict[str, Any]


_WHITESPACE_RUN = RegExp(r"\s+")


class TreeList:
    """Tree list component with selection and ASCII art visualization"""

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
        self.filterMode: FilterMode = "default"
        self.searchQuery = ""
        self.toolCallMap: dict[str, ToolCallInfo] = {}
        self.multipleRoots = False
        self.showLabelTimestamps = False
        self.activePathIds: set[str] = set()
        self.visibleParentMap: dict[str, str | None] = {}
        self.visibleChildrenMap: dict[str | None, list[str]] = {}
        self.lastSelectedId: str | None = None
        self.foldedNodes: set[str] = set()

        self.onSelect: Callable[[str], None] | None = None
        self.onCancel: Callable[[], None] | None = None
        self.onCopy: Callable[[str | None], None] | None = None
        self.onLabelEdit: Callable[[str, str | None], None] | None = None

        self.currentLeafId = currentLeafId
        self.maxVisibleLines = maxVisibleLines
        self.filterMode = initialFilterMode if initialFilterMode is not None else "default"
        self.multipleRoots = len(tree) > 1
        self.flatNodes = self.flattenTree(tree)
        self.buildActivePath()
        self.applyFilter()

        # Start with initialSelectedId if provided, otherwise current leaf
        targetId = initialSelectedId if initialSelectedId is not None else currentLeafId
        self.selectedIndex = self.findNearestVisibleIndex(targetId)
        selected = jsIndex(self.filteredNodes, self.selectedIndex)
        self.lastSelectedId = selected.node.entry["id"] if selected else None

    def findNearestVisibleIndex(self, entryId: str | None) -> int:
        """Find the index of the nearest visible entry, walking up the parent chain if needed.
        Returns the index in filteredNodes, or the last index as fallback."""
        if len(self.filteredNodes) == 0:
            return 0

        # Build a map for parent lookup
        entryMap: dict[str, FlatNode] = {}
        for flatNode in self.flatNodes:
            entryMap[flatNode.node.entry["id"]] = flatNode

        # Build a map of visible entry IDs to their indices in filteredNodes
        visibleIdToIndex = {node.node.entry["id"]: i for i, node in enumerate(self.filteredNodes)}

        # Walk from entryId up to root, looking for a visible entry
        currentId = entryId
        while currentId is not None:
            index = visibleIdToIndex.get(currentId)
            if index is not None:
                return index
            node = entryMap.get(currentId)
            if not node:
                break
            currentId = node.node.entry.get("parentId")

        # Fallback: last visible entry
        return len(self.filteredNodes) - 1

    def buildActivePath(self) -> None:
        """Build the set of entry IDs on the path from root to current leaf"""
        self.activePathIds.clear()
        if not self.currentLeafId:
            return

        # Build a map of id -> entry for parent lookup
        entryMap: dict[str, FlatNode] = {}
        for flatNode in self.flatNodes:
            entryMap[flatNode.node.entry["id"]] = flatNode

        # Walk from leaf to root
        currentId: str | None = self.currentLeafId
        while currentId:
            self.activePathIds.add(currentId)
            node = entryMap.get(currentId)
            if not node:
                break
            currentId = node.node.entry.get("parentId")

    def flattenTree(self, roots: list[SessionTreeNode]) -> list[FlatNode]:
        result: list[FlatNode] = []
        self.toolCallMap.clear()

        # Indentation rules:
        # - At indent 0: stay at 0 unless parent has >1 children (then +1)
        # - At indent 1: children always go to indent 2 (visual grouping of subtree)
        # - At indent 2+: stay flat for single-child chains, +1 only if parent branches

        # Stack items: [node, indent, justBranched, showConnector, isLast, gutters, isVirtualRootChild]
        stack: list[tuple[SessionTreeNode, int, bool, bool, bool, list[GutterInfo], bool]] = []

        # Determine which subtrees contain the active leaf (to sort current branch first)
        # Use iterative post-order traversal to avoid stack overflow
        # MISAKA fork: keyed by `id(node)`, as the nodes are unhashable dataclasses.
        containsActive: dict[int, bool] = {}
        leafId = self.currentLeafId
        # Build list in pre-order, then process in reverse for post-order effect
        allNodes: list[SessionTreeNode] = []
        preOrderStack: list[SessionTreeNode] = [*roots]
        while len(preOrderStack) > 0:
            node = preOrderStack.pop()
            allNodes.append(node)
            # Push children in reverse so they're processed left-to-right
            for i in range(len(node.children) - 1, -1, -1):
                preOrderStack.append(node.children[i])
        # Process in reverse (post-order): children before parents
        for i in range(len(allNodes) - 1, -1, -1):
            node = allNodes[i]
            has = leafId is not None and node.entry["id"] == leafId
            for child in node.children:
                if containsActive.get(id(child)):
                    has = True
            containsActive[id(node)] = has

        # Add roots in reverse order, prioritizing the one containing the active leaf
        # If multiple roots, treat them as children of a virtual root that branches
        multipleRoots = len(roots) > 1
        orderedRoots = sorted(roots, key=lambda node: int(bool(containsActive.get(id(node)))), reverse=True)
        for i in range(len(orderedRoots) - 1, -1, -1):
            isLast = i == len(orderedRoots) - 1
            stack.append(
                (orderedRoots[i], 1 if multipleRoots else 0, multipleRoots, multipleRoots, isLast, [], multipleRoots)
            )

        while len(stack) > 0:
            node, indent, justBranched, showConnector, isLast, gutters, isVirtualRootChild = stack.pop()

            # Extract tool calls from assistant messages for later lookup
            entry = node.entry
            message = entry.get("message")
            if entry.get("type") == "message" and isinstance(message, dict) and message.get("role") == "assistant":
                content = message.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "toolCall":
                            tc = block
                            self.toolCallMap[tc["id"]] = ToolCallInfo(name=tc["name"], arguments=tc["arguments"])

            result.append(
                FlatNode(
                    node=node,
                    indent=indent,
                    showConnector=showConnector,
                    isLast=isLast,
                    gutters=gutters,
                    isVirtualRootChild=isVirtualRootChild,
                )
            )

            children = node.children
            multipleChildren = len(children) > 1

            # Order children so the branch containing the active leaf comes first
            prioritized: list[SessionTreeNode] = []
            rest: list[SessionTreeNode] = []
            for child in children:
                if containsActive.get(id(child)):
                    prioritized.append(child)
                else:
                    rest.append(child)
            orderedChildren = [*prioritized, *rest]

            # Calculate child indent
            childIndent: int
            if multipleChildren:
                # Parent branches: children get +1
                childIndent = indent + 1
            elif justBranched and indent > 0:
                # First generation after a branch: +1 for visual grouping
                childIndent = indent + 1
            else:
                # Single-child chain: stay flat
                childIndent = indent

            # Build gutters for children
            # If this node showed a connector, add a gutter entry for descendants
            # Only add gutter if connector is actually displayed (not suppressed for virtual root children)
            connectorDisplayed = showConnector and not isVirtualRootChild
            # When connector is displayed, add a gutter entry at the connector's position
            # Connector is at position (displayIndent - 1), so gutter should be there too
            currentDisplayIndent = max(0, indent - 1) if self.multipleRoots else indent
            connectorPosition = max(0, currentDisplayIndent - 1)
            childGutters = (
                [*gutters, GutterInfo(position=connectorPosition, show=not isLast)] if connectorDisplayed else gutters
            )

            # Add children in reverse order
            for i in range(len(orderedChildren) - 1, -1, -1):
                childIsLast = i == len(orderedChildren) - 1
                stack.append(
                    (orderedChildren[i], childIndent, multipleChildren, multipleChildren, childIsLast, childGutters, False)
                )

        return result

    def applyFilter(self) -> None:
        # Update lastSelectedId only when we have a valid selection (non-empty list)
        # This preserves the selection when switching through empty filter results
        if len(self.filteredNodes) > 0:
            selected = jsIndex(self.filteredNodes, self.selectedIndex)
            self.lastSelectedId = selected.node.entry["id"] if selected else self.lastSelectedId

        searchTokens = [token for token in js_regexp.split(self.searchQuery.lower(), _WHITESPACE_RUN) if token]

        def keep(flatNode: FlatNode) -> bool:
            entry = flatNode.node.entry
            if entry.get("type") == "usage":
                return False
            isCurrentLeaf = entry["id"] == self.currentLeafId
            message = entry.get("message") if entry.get("type") == "message" else None
            role = message.get("role") if isinstance(message, dict) else None

            # Skip assistant messages with only tool calls (no text) unless error/aborted
            # Always show current leaf so active position is visible
            if isinstance(message, dict) and role == "assistant" and not isCurrentLeaf:
                hasText = self.hasTextContent(message.get("content"))
                stopReason = message.get("stopReason")
                isErrorOrAborted = stopReason and stopReason != "stop" and stopReason != "toolUse"
                # Only hide if no text AND not an error/aborted message
                if not hasText and not isErrorOrAborted:
                    return False

            # Apply filter mode
            passesFilter = True
            # Entry types hidden in default view (settings/bookkeeping)
            isSettingsEntry = entry.get("type") in (
                "label",
                "context_edit",
                "custom",
                "model_change",
                "thinking_level_change",
                "session_info",
            )

            match self.filterMode:
                case "user-only":
                    # Just user messages
                    passesFilter = entry.get("type") == "message" and role == "user"
                case "no-tools":
                    # Default minus tool results
                    passesFilter = not isSettingsEntry and not (entry.get("type") == "message" and role == "toolResult")
                case "labeled-only":
                    # Just labeled entries
                    passesFilter = flatNode.node.label is not None
                case "all":
                    # Show everything
                    passesFilter = True
                case _:
                    # Default mode: hide settings/bookkeeping entries
                    passesFilter = not isSettingsEntry

            if not passesFilter:
                return False

            # Apply search filter
            if len(searchTokens) > 0:
                nodeText = self.getSearchableText(flatNode.node).lower()
                return all(token in nodeText for token in searchTokens)

            return True

        self.filteredNodes = [flatNode for flatNode in self.flatNodes if keep(flatNode)]

        # Filter out descendants of folded nodes.
        if len(self.foldedNodes) > 0:
            skipSet: set[str] = set()
            for flatNode in self.flatNodes:
                id = flatNode.node.entry["id"]
                parentId = flatNode.node.entry.get("parentId")
                if parentId is not None and (parentId in self.foldedNodes or parentId in skipSet):
                    skipSet.add(id)
            self.filteredNodes = [
                flatNode for flatNode in self.filteredNodes if flatNode.node.entry["id"] not in skipSet
            ]

        # Recalculate visual structure (indent, connectors, gutters) based on visible tree
        self.recalculateVisualStructure()

        # Try to preserve cursor on the same node, or find nearest visible ancestor
        if self.lastSelectedId:
            self.selectedIndex = self.findNearestVisibleIndex(self.lastSelectedId)
        elif self.selectedIndex >= len(self.filteredNodes):
            # Clamp index if out of bounds
            self.selectedIndex = max(0, len(self.filteredNodes) - 1)

        # Update lastSelectedId to the actual selection (may have changed due to parent walk)
        if len(self.filteredNodes) > 0:
            selected = jsIndex(self.filteredNodes, self.selectedIndex)
            self.lastSelectedId = selected.node.entry["id"] if selected else self.lastSelectedId

    def recalculateVisualStructure(self) -> None:
        """Recompute indentation/connectors for the filtered view

        Filtering can hide intermediate entries; descendants attach to the nearest visible ancestor.
        Keep indentation semantics aligned with flattenTree() so single-child chains don't drift right."""
        if len(self.filteredNodes) == 0:
            return

        visibleIds = {n.node.entry["id"] for n in self.filteredNodes}

        # Build entry map for efficient parent lookup (using full tree)
        entryMap: dict[str, FlatNode] = {}
        for flatNode in self.flatNodes:
            entryMap[flatNode.node.entry["id"]] = flatNode

        # Find nearest visible ancestor for a node
        def findVisibleAncestor(nodeId: str) -> str | None:
            found: FlatNode | None = entryMap.get(nodeId)
            currentId = found.node.entry.get("parentId") if found else None
            while currentId is not None:
                if currentId in visibleIds:
                    return currentId
                found = entryMap.get(currentId)
                currentId = found.node.entry.get("parentId") if found else None
            return None

        # Build visible tree structure:
        # - visibleParent: nodeId → nearest visible ancestor (or null for roots)
        # - visibleChildren: parentId → list of visible children (in filteredNodes order)
        visibleParent: dict[str, str | None] = {}
        visibleChildren: dict[str | None, list[str]] = {}
        visibleChildren[None] = []  # root-level nodes

        for flatNode in self.filteredNodes:
            nodeId = flatNode.node.entry["id"]
            ancestorId = findVisibleAncestor(nodeId)
            visibleParent[nodeId] = ancestorId

            if ancestorId not in visibleChildren:
                visibleChildren[ancestorId] = []
            visibleChildren[ancestorId].append(nodeId)

        # Update multipleRoots based on visible roots
        visibleRootIds = visibleChildren[None]
        self.multipleRoots = len(visibleRootIds) > 1

        # Build a map for quick lookup: nodeId → FlatNode
        filteredNodeMap: dict[str, FlatNode] = {}
        for flatNode in self.filteredNodes:
            filteredNodeMap[flatNode.node.entry["id"]] = flatNode

        # DFS over the visible tree using flattenTree() indentation semantics
        # Stack items: [nodeId, indent, justBranched, showConnector, isLast, gutters, isVirtualRootChild]
        stack: list[tuple[str, int, bool, bool, bool, list[GutterInfo], bool]] = []

        # Add visible roots in reverse order (to process in forward order via stack)
        for i in range(len(visibleRootIds) - 1, -1, -1):
            isLast = i == len(visibleRootIds) - 1
            stack.append(
                (
                    visibleRootIds[i],
                    1 if self.multipleRoots else 0,
                    self.multipleRoots,
                    self.multipleRoots,
                    isLast,
                    [],
                    self.multipleRoots,
                )
            )

        while len(stack) > 0:
            nodeId, indent, justBranched, showConnector, isLast, gutters, isVirtualRootChild = stack.pop()

            visibleNode = filteredNodeMap.get(nodeId)
            if not visibleNode:
                continue
            flatNode = visibleNode

            # Update this node's visual properties
            flatNode.indent = indent
            flatNode.showConnector = showConnector
            flatNode.isLast = isLast
            flatNode.gutters = gutters
            flatNode.isVirtualRootChild = isVirtualRootChild

            # Get visible children of this node
            children = visibleChildren.get(nodeId) or []
            multipleChildren = len(children) > 1

            # Child indent follows flattenTree(): branch points (and first generation after a branch) shift +1
            childIndent: int
            if multipleChildren or justBranched and indent > 0:
                childIndent = indent + 1
            else:
                childIndent = indent

            # Child gutters follow flattenTree() connector/gutter rules
            connectorDisplayed = showConnector and not isVirtualRootChild
            currentDisplayIndent = max(0, indent - 1) if self.multipleRoots else indent
            connectorPosition = max(0, currentDisplayIndent - 1)
            childGutters = (
                [*gutters, GutterInfo(position=connectorPosition, show=not isLast)] if connectorDisplayed else gutters
            )

            # Add children in reverse order (to process in forward order via stack)
            for i in range(len(children) - 1, -1, -1):
                childIsLast = i == len(children) - 1
                stack.append(
                    (children[i], childIndent, multipleChildren, multipleChildren, childIsLast, childGutters, False)
                )

        # Store visible tree maps for ancestor/descendant lookups in navigation
        self.visibleParentMap = visibleParent
        self.visibleChildrenMap = visibleChildren

    def getSearchableText(self, node: SessionTreeNode) -> str:
        """Get searchable text content from a node"""
        entry = node.entry
        parts: list[Any] = []

        if node.label:
            parts.append(node.label)

        match entry.get("type"):
            case "message":
                msg = entry.get("message") or {}
                parts.append(msg.get("role"))
                if "content" in msg and msg.get("content"):
                    parts.append(self.extractContent(msg["content"]))
                if msg.get("role") == "bashExecution":
                    command = msg.get("command")
                    if command:
                        parts.append(command)
            case "custom_message":
                parts.append(entry.get("customType"))
                content = entry.get("content")
                if isinstance(content, str):
                    parts.append(content)
                else:
                    parts.append(self.extractContent(content))
            case "compaction":
                parts.append("compaction")
            case "branch_summary":
                parts.extend(["branch summary", entry.get("summary")])
            case "session_info":
                parts.append("title")
                if entry.get("name"):
                    parts.append(entry["name"])
            case "model_change":
                parts.extend(["model", entry.get("modelId")])
            case "thinking_level_change":
                parts.extend(["thinking", entry.get("thinkingLevel")])
            case "custom":
                parts.extend(["custom", entry.get("customType")])
            case "context_edit":
                parts.extend(
                    ["context edit", "omit" if entry.get("replacement") is None else "replace", entry.get("targetId")]
                )
            case "label":
                label = entry.get("label")
                parts.extend(["label", label if label is not None else ""])

        # `Array.prototype.join` writes `undefined`/`null` as "".
        return " ".join("" if part is None else jsString(part) for part in parts)

    def invalidate(self) -> None:
        pass

    def getSearchQuery(self) -> str:
        return self.searchQuery

    def getSelectedNode(self) -> SessionTreeNode | None:
        selected = jsIndex(self.filteredNodes, self.selectedIndex)
        return selected.node if selected else None

    def copySelected(self) -> None:
        node = self.getSelectedNode()
        if self.onCopy:
            self.onCopy(self.getEntryCopyText(node) if node else None)

    def updateNodeLabel(self, entryId: str, label: str | None, labelTimestamp: str | None = None) -> None:
        for flatNode in self.flatNodes:
            if flatNode.node.entry["id"] == entryId:
                flatNode.node.label = label
                flatNode.node.labelTimestamp = (
                    (
                        labelTimestamp
                        if labelTimestamp is not None
                        else datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                    )
                    if label
                    else None
                )
                break

    def getStatusLabels(self) -> str:
        labels = ""
        match self.filterMode:
            case "no-tools":
                labels += " [no-tools]"
            case "user-only":
                labels += " [user]"
            case "labeled-only":
                labels += " [labeled]"
            case "all":
                labels += " [all]"
        if self.showLabelTimestamps:
            labels += " [+label time]"
        return labels

    def render(self, width: int) -> list[str]:
        lines: list[str] = []

        if len(self.filteredNodes) == 0:
            lines.append(truncateToWidth(theme.fg("muted", "  No entries found"), width))
            lines.append(truncateToWidth(theme.fg("muted", f"  (0/0){self.getStatusLabels()}"), width))
            return lines

        startIndex = max(
            0,
            min(
                self.selectedIndex - math.floor(self.maxVisibleLines / 2),
                len(self.filteredNodes) - self.maxVisibleLines,
            ),
        )
        endIndex = min(startIndex + self.maxVisibleLines, len(self.filteredNodes))

        renderedRows: list[HorizontalViewportRow] = []
        for i in range(startIndex, endIndex):
            flatNode = self.filteredNodes[i]
            entry = flatNode.node.entry
            isSelected = i == self.selectedIndex

            # Build line: cursor + prefix + path marker + label + content
            cursor = theme.fg("accent", "› ") if isSelected else "  "

            # If multiple roots, shift display (roots at 0, not 1)
            displayIndent = max(0, flatNode.indent - 1) if self.multipleRoots else flatNode.indent

            # Build prefix with gutters at their correct positions
            # Each gutter has a position (displayIndent where its connector was shown)
            connector = (
                ("└─ " if flatNode.isLast else "├─ ")
                if flatNode.showConnector and not flatNode.isVirtualRootChild
                else ""
            )
            connectorPosition = displayIndent - 1 if connector else -1

            # Build prefix char by char, placing gutters and connector at their positions
            totalChars = displayIndent * 3
            prefixChars: list[str] = []
            isFolded = entry["id"] in self.foldedNodes
            for j in range(totalChars):
                level = math.floor(j / 3)
                posInLevel = j % 3

                # Check if there's a gutter at this level
                gutter = next((g for g in flatNode.gutters if g.position == level), None)
                if gutter:
                    if posInLevel == 0:
                        prefixChars.append("│" if gutter.show else " ")
                    else:
                        prefixChars.append(" ")
                elif connector and level == connectorPosition:
                    # Connector at this level, with fold indicator
                    if posInLevel == 0:
                        prefixChars.append("└" if flatNode.isLast else "├")
                    elif posInLevel == 1:
                        foldable = self.isFoldable(entry["id"])
                        prefixChars.append("⊞" if isFolded else "⊟" if foldable else "─")
                    else:
                        prefixChars.append(" ")
                else:
                    prefixChars.append(" ")
            prefix = "".join(prefixChars)

            # Fold marker for nodes without connectors (roots)
            showsFoldInConnector = flatNode.showConnector and not flatNode.isVirtualRootChild
            foldMarker = theme.fg("accent", "⊞ ") if isFolded and not showsFoldInConnector else ""

            # Active path marker - shown right before the entry text
            isOnActivePath = entry["id"] in self.activePathIds
            pathMarker = theme.fg("accent", "• ") if isOnActivePath else ""

            label = theme.fg("warning", f"[{flatNode.node.label}] ") if flatNode.node.label else ""
            labelTimestamp = (
                theme.fg("muted", f"{self.formatLabelTimestamp(flatNode.node.labelTimestamp)} ")
                if self.showLabelTimestamps and flatNode.node.label and flatNode.node.labelTimestamp
                else ""
            )
            content = self.getEntryDisplayText(flatNode.node, isSelected)
            prefixPart = theme.fg("dim", prefix) + foldMarker + pathMarker
            anchorCol = visibleWidth(prefixPart)
            gutterText = cursor
            body = prefixPart + label + labelTimestamp + content
            if isSelected:
                gutterText = theme.bg("selectedBg", gutterText)
                body = theme.bg("selectedBg", body)
            renderedRows.append(
                HorizontalViewportRow(
                    gutter=gutterText, body=body, anchorCol=anchorCol, bodyWidth=visibleWidth(body), isSelected=isSelected
                )
            )

        lines.extend(renderHorizontalViewport(renderedRows, width))
        lines.append(
            truncateToWidth(
                theme.fg("muted", f"  ({self.selectedIndex + 1}/{len(self.filteredNodes)}){self.getStatusLabels()}"),
                width,
            )
        )

        return lines

    def getEntryDisplayText(self, node: SessionTreeNode, isSelected: bool) -> str:
        entry = node.entry
        result: str

        def normalize(s: str) -> str:
            return jsTrim(s.replace("\n", " ").replace("\t", " "))

        match entry.get("type"):
            case "message":
                msg = entry.get("message") or {}
                role = msg.get("role")
                if role == "user":
                    content = normalize(self.extractContent(msg.get("content")))
                    result = theme.fg("accent", "user: ") + content
                elif role == "assistant":
                    textContent = normalize(self.extractContent(msg.get("content")))
                    if textContent:
                        result = theme.fg("success", "assistant: ") + textContent
                    elif msg.get("stopReason") == "aborted":
                        result = theme.fg("success", "assistant: ") + theme.fg("muted", "(aborted)")
                    elif msg.get("errorMessage"):
                        errMsg = jsSlice(normalize(msg["errorMessage"]), 0, 80)
                        result = theme.fg("success", "assistant: ") + theme.fg("error", errMsg)
                    else:
                        result = theme.fg("success", "assistant: ") + theme.fg("muted", "(no content)")
                elif role == "toolResult":
                    toolCallId = msg.get("toolCallId")
                    toolCall = self.toolCallMap.get(toolCallId) if toolCallId else None
                    if toolCall:
                        result = theme.fg("muted", self.formatToolCall(toolCall.name, toolCall.arguments))
                    else:
                        toolName = msg.get("toolName")
                        result = theme.fg("muted", f"[{toolName if toolName is not None else 'tool'}]")
                elif role == "bashExecution":
                    command = msg.get("command")
                    result = theme.fg("dim", f"[bash]: {normalize(command if command is not None else '')}")
                else:
                    result = theme.fg("dim", f"[{role}]")
            case "custom_message":
                rawContent: Any = entry.get("content")
                content = (
                    rawContent
                    if isinstance(rawContent, str)
                    else "".join(c["text"] for c in rawContent if c.get("type") == "text")
                )
                result = theme.fg("customMessageLabel", f"[{entry.get('customType')}]: ") + normalize(content)
            case "compaction":
                tokens = jsRound(entry["tokensBefore"] / 1000)
                result = theme.fg("borderAccent", f"[compaction: {tokens}k tokens]")
            case "branch_summary":
                result = theme.fg("warning", "[branch summary]: ") + normalize(entry["summary"])
            case "model_change":
                result = theme.fg("dim", f"[model: {entry.get('modelId')}]")
            case "thinking_level_change":
                result = theme.fg("dim", f"[thinking: {entry.get('thinkingLevel')}]")
            case "custom":
                result = theme.fg("dim", f"[custom: {entry.get('customType')}]")
            case "context_edit":
                result = theme.fg(
                    "dim",
                    f"[context {'omit' if entry.get('replacement') is None else 'replace'}: {entry.get('targetId')}]",
                )
            case "label":
                label = entry.get("label")
                result = theme.fg("dim", f"[label: {label if label is not None else '(cleared)'}]")
            case "session_info":
                result = (
                    "".join([theme.fg("dim", "[title: "), theme.fg("dim", entry["name"]), theme.fg("dim", "]")])
                    if entry.get("name")
                    else "".join(
                        [theme.fg("dim", "[title: "), theme.italic(theme.fg("dim", "empty")), theme.fg("dim", "]")]
                    )
                )
            case _:
                result = ""

        return theme.bold(result) if isSelected else result

    def formatLabelTimestamp(self, timestamp: str) -> str:
        date = datetime.fromisoformat(timestamp).astimezone()
        now = datetime.now().astimezone()
        hours = str(date.hour).rjust(2, "0")
        minutes = str(date.minute).rjust(2, "0")
        time = f"{hours}:{minutes}"

        if date.year == now.year and date.month == now.month and date.day == now.day:
            return time

        month = date.month
        day = date.day
        if date.year == now.year:
            return f"{month}/{day} {time}"

        year = str(date.year)[-2:]
        return f"{year}/{month}/{day} {time}"

    def extractContent(self, content: Any) -> str:
        return jsSlice(self.extractFullContent(content), 0, 200)

    def extractFullContent(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""

        result = ""
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                result += block["text"]
        return result

    def getEntryCopyText(self, node: SessionTreeNode) -> str | None:
        entry = node.entry
        text: str | None = None

        match entry.get("type"):
            case "message":
                message = entry.get("message") or {}
                if message.get("role") == "bashExecution":
                    text = message.get("command")
                elif "content" in message:
                    text = self.extractFullContent(message["content"])
                    if not text and message.get("role") == "assistant":
                        text = message.get("errorMessage")
            case "custom_message":
                text = self.extractFullContent(entry.get("content"))
            case "compaction":
                text = entry.get("summary")
            case "branch_summary":
                text = entry.get("summary")

        return text if text is not None and jsTrim(text) else None

    def hasTextContent(self, content: Any) -> bool:
        if isinstance(content, str):
            return len(jsTrim(content)) > 0
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    text = c.get("text")
                    if text and len(jsTrim(text)) > 0:
                        return True
        return False

    def formatToolCall(self, name: str, args: dict[str, Any]) -> str:
        def shortenPath(p: str) -> str:
            home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or ""
            if home and p.startswith(home):
                return f"~{p[len(home) :]}"
            return p

        match name:
            case "read":
                path = shortenPath(jsString(args.get("path") or args.get("file_path") or ""))
                return f"[read: {path}{formatReadLineRange(args.get('offset', UNDEFINED), args.get('limit', UNDEFINED))}]"
            case "write":
                path = shortenPath(jsString(args.get("path") or args.get("file_path") or ""))
                return f"[write: {path}]"
            case "edit":
                path = shortenPath(jsString(args.get("path") or args.get("file_path") or ""))
                return f"[edit: {path}]"
            case "bash":
                rawCmd = jsString(args.get("command") or "")
                cmd = jsSlice(jsTrim(rawCmd.replace("\n", " ").replace("\t", " ")), 0, 50)
                return f"[bash: {cmd}{'...' if jsLength(rawCmd) > 50 else ''}]"
            case "grep":
                pattern = jsString(args.get("pattern") or "")
                path = shortenPath(jsString(args.get("path") or "."))
                return f"[grep: /{pattern}/ in {path}]"
            case "find":
                pattern = jsString(args.get("pattern") or "")
                path = shortenPath(jsString(args.get("path") or "."))
                return f"[find: {pattern} in {path}]"
            case "ls":
                path = shortenPath(jsString(args.get("path") or "."))
                return f"[ls: {path}]"
            case _:
                # Custom tool - show name and truncated JSON args
                argsStr = jsSlice(jsonStringify(args), 0, 40)
                return f"[{name}: {argsStr}{'...' if jsLength(jsonStringify(args)) > 40 else ''}]"

    def handleInput(self, keyData: str) -> None:
        kb = getKeybindings()
        if kb.matches(keyData, "tui.select.up"):
            self.selectedIndex = len(self.filteredNodes) - 1 if self.selectedIndex == 0 else self.selectedIndex - 1
        elif kb.matches(keyData, "tui.select.down"):
            self.selectedIndex = 0 if self.selectedIndex == len(self.filteredNodes) - 1 else self.selectedIndex + 1
        elif kb.matches(keyData, "app.tree.foldOrUp"):
            selected = jsIndex(self.filteredNodes, self.selectedIndex)
            currentId = selected.node.entry["id"] if selected else None
            if currentId and self.isFoldable(currentId) and currentId not in self.foldedNodes:
                self.foldedNodes.add(currentId)
                self.applyFilter()
            else:
                self.selectedIndex = self.findBranchSegmentStart("up")
        elif kb.matches(keyData, "app.tree.unfoldOrDown"):
            selected = jsIndex(self.filteredNodes, self.selectedIndex)
            currentId = selected.node.entry["id"] if selected else None
            if currentId and currentId in self.foldedNodes:
                self.foldedNodes.discard(currentId)
                self.applyFilter()
            else:
                self.selectedIndex = self.findBranchSegmentStart("down")
        elif kb.matches(keyData, "tui.editor.cursorLeft") or kb.matches(keyData, "tui.select.pageUp"):
            # Page up
            self.selectedIndex = max(0, self.selectedIndex - self.maxVisibleLines)
        elif kb.matches(keyData, "tui.editor.cursorRight") or kb.matches(keyData, "tui.select.pageDown"):
            # Page down
            self.selectedIndex = min(len(self.filteredNodes) - 1, self.selectedIndex + self.maxVisibleLines)
        elif kb.matches(keyData, "tui.select.confirm"):
            selected = jsIndex(self.filteredNodes, self.selectedIndex)
            if selected and self.onSelect:
                self.onSelect(selected.node.entry["id"])
        elif kb.matches(keyData, "app.message.copy"):
            self.copySelected()
        elif kb.matches(keyData, "tui.select.cancel"):
            if self.searchQuery:
                self.searchQuery = ""
                self.foldedNodes.clear()
                self.applyFilter()
            elif self.onCancel:
                self.onCancel()
        elif kb.matches(keyData, "app.tree.filter.default"):
            # Direct filter: default
            self.filterMode = "default"
            self.foldedNodes.clear()
            self.applyFilter()
        elif kb.matches(keyData, "app.tree.filter.noTools"):
            # Toggle filter: no-tools ↔ default
            self.filterMode = "default" if self.filterMode == "no-tools" else "no-tools"
            self.foldedNodes.clear()
            self.applyFilter()
        elif kb.matches(keyData, "app.tree.filter.userOnly"):
            # Toggle filter: user-only ↔ default
            self.filterMode = "default" if self.filterMode == "user-only" else "user-only"
            self.foldedNodes.clear()
            self.applyFilter()
        elif kb.matches(keyData, "app.tree.filter.labeledOnly"):
            # Toggle filter: labeled-only ↔ default
            self.filterMode = "default" if self.filterMode == "labeled-only" else "labeled-only"
            self.foldedNodes.clear()
            self.applyFilter()
        elif kb.matches(keyData, "app.tree.filter.all"):
            # Toggle filter: all ↔ default
            self.filterMode = "default" if self.filterMode == "all" else "all"
            self.foldedNodes.clear()
            self.applyFilter()
        elif kb.matches(keyData, "app.tree.filter.cycleBackward"):
            # Cycle filter backwards
            modes: list[FilterMode] = ["default", "no-tools", "user-only", "labeled-only", "all"]
            currentIndex = modes.index(self.filterMode)
            self.filterMode = modes[(currentIndex - 1 + len(modes)) % len(modes)]
            self.foldedNodes.clear()
            self.applyFilter()
        elif kb.matches(keyData, "app.tree.filter.cycleForward"):
            # Cycle filter forwards: default → no-tools → user-only → labeled-only → all → default
            modes = ["default", "no-tools", "user-only", "labeled-only", "all"]
            currentIndex = modes.index(self.filterMode)
            self.filterMode = modes[(currentIndex + 1) % len(modes)]
            self.foldedNodes.clear()
            self.applyFilter()
        elif kb.matches(keyData, "tui.editor.deleteCharBackward"):
            if len(self.searchQuery) > 0:
                # MISAKA fork: drops the last code point; pi drops the last UTF-16 unit, which can leave half of a
                # surrogate pair that Python could not write to the terminal.
                self.searchQuery = self.searchQuery[:-1]
                self.foldedNodes.clear()
                self.applyFilter()
        elif kb.matches(keyData, "app.tree.editLabel"):
            selected = jsIndex(self.filteredNodes, self.selectedIndex)
            if selected and self.onLabelEdit:
                self.onLabelEdit(selected.node.entry["id"], selected.node.label)
        elif kb.matches(keyData, "app.tree.toggleLabelTimestamp"):
            self.showLabelTimestamps = not self.showLabelTimestamps
        else:
            hasControlChars = any(
                (code := ord(ch)) < 32 or code == 0x7F or 0x80 <= code <= 0x9F for ch in keyData
            )
            if not hasControlChars and len(keyData) > 0:
                self.searchQuery += keyData
                self.foldedNodes.clear()
                self.applyFilter()

    def isFoldable(self, entryId: str) -> bool:
        """Whether a node can be folded. A node is foldable if it has visible children
        and is either a root (no visible parent) or a segment start (visible parent
        has multiple visible children)."""
        children = self.visibleChildrenMap.get(entryId)
        if not children or len(children) == 0:
            return False
        parentId = self.visibleParentMap.get(entryId)
        if parentId is None:
            return True
        siblings = self.visibleChildrenMap.get(parentId)
        return siblings is not None and len(siblings) > 1

    def findBranchSegmentStart(self, direction: Literal["up", "down"]) -> int:
        """Find the index of the next branch segment start in the given direction.
        A segment start is the first child of a branch point.

        "up" walks the visible parent chain; "down" walks visible children
        (always following the first child)."""
        selected = jsIndex(self.filteredNodes, self.selectedIndex)
        selectedId = selected.node.entry["id"] if selected else None
        if not selectedId:
            return self.selectedIndex

        indexByEntryId = {node.node.entry["id"]: i for i, node in enumerate(self.filteredNodes)}
        currentId: str = selectedId
        if direction == "down":
            while True:
                children = self.visibleChildrenMap.get(currentId) or []
                if len(children) == 0:
                    return indexByEntryId[currentId]
                if len(children) > 1:
                    return indexByEntryId[children[0]]
                currentId = children[0]

        # direction === "up"
        while True:
            parentId = self.visibleParentMap.get(currentId)
            if parentId is None:
                return indexByEntryId[currentId]
            siblings = self.visibleChildrenMap.get(parentId) or []
            if len(siblings) > 1:
                segmentStart = indexByEntryId[currentId]
                if segmentStart < self.selectedIndex:
                    return segmentStart
            currentId = parentId


class SearchLine:
    """Component that displays the current search query"""

    def __init__(self, treeList: TreeList) -> None:
        self.treeList = treeList

    def invalidate(self) -> None:
        pass

    def render(self, width: int) -> list[str]:
        query = self.treeList.getSearchQuery()
        if query:
            return [truncateToWidth(f"  {theme.fg('muted', 'Type to search:')} {theme.fg('accent', query)}", width)]
        return [truncateToWidth(f"  {theme.fg('muted', 'Type to search:')}", width)]

    def handleInput(self, _keyData: str) -> None:
        pass


class TreeHelp:
    """Component that renders tree help as semantic rows with chunk-aware wrapping"""

    def invalidate(self) -> None:
        pass

    def render(self, width: int) -> list[str]:
        items: list[str] = []
        for item in TREE_HELP_ITEMS:
            text = formatHelpKeys(item.keys)
            if not text:
                items.append(item.label)
            else:
                items.append(f"{item.label} {text}" if item.labelFirst else f"{text} {item.label}")

        availableWidth = max(1, width)
        indent = "  "
        separator = " · "
        lines: list[str] = []
        currentLine = ""

        for item_ in items:
            candidate = (
                f"{currentLine}{separator}{item_}"
                if currentLine
                else (f"{indent}{item_}" if visibleWidth(f"{indent}{item_}") <= availableWidth else item_)
            )
            if not currentLine or visibleWidth(candidate) <= availableWidth:
                currentLine = candidate
                continue

            lines.extend(wrapTextWithAnsi(jsTrimEnd(currentLine), availableWidth))
            currentLine = f"{indent}{item_}" if visibleWidth(f"{indent}{item_}") <= availableWidth else item_

        if currentLine:
            lines.extend(wrapTextWithAnsi(jsTrimEnd(currentLine), availableWidth))

        return [theme.fg("muted", line) for line in lines]


@dataclass(slots=True)
class _TreeHelpItem:
    """MISAKA fork: a name for upstream's inline ``{ keys; label; labelFirst? }`` type."""

    keys: list[Keybinding]
    label: str
    labelFirst: bool = field(default=False)


TREE_HELP_ITEMS: list[_TreeHelpItem] = [
    _TreeHelpItem(keys=["tui.select.up", "tui.select.down"], label="move"),
    _TreeHelpItem(keys=["tui.editor.cursorLeft", "tui.editor.cursorRight"], label="page"),
    _TreeHelpItem(keys=["app.tree.foldOrUp", "app.tree.unfoldOrDown"], label="branch"),
    _TreeHelpItem(keys=["app.message.copy"], label="copy"),
    _TreeHelpItem(keys=["app.tree.editLabel"], label="label"),
    _TreeHelpItem(keys=["app.tree.toggleLabelTimestamp"], label="label time"),
    _TreeHelpItem(
        keys=[
            "app.tree.filter.default",
            "app.tree.filter.noTools",
            "app.tree.filter.userOnly",
            "app.tree.filter.labeledOnly",
            "app.tree.filter.all",
        ],
        label="filters",
        labelFirst=True,
    ),
    _TreeHelpItem(keys=["app.tree.filter.cycleForward", "app.tree.filter.cycleBackward"], label="cycle", labelFirst=True),
]

# JavaScript's `\b` is ASCII-only.
_PAGE_UP = re.compile(r"\bpageUp\b", re.ASCII)
_PAGE_DOWN = re.compile(r"\bpageDown\b", re.ASCII)
_UP = re.compile(r"\bup\b", re.ASCII)
_DOWN = re.compile(r"\bdown\b", re.ASCII)
_LEFT = re.compile(r"\bleft\b", re.ASCII)
_RIGHT = re.compile(r"\bright\b", re.ASCII)


def formatHelpKeys(keybindings: list[Keybinding]) -> str:
    keys: list[str] = []
    for keybinding in keybindings:
        key = jsIndex(getKeybindings().getKeys(keybinding), 0)
        if key is not None:
            keys.append(key)
    if len(keys) == 0:
        return ""

    text = formatKeyText(compactRawKeys(keys))
    text = _PAGE_UP.sub("pgup", text)
    text = _PAGE_DOWN.sub("pgdn", text)
    text = _UP.sub("↑", text)
    text = _DOWN.sub("↓", text)
    text = _LEFT.sub("←", text)
    return _RIGHT.sub("→", text)


def compactRawKeys(keys: list[str]) -> str:
    if len(keys) == 1:
        return keys[0]

    parts: list[tuple[str, str]] = []
    for key in keys:
        separatorIndex = key.rfind("+")
        parts.append(("", key) if separatorIndex == -1 else (key[: separatorIndex + 1], key[separatorIndex + 1 :]))
    prefix = parts[0][0]
    return (
        f"{prefix}{'/'.join(suffix for _prefix, suffix in parts)}"
        if prefix and all(partPrefix == prefix for partPrefix, _suffix in parts)
        else "/".join(keys)
    )


class LabelInput:
    """Label input component shown when editing a label"""

    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        self.input.focused = value

    def __init__(self, entryId: str, currentLabel: str | None) -> None:
        self.onSubmit: Callable[[str, str | None], None] | None = None
        self.onCancel: Callable[[], None] | None = None
        # Focusable implementation - propagate to input for IME cursor positioning
        self._focused = False

        self.entryId = entryId
        self.input = Input()
        if currentLabel:
            self.input.setValue(currentLabel)

    def invalidate(self) -> None:
        pass

    def render(self, width: int) -> list[str]:
        lines: list[str] = []
        indent = "  "
        availableWidth = width - len(indent)
        lines.append(truncateToWidth(f"{indent}{theme.fg('muted', 'Label (empty to remove):')}", width))
        lines.extend(truncateToWidth(f"{indent}{line}", width) for line in self.input.render(availableWidth))
        lines.append(
            truncateToWidth(
                f"{indent}{keyHint('tui.select.confirm', 'save')}  {keyHint('tui.select.cancel', 'cancel')}",
                width,
            )
        )
        return lines

    def handleInput(self, keyData: str) -> None:
        kb = getKeybindings()
        if kb.matches(keyData, "tui.select.confirm"):
            value = jsTrim(self.input.getValue())
            if self.onSubmit:
                self.onSubmit(self.entryId, value or None)
        elif kb.matches(keyData, "tui.select.cancel"):
            if self.onCancel:
                self.onCancel()
        else:
            self.input.handleInput(keyData)


class TreeSelectorComponent(Container):
    """Component that renders a session tree selector for navigation"""

    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        # Propagate to labelInput when it's active
        if self.labelInput:
            self.labelInput.focused = value

    def __init__(
        self,
        tree: list[SessionTreeNode],
        currentLeafId: str | None,
        terminalHeight: int,
        onSelect: Callable[[str], None],
        onCancel: Callable[[], None],
        onLabelChange: Callable[[str, str | None], None] | None = None,
        initialSelectedId: str | None = None,
        initialFilterMode: FilterMode | None = None,
    ) -> None:
        super().__init__()
        self.labelInput: LabelInput | None = None
        self.onCopy: Callable[[str | None], None] | None = None
        # Focusable implementation - propagate to labelInput when active for IME cursor positioning
        self._focused = False

        self.onLabelChangeCallback = onLabelChange
        maxVisibleLines = max(5, math.floor(terminalHeight / 2))

        self.treeList = TreeList(tree, currentLeafId, maxVisibleLines, initialSelectedId, initialFilterMode)
        self.treeList.onSelect = onSelect
        self.treeList.onCancel = onCancel

        def onCopy(text: str | None) -> None:
            if self.onCopy:
                self.onCopy(text)

        self.treeList.onCopy = onCopy
        self.treeList.onLabelEdit = lambda entryId, currentLabel: self.showLabelInput(entryId, currentLabel)

        self.treeContainer = Container()
        self.treeContainer.addChild(self.treeList)

        self.labelInputContainer = Container()

        self.addChild(Spacer(1))
        self.addChild(DynamicBorder())
        self.addChild(Text(theme.bold("  Session Tree"), 1, 0))
        self.addChild(TreeHelp())
        self.addChild(SearchLine(self.treeList))
        self.addChild(DynamicBorder())
        self.addChild(Spacer(1))
        self.addChild(self.treeContainer)
        self.addChild(self.labelInputContainer)
        self.addChild(Spacer(1))
        self.addChild(DynamicBorder())

        if len(tree) == 0:
            setTimeout(lambda: onCancel(), 100)

    def showLabelInput(self, entryId: str, currentLabel: str | None) -> None:
        self.labelInput = LabelInput(entryId, currentLabel)

        def onSubmit(id: str, label: str | None) -> None:
            self.treeList.updateNodeLabel(id, label)
            if self.onLabelChangeCallback:
                self.onLabelChangeCallback(id, label)
            self.hideLabelInput()

        self.labelInput.onSubmit = onSubmit
        self.labelInput.onCancel = lambda: self.hideLabelInput()

        # Propagate current focused state to the new labelInput
        self.labelInput.focused = self._focused

        self.treeContainer.clear()
        self.labelInputContainer.clear()
        self.labelInputContainer.addChild(self.labelInput)

    def hideLabelInput(self) -> None:
        self.labelInput = None
        self.labelInputContainer.clear()
        self.treeContainer.clear()
        self.treeContainer.addChild(self.treeList)

    def handleInput(self, keyData: str) -> None:
        if self.labelInput:
            self.labelInput.handleInput(keyData)
        else:
            self.treeList.handleInput(keyData)

    def getTreeList(self) -> TreeList:
        return self.treeList


__all__ = [
    "FilterMode",
    "TreeSelectorComponent",
]

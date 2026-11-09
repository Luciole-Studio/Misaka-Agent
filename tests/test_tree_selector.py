"""Translated from pi's ``coding-agent/test/tree-selector.test.ts`` (v1.1.0)."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import pytest

from misaka.core.keybindings import KeybindingsManager
from misaka.core.session_manager import SessionTreeNode
from misaka.modes.interactive.components.tree_selector import TreeSelectorComponent
from misaka.modes.interactive.theme.theme import initTheme
from misaka.ui.tui import setKeybindings, visibleWidth
from misaka.utils.ansi import stripAnsi


@pytest.fixture(autouse=True, scope="module")
def darkTheme():
    initTheme("dark")


@pytest.fixture(autouse=True)
def isolatedKeybindings():
    # Ensure test isolation: keybindings are a global singleton
    setKeybindings(KeybindingsManager())


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


_USAGE = {
    "input": 0,
    "output": 0,
    "cacheRead": 0,
    "cacheWrite": 0,
    "totalTokens": 0,
    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
}


def userMessage(id: str, parentId: str | None, content: str) -> dict[str, Any]:
    """Helper to create a user message entry"""
    return {
        "type": "message",
        "id": id,
        "parentId": parentId,
        "timestamp": _now(),
        "message": {"role": "user", "content": content, "timestamp": int(time.time() * 1000)},
    }


def assistantMessage(id: str, parentId: str | None, text: str) -> dict[str, Any]:
    """Helper to create an assistant message entry"""
    return {
        "type": "message",
        "id": id,
        "parentId": parentId,
        "timestamp": _now(),
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "api": "anthropic-messages",
            "provider": "anthropic",
            "model": "claude-sonnet-4",
            "usage": _USAGE,
            "stopReason": "stop",
            "timestamp": int(time.time() * 1000),
        },
    }


def toolCallOnlyAssistant(id: str, parentId: str | None) -> dict[str, Any]:
    """Helper to create a tool-call-only assistant message (filtered out in default mode)"""
    return {
        "type": "message",
        "id": id,
        "parentId": parentId,
        "timestamp": _now(),
        "message": {
            "role": "assistant",
            "content": [{"type": "toolCall", "id": f"tc-{id}", "name": "read", "arguments": {"path": "test.ts"}}],
            "api": "anthropic-messages",
            "provider": "anthropic",
            "model": "claude-sonnet-4",
            "usage": _USAGE,
            "stopReason": "toolUse",
            "timestamp": int(time.time() * 1000),
        },
    }


def modelChange(id: str, parentId: str | None) -> dict[str, Any]:
    """Helper to create a model_change entry"""
    return {
        "type": "model_change",
        "id": id,
        "parentId": parentId,
        "timestamp": _now(),
        "provider": "anthropic",
        "modelId": "claude-sonnet-4",
    }


def buildTree(entries: list[dict[str, Any]]) -> list[SessionTreeNode]:
    """Helper to build a tree from entries using parentId relationships"""
    if len(entries) == 0:
        return []

    nodes = [SessionTreeNode(entry=entry, children=[]) for entry in entries]

    byId = {node.entry["id"]: node for node in nodes}

    roots: list[SessionTreeNode] = []
    for node in nodes:
        if node.entry["parentId"] is None:
            roots.append(node)
        else:
            parent = byId.get(node.entry["parentId"])
            if parent:
                parent.children.append(node)
    return roots


def noop(*_args: Any) -> None:
    return None


def selectedId(selector: TreeSelectorComponent) -> str | None:
    node = selector.getTreeList().getSelectedNode()
    return node.entry["id"] if node else None


# initial selection with metadata entries


def test_focuses_nearest_visible_ancestor_when_current_leaf_is_a_model_change_with_sibling_branch():
    # Tree structure:
    # user-1
    # └── asst-1
    #     ├── user-2 (active branch)
    #     │   └── model-1 (model_change, CURRENT LEAF)
    #     └── user-3 (sibling branch, added later chronologically)
    entries = [
        userMessage("user-1", None, "hello"),
        assistantMessage("asst-1", "user-1", "hi"),
        userMessage("user-2", "asst-1", "active branch"),  # Active branch
        modelChange("model-1", "user-2"),  # Current leaf (metadata)
        userMessage("user-3", "asst-1", "sibling branch"),  # Sibling branch
    ]
    tree = buildTree(entries)

    selector = TreeSelectorComponent(tree, "model-1", 24, noop, noop)

    # Should focus on user-2 (parent of model-1), not user-3 (last item)
    assert selectedId(selector) == "user-2"


def test_hides_context_edits_by_default_and_labels_them_in_all_mode():
    entries = [
        userMessage("user-1", None, "hello"),
        assistantMessage("asst-1", "user-1", "hi"),
        {
            "type": "context_edit",
            "id": "edit-1",
            "parentId": "asst-1",
            "timestamp": _now(),
            "targetId": "asst-1",
            "replacement": None,
        },
    ]
    tree = buildTree(entries)
    defaultSelector = TreeSelectorComponent(tree, "edit-1", 24, noop, noop)
    assert selectedId(defaultSelector) == "asst-1"

    allSelector = TreeSelectorComponent(tree, "edit-1", 24, noop, noop, None, None, "all")
    rendered = "\n".join(stripAnsi(line) for line in allSelector.getTreeList().render(200))
    assert "[context omit: asst-1]" in rendered


def test_focuses_nearest_visible_ancestor_when_current_leaf_is_a_thinking_level_change_entry():
    # Similar structure with thinking_level_change instead of model_change
    entries = [
        userMessage("user-1", None, "hello"),
        assistantMessage("asst-1", "user-1", "hi"),
        userMessage("user-2", "asst-1", "active branch"),
        {
            "type": "thinking_level_change",
            "id": "thinking-1",
            "parentId": "user-2",
            "timestamp": _now(),
            "thinkingLevel": "high",
        },
        userMessage("user-3", "asst-1", "sibling branch"),
    ]
    tree = buildTree(entries)

    selector = TreeSelectorComponent(tree, "thinking-1", 24, noop, noop)

    assert selectedId(selector) == "user-2"


# filter switching with parent traversal


def _branchingEntries() -> list[dict[str, Any]]:
    return [
        userMessage("user-1", None, "hello"),
        assistantMessage("asst-1", "user-1", "hi"),
        userMessage("user-2", "asst-1", "active branch"),
        assistantMessage("asst-2", "user-2", "response"),
        userMessage("user-3", "asst-1", "sibling branch"),
    ]


def test_switches_to_nearest_visible_user_message_when_changing_to_user_only_filter():
    # In user-only filter: [user-1, user-2, user-3]
    selector = TreeSelectorComponent(buildTree(_branchingEntries()), "asst-2", 24, noop, noop)

    assert selectedId(selector) == "asst-2"

    # Simulate Ctrl+U (user-only filter)
    selector.handleInput("\x15")

    # Should now be on user-2 (the parent user message), not user-3
    assert selectedId(selector) == "user-2"


def test_returns_to_nearest_visible_ancestor_when_switching_back_to_default_filter():
    selector = TreeSelectorComponent(buildTree(_branchingEntries()), "asst-2", 24, noop, noop)

    assert selectedId(selector) == "asst-2"

    # Switch to user-only
    selector.handleInput("\x15")  # Ctrl+U
    assert selectedId(selector) == "user-2"

    # Switch back to default - should stay on user-2
    # (since that's what we navigated to via parent traversal)
    selector.handleInput("\x04")  # Ctrl+D
    assert selectedId(selector) == "user-2"


# help


def test_renders_semantic_help_rows_without_truncating_narrow_terminal_controls():
    entries = [userMessage("user-1", None, "hello"), assistantMessage("asst-1", "user-1", "hi")]
    selector = TreeSelectorComponent(buildTree(entries), "asst-1", 24, noop, noop)

    plainLines = [stripAnsi(line) for line in selector.render(30)]
    plain = "\n".join(plainLines)
    assert "branch" in plain
    assert "copy" in plain
    assert "filters" in plain
    assert "cycle" in plain
    assert "label time" in plain
    assert "..." not in plain
    assert all(visibleWidth(line) <= 30 for line in plainLines)


# copy


def test_copies_the_full_selected_message_with_ctrl_x():
    message = f"{'long message ' * 30}\nsecond line"
    tree = buildTree([userMessage("user-1", None, "hello"), assistantMessage("asst-1", "user-1", message)])
    selector = TreeSelectorComponent(tree, "asst-1", 24, noop, noop)
    copied: str | None = None

    def onCopy(text: str | None) -> None:
        nonlocal copied
        copied = text

    selector.onCopy = onCopy

    selector.handleInput("\x18")

    assert copied == message


# label timestamps


def test_toggles_label_timestamps_for_labeled_nodes():
    entries = [userMessage("user-1", None, "hello"), assistantMessage("asst-1", "user-1", "hi")]
    tree = buildTree(entries)
    labelDate = datetime(2026, 3, 28, 14, 32, 0).astimezone()
    tree[0].label = "checkpoint"
    tree[0].labelTimestamp = labelDate.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    selector = TreeSelectorComponent(tree, "asst-1", 24, noop, noop)

    list = selector.getTreeList()
    render = "\n".join(list.render(200))
    assert "[checkpoint]" in render
    assert "3/28 14:32" not in render
    assert "[+label time]" not in render

    selector.handleInput("T")

    render = "\n".join(list.render(200))
    expected = "3/28 14:32" if labelDate.year == datetime.now().year else "26/3/28 14:32"
    assert expected in render
    assert "[+label time]" in render


# empty filter preservation


def test_preserves_selection_when_switching_to_empty_labeled_filter_and_back():
    # Tree with no labels
    entries = [
        userMessage("user-1", None, "hello"),
        assistantMessage("asst-1", "user-1", "hi"),
        userMessage("user-2", "asst-1", "bye"),
        assistantMessage("asst-2", "user-2", "goodbye"),
    ]
    selector = TreeSelectorComponent(buildTree(entries), "asst-2", 24, noop, noop)

    assert selectedId(selector) == "asst-2"

    # Switch to labeled-only filter (no labels exist, so empty result)
    selector.handleInput("\x0c")  # Ctrl+L

    # The list should be empty, getSelectedNode returns undefined
    assert selector.getTreeList().getSelectedNode() is None

    # Switch back to default filter
    selector.handleInput("\x04")  # Ctrl+D

    # Should restore to asst-2 (the selection before we switched to empty filter)
    assert selectedId(selector) == "asst-2"


def test_preserves_selection_through_multiple_empty_filter_switches():
    entries = [userMessage("user-1", None, "hello"), assistantMessage("asst-1", "user-1", "hi")]
    selector = TreeSelectorComponent(buildTree(entries), "asst-1", 24, noop, noop)

    assert selectedId(selector) == "asst-1"

    # Switch to labeled-only (empty) - Ctrl+L toggles labeled ↔ default
    selector.handleInput("\x0c")  # Ctrl+L -> labeled-only
    assert selector.getTreeList().getSelectedNode() is None

    # Switch to default, then back to labeled-only
    selector.handleInput("\x0c")  # Ctrl+L -> default (toggle back)
    assert selectedId(selector) == "asst-1"

    selector.handleInput("\x0c")  # Ctrl+L -> labeled-only again
    assert selector.getTreeList().getSelectedNode() is None

    # Switch back to default with Ctrl+D
    selector.handleInput("\x04")  # Ctrl+D
    assert selectedId(selector) == "asst-1"


# branch navigation and folding with ctrl+arrow keys

# Key escape sequences
UP = "\x1b[A"
DOWN = "\x1b[B"
CTRL_LEFT = "\x1b[1;5D"
CTRL_RIGHT = "\x1b[1;5C"
ALT_LEFT = "\x1b[1;3D"
ALT_RIGHT = "\x1b[1;3C"


def buildBranchingTree() -> list[SessionTreeNode]:
    """Tree structure:

    user-1
    asst-1
    user-2
    asst-2          ← branch point (has 2 children)
    ├─ user-3a      ← branch A (active: leaf is asst-4a)
    │  asst-3a
    │  user-4a
    │  asst-4a
    └─ user-3b      ← branch B
       asst-3b
       user-4b

    Foldable nodes: user-1 (root), user-3a (segment start), user-3b (segment start)"""
    entries = [
        userMessage("user-1", None, "first message"),
        assistantMessage("asst-1", "user-1", "response 1"),
        userMessage("user-2", "asst-1", "second message"),
        assistantMessage("asst-2", "user-2", "response 2"),
        # Branch A (active)
        userMessage("user-3a", "asst-2", "branch A start"),
        assistantMessage("asst-3a", "user-3a", "branch A response"),
        userMessage("user-4a", "asst-3a", "branch A deep"),
        assistantMessage("asst-4a", "user-4a", "branch A leaf"),
        # Branch B
        userMessage("user-3b", "asst-2", "branch B start"),
        assistantMessage("asst-3b", "user-3b", "branch B response"),
        userMessage("user-4b", "asst-3b", "branch B deep"),
    ]
    return buildTree(entries)


def test_ctrl_right_unfolds_a_folded_node_then_does_segment_jump_when_unfolded():
    selector = TreeSelectorComponent(buildBranchingTree(), "asst-4a", 24, noop, noop)

    selector.handleInput(CTRL_LEFT)  # asst-4a → user-3a
    assert selectedId(selector) == "user-3a"

    selector.handleInput(CTRL_LEFT)  # fold user-3a
    assert selectedId(selector) == "user-3a"

    selector.handleInput(DOWN)  # user-3a → user-3b (children hidden)
    assert selectedId(selector) == "user-3b"

    selector.handleInput(UP)  # user-3b → user-3a
    assert selectedId(selector) == "user-3a"

    selector.handleInput(CTRL_RIGHT)  # unfold user-3a
    assert selectedId(selector) == "user-3a"

    selector.handleInput(DOWN)  # user-3a → asst-3a (children restored)
    assert selectedId(selector) == "asst-3a"

    selector.handleInput(CTRL_LEFT)  # asst-3a → user-3a
    assert selectedId(selector) == "user-3a"

    selector.handleInput(CTRL_RIGHT)  # user-3a → asst-4a (segment jump to leaf)
    assert selectedId(selector) == "asst-4a"


def test_alt_left_right_are_aliases_for_fold_and_unfold_navigation():
    selector = TreeSelectorComponent(buildBranchingTree(), "asst-4a", 24, noop, noop)

    selector.handleInput(ALT_LEFT)  # asst-4a → user-3a
    assert selectedId(selector) == "user-3a"

    selector.handleInput(ALT_LEFT)  # fold user-3a
    assert selectedId(selector) == "user-3a"

    selector.handleInput(ALT_RIGHT)  # unfold user-3a
    assert selectedId(selector) == "user-3a"

    selector.handleInput(ALT_RIGHT)  # user-3a → asst-4a
    assert selectedId(selector) == "asst-4a"


def test_folding_root_hides_entire_subtree_nested_fold_preserved_on_unfold():
    selector = TreeSelectorComponent(buildBranchingTree(), "asst-4a", 24, noop, noop)

    selector.handleInput(CTRL_LEFT)  # asst-4a → user-3a
    assert selectedId(selector) == "user-3a"

    selector.handleInput(CTRL_LEFT)  # fold user-3a
    assert selectedId(selector) == "user-3a"

    selector.handleInput(CTRL_LEFT)  # user-3a (folded) → user-1
    assert selectedId(selector) == "user-1"

    selector.handleInput(CTRL_LEFT)  # fold user-1
    assert selectedId(selector) == "user-1"

    selector.handleInput(DOWN)  # wrap (only visible node)
    assert selectedId(selector) == "user-1"

    selector.handleInput(CTRL_RIGHT)  # unfold user-1
    assert selectedId(selector) == "user-1"

    selector.handleInput(CTRL_RIGHT)  # user-1 → user-3a (segment jump, user-3a still folded)
    assert selectedId(selector) == "user-3a"

    selector.handleInput(DOWN)  # user-3a → user-3b (user-3a still folded)
    assert selectedId(selector) == "user-3b"


def test_fold_and_navigate_on_non_active_branch():
    selector = TreeSelectorComponent(buildBranchingTree(), "asst-4a", 24, noop, noop)

    # Navigate down to user-3b (branch B)
    found = False
    for _ in range(20):
        selector.handleInput(DOWN)
        if selectedId(selector) == "user-3b":
            found = True
            break
    assert found is True

    selector.handleInput(CTRL_RIGHT)  # user-3b → user-4b (segment jump to leaf)
    assert selectedId(selector) == "user-4b"

    selector.handleInput(CTRL_LEFT)  # user-4b → user-3b
    assert selectedId(selector) == "user-3b"

    selector.handleInput(CTRL_LEFT)  # fold user-3b
    assert selectedId(selector) == "user-3b"

    selector.handleInput(CTRL_LEFT)  # user-3b (folded) → user-1
    assert selectedId(selector) == "user-1"


def test_fold_and_navigate_with_multiple_roots():
    entries = [
        userMessage("user-1", None, "first root"),
        assistantMessage("asst-1", "user-1", "response 1"),
        userMessage("user-2", None, "second root"),
        assistantMessage("asst-2", "user-2", "response 2"),
    ]
    selector = TreeSelectorComponent(buildTree(entries), "asst-1", 24, noop, noop)

    assert selectedId(selector) == "asst-1"

    selector.handleInput(CTRL_LEFT)  # asst-1 → user-1
    assert selectedId(selector) == "user-1"

    selector.handleInput(CTRL_LEFT)  # fold user-1
    assert selectedId(selector) == "user-1"

    selector.handleInput(DOWN)  # user-1 → user-2 (children hidden)
    assert selectedId(selector) == "user-2"

    selector.handleInput(CTRL_RIGHT)  # user-2 → asst-2 (segment jump to leaf)
    assert selectedId(selector) == "asst-2"

    selector.handleInput(CTRL_LEFT)  # asst-2 → user-2
    assert selectedId(selector) == "user-2"

    selector.handleInput(CTRL_LEFT)  # fold user-2
    assert selectedId(selector) == "user-2"

    selector.handleInput(CTRL_LEFT)  # user-2 (folded, root) → stays on user-2
    assert selectedId(selector) == "user-2"


def test_folding_root_hides_descendants_even_when_intermediate_nodes_are_filtered_out():
    # user-1 → toolCallOnly-1 (filtered out) → user-2 → asst-2
    entries = [
        userMessage("user-1", None, "hello"),
        toolCallOnlyAssistant("tool-asst-1", "user-1"),
        userMessage("user-2", "tool-asst-1", "follow up"),
        assistantMessage("asst-2", "user-2", "response"),
    ]
    selector = TreeSelectorComponent(buildTree(entries), "asst-2", 24, noop, noop)

    selector.handleInput(CTRL_LEFT)  # asst-2 → user-1
    assert selectedId(selector) == "user-1"

    selector.handleInput(CTRL_LEFT)  # fold user-1
    assert selectedId(selector) == "user-1"

    selector.handleInput(DOWN)  # wrap (only visible node)
    assert selectedId(selector) == "user-1"


def test_search_resets_fold_state():
    selector = TreeSelectorComponent(buildBranchingTree(), "asst-4a", 24, noop, noop)

    selector.handleInput(CTRL_LEFT)  # asst-4a → user-3a
    selector.handleInput(CTRL_LEFT)  # fold user-3a

    selector.handleInput(DOWN)  # user-3a → user-3b (children hidden)
    assert selectedId(selector) == "user-3b"

    selector.handleInput("b")  # search resets folds
    selector.handleInput("\x1b")  # clear search

    # Navigate to user-3a to verify fold was reset
    currentId = ""
    for _ in range(20):
        selector.handleInput(DOWN)
        currentId = selectedId(selector) or ""
        if currentId == "user-3a":
            break
    assert currentId == "user-3a"

    selector.handleInput(DOWN)  # user-3a → asst-3a (not user-3b)
    assert selectedId(selector) == "asst-3a"


def test_filter_mode_change_resets_fold_state():
    selector = TreeSelectorComponent(buildBranchingTree(), "asst-4a", 24, noop, noop)

    selector.handleInput(CTRL_LEFT)  # asst-4a → user-3a
    selector.handleInput(CTRL_LEFT)  # fold user-3a

    selector.handleInput("\x15")  # ctrl+u: user-only filter resets folds
    selector.handleInput("\x04")  # ctrl+d: back to default

    # Navigate to user-3a to verify fold was reset
    currentId = ""
    for _ in range(20):
        selector.handleInput(DOWN)
        currentId = selectedId(selector) or ""
        if currentId == "user-3a":
            break
    assert currentId == "user-3a"

    selector.handleInput(DOWN)  # user-3a → asst-3a (not user-3b)
    assert selectedId(selector) == "asst-3a"

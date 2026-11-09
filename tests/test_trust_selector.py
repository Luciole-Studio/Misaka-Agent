"""Translated from pi's ``coding-agent/test/trust-selector.test.ts`` (v1.1.0).

Upstream writes POSIX paths; ``norm`` resolves them the way the trust store does, so the expectations hold on Windows
too (``/project`` is ``C:\\project`` there).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from misaka.core.keybindings import KeybindingsManager
from misaka.core.trust_manager import ProjectTrustStoreEntry, ProjectTrustUpdate
from misaka.modes.interactive.components.trust_selector import (
    TrustSelection,
    TrustSelectorComponent,
    TrustSelectorOptions,
)
from misaka.modes.interactive.theme.theme import initTheme
from misaka.ui.tui import setKeybindings
from misaka.utils.ansi import stripAnsi
from misaka.utils.paths import canonicalizePath, resolvePath


def norm(path: str) -> str:
    return canonicalizePath(resolvePath(path))


@pytest.fixture(autouse=True, scope="module")
def darkTheme():
    initTheme("dark")


@pytest.fixture(autouse=True)
def keybindings():
    setKeybindings(KeybindingsManager())


def test_keeps_the_saved_trusted_decision_marked_while_browsing():
    project = norm("/project")
    selector = TrustSelectorComponent(
        TrustSelectorOptions(
            cwd=project,
            savedDecision=ProjectTrustStoreEntry(path=project, decision=True),
            projectTrusted=True,
            onSelect=lambda _selection: None,
            onCancel=lambda: None,
        )
    )

    output = stripAnsi("\n".join(selector.render(120)))
    assert f"Saved decision: trusted ({project})" in output
    assert "Current session: trusted" in output
    assert "→ ✓ Trust" in output

    selector.handleInput("\x1b[B")
    output = stripAnsi("\n".join(selector.render(120)))
    assert "✓ Trust" in output
    assert f"→   Trust parent folder ({norm('/')})" in output
    assert "✓ Do not trust" not in output


def test_selects_a_trust_decision():
    project = norm("/project")
    onSelect = MagicMock()
    selector = TrustSelectorComponent(
        TrustSelectorOptions(cwd=project, savedDecision=None, projectTrusted=False, onSelect=onSelect, onCancel=lambda: None)
    )

    selector.handleInput("\n")

    onSelect.assert_called_with(TrustSelection(trusted=True, updates=[ProjectTrustUpdate(path=project, decision=True)]))


def test_labels_saved_ancestor_decisions_as_inherited():
    parent = norm("/parent")
    selector = TrustSelectorComponent(
        TrustSelectorOptions(
            cwd=norm("/parent/project/nested"),
            savedDecision=ProjectTrustStoreEntry(path=parent, decision=True),
            projectTrusted=True,
            onSelect=lambda _selection: None,
            onCancel=lambda: None,
        )
    )

    output = stripAnsi("\n".join(selector.render(120)))

    assert f"Saved decision: trusted (inherited from {parent})" in output


def test_adds_a_trust_parent_option():
    parent = norm("/parent")
    project = norm("/parent/project")
    onSelect = MagicMock()
    selector = TrustSelectorComponent(
        TrustSelectorOptions(
            cwd=project,
            savedDecision=ProjectTrustStoreEntry(path=parent, decision=True),
            projectTrusted=True,
            onSelect=onSelect,
            onCancel=lambda: None,
        )
    )

    output = stripAnsi("\n".join(selector.render(120)))
    assert f"Saved decision: trusted (inherited from {parent})" in output
    assert f"✓ Trust parent folder ({parent})" in output

    selector.handleInput("\n")

    onSelect.assert_called_with(
        TrustSelection(
            trusted=True,
            updates=[
                ProjectTrustUpdate(path=parent, decision=True),
                ProjectTrustUpdate(path=project, decision=None),
            ],
        )
    )

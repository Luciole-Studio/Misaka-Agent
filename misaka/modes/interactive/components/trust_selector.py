"""Translated from pi's ``modes/interactive/components/trust-selector.ts`` (v1.1.0)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from misaka.core.trust_manager import (
    ProjectTrustOption,
    ProjectTrustStoreEntry,
    ProjectTrustUpdate,
    getProjectTrustOptions,
)
from misaka.modes.interactive.theme.theme import theme
from misaka.ui.tui import Container, Spacer, Text, getKeybindings
from misaka.utils.js import jsIndex

from .dynamic_border import DynamicBorder
from .keybinding_hints import keyHint, rawKeyHint


@dataclass(frozen=True, slots=True)
class TrustSelection:
    """``Pick<ProjectTrustOption, "trusted" | "updates">``."""

    trusted: bool
    updates: list[ProjectTrustUpdate]


@dataclass(slots=True)
class TrustSelectorOptions:
    cwd: str
    savedDecision: ProjectTrustStoreEntry | None
    projectTrusted: bool
    onSelect: Callable[[TrustSelection], None]
    onCancel: Callable[[], None]


def formatDecision(trustPath: str | None, decision: ProjectTrustStoreEntry | None) -> str:
    if decision is None:
        return "none"
    label = "trusted" if decision.decision else "untrusted"
    if trustPath is not None and decision.path != trustPath:
        return f"{label} (inherited from {decision.path})"
    return f"{label} ({decision.path})"


class TrustSelectorComponent(Container):
    def __init__(self, options: TrustSelectorOptions) -> None:
        super().__init__()

        self.savedDecision = options.savedDecision
        self.trustOptions: list[ProjectTrustOption] = getProjectTrustOptions(options.cwd)
        self.selectedIndex = max(
            0,
            next((index for index, option in enumerate(self.trustOptions) if self.isSavedOption(option)), -1),
        )
        self.onSelectCallback = options.onSelect
        self.onCancelCallback = options.onCancel

        self.addChild(DynamicBorder())
        self.addChild(Spacer(1))
        self.addChild(Text(theme.fg("accent", theme.bold("Project trust")), 1, 0))
        self.addChild(Text(theme.fg("muted", options.cwd), 1, 0))
        self.addChild(Spacer(1))
        firstOption = jsIndex(self.trustOptions, 0)
        self.addChild(
            Text(
                theme.fg(
                    "muted",
                    f"Saved decision: {formatDecision(firstOption.savedPath if firstOption else None, options.savedDecision)}",
                ),
                1,
                0,
            )
        )
        self.addChild(
            Text(theme.fg("muted", f"Current session: {'trusted' if options.projectTrusted else 'untrusted'}"), 1, 0)
        )
        self.addChild(Spacer(1))

        self.listContainer = Container()
        self.addChild(self.listContainer)
        self.addChild(Spacer(1))
        self.addChild(
            Text(
                rawKeyHint("↑↓", "navigate")
                + "  "
                + keyHint("tui.select.confirm", "save")
                + "  "
                + keyHint("tui.select.cancel", "cancel"),
                1,
                0,
            )
        )
        self.addChild(Spacer(1))
        self.addChild(DynamicBorder())

        self.updateList()

    def isSavedOption(self, option: ProjectTrustOption) -> bool:
        return (
            option.savedPath is not None
            and self.savedDecision is not None
            and self.savedDecision.decision == option.trusted
            and self.savedDecision.path == option.savedPath
        )

    def updateList(self) -> None:
        self.listContainer.clear()
        for i in range(len(self.trustOptions)):
            option = self.trustOptions[i]
            if not option:
                continue

            isSelected = i == self.selectedIndex
            isCurrent = self.isSavedOption(option)
            currentMarker = theme.fg("accent", "✓ ") if isCurrent else "  "
            prefix = theme.fg("accent", "→ ") if isSelected else "  "
            label = theme.fg("accent", option.label) if isSelected else theme.fg("text", option.label)
            self.listContainer.addChild(Text(f"{prefix}{currentMarker}{label}", 1, 0))

    def handleInput(self, keyData: str) -> None:
        kb = getKeybindings()
        if kb.matches(keyData, "tui.select.up") or keyData == "k":
            self.selectedIndex = max(0, self.selectedIndex - 1)
            self.updateList()
        elif kb.matches(keyData, "tui.select.down") or keyData == "j":
            self.selectedIndex = min(len(self.trustOptions) - 1, self.selectedIndex + 1)
            self.updateList()
        elif kb.matches(keyData, "tui.select.confirm") or keyData == "\n":
            selected = jsIndex(self.trustOptions, self.selectedIndex)
            if selected:
                self.onSelectCallback(TrustSelection(trusted=selected.trusted, updates=list(selected.updates)))
        elif kb.matches(keyData, "tui.select.cancel"):
            self.onCancelCallback()


__all__ = [
    "TrustSelection",
    "TrustSelectorComponent",
    "TrustSelectorOptions",
]

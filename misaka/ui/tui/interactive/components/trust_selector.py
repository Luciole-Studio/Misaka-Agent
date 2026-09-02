"""Project-trust decision selector."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from misaka.core.project_trust import (
    ProjectTrustOption,
    ProjectTrustStoreEntry,
    ProjectTrustUpdate,
    get_project_trust_options,
)
from misaka.ui.tui import Container, Spacer, Text, getKeybindings
from misaka.ui.tui.interactive.theme.theme import theme

from .dynamic_border import DynamicBorder
from .keybinding_hints import key_hint, raw_key_hint


@dataclass(frozen=True, slots=True)
class TrustSelection:
    trusted: bool
    updates: list[ProjectTrustUpdate]


@dataclass(slots=True)
class TrustSelectorOptions:
    cwd: str
    savedDecision: ProjectTrustStoreEntry | None
    projectTrusted: bool
    onSelect: Callable[[TrustSelection], None]
    onCancel: Callable[[], None]


def _format_decision(
    trust_path: str | None,
    decision: ProjectTrustStoreEntry | None,
) -> str:
    if decision is None:
        return "none"
    label = "trusted" if decision.decision else "untrusted"
    if trust_path is not None and decision.path != trust_path:
        return f"{label} (inherited from {decision.path})"
    return f"{label} ({decision.path})"


class TrustSelectorComponent(Container):
    def __init__(self, options: TrustSelectorOptions) -> None:
        super().__init__()
        self.savedDecision = options.savedDecision
        self.trustOptions = get_project_trust_options(options.cwd)
        self.selectedIndex = max(
            0,
            next(
                (
                    index
                    for index, option in enumerate(self.trustOptions)
                    if self._is_saved_option(option)
                ),
                -1,
            ),
        )
        self.onSelectCallback = options.onSelect
        self.onCancelCallback = options.onCancel

        self.addChild(DynamicBorder())
        self.addChild(Spacer(1))
        self.addChild(Text(theme.fg("accent", theme.bold("Project trust")), 1, 0))
        self.addChild(Text(theme.fg("muted", options.cwd), 1, 0))
        self.addChild(Spacer(1))
        trust_path = self.trustOptions[0].savedPath if self.trustOptions else None
        self.addChild(
            Text(
                theme.fg(
                    "muted",
                    f"Saved decision: {_format_decision(trust_path, options.savedDecision)}",
                ),
                1,
                0,
            )
        )
        current = "trusted" if options.projectTrusted else "untrusted"
        self.addChild(Text(theme.fg("muted", f"Current session: {current}"), 1, 0))
        self.addChild(Spacer(1))

        self.listContainer = Container()
        self.addChild(self.listContainer)
        self.addChild(Spacer(1))
        self.addChild(
            Text(
                raw_key_hint("↑↓", "navigate")
                + "  "
                + key_hint("tui.select.confirm", "save")
                + "  "
                + key_hint("tui.select.cancel", "cancel"),
                1,
                0,
            )
        )
        self.addChild(Spacer(1))
        self.addChild(DynamicBorder())
        self._update_list()

    def _is_saved_option(self, option: ProjectTrustOption) -> bool:
        return (
            option.savedPath is not None
            and self.savedDecision is not None
            and self.savedDecision.decision == option.trusted
            and self.savedDecision.path == option.savedPath
        )

    def _update_list(self) -> None:
        self.listContainer.clear()
        for index, option in enumerate(self.trustOptions):
            selected = index == self.selectedIndex
            current = self._is_saved_option(option)
            checkmark = theme.fg("success", " ✓") if current else ""
            prefix = theme.fg("accent", "→ ") if selected else "  "
            label = (
                theme.fg("accent", option.label)
                if selected
                else theme.fg("text", option.label)
            )
            self.listContainer.addChild(Text(f"{prefix}{label}{checkmark}", 1, 0))

    def handleInput(self, keyData: str) -> None:
        kb = getKeybindings()
        if kb.matches(keyData, "tui.select.up") or keyData == "k":
            self.selectedIndex = max(0, self.selectedIndex - 1)
            self._update_list()
        elif kb.matches(keyData, "tui.select.down") or keyData == "j":
            self.selectedIndex = min(len(self.trustOptions) - 1, self.selectedIndex + 1)
            self._update_list()
        elif kb.matches(keyData, "tui.select.confirm") or keyData == "\n":
            if 0 <= self.selectedIndex < len(self.trustOptions):
                selected = self.trustOptions[self.selectedIndex]
                self.onSelectCallback(
                    TrustSelection(
                        trusted=selected.trusted,
                        updates=list(selected.updates),
                    )
                )
        elif kb.matches(keyData, "tui.select.cancel"):
            self.onCancelCallback()


__all__ = [
    "TrustSelection",
    "TrustSelectorComponent",
    "TrustSelectorOptions",
]

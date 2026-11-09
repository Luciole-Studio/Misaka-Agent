"""Translated from pi's ``modes/interactive/components/settings-submenu.ts`` (v1.1.0)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypedDict

from misaka.modes.interactive.theme.theme import getSelectListTheme, theme
from misaka.ui.tui import (
    Container,
    Input,
    SelectItem,
    SelectList,
    SelectListLayoutOptions,
    Spacer,
    Text,
    fuzzyFilter,
    getKeybindings,
)

SUBMENU_SELECT_LIST_LAYOUT = SelectListLayoutOptions(
    minPrimaryColumnWidth=12,
    maxPrimaryColumnWidth=32,
)


class SelectSubmenuOptions(TypedDict, total=False):
    # Enable type-to-search fuzzy filtering.
    searchable: bool | None
    # Override the select list layout (column widths).
    layout: SelectListLayoutOptions | None


class SelectSubmenu(Container):
    """Single-step submenu that shows a titled select list.
    With `searchable: true`, typing filters the list using fuzzy matching."""

    def __init__(
        self,
        title: str,
        description: str,
        options: list[SelectItem],
        currentValue: str,
        onSelect: Callable[[str], None],
        onCancel: Callable[[], None],
        onSelectionChange: Callable[[str], None] | None = None,
        submenuOptions: SelectSubmenuOptions | None = None,
    ) -> None:
        super().__init__()
        self.searchInput: Input | None = None

        self.allOptions = options
        layout = submenuOptions.get("layout") if submenuOptions else None
        self.listLayout: SelectListLayoutOptions = layout if layout is not None else SUBMENU_SELECT_LIST_LAYOUT
        self.onSelectCb = onSelect
        self.onCancelCb = onCancel
        self.onSelectionChangeCb = onSelectionChange

        # Title
        self.addChild(Text(theme.bold(theme.fg("accent", title)), 0, 0))

        # Description
        if description:
            self.addChild(Spacer(1))
            self.addChild(Text(theme.fg("muted", description), 0, 0))

        # Search input
        searchable = submenuOptions.get("searchable") if submenuOptions else None
        if searchable:
            self.addChild(Spacer(1))
            self.searchInput = Input()

            def onSubmit(_value: str) -> None:
                self.selectList.handleInput("\r")

            self.searchInput.onSubmit = onSubmit
            self.addChild(self.searchInput)

        # Spacer
        self.addChild(Spacer(1))

        # Select list
        self.selectList = self.buildSelectList(options, currentValue)
        self.listChildIndex = len(self.children)
        self.addChild(self.selectList)

        # Hint
        self.addChild(Spacer(1))
        hint = (
            "  Type to filter · Enter to select · Esc to go back"
            if searchable
            else "  Enter to select · Esc to go back"
        )
        self.addChild(Text(theme.fg("dim", hint), 0, 0))

    def buildSelectList(self, options: list[SelectItem], preselect: str) -> SelectList:
        list = SelectList(options, min(len(options), 10), getSelectListTheme(), self.listLayout)

        idx = next((index for index, o in enumerate(options) if o.value == preselect), -1)
        if idx != -1:
            list.setSelectedIndex(idx)

        list.onSelect = lambda item: self.onSelectCb(item.value)
        list.onCancel = self.onCancelCb
        if self.onSelectionChangeCb:
            cb = self.onSelectionChangeCb
            list.onSelectionChange = lambda item: cb(item.value)

        return list

    def applyFilter(self, query: str) -> None:
        filtered = (
            fuzzyFilter(
                self.allOptions,
                query,
                lambda item: f"{item.label} {item.description if item.description is not None else ''}",
            )
            if query
            else self.allOptions
        )

        newList = self.buildSelectList(filtered, "")
        self.children[self.listChildIndex] = newList
        self.selectList = newList

    def handleInput(self, data: str) -> None:
        if self.searchInput:
            kb = getKeybindings()
            isNav = (
                kb.matches(data, "tui.select.up")
                or kb.matches(data, "tui.select.down")
                or kb.matches(data, "tui.select.confirm")
                or kb.matches(data, "tui.select.cancel")
            )
            if isNav:
                self.selectList.handleInput(data)
            else:
                self.searchInput.handleInput(data)
                self.applyFilter(self.searchInput.getValue())
        else:
            self.selectList.handleInput(data)


# ============================================================================
# SteppedSubmenu — reusable multi-step selector
# ============================================================================


@dataclass(slots=True, kw_only=True)
class SteppedSubmenuStep:
    """One step in a `SteppedSubmenu`."""

    # Unique key — the selected value is stored in the result context under this key.
    key: str
    # Title shown at the top of the step. Receives prior selections.
    title: str | Callable[[dict[str, str]], str]
    # Description shown below the title. Receives prior selections.
    description: str | Callable[[dict[str, str]], str]
    # Build the option list for this step. Called fresh each time the step is shown.
    options: Callable[[dict[str, str]], list[SelectItem]]
    # Optionally pre-select a value when entering this step.
    preselect: Callable[[dict[str, str]], str | None] | None = None
    # Enable type-to-search fuzzy filtering for this step.
    searchable: bool | None = None
    # Override the select list layout (column widths) for this step.
    layout: SelectListLayoutOptions | None = None


class SteppedSubmenuOptions(TypedDict, total=False):
    # Start at this step index (0-based), skipping earlier steps. Requires initialContext for skipped keys.
    startAtStep: int
    # Pre-fill selections for skipped steps.
    initialContext: dict[str, str]
    # After completing the last step, loop back to step 0 instead of closing.
    loop: bool


class SteppedSubmenu(Container):
    """Generic N-step submenu built on top of `SelectSubmenu`.

    Each step's options can depend on prior selections via the shared context.
    Esc goes back one step; Esc at step 0 cancels.
    With `loop: true`, completing the final step invokes `onComplete` then returns to step 0."""

    def __init__(
        self,
        steps: list[SteppedSubmenuStep],
        onComplete: Callable[[dict[str, str]], None],
        onCancel: Callable[[], None],
        opts: SteppedSubmenuOptions | None = None,
    ) -> None:
        super().__init__()
        if opts is None:
            opts = {}
        self.steps = steps
        self.onComplete = onComplete
        self.onCancel = onCancel
        self.opts = opts
        initialContext = opts.get("initialContext")
        self.context: dict[str, str] = {**(initialContext if initialContext is not None else {})}
        startAtStep = opts.get("startAtStep")
        self.activeComponent: Any = self.buildStep(startAtStep if startAtStep is not None else 0)

    def buildStep(self, stepIndex: int) -> SelectSubmenu:
        step = self.steps[stepIndex]
        total = len(self.steps)
        stepLabel = f"Step {stepIndex + 1}/{total} · " if total > 1 else ""

        title = step.title(self.context) if callable(step.title) else step.title
        desc = step.description(self.context) if callable(step.description) else step.description
        items = step.options(self.context)
        preselected = step.preselect(self.context) if step.preselect else None
        preselect = preselected if preselected is not None else ""

        def onSelect(value: str) -> None:
            self.context[step.key] = value

            if stepIndex < total - 1:
                # Advance to next step
                self.activeComponent = self.buildStep(stepIndex + 1)
            else:
                # Final step — deliver result
                self.onComplete({**self.context})

                if self.opts.get("loop"):
                    self.context = {}
                    self.activeComponent = self.buildStep(0)
                else:
                    self.onCancel()

        def onCancel() -> None:
            if stepIndex > 0:
                self.context.pop(step.key, None)
                self.activeComponent = self.buildStep(stepIndex - 1)
            else:
                self.onCancel()

        return SelectSubmenu(
            title,
            f"{stepLabel}{desc}",
            items,
            preselect,
            onSelect,
            onCancel,
            None,
            {"searchable": step.searchable, "layout": step.layout} if step.searchable or step.layout else None,
        )

    def render(self, width: int) -> list[str]:
        return self.activeComponent.render(width)

    def handleInput(self, data: str) -> None:
        handleInput = getattr(self.activeComponent, "handleInput", None)
        if handleInput is not None:
            handleInput(data)

    def invalidate(self) -> None:
        invalidate = getattr(self.activeComponent, "invalidate", None)
        if invalidate is not None:
            invalidate()


__all__ = [
    "SUBMENU_SELECT_LIST_LAYOUT",
    "SelectSubmenu",
    "SelectSubmenuOptions",
    "SteppedSubmenu",
    "SteppedSubmenuOptions",
    "SteppedSubmenuStep",
]

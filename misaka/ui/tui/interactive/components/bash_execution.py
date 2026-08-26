"""Streaming bash-execution renderer for interactive mode."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from misaka.core.tools.truncate import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    TruncationOptions,
    TruncationResult,
    truncate_tail,
)
from misaka.ui.tui import Container, Loader, Spacer, Text
from misaka.ui.tui.interactive.components.dynamic_border import DynamicBorder
from misaka.ui.tui.interactive.components.keybinding_hints import key_hint, key_text
from misaka.ui.tui.interactive.components.visual_truncate import (
    truncate_to_visual_lines,
)
from misaka.ui.tui.interactive.theme.theme import theme
from misaka.utils.ansi import strip_ansi

PREVIEW_LINES = 20


@dataclass(slots=True)
class _CachedVisualLines:
    renderer: Any
    cachedWidth: int | None = None
    cachedLines: list[str] | None = None

    def invalidate(self) -> None:
        self.cachedWidth = None
        self.cachedLines = None

    def render(self, width: int) -> list[str]:
        if self.cachedLines is None or self.cachedWidth != width:
            self.cachedLines = list(self.renderer(width))
            self.cachedWidth = width
        return self.cachedLines


class BashExecutionComponent(Container):
    def __init__(self, command: str, ui: Any, excludeFromContext: bool = False) -> None:
        super().__init__()
        self.command = command
        self.outputLines: list[str] = []
        self.status: str = "running"
        self.exitCode: int | None = None
        self.truncationResult: TruncationResult | None = None
        self.fullOutputPath: str | None = None
        self.expanded = False
        self.ui = ui
        self.colorKey = "dim" if excludeFromContext else "bashMode"
        self.borderColor = lambda text: theme.fg(self.colorKey, text)

        self.addChild(Spacer(1))
        self.addChild(DynamicBorder(self.borderColor))
        self.contentContainer = Container()
        self.addChild(self.contentContainer)
        self.loader = Loader(
            ui,
            lambda spinner: theme.fg(self.colorKey, spinner),
            lambda text: theme.fg("muted", text),
            f"Running... ({key_text('tui.select.cancel')} to cancel)",
        )
        self.addChild(DynamicBorder(self.borderColor))
        self.updateDisplay()

    def setExpanded(self, expanded: bool) -> None:
        self.expanded = expanded
        self.updateDisplay()

    def invalidate(self) -> None:
        super().invalidate()
        self.updateDisplay()

    def appendOutput(self, chunk: str) -> None:
        clean = strip_ansi(chunk).replace("\r\n", "\n").replace("\r", "\n")
        new_lines = clean.split("\n")
        if self.outputLines and new_lines:
            self.outputLines[-1] += new_lines[0]
            self.outputLines.extend(new_lines[1:])
        else:
            self.outputLines.extend(new_lines)
        self.updateDisplay()

    def setComplete(
        self,
        exitCode: int | None,
        cancelled: bool,
        truncationResult: TruncationResult | None = None,
        fullOutputPath: str | None = None,
    ) -> None:
        self.exitCode = exitCode
        self.status = "cancelled" if cancelled else "error" if exitCode not in {0, None} else "complete"
        self.truncationResult = truncationResult
        self.fullOutputPath = fullOutputPath
        self.loader.stop()
        self.updateDisplay()

    def getOutput(self) -> str:
        return "\n".join(self.outputLines)

    def getCommand(self) -> str:
        return self.command

    def updateDisplay(self) -> None:
        full_output = self.getOutput()
        context_truncation = truncate_tail(
            full_output,
            TruncationOptions(maxLines=DEFAULT_MAX_LINES, maxBytes=DEFAULT_MAX_BYTES),
        )
        available_lines = context_truncation.content.split("\n") if context_truncation.content else []
        preview_logical_lines = available_lines[-PREVIEW_LINES:]
        hidden_line_count = len(available_lines) - len(preview_logical_lines)

        self.contentContainer.clear()
        self.contentContainer.addChild(Text(theme.fg("bashMode", theme.bold(f"$ {self.command}")), 1, 0))

        if available_lines:
            if self.expanded:
                display_text = "\n".join(theme.fg("muted", line) for line in available_lines)
                self.contentContainer.addChild(Text(f"\n{display_text}", 1, 0))
            else:
                styled_output = "\n".join(theme.fg("muted", line) for line in preview_logical_lines)
                styled_input = f"\n{styled_output}"
                self.contentContainer.addChild(
                    _CachedVisualLines(
                        lambda width: truncate_to_visual_lines(styled_input, PREVIEW_LINES, width, 1).visualLines
                    )
                )

        if self.status == "running":
            self.contentContainer.addChild(self.loader)
            return

        status_parts: list[str] = []
        if hidden_line_count > 0:
            if self.expanded:
                status_parts.append(f"({key_hint('app.tools.expand', 'to collapse')})")
            else:
                status_parts.append(
                    f"{theme.fg('muted', f'... {hidden_line_count} more lines')} "
                    f"({key_hint('app.tools.expand', 'to expand')})"
                )
        if self.status == "cancelled":
            status_parts.append(theme.fg("warning", "(cancelled)"))
        elif self.status == "error":
            status_parts.append(theme.fg("error", f"(exit {self.exitCode})"))

        was_truncated = bool(
            (self.truncationResult and self.truncationResult.truncated) or context_truncation.truncated
        )
        if was_truncated and self.fullOutputPath:
            status_parts.append(theme.fg("warning", f"Output truncated. Full output: {self.fullOutputPath}"))

        if status_parts:
            self.contentContainer.addChild(Text(f"\n{'\n'.join(status_parts)}", 1, 0))


__all__ = ["BashExecutionComponent"]

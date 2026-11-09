"""Translated from pi's ``modes/interactive/components/bash-execution.ts`` (v1.1.0).

Component for displaying bash command execution with streaming output.
"""

from __future__ import annotations

from typing import Any, Literal

from misaka.core.tools.truncate import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    TruncationOptions,
    TruncationResult,
    truncateTail,
)
from misaka.modes.interactive.theme.theme import theme
from misaka.ui.tui import Container, Loader, Spacer, Text
from misaka.utils.ansi import stripAnsi

from .dynamic_border import DynamicBorder
from .keybinding_hints import keyHint, keyText
from .visual_truncate import truncateToVisualLines

# Preview line limit when not expanded (matches tool execution behavior)
PREVIEW_LINES = 20


class BashExecutionComponent(Container):
    def __init__(self, command: str, ui: Any, excludeFromContext: bool = False, outputPad: int = 1) -> None:
        super().__init__()
        self.outputLines: list[str] = []
        self.status: Literal["running", "complete", "cancelled", "error"] = "running"
        self.exitCode: int | None = None
        self.truncationResult: TruncationResult | None = None
        self.fullOutputPath: str | None = None
        self.expanded = False
        self.command = command
        # `dim` marks `!!` commands, whose output is excluded from the model context.
        self.colorKey: Literal["dim", "bashMode"] = "dim" if excludeFromContext else "bashMode"
        self.outputPad = outputPad

        def borderColor(str: str) -> str:
            return theme.fg(self.colorKey, str)

        # Add spacer
        self.addChild(Spacer(1))

        # Top border
        self.addChild(DynamicBorder(borderColor))

        # Content container (holds dynamic content between borders)
        self.contentContainer = Container()
        self.addChild(self.contentContainer)

        self.loader = Loader(
            ui,
            lambda spinner: theme.fg(self.colorKey, spinner),
            lambda text: theme.fg("muted", text),
            f"Running... ({keyText('tui.select.cancel')} to cancel)",  # Plain text for loader
        )

        # Bottom border
        self.addChild(DynamicBorder(borderColor))

        self.updateDisplay()

    def setExpanded(self, expanded: bool) -> None:
        """Set whether the output is expanded (shows full output) or collapsed (preview only)."""
        self.expanded = expanded
        self.updateDisplay()

    def setOutputPad(self, outputPad: int) -> None:
        self.outputPad = outputPad
        self.updateDisplay()

    def invalidate(self) -> None:
        super().invalidate()
        self.updateDisplay()

    def appendOutput(self, chunk: str) -> None:
        # Strip ANSI codes and normalize line endings
        # Note: binary data is already sanitized in tui-renderer.ts executeBashCommand
        clean = stripAnsi(chunk).replace("\r\n", "\n").replace("\r", "\n")

        # Append to output lines
        newLines = clean.split("\n")
        if len(self.outputLines) > 0 and len(newLines) > 0:
            # Append first chunk to last line (incomplete line continuation)
            self.outputLines[len(self.outputLines) - 1] += newLines[0]
            self.outputLines.extend(newLines[1:])
        else:
            self.outputLines.extend(newLines)

        self.updateDisplay()

    def setComplete(
        self,
        exitCode: int | None,
        cancelled: bool,
        truncationResult: TruncationResult | None = None,
        fullOutputPath: str | None = None,
    ) -> None:
        self.exitCode = exitCode
        self.status = "cancelled" if cancelled else "error" if exitCode != 0 and exitCode is not None else "complete"
        self.truncationResult = truncationResult
        self.fullOutputPath = fullOutputPath

        # Stop loader
        self.loader.stop()

        self.updateDisplay()

    def updateDisplay(self) -> None:
        # Apply truncation for LLM context limits (same limits as bash tool)
        fullOutput = "\n".join(self.outputLines)
        contextTruncation = truncateTail(
            fullOutput,
            TruncationOptions(maxLines=DEFAULT_MAX_LINES, maxBytes=DEFAULT_MAX_BYTES),
        )

        # Get the lines to potentially display (after context truncation)
        availableLines = contextTruncation.content.split("\n") if contextTruncation.content else []

        # Apply preview truncation based on expanded state
        previewLogicalLines = availableLines[-PREVIEW_LINES:]
        hiddenLineCount = len(availableLines) - len(previewLogicalLines)

        # Rebuild content container
        self.contentContainer.clear()

        # Command header
        header = Text(theme.fg(self.colorKey, theme.bold(f"$ {self.command}")), self.outputPad, 0)
        self.contentContainer.addChild(header)

        # Output
        if len(availableLines) > 0:
            if self.expanded:
                # Show all lines
                displayText = "\n".join(theme.fg("muted", line) for line in availableLines)
                self.contentContainer.addChild(Text(f"\n{displayText}", self.outputPad, 0))
            else:
                # Use shared visual truncation utility with width-aware caching
                styledOutput = "\n".join(theme.fg("muted", line) for line in previewLogicalLines)
                styledInput = f"\n{styledOutput}"
                component = self

                class _Preview:
                    """MISAKA fork: a class for the inline ``{ render, invalidate }`` object."""

                    cachedWidth: int | None = None
                    cachedLines: list[str] | None = None

                    def render(preview, width: int) -> list[str]:
                        if preview.cachedLines is None or preview.cachedWidth != width:
                            result = truncateToVisualLines(styledInput, PREVIEW_LINES, width, component.outputPad)
                            preview.cachedLines = result.visualLines
                            preview.cachedWidth = width
                        return preview.cachedLines if preview.cachedLines is not None else []

                    def invalidate(preview) -> None:
                        preview.cachedWidth = None
                        preview.cachedLines = None

                self.contentContainer.addChild(_Preview())

        # Loader or status
        if self.status == "running":
            self.contentContainer.addChild(self.loader)
        else:
            statusParts: list[str] = []

            # Show how many lines are hidden (collapsed preview)
            if hiddenLineCount > 0:
                if self.expanded:
                    statusParts.append(
                        f"{theme.fg('muted', '(')}{keyHint('app.tools.expand', 'to collapse')}{theme.fg('muted', ')')}",
                    )
                else:
                    statusParts.append(
                        f"{theme.fg('muted', f'... {hiddenLineCount} more lines (')}"
                        f"{keyHint('app.tools.expand', 'to expand')}{theme.fg('muted', ')')}",
                    )

            if self.status == "cancelled":
                statusParts.append(theme.fg("warning", "(cancelled)"))
            elif self.status == "error":
                statusParts.append(theme.fg("error", f"(exit {self.exitCode})"))

            # Add truncation warning (context truncation, not preview truncation)
            wasTruncated = (self.truncationResult is not None and self.truncationResult.truncated) or (
                contextTruncation.truncated
            )
            if wasTruncated and self.fullOutputPath:
                statusParts.append(theme.fg("warning", f"Output truncated. Full output: {self.fullOutputPath}"))

            if len(statusParts) > 0:
                joined = "\n".join(statusParts)
                self.contentContainer.addChild(Text(f"\n{joined}", self.outputPad, 0))

    def getOutput(self) -> str:
        """Get the raw output for creating BashExecutionMessage."""
        return "\n".join(self.outputLines)

    def getCommand(self) -> str:
        """Get the command that was executed."""
        return self.command

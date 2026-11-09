"""Translated from pi's ``packages/coding-agent/test/edit-tool-no-full-redraw.test.ts`` (v1.1.0).

MISAKA fork: renders are compared with ANSI stripped. pi's test runs with chalk at colour level 0 (not a TTY), so
its renders are plain text; MISAKA's theme always emits colour, and the inverse highlight splits "line 50 changed".
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from misaka.core.tools.edit import createEditToolDefinition
from misaka.core.tools.edit_diff import Edit, EditDiffError, computeEditsDiff
from misaka.modes.interactive.components.tool_execution import ToolExecutionComponent
from misaka.modes.interactive.theme.theme import initTheme
from misaka.ui.tui import Container, Text
from misaka.ui.tui.tui_main_screen import TuiMainScreen
from misaka.utils.ansi import stripAnsi


class FakeTerminal:
    def __init__(self) -> None:
        self.writes: list[str] = []

    @property
    def columns(self) -> int:
        return 80

    @property
    def rows(self) -> int:
        return 24

    @property
    def kittyProtocolActive(self) -> bool:
        return True

    def start(self, *_: object) -> None:
        pass

    def stop(self) -> None:
        pass

    async def drainInput(self, *_: object) -> None:
        pass

    def write(self, data: str) -> None:
        self.writes.append(data)

    def moveBy(self, _lines: int) -> None:
        pass

    def hideCursor(self) -> None:
        pass

    def showCursor(self) -> None:
        pass

    def clearLine(self) -> None:
        pass

    def clearFromCursor(self) -> None:
        pass

    def clearScreen(self) -> None:
        pass

    def setTitle(self, _title: str) -> None:
        pass

    def setProgress(self, _active: bool) -> None:
        pass

    def setProgramStatus(self, *_: object) -> None:
        pass

    @property
    def fullClearCount(self) -> int:
        return len([write for write in self.writes if "\x1b[2J\x1b[H\x1b[3J" in write])


async def waitForRender() -> None:
    # pi: one `setTimeout(0)` turn.
    await asyncio.sleep(0.001)


async def waitForRenderedText(
    getRender: Callable[[], str],
    expectedText: str,
    onRetry: Callable[[], None] | None = None,
    timeoutMs: int = 2000,
) -> str:
    deadline = time.monotonic() + timeoutMs / 1000
    lastRender = ""
    while time.monotonic() < deadline:
        if onRetry is not None:
            onRetry()
        await waitForRender()
        lastRender = getRender()
        if expectedText in lastRender:
            return lastRender
    raise AssertionError(f'Timed out waiting for render to include "{expectedText}". Last render:\n{lastRender}')


def createLargeEdits(lines: list[str]) -> list[Edit]:
    def at(index: int) -> str:
        # JS `${lines[i]}` past the end interpolates "undefined".
        return lines[index] if index < len(lines) else "undefined"

    targets = [50, 150, 250, 350, 450, 550, 650, 750, 850, 950]
    return [
        Edit(
            oldText=f"{at(lineNumber - 1)}\n{at(lineNumber)}\n{at(lineNumber + 1)}",
            newText=f"{at(lineNumber - 1)}\n{at(lineNumber)} changed\n{at(lineNumber + 1)}",
        )
        for lineNumber in targets
    ]


def editArgs(path: str, edits: list[Edit]) -> dict[str, object]:
    return {"path": path, "edits": [{"oldText": edit.oldText, "newText": edit.newText} for edit in edits]}


@pytest.fixture(autouse=True)
def _theme() -> None:
    initTheme("dark")


async def test_renders_the_large_diff_in_the_call_preview_and_does_not_full_redraw_when_the_result_settles(
    tmp_path: Path,
):
    filePath = str(tmp_path / "large-edit.txt")
    Path(filePath).write_text("\n".join(f"line {i}" for i in range(1000)) + "\n", encoding="utf-8", newline="")
    lines = Path(filePath).read_text(encoding="utf-8").rstrip().split("\n")
    edits = createLargeEdits(lines)
    diff = await computeEditsDiff(filePath, edits, os.getcwd())
    assert not isinstance(diff, EditDiffError), getattr(diff, "error", None)

    terminal = FakeTerminal()
    tui = TuiMainScreen(terminal)
    root = Container()
    for i in range(200):
        root.addChild(Text(f"history {i}", 0, 0))

    component = ToolExecutionComponent(
        "edit",
        "tool-call-1",
        editArgs(filePath, edits),
        {},
        createEditToolDefinition(os.getcwd()),
        tui,
        os.getcwd(),
    )
    root.addChild(component)
    tui.addChild(root)
    tui.start()
    try:
        await waitForRender()

        component.setArgsComplete()
        tui.requestRender()
        await waitForRender()
        await waitForRender()

        callOnlyRender = await waitForRenderedText(
            lambda: stripAnsi("\n".join(component.render(80))),
            "line 50 changed",
            lambda: tui.requestRender(True),
        )
        assert "edit" in callOnlyRender
        assert "line 950 changed" in callOnlyRender

        redrawsBeforeResult = tui.fullRedraws
        clearsBeforeResult = terminal.fullClearCount
        component.updateResult(
            {
                "content": [{"type": "text", "text": f"Successfully replaced {len(edits)} block(s) in {filePath}."}],
                "details": diff,
                "isError": False,
            },
            False,
        )
        tui.requestRender()
        await waitForRender()

        assert tui.fullRedraws == redrawsBeforeResult
        assert terminal.fullClearCount == clearsBeforeResult

        settledRender = stripAnsi("\n".join(component.render(80)))
        assert "line 50 changed" in settledRender
        assert "line 950 changed" in settledRender
        assert "Successfully replaced" not in settledRender
    finally:
        tui.stop()


async def test_reconstructs_the_boxed_preview_from_a_settled_result_without_args_complete(tmp_path: Path):
    filePath = str(tmp_path / "replay-edit.txt")
    Path(filePath).write_text("\n".join(f"line {i}" for i in range(200)) + "\n", encoding="utf-8", newline="")
    lines = Path(filePath).read_text(encoding="utf-8").rstrip().split("\n")
    edits = createLargeEdits(lines)[:2]
    diff = await computeEditsDiff(filePath, edits, os.getcwd())
    assert not isinstance(diff, EditDiffError), getattr(diff, "error", None)
    os.remove(filePath)

    terminal = FakeTerminal()
    tui = TuiMainScreen(terminal)
    component = ToolExecutionComponent(
        "edit",
        "tool-call-replay",
        editArgs(filePath, edits),
        {},
        createEditToolDefinition(os.getcwd()),
        tui,
        os.getcwd(),
    )
    tui.addChild(component)
    tui.start()
    try:
        await waitForRender()

        component.updateResult(
            {
                "content": [{"type": "text", "text": f"Successfully replaced {len(edits)} block(s) in {filePath}."}],
                "details": diff,
                "isError": False,
            },
            False,
        )
        await waitForRender()
        await waitForRender()

        rendered = stripAnsi("\n".join(component.render(80)))
        assert "line 50 changed" in rendered
        assert "line 150 changed" in rendered
    finally:
        tui.stop()


async def test_shows_a_preflight_error_without_rendering_a_diff_when_the_edits_do_not_apply(tmp_path: Path):
    filePath = str(tmp_path / "missing-edit.txt")
    Path(filePath).write_text("line 0\nline 1\n", encoding="utf-8", newline="")

    terminal = FakeTerminal()
    tui = TuiMainScreen(terminal)
    component = ToolExecutionComponent(
        "edit",
        "tool-call-2",
        {"path": filePath, "edits": [{"oldText": "does not exist", "newText": "replacement"}]},
        {},
        createEditToolDefinition(os.getcwd()),
        tui,
        os.getcwd(),
    )
    tui.addChild(component)
    tui.start()
    try:
        await waitForRender()

        component.setArgsComplete()
        tui.requestRender()
        await waitForRender()
        await waitForRender()

        rendered = await waitForRenderedText(
            lambda: stripAnsi("\n".join(component.render(80))),
            "Could not find",
            lambda: tui.requestRender(True),
        )
        assert "+1 " not in rendered
        assert "-1 " not in rendered
    finally:
        tui.stop()

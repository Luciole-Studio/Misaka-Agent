"""Translated from pi-tui's ``test/overlay-non-capturing.test.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio

from misaka.ui.tui.tui import Container
from misaka.ui.tui.tui_main_screen import TuiMainScreen
from tests.tui_virtual_terminal import VirtualTerminal


class StaticOverlay:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines

    def render(self, _width: int) -> list[str]:
        return self.lines

    def invalidate(self) -> None:
        pass


class EmptyContent:
    def render(self, _width: int) -> list[str]:
        return []

    def invalidate(self) -> None:
        pass


class FocusableOverlay:
    def __init__(self, lines: list[str]) -> None:
        self.focused = False
        self.inputs: list[str] = []
        self.lines = lines

    def handleInput(self, data: str) -> None:
        self.inputs.append(data)

    def render(self, _width: int) -> list[str]:
        return self.lines

    def invalidate(self) -> None:
        pass


async def renderAndFlush(tui: TuiMainScreen, terminal: VirtualTerminal) -> None:
    tui.requestRender(True)
    await asyncio.sleep(0)
    await terminal.waitForRender()


def setup(columns: int = 80, rows: int = 24) -> tuple[VirtualTerminal, TuiMainScreen]:
    terminal = VirtualTerminal(columns, rows)
    return terminal, TuiMainScreen(terminal)


def firstChar(terminal: VirtualTerminal) -> str:
    line = terminal.getViewport()[0]
    return line[0] if line else ""


# --- focus management -----------------------------------------------------------------------------


async def test_non_capturing_overlay_preserves_focus_on_creation():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    overlay = FocusableOverlay(["OVERLAY"])
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        tui.showOverlay(overlay, {"nonCapturing": True})
        await renderAndFlush(tui, terminal)
        assert editor.focused is True
        assert overlay.focused is False
    finally:
        tui.stop()


async def test_focus_transfers_focus_to_the_overlay():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    overlay = FocusableOverlay(["OVERLAY"])
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        handle = tui.showOverlay(overlay, {"nonCapturing": True})
        handle.focus()
        await renderAndFlush(tui, terminal)
        assert editor.focused is False
        assert overlay.focused is True
        assert handle.isFocused() is True
    finally:
        tui.stop()


async def test_unfocus_restores_previous_focus():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    overlay = FocusableOverlay(["OVERLAY"])
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        handle = tui.showOverlay(overlay, {"nonCapturing": True})
        handle.focus()
        handle.unfocus()
        await renderAndFlush(tui, terminal)
        assert editor.focused is True
        assert overlay.focused is False
        assert handle.isFocused() is False
    finally:
        tui.stop()


async def test_sethidden_false_on_non_capturing_overlay_does_not_auto_focus():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    overlay = FocusableOverlay(["OVERLAY"])
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        handle = tui.showOverlay(overlay, {"nonCapturing": True})
        handle.setHidden(True)
        handle.setHidden(False)
        await renderAndFlush(tui, terminal)
        assert editor.focused is True
        assert overlay.focused is False
    finally:
        tui.stop()


async def test_hide_when_overlay_is_not_focused_does_not_change_focus():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    overlay = FocusableOverlay(["OVERLAY"])
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        handle = tui.showOverlay(overlay, {"nonCapturing": True})
        handle.hide()
        await renderAndFlush(tui, terminal)
        assert editor.focused is True
    finally:
        tui.stop()


async def test_hide_when_focused_restores_focus_correctly():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    overlay = FocusableOverlay(["OVERLAY"])
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        handle = tui.showOverlay(overlay, {"nonCapturing": True})
        handle.focus()
        handle.hide()
        await renderAndFlush(tui, terminal)
        assert editor.focused is True
        assert overlay.focused is False
    finally:
        tui.stop()


async def test_capturing_overlay_removed_with_non_capturing_below_restores_focus_to_editor():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    nonCapturing = FocusableOverlay(["NC"])
    capturing = FocusableOverlay(["CAP"])
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        tui.showOverlay(nonCapturing, {"nonCapturing": True})
        handle = tui.showOverlay(capturing)
        assert capturing.focused is True
        handle.hide()
        await renderAndFlush(tui, terminal)
        assert editor.focused is True
        assert nonCapturing.focused is False
    finally:
        tui.stop()


async def test_sub_overlay_cleanup_then_hideoverlay_restores_focus_and_input_to_editor():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    timer = FocusableOverlay(["TIMER"])
    controller = FocusableOverlay(["CTRL"])
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        timerHandle = tui.showOverlay(timer, {"nonCapturing": True})
        tui.showOverlay(controller)
        assert controller.focused is True
        assert editor.focused is False
        timerHandle.hide()
        tui.hideOverlay()
        await renderAndFlush(tui, terminal)
        assert editor.focused is True
        assert controller.focused is False
        assert timer.focused is False
        terminal.sendInput("x")
        await renderAndFlush(tui, terminal)
        assert editor.inputs == ["x"]
        assert controller.inputs == []
        assert timer.inputs == []
    finally:
        tui.stop()


async def test_removed_focused_child_overlay_does_not_become_parent_overlay_fallback():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    child = FocusableOverlay(["CHILD"])
    parent = FocusableOverlay(["PARENT"])
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        childHandle = tui.showOverlay(child, {"nonCapturing": True})
        childHandle.focus()
        parentHandle = tui.showOverlay(parent)
        assert parent.focused is True

        childHandle.hide()
        parentHandle.hide()
        terminal.sendInput("x")
        await renderAndFlush(tui, terminal)

        assert editor.inputs == ["x"]
        assert child.inputs == []
        assert parent.inputs == []
        assert editor.focused is True
    finally:
        tui.stop()


async def test_microtask_deferred_sub_overlay_pattern_showextensioncustom_simulation_restores_focus():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    timer = FocusableOverlay(["TIMER"])
    controller = FocusableOverlay(["CTRL"])
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        # Simulate showExtensionCustom: factory creates timer synchronously,
        # then .then() pushes controller as a microtask
        loop = asyncio.get_running_loop()
        overlayPromise: asyncio.Future[None] = loop.create_future()
        timerHandle = tui.showOverlay(timer, {"nonCapturing": True})

        def doneFn() -> None:
            timerHandle.hide()
            tui.hideOverlay()
            overlayPromise.set_result(None)

        # .then() runs as microtask — same as showExtensionCustom
        loop.call_soon(lambda: tui.showOverlay(controller))

        await asyncio.sleep(0)
        await renderAndFlush(tui, terminal)

        assert controller.focused is True
        assert editor.focused is False

        # Simulate Esc: cleanup + close (from inside handleInput)
        doneFn()
        # Now await the promise (simulating showExtensionCustom resolving)
        await overlayPromise
        await renderAndFlush(tui, terminal)

        assert editor.focused is True, "editor should regain focus"
        assert controller.focused is False
        assert timer.focused is False

        terminal.sendInput("x")
        await renderAndFlush(tui, terminal)
        assert editor.inputs == ["x"], "editor should receive input after close"
        assert controller.inputs == []
    finally:
        tui.stop()


async def test_handleinput_redirection_skips_non_capturing_overlays_when_focused_overlay_becomes_invisible():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    fallbackCapturing = FocusableOverlay(["FALLBACK"])
    nonCapturing = FocusableOverlay(["NC"])
    primary = FocusableOverlay(["PRIMARY"])
    isVisible = True
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        tui.showOverlay(fallbackCapturing)
        tui.showOverlay(nonCapturing, {"nonCapturing": True})
        tui.showOverlay(primary, {"visible": lambda _w, _h: isVisible})
        assert primary.focused is True
        isVisible = False
        terminal.sendInput("x")
        await renderAndFlush(tui, terminal)
        assert primary.inputs == []
        assert nonCapturing.inputs == []
        assert fallbackCapturing.inputs == ["x"]
        assert fallbackCapturing.focused is True
    finally:
        tui.stop()


async def test_active_base_focus_replacement_receives_close_input_before_overlay_restore():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    replacement = FocusableOverlay(["REPLACEMENT"])
    overlay = FocusableOverlay(["OVERLAY"])

    def overlayInput(data: str) -> None:
        overlay.inputs.append(data)
        if data == "b":
            tui.setFocus(replacement)

    def replacementInput(data: str) -> None:
        replacement.inputs.append(data)
        if data == "\r":
            tui.setFocus(editor)

    overlay.handleInput = overlayInput  # type: ignore[method-assign]
    replacement.handleInput = replacementInput  # type: ignore[method-assign]
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        tui.showOverlay(overlay)
        assert overlay.focused is True
        terminal.sendInput("b")
        await renderAndFlush(tui, terminal)
        assert replacement.focused is True

        terminal.sendInput("\r")
        await renderAndFlush(tui, terminal)
        assert replacement.inputs == ["\r"]
        assert overlay.inputs == ["b"]
        assert overlay.focused is True

        terminal.sendInput("x")
        await renderAndFlush(tui, terminal)
        assert overlay.inputs == ["b", "x"]
    finally:
        tui.stop()


async def test_active_replacement_still_receives_input_when_it_is_another_overlay_prefocus():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    replacement = FocusableOverlay(["REPLACEMENT"])
    passive = FocusableOverlay(["PASSIVE"])
    overlay = FocusableOverlay(["OVERLAY"])

    def overlayInput(data: str) -> None:
        overlay.inputs.append(data)
        if data == "b":
            tui.setFocus(replacement)

    def replacementInput(data: str) -> None:
        replacement.inputs.append(data)
        if data == "\r":
            tui.setFocus(editor)

    overlay.handleInput = overlayInput  # type: ignore[method-assign]
    replacement.handleInput = replacementInput  # type: ignore[method-assign]
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        tui.setFocus(replacement)
        tui.showOverlay(passive, {"nonCapturing": True})
        tui.setFocus(editor)
        tui.showOverlay(overlay)
        terminal.sendInput("b")
        await renderAndFlush(tui, terminal)
        assert replacement.focused is True

        terminal.sendInput("1")
        terminal.sendInput("\r")
        await renderAndFlush(tui, terminal)
        assert replacement.inputs == ["1", "\r"]
        assert overlay.inputs == ["b"]
        assert overlay.focused is True
    finally:
        tui.stop()


async def test_blocked_replacement_can_move_focus_internally_before_overlay_restore():
    terminal, tui = setup()
    base = Container()
    editor = FocusableOverlay(["EDITOR"])
    firstReplacement = FocusableOverlay(["FIRST"])
    secondReplacement = FocusableOverlay(["SECOND"])
    overlay = FocusableOverlay(["OVERLAY"])

    def overlayInput(data: str) -> None:
        overlay.inputs.append(data)
        if data == "b":
            tui.setFocus(firstReplacement)

    def firstInput(data: str) -> None:
        firstReplacement.inputs.append(data)
        if data == "n":
            tui.setFocus(secondReplacement)

    def secondInput(data: str) -> None:
        secondReplacement.inputs.append(data)
        if data == "\r":
            base.clear()
            base.addChild(editor)
            tui.setFocus(editor)

    overlay.handleInput = overlayInput  # type: ignore[method-assign]
    firstReplacement.handleInput = firstInput  # type: ignore[method-assign]
    secondReplacement.handleInput = secondInput  # type: ignore[method-assign]
    base.addChild(editor)
    base.addChild(firstReplacement)
    base.addChild(secondReplacement)
    tui.addChild(base)
    tui.setFocus(editor)
    tui.start()
    try:
        tui.showOverlay(overlay)
        terminal.sendInput("b")
        await renderAndFlush(tui, terminal)
        terminal.sendInput("n")
        await renderAndFlush(tui, terminal)
        terminal.sendInput("2")
        terminal.sendInput("\r")
        await renderAndFlush(tui, terminal)

        assert overlay.inputs == ["b"]
        assert firstReplacement.inputs == ["n"]
        assert secondReplacement.inputs == ["2", "\r"]
        assert overlay.focused is True
    finally:
        tui.stop()


async def test_removed_replacement_restores_overlay_even_when_overlay_prefocus_differs_from_next_focus():
    terminal, tui = setup()
    base = Container()
    editor = FocusableOverlay(["EDITOR"])
    palette = FocusableOverlay(["PALETTE"])
    replacement = FocusableOverlay(["REPLACEMENT"])
    overlay = FocusableOverlay(["OVERLAY"])

    def overlayInput(data: str) -> None:
        overlay.inputs.append(data)
        if data == "b":
            tui.setFocus(replacement)

    def replacementInput(data: str) -> None:
        replacement.inputs.append(data)
        if data == "\r":
            base.clear()
            base.addChild(editor)
            tui.setFocus(editor)

    overlay.handleInput = overlayInput  # type: ignore[method-assign]
    replacement.handleInput = replacementInput  # type: ignore[method-assign]
    base.addChild(editor)
    base.addChild(palette)
    base.addChild(replacement)
    tui.addChild(base)
    tui.setFocus(palette)
    tui.start()
    try:
        tui.showOverlay(overlay)
        terminal.sendInput("b")
        await renderAndFlush(tui, terminal)
        terminal.sendInput("\r")
        terminal.sendInput("x")
        await renderAndFlush(tui, terminal)

        assert overlay.inputs == ["b", "x"]
        assert replacement.inputs == ["\r"]
        assert editor.inputs == []
        assert overlay.focused is True
    finally:
        tui.stop()


async def test_unfocus_target_releases_a_blocked_overlay_while_replacement_remains_focused():
    terminal, tui = setup()
    fallback = FocusableOverlay(["FALLBACK"])
    target = FocusableOverlay(["TARGET"])
    replacement = FocusableOverlay(["REPLACEMENT"])
    overlay = FocusableOverlay(["OVERLAY"])

    def replacementInput(data: str) -> None:
        replacement.inputs.append(data)
        if data == "\r":
            tui.setFocus(fallback)

    replacement.handleInput = replacementInput  # type: ignore[method-assign]
    tui.addChild(EmptyContent())
    tui.start()
    try:
        overlayHandle = tui.showOverlay(overlay)

        def overlayInput(data: str) -> None:
            overlay.inputs.append(data)
            if data == "b":
                tui.setFocus(replacement)
                overlayHandle.unfocus({"target": target})

        overlay.handleInput = overlayInput  # type: ignore[method-assign]

        terminal.sendInput("b")
        await renderAndFlush(tui, terminal)
        assert replacement.focused is True
        terminal.sendInput("\r")
        terminal.sendInput("x")
        await renderAndFlush(tui, terminal)

        assert overlay.inputs == ["b"]
        assert replacement.inputs == ["\r"]
        assert fallback.inputs == []
        assert target.inputs == ["x"]
    finally:
        tui.stop()


async def test_handleinput_restores_focus_to_a_visible_focused_overlay_after_base_focus_steal():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    replacement = FocusableOverlay(["REPLACEMENT"])
    overlay = FocusableOverlay(["OVERLAY"])
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        tui.showOverlay(overlay)
        assert overlay.focused is True
        tui.setFocus(replacement)
        tui.setFocus(editor)
        terminal.sendInput("x")
        await renderAndFlush(tui, terminal)
        assert overlay.inputs == ["x"]
        assert editor.inputs == []
        assert overlay.focused is True
    finally:
        tui.stop()


async def test_handleinput_restores_focus_to_explicitly_focused_raw_sub_overlay_after_base_focus_steal():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    controller = FocusableOverlay(["CONTROLLER"])
    subOverlay = FocusableOverlay(["SUB"])
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        tui.showOverlay(controller)
        subHandle = tui.showOverlay(subOverlay, {"nonCapturing": True})
        subHandle.focus()
        tui.setFocus(editor)
        terminal.sendInput("x")
        await renderAndFlush(tui, terminal)
        assert subOverlay.inputs == ["x"]
        assert controller.inputs == []
        assert editor.inputs == []
    finally:
        tui.stop()


async def test_passive_non_capturing_overlay_does_not_regain_input_after_base_focus():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    passive = FocusableOverlay(["PASSIVE"])
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        tui.showOverlay(passive, {"nonCapturing": True})
        terminal.sendInput("x")
        await renderAndFlush(tui, terminal)
        assert editor.inputs == ["x"]
        assert passive.inputs == []
        assert editor.focused is True
    finally:
        tui.stop()


async def test_explicitly_focused_non_capturing_overlay_regains_input_after_base_focus_steal():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    overlay = FocusableOverlay(["NC"])
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        handle = tui.showOverlay(overlay, {"nonCapturing": True})
        handle.focus()
        tui.setFocus(editor)
        terminal.sendInput("x")
        await renderAndFlush(tui, terminal)
        assert overlay.inputs == ["x"]
        assert editor.inputs == []
    finally:
        tui.stop()


async def test_unfocus_prevents_visible_overlay_from_regaining_input():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    overlay = FocusableOverlay(["OVERLAY"])
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        handle = tui.showOverlay(overlay)
        handle.unfocus()
        terminal.sendInput("x")
        await renderAndFlush(tui, terminal)
        assert editor.inputs == ["x"]
        assert overlay.inputs == []
        assert editor.focused is True
    finally:
        tui.stop()


async def test_setfocus_null_explicitly_clears_visible_overlay_restore():
    terminal, tui = setup()
    overlay = FocusableOverlay(["OVERLAY"])
    tui.addChild(EmptyContent())
    tui.start()
    try:
        tui.showOverlay(overlay)
        tui.setFocus(None)
        terminal.sendInput("x")
        await renderAndFlush(tui, terminal)
        assert overlay.inputs == []
        assert overlay.focused is False
    finally:
        tui.stop()


async def test_blocked_replacement_setfocus_null_resumes_the_visible_overlay():
    terminal, tui = setup()
    replacement = FocusableOverlay(["REPLACEMENT"])
    overlay = FocusableOverlay(["OVERLAY"])

    def replacementInput(data: str) -> None:
        replacement.inputs.append(data)
        if data == "\r":
            tui.setFocus(None)

    def overlayInput(data: str) -> None:
        overlay.inputs.append(data)
        if data == "b":
            tui.setFocus(replacement)

    replacement.handleInput = replacementInput  # type: ignore[method-assign]
    overlay.handleInput = overlayInput  # type: ignore[method-assign]
    tui.addChild(EmptyContent())
    tui.start()
    try:
        tui.showOverlay(overlay)
        terminal.sendInput("b")
        await renderAndFlush(tui, terminal)
        terminal.sendInput("\r")
        terminal.sendInput("x")
        await renderAndFlush(tui, terminal)
        assert replacement.inputs == ["\r"]
        assert overlay.inputs == ["b", "x"]
        assert overlay.focused is True
    finally:
        tui.stop()


async def test_temporarily_invisible_focused_overlay_falls_back_without_losing_restore_eligibility():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    overlay = FocusableOverlay(["OVERLAY"])
    visible = True
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        tui.showOverlay(overlay, {"visible": lambda _w, _h: visible})
        tui.setFocus(editor)
        visible = False
        terminal.sendInput("x")
        await renderAndFlush(tui, terminal)
        assert editor.inputs == ["x"]
        assert overlay.inputs == []
        visible = True
        terminal.sendInput("y")
        await renderAndFlush(tui, terminal)
        assert editor.inputs == ["x"]
        assert overlay.inputs == ["y"]
    finally:
        tui.stop()


async def test_temporarily_invisible_focused_overlay_with_null_prefocus_restores_when_visible_again():
    terminal, tui = setup()
    overlay = FocusableOverlay(["OVERLAY"])
    visible = True
    tui.addChild(EmptyContent())
    tui.start()
    try:
        tui.showOverlay(overlay, {"visible": lambda _w, _h: visible})
        visible = False
        terminal.sendInput("x")
        await renderAndFlush(tui, terminal)
        assert overlay.inputs == []
        visible = True
        terminal.sendInput("y")
        await renderAndFlush(tui, terminal)
        assert overlay.inputs == ["y"]
    finally:
        tui.stop()


async def test_cyclic_overlay_prefocus_ancestry_does_not_hang_focus_changes():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    overlay = FocusableOverlay(["OVERLAY"])
    tui.addChild(EmptyContent())
    tui.setFocus(overlay)
    tui.start()
    try:
        handle = tui.showOverlay(overlay, {"nonCapturing": True})
        handle.focus()
        tui.setFocus(editor)
        terminal.sendInput("x")
        await renderAndFlush(tui, terminal)
        assert editor.inputs == ["x"]
        assert overlay.inputs == []
    finally:
        tui.stop()


async def test_handleinput_restores_the_focus_order_top_overlay_after_base_focus_steal():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    lower = FocusableOverlay(["LOWER"])
    upper = FocusableOverlay(["UPPER"])
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        lowerHandle = tui.showOverlay(lower)
        tui.showOverlay(upper)
        lowerHandle.focus()
        tui.setFocus(editor)
        terminal.sendInput("x")
        await renderAndFlush(tui, terminal)
        assert lower.inputs == ["x"]
        assert upper.inputs == []
        assert editor.inputs == []
    finally:
        tui.stop()


async def test_hideoverlay_does_not_reassign_focus_when_topmost_overlay_is_non_capturing():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    capturing = FocusableOverlay(["CAP"])
    nonCapturing = FocusableOverlay(["NC"])
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        tui.showOverlay(capturing)
        tui.showOverlay(nonCapturing, {"nonCapturing": True})
        assert capturing.focused is True
        tui.hideOverlay()
        await renderAndFlush(tui, terminal)
        assert capturing.focused is True
    finally:
        tui.stop()


async def test_multiple_capturing_and_non_capturing_overlays_restore_focus_through_removals():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    c1 = FocusableOverlay(["C1"])
    n1 = FocusableOverlay(["N1"])
    c2 = FocusableOverlay(["C2"])
    n2 = FocusableOverlay(["N2"])
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        c1Handle = tui.showOverlay(c1)
        tui.showOverlay(n1, {"nonCapturing": True})
        c2Handle = tui.showOverlay(c2)
        tui.showOverlay(n2, {"nonCapturing": True})
        assert c2.focused is True
        c2Handle.hide()
        await renderAndFlush(tui, terminal)
        assert c1.focused is True
        c1Handle.hide()
        await renderAndFlush(tui, terminal)
        assert editor.focused is True
    finally:
        tui.stop()


async def test_capturing_overlay_unfocus_on_topmost_capturing_overlay_falls_back_to_prefocus():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    capturing = FocusableOverlay(["CAP"])
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        handle = tui.showOverlay(capturing)
        assert capturing.focused is True
        handle.unfocus()
        await renderAndFlush(tui, terminal)
        assert editor.focused is True
        assert capturing.focused is False
    finally:
        tui.stop()


# --- no-op guards ---------------------------------------------------------------------------------


async def test_focus_on_hidden_overlay_is_a_no_op():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    overlay = FocusableOverlay(["OVERLAY"])
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        handle = tui.showOverlay(overlay, {"nonCapturing": True})
        handle.setHidden(True)
        handle.focus()
        await renderAndFlush(tui, terminal)
        assert editor.focused is True
        assert handle.isFocused() is False
    finally:
        tui.stop()


async def test_focus_after_hide_is_a_no_op():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    overlay = FocusableOverlay(["OVERLAY"])
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        handle = tui.showOverlay(overlay, {"nonCapturing": True})
        handle.hide()
        handle.focus()
        await renderAndFlush(tui, terminal)
        assert editor.focused is True
        assert handle.isFocused() is False
    finally:
        tui.stop()


async def test_unfocus_when_overlay_does_not_have_focus_is_a_no_op():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    overlay = FocusableOverlay(["OVERLAY"])
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        handle = tui.showOverlay(overlay, {"nonCapturing": True})
        handle.unfocus()
        await renderAndFlush(tui, terminal)
        assert editor.focused is True
        assert overlay.focused is False
    finally:
        tui.stop()


async def test_unfocus_with_null_prefocus_clears_focus_and_does_not_route_input_back_to_overlay():
    terminal, tui = setup()
    overlay = FocusableOverlay(["OVERLAY"])
    tui.addChild(EmptyContent())
    tui.start()
    try:
        handle = tui.showOverlay(overlay)
        assert overlay.focused is True
        handle.unfocus()
        assert overlay.focused is False
        terminal.sendInput("x")
        await renderAndFlush(tui, terminal)
        assert overlay.inputs == []
        assert handle.isFocused() is False
    finally:
        tui.stop()


# --- focus cycle prevention -----------------------------------------------------------------------


async def test_toggle_focus_between_non_capturing_overlays_then_unfocus_returns_to_editor():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    a = FocusableOverlay(["A"])
    b = FocusableOverlay(["B"])
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        aHandle = tui.showOverlay(a, {"nonCapturing": True})
        bHandle = tui.showOverlay(b, {"nonCapturing": True})
        aHandle.focus()
        bHandle.focus()
        aHandle.focus()
        aHandle.unfocus()
        await renderAndFlush(tui, terminal)
        assert editor.focused is True
        assert a.focused is False
        assert b.focused is False
    finally:
        tui.stop()


async def test_explicit_unfocus_target_supports_cycling_between_three_overlays_and_editor():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    a = FocusableOverlay(["A"])
    b = FocusableOverlay(["B"])
    c = FocusableOverlay(["C"])
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        aHandle = tui.showOverlay(a)
        bHandle = tui.showOverlay(b)
        cHandle = tui.showOverlay(c)

        aHandle.focus()
        terminal.sendInput("a")
        await renderAndFlush(tui, terminal)
        bHandle.focus()
        terminal.sendInput("b")
        await renderAndFlush(tui, terminal)
        cHandle.focus()
        terminal.sendInput("c")
        await renderAndFlush(tui, terminal)
        cHandle.unfocus({"target": editor})
        terminal.sendInput("e")
        await renderAndFlush(tui, terminal)
        aHandle.focus()
        terminal.sendInput("A")
        await renderAndFlush(tui, terminal)
        aHandle.unfocus({"target": editor})
        terminal.sendInput("E")
        await renderAndFlush(tui, terminal)

        assert a.inputs == ["a", "A"]
        assert b.inputs == ["b"]
        assert c.inputs == ["c"]
        assert editor.inputs == ["e", "E"]
        assert editor.focused is True
    finally:
        tui.stop()


async def test_explicit_null_unfocus_target_clears_focus_without_restoring_overlays():
    terminal, tui = setup()
    overlay = FocusableOverlay(["OVERLAY"])
    tui.addChild(EmptyContent())
    tui.start()
    try:
        handle = tui.showOverlay(overlay)
        handle.unfocus({"target": None})
        terminal.sendInput("x")
        await renderAndFlush(tui, terminal)
        assert overlay.inputs == []
        assert handle.isFocused() is False
    finally:
        tui.stop()


async def test_hiding_focused_overlay_falls_back_to_next_visual_frontmost_overlay():
    terminal, tui = setup()
    editor = FocusableOverlay(["EDITOR"])
    a = FocusableOverlay(["A"])
    b = FocusableOverlay(["B"])
    c = FocusableOverlay(["C"])
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        aHandle = tui.showOverlay(a)
        bHandle = tui.showOverlay(b)
        tui.showOverlay(c)
        aHandle.focus()
        bHandle.focus()
        bHandle.setHidden(True)
        terminal.sendInput("x")
        await renderAndFlush(tui, terminal)
        assert a.inputs == ["x"]
        assert c.inputs == []
        assert a.focused is True
    finally:
        tui.stop()


# --- rendering order ------------------------------------------------------------------------------

AT_ORIGIN = {"row": 0, "col": 0, "width": 1, "nonCapturing": True}


async def test_focus_on_already_focused_overlay_bumps_visual_order():
    terminal, tui = setup(20, 6)
    editor = FocusableOverlay(["EDITOR"])
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        aHandle = tui.showOverlay(StaticOverlay(["A"]), {**AT_ORIGIN})
        tui.showOverlay(StaticOverlay(["B"]), {**AT_ORIGIN})
        aHandle.focus()
        tui.showOverlay(StaticOverlay(["C"]), {**AT_ORIGIN})
        await renderAndFlush(tui, terminal)
        assert firstChar(terminal) == "C"
        aHandle.focus()
        await renderAndFlush(tui, terminal)
        assert firstChar(terminal) == "A"
        assert aHandle.isFocused() is True
    finally:
        tui.stop()


async def test_default_rendering_order_for_overlapping_overlays_follows_creation_order():
    terminal, tui = setup(20, 6)
    tui.addChild(EmptyContent())
    tui.start()
    try:
        tui.showOverlay(StaticOverlay(["A"]), {**AT_ORIGIN})
        tui.showOverlay(StaticOverlay(["B"]), {**AT_ORIGIN})
        await renderAndFlush(tui, terminal)
        assert firstChar(terminal) == "B"
    finally:
        tui.stop()


async def test_focus_on_lower_overlay_renders_it_on_top():
    terminal, tui = setup(20, 6)
    tui.addChild(EmptyContent())
    tui.start()
    try:
        lower = tui.showOverlay(StaticOverlay(["A"]), {**AT_ORIGIN})
        tui.showOverlay(StaticOverlay(["B"]), {**AT_ORIGIN})
        await renderAndFlush(tui, terminal)
        assert firstChar(terminal) == "B"
        lower.focus()
        await renderAndFlush(tui, terminal)
        assert firstChar(terminal) == "A"
    finally:
        tui.stop()


async def test_focusing_middle_overlay_places_it_on_top_while_preserving_others_relative_order():
    terminal, tui = setup(20, 6)
    tui.addChild(EmptyContent())
    tui.start()
    try:
        tui.showOverlay(StaticOverlay(["A"]), {**AT_ORIGIN})
        middle = tui.showOverlay(StaticOverlay(["B"]), {**AT_ORIGIN})
        top = tui.showOverlay(StaticOverlay(["C"]), {**AT_ORIGIN})
        await renderAndFlush(tui, terminal)
        assert firstChar(terminal) == "C"
        middle.focus()
        await renderAndFlush(tui, terminal)
        assert firstChar(terminal) == "B"
        middle.hide()
        await renderAndFlush(tui, terminal)
        assert firstChar(terminal) == "C"
        top.hide()
        await renderAndFlush(tui, terminal)
        assert firstChar(terminal) == "A"
    finally:
        tui.stop()


async def test_capturing_overlay_hidden_and_shown_again_renders_on_top_after_unhide():
    terminal, tui = setup(20, 6)
    tui.addChild(EmptyContent())
    tui.start()
    try:
        tui.showOverlay(StaticOverlay(["A"]), {**AT_ORIGIN})
        capturing = tui.showOverlay(StaticOverlay(["B"]), {"row": 0, "col": 0, "width": 1})
        await renderAndFlush(tui, terminal)
        assert firstChar(terminal) == "B"
        capturing.setHidden(True)
        tui.showOverlay(StaticOverlay(["C"]), {**AT_ORIGIN})
        await renderAndFlush(tui, terminal)
        assert firstChar(terminal) == "C"
        capturing.setHidden(False)
        await renderAndFlush(tui, terminal)
        assert firstChar(terminal) == "B"
    finally:
        tui.stop()


async def test_unfocus_does_not_change_visual_order_until_another_overlay_is_focused():
    terminal, tui = setup(20, 6)
    editor = FocusableOverlay(["EDITOR"])
    tui.addChild(EmptyContent())
    tui.setFocus(editor)
    tui.start()
    try:
        a = tui.showOverlay(StaticOverlay(["A"]), {**AT_ORIGIN})
        b = tui.showOverlay(StaticOverlay(["B"]), {**AT_ORIGIN})
        await renderAndFlush(tui, terminal)
        assert firstChar(terminal) == "B"
        a.focus()
        await renderAndFlush(tui, terminal)
        assert firstChar(terminal) == "A"
        a.unfocus()
        await renderAndFlush(tui, terminal)
        assert firstChar(terminal) == "A"
        b.focus()
        await renderAndFlush(tui, terminal)
        assert firstChar(terminal) == "B"
    finally:
        tui.stop()

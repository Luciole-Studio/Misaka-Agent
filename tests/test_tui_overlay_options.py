"""Translated from pi-tui's ``test/overlay-options.test.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio

from misaka.ui.tui.tui_main_screen import TuiMainScreen
from tests.tui_virtual_terminal import VirtualTerminal


class StaticOverlay:
    def __init__(self, lines: list[str], requestedWidth: int | None = None) -> None:
        self.lines = lines
        self.requestedWidth = requestedWidth

    def render(self, width: int) -> list[str]:
        # Store the width we were asked to render at for verification
        self.requestedWidth = width
        return self.lines

    def invalidate(self) -> None:
        pass


class CursorTrackingTerminal(VirtualTerminal):
    def __init__(self, columns: int = 80, rows: int = 24) -> None:
        super().__init__(columns, rows)
        self.cursorVisible = True

    def hideCursor(self) -> None:
        self.cursorVisible = False
        super().hideCursor()

    def showCursor(self) -> None:
        self.cursorVisible = True
        super().showCursor()


class EmptyContent:
    def render(self, _width: int) -> list[str]:
        return []

    def invalidate(self) -> None:
        pass


async def renderAndFlush(tui: TuiMainScreen, terminal: VirtualTerminal) -> None:
    tui.requestRender(True)
    await asyncio.sleep(0)
    await terminal.waitForRender()


def row(viewport: list[str], index: int) -> str:
    return viewport[index] if index < len(viewport) else ""


# --- width overflow protection --------------------------------------------------------------------


async def test_should_truncate_overlay_lines_that_exceed_declared_width():
    terminal = VirtualTerminal(80, 24)
    tui = TuiMainScreen(terminal)
    # Overlay declares width 20 but renders lines much wider
    overlay = StaticOverlay(["X" * 100])

    tui.addChild(EmptyContent())
    tui.showOverlay(overlay, {"width": 20})
    tui.start()
    await renderAndFlush(tui, terminal)

    # Should not crash, and no line should exceed terminal width
    viewport = terminal.getViewport()
    for line in viewport:
        # The important thing is it didn't crash
        assert line is not None
    tui.stop()


async def test_should_handle_overlay_with_complex_ansi_sequences_without_crashing():
    terminal = VirtualTerminal(80, 24)
    tui = TuiMainScreen(terminal)
    # Simulate complex ANSI content like the crash log showed
    complexLine = (
        "\x1b[48;2;40;50;40m \x1b[38;2;128;128;128mSome styled content\x1b[39m\x1b[49m"
        + "\x1b]8;;http://example.com\x07link\x1b]8;;\x07"
        + " more content " * 10
    )
    overlay = StaticOverlay([complexLine, complexLine, complexLine])

    tui.addChild(EmptyContent())
    tui.showOverlay(overlay, {"width": 60})
    tui.start()
    await renderAndFlush(tui, terminal)

    # Should not crash
    viewport = terminal.getViewport()
    assert len(viewport) > 0
    tui.stop()


async def test_should_handle_overlay_composited_on_styled_base_content():
    terminal = VirtualTerminal(80, 24)
    tui = TuiMainScreen(terminal)

    # Base content with styling
    class StyledContent:
        def render(self, width: int) -> list[str]:
            styledLine = f"\x1b[1m\x1b[38;2;255;0;0m{'X' * width}\x1b[0m"
            return [styledLine, styledLine, styledLine]

        def invalidate(self) -> None:
            pass

    overlay = StaticOverlay(["OVERLAY"])

    tui.addChild(StyledContent())
    tui.showOverlay(overlay, {"width": 20, "anchor": "center"})
    tui.start()
    await renderAndFlush(tui, terminal)

    # Should not crash and overlay should be visible
    viewport = terminal.getViewport()
    hasOverlay = any("OVERLAY" in line for line in viewport)
    assert hasOverlay, "Overlay should be visible"
    tui.stop()


async def test_should_handle_wide_characters_at_overlay_boundary():
    terminal = VirtualTerminal(80, 24)
    tui = TuiMainScreen(terminal)
    # Wide chars (each takes 2 columns) at the edge of declared width
    wideCharLine = "中文日本語한글テスト漢字"  # Mix of CJK chars
    overlay = StaticOverlay([wideCharLine])

    tui.addChild(EmptyContent())
    tui.showOverlay(overlay, {"width": 15})  # Odd width to potentially hit boundary
    tui.start()
    await renderAndFlush(tui, terminal)

    # Should not crash
    viewport = terminal.getViewport()
    assert len(viewport) > 0
    tui.stop()


async def test_should_handle_overlay_positioned_at_terminal_edge():
    terminal = VirtualTerminal(80, 24)
    tui = TuiMainScreen(terminal)
    # Overlay positioned at right edge with content that exceeds declared width
    overlay = StaticOverlay(["X" * 50])

    tui.addChild(EmptyContent())
    # Position at col 60 with width 20 - should fit exactly at right edge
    tui.showOverlay(overlay, {"col": 60, "width": 20})
    tui.start()
    await renderAndFlush(tui, terminal)

    # Should not crash
    viewport = terminal.getViewport()
    assert len(viewport) > 0
    tui.stop()


async def test_should_handle_overlay_on_base_content_with_osc_sequences():
    terminal = VirtualTerminal(80, 24)
    tui = TuiMainScreen(terminal)

    # Base content with OSC 8 hyperlinks (like file paths in agent output)
    class HyperlinkContent:
        def render(self, width: int) -> list[str]:
            link = "\x1b]8;;file:///path/to/file.ts\x07file.ts\x1b]8;;\x07"
            line = f"See {link} for details {'X' * (width - 30)}"
            return [line, line, line]

        def invalidate(self) -> None:
            pass

    overlay = StaticOverlay(["OVERLAY-TEXT"])

    tui.addChild(HyperlinkContent())
    tui.showOverlay(overlay, {"anchor": "center", "width": 20})
    tui.start()
    await renderAndFlush(tui, terminal)

    # Should not crash - this was the original bug scenario
    viewport = terminal.getViewport()
    assert len(viewport) > 0
    tui.stop()


# --- width percentage -----------------------------------------------------------------------------


async def test_should_render_overlay_at_percentage_of_terminal_width():
    terminal = VirtualTerminal(100, 24)
    tui = TuiMainScreen(terminal)
    overlay = StaticOverlay(["test"])

    tui.addChild(EmptyContent())
    tui.showOverlay(overlay, {"width": "50%"})
    tui.start()
    await renderAndFlush(tui, terminal)

    assert overlay.requestedWidth == 50
    tui.stop()


async def test_should_respect_minwidth_when_widthpercent_results_in_smaller_width():
    terminal = VirtualTerminal(100, 24)
    tui = TuiMainScreen(terminal)
    overlay = StaticOverlay(["test"])

    tui.addChild(EmptyContent())
    tui.showOverlay(overlay, {"width": "10%", "minWidth": 30})
    tui.start()
    await renderAndFlush(tui, terminal)

    assert overlay.requestedWidth == 30
    tui.stop()


# --- anchor positioning ---------------------------------------------------------------------------


async def test_should_position_overlay_at_top_left():
    terminal = VirtualTerminal(80, 24)
    tui = TuiMainScreen(terminal)
    overlay = StaticOverlay(["TOP-LEFT"])

    tui.addChild(EmptyContent())
    tui.showOverlay(overlay, {"anchor": "top-left", "width": 10})
    tui.start()
    await renderAndFlush(tui, terminal)

    viewport = terminal.getViewport()
    assert row(viewport, 0).startswith("TOP-LEFT"), f"Expected TOP-LEFT at start, got: {row(viewport, 0)}"
    tui.stop()


async def test_should_position_overlay_at_bottom_right():
    terminal = VirtualTerminal(80, 24)
    tui = TuiMainScreen(terminal)
    overlay = StaticOverlay(["BTM-RIGHT"])

    tui.addChild(EmptyContent())
    tui.showOverlay(overlay, {"anchor": "bottom-right", "width": 10})
    tui.start()
    await renderAndFlush(tui, terminal)

    viewport = terminal.getViewport()
    # Should be on last row, ending at last column
    lastRow = row(viewport, 23)
    assert "BTM-RIGHT" in lastRow, f"Expected BTM-RIGHT on last row, got: {lastRow}"
    assert lastRow.rstrip().endswith("BTM-RIGHT"), f"Expected BTM-RIGHT at end, got: {lastRow}"
    tui.stop()


async def test_should_position_overlay_at_top_center():
    terminal = VirtualTerminal(80, 24)
    tui = TuiMainScreen(terminal)
    overlay = StaticOverlay(["CENTERED"])

    tui.addChild(EmptyContent())
    tui.showOverlay(overlay, {"anchor": "top-center", "width": 10})
    tui.start()
    await renderAndFlush(tui, terminal)

    viewport = terminal.getViewport()
    # Should be on first row, centered horizontally
    firstRow = row(viewport, 0)
    assert "CENTERED" in firstRow, f"Expected CENTERED on first row, got: {firstRow}"
    # Check it's roughly centered (col 35 for width 10 in 80 col terminal)
    colIndex = firstRow.find("CENTERED")
    assert colIndex >= 30 and colIndex <= 40, f"Expected centered, got col {colIndex}"
    tui.stop()


# --- margin ---------------------------------------------------------------------------------------


async def test_should_clamp_negative_margins_to_zero():
    terminal = VirtualTerminal(80, 24)
    tui = TuiMainScreen(terminal)
    overlay = StaticOverlay(["NEG-MARGIN"])

    tui.addChild(EmptyContent())
    # Negative margins should be treated as 0
    tui.showOverlay(
        overlay,
        {
            "anchor": "top-left",
            "width": 12,
            "margin": {"top": -5, "left": -10, "right": 0, "bottom": 0},
        },
    )
    tui.start()
    await renderAndFlush(tui, terminal)

    viewport = terminal.getViewport()
    # Should be at row 0, col 0 (negative margins clamped to 0)
    assert row(viewport, 0).startswith("NEG-MARGIN"), f"Expected NEG-MARGIN at start of row 0, got: {row(viewport, 0)}"
    tui.stop()


async def test_should_respect_margin_as_number():
    terminal = VirtualTerminal(80, 24)
    tui = TuiMainScreen(terminal)
    overlay = StaticOverlay(["MARGIN"])

    tui.addChild(EmptyContent())
    tui.showOverlay(overlay, {"anchor": "top-left", "width": 10, "margin": 5})
    tui.start()
    await renderAndFlush(tui, terminal)

    viewport = terminal.getViewport()
    # Should be on row 5 (not 0) due to margin
    assert "MARGIN" not in row(viewport, 0), "Should not be on row 0"
    assert "MARGIN" not in row(viewport, 4), "Should not be on row 4"
    assert "MARGIN" in row(viewport, 5), f"Expected MARGIN on row 5, got: {row(viewport, 5)}"
    # Should start at col 5 (not 0)
    colIndex = row(viewport, 5).find("MARGIN")
    assert colIndex == 5, f"Expected col 5, got {colIndex}"
    tui.stop()


async def test_should_respect_margin_object():
    terminal = VirtualTerminal(80, 24)
    tui = TuiMainScreen(terminal)
    overlay = StaticOverlay(["MARGIN"])

    tui.addChild(EmptyContent())
    tui.showOverlay(
        overlay,
        {
            "anchor": "top-left",
            "width": 10,
            "margin": {"top": 2, "left": 3, "right": 0, "bottom": 0},
        },
    )
    tui.start()
    await renderAndFlush(tui, terminal)

    viewport = terminal.getViewport()
    assert "MARGIN" in row(viewport, 2), f"Expected MARGIN on row 2, got: {row(viewport, 2)}"
    colIndex = row(viewport, 2).find("MARGIN")
    assert colIndex == 3, f"Expected col 3, got {colIndex}"
    tui.stop()


# --- offset ---------------------------------------------------------------------------------------


async def test_should_apply_offsetx_and_offsety_from_anchor_position():
    terminal = VirtualTerminal(80, 24)
    tui = TuiMainScreen(terminal)
    overlay = StaticOverlay(["OFFSET"])

    tui.addChild(EmptyContent())
    tui.showOverlay(overlay, {"anchor": "top-left", "width": 10, "offsetX": 10, "offsetY": 5})
    tui.start()
    await renderAndFlush(tui, terminal)

    viewport = terminal.getViewport()
    assert "OFFSET" in row(viewport, 5), f"Expected OFFSET on row 5, got: {row(viewport, 5)}"
    colIndex = row(viewport, 5).find("OFFSET")
    assert colIndex == 10, f"Expected col 10, got {colIndex}"
    tui.stop()


# --- percentage positioning -----------------------------------------------------------------------


async def test_should_position_with_rowpercent_and_colpercent():
    terminal = VirtualTerminal(80, 24)
    tui = TuiMainScreen(terminal)
    overlay = StaticOverlay(["PCT"])

    tui.addChild(EmptyContent())
    # 50% should center both ways
    tui.showOverlay(overlay, {"width": 10, "row": "50%", "col": "50%"})
    tui.start()
    await renderAndFlush(tui, terminal)

    viewport = terminal.getViewport()
    # Find the row with PCT
    foundRow = -1
    for i in range(len(viewport)):
        if "PCT" in viewport[i]:
            foundRow = i
            break
    # Should be roughly centered vertically (row ~11-12 for 24 row terminal)
    assert foundRow >= 10 and foundRow <= 13, f"Expected centered row, got {foundRow}"
    tui.stop()


async def test_rowpercent_0_should_position_at_top():
    terminal = VirtualTerminal(80, 24)
    tui = TuiMainScreen(terminal)
    overlay = StaticOverlay(["TOP"])

    tui.addChild(EmptyContent())
    tui.showOverlay(overlay, {"width": 10, "row": "0%"})
    tui.start()
    await renderAndFlush(tui, terminal)

    viewport = terminal.getViewport()
    assert "TOP" in row(viewport, 0), f"Expected TOP on row 0, got: {row(viewport, 0)}"
    tui.stop()


async def test_rowpercent_100_should_position_at_bottom():
    terminal = VirtualTerminal(80, 24)
    tui = TuiMainScreen(terminal)
    overlay = StaticOverlay(["BOTTOM"])

    tui.addChild(EmptyContent())
    tui.showOverlay(overlay, {"width": 10, "row": "100%"})
    tui.start()
    await renderAndFlush(tui, terminal)

    viewport = terminal.getViewport()
    assert "BOTTOM" in row(viewport, 23), f"Expected BOTTOM on last row, got: {row(viewport, 23)}"
    tui.stop()


# --- maxHeight ------------------------------------------------------------------------------------


async def test_should_truncate_overlay_to_maxheight():
    terminal = VirtualTerminal(80, 24)
    tui = TuiMainScreen(terminal)
    overlay = StaticOverlay(["Line 1", "Line 2", "Line 3", "Line 4", "Line 5"])

    tui.addChild(EmptyContent())
    tui.showOverlay(overlay, {"maxHeight": 3})
    tui.start()
    await renderAndFlush(tui, terminal)

    viewport = terminal.getViewport()
    content = "\n".join(viewport)
    assert "Line 1" in content, "Should include Line 1"
    assert "Line 2" in content, "Should include Line 2"
    assert "Line 3" in content, "Should include Line 3"
    assert "Line 4" not in content, "Should NOT include Line 4"
    assert "Line 5" not in content, "Should NOT include Line 5"
    tui.stop()


async def test_should_truncate_overlay_to_maxheightpercent():
    terminal = VirtualTerminal(80, 10)
    tui = TuiMainScreen(terminal)
    # 10 lines in a 10 row terminal with 50% maxHeight should show 5 lines
    overlay = StaticOverlay(["L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8", "L9", "L10"])

    tui.addChild(EmptyContent())
    tui.showOverlay(overlay, {"maxHeight": "50%"})
    tui.start()
    await renderAndFlush(tui, terminal)

    viewport = terminal.getViewport()
    content = "\n".join(viewport)
    assert "L1" in content, "Should include L1"
    assert "L5" in content, "Should include L5"
    assert "L6" not in content, "Should NOT include L6"
    tui.stop()


# --- absolute positioning -------------------------------------------------------------------------


async def test_row_and_col_should_override_anchor():
    terminal = VirtualTerminal(80, 24)
    tui = TuiMainScreen(terminal)
    overlay = StaticOverlay(["ABSOLUTE"])

    tui.addChild(EmptyContent())
    # Even with bottom-right anchor, row/col should win
    tui.showOverlay(overlay, {"anchor": "bottom-right", "row": 3, "col": 5, "width": 10})
    tui.start()
    await renderAndFlush(tui, terminal)

    viewport = terminal.getViewport()
    assert "ABSOLUTE" in row(viewport, 3), f"Expected ABSOLUTE on row 3, got: {row(viewport, 3)}"
    colIndex = row(viewport, 3).find("ABSOLUTE")
    assert colIndex == 5, f"Expected col 5, got {colIndex}"
    tui.stop()


# --- stacked overlays -----------------------------------------------------------------------------


async def test_should_render_multiple_overlays_with_later_ones_on_top():
    terminal = VirtualTerminal(80, 24)
    tui = TuiMainScreen(terminal)

    tui.addChild(EmptyContent())

    # First overlay at top-left
    overlay1 = StaticOverlay(["FIRST-OVERLAY"])
    tui.showOverlay(overlay1, {"anchor": "top-left", "width": 20})

    # Second overlay at top-left (should cover part of first)
    overlay2 = StaticOverlay(["SECOND"])
    tui.showOverlay(overlay2, {"anchor": "top-left", "width": 10})

    tui.start()
    await renderAndFlush(tui, terminal)

    viewport = terminal.getViewport()
    # Second overlay should be visible (on top)
    assert "SECOND" in row(viewport, 0), f"Expected SECOND on row 0, got: {row(viewport, 0)}"
    # Part of first overlay might still be visible after SECOND
    # FIRST-OVERLAY is 13 chars, SECOND is 6 chars, so "OVERLAY" part might show
    tui.stop()


async def test_should_handle_overlays_at_different_positions_without_interference():
    terminal = VirtualTerminal(80, 24)
    tui = TuiMainScreen(terminal)

    tui.addChild(EmptyContent())

    # Overlay at top-left
    overlay1 = StaticOverlay(["TOP-LEFT"])
    tui.showOverlay(overlay1, {"anchor": "top-left", "width": 15})

    # Overlay at bottom-right
    overlay2 = StaticOverlay(["BTM-RIGHT"])
    tui.showOverlay(overlay2, {"anchor": "bottom-right", "width": 15})

    tui.start()
    await renderAndFlush(tui, terminal)

    viewport = terminal.getViewport()
    # Both should be visible
    assert "TOP-LEFT" in row(viewport, 0), f"Expected TOP-LEFT on row 0, got: {row(viewport, 0)}"
    assert "BTM-RIGHT" in row(viewport, 23), f"Expected BTM-RIGHT on row 23, got: {row(viewport, 23)}"
    tui.stop()


async def test_should_properly_hide_overlays_in_stack_order():
    terminal = VirtualTerminal(80, 24)
    tui = TuiMainScreen(terminal)

    tui.addChild(EmptyContent())

    # Show two overlays
    overlay1 = StaticOverlay(["FIRST"])
    tui.showOverlay(overlay1, {"anchor": "top-left", "width": 10})

    overlay2 = StaticOverlay(["SECOND"])
    tui.showOverlay(overlay2, {"anchor": "top-left", "width": 10})

    tui.start()
    await renderAndFlush(tui, terminal)

    # Second should be visible
    viewport = terminal.getViewport()
    assert "SECOND" in row(viewport, 0), "SECOND should be visible initially"

    # Hide top overlay
    tui.hideOverlay()
    await renderAndFlush(tui, terminal)

    # First should now be visible
    viewport = terminal.getViewport()
    assert "FIRST" in row(viewport, 0), "FIRST should be visible after hiding SECOND"

    tui.stop()


# --- hiding after stop (https://github.com/earendil-works/pi/issues/10026) ------------------------


async def test_hideoverlay_leaves_the_cursor_visible():
    terminal = CursorTrackingTerminal(80, 24)
    tui = TuiMainScreen(terminal)
    tui.start()
    tui.showOverlay(StaticOverlay(["OVERLAY"]), {"nonCapturing": True})

    tui.stop()
    tui.hideOverlay()

    assert terminal.cursorVisible is True


async def test_overlay_handle_hide_leaves_the_cursor_visible():
    terminal = CursorTrackingTerminal(80, 24)
    tui = TuiMainScreen(terminal)
    tui.start()
    handle = tui.showOverlay(StaticOverlay(["OVERLAY"]), {"nonCapturing": True})

    tui.stop()
    handle.hide()

    assert terminal.cursorVisible is True

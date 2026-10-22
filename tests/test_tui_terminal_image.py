"""Translated from pi-tui's ``test/terminal-image.test.ts`` (v1.3.0).

Tests for terminal image detection and line handling

MISAKA forks: the environment overrides are ``MISAKA_HYPERLINKS``/``MISAKA_IMAGE_PROTOCOL``/``MISAKA_TRUE_COLOR``
(pi: ``PI_*``). D1 (the fullscreen renderer's Kitty metadata registry, cropping and placement-only commands): "reads
explicit placement rows without registered metadata", "registers metadata and crops a partially visible placement"
and "creates placement-only commands for uploaded and cropped images" are not ported, and the D1 assertions of
"suppresses Kitty replies for delete commands" and the two "... consistent ..." cases are left out.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Callable, Iterator

import pytest

from misaka.ui.tui.components import image as image_module
from misaka.ui.tui.components.image import (
    Image,
    ImageOptions,
    ImageTheme,
    setImageTranscoder,
)
from misaka.ui.tui.terminal_image import (
    CellDimensions,
    ImageDimensions,
    TerminalCapabilities,
    deleteKittyImage,
    detectCapabilities,
    encodeITerm2,
    encodeKitty,
    getCapabilities,
    hyperlink,
    imageFallback,
    isImageLine,
    renderImage,
    resetCapabilitiesCache,
    setCapabilities,
    setCapabilityOverrides,
    setCellDimensions,
)
from misaka.ui.tui.utils import visibleWidth
from misaka.utils import js_regexp
from misaka.utils.js_regexp import RegExp

ENV_KEYS = [
    "TERM",
    "TERM_PROGRAM",
    "TERMINAL_EMULATOR",
    "COLORTERM",
    "TMUX",
    "KITTY_WINDOW_ID",
    "GHOSTTY_RESOURCES_DIR",
    "WEZTERM_PANE",
    "ITERM_SESSION_ID",
    "WT_SESSION",
    "CMUX_WORKSPACE_ID",
    "WARP_SESSION_ID",
    "WARP_TERMINAL_SESSION_UUID",
    "MISAKA_HYPERLINKS",
    "MISAKA_IMAGE_PROTOCOL",
    "MISAKA_TRUE_COLOR",
]


@pytest.fixture(autouse=True)
def _restoreModuleState() -> Iterator[None]:
    # The capabilities, the cell size and the transcoder are module globals shared with every other test.
    yield
    setCapabilityOverrides({})
    resetCapabilitiesCache()
    setCellDimensions(CellDimensions(widthPx=9, heightPx=18))
    setImageTranscoder(None)


@pytest.fixture
def withEnv(monkeypatch: pytest.MonkeyPatch) -> Callable[..., object]:
    def run(overrides: dict[str, str | None], fn: Callable[[], object]) -> object:
        with monkeypatch.context() as patch:
            for key in ENV_KEYS:
                patch.delenv(key, raising=False)
            for key, value in overrides.items():
                if value is None:
                    patch.delenv(key, raising=False)
                else:
                    patch.setenv(key, value)
            return fn()

    return run


def caps(images, trueColor, hyperlinks) -> TerminalCapabilities:
    return TerminalCapabilities(images=images, trueColor=trueColor, hyperlinks=hyperlinks)


def fallbackTheme() -> ImageTheme:
    return ImageTheme(fallbackColor=lambda value: value)


# --- isImageLine: iTerm2 image protocol -----------------------------------------------------------


def test_should_detect_iterm2_image_escape_sequence_at_start_of_line():
    # iTerm2 image escape sequence: ESC ]1337;File=...
    iterm2ImageLine = "\x1b]1337;File=size=100,100;inline=1:base64encodeddata==\x07"
    assert isImageLine(iterm2ImageLine) is True


def test_should_detect_iterm2_image_escape_sequence_with_text_before_it():
    # Simulating a line that has text then image data (bug scenario)
    lineWithTextAndImage = "Some text \x1b]1337;File=size=100,100;inline=1:base64data==\x07 more text"
    assert isImageLine(lineWithTextAndImage) is True


def test_should_detect_iterm2_image_escape_sequence_in_middle_of_long_line():
    # Simulate a very long line with image data in the middle
    longLineWithImage = "Text before image..." + "\x1b]1337;File=inline=1:verylongbase64data==" + "...text after"
    assert isImageLine(longLineWithImage) is True


def test_should_detect_iterm2_image_escape_sequence_at_end_of_line():
    lineWithImageAtEnd = "Regular text ending with \x1b]1337;File=inline=1:base64data==\x07"
    assert isImageLine(lineWithImageAtEnd) is True


def test_should_detect_minimal_iterm2_image_escape_sequence():
    minimalImageLine = "\x1b]1337;File=:\x07"
    assert isImageLine(minimalImageLine) is True


# --- isImageLine: Kitty image protocol ------------------------------------------------------------


def test_should_detect_kitty_image_escape_sequence_at_start_of_line():
    # Kitty image escape sequence: ESC _G
    kittyImageLine = "\x1b_Ga=T,f=100,t=f,d=base64data...\x1b\\\x1b_Gm=i=1;\x1b\\"
    assert isImageLine(kittyImageLine) is True


def test_should_detect_kitty_image_escape_sequence_with_text_before_it():
    # Bug scenario: text + image data in same line
    lineWithTextAndKittyImage = "Output: \x1b_Ga=T,f=100;data...\x1b\\\x1b_Gm=i=1;\x1b\\"
    assert isImageLine(lineWithTextAndKittyImage) is True


def test_should_detect_kitty_image_escape_sequence_with_padding():
    # Kitty protocol adds padding to escape sequences
    kittyWithPadding = "  \x1b_Ga=T,f=100...\x1b\\\x1b_Gm=i=1;\x1b\\  "
    assert isImageLine(kittyWithPadding) is True


# --- isImageLine: Bug regression tests ------------------------------------------------------------


def test_should_detect_image_sequences_in_very_long_lines_304k_chars():
    # This simulates the crash scenario: a line with 304,401 chars
    # containing image escape sequences somewhere
    base64Char = "A" * 100  # 100 chars of base64-like data
    imageSequence = "\x1b]1337;File=size=800,600;inline=1:"

    # Build a long line with image sequence
    longLine = "Text prefix " + imageSequence + base64Char * 3000 + " suffix"  # ~300,000 chars

    assert (len(longLine) > 300000) is True
    assert isImageLine(longLine) is True


def test_should_detect_image_sequences_when_terminal_doesnt_support_images():
    # The bug occurred when getImageEscapePrefix() returned null
    # isImageLine should still detect image sequences regardless
    lineWithImage = "Read image file [image/jpeg]\x1b]1337;File=inline=1:base64data==\x07"
    assert isImageLine(lineWithImage) is True


def test_should_detect_image_sequences_with_ansi_codes_before_them():
    # Text might have ANSI styling before image data
    lineWithAnsiAndImage = "\x1b[31mError output \x1b]1337;File=inline=1:image==\x07"
    assert isImageLine(lineWithAnsiAndImage) is True


def test_should_detect_image_sequences_with_ansi_codes_after_them():
    lineWithImageAndAnsi = "\x1b_Ga=T,f=100:data...\x1b\\\x1b_Gm=i=1;\x1b\\\x1b[0m reset"
    assert isImageLine(lineWithImageAndAnsi) is True


# --- isImageLine: Negative cases - lines without images -------------------------------------------


def test_should_not_detect_images_in_plain_text_lines():
    plainText = "This is just a regular text line without any escape sequences"
    assert isImageLine(plainText) is False


def test_should_not_detect_images_in_lines_with_only_ansi_codes():
    ansiText = "\x1b[31mRed text\x1b[0m and \x1b[32mgreen text\x1b[0m"
    assert isImageLine(ansiText) is False


def test_should_not_detect_images_in_lines_with_cursor_movement_codes():
    cursorCodes = "\x1b[1A\x1b[2KLine cleared and moved up"
    assert isImageLine(cursorCodes) is False


def test_should_not_detect_images_in_lines_with_partial_iterm2_sequences():
    # Similar prefix but missing the complete sequence
    partialSequence = "Some text with ]1337;File but missing ESC at start"
    assert isImageLine(partialSequence) is False


def test_should_not_detect_images_in_lines_with_partial_kitty_sequences():
    # Similar prefix but missing the complete sequence
    partialSequence = "Some text with _G but missing ESC at start"
    assert isImageLine(partialSequence) is False


def test_should_not_detect_images_in_empty_lines():
    assert isImageLine("") is False


def test_should_not_detect_images_in_lines_with_newlines_only():
    assert isImageLine("\n") is False
    assert isImageLine("\n\n") is False


# --- isImageLine: Mixed content scenarios ---------------------------------------------------------


def test_should_detect_images_when_line_has_both_kitty_and_iterm2_sequences():
    mixedLine = "Kitty: \x1b_Ga=T...\x1b\\\x1b_Gm=i=1;\x1b\\ iTerm2: \x1b]1337;File=inline=1:data==\x07"
    assert isImageLine(mixedLine) is True


def test_should_detect_image_in_line_with_multiple_text_and_image_segments():
    complexLine = "Start \x1b]1337;File=img1==\x07 middle \x1b]1337;File=img2==\x07 end"
    assert isImageLine(complexLine) is True


def test_should_not_falsely_detect_image_in_line_with_file_path_containing_keywords():
    # File path might contain "1337" or "File" but without escape sequences
    filePathLine = "/path/to/File_1337_backup/image.jpg"
    assert isImageLine(filePathLine) is False


# --- detectCapabilities ---------------------------------------------------------------------------


def test_defaults_to_hyperlinks_false_for_unknown_terminals(withEnv):
    def check():
        caps_ = detectCapabilities()
        assert caps_.hyperlinks is False
        assert caps_.images is None

    withEnv({}, check)


def test_applies_environment_overrides(withEnv):
    assert withEnv(
        {"MISAKA_HYPERLINKS": "1", "MISAKA_IMAGE_PROTOCOL": "kitty", "MISAKA_TRUE_COLOR": "1"}, detectCapabilities
    ) == caps("kitty", True, True)
    assert withEnv(
        {"TERM_PROGRAM": "iterm.app", "MISAKA_HYPERLINKS": "0", "MISAKA_IMAGE_PROTOCOL": "none", "MISAKA_TRUE_COLOR": "0"},
        detectCapabilities,
    ) == caps(None, False, False)


def test_preserves_auto_detection_for_auto_environment_overrides(withEnv):
    assert withEnv(
        {
            "TERM_PROGRAM": "ghostty",
            "MISAKA_HYPERLINKS": "auto",
            "MISAKA_IMAGE_PROTOCOL": "auto",
            "MISAKA_TRUE_COLOR": "auto",
        },
        detectCapabilities,
    ) == caps("kitty", True, True)


def test_applies_and_clears_programmatic_overrides(withEnv):
    def check():
        setCapabilityOverrides({"images": None, "trueColor": False, "hyperlinks": False})
        try:
            assert getCapabilities() == caps(None, False, False)
            setCapabilityOverrides({})
            assert getCapabilities() == caps("kitty", True, True)
        finally:
            setCapabilityOverrides({})
            resetCapabilitiesCache()

    withEnv({"MISAKA_HYPERLINKS": "1", "MISAKA_IMAGE_PROTOCOL": "kitty", "MISAKA_TRUE_COLOR": "1"}, check)


def test_bypasses_the_tmux_probe_when_hyperlinks_are_overridden(withEnv):
    probed = False

    def probe() -> bool:
        nonlocal probed
        probed = True
        return False

    caps_ = withEnv(
        {"TMUX": "/tmp/tmux-1000/default,1234,0", "MISAKA_HYPERLINKS": "1", "MISAKA_IMAGE_PROTOCOL": "kitty"},
        lambda: detectCapabilities(probe),
    )
    assert probed is False
    assert caps_.hyperlinks is True
    assert caps_.images == "kitty"


def test_enables_hyperlinks_under_tmux_when_the_client_forwards_them(withEnv):
    def check():
        caps_ = detectCapabilities(lambda: True)
        assert caps_.hyperlinks is True
        assert caps_.images is None

    withEnv({"TMUX": "/tmp/tmux-1000/default,1234,0", "TERM_PROGRAM": "ghostty"}, check)


def test_disables_hyperlinks_under_tmux_when_the_client_does_not_forward_them(withEnv):
    def check():
        caps_ = detectCapabilities(lambda: False)
        assert caps_.hyperlinks is False
        assert caps_.images is None

    withEnv({"TMUX": "/tmp/tmux-1000/default,1234,0", "TERM_PROGRAM": "ghostty"}, check)


def test_checks_tmux_capability_when_term_starts_with_tmux(withEnv):
    def check():
        caps_ = detectCapabilities(lambda: True)
        assert caps_.hyperlinks is True
        assert caps_.images is None

        caps2 = detectCapabilities(lambda: False)
        assert caps2.hyperlinks is False

    withEnv({"TERM": "tmux-256color", "TERM_PROGRAM": "iterm.app"}, check)


def test_forces_hyperlinks_false_when_term_starts_with_screen(withEnv):
    def check():
        caps_ = detectCapabilities()
        assert caps_.hyperlinks is False
        assert caps_.images is None

    withEnv({"TERM": "screen-256color"}, check)


# #10573
def test_enables_hyperlinks_and_kitty_images_for_herdr(withEnv):
    def check():
        caps_ = detectCapabilities()
        assert caps_.hyperlinks is True
        assert caps_.images == "kitty"
        assert caps_.trueColor is True

    withEnv({"TERM_PROGRAM": "herdr", "TERM": "xterm-256color", "COLORTERM": "truecolor", "KITTY_WINDOW_ID": "1"}, check)
    withEnv({"TERM_PROGRAM": "herdr", "MISAKA_HYPERLINKS": "0"}, lambda: _assertFalse(detectCapabilities().hyperlinks))


def _assertFalse(value: bool) -> None:
    assert value is False


def test_enables_hyperlinks_for_ghostty(withEnv):
    withEnv({"TERM_PROGRAM": "ghostty"}, lambda: _assertTrue(detectCapabilities().hyperlinks))


def _assertTrue(value: bool) -> None:
    assert value is True


def test_does_not_disable_ghostty_images_solely_because_cmux_is_present(withEnv):
    def check():
        caps_ = detectCapabilities()
        assert caps_.images == "kitty"
        assert caps_.hyperlinks is True

    withEnv({"TERM_PROGRAM": "ghostty", "CMUX_WORKSPACE_ID": "workspace"}, check)


def test_enables_hyperlinks_for_kitty(withEnv):
    withEnv({"KITTY_WINDOW_ID": "1"}, lambda: _assertTrue(detectCapabilities().hyperlinks))


def test_enables_hyperlinks_for_wezterm(withEnv):
    withEnv({"WEZTERM_PANE": "0"}, lambda: _assertTrue(detectCapabilities().hyperlinks))


def _assertWarp(caps_: TerminalCapabilities) -> None:
    assert caps_.images == "kitty"
    assert caps_.trueColor is True
    assert caps_.hyperlinks is True


def test_enables_images_and_hyperlinks_for_warp_via_term_program(withEnv):
    withEnv({"TERM_PROGRAM": "WarpTerminal"}, lambda: _assertWarp(detectCapabilities()))


def test_enables_images_and_hyperlinks_for_warp_via_warp_session_id(withEnv):
    withEnv({"WARP_SESSION_ID": "some-session-id"}, lambda: _assertWarp(detectCapabilities()))


def test_enables_images_and_hyperlinks_for_warp_via_warp_terminal_session_uuid(withEnv):
    withEnv({"WARP_TERMINAL_SESSION_UUID": "d0e1a2e5-7ca7-44cd-9037-ac7222011161"}, lambda: _assertWarp(detectCapabilities()))


def test_disables_images_for_warp_inside_tmux(withEnv):
    def check():
        caps_ = detectCapabilities(lambda: True)
        assert caps_.images is None
        assert caps_.hyperlinks is True

    withEnv(
        {
            "TERM_PROGRAM": "WarpTerminal",
            "TMUX": "/tmp/tmux-1000/default,1234,0",
            "TERM": "tmux-256color",
        },
        check,
    )


def test_enables_hyperlinks_for_iterm2(withEnv):
    withEnv({"TERM_PROGRAM": "iterm.app"}, lambda: _assertTrue(detectCapabilities().hyperlinks))


def test_enables_hyperlinks_for_vscode(withEnv):
    withEnv({"TERM_PROGRAM": "vscode"}, lambda: _assertTrue(detectCapabilities().hyperlinks))


def test_enables_alacritty_capabilities_for_zed(withEnv):
    assert withEnv({"TERM_PROGRAM": "zed"}, detectCapabilities) == caps(None, True, True)


def test_enables_truecolor_and_hyperlinks_for_windows_terminal_outside_multiplexers(withEnv):
    def check():
        caps_ = detectCapabilities()
        assert caps_.trueColor is True
        assert caps_.hyperlinks is True
        assert caps_.images is None

    withEnv({"WT_SESSION": "session", "TERM": "xterm-256color"}, check)


def test_enables_truecolor_without_hyperlinks_for_jetbrains_terminal(withEnv):
    def check():
        caps_ = detectCapabilities()
        assert caps_.trueColor is True
        assert caps_.hyperlinks is False
        assert caps_.images is None

    withEnv({"TERMINAL_EMULATOR": "JetBrains-JediTerm", "TERM": "xterm-256color"}, check)


def test_does_not_inherit_windows_terminal_truecolor_through_tmux(withEnv):
    def check():
        caps_ = detectCapabilities(lambda: False)
        assert caps_.trueColor is False
        assert caps_.hyperlinks is False
        assert caps_.images is None

    withEnv({"WT_SESSION": "session", "TMUX": "/tmp/tmux-1000/default,1234,0", "TERM": "tmux-256color"}, check)


def test_trusts_explicit_truecolor_hints_through_tmux(withEnv):
    def check():
        caps_ = detectCapabilities(lambda: False)
        assert caps_.trueColor is True
        assert caps_.hyperlinks is False
        assert caps_.images is None

    withEnv({"COLORTERM": "truecolor", "TMUX": "/tmp/tmux-1000/default,1234,0", "TERM": "tmux-256color"}, check)


def test_detects_truecolor_from_direct_color_term_values(withEnv):
    withEnv({"TERM": "xterm-direct"}, lambda: _assertTrue(detectCapabilities(lambda: False).trueColor))


# MISAKA fork: a pane of MISAKA's panel shows neither images nor hyperlinks, whatever the host's variables say.
def test_disables_images_and_hyperlinks_in_a_panel_pane(withEnv):
    assert withEnv(
        {"TERM_PROGRAM": "misaka", "COLORTERM": "truecolor", "GHOSTTY_RESOURCES_DIR": "/x", "KITTY_WINDOW_ID": "1"},
        detectCapabilities,
    ) == caps(None, True, False)
    assert withEnv({"TERM_PROGRAM": "misaka", "MISAKA_HYPERLINKS": "1"}, detectCapabilities).hyperlinks is True


# --- iTerm2 image encoding ------------------------------------------------------------------------


def test_includes_the_decoded_payload_size_in_osc_1337_metadata():
    sequence = encodeITerm2("AAAA", {"width": 2, "height": "auto"})
    assert sequence == "\x1b]1337;File=inline=1;size=3;width=2;height=auto:AAAA\x07"


# --- Kitty image cursor movement ------------------------------------------------------------------


def test_can_request_no_terminal_side_cursor_movement():
    sequence = encodeKitty("AAAA", {"columns": 2, "rows": 2, "moveCursor": False})
    assert sequence.startswith("\x1b_Ga=T,f=100,q=2,C=1,c=2,r=2;")


def test_suppresses_kitty_replies_for_delete_commands():
    assert deleteKittyImage(42) == "\x1b_Ga=d,d=I,i=42,q=2\x1b\\"


def test_preserves_renderimages_default_terminal_side_cursor_movement():
    setCapabilities(caps("kitty", True, True))
    setCellDimensions({"widthPx": 10, "heightPx": 10})
    result = renderImage("AAAA", ImageDimensions(widthPx=20, heightPx=20), {"maxWidthCells": 2})
    assert result
    assert ",C=1," not in result.sequence
    assert result.rows == 2


def test_can_opt_renderimage_into_no_terminal_side_cursor_movement():
    setCapabilities(caps("kitty", True, True))
    setCellDimensions({"widthPx": 10, "heightPx": 10})
    result = renderImage("AAAA", ImageDimensions(widthPx=20, heightPx=20), {"maxWidthCells": 2, "moveCursor": False})
    assert result
    assert ",C=1," in result.sequence
    assert result.rows == 2


def test_honors_maxheightcells_by_reducing_rendered_width():
    setCapabilities(caps("kitty", True, True))
    setCellDimensions({"widthPx": 10, "heightPx": 10})
    result = renderImage("AAAA", ImageDimensions(widthPx=10, heightPx=100), {"maxWidthCells": 10, "maxHeightCells": 5})
    assert result
    assert result.rows == 5
    assert ",c=1,r=5" in result.sequence


def test_caps_image_component_height_to_a_square_pixel_box_by_default():
    setCapabilities(caps("kitty", True, True))
    setCellDimensions({"widthPx": 10, "heightPx": 20})
    image = Image(
        "AAAA",
        "image/png",
        fallbackTheme(),
        ImageOptions(maxWidthCells=10),
        ImageDimensions(widthPx=10, heightPx=100),
    )
    lines = image.render(12)
    assert len(lines) == 5
    assert ",c=1,r=5" in lines[0]


def test_places_image_sequence_on_first_line_with_empty_padding_rows():
    setCapabilities(caps("kitty", True, True))
    setCellDimensions({"widthPx": 10, "heightPx": 10})
    image = Image(
        "AAAA",
        "image/png",
        fallbackTheme(),
        ImageOptions(maxWidthCells=2),
        ImageDimensions(widthPx=20, heightPx=20),
    )
    lines = image.render(4)
    imageId = image.getImageId()
    assert isinstance(imageId, int)
    assert lines[0].startswith("\x1b_G")
    assert ",C=1," in lines[0]
    assert f",i={imageId}" in lines[0]
    assert lines[0].endswith("\x1b\\")
    assert lines[1 : len(lines)] == [""]


def test_truncates_long_image_fallback_lines_to_render_width():
    setCapabilities(caps(None, False, False))
    longPath = os.path.join(
        os.path.expanduser("~"),
        "images",
        f"{'generated-image-with-a-very-long-absolute-path' * 4}.png",
    )
    width = 40
    image = Image(
        "AAAA",
        "image/png",
        ImageTheme(fallbackColor=lambda value: f"\x1b[33m{value}\x1b[0m"),
        ImageOptions(filename=longPath),
        ImageDimensions(widthPx=1280, heightPx=720),
    )
    lines = image.render(width)
    assert len(lines) == 1
    assert visibleWidth(lines[0]) <= width, (
        f"fallback line wider than {width}: visible={visibleWidth(lines[0])} raw={lines[0]!r}"
    )
    assert "..." in lines[0], "expected ellipsis when truncating long fallback path"
    assert "~" in lines[0], "expected home-shortened path in fallback"


# --- image cell sizing (#8938: reduce Kitty placement distortion without shrinking iTerm2 reservations) ---


@pytest.fixture
def kitty() -> None:
    setCapabilities(caps("kitty", True, True))


@pytest.fixture
def iterm2() -> None:
    setCapabilities(caps("iterm2", True, True))


def test_reserves_at_least_one_kitty_row_for_thin_images(kitty):
    setCellDimensions({"widthPx": 9, "heightPx": 18})
    result = renderImage("AAAA", ImageDimensions(widthPx=1200, heightPx=12), {"maxWidthCells": 60})
    assert result
    assert result.rows == 1
    assert ",c=60,r=1;" in result.sequence


def test_keeps_kitty_placement_and_reserved_lines_consistent_across_width_changes(kitty):
    setCellDimensions({"widthPx": 9, "heightPx": 18})
    image = Image(
        "AAAA",
        "image/png",
        fallbackTheme(),
        ImageOptions(maxWidthCells=60, imageId=8938),
        ImageDimensions(widthPx=615, heightPx=86),
    )
    lines = image.render(62)
    assert len(lines) == 4
    assert lines[1:] == ["", "", ""]
    assert ",c=60,r=4,i=8938;" in lines[0]

    narrowerLines = image.render(32)
    assert len(narrowerLines) == 2
    assert ",c=30,r=2,i=8938;" in narrowerLines[0]


def test_keeps_the_ceiling_placement_when_rounding_down_would_increase_distortion(kitty):
    setCellDimensions({"widthPx": 15, "heightPx": 28})
    result = renderImage("AAAA", ImageDimensions(widthPx=615, heightPx=86), {"maxWidthCells": 60})
    assert result
    assert result.rows == 5
    assert ",c=60,r=5;" in result.sequence


def test_keeps_height_limited_kitty_columns_and_reservations_consistent(kitty):
    setCellDimensions({"widthPx": 14, "heightPx": 28})
    image = Image(
        "AAAA",
        "image/png",
        fallbackTheme(),
        ImageOptions(maxWidthCells=30, imageId=8938),
        ImageDimensions(widthPx=400, heightPx=900),
    )
    lines = image.render(32)
    assert len(lines) == 15
    assert ",c=13,r=15,i=8938;" in lines[0]
    narrowerLines = image.render(22)
    assert len(narrowerLines) == 10
    assert ",c=9,r=10,i=8938;" in narrowerLines[0]


def test_chooses_thin_kitty_widths_by_proportions_while_keeping_at_least_one_column(kitty):
    setCellDimensions({"widthPx": 1, "heightPx": 1})
    for widthPx, columns in [(1, 1), (140, 1), (149, 2)]:
        result = renderImage(
            "AAAA", ImageDimensions(widthPx=widthPx, heightPx=1000), {"maxWidthCells": 30, "maxHeightCells": 10}
        )
        assert result
        assert result.columns == columns
        assert result.rows == 10
        assert f",c={columns},r=10;" in result.sequence


def test_keeps_iterm2s_ceiling_width_when_height_limited(iterm2):
    setCellDimensions({"widthPx": 14, "heightPx": 28})
    result = renderImage("AAAA", ImageDimensions(widthPx=400, heightPx=900), {"maxWidthCells": 30, "maxHeightCells": 15})
    assert result
    assert result.columns == 14
    assert result.rows == 15
    assert result.sequence == "\x1b]1337;File=inline=1;size=3;width=14;height=auto:AAAA\x07"


def test_keeps_iterm2s_ceiling_based_reserved_lines_and_cursor_offset(iterm2):
    setCellDimensions({"widthPx": 9, "heightPx": 18})
    image = Image(
        "AAAA",
        "image/png",
        fallbackTheme(),
        ImageOptions(maxWidthCells=60),
        ImageDimensions(widthPx=615, heightPx=86),
    )
    assert image.render(62) == [
        "",
        "",
        "",
        "",
        "\x1b[4A\x1b]1337;File=inline=1;size=3;width=60;height=auto:AAAA\x07",
    ]


# --- imageFallback --------------------------------------------------------------------------------

# MISAKA fork: the expected display path uses the platform's separator (upstream writes `~/...`, which only holds on
# POSIX; Node shortens `C:\Users\x\.pi\...` to `~\.pi\...` as well).
shortenedShot = os.path.join("~", ".pi", "agent", "shot.png")


def test_shortens_home_prefixed_absolute_paths_without_hyperlinks():
    setCapabilities(caps(None, False, False))
    abs_ = os.path.join(os.path.expanduser("~"), ".pi", "agent", "shot.png")
    result = imageFallback("image/png", ImageDimensions(widthPx=1280, heightPx=720), abs_)
    assert result == f"[Image: {shortenedShot} [image/png] 1280x720]"


def test_wraps_shortened_absolute_paths_in_osc_8_file_links_when_hyperlinks_are_enabled():
    setCapabilities(caps(None, False, True))
    abs_ = os.path.join(os.path.expanduser("~"), ".pi", "agent", "shot.png")
    result = imageFallback("image/png", ImageDimensions(widthPx=10, heightPx=10), abs_)
    assert "\x1b]8;;file://" in result, "expected OSC 8 file link"
    assert abs_.replace("\\", "/") in result or abs_ in result, "file URL should target absolute path"
    # Visible text must use ~/... not the expanded home path.
    visible = js_regexp.replace(result, RegExp(r"\x1b\]8;;.*?\x1b\\", "g"), "")
    assert visible == f"[Image: {shortenedShot} [image/png] 10x10]"


def test_leaves_bare_basenames_unchanged_and_does_not_hyperlink_them():
    setCapabilities(caps(None, False, True))
    result = imageFallback("image/png", ImageDimensions(widthPx=1, heightPx=1), "clankolas.png")
    assert result == "[Image: clankolas.png [image/png] 1x1]"
    assert "\x1b]8;" not in result, "basename must not be hyperlinked"


def test_omits_filename_segment_when_not_provided():
    setCapabilities(caps(None, False, False))
    assert imageFallback("image/png", ImageDimensions(widthPx=8, heightPx=6)) == "[Image: [image/png] 8x6]"


# --- Image transcoding (Kitty only accepts PNG (f=100); non-PNG images must be transcoded (#10292)) ---

jpeg = base64.b64encode(b"jpeg").decode()
# Minimal PNG header (signature + IHDR) for a 40x10 image. Enough for getPngDimensions.
png = base64.b64encode(bytes.fromhex("89504e470d0a1a0a0000000d49484452000000280000000a")).decode()


@pytest.fixture
def transcoding() -> Iterator[list[str]]:
    calls: list[str] = []
    setCapabilities(caps("kitty", True, True))
    setCellDimensions({"widthPx": 10, "heightPx": 10})
    yield calls


def render(data: str, mimeType: str) -> list[str]:
    return Image(data, mimeType, fallbackTheme(), ImageOptions(), ImageDimensions(widthPx=20, heightPx=20)).render(20)


def makeTranscode(calls: list[str]) -> Callable[[str, str], str | None]:
    def transcode(data: str, _mimeType: str) -> str | None:
        calls.append(data)
        return png if data == jpeg else None

    return transcode


def test_sends_converted_png_data_sized_from_the_png(transcoding):
    setImageTranscoder(makeTranscode(transcoding))
    lines = render(jpeg, "image/jpeg")
    assert "f=100" in lines[0] and f";{png}\x1b\\" in lines[0]
    # 40x10 PNG at 18 columns: 5 rows, not the 18 rows of the 20x20 source dimensions.
    assert len(lines) == 5


def test_renders_a_text_fallback_until_a_working_transcoder_is_registered(transcoding):
    image = Image(jpeg, "image/jpeg", fallbackTheme())
    assert RegExp(r"^\[Image: \[image\/jpeg\]").test(image.render(80)[0])
    setImageTranscoder(lambda _data, _mimeType: None)
    image.invalidate()
    assert RegExp(r"^\[Image: \[image\/jpeg\]").test(image.render(80)[0])
    setImageTranscoder(makeTranscode(transcoding))
    image.invalidate()
    assert "\x1b_G" in image.render(80)[0]


def test_converts_each_image_once(transcoding):
    setImageTranscoder(makeTranscode(transcoding))
    image = Image(jpeg, "image/jpeg", fallbackTheme())
    image.render(80)
    render(jpeg, "image/jpeg")  # New instance hits the shared cache.
    for i in range(40):
        render(f"other-{i}", "image/jpeg")  # Evicts the shared entry.
    image.invalidate()
    image.render(40)  # Instance keeps its own PNG.
    assert len([data for data in transcoding if data == jpeg]) == 1
    assert len(image_module.pngCache) == 32


def test_does_not_convert_png_data_or_iterm2_output(transcoding):
    setImageTranscoder(makeTranscode(transcoding))
    assert f";{png}\x1b\\" in render(png, "image/png")[0]
    setCapabilities(caps("iterm2", True, True))
    assert render(jpeg, "image/jpeg")[-1].endswith(f":{jpeg}\x07")
    assert transcoding == []


# --- hyperlink ------------------------------------------------------------------------------------


def test_wraps_text_in_osc_8_open_and_close_sequences():
    result = hyperlink("click me", "https://example.com")
    assert result == "\x1b]8;;https://example.com\x1b\\click me\x1b]8;;\x1b\\"


def test_preserves_ansi_styling_inside_the_hyperlink():
    styled = "\x1b[4m\x1b[34mclick me\x1b[0m"
    result = hyperlink(styled, "https://example.com")
    assert result.startswith("\x1b]8;;https://example.com\x1b\\")
    assert styled in result
    assert result.endswith("\x1b]8;;\x1b\\")


def test_works_with_empty_text():
    result = hyperlink("", "https://example.com")
    assert result == "\x1b]8;;https://example.com\x1b\\\x1b]8;;\x1b\\"


def test_works_with_file_uris():
    result = hyperlink("README.md", "file:///home/user/README.md")
    assert "file:///home/user/README.md" in result
    assert "README.md" in result


# --- MISAKA ---------------------------------------------------------------------------------------


def test_file_links_encode_paths_as_node_does(monkeypatch):
    from misaka.ui.tui import terminal_image

    monkeypatch.setattr(terminal_image.sys, "platform", "linux")
    monkeypatch.setattr(terminal_image.node_path, "resolve", terminal_image.node_path.posixResolve)
    monkeypatch.setattr(terminal_image.node_path, "sep", "/")
    assert terminal_image.pathToFileURL("/tmp/a b/(c)!#?%.png") == "file:///tmp/a%20b/(c)!%23%3F%25.png"
    assert terminal_image.pathToFileURL("/tmp/日本.png") == "file:///tmp/%E6%97%A5%E6%9C%AC.png"
    assert terminal_image.pathToFileURL("/tmp/~[x]^.png") == "file:///tmp/%7E%5Bx%5D%5E.png"
    # `path.resolve` drops the trailing separator; `pathToFileURL` keeps it, and collapses a leading `//`.
    assert terminal_image.pathToFileURL("/tmp/dir/") == "file:///tmp/dir/"
    assert terminal_image.pathToFileURL("//tmp/dir") == "file:///tmp/dir"

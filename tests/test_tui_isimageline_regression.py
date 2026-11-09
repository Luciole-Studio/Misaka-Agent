"""Translated from pi-tui's ``test/bug-regression-isimageline-startswith-bug.test.ts`` (v1.1.0).

Bug regression test for isImageLine() crash scenario

Bug: When isImageLine() used startsWith() and terminal doesn't support images,
it would return false for lines containing image escape sequences, causing TUI to
crash with "Rendered line exceeds terminal width" error.

Fix: Changed to use includes() to detect escape sequences anywhere in the line.

This test demonstrates:
1. The bug scenario with the old implementation
2. That the fix works correctly
"""

from misaka.ui.tui.terminal_image import isImageLine

# --- Bug scenario: Terminal without image support -------------------------------------------------


def test_old_implementation_would_return_false_causing_crash():
    # OLD IMPLEMENTATION (buggy):
    #   export function isImageLine(line: string): boolean {
    #     const prefix = getImageEscapePrefix();
    #     return prefix !== null && line.startsWith(prefix);
    #   }
    #
    # When terminal doesn't support images:
    # - getImageEscapePrefix() returns null
    # - isImageLine() returns false even for lines containing image sequences
    # - TUI performs width check on line containing 300KB+ of base64 data
    # - Crash: "Rendered line exceeds terminal width (304401 > 115)"

    # Simulate old implementation behavior
    def oldIsImageLine(line: str, imageEscapePrefix: str | None) -> bool:
        return imageEscapePrefix is not None and line.startswith(imageEscapePrefix)

    # When terminal doesn't support images, prefix is null
    terminalWithoutImageSupport = None

    # Line containing image escape sequence with text before it (common bug scenario)
    lineWithImageSequence = "Read image file [image/jpeg]\x1b]1337;File=size=800,600;inline=1:base64data...\x07"

    # Old implementation would return false (BUG!)
    oldResult = oldIsImageLine(lineWithImageSequence, terminalWithoutImageSupport)
    assert oldResult is False, (
        "Bug: old implementation returns false for line containing image sequence when terminal has no image support"
    )


def test_new_implementation_returns_true_correctly():
    # Line containing image escape sequence with text before it
    lineWithImageSequence = "Read image file [image/jpeg]\x1b]1337;File=size=800,600;inline=1:base64data...\x07"

    # New implementation should return true (FIX!)
    newResult = isImageLine(lineWithImageSequence)
    assert newResult is True, "Fix: new implementation returns true for line containing image sequence"


def test_new_implementation_detects_kitty_sequences_in_any_position():
    scenarios = [
        "At start: \x1b_Ga=T,f=100,data...\x1b\\",
        "Prefix \x1b_Ga=T,data...\x1b\\",
        "Suffix text \x1b_Ga=T,data...\x1b\\ suffix",
        "Middle \x1b_Ga=T,data...\x1b\\ more text",
        # Very long line (simulating 300KB+ crash scenario)
        f"Text before \x1b_Ga=T,f=100{'A' * 300000} text after",
    ]

    for line in scenarios:
        assert isImageLine(line) is True, f"Should detect Kitty sequence in: {line[:50]}..."


def test_new_implementation_detects_iterm2_sequences_in_any_position():
    scenarios = [
        "At start: \x1b]1337;File=size=100,100:base64...\x07",
        "Prefix \x1b]1337;File=inline=1:data==\x07",
        "Suffix text \x1b]1337;File=inline=1:data==\x07 suffix",
        "Middle \x1b]1337;File=inline=1:data==\x07 more text",
        # Very long line (simulating 304KB crash scenario)
        f"Text before \x1b]1337;File=size=800,600;inline=1:{'B' * 300000} text after",
    ]

    for line in scenarios:
        assert isImageLine(line) is True, f"Should detect iTerm2 sequence in: {line[:50]}..."


# --- Integration: Tool execution scenario ---------------------------------------------------------
# This simulates what happens when the `read` tool reads an image file. The tool result contains both
# text and image content; when this is rendered, the image component creates escape sequences.
# If isImageLine() doesn't detect them, TUI crashes.


def test_detects_image_sequences_in_read_tool_output():
    # Simulate output when read tool processes an image
    # The line might have text from the read result plus the image escape sequence
    toolOutputLine = "Read image file [image/jpeg]\x1b]1337;File=size=800,600;inline=1:base64image...\x07"

    assert isImageLine(toolOutputLine) is True, "Should detect image sequence in tool output line"


def test_detects_kitty_sequences_from_image_component():
    # Kitty image component creates multi-line output with escape sequences
    kittyLine = "\x1b_Ga=T,f=100,t=f,d=base64data...\x1b\\\x1b_Gm=i=1;\x1b\\"

    assert isImageLine(kittyLine) is True, "Should detect Kitty image component output"


def test_handles_ansi_codes_before_image_sequences():
    # Line might have styling (error, warning, etc.) before image data
    lines = [
        "\x1b[31mError\x1b[0m: \x1b]1337;File=inline=1:base64==\x07",
        "\x1b[33mWarning\x1b[0m: \x1b_Ga=T,data...\x1b\\",
        "\x1b[1mBold\x1b[0m \x1b]1337;File=:base64==\x07\x1b[0m",
    ]

    for line in lines:
        assert isImageLine(line) is True, f"Should detect image sequence after ANSI codes: {line[:30]}..."


# --- Crash scenario simulation --------------------------------------------------------------------


def test_does_not_crash_on_very_long_lines_with_image_sequences():
    # Simulate the exact crash scenario:
    # - Line is 304,401 characters (the crash log showed 58649 > 115)
    # - Contains image escape sequence somewhere in the middle
    # - Old implementation would return false, causing TUI to do width check
    # - New implementation returns true, skipping width check (preventing crash)

    base64Char = "A" * 100
    iterm2Sequence = "\x1b]1337;File=size=800,600;inline=1:"

    # Build a line that would cause the crash
    crashLine = "Output: " + iterm2Sequence + base64Char * 3040 + " end of output"  # ~304,000 chars

    # Verify line is very long
    assert len(crashLine) > 300000, "Test line should be > 300KB"

    # New implementation should detect it (prevents crash)
    detected = isImageLine(crashLine)
    assert detected is True, "Should detect image sequence in very long line, preventing TUI crash"


def test_handles_lines_exactly_matching_crash_log_dimensions():
    # Crash log showed: line 58649 chars wide, terminal width 115
    # Let's create a line with similar characteristics
    targetWidth = 58649
    prefix = "Text"
    sequence = "\x1b_Ga=T,f=100"
    suffix = "End"
    padding = "A" * (targetWidth - len(prefix) - len(sequence) - len(suffix))
    line = f"{prefix}{sequence}{padding}{suffix}"

    assert len(line) == 58649
    assert isImageLine(line) is True, "Should detect image sequence in 58649-char line"


# --- Negative cases: Don't false positive ---------------------------------------------------------


def test_does_not_detect_images_in_regular_long_text():
    # Very long line WITHOUT image sequences
    longText = "A" * 100000

    assert isImageLine(longText) is False, "Should not detect images in plain long text"


def test_does_not_detect_images_in_lines_with_file_paths():
    filePaths = [
        "/path/to/1337/image.jpg",
        "/usr/local/bin/File_converter",
        "~/Documents/1337File_backup.png",
        "./_G_test_file.txt",
    ]

    for path in filePaths:
        assert isImageLine(path) is False, f"Should not falsely detect image sequence in path: {path}"

"""Translated from pi-tui's ``test/markdown.test.ts`` (v1.1.0).

MISAKA fork: ``chalk`` (level 3) is ``tests.tui_test_themes.chalk``; the cell attribute reads go through
``VirtualTerminal.getCell``.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from misaka.ui.tui.components.markdown import (
    DefaultTextStyle,
    Markdown,
    MarkdownOptions,
    MarkdownTheme,
)
from misaka.ui.tui.terminal_image import (
    TerminalCapabilities,
    resetCapabilitiesCache,
    setCapabilities,
)
from misaka.ui.tui.tui_main_screen import TuiMainScreen
from tests.tui_test_themes import chalk, defaultMarkdownTheme
from tests.tui_virtual_terminal import VirtualTerminal

_SGR = re.compile(r"\x1b\[[0-9;]*m")


def stripAnsi(line: str) -> str:
    return _SGR.sub("", line)


def plain(lines: list[str]) -> list[str]:
    return [_SGR.sub("", line) for line in lines]


# --- Transforms -----------------------------------------------------------------------------------


def test_caches_transformed_markdown_by_source_and_available_width():
    calls: list[dict[str, Any]] = []

    def transform(source: str, availableWidth: int) -> str:
        calls.append({"source": source, "availableWidth": availableWidth})
        return f"{source} {availableWidth}"

    markdown = Markdown("source", 2, 0, defaultMarkdownTheme, None, MarkdownOptions(transform=transform))

    assert [stripAnsi(line).strip() for line in markdown.render(80)] == ["source 76"]
    markdown.render(80)
    assert [stripAnsi(line).strip() for line in markdown.render(60)] == ["source 56"]
    assert calls == [
        {"source": "source", "availableWidth": 76},
        {"source": "source", "availableWidth": 56},
    ]

    markdown.setText("updated")
    assert [stripAnsi(line).strip() for line in markdown.render(60)] == ["updated 56"]
    assert calls[-1] == {"source": "updated", "availableWidth": 56}

    markdown.invalidate()
    markdown.render(60)
    assert calls[-1] == {"source": "updated", "availableWidth": 56}
    assert len(calls) == 4


# --- Lists ----------------------------------------------------------------------------------------


def test_should_render_simple_nested_list():
    markdown = Markdown(
        """- Item 1
  - Nested 1.1
  - Nested 1.2
- Item 2""",
        0,
        0,
        defaultMarkdownTheme,
    )

    lines = markdown.render(80)

    # Check that we have content
    assert len(lines) > 0

    # Strip ANSI codes for checking
    plainLines = plain(lines)

    # Check structure
    assert any("- Item 1" in line for line in plainLines)
    assert any("    - Nested 1.1" in line for line in plainLines)
    assert any("    - Nested 1.2" in line for line in plainLines)
    assert any("- Item 2" in line for line in plainLines)


def test_should_render_deeply_nested_list():
    markdown = Markdown(
        """- Level 1
  - Level 2
    - Level 3
      - Level 4""",
        0,
        0,
        defaultMarkdownTheme,
    )

    lines = markdown.render(80)
    plainLines = plain(lines)

    # Check proper indentation
    assert any("- Level 1" in line for line in plainLines)
    assert any("    - Level 2" in line for line in plainLines)
    assert any("        - Level 3" in line for line in plainLines)
    assert any("            - Level 4" in line for line in plainLines)


def test_should_render_ordered_nested_list():
    markdown = Markdown(
        """1. First
   1. Nested first
   2. Nested second
2. Second""",
        0,
        0,
        defaultMarkdownTheme,
    )

    lines = markdown.render(80)
    plainLines = plain(lines)

    assert any("1. First" in line for line in plainLines)
    assert any("    1. Nested first" in line for line in plainLines)
    assert any("    2. Nested second" in line for line in plainLines)
    assert any("2. Second" in line for line in plainLines)


def test_should_normalize_ordered_list_markers_by_default():
    markdown = Markdown("1. alpha\n1. beta\n1. gamma", 0, 0, defaultMarkdownTheme)

    lines = [stripAnsi(line).rstrip() for line in markdown.render(80)]

    assert lines == ["1. alpha", "2. beta", "3. gamma"]


def test_should_preserve_source_list_markers_when_configured():
    markdown = Markdown(
        "  4. forth\n  3. third\n\n10) ten\n7) seven\n\n+ plus\n* star\n- minus\n+",
        0,
        0,
        defaultMarkdownTheme,
        None,
        MarkdownOptions(preserveOrderedListMarkers=True),
    )

    lines = [stripAnsi(line).rstrip() for line in markdown.render(80)]

    assert lines == [
        "4. forth",
        "3. third",
        "",
        "10) ten",
        "7) seven",
        "",
        "+ plus",
        "* star",
        "- minus",
        "+",
    ]


def test_should_render_mixed_ordered_and_unordered_nested_lists():
    markdown = Markdown(
        """1. Ordered item
   - Unordered nested
   - Another nested
2. Second ordered
   - More nested""",
        0,
        0,
        defaultMarkdownTheme,
    )

    lines = markdown.render(80)
    plainLines = plain(lines)

    assert any("1. Ordered item" in line for line in plainLines)
    assert any("    - Unordered nested" in line for line in plainLines)
    assert any("2. Second ordered" in line for line in plainLines)


def test_should_render_blank_lines_between_loose_list_items():
    markdown = Markdown(
        """1. Lorem ipsum dolor sit amet.

   Ut enim ad minim veniam.

2. Duis aute irure dolor.

   Excepteur sint occaecat cupidatat.

3. Beep boop""",
        0,
        0,
        defaultMarkdownTheme,
    )

    lines = [stripAnsi(line).rstrip() for line in markdown.render(80)]

    assert lines == [
        "1. Lorem ipsum dolor sit amet.",
        "",
        "   Ut enim ad minim veniam.",
        "",
        "2. Duis aute irure dolor.",
        "",
        "   Excepteur sint occaecat cupidatat.",
        "",
        "3. Beep boop",
    ]


def test_should_render_task_list_markers():
    markdown = Markdown("- [ ] beep\n- [x] boop", 0, 0, defaultMarkdownTheme)

    lines = [stripAnsi(line).rstrip() for line in markdown.render(80)]

    assert lines == ["- [ ] beep", "- [x] boop"]


def test_should_maintain_numbering_when_code_blocks_are_not_indented_llm_output():
    # When code blocks aren't indented, marked parses each item as a separate list.
    # We use token.start to preserve the original numbering.
    markdown = Markdown(
        """1. First item

```typescript
// code block
```

2. Second item

```typescript
// another code block
```

3. Third item""",
        0,
        0,
        defaultMarkdownTheme,
    )

    lines = markdown.render(80)
    plainLines = [line.strip() for line in plain(lines)]

    # Find all lines that start with a number and period
    numberedLines = [line for line in plainLines if re.match(r"^\d+\.", line)]

    # Should have 3 numbered items
    assert len(numberedLines) == 3, f"Expected 3 numbered items, got: {', '.join(numberedLines)}"

    # Check the actual numbers
    assert numberedLines[0].startswith("1."), f'First item should be "1.", got: {numberedLines[0]}'
    assert numberedLines[1].startswith("2."), f'Second item should be "2.", got: {numberedLines[1]}'
    assert numberedLines[2].startswith("3."), f'Third item should be "3.", got: {numberedLines[2]}'


def test_should_indent_wrapped_unordered_list_lines():
    markdown = Markdown("- alpha beta gamma delta epsilon", 0, 0, defaultMarkdownTheme)

    lines = [stripAnsi(line).rstrip() for line in markdown.render(20)]

    assert lines == ["- alpha beta gamma", "  delta epsilon"]


def test_should_indent_wrapped_ordered_list_lines():
    markdown = Markdown("1. alpha beta gamma delta epsilon", 0, 0, defaultMarkdownTheme)

    lines = [stripAnsi(line).rstrip() for line in markdown.render(20)]

    assert lines == ["1. alpha beta gamma", "   delta epsilon"]


def test_should_indent_wrapped_ordered_list_lines_with_multi_digit_markers():
    markdown = Markdown("10. alpha beta gamma delta epsilon", 0, 0, defaultMarkdownTheme)

    lines = [stripAnsi(line).rstrip() for line in markdown.render(21)]

    assert lines == ["10. alpha beta gamma", "    delta epsilon"]


def test_should_indent_wrapped_nested_list_lines():
    markdown = Markdown("- parent\n  - alpha beta gamma delta epsilon", 0, 0, defaultMarkdownTheme)

    lines = [stripAnsi(line).rstrip() for line in markdown.render(24)]

    assert lines == ["- parent", "    - alpha beta gamma", "      delta epsilon"]


def test_should_indent_wrapped_nested_list_lines_under_ordered_parents():
    markdown = Markdown("1. parent\n   - alpha beta gamma delta epsilon", 0, 0, defaultMarkdownTheme)

    lines = [stripAnsi(line).rstrip() for line in markdown.render(24)]

    assert lines == ["1. parent", "    - alpha beta gamma", "      delta epsilon"]


def test_should_render_and_wrap_blockquotes_inside_list_items():
    markdown = Markdown("- > alpha beta gamma delta epsilon zeta", 0, 0, defaultMarkdownTheme)

    lines = [stripAnsi(line).rstrip() for line in markdown.render(24)]

    assert lines == ["- │ alpha beta gamma", "  │ delta epsilon zeta"]


def test_should_render_and_wrap_code_blocks_inside_list_items():
    markdown = Markdown(
        "- ```ts\n  alpha beta gamma delta epsilon zeta\n  ```",
        0,
        0,
        defaultMarkdownTheme,
    )

    lines = [stripAnsi(line).rstrip() for line in markdown.render(24)]

    assert lines == ["- ```ts", "    alpha beta gamma", "  delta epsilon zeta", "  ```"]


# --- Tables ---------------------------------------------------------------------------------------

SIMPLE_TABLE = """| Name | Age |
| --- | --- |
| Alice | 30 |
| Bob | 25 |"""


def test_should_render_simple_table():
    markdown = Markdown(SIMPLE_TABLE, 0, 0, defaultMarkdownTheme)

    lines = markdown.render(80)
    plainLines = plain(lines)

    # Check table structure
    assert any("Name" in line for line in plainLines)
    assert any("Age" in line for line in plainLines)
    assert any("Alice" in line for line in plainLines)
    assert any("Bob" in line for line in plainLines)
    # Check for table borders
    assert any("│" in line for line in plainLines)
    assert any("─" in line for line in plainLines)


def test_should_render_row_dividers_between_data_rows():
    markdown = Markdown(SIMPLE_TABLE, 0, 0, defaultMarkdownTheme)

    lines = markdown.render(80)
    plainLines = plain(lines)
    dividerLines = [line for line in plainLines if "┼" in line]

    assert len(dividerLines) == 2, "Expected header + row divider"


def test_should_keep_column_width_at_least_the_longest_word():
    longestWord = "superlongword"
    markdown = Markdown(
        f"""| Column One | Column Two |
| --- | --- |
| {longestWord} short | otherword |
| small | tiny |""",
        0,
        0,
        defaultMarkdownTheme,
    )

    lines = markdown.render(32)
    plainLines = plain(lines)
    dataLine = next((line for line in plainLines if longestWord in line), None)
    assert dataLine, "Expected data row containing longest word"

    segments = dataLine.split("│")[1:-1]
    firstSegment = segments[0] if segments else None
    assert firstSegment, "Expected first column segment"
    firstColumnWidth = len(firstSegment) - 2

    assert firstColumnWidth >= len(longestWord), (
        f"Expected first column width >= {len(longestWord)}, got {firstColumnWidth}"
    )


def test_should_render_table_with_alignment():
    markdown = Markdown(
        """| Left | Center | Right |
| :--- | :---: | ---: |
| A | B | C |
| Long text | Middle | End |""",
        0,
        0,
        defaultMarkdownTheme,
    )

    lines = markdown.render(80)
    plainLines = plain(lines)

    # Check headers
    assert any("Left" in line for line in plainLines)
    assert any("Center" in line for line in plainLines)
    assert any("Right" in line for line in plainLines)
    # Check content
    assert any("Long text" in line for line in plainLines)


def test_should_handle_tables_with_varying_column_widths():
    markdown = Markdown(
        """| Short | Very long column header |
| --- | --- |
| A | This is a much longer cell content |
| B | Short |""",
        0,
        0,
        defaultMarkdownTheme,
    )

    lines = markdown.render(80)

    # Should render without errors
    assert len(lines) > 0

    plainLines = plain(lines)
    assert any("Very long column header" in line for line in plainLines)
    assert any("This is a much longer cell content" in line for line in plainLines)


def test_should_wrap_table_cells_when_table_exceeds_available_width():
    markdown = Markdown(
        """| Command | Description | Example |
| --- | --- | --- |
| npm install | Install all dependencies | npm install |
| npm run build | Build the project | npm run build |""",
        0,
        0,
        defaultMarkdownTheme,
    )

    # Render at narrow width that forces wrapping
    lines = markdown.render(50)
    plainLines = [line.rstrip() for line in plain(lines)]

    # All lines should fit within width
    for line in plainLines:
        assert len(line) <= 50, f'Line exceeds width 50: "{line}" (length: {len(line)})'

    # Content should still be present (possibly wrapped across lines)
    allText = " ".join(plainLines)
    assert "Command" in allText, "Should contain 'Command'"
    assert "Description" in allText, "Should contain 'Description'"
    assert "npm install" in allText, "Should contain 'npm install'"
    assert "Install" in allText, "Should contain 'Install'"


async def test_should_not_leak_wrapped_link_styles_into_table_borders_or_plain_cells():
    source = """| Link | Plain |
| --- | --- |
| [**one two three four five six**](https://example.com) | normal text |"""

    try:
        for hyperlinks in [True, False]:
            setCapabilities(TerminalCapabilities(images=None, trueColor=False, hyperlinks=hyperlinks))
            terminal = VirtualTerminal(24, 16)
            tui = TuiMainScreen(terminal)
            tui.addChild(Markdown(source, 0, 0, defaultMarkdownTheme))
            tui.start()

            try:
                await terminal.waitForRender()
                viewport = terminal.getViewport()
                row = next((index for index, line in enumerate(viewport) if "one" in line and "norm" in line), -1)
                assert row != -1, f"Missing wrapped table row: {viewport!r}"
                line = viewport[row]
                linkCol = line.find("one")
                separatorCol = line.find("│", linkCol)
                plainCol = line.find("norm")
                assert linkCol >= 0 and separatorCol > linkCol and plainCol > separatorCol
                assert terminal.getCell(row, linkCol).isFgDefault() is False
                assert terminal.getCell(row, separatorCol).isFgDefault() is True
                assert terminal.getCell(row, plainCol).isFgDefault() is True
                assert terminal.getCell(row, linkCol).isBold() != 0
                assert terminal.getCell(row, separatorCol).isBold() == 0
                assert terminal.getCell(row, plainCol).isBold() == 0

                if not hyperlinks:
                    urlRow = next((index for index, viewportLine in enumerate(viewport) if "https" in viewportLine), -1)
                    assert urlRow != -1, f"Missing fallback URL row: {viewport!r}"
                    urlLine = viewport[urlRow]
                    urlCol = urlLine.find("https")
                    urlSeparatorCol = urlLine.find("│", urlCol)
                    urlBorderCol = urlLine.rfind("│")
                    assert urlCol >= 0 and urlSeparatorCol > urlCol and urlBorderCol > urlSeparatorCol
                    assert terminal.getCell(urlRow, urlCol).isDim() != 0
                    assert terminal.getCell(urlRow, urlSeparatorCol).isDim() == 0
                    assert terminal.getCell(urlRow, urlBorderCol).isDim() == 0
            finally:
                tui.stop()
    finally:
        resetCapabilitiesCache()


async def test_should_restore_the_enclosing_style_after_a_wrapped_table_link():
    import dataclasses

    quoteColor = 0x123456
    theme: MarkdownTheme = dataclasses.replace(
        defaultMarkdownTheme,
        # Use a basic wrapper that does not automatically reopen itself after nested resets.
        quote=lambda text: f"\x1b[38;2;18;52;86m{text}\x1b[39m",
        link=lambda text: f"\x1b[38;2;129;162;190m{text}\x1b[39m",
    )
    source = """> | Link | Plain |
> | --- | --- |
> | [one two three four five six](https://example.com) | normal text |"""

    setCapabilities(TerminalCapabilities(images=None, trueColor=True, hyperlinks=True))
    terminal = VirtualTerminal(28, 10)
    tui = TuiMainScreen(terminal)
    tui.addChild(Markdown(source, 0, 0, theme))
    tui.start()

    try:
        await terminal.waitForRender()
        viewport = terminal.getViewport()
        row = next((index for index, line in enumerate(viewport) if "one" in line and "normal" in line), -1)
        assert row != -1, f"Missing wrapped blockquote table row: {viewport!r}"
        line = viewport[row]
        linkCol = line.find("one")
        separatorCol = line.find("│", linkCol)
        plainCol = line.find("normal")
        assert linkCol >= 0 and separatorCol > linkCol and plainCol > separatorCol

        assert terminal.getCell(row, linkCol).getFgColor() != quoteColor
        assert terminal.getCell(row, separatorCol).getFgColor() == quoteColor
        assert terminal.getCell(row, plainCol).getFgColor() == quoteColor

        finalRow = next((index for index, line in enumerate(viewport) if "five six" in line), -1)
        assert finalRow != -1, f"Missing final wrapped link row: {viewport!r}"
        finalLine = viewport[finalRow]
        finalLinkCol = finalLine.find("five six")
        finalSeparatorCol = finalLine.find("│", finalLinkCol)
        finalBorderCol = finalLine.rfind("│")
        assert finalLinkCol >= 0 and finalSeparatorCol > finalLinkCol and finalBorderCol > finalSeparatorCol
        assert terminal.getCell(finalRow, finalSeparatorCol).getFgColor() == quoteColor
        assert terminal.getCell(finalRow, finalBorderCol).getFgColor() == quoteColor
    finally:
        tui.stop()
        resetCapabilitiesCache()


def test_should_wrap_long_cell_content_to_multiple_lines():
    markdown = Markdown(
        """| Header |
| --- |
| This is a very long cell content that should wrap |""",
        0,
        0,
        defaultMarkdownTheme,
    )

    # Render at width that forces the cell to wrap
    lines = markdown.render(25)
    plainLines = [line.rstrip() for line in plain(lines)]

    # Should have multiple data rows due to wrapping
    dataRows = [line for line in plainLines if line.startswith("│") and "─" not in line]
    assert len(dataRows) > 2, f"Expected wrapped rows, got {len(dataRows)} rows"

    # All content should be preserved (may be split across lines)
    allText = " ".join(plainLines)
    assert "very long" in allText, "Should preserve 'very long'"
    assert "cell content" in allText, "Should preserve 'cell content'"
    assert "should wrap" in allText, "Should preserve 'should wrap'"


def test_should_wrap_long_unbroken_tokens_inside_table_cells_not_only_at_line_start():
    # Pin to no-hyperlinks so width checks work on plain text without OSC 8 sequences.
    setCapabilities(TerminalCapabilities(images=None, trueColor=False, hyperlinks=False))
    url = "https://example.com/this/is/a/very/long/url/that/should/wrap"
    markdown = Markdown(
        f"""| Value |
| --- |
| prefix {url} |""",
        0,
        0,
        defaultMarkdownTheme,
    )

    width = 30
    lines = markdown.render(width)
    resetCapabilitiesCache()
    plainLines = [line.rstrip() for line in plain(lines)]

    for line in plainLines:
        assert len(line) <= width, f'Line exceeds width {width}: "{line}" (length: {len(line)})'

    # Borders should stay intact (exactly 2 vertical borders for a 1-col table)
    tableLines = [line for line in plainLines if line.startswith("│")]
    assert len(tableLines) > 0, "Expected table rows to render"
    for line in tableLines:
        borderCount = len(line.split("│")) - 1
        assert borderCount == 2, f'Expected 2 borders, got {borderCount}: "{line}"'

    # Strip box drawing characters + whitespace so we can assert the URL is preserved
    # even if it was split across multiple wrapped lines.
    extracted = re.sub(r"[│├┤─\s]", "", "".join(plainLines))
    assert "prefix" in extracted, "Should preserve 'prefix'"
    assert url in extracted, "Should preserve URL"


def test_should_wrap_styled_inline_code_inside_table_cells_without_breaking_borders():
    markdown = Markdown(
        """| Code |
| --- |
| `averyveryveryverylongidentifier` |""",
        0,
        0,
        defaultMarkdownTheme,
    )

    width = 20
    lines = markdown.render(width)
    joinedOutput = "\n".join(lines)
    assert "\x1b[33m" in joinedOutput, "Inline code should be styled (yellow)"

    plainLines = [line.rstrip() for line in plain(lines)]
    for line in plainLines:
        assert len(line) <= width, f'Line exceeds width {width}: "{line}" (length: {len(line)})'

    tableLines = [line for line in plainLines if line.startswith("│")]
    for line in tableLines:
        borderCount = len(line.split("│")) - 1
        assert borderCount == 2, f'Expected 2 borders, got {borderCount}: "{line}"'


def test_should_handle_extremely_narrow_width_gracefully():
    markdown = Markdown(
        """| A | B | C |
| --- | --- | --- |
| 1 | 2 | 3 |""",
        0,
        0,
        defaultMarkdownTheme,
    )

    # Very narrow width
    lines = markdown.render(15)
    plainLines = [line.rstrip() for line in plain(lines)]

    # Should not crash and should produce output
    assert len(lines) > 0, "Should produce output"

    # Lines should not exceed width
    for line in plainLines:
        assert len(line) <= 15, f'Line exceeds width 15: "{line}" (length: {len(line)})'


def test_should_render_table_correctly_when_it_fits_naturally():
    markdown = Markdown(
        """| A | B |
| --- | --- |
| 1 | 2 |""",
        0,
        0,
        defaultMarkdownTheme,
    )

    # Wide width where table fits naturally
    lines = markdown.render(80)
    plainLines = [line.rstrip() for line in plain(lines)]

    # Should have proper table structure
    headerLine = next((line for line in plainLines if "A" in line and "B" in line), None)
    assert headerLine, "Should have header row"
    assert "│" in headerLine, "Header should have borders"

    separatorLine = next((line for line in plainLines if "├" in line and "┼" in line), None)
    assert separatorLine, "Should have separator row"

    dataLine = next((line for line in plainLines if "1" in line and "2" in line), None)
    assert dataLine, "Should have data row"


def test_should_respect_paddingx_when_calculating_table_width():
    markdown = Markdown(
        """| Column One | Column Two |
| --- | --- |
| Data 1 | Data 2 |""",
        2,  # paddingX = 2
        0,
        defaultMarkdownTheme,
    )

    # Width 40 with paddingX=2 means contentWidth=36
    lines = markdown.render(40)
    plainLines = [line.rstrip() for line in plain(lines)]

    # All lines should respect width
    for line in plainLines:
        assert len(line) <= 40, f'Line exceeds width 40: "{line}" (length: {len(line)})'

    # Table rows should have left padding
    tableRow = next((line for line in plainLines if "│" in line), None)
    assert tableRow is not None and tableRow.startswith("  "), "Table should have left padding"


def test_should_not_add_a_trailing_blank_line_when_table_is_the_last_rendered_block():
    markdown = Markdown(
        """| Name |
| --- |
| Alice |""",
        0,
        0,
        defaultMarkdownTheme,
    )

    lines = markdown.render(80)
    plainLines = [line.rstrip() for line in plain(lines)]

    assert plainLines[-1] != "", f"Expected table to end without a blank line: {plainLines!r}"


# --- Combined features ----------------------------------------------------------------------------


def test_should_render_lists_and_tables_together():
    markdown = Markdown(
        """# Test Document

- Item 1
  - Nested item
- Item 2

| Col1 | Col2 |
| --- | --- |
| A | B |""",
        0,
        0,
        defaultMarkdownTheme,
    )

    lines = markdown.render(80)
    plainLines = plain(lines)

    # Check heading
    assert any("Test Document" in line for line in plainLines)
    # Check list
    assert any("- Item 1" in line for line in plainLines)
    assert any("    - Nested item" in line for line in plainLines)
    # Check table
    assert any("Col1" in line for line in plainLines)
    assert any("│" in line for line in plainLines)


# --- LaTeX math -----------------------------------------------------------------------------------


def _rendered(markdown: Markdown, width: int = 80) -> list[str]:
    return [stripAnsi(line).rstrip() for line in markdown.render(width)]


def test_renders_inline_dollar_and_parenthesis_delimiters():
    markdown = Markdown(
        r"A map $\mathbb{C}^3 \to \mathbb{C}^3$, $xy$, $x-y$, $-x$, $\frac{1}{2}$, $\rightarrow$, and \(s \to \infty\).",
        0,
        0,
        defaultMarkdownTheme,
    )

    assert _rendered(markdown) == ["A map ℂ³ → ℂ³, xy, x-y, -x, 1/2, →, and s → ∞."]


def test_renders_display_dollar_delimiters_without_markdown_escape_corruption():
    markdown = Markdown(
        r"""Before

$$\{3x+2y,\; x \in \{0, \pm 1\}\}$$

after""",
        0,
        0,
        defaultMarkdownTheme,
    )

    assert _rendered(markdown) == ["Before", "", "{3x+2y, x ∈ {0, ± 1}}", "", "after"]


def test_renders_display_bracket_delimiters():
    markdown = Markdown(
        r"""Before

\[
E \approx \frac{0.1\ \text{lux}}{100\ \text{lm/W}}
\]

after""",
        0,
        0,
        defaultMarkdownTheme,
    )

    assert _rendered(markdown) == ["Before", "", "    0.1 lux", "E ≈ ────────", "    100 lm/W", "", "after"]


def test_aligns_matrix_rows_with_the_opening_delimiter():
    markdown = Markdown(
        r"""Consider the matrix

\[
A=
\begin{pmatrix}
\pi & 0\\
0 & \frac{1}{\pi}
\end{pmatrix}.
\]""",
        0,
        0,
        defaultMarkdownTheme,
    )

    assert _rendered(markdown) == ["Consider the matrix", "", "A = ⎛ π │ 0   ⎞", "    ⎝ 0 │ 1/π ⎠."]


def test_renders_lower_limits_beneath_display_operators():
    markdown = Markdown(
        r"""\[
\lim_{x\to 0}\frac{\frac{\sin x}{x}-1}{\frac{e^x-1}{x}-1}=0
\]""",
        0,
        0,
        defaultMarkdownTheme,
    )

    assert _rendered(markdown) == ["     (sin x)/x-1", "lim  ─────────── = 0", "x→0  (eˣ-1)/x-1"]


def test_renders_math_inside_lists_and_tables():
    markdown = Markdown(
        r"""- Formula: $F_1 = u^2$

| Value |
| --- |
| $\mathbb{C}^3$ |""",
        0,
        0,
        defaultMarkdownTheme,
    )

    output = "\n".join(_rendered(markdown))

    assert "- Formula: F₁ = u²" in output
    assert "│ ℂ³" in output


def test_does_not_treat_currency_shell_variables_or_code_spans_as_math():
    source = "Costs $5 and $10 or $8k–$12k; use `$x$`, $HOME, and $" + "{PATH}."
    markdown = Markdown(source, 0, 0, defaultMarkdownTheme)

    assert _rendered(markdown) == ["Costs $5 and $10 or $8k–$12k; use $x$, $HOME, and $" + "{PATH}."]

    shellVariables = "Paths: $HOME/$USER and $XDG_CONFIG_HOME/$APP_CONFIG"
    shellLines = _rendered(Markdown(shellVariables, 0, 0, defaultMarkdownTheme))
    assert shellLines == [shellVariables]


def test_preserves_unsupported_and_incomplete_latex_exactly():
    cases = [r"Unknown $x + \unknown{y}$ after", r"Streaming $\mathbb{C}^3"]

    for source in cases:
        markdown = Markdown(source, 0, 0, defaultMarkdownTheme)
        assert _rendered(markdown) == [source]


def test_preserves_incomplete_backslash_delimiters_while_streaming():
    inline = Markdown(r"Map \(\mathbb{C}^3", 0, 0, defaultMarkdownTheme)
    assert _rendered(inline) == [r"Map \(\mathbb{C}^3"]

    display = Markdown("\\[\nx^2", 0, 0, defaultMarkdownTheme)
    assert _rendered(display) == ["\\[", "x^2"]


def test_does_not_render_latex_inside_escaped_delimiters_or_code_fences():
    source = r"""Escaped \$x-y\$.

```text
$\mathbb{C}^3$
```"""
    markdown = Markdown(source, 0, 0, defaultMarkdownTheme)

    assert _rendered(markdown) == ["Escaped $x-y$.", "", "```text", "  $\\mathbb{C}^3$", "```"]


def test_allows_latex_rendering_to_be_disabled():
    markdown = Markdown(
        r"""$$
\widetilde Y_{sf}
=
(1-w_{sf})\mu_{sf}^{\mathrm{MAR}}
$$

Inline \(A_{sf}\)""",
        0,
        0,
        defaultMarkdownTheme,
        None,
        MarkdownOptions(renderLatex=False),
    )

    assert _rendered(markdown) == [
        "$$",
        r"\widetilde Y_{sf}",
        "=",
        r"(1-w_{sf})\mu_{sf}^{\mathrm{MAR}}",
        "$$",
        "",
        r"Inline \(A_{sf}\)",
    ]


def test_switches_from_raw_to_rendered_math_when_a_streamed_delimiter_closes():
    markdown = Markdown(r"Map $\mathbb{C}^3", 0, 0, defaultMarkdownTheme)
    assert _rendered(markdown) == [r"Map $\mathbb{C}^3"]

    markdown.setText(r"Map $\mathbb{C}^3$")

    assert _rendered(markdown) == ["Map ℂ³"]


# --- Backslash escapes ----------------------------------------------------------------------------


def test_should_normalize_escaped_punctuation_by_default():
    markdown = Markdown(r'"\"', 0, 0, defaultMarkdownTheme)

    assert _rendered(markdown) == ['""']


def test_should_preserve_source_backslash_escapes_when_configured():
    markdown = Markdown(r'"\"', 0, 0, defaultMarkdownTheme, None, MarkdownOptions(preserveBackslashEscapes=True))

    assert _rendered(markdown) == [r'"\"']


# --- Pre-styled text (thinking traces) ------------------------------------------------------------


def test_should_preserve_gray_italic_styling_after_inline_code():
    # This replicates how thinking content is rendered in assistant-message.ts
    markdown = Markdown(
        "This is thinking with `inline code` and more text after",
        1,
        0,
        defaultMarkdownTheme,
        DefaultTextStyle(color=lambda text: chalk.gray(text), italic=True),
    )

    lines = markdown.render(80)
    joinedOutput = "\n".join(lines)

    # Should contain the inline code block
    assert "inline code" in joinedOutput

    # The output should have ANSI codes for gray (90) and italic (3)
    assert "\x1b[90m" in joinedOutput, "Should have gray color code"
    assert "\x1b[3m" in joinedOutput, "Should have italic code"

    # Verify that inline code is styled (theme uses yellow)
    hasCodeColor = "\x1b[33m" in joinedOutput
    assert hasCodeColor, "Should style inline code"


def test_should_preserve_gray_italic_styling_after_bold_text():
    markdown = Markdown(
        "This is thinking with **bold text** and more after",
        1,
        0,
        defaultMarkdownTheme,
        DefaultTextStyle(color=lambda text: chalk.gray(text), italic=True),
    )

    lines = markdown.render(80)
    joinedOutput = "\n".join(lines)

    # Should contain bold text
    assert "bold text" in joinedOutput

    # The output should have ANSI codes for gray (90) and italic (3)
    assert "\x1b[90m" in joinedOutput, "Should have gray color code"
    assert "\x1b[3m" in joinedOutput, "Should have italic code"

    # Should have bold codes (1 or 22 for bold on/off)
    assert "\x1b[1m" in joinedOutput, "Should have bold code"


async def test_should_not_leak_styles_into_following_lines_when_rendered_in_tui():
    class MarkdownWithInput:
        def __init__(self, markdown: Markdown) -> None:
            self.markdownLineCount = 0
            self.markdown = markdown

        def render(self, width: int) -> list[str]:
            lines = self.markdown.render(width)
            self.markdownLineCount = len(lines)
            return [*lines, "INPUT"]

        def invalidate(self) -> None:
            self.markdown.invalidate()

    markdown = Markdown(
        "This is thinking with `inline code`",
        1,
        0,
        defaultMarkdownTheme,
        DefaultTextStyle(color=lambda text: chalk.gray(text), italic=True),
    )

    terminal = VirtualTerminal(80, 6)
    tui = TuiMainScreen(terminal)
    component = MarkdownWithInput(markdown)
    tui.addChild(component)
    tui.start()
    await terminal.waitForRender()

    assert component.markdownLineCount > 0
    inputRow = component.markdownLineCount
    assert terminal.getCell(inputRow, 0).isItalic() == 0
    tui.stop()


# --- Spacing after code blocks --------------------------------------------------------------------


def _trimmed(markdown: Markdown, width: int = 80) -> list[str]:
    return [stripAnsi(line).rstrip() for line in markdown.render(width)]


def _findIndex(lines: list[str], predicate: Any) -> int:
    return next((index for index, line in enumerate(lines) if predicate(line)), -1)


def test_should_have_only_one_blank_line_between_code_block_and_following_paragraph():
    markdown = Markdown(
        """hello world

```js
const hello = "world";
```

again, hello world""",
        0,
        0,
        defaultMarkdownTheme,
    )

    plainLines = _trimmed(markdown)

    closingBackticksIndex = _findIndex(plainLines, lambda line: line == "```")
    assert closingBackticksIndex != -1, "Should have closing backticks"

    afterBackticks = plainLines[closingBackticksIndex + 1 :]
    emptyLineCount = _findIndex(afterBackticks, lambda line: line != "")

    assert emptyLineCount == 1, (
        f"Expected 1 empty line after code block, but found {emptyLineCount}. "
        f"Lines after backticks: {afterBackticks[:5]!r}"
    )


def test_should_normalize_paragraph_and_code_block_spacing_to_one_blank_line():
    cases = [
        """hello this is text
```
code block
```
more text""",
        """hello this is text

```
code block
```

more text""",
    ]
    expectedLines = ["hello this is text", "", "```", "  code block", "```", "", "more text"]

    for text in cases:
        markdown = Markdown(text, 0, 0, defaultMarkdownTheme)
        plainLines = _trimmed(markdown)

        assert plainLines == expectedLines, f"Unexpected spacing for markdown: {text!r}"


def test_should_not_add_a_trailing_blank_line_when_code_block_is_the_last_rendered_block():
    cases = ["```js\nconst hello = 'world';\n```", "hello world\n\n```js\nconst hello = 'world';\n```"]

    for text in cases:
        markdown = Markdown(text, 0, 0, defaultMarkdownTheme)
        plainLines = _trimmed(markdown)

        assert plainLines[-1] != "", f"Expected code block to end without a blank line: {plainLines!r}"


# --- Spacing after dividers -----------------------------------------------------------------------


def test_should_have_only_one_blank_line_between_divider_and_following_paragraph():
    markdown = Markdown(
        """hello world

---

again, hello world""",
        0,
        0,
        defaultMarkdownTheme,
    )

    plainLines = _trimmed(markdown)

    dividerIndex = _findIndex(plainLines, lambda line: "─" in line)
    assert dividerIndex != -1, "Should have divider"

    afterDivider = plainLines[dividerIndex + 1 :]
    emptyLineCount = _findIndex(afterDivider, lambda line: line != "")

    assert emptyLineCount == 1, (
        f"Expected 1 empty line after divider, but found {emptyLineCount}. Lines after divider: {afterDivider[:5]!r}"
    )


def test_should_not_add_a_trailing_blank_line_when_divider_is_the_last_rendered_block():
    markdown = Markdown("---", 0, 0, defaultMarkdownTheme)
    plainLines = _trimmed(markdown)

    assert plainLines[-1] != "", f"Expected divider to end without a blank line: {plainLines!r}"


# --- Spacing after headings -----------------------------------------------------------------------


def test_should_have_only_one_blank_line_between_heading_and_following_paragraph():
    markdown = Markdown(
        """# Hello

This is a paragraph""",
        0,
        0,
        defaultMarkdownTheme,
    )

    plainLines = _trimmed(markdown)

    headingIndex = _findIndex(plainLines, lambda line: "Hello" in line)
    assert headingIndex != -1, "Should have heading"

    afterHeading = plainLines[headingIndex + 1 :]
    emptyLineCount = _findIndex(afterHeading, lambda line: line != "")

    assert emptyLineCount == 1, (
        f"Expected 1 empty line after heading, but found {emptyLineCount}. Lines after heading: {afterHeading[:5]!r}"
    )


def test_should_not_add_a_trailing_blank_line_when_heading_is_the_last_rendered_block():
    markdown = Markdown("# Hello", 0, 0, defaultMarkdownTheme)
    plainLines = _trimmed(markdown)

    assert plainLines[-1] != "", f"Expected heading to end without a blank line: {plainLines!r}"


# --- Spacing after blockquotes --------------------------------------------------------------------


def test_should_have_only_one_blank_line_between_blockquote_and_following_paragraph():
    markdown = Markdown(
        """hello world

> This is a quote

again, hello world""",
        0,
        0,
        defaultMarkdownTheme,
    )

    plainLines = _trimmed(markdown)

    quoteIndex = _findIndex(plainLines, lambda line: "This is a quote" in line)
    assert quoteIndex != -1, "Should have blockquote"

    afterQuote = plainLines[quoteIndex + 1 :]
    emptyLineCount = _findIndex(afterQuote, lambda line: line != "")

    assert emptyLineCount == 1, (
        f"Expected 1 empty line after blockquote, but found {emptyLineCount}. Lines after quote: {afterQuote[:5]!r}"
    )


def test_should_not_add_a_trailing_blank_line_when_blockquote_is_the_last_rendered_block():
    markdown = Markdown("> This is a quote", 0, 0, defaultMarkdownTheme)
    plainLines = _trimmed(markdown)

    assert plainLines[-1] != "", f"Expected blockquote to end without a blank line: {plainLines!r}"


# --- Blockquotes with multiline content -----------------------------------------------------------


def test_should_apply_consistent_styling_to_all_lines_in_lazy_continuation_blockquote():
    # Markdown "lazy continuation" - second line without > is still part of the quote
    markdown = Markdown(
        """>Foo
bar""",
        0,
        0,
        defaultMarkdownTheme,
        DefaultTextStyle(color=lambda text: chalk.magenta(text)),  # This should NOT be applied to blockquotes
    )

    lines = markdown.render(80)

    # Both lines should have the quote border
    plainLines = plain(lines)
    quotedLines = [line for line in plainLines if line.startswith("│ ")]
    assert len(quotedLines) == 2, f"Expected 2 quoted lines, got: {plainLines!r}"

    # Both lines should have italic (from theme.quote styling)
    fooLine = next((line for line in lines if "Foo" in line), None)
    barLine = next((line for line in lines if "bar" in line), None)
    assert fooLine, "Should have Foo line"
    assert barLine, "Should have bar line"

    # Check that both have italic (\x1b[3m) - blockquotes use theme styling, not default message color
    assert "\x1b[3m" in fooLine, f"Foo line should have italic: {fooLine}"
    assert "\x1b[3m" in barLine, f"bar line should have italic: {barLine}"

    # Blockquotes should NOT have the default message color (magenta)
    assert "\x1b[35m" not in fooLine, f"Foo line should NOT have magenta color: {fooLine}"
    assert "\x1b[35m" not in barLine, f"bar line should NOT have magenta color: {barLine}"


def test_should_apply_consistent_styling_to_explicit_multiline_blockquote():
    markdown = Markdown(
        """>Foo
>bar""",
        0,
        0,
        defaultMarkdownTheme,
        DefaultTextStyle(color=lambda text: chalk.cyan(text)),  # This should NOT be applied to blockquotes
    )

    lines = markdown.render(80)

    # Both lines should have the quote border
    plainLines = plain(lines)
    quotedLines = [line for line in plainLines if line.startswith("│ ")]
    assert len(quotedLines) == 2, f"Expected 2 quoted lines, got: {plainLines!r}"

    # Both lines should have italic (from theme.quote styling)
    fooLine = next((line for line in lines if "Foo" in line), None)
    barLine = next((line for line in lines if "bar" in line), None)
    assert fooLine is not None and "\x1b[3m" in fooLine, f"Foo line should have italic: {fooLine}"
    assert barLine is not None and "\x1b[3m" in barLine, f"bar line should have italic: {barLine}"

    # Blockquotes should NOT have the default message color (cyan)
    assert "\x1b[36m" not in fooLine, f"Foo line should NOT have cyan color: {fooLine}"
    assert "\x1b[36m" not in barLine, f"bar line should NOT have cyan color: {barLine}"


def test_should_render_list_content_inside_blockquotes():
    markdown = Markdown(
        """> 1. bla bla
> - nested bullet""",
        0,
        0,
        defaultMarkdownTheme,
    )

    lines = markdown.render(80)
    plainLines = plain(lines)
    quotedLines = [line for line in plainLines if line.startswith("│ ")]

    assert any("1. bla bla" in line for line in quotedLines), f"Missing ordered list item: {quotedLines!r}"
    assert any("- nested bullet" in line for line in quotedLines), f"Missing unordered list item: {quotedLines!r}"


def test_should_wrap_long_blockquote_lines_and_add_border_to_each_wrapped_line():
    longText = "This is a very long blockquote line that should wrap to multiple lines when rendered"
    markdown = Markdown(f"> {longText}", 0, 0, defaultMarkdownTheme)

    # Render at narrow width to force wrapping
    plainLines = _trimmed(markdown, 30)

    # Filter to non-empty lines (exclude trailing blank line after blockquote)
    contentLines = [line for line in plainLines if len(line) > 0]

    # Should have multiple lines due to wrapping
    assert len(contentLines) > 1, f"Expected multiple wrapped lines, got: {contentLines!r}"

    # Every content line should start with the quote border
    for line in contentLines:
        assert line.startswith("│ "), f'Wrapped line should have quote border: "{line}"'

    # All content should be preserved
    allText = " ".join(contentLines)
    assert "very long" in allText, "Should preserve 'very long'"
    assert "blockquote" in allText, "Should preserve 'blockquote'"
    assert "multiple" in allText, "Should preserve 'multiple'"


def test_should_properly_indent_wrapped_blockquote_lines_with_styling():
    markdown = Markdown(
        "> This is styled text that is long enough to wrap",
        0,
        0,
        defaultMarkdownTheme,
        DefaultTextStyle(
            color=lambda text: chalk.yellow(text),  # This should NOT be applied to blockquotes
            italic=True,
        ),
    )

    lines = markdown.render(25)
    plainLines = [line.rstrip() for line in plain(lines)]

    # Filter to non-empty lines
    contentLines = [line for line in plainLines if len(line) > 0]

    # All lines should have the quote border
    for line in contentLines:
        assert line.startswith("│ "), f'Line should have quote border: "{line}"'

    # Check that italic is applied (from theme.quote)
    allOutput = "\n".join(lines)
    assert "\x1b[3m" in allOutput, "Should have italic"

    # Blockquotes should NOT have the default message color (yellow)
    assert "\x1b[33m" not in allOutput, "Should NOT have yellow color from default style"


def test_should_render_inline_formatting_inside_blockquotes_and_reapply_quote_styling_after():
    markdown = Markdown("> Quote with **bold** and `code`", 0, 0, defaultMarkdownTheme)

    lines = markdown.render(80)
    plainLines = plain(lines)

    # Should have the quote border
    assert any(line.startswith("│ ") for line in plainLines), "Should have quote border"

    # Content should be preserved
    allPlain = " ".join(plainLines)
    assert "Quote with" in allPlain, "Should preserve 'Quote with'"
    assert "bold" in allPlain, "Should preserve 'bold'"
    assert "code" in allPlain, "Should preserve 'code'"

    allOutput = "\n".join(lines)

    # Should have bold styling (\x1b[1m)
    assert "\x1b[1m" in allOutput, "Should have bold styling"

    # Should have code styling (yellow = \x1b[33m from defaultMarkdownTheme)
    assert "\x1b[33m" in allOutput, "Should have code styling (yellow)"

    # Should have italic from quote styling (\x1b[3m)
    assert "\x1b[3m" in allOutput, "Should have italic from quote styling"


# --- Heading with inline code ---------------------------------------------------------------------


def test_should_preserve_heading_styling_after_inline_code():
    markdown = Markdown("### Why `sourceInfo` should not be optional", 0, 0, defaultMarkdownTheme)

    lines = markdown.render(80)
    joinedOutput = "\n".join(lines)

    # The heading theme is bold+cyan. After the yellow inline code, the heading
    # styling (bold+cyan) must be restored so subsequent text is styled correctly.
    # bold = \x1b[1m, cyan = \x1b[36m, yellow = \x1b[33m
    assert "\x1b[33m" in joinedOutput, "Should have yellow for inline code"

    # Find the position of "should not be optional" in the raw output.
    # It must be preceded by heading style codes (bold+cyan), not appear unstyled.
    afterCodeIndex = joinedOutput.find("should not be optional")
    assert afterCodeIndex > 0, "Should contain text after inline code"

    # Look at the ANSI codes between the code span end and "should not be optional".
    # There should be bold (\x1b[1m) and cyan (\x1b[36m) re-applied.
    precedingChunk = joinedOutput[max(0, afterCodeIndex - 40) : afterCodeIndex]
    assert "\x1b[1m" in precedingChunk, f"Should re-apply bold before text after code: {precedingChunk}"
    assert "\x1b[36m" in precedingChunk, f"Should re-apply cyan before text after code: {precedingChunk}"


def test_should_preserve_heading_styling_after_inline_code_for_h1():
    markdown = Markdown("# Title with `code` inside", 0, 0, defaultMarkdownTheme)

    lines = markdown.render(80)
    joinedOutput = "\n".join(lines)

    afterCodeIndex = joinedOutput.find("inside")
    assert afterCodeIndex > 0, "Should contain text after inline code"

    precedingChunk = joinedOutput[max(0, afterCodeIndex - 40) : afterCodeIndex]
    # H1 uses heading + bold + underline
    assert "\x1b[1m" in precedingChunk, f"Should re-apply bold for h1: {precedingChunk}"
    assert "\x1b[36m" in precedingChunk, f"Should re-apply cyan for h1: {precedingChunk}"
    assert "\x1b[4m" in precedingChunk, f"Should re-apply underline for h1: {precedingChunk}"


async def test_should_not_leak_h1_underline_into_padding_when_inline_code_is_the_last_token():
    markdown = Markdown("# Important distinction from `open()`", 0, 0, defaultMarkdownTheme)
    terminal = VirtualTerminal(80, 4)
    tui = TuiMainScreen(terminal)
    tui.addChild(markdown)
    tui.start()
    await terminal.waitForRender()

    renderedLine = markdown.render(80)[0]
    assert renderedLine, "Should render heading line"
    contentWidth = len(stripAnsi(renderedLine).rstrip())
    assert contentWidth > 0, "Should have visible heading content"

    for col in range(contentWidth, 80):
        assert terminal.getCell(0, col).isUnderline() == 0, f"Expected no underline in padding at col {col}"

    tui.stop()


def test_should_preserve_heading_styling_after_bold_text():
    markdown = Markdown("## Heading with **bold** and more", 0, 0, defaultMarkdownTheme)

    lines = markdown.render(80)
    joinedOutput = "\n".join(lines)

    afterBoldIndex = joinedOutput.find("and more")
    assert afterBoldIndex > 0, "Should contain text after bold"

    precedingChunk = joinedOutput[max(0, afterBoldIndex - 40) : afterBoldIndex]
    assert "\x1b[1m" in precedingChunk, f"Should re-apply bold for h2: {precedingChunk}"
    assert "\x1b[36m" in precedingChunk, f"Should re-apply cyan for h2: {precedingChunk}"


# --- Strikethrough syntax -------------------------------------------------------------------------


def test_should_render_double_tilde_text_as_strikethrough():
    markdown = Markdown("Use ~~strikethrough~~ here", 0, 0, defaultMarkdownTheme)

    lines = markdown.render(80)
    joinedOutput = "\n".join(lines)
    joinedPlain = " ".join(plain(lines))

    assert "\x1b[9m" in joinedOutput, "Should apply strikethrough styling"
    assert "strikethrough" in joinedPlain, "Should include struck text content"
    assert "~~strikethrough~~" not in joinedPlain, "Should not render delimiters as text"


def test_should_keep_single_tilde_text_as_plain_text():
    markdown = Markdown("Use ~strikethrough~ literally", 0, 0, defaultMarkdownTheme)

    lines = markdown.render(80)
    joinedOutput = "\n".join(lines)
    joinedPlain = " ".join(plain(lines))

    assert "~strikethrough~" in joinedPlain, "Single-tilde delimiters should remain visible"
    assert "\x1b[9m" not in joinedOutput, "Single-tilde text should not use strikethrough styling"


# --- Links ----------------------------------------------------------------------------------------


@pytest.fixture
def reset_capabilities():
    yield
    resetCapabilitiesCache()


_OSC8 = re.compile(r"\x1b\]8;;[^\x1b]*\x1b\\")


def test_should_not_duplicate_url_for_autolinked_emails(reset_capabilities):
    # Hyperlinks capability does not affect the mailto: display check.
    setCapabilities(TerminalCapabilities(images=None, trueColor=False, hyperlinks=False))
    markdown = Markdown("Contact user@example.com for help", 0, 0, defaultMarkdownTheme)

    lines = markdown.render(80)
    joinedPlain = " ".join(plain(lines))

    # Should contain the email once, not duplicated with mailto:
    assert "user@example.com" in joinedPlain, "Should contain email"
    assert "mailto:" not in joinedPlain, "Should not show mailto: prefix for autolinked emails"


def test_should_not_duplicate_url_for_bare_urls(reset_capabilities):
    setCapabilities(TerminalCapabilities(images=None, trueColor=False, hyperlinks=False))
    markdown = Markdown("Visit https://example.com for more", 0, 0, defaultMarkdownTheme)

    lines = markdown.render(80)
    joinedPlain = " ".join(plain(lines))

    # URL should appear only once
    urlCount = len(re.findall(r"https://example\.com", joinedPlain))
    assert urlCount == 1, "URL should appear exactly once"


def test_should_show_url_in_parentheses_when_hyperlinks_are_not_supported(reset_capabilities):
    setCapabilities(TerminalCapabilities(images=None, trueColor=False, hyperlinks=False))
    markdown = Markdown("[click here](https://example.com)", 0, 0, defaultMarkdownTheme)

    lines = markdown.render(80)
    joinedPlain = " ".join(plain(lines))

    assert "click here" in joinedPlain, "Should contain link text"
    assert "(https://example.com)" in joinedPlain, "Should show URL in parentheses"


def test_should_show_mailto_url_in_parentheses_when_hyperlinks_are_not_supported(reset_capabilities):
    setCapabilities(TerminalCapabilities(images=None, trueColor=False, hyperlinks=False))
    markdown = Markdown("[Email me](mailto:test@example.com)", 0, 0, defaultMarkdownTheme)

    lines = markdown.render(80)
    joinedPlain = " ".join(plain(lines))

    assert "Email me" in joinedPlain, "Should contain link text"
    assert "(mailto:test@example.com)" in joinedPlain, "Should show mailto URL in parentheses"


def test_should_emit_osc_8_hyperlink_sequence_when_terminal_supports_hyperlinks(reset_capabilities):
    setCapabilities(TerminalCapabilities(images=None, trueColor=False, hyperlinks=True))
    markdown = Markdown("[click here](https://example.com)", 0, 0, defaultMarkdownTheme)

    lines = markdown.render(80)
    joined = "".join(lines)

    # OSC 8 open: ESC ] 8 ; ; <url> ESC \
    assert "\x1b]8;;https://example.com\x1b\\" in joined, "Should contain OSC 8 open sequence"
    # OSC 8 close: ESC ] 8 ; ; ESC \
    assert "\x1b]8;;\x1b\\" in joined, "Should contain OSC 8 close sequence"
    # Visible text is present
    plainLines = [re.sub(r"\x1b[^a-zA-Z]*[a-zA-Z]|\x1b\].*?\x1b\\", "", line) for line in lines]
    assert "click here" in "".join(plainLines), "Should contain link text"
    # URL is NOT printed inline as plain text
    rawPlain = [stripAnsi(_OSC8.sub("", line)) for line in lines]
    assert "(https://example.com)" not in "".join(rawPlain), "URL should not appear inline in parentheses"


def test_should_use_osc_8_for_mailto_links_when_terminal_supports_hyperlinks(reset_capabilities):
    setCapabilities(TerminalCapabilities(images=None, trueColor=False, hyperlinks=True))
    markdown = Markdown("[Email me](mailto:test@example.com)", 0, 0, defaultMarkdownTheme)

    lines = markdown.render(80)
    joined = "".join(lines)

    assert "\x1b]8;;mailto:test@example.com\x1b\\" in joined, "Should contain OSC 8 open with mailto URL"
    assert "\x1b]8;;\x1b\\" in joined, "Should contain OSC 8 close sequence"


def test_should_use_osc_8_for_bare_urls_when_terminal_supports_hyperlinks(reset_capabilities):
    setCapabilities(TerminalCapabilities(images=None, trueColor=False, hyperlinks=True))
    markdown = Markdown("Visit https://example.com for more", 0, 0, defaultMarkdownTheme)

    lines = markdown.render(80)
    joined = "".join(lines)

    assert "\x1b]8;;https://example.com\x1b\\" in joined, "Should contain OSC 8 hyperlink"
    # URL should not also appear as raw parenthetical text
    rawPlain = [stripAnsi(_OSC8.sub("", line)) for line in lines]
    assert "(https://example.com)" not in "".join(rawPlain), "URL should not appear twice"


# --- HTML-like tags in text -----------------------------------------------------------------------


def test_should_render_content_with_html_like_tags_as_text():
    # When the model emits something like <thinking>content</thinking> in regular text,
    # marked might treat it as HTML and hide the content
    markdown = Markdown(
        "This is text with <thinking>hidden content</thinking> that should be visible",
        0,
        0,
        defaultMarkdownTheme,
    )

    lines = markdown.render(80)
    joinedPlain = " ".join(plain(lines))

    # The content inside the tags should be visible
    assert "hidden content" in joinedPlain or "<thinking>" in joinedPlain, (
        "Should render HTML-like tags or their content as text, not hide them"
    )


def test_should_render_html_tags_in_code_blocks_correctly():
    markdown = Markdown("```html\n<div>Some HTML</div>\n```", 0, 0, defaultMarkdownTheme)

    lines = markdown.render(80)
    joinedPlain = "\n".join(plain(lines))

    # HTML in code blocks should be visible
    assert "<div>" in joinedPlain and "</div>" in joinedPlain, "Should render HTML in code blocks"


# --- Streaming code fences ------------------------------------------------------------------------


def test_stabilizes_partial_closing_fence_rendering():
    cases: list[tuple[str, list[str]]] = [
        ("```ts\nconst x = 1;\n``", ["```ts", "  const x = 1;", "```"]),
        ("```md\nnot a closing fence:\n``\n```", ["```md", "  not a closing fence:", "  ``", "```"]),
        ("```ts\n``", ["```ts", "", "```"]),
        ("````\n```", ["```", "", "```"]),
        ("~~~~~\n~~~~", ["```", "", "```"]),
        (
            "```md\nnot a closing fence:\n``\n```\n\nafter",
            ["```md", "  not a closing fence:", "  ``", "```", "", "after"],
        ),
    ]

    for input, expected in cases:
        markdown = Markdown(input, 0, 0, defaultMarkdownTheme)
        lines = _trimmed(markdown)

        assert lines == expected

    partial = Markdown("```ts\nconst x = 1;\n``", 0, 0, defaultMarkdownTheme)
    complete = Markdown("```ts\nconst x = 1;\n```", 0, 0, defaultMarkdownTheme)

    assert len(partial.render(80)) == len(complete.render(80))

"""Translated from pi-tui's ``test/select-list.test.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ui.tui.components.select_list import (
    SelectItem,
    SelectList,
    SelectListLayoutOptions,
    SelectListTheme,
    SelectListTruncatePrimaryContext,
)
from misaka.ui.tui.utils import visibleWidth

testTheme = SelectListTheme(
    selectedPrefix=lambda text: text,
    selectedText=lambda text: text,
    description=lambda text: text,
    scrollInfo=lambda text: text,
    noMatch=lambda text: text,
)


def visibleIndexOf(line: str, text: str) -> int:
    index = line.find(text)
    assert index != -1
    return visibleWidth(line[:index])


def test_normalizes_multiline_descriptions_to_single_line():
    items = [
        SelectItem(
            value="test",
            label="test",
            description="Line one\nLine two\nLine three",
        ),
    ]

    selectList = SelectList(items, 5, testTheme)
    rendered = selectList.render(100)

    assert len(rendered) > 0
    assert "\n" not in rendered[0]
    assert "Line one Line two Line three" in rendered[0]


def test_keeps_descriptions_aligned_when_the_primary_text_is_truncated():
    items = [
        SelectItem(value="short", label="short", description="short description"),
        SelectItem(
            value="very-long-command-name-that-needs-truncation",
            label="very-long-command-name-that-needs-truncation",
            description="long description",
        ),
    ]

    selectList = SelectList(items, 5, testTheme)
    rendered = selectList.render(80)

    assert visibleIndexOf(rendered[0], "short description") == visibleIndexOf(rendered[1], "long description")


def test_uses_the_configured_minimum_primary_column_width():
    items = [
        SelectItem(value="a", label="a", description="first"),
        SelectItem(value="bb", label="bb", description="second"),
    ]

    selectList = SelectList(
        items,
        5,
        testTheme,
        SelectListLayoutOptions(
            minPrimaryColumnWidth=12,
            maxPrimaryColumnWidth=20,
        ),
    )
    rendered = selectList.render(80)

    assert rendered[0].find("first") == 14
    assert rendered[1].find("second") == 14


def test_uses_the_configured_maximum_primary_column_width():
    items = [
        SelectItem(
            value="very-long-command-name-that-needs-truncation",
            label="very-long-command-name-that-needs-truncation",
            description="first",
        ),
        SelectItem(value="short", label="short", description="second"),
    ]

    selectList = SelectList(
        items,
        5,
        testTheme,
        SelectListLayoutOptions(
            minPrimaryColumnWidth=12,
            maxPrimaryColumnWidth=20,
        ),
    )
    rendered = selectList.render(80)

    assert visibleIndexOf(rendered[0], "first") == 22
    assert visibleIndexOf(rendered[1], "second") == 22


def test_allows_overriding_primary_truncation_while_preserving_description_alignment():
    items = [
        SelectItem(
            value="very-long-command-name-that-needs-truncation",
            label="very-long-command-name-that-needs-truncation",
            description="first",
        ),
        SelectItem(value="short", label="short", description="second"),
    ]

    def truncatePrimary(context: SelectListTruncatePrimaryContext) -> str:
        text, maxWidth = context.text, context.maxWidth
        if len(text) <= maxWidth:
            return text

        return f"{text[: max(0, maxWidth - 1)]}…"

    selectList = SelectList(
        items,
        5,
        testTheme,
        SelectListLayoutOptions(
            minPrimaryColumnWidth=12,
            maxPrimaryColumnWidth=12,
            truncatePrimary=truncatePrimary,
        ),
    )
    rendered = selectList.render(80)

    assert "…" in rendered[0]
    assert visibleIndexOf(rendered[0], "first") == visibleIndexOf(rendered[1], "second")

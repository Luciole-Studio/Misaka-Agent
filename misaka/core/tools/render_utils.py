"""Rendering helpers shared by coding-agent tools."""

from __future__ import annotations

import builtins
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, TypeVar

from misaka.ui.tui.terminal_image import (
    getCapabilities,
    getImageDimensions,
    hyperlink,
    imageFallback,
)
from misaka.utils.ansi import strip_ansi
from misaka.utils.paths import resolve_path
from misaka.utils.shell import sanitize_binary_output

TDetails = TypeVar("TDetails")


class ToolRenderResultLike(Protocol[TDetails]):
    content: list[Any]
    details: TDetails


def shorten_path(path: object) -> builtins.str:
    if not isinstance(path, builtins.str):
        return ""
    home = os.path.expanduser("~")
    if path.startswith(home):
        return f"~{path[len(home) :]}"
    return path


def link_path(
    styled_text: builtins.str,
    raw_path: builtins.str,
    cwd: builtins.str,
) -> builtins.str:
    if not getCapabilities().hyperlinks:
        return styled_text
    absolute_path = resolve_path(raw_path, cwd)
    return hyperlink(styled_text, Path(absolute_path).as_uri())


def render_tool_path(
    raw_path: builtins.str | None,
    theme: object,
    cwd: builtins.str,
    options: Mapping[builtins.str, builtins.str] | None = None,
) -> builtins.str:
    if raw_path is None:
        return invalid_arg_text(theme)
    value = raw_path or (options.get("emptyFallback") if options is not None else None)
    if not value:
        return theme.fg("toolOutput", "...")
    return link_path(theme.fg("accent", shorten_path(value)), value, cwd)


def str_value(value: object) -> builtins.str | None:
    if isinstance(value, builtins.str):
        return value
    if value is None:
        return ""
    return None


def replace_tabs(text: builtins.str) -> builtins.str:
    return text.replace("\t", "   ")


def normalize_display_text(text: builtins.str) -> builtins.str:
    return text.replace("\r", "")


def get_text_output(result: object | None, show_images: bool) -> builtins.str:
    if result is None:
        return ""

    content = get_attr(result, "content")
    if not isinstance(content, list):
        return ""

    text_blocks = [block for block in content if get_attr(block, "type") == "text"]
    image_blocks = [block for block in content if get_attr(block, "type") == "image"]

    output = "\n".join(
        sanitize_binary_output(strip_ansi(get_attr(block, "text") or "")).replace(
            "\r", ""
        )
        for block in text_blocks
    )

    caps = getCapabilities()
    if image_blocks and ((not getattr(caps, "images", None)) or not show_images):
        image_indicators = "\n".join(
            render_image_indicator(block) for block in image_blocks
        )
        output = f"{output}\n{image_indicators}" if output else image_indicators

    return output


def render_image_indicator(block: object) -> builtins.str:
    mime_type = get_attr(block, "mimeType") or "image/unknown"
    data = get_attr(block, "data")
    dims = (
        getImageDimensions(data, mime_type)
        if isinstance(data, builtins.str) and isinstance(mime_type, builtins.str)
        else None
    )
    return imageFallback(mime_type, dims)


def get_attr(value: object, name: builtins.str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def invalid_arg_text(theme: object) -> builtins.str:
    return theme.fg("error", "[invalid arg]")


shortenPath = shorten_path
getTextOutput = get_text_output
invalidArgText = invalid_arg_text
linkPath = link_path
normalizeDisplayText = normalize_display_text
renderToolPath = render_tool_path
replaceTabs = replace_tabs
str = str_value
__all__ = [
    "ToolRenderResultLike",
    "getTextOutput",
    "invalidArgText",
    "linkPath",
    "normalizeDisplayText",
    "renderToolPath",
    "replaceTabs",
    "shortenPath",
    "str",
]

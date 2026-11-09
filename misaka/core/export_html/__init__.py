"""HTML export helpers for coding-agent sessions."""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypedDict, cast

from misaka.ai.utils.typebox_helpers import typeboxSchema
from misaka.config import APP_NAME, get_export_template_dir
from misaka.core.export_html.tool_renderer import (
    ToolHtmlRenderer,
)
from misaka.core.session_manager import SessionManager
from misaka.modes.interactive.theme.theme import (
    getResolvedThemeColors,
    getThemeExportColors,
)
from misaka.modes.json_event import to_jsonable
from misaka.utils.paths import normalizePath, resolvePath

TEMPLATE_RENDERED_TOOLS = {"bash", "read", "write", "edit", "ls"}


class ToolDefinitionLike(Protocol):
    name: str
    description: str
    parameters: Any


class AgentStateLike(Protocol):
    systemPrompt: str | None
    tools: list[ToolDefinitionLike] | None


class ExportOptions(TypedDict, total=False):
    outputPath: str
    themeName: str
    toolRenderer: ToolHtmlRenderer


class RenderedToolHtml(TypedDict, total=False):
    callHtml: str
    resultHtmlCollapsed: str
    resultHtmlExpanded: str


class SessionData(TypedDict, total=False):
    header: dict[str, Any] | None
    entries: list[dict[str, Any]]
    leafId: str | None
    systemPrompt: str | None
    tools: list[dict[str, Any]] | None
    renderedTools: dict[str, RenderedToolHtml]


@dataclass(slots=True)
class _RgbColor:
    r: int
    g: int
    b: int


def parseColor(color: str) -> _RgbColor | None:
    hex_match = re.fullmatch(r"#([0-9a-fA-F]{6})", color)
    if hex_match:
        return _RgbColor(
            r=int(hex_match.group(1)[0:2], 16),
            g=int(hex_match.group(1)[2:4], 16),
            b=int(hex_match.group(1)[4:6], 16),
        )

    rgb_match = re.fullmatch(r"rgb\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", color)
    if rgb_match:
        return _RgbColor(r=int(rgb_match.group(1)), g=int(rgb_match.group(2)), b=int(rgb_match.group(3)))
    return None


def getLuminance(red: int, green: int, blue: int) -> float:
    def to_linear(component: int) -> float:
        scaled = component / 255.0
        return scaled / 12.92 if scaled <= 0.03928 else ((scaled + 0.055) / 1.055) ** 2.4

    return 0.2126 * to_linear(red) + 0.7152 * to_linear(green) + 0.0722 * to_linear(blue)


def adjustBrightness(color: str, factor: float) -> str:
    parsed = parseColor(color)
    if parsed is None:
        return color

    def adjust(component: int) -> int:
        return max(0, min(255, round(component * factor)))

    return f"rgb({adjust(parsed.r)}, {adjust(parsed.g)}, {adjust(parsed.b)})"


def _nullish(value: str | None, fallback: str) -> str:
    return fallback if value is None else value


def deriveExportColors(base_color: str) -> dict[str, str]:
    parsed = parseColor(base_color)
    if parsed is None:
        return {
            "pageBg": "rgb(24, 24, 30)",
            "cardBg": "rgb(30, 30, 36)",
            "infoBg": "rgb(60, 55, 40)",
        }

    luminance = getLuminance(parsed.r, parsed.g, parsed.b)
    if luminance > 0.5:
        return {
            "pageBg": adjustBrightness(base_color, 0.96),
            "cardBg": base_color,
            "infoBg": f"rgb({min(255, parsed.r + 10)}, {min(255, parsed.g + 5)}, {max(0, parsed.b - 20)})",
        }
    return {
        "pageBg": adjustBrightness(base_color, 0.7),
        "cardBg": adjustBrightness(base_color, 0.85),
        "infoBg": f"rgb({min(255, parsed.r + 20)}, {min(255, parsed.g + 15)}, {parsed.b})",
    }


# MISAKA fork: theme values are whitelisted before they reach the CSS. A theme is a data file: it comes from
# `get_custom_themes_dir()`, and themes get
# copied off the internet and passed between people. Its keys and values land inside
# the export's `<head><style>`, so an unvalidated value can close the element
# (`red;} </style><script>...`) and run script in the browser of everyone the export
# is handed to -- with the whole transcript in reach, since the page decodes and
# renders it itself. The session-content path is already fenced (base64 + escapeHtml +
# marked's html/tag tokenizers disabled); this is the one hole left.
# The schema (misaka/schemas/theme.schema.json) admits any string -- hex such as `#RRGGBB`, a variable
# name, "" -- or a 0-255 palette index, so it keeps nothing out of the CSS; `adjustBrightness` emits
# `rgb(r, g, b)`. The whitelist below admits every color a legitimate theme produces; anything outside
# it falls back rather than reaching the CSS.
_SAFE_CSS_IDENT = re.compile(r"[A-Za-z0-9_-]+")
_SAFE_CSS_COLOR = re.compile(
    r"#[0-9A-Fa-f]{3,8}"  # hex, the only literal the schema documents
    r"|[A-Za-z]+"  # named colours, `transparent`, `currentColor`
    r"|(?:rgb|rgba|hsl|hsla)\(\s*[0-9.,%\s/-]*\)"  # what adjustBrightness emits
)
_FALLBACK_CSS_COLOR = "inherit"


def _safe_css_color(value: str, fallback: str = _FALLBACK_CSS_COLOR) -> str:
    return value if _SAFE_CSS_COLOR.fullmatch(value) else fallback


def generateThemeVars(theme_name: str | None = None) -> str:
    colors = getResolvedThemeColors(theme_name)
    lines = [
        f"--{key}: {_safe_css_color(value)};"
        for key, value in colors.items()
        if _SAFE_CSS_IDENT.fullmatch(key)
    ]
    theme_export = getThemeExportColors(theme_name)
    derived = deriveExportColors(colors.get("userMessageBg") or "#343541")
    lines.append(f"--exportPageBg: {_safe_css_color(_nullish(theme_export.get('pageBg'), derived['pageBg']))};")
    lines.append(f"--exportCardBg: {_safe_css_color(_nullish(theme_export.get('cardBg'), derived['cardBg']))};")
    lines.append(f"--exportInfoBg: {_safe_css_color(_nullish(theme_export.get('infoBg'), derived['infoBg']))};")
    return "\n      ".join(lines)


def templateDir() -> Path:
    return Path(get_export_template_dir())


def _read_template(relative_path: str) -> str:
    return (templateDir() / relative_path).read_text(encoding="utf-8")


def generateHtml(session_data: SessionData, theme_name: str | None = None) -> str:
    template = _read_template("template.html")
    template_css = _read_template("template.css")
    template_js = _read_template("template.js")
    marked_js = _read_template("vendor/marked.min.js")
    highlight_js = _read_template("vendor/highlight.min.js")

    theme_vars = generateThemeVars(theme_name)
    colors = getResolvedThemeColors(theme_name)
    theme_export = getThemeExportColors(theme_name)
    derived = deriveExportColors(colors.get("userMessageBg") or "#343541")
    # Same substitution hazard as generateThemeVars: these three land in template.css's
    # `--body-bg`/`--container-bg`/`--info-bg` declarations, inside the same `<style>`.
    body_bg = _safe_css_color(_nullish(theme_export.get("pageBg"), derived["pageBg"]))
    container_bg = _safe_css_color(_nullish(theme_export.get("cardBg"), derived["cardBg"]))
    info_bg = _safe_css_color(_nullish(theme_export.get("infoBg"), derived["infoBg"]))

    # Entries may carry dataclass/pydantic values (a `!cmd` run puts a BashExecutionMessage
    # into fileEntries); serialize through the same wire conversion the JSONL mode uses.
    encoded_session_data = base64.b64encode(
        json.dumps(to_jsonable(session_data), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")

    css = (
        template_css.replace("{{THEME_VARS}}", theme_vars)
        .replace("{{BODY_BG}}", body_bg)
        .replace("{{CONTAINER_BG}}", container_bg)
        .replace("{{INFO_BG}}", info_bg)
    )

    return (
        template.replace("{{CSS}}", css)
        .replace("{{JS}}", template_js)
        .replace("{{SESSION_DATA}}", encoded_session_data)
        .replace("{{MARKED_JS}}", marked_js)
        .replace("{{HIGHLIGHT_JS}}", highlight_js)
    )


def _normalize_options(options: ExportOptions | str | None) -> ExportOptions:
    if isinstance(options, str):
        return {"outputPath": options}
    return dict(options or {})


def _get_value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _get_message_role(message: Any) -> str | None:
    role = _get_value(message, "role")
    return role if isinstance(role, str) else None


def _get_content_blocks(message: Any) -> list[Any]:
    content = _get_value(message, "content", [])
    return list(content) if isinstance(content, list) else []


def _get_block_type(block: Any) -> str | None:
    block_type = _get_value(block, "type")
    return block_type if isinstance(block_type, str) else None


def _get_render_result_payload(message: Any) -> list[dict[str, Any]]:
    content = _get_value(message, "content", [])
    if isinstance(content, list):
        return [cast(dict[str, Any], item) if isinstance(item, dict) else dict(item) for item in content]
    return []


def preRenderCustomTools(
    entries: list[dict[str, Any]],
    tool_renderer: ToolHtmlRenderer,
) -> dict[str, RenderedToolHtml]:
    rendered_tools: dict[str, RenderedToolHtml] = {}

    for entry in entries:
        if entry.get("type") != "message":
            continue
        message = entry.get("message")
        role = _get_message_role(message)

        if role == "assistant":
            for block in _get_content_blocks(message):
                if _get_block_type(block) != "toolCall":
                    continue
                tool_name = _get_value(block, "name", "")
                tool_call_id = _get_value(block, "id", "")
                if not isinstance(tool_name, str) or not isinstance(tool_call_id, str):
                    continue
                if tool_name in TEMPLATE_RENDERED_TOOLS:
                    continue
                call_html = tool_renderer.renderCall(tool_call_id, tool_name, _get_value(block, "arguments"))
                if call_html:
                    rendered_tools[tool_call_id] = {"callHtml": call_html}

        if role == "toolResult":
            tool_call_id = _get_value(message, "toolCallId")
            tool_name = _get_value(message, "toolName", "")
            if not isinstance(tool_call_id, str) or not isinstance(tool_name, str):
                continue
            existing = rendered_tools.get(tool_call_id)
            if existing is None and tool_name in TEMPLATE_RENDERED_TOOLS:
                continue
            rendered = tool_renderer.renderResult(
                tool_call_id,
                tool_name,
                _get_render_result_payload(message),
                _get_value(message, "details"),
                bool(_get_value(message, "isError", False)),
            )
            if rendered:
                merged: RenderedToolHtml = dict(existing or {})
                if rendered.collapsed is not None:
                    merged["resultHtmlCollapsed"] = rendered.collapsed
                if rendered.expanded is not None:
                    merged["resultHtmlExpanded"] = rendered.expanded
                rendered_tools[tool_call_id] = merged

    return rendered_tools


def _serialize_tool(tool: Any) -> dict[str, Any]:
    parameters = _get_value(tool, "parameters")
    return {
        "name": _get_value(tool, "name"),
        "description": _get_value(tool, "description"),
        # MISAKA fork: parameters may be a pydantic model class, which the export writes as its JSON schema.
        "parameters": (
            typeboxSchema(parameters)
            if isinstance(parameters, type) and hasattr(parameters, "model_json_schema")
            else parameters
        ),
    }


async def exportSessionToHtml(
    session_manager: SessionManager,
    state: AgentStateLike | dict[str, Any] | None = None,
    options: ExportOptions | str | None = None,
) -> str:
    resolved_options = _normalize_options(options)
    session_file = session_manager.getSessionFile()
    if not session_file:
        raise RuntimeError("Cannot export in-memory session to HTML")
    if not os.path.exists(session_file):
        raise RuntimeError("Nothing to export yet - start a conversation first")

    entries = session_manager.getEntries()
    rendered_tools: dict[str, RenderedToolHtml] | None = None
    tool_renderer = resolved_options.get("toolRenderer")
    if tool_renderer is not None:
        prerendered = preRenderCustomTools(entries, tool_renderer)
        if prerendered:
            rendered_tools = prerendered

    state_tools = _get_value(state, "tools") if state is not None else None
    session_data: SessionData = {
        "header": session_manager.getHeader(),
        "entries": entries,
        "leafId": session_manager.getLeafId(),
    }
    system_prompt = _get_value(state, "systemPrompt") if state is not None else None
    if system_prompt is not None:
        session_data["systemPrompt"] = system_prompt
    if isinstance(state_tools, list):
        session_data["tools"] = [_serialize_tool(tool) for tool in state_tools]
    if rendered_tools:
        session_data["renderedTools"] = rendered_tools

    html = generateHtml(session_data, resolved_options.get("themeName"))
    output_path = normalizePath(resolved_options["outputPath"]) if resolved_options.get("outputPath") else None
    if not output_path:
        output_path = f"{APP_NAME}-session-{Path(session_file).stem}.html"
    Path(output_path).write_text(html, encoding="utf-8")
    return output_path


async def exportFromFile(
    input_path: str,
    options: ExportOptions | str | None = None,
) -> str:
    resolved_options = _normalize_options(options)
    resolved_input_path = resolvePath(input_path)
    if not os.path.exists(resolved_input_path):
        raise RuntimeError(f"File not found: {resolved_input_path}")

    session_manager = SessionManager.open(resolved_input_path)
    session_data: SessionData = {
        "header": session_manager.getHeader(),
        "entries": session_manager.getEntries(),
        "leafId": session_manager.getLeafId(),
    }
    html = generateHtml(session_data, resolved_options.get("themeName"))

    output_path = normalizePath(resolved_options["outputPath"]) if resolved_options.get("outputPath") else None
    if not output_path:
        output_path = f"{APP_NAME}-session-{Path(resolved_input_path).stem}.html"
    Path(output_path).write_text(html, encoding="utf-8")
    return output_path


__all__ = [
    "ExportOptions",
    "ToolHtmlRenderer",
    "exportFromFile",
    "exportSessionToHtml",
]

"""Translated from pi's ``modes/interactive/theme/theme-tokens.ts`` (v1.3.0).

MISAKA forks:

- ``THEME_TOKENS`` also has MISAKA's ``sectionTitle``, ``appTitle`` and ``appTitleOnLight`` tokens, optional with
  fallbacks, so themes without them still load and the strict schema accepts the bundled themes.
- Python cannot derive types from the table: ``ThemeToken``, ``ThemeColor`` and ``ThemeBg`` are spelled out
  (tests/test_theme_tokens.py keeps them equal to the table), and ``ThemeColorValues``/``ResolvedThemeColorValues`` are
  mappings by token name.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, NotRequired, TypedDict


class ThemeTokenDescriptor(TypedDict):
    slot: Literal["foreground", "background"]
    description: str
    fallback: NotRequired[str]


type ThemeTokenDescriptors = Mapping[str, ThemeTokenDescriptor]


def defineThemeTokens[T: ThemeTokenDescriptors](tokens: T) -> T:
    for name, descriptor in tokens.items():
        if "fallback" not in descriptor:
            continue
        fallback = tokens.get(descriptor["fallback"])
        if fallback is None:
            raise ValueError(f"Theme token {name} has unknown fallback {descriptor['fallback']}")
        if fallback["slot"] != descriptor["slot"]:
            raise ValueError(f"Theme token {name} has fallback {descriptor['fallback']} in a different color slot")
    return tokens


# Theme token metadata in the same order used by theme files and the published JSON Schema.
THEME_TOKENS: dict[str, ThemeTokenDescriptor] = defineThemeTokens({
    "accent": {"slot": "foreground", "description": "Primary accent color (logo, selected items, cursor)"},
    "border": {"slot": "foreground", "description": "Normal borders"},
    "borderAccent": {"slot": "foreground", "description": "Highlighted borders"},
    "borderMuted": {"slot": "foreground", "description": "Subtle borders"},
    "success": {"slot": "foreground", "description": "Success states"},
    "error": {"slot": "foreground", "description": "Error states"},
    "warning": {"slot": "foreground", "description": "Warning states"},
    "muted": {"slot": "foreground", "description": "Secondary/dimmed text"},
    "dim": {"slot": "foreground", "description": "Very dimmed text (more subtle than muted)"},
    "text": {"slot": "foreground", "description": "Default text color (usually empty string)"},
    "thinkingText": {"slot": "foreground", "description": "Thinking block text color"},
    "scrollbarTrack": {
        "slot": "foreground",
        "description": "Fullscreen scrollbar track foreground (falls back to muted when omitted)",
        "fallback": "muted",
    },
    "scrollbarThumb": {
        "slot": "foreground",
        "description": "Fullscreen scrollbar thumb foreground (falls back to text when omitted)",
        "fallback": "text",
    },
    "selectedBg": {"slot": "background", "description": "Selected item background"},
    "searchMatchBg": {
        "slot": "background",
        "description": "Transcript search match background and current-match text (falls back to selectedBg when omitted)",
        "fallback": "selectedBg",
    },
    "searchMatchText": {
        "slot": "foreground",
        "description": "Transcript search match text and current-match background (falls back to text when omitted)",
        "fallback": "text",
    },
    "userMessageBg": {"slot": "background", "description": "User message background"},
    "userMessageText": {"slot": "foreground", "description": "User message text color"},
    "customMessageBg": {"slot": "background", "description": "Custom message background (hook-injected messages)"},
    "customMessageText": {"slot": "foreground", "description": "Custom message text color"},
    "customMessageLabel": {"slot": "foreground", "description": "Custom message type label color"},
    "toolPendingBg": {"slot": "background", "description": "Tool execution box (pending state)"},
    "toolSuccessBg": {"slot": "background", "description": "Tool execution box (success state)"},
    "toolErrorBg": {"slot": "background", "description": "Tool execution box (error state)"},
    "toolTitle": {"slot": "foreground", "description": "Tool execution box title color"},
    "toolOutput": {"slot": "foreground", "description": "Tool execution box output text color"},
    "mdHeading": {"slot": "foreground", "description": "Markdown heading text"},
    "mdLink": {"slot": "foreground", "description": "Markdown link text"},
    "mdLinkUrl": {"slot": "foreground", "description": "Markdown link URL"},
    "mdCode": {"slot": "foreground", "description": "Markdown inline code"},
    "mdCodeBlock": {"slot": "foreground", "description": "Markdown code block content"},
    "mdCodeBlockBorder": {"slot": "foreground", "description": "Markdown code block fences"},
    "mdQuote": {"slot": "foreground", "description": "Markdown blockquote text"},
    "mdQuoteBorder": {"slot": "foreground", "description": "Markdown blockquote border"},
    "mdHr": {"slot": "foreground", "description": "Markdown horizontal rule"},
    "mdListBullet": {"slot": "foreground", "description": "Markdown list bullets/numbers"},
    "toolDiffAdded": {"slot": "foreground", "description": "Added lines in tool diffs"},
    "toolDiffRemoved": {"slot": "foreground", "description": "Removed lines in tool diffs"},
    "toolDiffContext": {"slot": "foreground", "description": "Context lines in tool diffs"},
    "syntaxComment": {"slot": "foreground", "description": "Syntax highlighting: comments"},
    "syntaxKeyword": {"slot": "foreground", "description": "Syntax highlighting: keywords"},
    "syntaxFunction": {"slot": "foreground", "description": "Syntax highlighting: function names"},
    "syntaxVariable": {"slot": "foreground", "description": "Syntax highlighting: variable names"},
    "syntaxString": {"slot": "foreground", "description": "Syntax highlighting: string literals"},
    "syntaxNumber": {"slot": "foreground", "description": "Syntax highlighting: number literals"},
    "syntaxType": {"slot": "foreground", "description": "Syntax highlighting: type names"},
    "syntaxOperator": {"slot": "foreground", "description": "Syntax highlighting: operators"},
    "syntaxPunctuation": {"slot": "foreground", "description": "Syntax highlighting: punctuation"},
    "thinkingOff": {"slot": "foreground", "description": "Thinking level border: off"},
    "thinkingMinimal": {"slot": "foreground", "description": "Thinking level border: minimal"},
    "thinkingLow": {"slot": "foreground", "description": "Thinking level border: low"},
    "thinkingMedium": {"slot": "foreground", "description": "Thinking level border: medium"},
    "thinkingHigh": {"slot": "foreground", "description": "Thinking level border: high"},
    "thinkingXhigh": {"slot": "foreground", "description": "Thinking level border: xhigh"},
    "thinkingMax": {
        "slot": "foreground",
        "description": "Thinking level border: max (falls back to thinkingXhigh when omitted)",
        "fallback": "thinkingXhigh",
    },
    "bashMode": {"slot": "foreground", "description": "Editor border color in bash mode"},
    # MISAKA fork: startup section titles and the app title, on dark and light terminals.
    "sectionTitle": {
        "slot": "foreground",
        "description": "Startup-screen section titles (falls back to accent when omitted)",
        "fallback": "accent",
    },
    "appTitle": {
        "slot": "foreground",
        "description": "App title on a dark terminal background (falls back to text when omitted)",
        "fallback": "text",
    },
    "appTitleOnLight": {
        "slot": "foreground",
        "description": "App title on a light terminal background (falls back to text when omitted)",
        "fallback": "text",
    },
})

type ThemeToken = ThemeColor | ThemeBg

type ThemeColor = Literal[
    "accent",
    "border",
    "borderAccent",
    "borderMuted",
    "success",
    "error",
    "warning",
    "muted",
    "dim",
    "text",
    "thinkingText",
    "scrollbarTrack",
    "scrollbarThumb",
    "searchMatchText",
    "userMessageText",
    "customMessageText",
    "customMessageLabel",
    "toolTitle",
    "toolOutput",
    "mdHeading",
    "mdLink",
    "mdLinkUrl",
    "mdCode",
    "mdCodeBlock",
    "mdCodeBlockBorder",
    "mdQuote",
    "mdQuoteBorder",
    "mdHr",
    "mdListBullet",
    "toolDiffAdded",
    "toolDiffRemoved",
    "toolDiffContext",
    "syntaxComment",
    "syntaxKeyword",
    "syntaxFunction",
    "syntaxVariable",
    "syntaxString",
    "syntaxNumber",
    "syntaxType",
    "syntaxOperator",
    "syntaxPunctuation",
    "thinkingOff",
    "thinkingMinimal",
    "thinkingLow",
    "thinkingMedium",
    "thinkingHigh",
    "thinkingXhigh",
    "thinkingMax",
    "bashMode",
    # MISAKA fork: startup section titles and the app title.
    "sectionTitle",
    "appTitle",
    "appTitleOnLight",
]

type ThemeBg = Literal[
    "selectedBg",
    "searchMatchBg",
    "userMessageBg",
    "customMessageBg",
    "toolPendingBg",
    "toolSuccessBg",
    "toolErrorBg",
]

# Required tokens are present; tokens with a fallback may be missing.
type ThemeColorValues[Value] = Mapping[str, Value]

type ResolvedThemeColorValues[Value] = Mapping[ThemeToken, Value]

__all__ = [
    "THEME_TOKENS",
    "ResolvedThemeColorValues",
    "ThemeBg",
    "ThemeColor",
    "ThemeColorValues",
    "ThemeToken",
    "ThemeTokenDescriptor",
    "ThemeTokenDescriptors",
    "defineThemeTokens",
]

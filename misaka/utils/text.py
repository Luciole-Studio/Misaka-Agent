"""Translated from pi's ``coding-agent/src/utils/text.ts`` (v1.1.0)."""

from __future__ import annotations

from typing import TypedDict


class SplitBomResult(TypedDict):
    bom: str
    text: str


def splitBom(content: str) -> SplitBomResult:
    """Split a leading UTF-8 byte order mark from decoded text."""
    return {"bom": "﻿", "text": content[1:]} if content.startswith("﻿") else {"bom": "", "text": content}


def stripBom(content: str) -> str:
    """Remove a leading UTF-8 byte order mark from decoded text."""
    return splitBom(content)["text"]


__all__ = ["SplitBomResult", "splitBom", "stripBom"]

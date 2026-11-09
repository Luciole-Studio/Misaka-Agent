"""JSON helpers, translated from pi's ``coding-agent/src/utils/json.ts``."""

from __future__ import annotations

import re

_STRING_OR_LINE_COMMENT = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*')
_STRING_OR_TRAILING_COMMA = re.compile(r'"(?:\\.|[^"\\])*"|,(\s*[}\]])')


def stripJsonComments(input: str) -> str:
    """Strip ``//`` line comments and trailing commas from JSON, leaving string literals untouched."""
    without_comments = _STRING_OR_LINE_COMMENT.sub(
        lambda match: match.group(0) if match.group(0)[0] == '"' else "", input
    )
    return _STRING_OR_TRAILING_COMMA.sub(
        lambda match: match.group(1) if match.group(1) is not None else match.group(0), without_comments
    )


__all__ = ["stripJsonComments"]

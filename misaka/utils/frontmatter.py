"""YAML frontmatter parsing helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ruamel.yaml import YAML


@dataclass(slots=True)
class ParsedFrontmatter[T: dict[str, Any]]:
    frontmatter: T
    body: str


def parse_frontmatter(content: str) -> ParsedFrontmatter[dict[str, Any]]:
    yaml_string, body = _extract_frontmatter(content)
    if yaml_string is None:
        return ParsedFrontmatter(frontmatter={}, body=body)

    parsed = _yaml_load(yaml_string)
    if not isinstance(parsed, dict):
        parsed = {}
    return ParsedFrontmatter(frontmatter=parsed, body=body)


def _normalize_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _extract_frontmatter(content: str) -> tuple[str | None, str]:
    # A UTF-8 BOM makes startswith("---") always false and silently drops the frontmatter (upstream #8337/1355cd36e)
    normalized = _normalize_newlines(content.removeprefix("\ufeff"))
    if not normalized.startswith("---"):
        return None, normalized

    end_index = normalized.find("\n---", 3)
    if end_index == -1:
        return None, normalized

    return normalized[4 : end_index + 1], normalized[end_index + 4 :].strip()


class FrontmatterError(ValueError):
    """The frontmatter block is not valid YAML; callers turn this into a validation message."""


def _yaml_load(content: str) -> Any:
    from ruamel.yaml.error import YAMLError
    try:
        return YAML(typ="safe").load(content)
    except YAMLError as error:
        raise FrontmatterError(str(error).strip().splitlines()[0]) from error


__all__ = [
    "FrontmatterError",
    "ParsedFrontmatter",
    "parse_frontmatter",
]

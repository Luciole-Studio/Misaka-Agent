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


def strip_frontmatter(content: str) -> str:
    return parse_frontmatter(content).body


def _normalize_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _extract_frontmatter(content: str) -> tuple[str | None, str]:
    normalized = _normalize_newlines(content)
    if not normalized.startswith("---"):
        return None, normalized

    end_index = normalized.find("\n---", 3)
    if end_index == -1:
        return None, normalized

    return normalized[4 : end_index + 1], normalized[end_index + 4 :].strip()


def _yaml_load(content: str) -> Any:
    yaml = YAML(typ="safe")
    return yaml.load(content)


parseFrontmatter = parse_frontmatter
stripFrontmatter = strip_frontmatter

__all__ = [
    "parseFrontmatter",
    "stripFrontmatter",
]

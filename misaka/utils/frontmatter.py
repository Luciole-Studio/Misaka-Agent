"""YAML frontmatter parsing helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ruamel.yaml import YAML

from misaka.utils.js import jsTrim
from misaka.utils.text import stripBom


@dataclass(slots=True)
class ParsedFrontmatter[T: dict[str, Any]]:
    frontmatter: T
    body: str


def parseFrontmatter(content: str) -> ParsedFrontmatter[dict[str, Any]]:
    yaml_string, body = extractFrontmatter(content)
    if yaml_string is None:
        return ParsedFrontmatter(frontmatter={}, body=body)

    parsed = _yaml_load(yaml_string)
    # MISAKA fork: YAML that is not a mapping reads as no frontmatter (pi returns the parsed value as is); every
    # caller reads it as a mapping.
    if not isinstance(parsed, dict):
        parsed = {}
    return ParsedFrontmatter(frontmatter=parsed, body=body)


def normalizeNewlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def extractFrontmatter(content: str) -> tuple[str | None, str]:
    normalized = normalizeNewlines(stripBom(content))
    if not normalized.startswith("---"):
        return None, normalized

    end_index = normalized.find("\n---", 3)
    if end_index == -1:
        return None, normalized

    # MISAKA fork: the YAML keeps its last line break (pi slices it off); the `yaml` package ends a `|` block at the
    # end of input with a line break, ruamel only with one in the input.
    return normalized[4 : end_index + 1], jsTrim(normalized[end_index + 4 :])


def stripFrontmatter(content: str) -> str:
    return parseFrontmatter(content).body


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
    "parseFrontmatter",
    "stripFrontmatter",
]

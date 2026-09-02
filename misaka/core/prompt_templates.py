"""Prompt-template loading and expansion helpers."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from misaka.core.source_info import SourceInfo, create_synthetic_source_info
from misaka.utils.frontmatter import parse_frontmatter
from misaka.utils.paths import resolve_path


@dataclass(slots=True)
class PromptTemplate:
    name: str
    description: str
    content: str
    sourceInfo: SourceInfo
    filePath: str
    argumentHint: str | None = None


class LoadPromptTemplatesOptions(TypedDict):
    cwd: str
    agentDir: str
    promptPaths: list[str]


_ECMASCRIPT_WHITESPACE = frozenset(
    "\t\n\v\f\r \u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)


def parse_command_args(args_string: str) -> list[str]:
    args: list[str] = []
    current = ""
    in_quote: str | None = None
    for char in args_string:
        if in_quote is not None:
            if char == in_quote:
                in_quote = None
            else:
                current += char
        elif char in {'"', "'"}:
            in_quote = char
        elif char in _ECMASCRIPT_WHITESPACE:
            if current:
                args.append(current)
                current = ""
        else:
            current += char
    if current:
        args.append(current)
    return args


# One alternation covering every placeholder form, so a single pass over the template
# rewrites each one exactly once.  Sequential passes would re-scan text that an earlier
# pass had already substituted, letting an argument value that happens to contain
# "$ARGUMENTS" expand a second time.
_SUBSTITUTE_ARGS_RE = re.compile(
    r"\$\{([0-9]+|ARGUMENTS|@):-([^}]*)\}"  # ${N:-default} / ${@:-default} / ${ARGUMENTS:-default}
    r"|\$\{@:([0-9]+)(?::([0-9]+))?\}"  # ${@:N} / ${@:N:L}
    r"|\$(ARGUMENTS|@|[0-9]+)"  # $ARGUMENTS / $@ / $N
)


def _parse_decimal_capped(raw: str, cap: int) -> int:
    """Parse a decimal only as far as the caller can observe."""
    normalized = raw.lstrip("0") or "0"
    limit = str(cap)
    if len(normalized) > len(limit) or (len(normalized) == len(limit) and normalized > limit):
        return cap
    return int(normalized)


def substitute_args(content: str, args: list[str]) -> str:
    """Substitute argument placeholders in template content.

    Supports ``$1``/``$2``..., ``$@`` and ``$ARGUMENTS`` for all args, ``${N:-default}``
    for a positional arg with a default when missing or empty, ``${@:-default}`` /
    ``${ARGUMENTS:-default}`` for all args with a default when empty, and the bash-style
    slices ``${@:N}`` and ``${@:N:L}``.

    Replacement happens on the template string only: argument and default values that
    themselves contain ``$1``, ``$@`` or ``$ARGUMENTS`` are NOT recursively substituted.
    """
    all_args = " ".join(args)

    def positional(raw_index: str) -> str:
        index = _parse_decimal_capped(raw_index, len(args) + 1) - 1
        return args[index] if 0 <= index < len(args) else ""

    def replace(match: re.Match[str]) -> str:
        default_target, default_value, slice_start, slice_length, simple = match.groups()

        if default_target is not None:
            value = all_args if default_target in ("@", "ARGUMENTS") else positional(default_target)
            return value if value else default_value

        if slice_start is not None:
            start = max(_parse_decimal_capped(slice_start, len(args) + 1) - 1, 0)
            if slice_length is not None:
                length = _parse_decimal_capped(slice_length, len(args) - start)
                return " ".join(args[start : start + length])
            return " ".join(args[start:])

        if simple in ("ARGUMENTS", "@"):
            return all_args
        return positional(simple)

    return _SUBSTITUTE_ARGS_RE.sub(replace, content)


def parse_prompt_template_invocation(text: str) -> tuple[str, str] | None:
    """Return the template name and raw argument text using Pi's JavaScript whitespace rules."""
    if not text.startswith("/"):
        return None

    name_end = 1
    while name_end < len(text) and text[name_end] not in _ECMASCRIPT_WHITESPACE:
        name_end += 1
    if name_end == 1:
        return None

    args_start = name_end
    while args_start < len(text) and text[args_start] in _ECMASCRIPT_WHITESPACE:
        args_start += 1
    return text[1:name_end], text[args_start:]


def load_prompt_templates(options: LoadPromptTemplatesOptions) -> list[PromptTemplate]:
    resolved_cwd = resolve_path(options["cwd"])
    resolved_agent_dir = resolve_path(options["agentDir"])
    prompt_paths = options["promptPaths"]

    templates: list[PromptTemplate] = []
    global_prompts_dir = os.path.join(resolved_agent_dir, "prompts")
    project_prompts_dir = os.path.join(resolved_cwd, ".misaka", "prompts")

    def is_under_path(target: str, root: str) -> bool:
        normalized_root = os.path.abspath(root)
        normalized_target = os.path.abspath(target)
        if normalized_target == normalized_root:
            return True
        prefix = normalized_root if normalized_root.endswith(os.sep) else f"{normalized_root}{os.sep}"
        return normalized_target.startswith(prefix)

    def get_source_info(resolved_path: str) -> SourceInfo:
        if is_under_path(resolved_path, global_prompts_dir):
            return create_synthetic_source_info(
                resolved_path,
                {"source": "local", "scope": "user", "baseDir": global_prompts_dir},
            )
        if is_under_path(resolved_path, project_prompts_dir):
            return create_synthetic_source_info(
                resolved_path,
                {"source": "local", "scope": "project", "baseDir": project_prompts_dir},
            )
        base_dir = resolved_path if os.path.isdir(resolved_path) else os.path.dirname(resolved_path)
        return create_synthetic_source_info(
            resolved_path,
            {"source": "local", "baseDir": base_dir},
        )

    # `includeDefaults` used to sit here (always False at the one call site,
    # `resource_loader.py`), auto-loading `<agentDir>/prompts` and -- with no
    # `isProjectTrusted()` gate of its own -- `<cwd>/.misaka/prompts`. The package manager
    # is where project prompts get their trust gate, so the dead branch was a latent hole
    # rather than a feature; removed with the option (audit 2026-09-02, core-config-05).

    for raw_path in prompt_paths:
        resolved = resolve_path(raw_path, resolved_cwd, trim=True)
        if not os.path.exists(resolved):
            continue
        try:
            if os.path.isdir(resolved):
                templates.extend(_load_templates_from_dir(resolved, get_source_info))
            elif os.path.isfile(resolved) and resolved.endswith(".md"):
                template = _load_template_from_file(resolved, get_source_info(resolved))
                if template is not None:
                    templates.append(template)
        except Exception:  # noqa: BLE001, S112 - an unreadable entry is skipped
            continue

    return templates


def expand_prompt_template(text: str, templates: list[PromptTemplate]) -> str:
    invocation = parse_prompt_template_invocation(text)
    if invocation is None:
        return text
    template_name, args_string = invocation
    template = next((candidate for candidate in templates if candidate.name == template_name), None)
    if template is None:
        return text
    return substitute_args(template.content, parse_command_args(args_string))


def _load_templates_from_dir(
    dir_path: str,
    get_source_info: Callable[[str], SourceInfo],
) -> list[PromptTemplate]:
    if not os.path.isdir(dir_path):
        return []
    templates: list[PromptTemplate] = []
    try:
        for entry in os.scandir(dir_path):
            entry_path = entry.path
            is_file = entry.is_file(follow_symlinks=False)
            if entry.is_symlink():
                try:
                    is_file = os.stat(entry_path).st_mode is not None and os.path.isfile(entry_path)
                except Exception:  # noqa: BLE001, S112 - an unreadable entry is skipped
                    continue
            if is_file and entry.name.endswith(".md"):
                template = _load_template_from_file(entry_path, get_source_info(entry_path))
                if template is not None:
                    templates.append(template)
    except Exception:  # noqa: BLE001
        return []
    return templates


def _load_template_from_file(file_path: str, source_info: SourceInfo) -> PromptTemplate | None:
    try:
        raw_content = Path(file_path).read_text(encoding="utf-8")
        parsed = parse_frontmatter(raw_content)
        frontmatter = parsed.frontmatter
        body = parsed.body
        name = Path(file_path).stem
        description = frontmatter.get("description") or ""
        if not description:
            first_line = next((line for line in body.split("\n") if line.strip()), None)
            if first_line:
                description = first_line[:60] + ("..." if len(first_line) > 60 else "")
        return PromptTemplate(
            name=name,
            description=description,
            argumentHint=frontmatter.get("argument-hint") or None,
            content=body,
            sourceInfo=source_info,
            filePath=file_path,
        )
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "LoadPromptTemplatesOptions",
    "PromptTemplate",
    ]

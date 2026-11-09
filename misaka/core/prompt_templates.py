"""Translated from pi's ``packages/coding-agent/src/core/prompt-templates.ts`` (v1.3.0).

MISAKA forks:
- The project prompts directory is the project's MISAKA home (``home.project_dir``), which a cwd at $HOME has none of.
- ``parse_prompt_template_invocation`` is the template-name match of ``expandPromptTemplate``, shared with interactive
  mode; ``_ECMASCRIPT_WHITESPACE`` is kept for ``misaka.core.subagent``, which imports it from here.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from misaka.config import home
from misaka.core.diagnostics import ResourceDiagnostic
from misaka.core.source_info import SourceInfo, createSyntheticSourceInfo
from misaka.utils.frontmatter import parseFrontmatter
from misaka.utils.js import (
    ECMASCRIPT_WHITESPACE,
    jsLength,
    jsSlice,
    jsTrim,
    toWellFormed,
)
from misaka.utils.node_fs import readdirEntries
from misaka.utils.paths import resolvePath

# MISAKA fork: misaka.core.subagent imports this name from here (a set it tests characters against).
_ECMASCRIPT_WHITESPACE = frozenset(ECMASCRIPT_WHITESPACE)


@dataclass(slots=True)
class PromptTemplate:
    """Represents a prompt template loaded from a markdown file"""

    name: str
    description: str
    content: str
    sourceInfo: SourceInfo
    filePath: str  # Absolute path to the template file
    argumentHint: str | None = None


def parseCommandArgs(argsString: str) -> list[str]:
    """Parse command arguments respecting quoted strings (bash-style)
    Returns array of arguments
    """
    args: list[str] = []
    current = ""
    # Tracks whether a token has started, so quoted empty strings ("" or '') are kept as arguments
    hasToken = False
    inQuote: str | None = None

    for char in argsString:
        if inQuote:
            if char == inQuote:
                inQuote = None
            else:
                current += char
        elif char == '"' or char == "'":
            inQuote = char
            hasToken = True
        elif char in ECMASCRIPT_WHITESPACE:
            if hasToken:
                args.append(current)
                current = ""
                hasToken = False
        else:
            current += char
            hasToken = True

    if hasToken:
        args.append(current)

    return args


_SUBSTITUTE_ARGS_RE = re.compile(
    r"\$\{([0-9]+|ARGUMENTS|@):-([^}]*)\}|\$\{@:([0-9]+)(?::([0-9]+))?\}|\$(ARGUMENTS|@|[0-9]+)"
)


def _parseIntCapped(raw: str, cap: int) -> int:
    """``parseInt(raw, 10)``, capped at what the caller can observe (Python refuses very long digit strings)."""
    normalized = raw.lstrip("0") or "0"
    limit = str(cap)
    if len(normalized) > len(limit) or (len(normalized) == len(limit) and normalized > limit):
        return cap
    return int(normalized)


def substituteArgs(content: str, args: list[str]) -> str:
    """Substitute argument placeholders in template content
    Supports:
    - $1, $2, ... for positional args
    - $@ and $ARGUMENTS for all args
    - ${N:-default} for positional arg N with default when missing/empty
    - ${@:-default} and ${ARGUMENTS:-default} for all args with a default when empty
    - ${@:N} for args from Nth onwards (bash-style slicing)
    - ${@:N:L} for L args starting from Nth

    Note: Replacement happens on the template string only. Argument and default values
    containing patterns like $1, $@, or $ARGUMENTS are NOT recursively substituted.
    """
    allArgs = " ".join(args)

    def positional(raw: str) -> str:
        index = _parseIntCapped(raw, len(args) + 1) - 1
        return args[index] if 0 <= index < len(args) else ""

    def replace(match: re.Match[str]) -> str:
        defaultTarget, defaultValue, sliceStart, sliceLength, simple = match.groups()
        if defaultTarget:
            value = allArgs if defaultTarget == "@" or defaultTarget == "ARGUMENTS" else positional(defaultTarget)
            return value if value else defaultValue

        if sliceStart:
            start = _parseIntCapped(sliceStart, len(args) + 1) - 1  # Convert to 0-indexed (user provides 1-indexed)
            # Treat 0 as 1 (bash convention: args start at 1)
            start = max(start, 0)

            if sliceLength:
                length = _parseIntCapped(sliceLength, len(args) - start)
                return " ".join(args[start : start + length])
            return " ".join(args[start:])

        if simple == "ARGUMENTS" or simple == "@":
            return allArgs

        return positional(simple)

    return _SUBSTITUTE_ARGS_RE.sub(replace, content)


@dataclass(slots=True)
class _LoadedTemplate:
    template: PromptTemplate | None
    diagnostics: list[ResourceDiagnostic]


def loadTemplateFromFile(filePath: str, sourceInfo: SourceInfo) -> _LoadedTemplate:
    diagnostics: list[ResourceDiagnostic] = []
    try:
        # readFileSync(..., "utf-8") decodes invalid bytes to U+FFFD rather than failing.
        rawContent = Path(filePath).read_bytes().decode("utf-8", errors="replace")
    except Exception as error:  # noqa: BLE001 - reported, not swallowed
        message = str(error) or "failed to read prompt template file"
        diagnostics.append(ResourceDiagnostic(type="warning", message=message, path=filePath))
        return _LoadedTemplate(template=None, diagnostics=diagnostics)

    try:
        parsed = parseFrontmatter(rawContent)
    except Exception as error:  # noqa: BLE001 - reported, not swallowed
        message = str(error) or "failed to parse prompt template file"
        diagnostics.append(ResourceDiagnostic(type="warning", message=message, path=filePath))
        return _LoadedTemplate(template=None, diagnostics=diagnostics)
    frontmatter, body = parsed.frontmatter, parsed.body

    name = os.path.basename(filePath).removesuffix(".md")

    # Get description from frontmatter or first non-empty line
    description = frontmatter.get("description") if isinstance(frontmatter.get("description"), str) else ""
    if not description:
        firstLine = next((line for line in body.split("\n") if jsTrim(line)), None)
        if firstLine:
            # Truncate if too long
            # MISAKA fork: a cut through a surrogate pair becomes U+FFFD here (Node writes U+FFFD for it on output;
            # Python cannot encode a lone surrogate at all).
            description = toWellFormed(jsSlice(firstLine, 0, 60))
            if jsLength(firstLine) > 60:
                description += "..."

    argumentHint = frontmatter.get("argument-hint") if isinstance(frontmatter.get("argument-hint"), str) else None
    return _LoadedTemplate(
        template=PromptTemplate(
            name=name,
            description=description,
            argumentHint=argumentHint or None,
            content=body,
            sourceInfo=sourceInfo,
            filePath=filePath,
        ),
        diagnostics=diagnostics,
    )


def loadTemplatesFromDir(dir: str, getSourceInfo: Callable[[str], SourceInfo]) -> LoadPromptTemplatesResult:
    """Scan a directory for .md files (non-recursive) and load them as prompt templates."""
    templates: list[PromptTemplate] = []
    diagnostics: list[ResourceDiagnostic] = []

    if not os.path.exists(dir):
        return LoadPromptTemplatesResult(templates=templates, diagnostics=diagnostics)

    try:
        entries = readdirEntries(dir)

        for entry in entries:
            fullPath = os.path.join(dir, entry.name)

            # For symlinks, check if they point to a file
            isFile = entry.is_file(follow_symlinks=False)
            if entry.is_symlink():
                try:
                    isFile = os.path.isfile(fullPath) and os.stat(fullPath) is not None
                except Exception:  # noqa: BLE001, S112
                    # Broken symlink, skip it
                    continue

            if isFile and entry.name.endswith(".md"):
                result = loadTemplateFromFile(fullPath, getSourceInfo(fullPath))
                if result.template:
                    templates.append(result.template)
                diagnostics.extend(result.diagnostics)
    except Exception:  # noqa: BLE001
        return LoadPromptTemplatesResult(templates=templates, diagnostics=diagnostics)

    return LoadPromptTemplatesResult(templates=templates, diagnostics=diagnostics)


class LoadPromptTemplatesOptions(TypedDict):
    # Working directory for project-local templates.
    cwd: str
    # Agent config directory for global templates.
    agentDir: str
    # Explicit prompt template paths (files or directories).
    promptPaths: list[str]
    # Include default prompt directories.
    includeDefaults: bool


@dataclass(slots=True)
class LoadPromptTemplatesResult:
    templates: list[PromptTemplate]
    diagnostics: list[ResourceDiagnostic]


def loadPromptTemplates(options: LoadPromptTemplatesOptions) -> LoadPromptTemplatesResult:
    """Load all prompt templates from:
    1. Global: agentDir/prompts/
    2. Project: cwd/{CONFIG_DIR_NAME}/prompts/
    3. Explicit prompt paths
    """
    resolvedCwd = resolvePath(options["cwd"])
    resolvedAgentDir = resolvePath(options["agentDir"])
    promptPaths = options["promptPaths"]
    includeDefaults = options["includeDefaults"]

    templates: list[PromptTemplate] = []
    diagnostics: list[ResourceDiagnostic] = []

    def addResult(result: LoadPromptTemplatesResult) -> None:
        templates.extend(result.templates)
        diagnostics.extend(result.diagnostics)

    globalPromptsDir = os.path.join(resolvedAgentDir, "prompts")
    # MISAKA fork: the project's MISAKA home, which a cwd at $HOME has none of.
    projectDir = home.project_dir(resolvedCwd)
    projectPromptsDir = str(projectDir / "prompts") if projectDir is not None else None

    def isUnderPath(target: str, root: str) -> bool:
        normalizedRoot = os.path.abspath(root)
        if os.path.abspath(target) == normalizedRoot:
            return True
        prefix = normalizedRoot if normalizedRoot.endswith(os.sep) else f"{normalizedRoot}{os.sep}"
        return os.path.abspath(target).startswith(prefix)

    def getSourceInfo(resolvedPath: str) -> SourceInfo:
        if isUnderPath(resolvedPath, globalPromptsDir):
            return createSyntheticSourceInfo(resolvedPath, {"source": "local", "scope": "user", "baseDir": globalPromptsDir})
        if projectPromptsDir is not None and isUnderPath(resolvedPath, projectPromptsDir):
            return createSyntheticSourceInfo(
                resolvedPath, {"source": "local", "scope": "project", "baseDir": projectPromptsDir}
            )
        return createSyntheticSourceInfo(
            resolvedPath,
            {"source": "local", "baseDir": resolvedPath if os.path.isdir(resolvedPath) else os.path.dirname(resolvedPath)},
        )

    if includeDefaults:
        addResult(loadTemplatesFromDir(globalPromptsDir, getSourceInfo))
        if projectPromptsDir is not None:
            addResult(loadTemplatesFromDir(projectPromptsDir, getSourceInfo))

    # 3. Load explicit prompt paths
    for rawPath in promptPaths:
        resolvedPath = resolvePath(rawPath, resolvedCwd, {"trim": True})
        if not os.path.exists(resolvedPath):
            continue

        try:
            if os.path.isdir(resolvedPath):
                addResult(loadTemplatesFromDir(resolvedPath, getSourceInfo))
            elif os.path.isfile(resolvedPath) and resolvedPath.endswith(".md"):
                result = loadTemplateFromFile(resolvedPath, getSourceInfo(resolvedPath))
                if result.template:
                    templates.append(result.template)
                diagnostics.extend(result.diagnostics)
        except Exception as error:  # noqa: BLE001 - an unreadable entry becomes a warning
            message = str(error) or "failed to read prompt template path"
            diagnostics.append(ResourceDiagnostic(type="warning", message=message, path=resolvedPath))

    return LoadPromptTemplatesResult(templates=templates, diagnostics=diagnostics)


def parse_prompt_template_invocation(text: str) -> tuple[str, str] | None:
    """MISAKA fork: ``text.match(/^\\/([^\\s]+)(?:\\s+([\\s\\S]*))?$/)`` with JavaScript's ``\\s``: the template name and
    the argument text, or None."""
    if not text.startswith("/"):
        return None

    nameEnd = 1
    while nameEnd < len(text) and text[nameEnd] not in ECMASCRIPT_WHITESPACE:
        nameEnd += 1
    if nameEnd == 1:
        return None

    argsStart = nameEnd
    while argsStart < len(text) and text[argsStart] in ECMASCRIPT_WHITESPACE:
        argsStart += 1
    return text[1:nameEnd], text[argsStart:]


def expandPromptTemplate(text: str, templates: list[PromptTemplate]) -> str:
    """Expand a prompt template if it matches a template name.
    Returns the expanded content or the original text if not a template.
    """
    if not text.startswith("/"):
        return text

    match = parse_prompt_template_invocation(text)
    if not match:
        return text

    templateName, argsString = match

    template = next((t for t in templates if t.name == templateName), None)
    if template:
        args = parseCommandArgs(argsString)
        return substituteArgs(template.content, args)

    return text


__all__ = [
    "LoadPromptTemplatesOptions",
    "LoadPromptTemplatesResult",
    "PromptTemplate",
    "expandPromptTemplate",
    "loadPromptTemplates",
    "parseCommandArgs",
    "substituteArgs",
]

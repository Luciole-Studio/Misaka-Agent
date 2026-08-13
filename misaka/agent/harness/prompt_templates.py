"""Prompt-template loading and formatting helpers for the harness."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

from ruamel.yaml import YAML

from misaka.agent.harness.types import (
    Err,
    ExecutionEnv,
    FileInfo,
    PromptTemplate,
    Result,
    err,
    ok,
    to_error,
)

PromptTemplateDiagnosticCode = Literal["file_info_failed", "list_failed", "read_failed", "parse_failed"]
TSource = TypeVar("TSource")
TPromptTemplate = TypeVar("TPromptTemplate", bound=PromptTemplate)


@dataclass(slots=True)
class PromptTemplateDiagnostic:
    code: PromptTemplateDiagnosticCode
    message: str
    path: str
    type: Literal["warning"] = field(default="warning", init=False)


@dataclass(slots=True)
class SourcedPromptTemplate[TSource, TPromptTemplate: PromptTemplate]:
    promptTemplate: TPromptTemplate
    source: TSource


@dataclass(slots=True)
class SourcedPromptTemplateDiagnostic[TSource]:
    code: PromptTemplateDiagnosticCode
    message: str
    path: str
    source: TSource
    type: Literal["warning"] = field(default="warning", init=False)


@dataclass(slots=True)
class LoadPromptTemplatesResult:
    promptTemplates: list[PromptTemplate]
    diagnostics: list[PromptTemplateDiagnostic]


@dataclass(slots=True)
class LoadSourcedPromptTemplatesResult[TSource, TPromptTemplate: PromptTemplate]:
    promptTemplates: list[SourcedPromptTemplate[TSource, TPromptTemplate]]
    diagnostics: list[SourcedPromptTemplateDiagnostic[TSource]]


@dataclass(slots=True)
class _PromptTemplateFrontmatter:
    description: str | None = None
    argument_hint: str | None = None


async def load_prompt_templates(
    env: ExecutionEnv,
    paths: str | list[str],
) -> LoadPromptTemplatesResult:
    prompt_templates: list[PromptTemplate] = []
    diagnostics: list[PromptTemplateDiagnostic] = []
    for path in [paths] if isinstance(paths, str) else list(paths):
        info_result = await env.fileInfo(path)
        if isinstance(info_result, Err):
            if info_result.error.code != "not_found":
                diagnostics.append(
                    PromptTemplateDiagnostic(
                        code="file_info_failed",
                        message=str(info_result.error),
                        path=path,
                    )
                )
            continue
        info = info_result.value
        kind = await _resolve_kind(env, info, diagnostics)
        if kind == "directory":
            result = await _load_templates_from_dir(env, info.path)
            prompt_templates.extend(result.promptTemplates)
            diagnostics.extend(result.diagnostics)
        elif kind == "file" and info.name.endswith(".md"):
            result = await _load_template_from_file(env, info.path)
            if result.promptTemplate is not None:
                prompt_templates.append(result.promptTemplate)
            diagnostics.extend(result.diagnostics)
    return LoadPromptTemplatesResult(promptTemplates=prompt_templates, diagnostics=diagnostics)


async def load_sourced_prompt_templates(
    env: ExecutionEnv,
    inputs: list[dict[str, Any]],
    mapPromptTemplate: Any | None = None,
) -> LoadSourcedPromptTemplatesResult[Any, Any]:
    prompt_templates: list[SourcedPromptTemplate[Any, Any]] = []
    diagnostics: list[SourcedPromptTemplateDiagnostic[Any]] = []
    for input_item in inputs:
        result = await load_prompt_templates(env, input_item["path"])
        for prompt_template in result.promptTemplates:
            mapped = (
                mapPromptTemplate(prompt_template, input_item["source"])
                if mapPromptTemplate is not None
                else prompt_template
            )
            prompt_templates.append(SourcedPromptTemplate(promptTemplate=mapped, source=input_item["source"]))
        for diagnostic in result.diagnostics:
            diagnostics.append(
                SourcedPromptTemplateDiagnostic(
                    code=diagnostic.code,
                    message=diagnostic.message,
                    path=diagnostic.path,
                    source=input_item["source"],
                )
            )
    return LoadSourcedPromptTemplatesResult(promptTemplates=prompt_templates, diagnostics=diagnostics)


@dataclass(slots=True)
class _LoadTemplateFromFileResult:
    promptTemplate: PromptTemplate | None
    diagnostics: list[PromptTemplateDiagnostic]


async def _load_templates_from_dir(env: ExecutionEnv, dir: str) -> LoadPromptTemplatesResult:
    prompt_templates: list[PromptTemplate] = []
    diagnostics: list[PromptTemplateDiagnostic] = []
    entries_result = await env.listDir(dir)
    if isinstance(entries_result, Err):
        diagnostics.append(
            PromptTemplateDiagnostic(
                code="list_failed",
                message=str(entries_result.error),
                path=dir,
            )
        )
        return LoadPromptTemplatesResult(promptTemplates=prompt_templates, diagnostics=diagnostics)

    for entry in sorted(entries_result.value, key=lambda item: item.name):
        kind = await _resolve_kind(env, entry, diagnostics)
        if kind != "file" or not entry.name.endswith(".md"):
            continue
        result = await _load_template_from_file(env, entry.path)
        if result.promptTemplate is not None:
            prompt_templates.append(result.promptTemplate)
        diagnostics.extend(result.diagnostics)
    return LoadPromptTemplatesResult(promptTemplates=prompt_templates, diagnostics=diagnostics)


async def _load_template_from_file(env: ExecutionEnv, file_path: str) -> _LoadTemplateFromFileResult:
    diagnostics: list[PromptTemplateDiagnostic] = []
    raw_content = await env.readTextFile(file_path)
    if isinstance(raw_content, Err):
        diagnostics.append(
            PromptTemplateDiagnostic(
                code="read_failed",
                message=str(raw_content.error),
                path=file_path,
            )
        )
        return _LoadTemplateFromFileResult(promptTemplate=None, diagnostics=diagnostics)

    parsed = _parse_frontmatter(raw_content.value)
    if isinstance(parsed, Err):
        diagnostics.append(
            PromptTemplateDiagnostic(
                code="parse_failed",
                message=str(parsed.error),
                path=file_path,
            )
        )
        return _LoadTemplateFromFileResult(promptTemplate=None, diagnostics=diagnostics)

    frontmatter, body = parsed.value
    first_line = next((line for line in body.split("\n") if line.strip()), None)
    description = frontmatter.description if isinstance(frontmatter.description, str) else ""
    if not description and first_line:
        description = first_line[:60]
        if len(first_line) > 60:
            description += "..."
    return _LoadTemplateFromFileResult(
        promptTemplate=PromptTemplate(
            name=_basename_env_path(file_path).removesuffix(".md"),
            description=description,
            content=body,
        ),
        diagnostics=diagnostics,
    )


async def _resolve_kind(
    env: ExecutionEnv,
    info: FileInfo,
    diagnostics: list[PromptTemplateDiagnostic],
) -> Literal["file", "directory"] | None:
    if info.kind in {"file", "directory"}:
        return info.kind
    canonical_path = await env.canonicalPath(info.path)
    if isinstance(canonical_path, Err):
        if canonical_path.error.code != "not_found":
            diagnostics.append(
                PromptTemplateDiagnostic(
                    code="file_info_failed",
                    message=str(canonical_path.error),
                    path=info.path,
                )
            )
        return None
    target = await env.fileInfo(canonical_path.value)
    if isinstance(target, Err):
        if target.error.code != "not_found":
            diagnostics.append(
                PromptTemplateDiagnostic(
                    code="file_info_failed",
                    message=str(target.error),
                    path=info.path,
                )
            )
        return None
    if target.value.kind in {"file", "directory"}:
        return target.value.kind
    return None


def _parse_frontmatter(content: str) -> Result[tuple[_PromptTemplateFrontmatter, str], Exception]:
    try:
        normalized = content.replace("\r\n", "\n").replace("\r", "\n")
        if not normalized.startswith("---"):
            return ok((_PromptTemplateFrontmatter(), normalized))
        end_index = normalized.find("\n---", 3)
        if end_index == -1:
            return ok((_PromptTemplateFrontmatter(), normalized))
        yaml_string = normalized[4:end_index]
        body = normalized[end_index + 4 :].strip()
        data = _yaml_load(yaml_string)
        frontmatter = data if isinstance(data, Mapping) else {}
        description = frontmatter.get("description")
        argument_hint = frontmatter.get("argument-hint")
        return ok(
            (
                _PromptTemplateFrontmatter(
                    description=description if isinstance(description, str) else None,
                    argument_hint=argument_hint if isinstance(argument_hint, str) else None,
                ),
                body,
            )
        )
    except Exception as error:
        return err(to_error(error))


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
        elif char in {" ", "\t"}:
            if current:
                args.append(current)
                current = ""
        else:
            current += char
    if current:
        args.append(current)
    return args


def substitute_args(content: str, args: list[str]) -> str:
    import re

    def replace_index(match: Any) -> str:
        index = int(match.group(1)) - 1
        return args[index] if 0 <= index < len(args) else ""

    def replace_slice(match: Any) -> str:
        start = max(int(match.group(1)) - 1, 0)
        length = match.group(2)
        if length is not None:
            return " ".join(args[start : start + int(length)])
        return " ".join(args[start:])

    result = re.sub(r"\$(\d+)", replace_index, content)
    result = re.sub(r"\$\{@:(\d+)(?::(\d+))?\}", replace_slice, result)
    all_args = " ".join(args)
    result = result.replace("$ARGUMENTS", all_args)
    result = result.replace("$@", all_args)
    return result


def format_prompt_template_invocation(template: PromptTemplate, args: list[str] | None = None) -> str:
    return substitute_args(template.content, list(args or []))


def _basename_env_path(path: str) -> str:
    normalized = path.rstrip("/")
    if "/" not in normalized:
        return normalized
    return normalized.rsplit("/", 1)[1]


def _yaml_load(content: str) -> Any:
    yaml = YAML(typ="safe")
    return yaml.load(content)


loadPromptTemplates = load_prompt_templates
loadSourcedPromptTemplates = load_sourced_prompt_templates
parseCommandArgs = parse_command_args
substituteArgs = substitute_args
formatPromptTemplateInvocation = format_prompt_template_invocation

__all__ = [
    "LoadPromptTemplatesResult",
    "LoadSourcedPromptTemplatesResult",
    "PromptTemplateDiagnostic",
    "PromptTemplateDiagnosticCode",
    "SourcedPromptTemplate",
    "SourcedPromptTemplateDiagnostic",
    "formatPromptTemplateInvocation",
    "format_prompt_template_invocation",
    "loadPromptTemplates",
    "loadSourcedPromptTemplates",
    "load_prompt_templates",
    "load_sourced_prompt_templates",
    "parseCommandArgs",
    "parse_command_args",
    "substituteArgs",
    "substitute_args",
]

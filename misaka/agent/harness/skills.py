"""Skill loading and system-prompt invocation helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from collections.abc import Mapping
from typing import Any, Literal, TypeVar

from pathspec import GitIgnoreSpec
from ruamel.yaml import YAML

from misaka.agent.harness.types import Err, ExecutionEnv, FileInfo, Result, Skill, err, ok, to_error

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
IGNORE_FILE_NAMES = [".gitignore", ".ignore", ".fdignore"]

SkillDiagnosticCode = Literal[
    "file_info_failed",
    "list_failed",
    "read_failed",
    "parse_failed",
    "invalid_metadata",
]
TSource = TypeVar("TSource")
TSkill = TypeVar("TSkill", bound=Skill)


@dataclass(slots=True)
class SkillDiagnostic:
    code: SkillDiagnosticCode
    message: str
    path: str
    type: Literal["warning"] = field(default="warning", init=False)


@dataclass(slots=True)
class SourcedSkill[TSource, TSkill: Skill]:
    skill: TSkill
    source: TSource


@dataclass(slots=True)
class SourcedSkillDiagnostic[TSource]:
    code: SkillDiagnosticCode
    message: str
    path: str
    source: TSource
    type: Literal["warning"] = field(default="warning", init=False)


@dataclass(slots=True)
class LoadSkillsResult:
    skills: list[Skill]
    diagnostics: list[SkillDiagnostic]


@dataclass(slots=True)
class LoadSourcedSkillsResult[TSource, TSkill: Skill]:
    skills: list[SourcedSkill[TSource, TSkill]]
    diagnostics: list[SourcedSkillDiagnostic[TSource]]


@dataclass(slots=True)
class _LoadSkillFromFileResult:
    skill: Skill | None
    diagnostics: list[SkillDiagnostic]


@dataclass(slots=True)
class _SkillFrontmatter:
    name: str | None = None
    description: str | None = None
    disable_model_invocation: bool = False


class _IgnoreMatcher:
    def __init__(self) -> None:
        self._patterns: list[str] = []

    def add(self, patterns: list[str]) -> None:
        self._patterns.extend(patterns)

    def ignores(self, path: str) -> bool:
        if not self._patterns:
            return False
        return GitIgnoreSpec.from_lines(self._patterns).match_file(path)


def format_skill_invocation(skill: Skill, additional_instructions: str | None = None) -> str:
    skill_block = (
        f'<skill name="{skill.name}" location="{skill.filePath}">\n'
        f"References are relative to {_dirname_env_path(skill.filePath)}.\n\n"
        f"{skill.content}\n"
        "</skill>"
    )
    if additional_instructions:
        return f"{skill_block}\n\n{additional_instructions}"
    return skill_block


async def load_skills(env: ExecutionEnv, dirs: str | list[str]) -> LoadSkillsResult:
    skills: list[Skill] = []
    diagnostics: list[SkillDiagnostic] = []
    for dir in [dirs] if isinstance(dirs, str) else list(dirs):
        root_info_result = await env.fileInfo(dir)
        if isinstance(root_info_result, Err):
            if root_info_result.error.code != "not_found":
                diagnostics.append(
                    SkillDiagnostic(
                        code="file_info_failed",
                        message=str(root_info_result.error),
                        path=dir,
                    )
                )
            continue
        root_info = root_info_result.value
        if await _resolve_kind(env, root_info, diagnostics) != "directory":
            continue
        result = await _load_skills_from_dir_internal(
            env,
            root_info.path,
            include_root_files=True,
            ignore_matcher=_IgnoreMatcher(),
            root_dir=root_info.path,
        )
        skills.extend(result.skills)
        diagnostics.extend(result.diagnostics)
    return LoadSkillsResult(skills=skills, diagnostics=diagnostics)


async def load_sourced_skills(
    env: ExecutionEnv,
    inputs: list[dict[str, Any]],
    mapSkill: Any | None = None,
) -> LoadSourcedSkillsResult[Any, Any]:
    skills: list[SourcedSkill[Any, Any]] = []
    diagnostics: list[SourcedSkillDiagnostic[Any]] = []
    for input_item in inputs:
        result = await load_skills(env, input_item["path"])
        for skill in result.skills:
            mapped = mapSkill(skill, input_item["source"]) if mapSkill is not None else skill
            skills.append(SourcedSkill(skill=mapped, source=input_item["source"]))
        for diagnostic in result.diagnostics:
            diagnostics.append(
                SourcedSkillDiagnostic(
                    code=diagnostic.code,
                    message=diagnostic.message,
                    path=diagnostic.path,
                    source=input_item["source"],
                )
            )
    return LoadSourcedSkillsResult(skills=skills, diagnostics=diagnostics)


async def _load_skills_from_dir_internal(
    env: ExecutionEnv,
    dir: str,
    include_root_files: bool,
    ignore_matcher: _IgnoreMatcher,
    root_dir: str,
) -> LoadSkillsResult:
    skills: list[Skill] = []
    diagnostics: list[SkillDiagnostic] = []

    dir_info_result = await env.fileInfo(dir)
    if isinstance(dir_info_result, Err):
        if dir_info_result.error.code != "not_found":
            diagnostics.append(
                SkillDiagnostic(
                    code="file_info_failed",
                    message=str(dir_info_result.error),
                    path=dir,
                )
            )
        return LoadSkillsResult(skills=skills, diagnostics=diagnostics)
    if await _resolve_kind(env, dir_info_result.value, diagnostics) != "directory":
        return LoadSkillsResult(skills=skills, diagnostics=diagnostics)

    await _add_ignore_rules(env, ignore_matcher, dir, root_dir, diagnostics)

    entries_result = await env.listDir(dir)
    if isinstance(entries_result, Err):
        diagnostics.append(
            SkillDiagnostic(
                code="list_failed",
                message=str(entries_result.error),
                path=dir,
            )
        )
        return LoadSkillsResult(skills=skills, diagnostics=diagnostics)
    entries = entries_result.value

    for entry in entries:
        if entry.name != "SKILL.md":
            continue
        kind = await _resolve_kind(env, entry, diagnostics)
        if kind != "file":
            continue
        rel_path = _relative_env_path(root_dir, entry.path)
        if ignore_matcher.ignores(rel_path):
            continue
        result = await _load_skill_from_file(env, entry.path)
        if result.skill is not None:
            skills.append(result.skill)
        diagnostics.extend(result.diagnostics)
        return LoadSkillsResult(skills=skills, diagnostics=diagnostics)

    for entry in sorted(entries, key=lambda item: item.name):
        if entry.name.startswith(".") or entry.name == "node_modules":
            continue
        kind = await _resolve_kind(env, entry, diagnostics)
        if kind is None:
            continue

        rel_path = _relative_env_path(root_dir, entry.path)
        ignore_path = f"{rel_path}/" if kind == "directory" else rel_path
        if ignore_matcher.ignores(ignore_path):
            continue

        if kind == "directory":
            result = await _load_skills_from_dir_internal(
                env,
                entry.path,
                include_root_files=False,
                ignore_matcher=ignore_matcher,
                root_dir=root_dir,
            )
            skills.extend(result.skills)
            diagnostics.extend(result.diagnostics)
            continue

        if kind == "file" and include_root_files and entry.name.endswith(".md"):
            result = await _load_skill_from_file(env, entry.path)
            if result.skill is not None:
                skills.append(result.skill)
            diagnostics.extend(result.diagnostics)

    return LoadSkillsResult(skills=skills, diagnostics=diagnostics)


async def _add_ignore_rules(
    env: ExecutionEnv,
    ignore_matcher: _IgnoreMatcher,
    dir: str,
    root_dir: str,
    diagnostics: list[SkillDiagnostic],
) -> None:
    relative_dir = _relative_env_path(root_dir, dir)
    prefix = f"{relative_dir}/" if relative_dir else ""

    for filename in IGNORE_FILE_NAMES:
        ignore_path = _join_env_path(dir, filename)
        info = await env.fileInfo(ignore_path)
        if isinstance(info, Err):
            if info.error.code != "not_found":
                diagnostics.append(
                    SkillDiagnostic(
                        code="file_info_failed",
                        message=str(info.error),
                        path=ignore_path,
                    )
                )
            continue
        if info.value.kind != "file":
            continue
        content = await env.readTextFile(ignore_path)
        if isinstance(content, Err):
            diagnostics.append(
                SkillDiagnostic(
                    code="read_failed",
                    message=str(content.error),
                    path=ignore_path,
                )
            )
            continue
        patterns = [
            pattern
            for line in content.value.splitlines()
            if (pattern := _prefix_ignore_pattern(line, prefix)) is not None
        ]
        if patterns:
            ignore_matcher.add(patterns)


async def _load_skill_from_file(env: ExecutionEnv, file_path: str) -> _LoadSkillFromFileResult:
    diagnostics: list[SkillDiagnostic] = []
    raw_content = await env.readTextFile(file_path)
    if isinstance(raw_content, Err):
        diagnostics.append(
            SkillDiagnostic(
                code="read_failed",
                message=str(raw_content.error),
                path=file_path,
            )
        )
        return _LoadSkillFromFileResult(skill=None, diagnostics=diagnostics)

    parsed = _parse_frontmatter(raw_content.value)
    if isinstance(parsed, Err):
        diagnostics.append(
            SkillDiagnostic(
                code="parse_failed",
                message=str(parsed.error),
                path=file_path,
            )
        )
        return _LoadSkillFromFileResult(skill=None, diagnostics=diagnostics)

    frontmatter, body = parsed.value
    skill_dir = _dirname_env_path(file_path)
    parent_dir_name = _basename_env_path(skill_dir)
    description = frontmatter.description if isinstance(frontmatter.description, str) else None
    for error in _validate_description(description):
        diagnostics.append(SkillDiagnostic(code="invalid_metadata", message=error, path=file_path))

    frontmatter_name = frontmatter.name if isinstance(frontmatter.name, str) else None
    name = frontmatter_name or parent_dir_name
    for error in _validate_name(name, parent_dir_name):
        diagnostics.append(SkillDiagnostic(code="invalid_metadata", message=error, path=file_path))

    if description is None or not description.strip():
        return _LoadSkillFromFileResult(skill=None, diagnostics=diagnostics)

    return _LoadSkillFromFileResult(
        skill=Skill(
            name=name,
            description=description,
            content=body,
            filePath=file_path,
            disableModelInvocation=frontmatter.disable_model_invocation,
        ),
        diagnostics=diagnostics,
    )


def _validate_name(name: str, parent_dir_name: str) -> list[str]:
    errors: list[str] = []
    if name != parent_dir_name:
        errors.append(f'name "{name}" does not match parent directory "{parent_dir_name}"')
    if len(name) > MAX_NAME_LENGTH:
        errors.append(f"name exceeds {MAX_NAME_LENGTH} characters ({len(name)})")
    if re.fullmatch(r"[a-z0-9-]+", name) is None:
        errors.append("name contains invalid characters (must be lowercase a-z, 0-9, hyphens only)")
    if name.startswith("-") or name.endswith("-"):
        errors.append("name must not start or end with a hyphen")
    if "--" in name:
        errors.append("name must not contain consecutive hyphens")
    return errors


def _validate_description(description: str | None) -> list[str]:
    if description is None or not description.strip():
        return ["description is required"]
    if len(description) > MAX_DESCRIPTION_LENGTH:
        return [f"description exceeds {MAX_DESCRIPTION_LENGTH} characters ({len(description)})"]
    return []


def _parse_frontmatter(content: str) -> Result[tuple[_SkillFrontmatter, str], Exception]:
    try:
        normalized = content.replace("\r\n", "\n").replace("\r", "\n")
        if not normalized.startswith("---"):
            return ok((_SkillFrontmatter(), normalized))
        end_index = normalized.find("\n---", 3)
        if end_index == -1:
            return ok((_SkillFrontmatter(), normalized))
        yaml_string = normalized[4:end_index]
        body = normalized[end_index + 4 :].strip()
        data = _yaml_load(yaml_string)
        frontmatter = data if isinstance(data, Mapping) else {}
        name = frontmatter.get("name")
        description = frontmatter.get("description")
        return ok(
            (
                _SkillFrontmatter(
                    name=name if isinstance(name, str) else None,
                    description=description if isinstance(description, str) else None,
                    disable_model_invocation=frontmatter.get("disable-model-invocation") is True,
                ),
                body,
            )
        )
    except Exception as error:
        return err(to_error(error))


async def _resolve_kind(
    env: ExecutionEnv,
    info: FileInfo,
    diagnostics: list[SkillDiagnostic],
) -> Literal["file", "directory"] | None:
    if info.kind in {"file", "directory"}:
        return info.kind
    canonical_path = await env.canonicalPath(info.path)
    if isinstance(canonical_path, Err):
        if canonical_path.error.code != "not_found":
            diagnostics.append(
                SkillDiagnostic(
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
                SkillDiagnostic(
                    code="file_info_failed",
                    message=str(target.error),
                    path=info.path,
                )
            )
        return None
    if target.value.kind in {"file", "directory"}:
        return target.value.kind
    return None


def _prefix_ignore_pattern(line: str, prefix: str) -> str | None:
    trimmed = line.strip()
    if not trimmed:
        return None
    if trimmed.startswith("#") and not trimmed.startswith("\\#"):
        return None

    pattern = line
    negated = False
    if pattern.startswith("!"):
        negated = True
        pattern = pattern[1:]
    elif pattern.startswith("\\!"):
        pattern = pattern[1:]
    if pattern.startswith("/"):
        pattern = pattern[1:]
    prefixed = f"{prefix}{pattern}" if prefix else pattern
    return f"!{prefixed}" if negated else prefixed


def _join_env_path(base: str, child: str) -> str:
    return f"{base.rstrip('/')}/{child.lstrip('/')}"


def _dirname_env_path(path: str) -> str:
    normalized = path.rstrip("/")
    slash_index = normalized.rfind("/")
    if slash_index <= 0:
        return "/"
    return normalized[:slash_index]


def _basename_env_path(path: str) -> str:
    normalized = path.rstrip("/")
    slash_index = normalized.rfind("/")
    if slash_index == -1:
        return normalized
    return normalized[slash_index + 1 :]


def _relative_env_path(root: str, path: str) -> str:
    normalized_root = root.rstrip("/")
    normalized_path = path.rstrip("/")
    if normalized_path == normalized_root:
        return ""
    prefix = f"{normalized_root}/"
    if normalized_path.startswith(prefix):
        return normalized_path[len(prefix) :]
    return normalized_path.lstrip("/")


def _yaml_load(content: str) -> Any:
    yaml = YAML(typ="safe")
    return yaml.load(content)


formatSkillInvocation = format_skill_invocation
loadSkills = load_skills
loadSourcedSkills = load_sourced_skills

__all__ = [
    "IGNORE_FILE_NAMES",
    "LoadSkillsResult",
    "LoadSourcedSkillsResult",
    "MAX_DESCRIPTION_LENGTH",
    "MAX_NAME_LENGTH",
    "SkillDiagnostic",
    "SkillDiagnosticCode",
    "SourcedSkill",
    "SourcedSkillDiagnostic",
    "formatSkillInvocation",
    "format_skill_invocation",
    "loadSkills",
    "loadSourcedSkills",
    "load_skills",
    "load_sourced_skills",
]

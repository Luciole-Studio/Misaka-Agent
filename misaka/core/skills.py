"""Skill discovery, validation, and prompt formatting helpers."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from pathspec import GitIgnoreSpec

from misaka.config import CONFIG_DIR_NAME as _CONFIG_DIR_NAME
from misaka.config import get_agent_dir
from misaka.core.diagnostics import ResourceCollision, ResourceDiagnostic
from misaka.core.source_info import SourceInfo, create_synthetic_source_info
from misaka.utils.frontmatter import parse_frontmatter
from misaka.utils.paths import canonicalize_path, resolve_path

_MAX_NAME_LENGTH = 64
_MAX_DESCRIPTION_LENGTH = 1024
_IGNORE_FILE_NAMES = [".gitignore", ".ignore", ".fdignore"]
_VALID_SKILL_NAME_RE = re.compile(r"^[a-z0-9-]+$")


@dataclass(slots=True)
class Skill:
    name: str
    description: str
    filePath: str
    baseDir: str
    sourceInfo: SourceInfo
    disableModelInvocation: bool


@dataclass(slots=True)
class LoadSkillsResult:
    skills: list[Skill]
    diagnostics: list[ResourceDiagnostic]


class LoadSkillsFromDirOptions(TypedDict):
    dir: str
    source: str


class LoadSkillsOptions(TypedDict):
    cwd: str
    agentDir: str
    skillPaths: list[str]
    includeDefaults: bool


SkillFrontmatter = TypedDict(
    "SkillFrontmatter",
    {
        "name": str,
        "description": str,
        "disable-model-invocation": bool,
    },
    total=False,
)


class _IgnoreMatcher:
    def __init__(self) -> None:
        self._patterns: list[str] = []

    def add(self, patterns: list[str]) -> None:
        self._patterns.extend(patterns)

    def ignores(self, path: str) -> bool:
        if not self._patterns:
            return False
        return GitIgnoreSpec.from_lines(self._patterns).match_file(path)


def load_skills_from_dir(options: LoadSkillsFromDirOptions) -> LoadSkillsResult:
    return _load_skills_from_dir_internal(options["dir"], options["source"], True)


def load_skills(options: LoadSkillsOptions) -> LoadSkillsResult:
    resolved_cwd = resolve_path(options["cwd"])
    agent_dir = options.get("agentDir")
    resolved_agent_dir = resolve_path(get_agent_dir() if agent_dir is None else agent_dir)
    include_defaults = options["includeDefaults"]
    skill_paths = options["skillPaths"]

    skill_map: dict[str, Skill] = {}
    real_path_set: set[str] = set()
    diagnostics: list[ResourceDiagnostic] = []
    collision_diagnostics: list[ResourceDiagnostic] = []

    def add_skills(result: LoadSkillsResult) -> None:
        diagnostics.extend(result.diagnostics)
        for skill in result.skills:
            real_path = canonicalize_path(skill.filePath)
            if real_path in real_path_set:
                continue
            existing = skill_map.get(skill.name)
            if existing is not None:
                collision_diagnostics.append(
                    ResourceDiagnostic(
                        type="collision",
                        message=f'name "{skill.name}" collision',
                        path=skill.filePath,
                        collision=ResourceCollision(
                            resourceType="skill",
                            name=skill.name,
                            winnerPath=existing.filePath,
                            loserPath=skill.filePath,
                        ),
                    )
                )
                continue
            skill_map[skill.name] = skill
            real_path_set.add(real_path)

    user_skills_dir = os.path.join(resolved_agent_dir, "skills")
    project_skills_dir = os.path.join(resolved_cwd, _CONFIG_DIR_NAME, "skills")

    if include_defaults:
        add_skills(_load_skills_from_dir_internal(user_skills_dir, "user", True))
        add_skills(_load_skills_from_dir_internal(project_skills_dir, "project", True))

    def is_under_path(target: str, root: str) -> bool:
        normalized_root = os.path.abspath(root)
        normalized_target = os.path.abspath(target)
        if normalized_target == normalized_root:
            return True
        prefix = normalized_root if normalized_root.endswith(os.sep) else f"{normalized_root}{os.sep}"
        return normalized_target.startswith(prefix)

    def get_source(resolved_path: str) -> str:
        if not include_defaults:
            if is_under_path(resolved_path, user_skills_dir):
                return "user"
            if is_under_path(resolved_path, project_skills_dir):
                return "project"
        return "path"

    for raw_path in skill_paths:
        resolved = resolve_path(raw_path, resolved_cwd, trim=True)
        if not os.path.exists(resolved):
            diagnostics.append(ResourceDiagnostic(type="warning", message="skill path does not exist", path=resolved))
            continue
        try:
            source = get_source(resolved)
            if os.path.isdir(resolved):
                add_skills(_load_skills_from_dir_internal(resolved, source, True))
            elif os.path.isfile(resolved) and resolved.endswith(".md"):
                result = _load_skill_from_file(resolved, source)
                if result.skill is not None:
                    add_skills(LoadSkillsResult(skills=[result.skill], diagnostics=result.diagnostics))
                else:
                    diagnostics.extend(result.diagnostics)
            else:
                diagnostics.append(
                    ResourceDiagnostic(
                        type="warning",
                        message="skill path is not a markdown file",
                        path=resolved,
                    )
                )
        except Exception as error:  # noqa: BLE001
            diagnostics.append(ResourceDiagnostic(type="warning", message=str(error), path=resolved))

    return LoadSkillsResult(skills=list(skill_map.values()), diagnostics=[*diagnostics, *collision_diagnostics])


# 系统提示技能索引里的 description 上限（hermes SKILL_PROMPT_DESC_LIMIT 同值）。
# 索引每会话常驻，超出部分截断——learn_prompt 的 HARDLINE「≤60 字符」正是据此，
# 此前 misaka 抄了纪律没抄机制，那条要求一直悬空（2026-08-20 用户裁定补齐）。
SKILL_PROMPT_DESC_LIMIT = 60


def truncate_skill_description(description: str) -> str:
    """索引用 description：超限截断加省略号（hermes extract_skill_description 同款）。"""
    desc = str(description or "").strip().strip("'\"")
    if len(desc) > SKILL_PROMPT_DESC_LIMIT:
        return desc[: SKILL_PROMPT_DESC_LIMIT - 3] + "..."
    return desc


def is_skill_description_truncated(description: str) -> bool:
    """这条 description 会不会在系统提示索引里被截断（给 linter/`/learn` 用）。"""
    return len(str(description or "").strip().strip("'\"")) > SKILL_PROMPT_DESC_LIMIT


def format_skills_for_prompt(skills: list[Skill]) -> str:
    visible_skills = [skill for skill in skills if not skill.disableModelInvocation]
    if not visible_skills:
        return ""
    lines = [
        "",
        "",
        "The following skills provide specialized instructions for specific tasks.",
        "Use the read tool to load a skill's file when the task matches its description.",
        (
            "When a skill file references a relative path, resolve it against the skill directory "
            "(parent of SKILL.md / dirname of the path) and use that absolute path in tool commands."
        ),
        "",
        "<available_skills>",
    ]
    for skill in visible_skills:
        lines.append("  <skill>")
        lines.append(f"    <name>{_escape_xml(skill.name)}</name>")
        lines.append(
            f"    <description>{_escape_xml(truncate_skill_description(skill.description))}</description>")
        lines.append(f"    <location>{_escape_xml(skill.filePath)}</location>")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    return "\n".join(lines)


def _load_skills_from_dir_internal(
    dir_path: str,
    source: str,
    include_root_files: bool,
    ignore_matcher: _IgnoreMatcher | None = None,
    root_dir: str | None = None,
) -> LoadSkillsResult:
    skills: list[Skill] = []
    diagnostics: list[ResourceDiagnostic] = []
    if not os.path.isdir(dir_path):
        return LoadSkillsResult(skills=skills, diagnostics=diagnostics)

    root = root_dir or dir_path
    matcher = ignore_matcher or _IgnoreMatcher()
    _add_ignore_rules(matcher, dir_path, root)

    try:
        entries = list(Path(dir_path).iterdir())
    except OSError:
        return LoadSkillsResult(skills=skills, diagnostics=diagnostics)

    for entry in entries:
        if entry.name != "SKILL.md":
            continue
        entry_path = str(entry)
        if not _is_file(entry):
            continue
        rel_path = _to_posix_path(os.path.relpath(entry_path, root))
        if matcher.ignores(rel_path):
            continue
        result = _load_skill_from_file(entry_path, source)
        if result.skill is not None:
            skills.append(result.skill)
        diagnostics.extend(result.diagnostics)
        return LoadSkillsResult(skills=skills, diagnostics=diagnostics)

    for entry in entries:
        if entry.name.startswith(".") or entry.name == "node_modules":
            continue
        entry_path = str(entry)
        is_directory = _is_dir(entry)
        is_file = _is_file(entry)
        rel_path = _to_posix_path(os.path.relpath(entry_path, root))
        ignore_path = f"{rel_path}/" if is_directory else rel_path
        if matcher.ignores(ignore_path):
            continue
        if is_directory:
            result = _load_skills_from_dir_internal(entry_path, source, False, matcher, root)
            skills.extend(result.skills)
            diagnostics.extend(result.diagnostics)
            continue
        if is_file and include_root_files and entry.name.endswith(".md"):
            result = _load_skill_from_file(entry_path, source)
            if result.skill is not None:
                skills.append(result.skill)
            diagnostics.extend(result.diagnostics)

    return LoadSkillsResult(skills=skills, diagnostics=diagnostics)


@dataclass(slots=True)
class _LoadSkillFromFileResult:
    skill: Skill | None
    diagnostics: list[ResourceDiagnostic]


def _load_skill_from_file(file_path: str, source: str) -> _LoadSkillFromFileResult:
    diagnostics: list[ResourceDiagnostic] = []
    # 上游 #7805/8c2529dae：只有 SKILL.md 是「声明的技能」；技能目录里的 README.md
    # 等普通根 .md 缺 frontmatter 不是坏技能——静默跳过，不出诊断
    declared = os.path.basename(file_path) == "SKILL.md"
    try:
        raw_content = Path(file_path).read_text(encoding="utf-8-sig")
        frontmatter = parse_frontmatter(raw_content).frontmatter
        skill_dir = os.path.dirname(file_path)
        parent_dir_name = os.path.basename(skill_dir)
        description = frontmatter.get("description")
        if not declared and (not isinstance(description, str) or not description.strip()):
            return _LoadSkillFromFileResult(skill=None, diagnostics=[])
        for error in _validate_description(description):
            diagnostics.append(ResourceDiagnostic(type="warning", message=error, path=file_path))

        name = frontmatter.get("name") or parent_dir_name
        for error in _validate_name(name):
            diagnostics.append(ResourceDiagnostic(type="warning", message=error, path=file_path))

        if not description or description.strip() == "":
            return _LoadSkillFromFileResult(skill=None, diagnostics=diagnostics)

        return _LoadSkillFromFileResult(
            skill=Skill(
                name=name,
                description=description,
                filePath=file_path,
                baseDir=skill_dir,
                sourceInfo=_create_skill_source_info(file_path, skill_dir, source),
                disableModelInvocation=frontmatter.get("disable-model-invocation") is True,
            ),
            diagnostics=diagnostics,
        )
    except Exception as error:
        if not declared:   # 普通 .md 解析不动也不算坏技能（#7805）
            return _LoadSkillFromFileResult(skill=None, diagnostics=[])
        diagnostics.append(ResourceDiagnostic(type="warning", message=str(error), path=file_path))
        return _LoadSkillFromFileResult(skill=None, diagnostics=diagnostics)


def _validate_name(name: Any) -> list[str]:
    errors: list[str] = []
    if len(name) > _MAX_NAME_LENGTH:
        errors.append(f"name exceeds {_MAX_NAME_LENGTH} characters ({len(name)})")
    if not _VALID_SKILL_NAME_RE.fullmatch(name):
        errors.append("name contains invalid characters (must be lowercase a-z, 0-9, hyphens only)")
    if name.startswith("-") or name.endswith("-"):
        errors.append("name must not start or end with a hyphen")
    if "--" in name:
        errors.append("name must not contain consecutive hyphens")
    return errors


def _validate_description(description: Any) -> list[str]:
    errors: list[str] = []
    if not description or description.strip() == "":
        errors.append("description is required")
    elif len(description) > _MAX_DESCRIPTION_LENGTH:
        errors.append(f"description exceeds {_MAX_DESCRIPTION_LENGTH} characters ({len(description)})")
    return errors


def _create_skill_source_info(file_path: str, base_dir: str, source: str) -> SourceInfo:
    if source == "user":
        return create_synthetic_source_info(file_path, {"source": "local", "scope": "user", "baseDir": base_dir})
    if source == "project":
        return create_synthetic_source_info(file_path, {"source": "local", "scope": "project", "baseDir": base_dir})
    if source == "path":
        return create_synthetic_source_info(file_path, {"source": "local", "baseDir": base_dir})
    return create_synthetic_source_info(file_path, {"source": source, "baseDir": base_dir})


def _add_ignore_rules(matcher: _IgnoreMatcher, dir_path: str, root_dir: str) -> None:
    relative_dir = os.path.relpath(dir_path, root_dir)
    prefix = f"{_to_posix_path(relative_dir)}/" if relative_dir != "." else ""
    for filename in _IGNORE_FILE_NAMES:
        ignore_path = os.path.join(dir_path, filename)
        if not os.path.exists(ignore_path):
            continue
        try:
            content = Path(ignore_path).read_text(encoding="utf-8")
        except OSError:
            continue
        patterns = [
            prefixed
            for line in content.splitlines()
            if (prefixed := _prefix_ignore_pattern(line, prefix)) is not None
        ]
        if patterns:
            matcher.add(patterns)


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


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _to_posix_path(value: str) -> str:
    return value.replace(os.sep, "/")


def _escape_xml(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


formatSkillsForPrompt = format_skills_for_prompt
loadSkills = load_skills
loadSkillsFromDir = load_skills_from_dir

__all__ = [
    "SkillFrontmatter",
    "Skill",
    "LoadSkillsResult",
    "LoadSkillsFromDirOptions",
    "loadSkillsFromDir",
    "formatSkillsForPrompt",
    "LoadSkillsOptions",
    "loadSkills",
]

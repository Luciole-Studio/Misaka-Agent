"""Validated entry point for creating and modifying skills."""
import contextvars
import os
import shutil
from pathlib import Path

from misaka.skills import write as skill_write

MAX_SKILL_CONTENT_CHARS = 40_000
MAX_DESCRIPTION_LENGTH = 1024
_VALID_NAME = __import__("re").compile(r"^[a-z0-9][a-z0-9_-]*$")

_bypass = contextvars.ContextVar("misaka_skill_gate_bypass", default=False)


def lookup_path_error(name):
    """Return an error message if ``name`` could escape the skills root as a path, else None."""
    from pathlib import PurePosixPath, PureWindowsPath
    if not isinstance(name, str):
        return "Skill name must be a string."
    value = name.strip()
    if not value:
        return "Skill name cannot be empty."
    if PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute() \
            or PureWindowsPath(value).drive:
        return f"Skill name cannot be an absolute path: {value}"
    if ".." in PurePosixPath(value).parts or ".." in PureWindowsPath(value).parts:
        return f"Skill name cannot contain `..`: {value}"
    return None



def _skills_root(profile_dir):
    return Path(profile_dir) / "skills"


def validate_frontmatter(content, *, new_skill=False):
    """Return an error message if the SKILL.md frontmatter or body is invalid, else None."""
    from misaka.core.skills import SKILL_PROMPT_DESC_LIMIT
    from misaka.utils.frontmatter import parse_frontmatter

    if not str(content or "").strip():
        return "Content cannot be empty."
    text = str(content).lstrip("﻿")
    if not text.startswith("---"):
        return "SKILL.md must start with YAML frontmatter (`---`)."
    parsed = parse_frontmatter(text)
    fm = parsed.frontmatter
    if not isinstance(fm, dict) or not fm:
        return "Frontmatter is unclosed or is not a key-value mapping."
    if "name" not in fm:
        return "Frontmatter must contain a `name` field."
    if "description" not in fm:
        return "Frontmatter must contain a `description` field."
    desc = str(fm["description"]).strip().strip("'\"")
    if len(desc) > MAX_DESCRIPTION_LENGTH:
        return f"Description exceeds {MAX_DESCRIPTION_LENGTH} characters."
    if new_skill and len(desc) > SKILL_PROMPT_DESC_LIMIT:
        return (
            f"description is {len(desc)} characters; new skills must fit the "
            f"{SKILL_PROMPT_DESC_LIMIT}-character prompt index budget. Use one sentence, "
            "put trigger conditions first, and move details into the body."
        )
    if not (parsed.body or "").strip():
        return "SKILL.md must contain a body after frontmatter."
    return None


def validate_content_size(content, label="SKILL.md"):
    if len(str(content or "")) > MAX_SKILL_CONTENT_CHARS:
        return (
            f"{label} is {len(content):,} characters; the upper limit is {MAX_SKILL_CONTENT_CHARS:,}. "
            "Keep SKILL.md concise and move detail into references/."
        )
    return None


def _security_scan(skill_dir):
    """Run the skill security scan; return a blocking message, or None when allowed or the scanner fails."""
    try:
        from misaka.skills.guard import (
            format_scan_report, scan_skill, should_allow_install)
        result = scan_skill(Path(skill_dir), source="agent-created")
        allowed, reason = should_allow_install(result)
        if allowed is False or allowed is None:
            return f"""The security scan blocked this skill ({reason}):
{format_scan_report(result)}"""
    except Exception:  # noqa: BLE001 - scanner failure is fail-open, matching load behavior
        return None
    return None


def _lint_findings(skill_md):
    """Lint the skill containing ``skill_md``; return findings as plain dicts, or [] on scanner failure."""
    try:
        from misaka.skills.linter import lint_skill
        found = lint_skill(Path(skill_md).parent)
    except Exception:  # noqa: BLE001
        return []
    return [{"severity": f.severity, "rule": f.rule, "message": f.message} for f in found]


def _description_preview(content):
    """Return the description exactly as the prompt index will display it."""
    from misaka.core.skills import is_skill_description_truncated, truncate_skill_description
    from misaka.utils.frontmatter import parse_frontmatter
    desc = str((parse_frontmatter(content).frontmatter or {}).get("description") or "")
    if not is_skill_description_truncated(desc):
        return None
    return f"The skill index will show: {truncate_skill_description(desc)}"


def _invalidate_index():
    """Drop the cached skill index so the next prompt build sees the change."""
    try:
        from misaka.core import skills as _skills
        invalidate = getattr(_skills, "invalidate_skills_cache", None)
        if callable(invalidate):
            invalidate()
    except Exception:  # noqa: BLE001
        pass


def _create(profile_dir, name, content):
    err = None if _VALID_NAME.fullmatch(name) else \
        f"Invalid skill name '{name}'; use lowercase letters, numbers, underscores, and hyphens."
    err = err or validate_frontmatter(content, new_skill=True)
    err = err or validate_content_size(content)
    if err:
        return {"success": False, "error": err}

    skill_dir = _skills_root(profile_dir) / name
    if skill_dir.exists():
        return {"success": False, "error": f"Skill {name!r} already exists: {skill_dir}"}

    skill_dir.mkdir(parents=True, exist_ok=True)
    md = skill_dir / "SKILL.md"
    tmp = md.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, md)
    os.chmod(md, 0o644)

    scan_error = _security_scan(skill_dir)
    if scan_error:
        shutil.rmtree(skill_dir, ignore_errors=True)
        return {"success": False, "error": scan_error}

    result = {"success": True, "message": f"Skill '{name}' created.",
              "skill_md": str(md), "path": str(skill_dir)}
    findings = _lint_findings(md)
    if findings:
        result["lint_warnings"] = findings
        result["lint_hint"] = (
            "The skill was created with advisory lint warnings. Update SKILL.md or use "
            "skill_manage(action='write_file') to address them."
        )
    preview = _description_preview(content)
    if preview:
        result["description_preview"] = preview
    result["hint"] = (
        "To add references, templates, or scripts, call "
        f"skill_manage(action='write_file', name='{name}', "
        "file_path='references/examples.md', file_content='...')."
    )
    return result


MAX_SKILL_FILE_BYTES = 1024 * 1024


def _resolve_target(skill_dir, file_path):
    """Resolve a support-file path within a skill directory."""
    err = lookup_path_error(file_path)
    if err:
        return None, err
    target = (Path(skill_dir) / file_path).resolve()
    try:
        target.relative_to(Path(skill_dir).resolve())
    except ValueError:
        return None, f"Path escapes the skill directory: {file_path}"
    return target, None


def _require_skill(profile_dir, name):
    """Return ``(skill_dir, None)`` for an existing skill, or ``(None, error)``."""
    skill_dir = _skills_root(profile_dir) / name
    if not (skill_dir / "SKILL.md").is_file():
        return None, f"Skill '{name}' does not exist in this role."
    return skill_dir, None


def _atomic_write(target, text):
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)


def _write_file(profile_dir, name, file_path, file_content):
    if file_content is None:
        return {"success": False, "error": "write_file requires file_content; pass an empty string for an empty file"}
    if len(file_content.encode("utf-8")) > MAX_SKILL_FILE_BYTES:
        return {"success": False,
                "error": f"Support file exceeds {MAX_SKILL_FILE_BYTES:,} bytes (1 MiB)."}
    err = validate_content_size(file_content, label=file_path)
    if err:
        return {"success": False, "error": err}
    skill_dir, err = _require_skill(profile_dir, name)
    if err:
        return {"success": False, "error": err}
    target, err = _resolve_target(skill_dir, file_path)
    if err:
        return {"success": False, "error": err}
    if target.name == "SKILL.md":
        return {"success": False, "error": "Use create or edit for SKILL.md so frontmatter is validated."}

    target.parent.mkdir(parents=True, exist_ok=True)
    original = target.read_text(encoding="utf-8") if target.exists() else None
    _atomic_write(target, file_content)

    scan_error = _security_scan(skill_dir)
    if scan_error:
        if original is not None:
            _atomic_write(target, original)
        else:
            target.unlink(missing_ok=True)
        return {"success": False, "error": scan_error}
    return {"success": True, "message": f"Wrote {name}/{file_path}.", "path": str(target)}


def _edit_skill(profile_dir, name, content):
    """Replace SKILL.md wholesale, rolling back if the security scan rejects the result."""
    err = validate_frontmatter(content) or validate_content_size(content)
    if err:
        return {"success": False, "error": err}
    skill_dir, err = _require_skill(profile_dir, name)
    if err:
        return {"success": False, "error": err}
    md = skill_dir / "SKILL.md"
    original = md.read_text(encoding="utf-8")
    _atomic_write(md, content)
    scan_error = _security_scan(skill_dir)
    if scan_error:
        _atomic_write(md, original)
        return {"success": False, "error": scan_error}
    result = {"success": True, "message": f"Skill '{name}' replaced.", "path": str(skill_dir)}
    preview = _description_preview(content)
    if preview:
        result["description_preview"] = preview
    return result


def _patch_skill(profile_dir, name, old_string, new_string, file_path=None,
                 replace_all=False):
    """Replace ``old_string`` with ``new_string`` in SKILL.md or a support file."""
    if not old_string:
        return {"success": False, "error": "patch requires old_string"}
    if new_string is None:
        return {"success": False, "error": "patch requires new_string; use an empty string to remove the match"}
    skill_dir, err = _require_skill(profile_dir, name)
    if err:
        return {"success": False, "error": err}
    if file_path:
        target, err = _resolve_target(skill_dir, file_path)
        if err:
            return {"success": False, "error": err}
    else:
        target = skill_dir / "SKILL.md"
    if not target.exists():
        return {"success": False, "error": f"File does not exist: {file_path or 'SKILL.md'}"}

    content = target.read_text(encoding="utf-8")
    count = content.count(old_string)
    if count == 0:
        return {"success": False,
                "error": "old_string did not match; whitespace and indentation must match exactly",
                "file_preview": content[:500] + ("..." if len(content) > 500 else "")}
    if count > 1 and not replace_all:
        return {"success": False,
                "error": f"old_string matched {count} locations; make it unique or set replace_all=true"}
    new_content = content.replace(old_string, new_string) if replace_all \
        else content.replace(old_string, new_string, 1)

    label = file_path or "SKILL.md"
    err = validate_content_size(new_content, label=label)
    if err:
        return {"success": False, "error": err}
    if not file_path:
        err = validate_frontmatter(new_content)
        if err:
            return {"success": False, "error": f"This patch would break SKILL.md structure: {err}"}

    _atomic_write(target, new_content)
    scan_error = _security_scan(skill_dir)
    if scan_error:
        _atomic_write(target, content)
        return {"success": False, "error": scan_error}
    n = count if replace_all else 1
    return {"success": True, "message": f"Patched {label} ({n} replacement{'s' if n != 1 else ''})."}


def _delete_skill(profile_dir, name, absorbed_into=None):
    """Delete a whole skill directory, optionally noting which skill absorbed its content."""
    skill_dir, err = _require_skill(profile_dir, name)
    if err:
        return {"success": False, "error": err}
    absorbed_target = (absorbed_into or "").strip()
    if absorbed_target:
        if absorbed_target == name:
            return {"success": False, "error": "absorbed_into cannot name the skill being deleted."}
        umbrella = _skills_root(profile_dir) / absorbed_target
        if not (umbrella / "SKILL.md").is_file():
            return {"success": False,
                    "error": f"Absorbing skill '{absorbed_target}' does not exist; create or update it before deletion."}
    root = _skills_root(profile_dir).resolve()
    resolved = skill_dir.resolve()
    if resolved == root or root not in resolved.parents:
        return {"success": False, "error": "Deletion target is outside this role's skill directory."}

    shutil.rmtree(skill_dir)
    message = f"Skill {name!r} deleted."
    if absorbed_target:
        message += f" Content was merged into {absorbed_target!r}."
    return {"success": True, "message": message}


def _remove_file(profile_dir, name, file_path):
    """Delete a support file and remove its empty parent directory."""
    skill_dir, err = _require_skill(profile_dir, name)
    if err:
        return {"success": False, "error": err}
    target, err = _resolve_target(skill_dir, file_path)
    if err:
        return {"success": False, "error": err}
    if target.name == "SKILL.md":
        return {"success": False, "error": "remove_file cannot delete SKILL.md; use delete for the whole skill."}
    if not target.exists():
        available = [str(f.relative_to(skill_dir))
                     for sub in ("references", "templates", "assets", "scripts")
                     if (skill_dir / sub).exists()
                     for f in sorted((skill_dir / sub).rglob("*")) if f.is_file()]
        return {"success": False,
                "error": f"Skill '{name}' has no file at {file_path}.",
                "available_files": available or None}
    target.unlink()
    parent = target.parent
    if parent != skill_dir and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()
    return {"success": True, "message": f"Deleted {file_path} from '{name}'."}


_ACTIONS = ("create", "edit", "patch", "delete", "write_file", "remove_file")


def _gist(action, name, content="", file_path="", old_string=""):
    """Build a concise pending-review summary."""
    if action == "write_file":
        return f"Add {file_path} to skill '{name}'"
    if action == "remove_file":
        return f"Remove {file_path} from skill '{name}'"
    if action == "patch":
        return f"patch skill {name}{('/' + file_path) if file_path else ''}: {old_string[:40]}…"
    if action == "delete":
        return f"Delete skill '{name}'"
    from misaka.utils.frontmatter import parse_frontmatter
    desc = str((parse_frontmatter(content or "").frontmatter or {}).get("description") or "")
    label = "Rewrite skill" if action == "edit" else "Create skill"
    return f"{label} '{name}': {desc[:60]}" if desc else f"{label} '{name}'"


def manage(action, name, *, profile_dir, content=None, file_path=None,
           file_content=None, old_string=None, new_string=None,
           replace_all=False, absorbed_into=None):
    """Apply one validated skill mutation through the write gate and ledger."""
    if action not in _ACTIONS:
        return {"success": False,
                "error": f"Unknown action {action!r}. Available: {', '.join(_ACTIONS)}"}

    if not _bypass.get():
        decision, note = skill_write.evaluate_gate()
        if decision == "off":
            return {"success": False, "error": note}
        if decision == "stage":
            payload = {"action": action, "name": name, "profile_dir": profile_dir,
                       "content": content, "file_path": file_path,
                       "file_content": file_content, "old_string": old_string,
                       "new_string": new_string, "replace_all": replace_all,
                       "absorbed_into": absorbed_into}
            gist = _gist(action, name, content or "", file_path or "", old_string or "")
            record = skill_write.stage(payload, summary=gist)
            return {"success": True, "staged": True, "pending_id": record["id"],
                    "gist": gist, "message": note}

    skill_dir = _skills_root(profile_dir) / name
    before = skill_write.snapshot(skill_dir)

    if action == "create":
        result = _create(profile_dir, name, content or "")
    elif action == "edit":
        result = _edit_skill(profile_dir, name, content or "")
    elif action == "patch":
        result = _patch_skill(profile_dir, name, old_string or "", new_string,
                              file_path=file_path, replace_all=replace_all)
    elif action == "delete":
        result = _delete_skill(profile_dir, name, absorbed_into=absorbed_into)
    elif action == "remove_file":
        result = _remove_file(profile_dir, name, file_path or "")
    else:
        result = _write_file(profile_dir, name, file_path or "", file_content)

    if result.get("success"):
        evidence = {k: v for k, v in (("file_path", file_path),
                                      ("absorbed_into", absorbed_into)) if v is not None}
        skill_write.record(action, name, before=before, after_root=skill_dir,
                           evidence=evidence)
        _invalidate_index()
    return result


def apply_pending(payload):
    """Apply a user-approved pending write, bypassing the gate it already passed."""
    token = _bypass.set(True)
    try:
        return manage(payload.get("action", ""), payload.get("name", ""),
                      profile_dir=payload.get("profile_dir", ""),
                      content=payload.get("content"),
                      file_path=payload.get("file_path"),
                      file_content=payload.get("file_content"),
                      old_string=payload.get("old_string"),
                      new_string=payload.get("new_string"),
                      replace_all=bool(payload.get("replace_all")),
                      absorbed_into=payload.get("absorbed_into"))
    finally:
        _bypass.reset(token)

"""Validated entry point for creating and modifying skills."""
import contextvars
import os
import shutil
from pathlib import Path

from misaka.skills import write as skill_write
from misaka.skills.linter import NAME_RE
from misaka.utils import atomic

MAX_SKILL_CONTENT_CHARS = 40_000
MAX_DESCRIPTION_LENGTH = 1024

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


def _skill_dir(profile_dir, name):
    """The one way from a skill name to its directory: ``(skill_dir, None)`` or ``(None, error)``.
    The name must be a plain identifier and the directory, symlinks followed, must stay inside
    the role's skills root; every read and write path goes through here."""
    err = lookup_path_error(name)
    if err:
        return None, err
    if not NAME_RE.fullmatch(name) or len(name) > 64:
        return None, f"Invalid skill name '{name}'; use up to 64 lowercase letters, numbers, underscores, and hyphens."
    root = _skills_root(profile_dir)
    skill_dir = root / name
    try:
        resolved, root_resolved = skill_dir.resolve(), root.resolve()
        resolved.relative_to(root_resolved)
    except ValueError:
        return None, f"Skill '{name}' resolves outside this role's skill directory."
    except OSError as error:
        return None, f"Cannot resolve skill '{name}': {error}"
    return skill_dir, None


def _is_skill_md(target, skill_dir):
    """True when ``target`` is the skill's SKILL.md -- by file identity, so ``skill.md`` on a
    case-insensitive filesystem counts too."""
    main = Path(skill_dir) / "SKILL.md"
    try:
        if target.name.casefold() == "skill.md" and target.parent.resolve() == main.parent.resolve():
            return True
        return main.exists() and target.exists() and os.path.samefile(target, main)
    except OSError:
        return False


def validate_frontmatter(content, *, new_skill=False):
    """Return an error message if the SKILL.md is invalid, else None. The linter's error rules are
    the validator: what a write refuses is exactly what ``lint_skill`` would flag as an error,
    plus the size limits only a write enforces."""
    from misaka.skills.index import SKILL_PROMPT_DESC_LIMIT
    from misaka.skills.linter import lint_content
    from misaka.utils.frontmatter import parse_frontmatter

    if not str(content or "").strip():
        return "Content cannot be empty."
    text = str(content).lstrip("﻿")
    if not text.startswith("---"):
        return "SKILL.md must start with YAML frontmatter (`---`)."
    errors = [f.message for f in lint_content(text) if f.severity == "error"]
    if errors:
        return "; ".join(errors)
    parsed = parse_frontmatter(text)                     # the linter parsed it: valid, typed name and description
    desc = parsed.frontmatter["description"].strip().strip("'\"")
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


def name_mismatch(name, content):
    """The frontmatter ``name`` is the directory name: the index, ``/skill`` and ``skill_view``
    all address a skill by it, so the two must not drift apart."""
    from misaka.utils.frontmatter import parse_frontmatter
    declared = str((parse_frontmatter(str(content)).frontmatter or {}).get("name") or "").strip()
    if declared != name:
        return f"Frontmatter name '{declared}' must equal the skill directory name '{name}'."
    return None


def validate_content_size(content, label="SKILL.md"):
    if len(str(content or "")) > MAX_SKILL_CONTENT_CHARS:
        return (
            f"{label} is {len(content):,} characters; the upper limit is {MAX_SKILL_CONTENT_CHARS:,}. "
            "Keep SKILL.md concise and move detail into references/."
        )
    return None


def _security_scan(skill_dir):
    """Run the skill security scan; return a blocking message, or None when allowed. A scanner
    that cannot run blocks too: a change nobody scanned is not a scanned change."""
    try:
        from misaka.skills.guard import (
            format_scan_report,
            scan_skill,
            should_allow_install,
        )
        result = scan_skill(Path(skill_dir), source="agent-created", honor_ignore=False)   # never the skill's own ignore file
        allowed, reason = should_allow_install(result)
    except Exception as error:  # noqa: BLE001 - whatever failed inside the scanner, the answer is "not scanned"
        return f"The security scan could not run ({type(error).__name__}: {error}); the change was not applied."
    if allowed is False or allowed is None:
        return f"""The security scan blocked this skill ({reason}):
{format_scan_report(result)}"""
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
    from misaka.skills.index import (
        is_skill_description_truncated,
        truncate_skill_description,
    )
    from misaka.utils.frontmatter import parse_frontmatter
    desc = str((parse_frontmatter(content).frontmatter or {}).get("description") or "")
    if not is_skill_description_truncated(desc):
        return None
    return f"The skill index will show: {truncate_skill_description(desc)}"


def _invalidate_index():
    """Drop the cached skill index so the next prompt build sees the change."""
    from misaka.skills import index
    index.invalidate()


def _create(profile_dir, name, content):
    skill_dir, err = _skill_dir(profile_dir, name)
    err = err or validate_frontmatter(content, new_skill=True)
    err = err or name_mismatch(name, content) or validate_content_size(content)
    if err:
        return {"success": False, "error": err}

    if skill_dir.exists():
        return {"success": False, "error": f"Skill {name!r} already exists: {skill_dir}"}

    skill_dir.mkdir(parents=True, exist_ok=True)
    md = skill_dir / "SKILL.md"
    atomic.write_text(md, content, mode=0o644)

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


def _resolve_target(skill_dir, file_path):
    """Resolve a support-file path within a skill directory."""
    err = lookup_path_error(file_path)
    if err:
        return None, err
    from misaka.skills.guard import SKILL_IGNORE_FILENAMES
    if Path(file_path).name in SKILL_IGNORE_FILENAMES:
        return None, f"{file_path} controls what the security scanner sees; it is not written through skill_manage."
    target = (Path(skill_dir) / file_path).resolve()
    try:
        target.relative_to(Path(skill_dir).resolve())
    except ValueError:
        return None, f"Path escapes the skill directory: {file_path}"
    return target, None


def _support_file_error(text, label):
    """One limit for support files, the scanner's: what it would refuse to read is refused here."""
    from misaka.skills.guard import MAX_SINGLE_FILE_KB
    if len(text.encode("utf-8")) > MAX_SINGLE_FILE_KB * 1024:
        return f"{label} exceeds {MAX_SINGLE_FILE_KB} KB, the security scanner's single-file limit."
    return None


def _require_skill(profile_dir, name):
    """Return ``(skill_dir, None)`` for an existing skill, or ``(None, error)``."""
    skill_dir, err = _skill_dir(profile_dir, name)
    if err:
        return None, err
    if not (skill_dir / "SKILL.md").is_file():
        return None, f"Skill '{name}' does not exist in this role."
    return skill_dir, None


def _atomic_write(target, text):
    atomic.write_text(target, text)


def _write_file(profile_dir, name, file_path, file_content):
    if file_content is None:
        return {"success": False, "error": "write_file requires file_content; pass an empty string for an empty file"}
    err = _support_file_error(file_content, file_path)
    if err:
        return {"success": False, "error": err}
    skill_dir, err = _require_skill(profile_dir, name)
    if err:
        return {"success": False, "error": err}
    target, err = _resolve_target(skill_dir, file_path)
    if err:
        return {"success": False, "error": err}
    if _is_skill_md(target, skill_dir):
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
    err = validate_frontmatter(content) or name_mismatch(name, content) or validate_content_size(content)
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
    if not file_path or _is_skill_md(target, skill_dir):
        err = (validate_content_size(new_content) or validate_frontmatter(new_content)
               or name_mismatch(name, new_content))
        if err:
            return {"success": False, "error": f"This patch would break SKILL.md structure: {err}"}
    else:
        err = _support_file_error(new_content, label)
        if err:
            return {"success": False, "error": err}

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
        umbrella, err = _skill_dir(profile_dir, absorbed_target)
        if err or not (umbrella / "SKILL.md").is_file():
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
    if _is_skill_md(target, skill_dir):
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
    from misaka.utils.frontmatter import FrontmatterError, parse_frontmatter
    try:
        desc = str((parse_frontmatter(content or "").frontmatter or {}).get("description") or "")
    except FrontmatterError:
        desc = ""
    label = "Rewrite skill" if action == "edit" else "Create skill"
    return f"{label} '{name}': {desc[:60]}" if desc else f"{label} '{name}'"


def _precheck(action, skill_dir, name, content, file_path, file_content, old_string, new_string):
    """The pure checks, before the gate and the lock: a request that could never apply is refused
    now, not staged for the user to review and fail at approval."""
    if action in ("create", "edit"):
        return (validate_frontmatter(content, new_skill=action == "create") or name_mismatch(name, content)
                or validate_content_size(content))
    if action == "write_file":
        if file_content is None:
            return "write_file requires file_content; pass an empty string for an empty file"
        return _support_file_error(file_content, file_path) or _resolve_target(skill_dir, file_path)[1]
    if action == "patch":
        if not old_string:
            return "patch requires old_string"
        if new_string is None:
            return "patch requires new_string; use an empty string to remove the match"
        return _resolve_target(skill_dir, file_path)[1] if file_path else None
    if action == "remove_file":
        return _resolve_target(skill_dir, file_path)[1]
    return None


def manage(action, name, *, profile_dir, content=None, file_path=None,
           file_content=None, old_string=None, new_string=None,
           replace_all=False, absorbed_into=None, base=None):
    """Apply one validated skill mutation through the write gate, under the skill lock, into the
    ledger; ``base`` is the digest of the live tree an approved pending write was reviewed against."""
    if action not in _ACTIONS:
        return {"success": False,
                "error": f"Unknown action {action!r}. Available: {', '.join(_ACTIONS)}"}
    skill_dir, err = _skill_dir(profile_dir, name)
    err = err or _precheck(action, skill_dir, name, content or "", file_path or "",
                           file_content, old_string or "", new_string)
    if err:
        return {"success": False, "error": err}

    if not _bypass.get():
        decision, note = skill_write.evaluate_gate()
        if decision == "off":
            return {"success": False, "error": note}
        if decision == "stage":
            payload = {"action": action, "name": name, "profile_dir": profile_dir,
                       "content": content, "file_path": file_path,
                       "file_content": file_content, "old_string": old_string,
                       "new_string": new_string, "replace_all": replace_all,
                       "absorbed_into": absorbed_into,
                       "base": skill_write.digest(skill_dir)}       # what the reviewer will look at
            gist = _gist(action, name, content or "", file_path or "", old_string or "")
            try:
                record = skill_write.stage(payload, summary=gist)
            except OSError as error:
                return {"success": False,
                        "error": f"Could not stage the skill write for review: {error}"}
            return {"success": True, "staged": True, "pending_id": record["id"],
                    "gist": gist, "message": note}

    with skill_write.mutation_lock():
        if base is not None and skill_write.digest(skill_dir) != base:
            return {"success": False, "error": (f"Skill '{name}' changed after this write was reviewed; look at it "
                                                "again with `misaka skills pending` and stage it anew.")}
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
            try:
                skill_write.record(action, name, before=before, after_root=skill_dir, evidence=evidence)
            except OSError as error:                       # unrecorded is unapplied: the tree goes back
                skill_write.restore(skill_dir, before)
                result = {"success": False,
                          "error": f"The skill ledger could not be written ({error}); the change was rolled back."}
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
                      absorbed_into=payload.get("absorbed_into"),
                      base=payload.get("base"))
    finally:
        _bypass.reset(token)


def pending_diff(payload):
    """Unified diff a pending write would produce, resolving paths exactly as ``apply_pending`` will."""
    import difflib

    action = str(payload.get("action") or "")
    name = str(payload.get("name") or "")
    skill_dir, err = _skill_dir(payload.get("profile_dir") or "", name)
    if err:
        return f"(cannot preview: {err})"
    file_path = payload.get("file_path")

    def read(path):
        try:
            return path.read_text(encoding="utf-8") if path.is_file() else ""
        except UnicodeDecodeError:
            return None          # binary: previewed by name only
        except OSError:
            return ""

    def rel_name(path):
        try:
            return f"{name}/{path.resolve().relative_to(skill_dir.resolve())}"
        except ValueError:
            return f"{name}/{path.name}"

    def udiff(rel, old, new):
        if old is None or new is None:
            return f"(binary file: {rel})"
        return "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                              fromfile=f"live/{rel}", tofile=f"pending/{rel}", lineterm=""))

    if action == "delete":
        if not skill_dir.is_dir():
            return f"(skill {name!r} does not exist)"
        parts = [udiff(rel_name(path), read(path), "")
                 for path in sorted(p for p in skill_dir.rglob("*") if p.is_file())]
        return "\n".join(p for p in parts if p) or f"(skill {name!r} has no files)"

    if action in ("create", "edit") or (action == "patch" and not file_path):
        target = skill_dir / "SKILL.md"
    elif action in ("patch", "write_file", "remove_file"):
        target, err = _resolve_target(skill_dir, file_path or "")
        if err:
            return f"(cannot preview: {err})"
    else:
        return f"(no preview for action {action!r})"

    old = read(target)
    if action in ("create", "edit"):
        new = str(payload.get("content") or "")
    elif action == "write_file":
        new = str(payload.get("file_content") or "")
    elif action == "remove_file":
        new = ""
    else:
        old_string = str(payload.get("old_string") or "")
        new_string = str(payload.get("new_string") or "")
        if old is None:
            return f"(cannot preview: {rel_name(target)} is a binary file)"
        if not old_string or old_string not in old:
            return "(cannot preview: old_string does not match the live file)"
        new = old.replace(old_string, new_string) if payload.get("replace_all") \
            else old.replace(old_string, new_string, 1)
    return udiff(rel_name(target), old, new) or "(no changes)"

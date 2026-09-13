"""Validated entry point for creating and modifying skills."""
import contextvars
import copy
import logging
import os
import shutil
import tempfile
from pathlib import Path

from misaka.core.skills import write as skill_write
from misaka.core.skills.vendor.fuzzy_match import (
    format_no_match_hint,
    fuzzy_find_and_replace,
)
from misaka.core.skills.vendor.manager import (
    MAX_DESCRIPTION_LENGTH,
    SKILL_MANAGE_SCHEMA,
    _validate_category,
    _validate_content_size,
)
from misaka.core.skills.vendor.manager import (
    VALID_NAME_RE as NAME_RE,
)
from misaka.utils import atomic

logger = logging.getLogger(__name__)

_bypass = contextvars.ContextVar("misaka_skill_gate_bypass", default=False)
_VISIBLE_ROOTS_UNSET = object()


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
    parts = Path(name).parts
    org_mirror = parts[0] == '_org'
    checked_parts = parts[1:] if org_mirror else parts
    if any(not NAME_RE.fullmatch(part) or len(part) > 64 for part in checked_parts):
        return None, f"Invalid skill name '{name}'; use up to 64 lowercase letters, numbers, underscores, and hyphens."
    root = _skills_root(profile_dir)
    if org_mirror and (len(parts) < 3 or not (root / name / 'SKILL.md').is_file()):
        return None, "Organisation mirror writes require an existing, resolved Skill."
    skill_dir = root / name
    try:
        resolved, root_resolved = skill_dir.resolve(), root.resolve()
        resolved.relative_to(root_resolved)
    except ValueError:
        return None, f"Skill '{name}' resolves outside this role's skill directory."
    except OSError as error:
        return None, f"Cannot resolve skill '{name}': {error}"
    try:
        skill_write._safe_parents(skill_dir)
    except (OSError, ValueError) as error:
        return None, str(error)
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
    from misaka.core.skills.index import SKILL_PROMPT_DESC_LIMIT
    from misaka.core.skills.linter import lint_content
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
    # Last gate: the index reads SKILL.md with its own lenient parser, and what
    # this function accepts is worth nothing if that parser comes back empty --
    # the skill installs, then advertises no description and enforces no
    # `platforms:`, silently. The two share a frontmatter boundary now
    # (index.parse_skill_markdown), so this only catches a disagreement between
    # the two YAML loaders; it is cheap, and it fails at write time where the
    # author can still see why.
    from misaka.core.skills.index import parse_skill_markdown
    indexed = parse_skill_markdown(text)[0]
    if not str(indexed.get("name") or "").strip() or not str(indexed.get("description") or "").strip():
        return ("The skill index cannot read a name and description back out of this frontmatter. "
                "Keep it to plain `key: value` lines between two `---` fences.")
    return None


def name_mismatch(name, content):
    """The frontmatter ``name`` is the directory name: the index, ``/skill`` and ``skill_view``
    all address a skill by it, so the two must not drift apart."""
    from misaka.utils.frontmatter import parse_frontmatter
    declared = str((parse_frontmatter(str(content)).frontmatter or {}).get("name") or "").strip()
    name = Path(name).name
    if declared != name:
        return f"Frontmatter name '{declared}' must equal the skill directory name '{name}'."
    return None


def validate_content_size(content, label="SKILL.md"):
    return _validate_content_size(str(content or ""), label)


def _security_scan(skill_dir):
    """Run the skill security scan; return a blocking message, or None when allowed. A scanner
    that cannot run blocks too: a change nobody scanned is not a scanned change."""
    try:
        from misaka.core.skills.guard import (
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
        from misaka.core.skills.linter import lint_skill
        found = lint_skill(Path(skill_md).parent)
    except Exception:  # noqa: BLE001
        return []
    return [{"severity": f.severity, "rule": f.rule, "message": f.message} for f in found]


def _description_preview(content):
    """Return the description exactly as the prompt index will display it."""
    from misaka.core.skills.index import (
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
    from misaka.core.skills import index
    index.invalidate()


def _normalize_visible_roots(profile_dir, roots):
    """Freeze session roots into the JSON shape carried by pending creates."""
    if roots is _VISIBLE_ROOTS_UNSET:
        from misaka.core.skills.layers import skill_roots
        roots = skill_roots(profile_dir)
    if not isinstance(roots, (list, tuple)):
        return None, "visible_roots must be a list of [layer, absolute path] pairs."
    normalized = []
    for item in roots:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            return None, "visible_roots must contain [layer, absolute path] pairs."
        layer, root = item
        if not isinstance(layer, str) or not layer.strip():
            return None, "Each visible skill root must have a non-empty layer name."
        try:
            root = os.fspath(root)
        except TypeError:
            return None, "Each visible skill root path must be a string or path-like value."
        if not isinstance(root, str) or not root or "\0" in root:
            return None, "Each visible skill root path must be a non-empty filesystem path."
        normalized.append((layer, os.path.abspath(os.path.expanduser(root))))
    return tuple(normalized), None


def _visible_skill_conflict(name, visible_roots):
    """Return the visible skill that already claims ``name``, if any."""
    from misaka.core.skills import index

    matches = index.candidates(visible_roots, name)
    return matches[0] if matches else None


def _create_conflict_error(name, visible_roots):
    """Creating a shadowed or ambiguous skill is not a successful create."""
    existing = _visible_skill_conflict(name, visible_roots)
    if existing:
        return (
            f"A visible skill named '{name}' already exists in the {existing['layer']} "
            f"layer: {existing['dir']}"
        )
    return None


def _create(profile_dir, name, content, visible_roots=_VISIBLE_ROOTS_UNSET):
    roots_error = None
    if visible_roots is _VISIBLE_ROOTS_UNSET:
        visible_roots, roots_error = _normalize_visible_roots(profile_dir, visible_roots)
    skill_dir, err = _skill_dir(profile_dir, name)
    err = err or validate_frontmatter(content, new_skill=True)
    err = err or name_mismatch(name, content) or validate_content_size(content)
    err = err or roots_error
    err = err or _create_conflict_error(name, visible_roots)
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
    """Return a lexical support-file path, rejecting redirects inside the skill."""
    err = lookup_path_error(file_path)
    if err:
        return None, err
    from misaka.core.skills.guard import SKILL_IGNORE_FILENAMES
    relative = Path(file_path)
    if relative.name in SKILL_IGNORE_FILENAMES:
        return None, f"{file_path} controls what the security scanner sees; it is not written through skill_manage."
    # A SKILL.md anywhere but the skill root is a second skill, not a support
    # file: the index walks every directory under the layer root and any folder
    # holding a SKILL.md becomes an entry of its own, named by its own
    # frontmatter. Writing one through this path would skip everything create
    # enforces (frontmatter validation, name == directory name, the visible-layer
    # conflict check, the 60-character description budget) and would be recorded
    # in the ledger as "add a file to skill X" while actually adding skill Y.
    # The root SKILL.md is still reachable here — `patch(file_path='SKILL.md')`
    # is a normal thing to ask for, and the callers re-validate it as SKILL.md.
    if relative.name.casefold() == "skill.md" and len(relative.parts) > 1:
        return None, (
            f"{file_path} would define a second skill inside '{Path(skill_dir).name}'. "
            "Create a skill with skill_manage(action='create'); support files cannot be named SKILL.md."
        )
    current = Path(skill_dir)
    if current.is_symlink():
        return None, f"Skill mutation paths cannot contain symlinks: {current}"
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return None, f"Skill mutation paths cannot contain symlinks: {current}"
    target = Path(skill_dir) / file_path
    if target.is_dir():
        # Every caller from here reads, writes or unlinks the path as a file;
        # a directory turns that into an uncaught IsADirectoryError/PermissionError
        # and the tool answers with a traceback instead of its error contract.
        return None, f"{file_path} is a directory, not a file."
    return target, None


def _read_text_file(path, label):
    """``(text, None)`` for a UTF-8 file, ``(None, error)`` otherwise.

    ``read_text`` raises on both halves of this: a directory and a file that is
    not UTF-8. The mutation tool's contract is a result dict, and
    ``manage_execute`` reads it without a try/except, so neither may escape.
    """
    try:
        return path.read_bytes().decode("utf-8"), None
    except UnicodeDecodeError:
        return None, (f"{label} is not UTF-8 text, so it cannot be edited through skill_manage. "
                      "Replace it with remove_file plus write_file, or edit it outside the tool.")
    except OSError as error:
        return None, f"Cannot read {label}: {error}"


def _support_file_error(text, label):
    """One limit for support files, the scanner's: what it would refuse to read is refused here."""
    from misaka.core.skills.guard import MAX_SINGLE_FILE_KB
    if len(text.encode("utf-8")) > MAX_SINGLE_FILE_KB * 1024:
        return f"{label} exceeds {MAX_SINGLE_FILE_KB} KB, the security scanner's single-file limit."
    return validate_content_size(text, label)


def _require_skill(profile_dir, name):
    """Return ``(skill_dir, None)`` for an existing skill, or ``(None, error)``."""
    skill_dir, err = _skill_dir(profile_dir, name)
    if err:
        return None, err
    if skill_dir.is_symlink():
        return None, f"Skill mutation paths cannot contain symlinks: {skill_dir}"
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
    # Bytes, not text: the copy exists only to put the file back if the scan
    # rejects the write, and an existing support file the tool did not create
    # (a vendored asset, a latin-1 note) is allowed to be anything at all.
    # Decoding it here would fail the whole call on a file we never had to read.
    try:
        original = target.read_bytes() if target.exists() else None
    except OSError as error:
        return {"success": False, "error": f"Cannot read {file_path}: {error}"}
    _atomic_write(target, file_content)

    scan_error = _security_scan(skill_dir)
    if scan_error:
        if original is not None:
            atomic.write_bytes(target, original)
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
    # The replacement is validated; the old bytes are only the undo. Reading them
    # as text would make a skill whose SKILL.md is not UTF-8 (installed by hand,
    # written by another tool) impossible to repair with the very action meant
    # to replace it wholesale.
    try:
        original = md.read_bytes()
    except OSError as error:
        return {"success": False, "error": f"Cannot read SKILL.md for '{name}': {error}"}
    _atomic_write(md, content)
    scan_error = _security_scan(skill_dir)
    if scan_error:
        atomic.write_bytes(md, original)
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
    label = file_path or "SKILL.md"
    if not target.exists():
        return {"success": False, "error": f"File does not exist: {label}"}
    if target.is_dir():
        return {"success": False, "error": f"{label} is a directory, not a file."}

    content, err = _read_text_file(target, label)
    if err:
        return {"success": False, "error": err}
    new_content, count, _strategy, match_error = fuzzy_find_and_replace(
        content, old_string, new_string, replace_all)
    if match_error:
        match_error += format_no_match_hint(match_error, count, old_string, content)
        return {"success": False, "error": match_error,
                "file_preview": content[:500] + ("..." if len(content) > 500 else "")}

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

    from .vendor.skill_provenance import is_background_review
    if is_background_review():
        from .vendor.skill_usage import archive_skill
        ok, message = archive_skill(name)
        return {"success": ok, "message": message, "_archived": ok}
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
    try:
        target.unlink()
    except OSError as error:                # unreadable parent, read-only mount, a race with _resolve_target's is_dir check
        return {"success": False, "error": f"Cannot delete {file_path}: {error}"}
    parent = target.parent
    try:
        if parent != skill_dir and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:                         # tidying an emptied directory is best-effort; the file is gone either way
        pass
    return {"success": True, "message": f"Deleted {file_path} from '{name}'."}




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


# Hermes exposes ONE shape; legacy calls are normalized before schema validation.
# Keep unknown keys so validation reports them rather than silently dropping work.
_OPERATION_KEYS = {"action", "name", "content", "category", "file_path", "file_content",
                   "old_string", "new_string", "replace_all", "absorbed_into"}
MANAGE_PARAMETERS = copy.deepcopy(SKILL_MANAGE_SCHEMA["parameters"])
MANAGE_PARAMETERS["additionalProperties"] = False
MANAGE_PARAMETERS["properties"]["operations"]["items"]["additionalProperties"] = True


def prepare_arguments(raw):
    if not isinstance(raw, dict):
        return raw
    result = copy.deepcopy(raw)
    if result.get("operations") is not None:
        default_name = result.get("name")
        result = {k: v for k, v in result.items() if k not in _OPERATION_KEYS}
        if isinstance(result["operations"], list):
            for op in result["operations"]:
                if isinstance(op, dict) and not op.get("name") and default_name:
                    op["name"] = default_name
    else:
        op = {k: v for k, v in result.items() if k in _OPERATION_KEYS}
        result = {k: v for k, v in result.items() if k not in _OPERATION_KEYS and k != "operations"}
        result["operations"] = [op]
    if isinstance(result.get("operations"), list):
        for op in result["operations"]:
            if isinstance(op, dict) and op.get("action") == "edit":
                op["action"] = "patch"
    return result


def _validate_operations(operations):
    from .vendor.batch import _BATCH_MAX_OPS, _validate_batch_ops
    if not isinstance(operations, list) or not operations:
        return "operations must be a non-empty array."
    if len(operations) > _BATCH_MAX_OPS:
        return f"operations is capped at {_BATCH_MAX_OPS} ops per call."
    for op in operations:
        if not isinstance(op, dict):
            return "Every operation must be an object."
        if unknown := op.keys() - _OPERATION_KEYS:
            return f"Unknown operation fields: {', '.join(sorted(unknown))}"
        for k, v in op.items():
            if k == "replace_all":
                if not isinstance(v, bool):
                    return "replace_all must be a boolean."
            elif not isinstance(v, str):
                return f"{k} must be a string."
        if not op.get("name") or not op.get("action"):
            return "Every operation needs a name and action."
    if any(op["action"] == "delete" for op in operations):
        return None if len(operations) == 1 else "delete must be the SOLE op in its call."
    _, err = _validate_batch_ops(operations, None, lambda message, **_: message, lambda *_: None)
    return err


def _apply_operation(profile_dir, op):
    action, name = op["action"], op["name"]
    if action == "create":
        return _create(profile_dir, name, op.get("content", ""), ())
    if action == "patch":
        if op.get("content") and (op.get("old_string") or op.get("new_string") is not None):
            return {"success": False, "error": "Pass EITHER content (full SKILL.md rewrite) OR old_string/new_string (targeted replacement), not both."}
        if op.get("content"):
            return _edit_skill(profile_dir, name, op["content"])
        return _patch_skill(profile_dir, name, op.get("old_string"), op.get("new_string"), op.get("file_path"), op.get("replace_all", False))
    if action == "delete":
        return _delete_skill(profile_dir, name, op.get("absorbed_into"))
    if action == "write_file":
        return _write_file(profile_dir, name, op.get("file_path", ""), op.get("file_content"))
    return _remove_file(profile_dir, name, op.get("file_path", ""))


def _bind_operations(profile_dir, operations, visible_roots):
    """Read and write resolve the same identity; only this role's objects are mutable."""
    from . import index
    root = _skills_root(profile_dir).absolute()
    created = {}
    bound = []
    for original in operations:
        op = dict(original)
        name = op["name"]
        if op["action"] == "create":
            if err := _validate_category(op.get("category")):
                return None, err
            if "/" in name or "\\" in name:
                return None, "New skill names are identifiers; use category for the category directory."
            if err := _create_conflict_error(name, visible_roots):
                return None, err
            rel = str(Path(op.get("category") or "") / name)
            created[name] = rel
            created[rel] = rel
        elif name in created:
            rel = created[name]
        else:
            entry, err = index.resolve(visible_roots, name, require_compatible=False)
            if err:
                return None, err
            if entry.get("legacy"):
                return None, "Legacy flat Skill documents are read-only; create a directory Skill to migrate one."
            if Path(entry.get("root", "")).resolve() != root.resolve():
                return None, f"Skill '{name}' belongs to the {entry['layer']} layer; this session only manages its own role."
            rel = str(Path(entry["dir"]).relative_to(root))
        _directory, err = _skill_dir(profile_dir, rel)
        if err:
            return None, err
        op["name"] = rel
        if op.get("absorbed_into"):
            target, err = index.resolve(visible_roots, op["absorbed_into"], require_compatible=False)
            if err or target.get("legacy") or Path(target.get("root", "")).resolve() != root.resolve():
                return None, "The absorbing skill must exist in this role."
            op["absorbed_into"] = target["rel"]
        bound.append(op)
    return bound, None


def manage(action=None, name=None, *, profile_dir, content=None, file_path=None,
           file_content=None, old_string=None, new_string=None, category=None,
           replace_all=False, absorbed_into=None, base=None, operations=None,
           approved_payload_hash=None, visible_roots=_VISIBLE_ROOTS_UNSET,
           workspace=None, _reviewed=None):
    """One isolated, recoverable transaction for single calls, batches, and approvals."""
    from .layers import skill_roots
    from .scope import scope_for, using_scope
    if profile_dir is None:
        return {"success": False, "error": "Skill writing requires a role profile."}
    workspace = os.path.abspath(workspace if workspace is not None else os.getcwd())
    profile_dir = os.path.abspath(os.path.expanduser(profile_dir))
    flat = {k: v for k, v in {"action": action, "name": name, "content": content, "file_path": file_path,
            "file_content": file_content, "old_string": old_string, "new_string": new_string,
            "category": category, "replace_all": replace_all, "absorbed_into": absorbed_into}.items() if v is not None}
    canonical = prepare_arguments({**flat, **({"operations": operations} if operations is not None else {})})["operations"]
    if err := _validate_operations(canonical):
        return {"success": False, "error": err}
    if visible_roots is _VISIBLE_ROOTS_UNSET:
        visible_roots = skill_roots(profile_dir, workspace)
    supplied, err = _normalize_visible_roots(profile_dir, visible_roots)
    if err:
        return {"success": False, "error": err}
    # Fixed workspace identity, but fresh discovery: absent project roots can appear
    # between staging and approval. Explicit extension roots remain part of review.
    extensions = [root for layer, root in supplied if layer == "extension"]
    def roots_now():
        return list(dict.fromkeys([*skill_roots(profile_dir, workspace, extension_paths=extensions), *supplied]))
    try:
        with using_scope(scope_for(profile_dir, workspace)), skill_write.mutation_lock():
            # Recover before discovery: a crash mid-delete can make the target
            # temporarily absent, so resolving first would strand its journal.
            skill_write.recover_transactions()
            if approved_payload_hash is not None and (_reviewed is None or skill_write.payload_sha256(_reviewed) != approved_payload_hash):
                return {"success": False, "error": "Approved skill payload changed after review; nothing applied."}
            bound, err = _bind_operations(profile_dir, canonical, roots_now())
            if err:
                return {"success": False, "error": err}
            from .operations import guards as maintenance_guards
            if denied := maintenance_guards(bound, profile_dir, workspace):
                return denied
            # Cheap validation precedes staging; do not ask the user to approve a
            # malformed main document or an invalid support-file target.
            for op in bound:
                a = "edit" if op["action"] == "patch" and op.get("content") else op["action"]
                err = _precheck(a, _skills_root(profile_dir) / op["name"], op["name"], op.get("content", ""), op.get("file_path", ""), op.get("file_content"), op.get("old_string", ""), op.get("new_string"))
                if err:
                    return {"success": False, "error": err}
            if err := _validate_operations(bound):
                return {"success": False, "error": err}
            from .vendor.skill_provenance import is_background_review
            curator_archive = is_background_review() and any(op['action'] == 'delete' for op in bound)
            paths = ([str(_skills_root(profile_dir))] if curator_archive else
                     list(dict.fromkeys(str(_skills_root(profile_dir) / op["name"]) for op in bound)))
            dependencies = list(dict.fromkeys(str(_skills_root(profile_dir) / op["absorbed_into"])
                                               for op in bound if op.get("absorbed_into")))
            bases = {path: skill_write.digest(path) for path in [*paths, *dependencies]}
            if _reviewed is not None and bases != _reviewed.get("bases"):
                return {"success": False, "error": "Skill changed after this write was reviewed; inspect and stage it again."}
            if base is not None and len(paths) == 1 and bases[paths[0]] != base:
                return {"success": False, "error": "Skill changed after review."}
            from .release import token as writer_token
            from .vendor.skill_provenance import get_current_write_origin
            generation = writer_token(profile_dir)
            if _reviewed is not None and _reviewed.get("writer_generation") != generation:
                return {"success": False, "error": "Writer generation changed after review; stage again."}
            payload = {"version": 2, "writer_generation": generation, "write_origin": (_reviewed or {}).get("write_origin", get_current_write_origin()), "profile_dir": profile_dir, "workspace": workspace,
                       "operations": canonical, "visible_roots": supplied, "bases": bases,
                       "bound_names": [op["name"] for op in bound]}
            if payload["write_origin"] == "background_review":
                from .vendor.skill_manager_guards import _background_review_has_read
                targets = [_skills_root(profile_dir) / op['name'] / (op.get('file_path') or 'SKILL.md') for op in bound]
                payload['read_paths'] = [str(path.resolve()) for path in targets if _background_review_has_read(path)]
            if _reviewed is not None and payload["bound_names"] != _reviewed.get("bound_names"):
                return {"success": False, "error": "Skill identity changed after review; stage again."}
            if not _bypass.get():
                decision, note = skill_write.evaluate_gate()
                if decision == "off":
                    return {"success": False, "error": note}
                if decision == "stage":
                    gist = "; ".join(_gist(op["action"], op["name"], op.get("content", ""), op.get("file_path", ""), op.get("old_string", "")) for op in canonical)
                    try:
                        staged = skill_write.stage(payload, summary=gist)
                    except OSError as error:
                        return {"success": False, "error": f"Could not stage the skill write for review: {error}"}
                    return {"success": True, "staged": True, "pending_id": staged["id"], "gist": gist, "message": note}
            from .vendor.skill_provenance import get_current_write_origin
            evidence = {"profile_dir": profile_dir, "workspace": workspace,
                        "operations": canonical, "bound_operations": bound, "write_origin": payload["write_origin"],
                        "payload_sha256": approved_payload_hash, "curator_archive": curator_archive}
            journal = skill_write.prepare_transaction(paths, action=action if operations is None else "batch", skill=", ".join(dict.fromkeys(op["name"] for op in canonical)), evidence=evidence)
            # Work outside every live Skill tree. The transaction commits only after
            # ALL operations, validations and scans succeeded. No partial rmtree.
            with tempfile.TemporaryDirectory(prefix="misaka-skill-stage-") as temp:
                stage_profile = Path(temp)
                for change in journal["changes"]:
                    dest = stage_profile / "skills" / Path(change["root"]).relative_to(_skills_root(profile_dir))
                    skill_write._materialize(dest, change["before"])
                for dependency in dependencies:
                    dest = stage_profile / "skills" / Path(dependency).relative_to(_skills_root(profile_dir))
                    if not dest.exists():
                        skill_write._materialize(dest, skill_write.tree_image(dependency))
                results = []
                try:
                    for i, op in enumerate(bound):
                        if curator_archive:
                            from dataclasses import replace

                            from .scope import SkillScope, _current, using_scope
                            scope = _current.get() or SkillScope(Path(profile_dir), Path(workspace), origin='background_review')
                            with using_scope(replace(scope, storage=stage_profile)):
                                result = _apply_operation(str(stage_profile), op)
                        else:
                            result = _apply_operation(str(stage_profile), op)
                        if not result.get("success"):
                            skill_write.abort_transaction(journal)
                            if operations is None:
                                return result
                            return {**result, "failed_index": i, "completed_before_failure": i,
                                    "error": f"operations[{i}] failed: {result.get('error')}; live skills unchanged."}
                        results.append(result)
                    after = [skill_write.tree_image(stage_profile / "skills" / Path(path).relative_to(_skills_root(profile_dir))) for path in paths]
                    if curator_archive:
                        from .distribution import _pre_publish
                        _pre_publish(journal['changes'][0]['before'], after[0], scope, 'archive')
                    entry_id = skill_write.commit_transaction(journal, after)
                except BaseException as error:
                    if journal["state"] == "prepared":
                        skill_write._recover(journal)
                    if journal["state"] == "aborted" and isinstance(error, Exception):
                        return {"success": False, "error": f"Skill transaction failed and was rolled back ({error}); live skills retain their before-images."}
                    raise
                for result in results:
                    for key in ("path", "skill_md"):
                        if isinstance(result.get(key), str):
                            result[key] = result[key].replace(str(stage_profile), profile_dir, 1)
                _invalidate_index()
                from .operations import flush_usage
                try:
                    flush_usage(profile_dir, workspace)
                except Exception:
                    logger.warning("Committed Skill usage effects await ledger replay", exc_info=True)
                from .operations import org_edit_notes
                try:
                    notes = org_edit_notes(bound, profile_dir, workspace)
                except Exception as error:  # Post-commit sharing must not falsify the committed local receipt.
                    logger.warning("Skill committed; organisation sharing failed", exc_info=True)
                    notes = [f"Local Skill committed; organisation sharing failed: {error}"]
                sharing = {"org_sharing": " ".join(notes)} if notes else {}
                if operations is None:
                    single = {**results[0], "ledger_id": entry_id, **sharing}
                    if notes:
                        single["message"] = (single.get("message", "") + " " + sharing["org_sharing"]).strip()
                    return single
                return {"success": True, "operations_applied": len(results), "ledger_id": entry_id, **sharing,
                        "results": [{"name": raw["name"], "action": raw["action"], "file_path": raw.get("file_path"), "success": True} for raw in canonical]}
    except (OSError, ValueError, TypeError) as error:
        return {"success": False, "error": f"Skill transaction did not complete: {error}"}


def apply_pending(record):
    """Verify original ID and payload BEFORE interpreting or converting it."""
    integrity_error = skill_write.pending_integrity_error(record, file_id=record.get("_pending_file_id") if isinstance(record, dict) else None)
    if integrity_error:
        return {"success": False, "error": f"Approval integrity check failed: {integrity_error}"}
    payload = copy.deepcopy(record["payload"])
    if payload.get("version") == 3 and payload.get("kind") == "distribution":
        from .distribution import apply_pending
        return apply_pending(payload)
    if payload.get("version") != 2 or not payload.get("workspace") or not payload.get("profile_dir"):
        return {"success": False, "error": "Legacy pending write has no bound workspace/source identity; inspect the original request and stage it again."}
    if skill_write.evaluate_gate()[0] == 'off':
        return {"success": False, "error": skill_write.evaluate_gate()[1]}
    from .scope import SkillScope, using_scope
    from .vendor.skill_manager_guards import mark_background_review_skill_read
    scope = SkillScope(Path(payload['profile_dir']), Path(payload['workspace']), origin=payload.get('write_origin', 'foreground'))
    token = _bypass.set(True)
    try:
        with using_scope(scope):
            # The immutable staged payload records actual read marks. Original base
            # digests are rechecked by manage before any operation is committed.
            for path in payload.get('read_paths', []):
                mark_background_review_skill_read(Path(path))
            return manage(profile_dir=payload["profile_dir"], operations=payload["operations"],
                          workspace=payload["workspace"], visible_roots=payload.get("visible_roots", ()),
                          _reviewed=payload, approved_payload_hash=record["payload_sha256"])
    finally:
        _bypass.reset(token)


def pending_diff(payload):
    """Unified diff a pending write would produce, resolving paths exactly as ``apply_pending`` will."""
    import difflib

    if payload.get("version") == 2:
        # Simulate the SAME ordered operations on detached copies. Previewing
        # each against live files lies for create->write_file and patch chains.
        from .layers import skill_roots
        profile = payload["profile_dir"]
        supplied = payload.get("visible_roots", [])
        extensions = [root for layer, root in supplied if layer == "extension"]
        roots = list(dict.fromkeys([*skill_roots(profile, payload["workspace"], extension_paths=extensions),
                                  *(tuple(r) for r in supplied)]))
        with skill_write.mutation_lock(), tempfile.TemporaryDirectory(prefix="misaka-skill-preview-") as temp:
            bound, err = _bind_operations(profile, payload["operations"], roots)
            if err or [op["name"] for op in bound] != payload["bound_names"]:
                return f"(cannot preview: {err or 'Skill identity changed after review'})"
            for source, expected in payload["bases"].items():
                if skill_write.digest(source) != expected:
                    return "(cannot preview: Skill changed after review; stage again)"
                path = Path(source)
                if path.exists():
                    skill_write._safe_parents(path)
                    dest = Path(temp) / "skills" / path.relative_to(_skills_root(profile))
                    if not dest.exists():  # A whole-role base already contains its dependencies.
                        shutil.copytree(path, dest, symlinks=True)
            from .scope import SkillScope, using_scope
            scope = SkillScope(Path(profile), Path(payload['workspace']), storage=Path(temp),
                               origin=payload.get('write_origin', 'foreground'))
            diffs = []
            token = _bypass.set(True)  # Only detached preview storage, never the live gate.
            try:
                with using_scope(scope):
                    for i, op in enumerate(bound):
                        diff = pending_diff({**op, "profile_dir": temp})
                        result = _apply_operation(temp, op)
                        if not result.get("success"):
                            return f"(cannot preview: operations[{i}] failed: {result.get('error')}; nothing applied)"
                        if result.get('_archived'):
                            diff = "Recoverable archive (not hard deletion); restore retains this content.\n" + diff
                        diffs.append(f"operations[{i}] {op['action']} {op['name']}\n{diff}")
            finally:
                _bypass.reset(token)
            return "\n\n".join(diffs)
    action = str(payload.get("action") or "")
    if action == "patch" and payload.get("content"):
        action = "edit"
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
        if not old_string:
            return "(cannot preview: old_string is empty)"
        new, _, _, error = fuzzy_find_and_replace(old, old_string, new_string, payload.get("replace_all", False))
        if error:
            return f"(cannot preview: {error})"
    return udiff(rel_name(target), old, new) or "(no changes)"

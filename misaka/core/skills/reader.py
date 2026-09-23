"""Single Skill reader used by tools, commands and definition preloads.

Result fields/order follow tools/skills_tool.py at the pin in vendor/PROVENANCE.
MISAKA supplies admitted identities, local readiness and stronger link containment.
No discovery, plugin registration, process-global secret callback, or hidden writes.
"""

from pathlib import Path

from .index import parse_skill_markdown as parse_frontmatter
from .layers import SKILL_SUPPORT_DIRS
from .manage import lookup_path_error
from .preprocessing import preprocess_skill_content
from .vendor.metadata import skill_matches_platform
from .vendor.view import _parse_tags


def support_target(skill_dir, file_path):
    if err := lookup_path_error(file_path):
        return None, err
    root = Path(skill_dir).resolve()
    target = root / file_path
    current = root
    for component in Path(file_path).parts:
        current /= component
        if current.is_symlink():
            return None, f"Skill support paths cannot contain symlinks: {file_path}"
    if not target.resolve().is_relative_to(root):
        return None, f"File is outside the skill directory: {file_path}"
    if not target.is_file():
        return None, f"Skill support file not found: {file_path}"
    return target, None


def collect_linked_files(skill_dir):
    root = Path(skill_dir)
    out = {}
    for category in sorted(SKILL_SUPPORT_DIRS):
        sub = root / category
        if not sub.is_dir() or sub.is_symlink():
            continue
        files = [
            str(f.relative_to(root))
            for f in sorted(sub.rglob("*"))
            if support_target(root, str(f.relative_to(root)))[1] is None
        ]
        if files:
            out[category] = files
    return out


def read_support_file(skill_dir, file_path):
    target, err = support_target(skill_dir, file_path)
    if err:
        return None, err
    try:
        data = target.read_bytes()
        try:
            return data.decode("utf-8-sig"), None
        except UnicodeDecodeError:
            return f"[Binary file: {target.name}, {len(data)} bytes]", None
    except OSError as error:
        return None, str(error)


def readiness(frontmatter, profile_dir=None, *, runtime=None, name="", capture=False):
    from .runtime import SkillRuntime
    return (runtime or SkillRuntime(profile_dir)).readiness(frontmatter, name, capture=capture)


def load(entry, session_id=None, *, file_path=None, profile_dir=None, preprocess=True, runtime=None):
    from .runtime import _active, using_runtime
    if runtime is not None and _active.get() is not runtime:
        with using_runtime(runtime):
            return load(entry, session_id, file_path=file_path, profile_dir=profile_dir,
                        preprocess=preprocess, runtime=runtime)
    path, directory = Path(entry["path"]), Path(entry["dir"])
    content = path.read_text(encoding="utf-8-sig", errors="replace")
    fm, _ = parse_frontmatter(content)
    name = fm.get("name", entry.get("runtime_name", entry.get("name", directory.name)))
    if entry.get("namespace"):
        name = entry["runtime_name"]
    if not skill_matches_platform(fm):
        return {
            "success": False,
            "error": f"Skill '{name}' is not supported on this platform.",
            "readiness_status": "unsupported",
        }
    if file_path:
        text, error = read_support_file(directory, file_path)
        return (
            {"success": False, "error": error}
            if error
            else {
                "success": True,
                "name": name,
                "file": file_path,
                "content": text,
                "_source_path": str(directory / file_path),
            }
        )
    meta = fm.get("metadata")
    hm = meta.get("hermes") if isinstance(meta, dict) else {}
    hm = hm if isinstance(hm, dict) else {}
    tags, related = (
        _parse_tags(hm.get(k) or fm.get(k, "")) for k in ("tags", "related_skills")
    )
    linked = collect_linked_files(directory)
    setup = readiness(fm, profile_dir, runtime=runtime, name=name, capture=True)
    rendered = (
        preprocess_skill_content(
            content,
            directory,
            session_id,
            layer=entry.get("origin_layer", entry.get("layer")),
        )
        if preprocess
        else content
    )
    result = {
        "success": True,
        "name": name,
        "description": fm.get("description", ""),
        "tags": tags,
        "related_skills": related,
        "content": rendered,
        "path": str(Path(entry.get("rel", directory.name)) / path.name),
        "skill_dir": str(directory),
        "org_provenance": entry.get("org_provenance"),
        "linked_files": linked or None,
        "usage_hint": "To view linked files, call skill_view(name, file_path) where file_path is e.g. 'references/api.md' or 'assets/config.yaml'"
        if linked
        else None,
        **setup,
        "_source_path": str(path),
    }
    if entry.get("namespace"):
        namespace, siblings = entry["namespace"], entry.get("siblings", [])
        result["extension_provenance"] = {"namespace": namespace, "root": entry.get("extension_root"),
                                          "path": entry.get("origin_path", entry["path"])}
        banner = f"[Bundle context: This skill is part of the '{namespace}' plugin." + (
            f"\nSibling skills: {', '.join(siblings)}.\nUse qualified form to invoke siblings "
            f"(e.g. {namespace}:{siblings[0]})." if siblings else "") + "]\n\n"
        # Keep raw metadata at the start for commands' config parsing. The
        # display banner is appended after the document, not before frontmatter.
        result["content"] = rendered + "\n\n" + banner.rstrip()
    if entry.get("org_header"):
        result["content"] += "\n\n" + entry["org_header"].rstrip()
    if fm.get("compatibility"):
        result["compatibility"] = fm["compatibility"]
    if isinstance(meta, dict):
        result["metadata"] = meta
    return result

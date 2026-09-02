"""The skill index: what a session can load, and how it is advertised.

Port of hermes ``build_skills_system_prompt`` / ``skills_list`` / ``skill_view``
lookups. The prompt reads only frontmatter; ``skills_list`` reads the first prose
line only when a description is missing, and the full body otherwise loads on
demand through ``skill_view``. The engine's own skill loading is off for every
MISAKA session: the extension in
:mod:`misaka.extensions.skills` is the one consumer of this index, so a session's
skills are decided in exactly one place.

Caching, as hermes: an in-process cache per roots and disabled list (dropped by
``invalidate()``), and for the personal layers a disk snapshot validated by an
mtime/size manifest of every SKILL.md and DESCRIPTION.md, so a cold start never
re-reads an unchanged tree. Project entries pass the quarantine scanner after a
whole-bundle fingerprint changes; external layers are scanned directly.
"""
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from misaka.skills.layers import (
    PERSONAL_LAYERS,
    disabled_skill_names,
    home,
    iter_project_skill_files,
    iter_skill_files,
    project_skill_tree_fingerprint,
    walk_skill_tree,
)

_INVALID = re.compile(r"[^a-z0-9-]")
_MULTI_HYPHEN = re.compile(r"-{2,}")
_FRONTMATTER_END = re.compile(r"\n---\s*\n")
_CACHE = {}                 # (roots, disabled) -> (prompt entries, categories, all, runtime entries)
_CACHE_MAX = 32
SNAPSHOT_VERSION = 2

# Cap on description length in the system-prompt skill index (hermes SKILL_PROMPT_DESC_LIMIT).
# The index lives in every session, so longer descriptions are truncated; learn_prompt's hard
# "<= 60 characters" rule comes from this limit.
SKILL_PROMPT_DESC_LIMIT = 60
SKILL_LIST_DESC_LIMIT = 1024
_PLATFORM_MAP = {"macos": "darwin", "linux": "linux", "windows": "win32"}


def truncate_skill_description(description):
    """Description for the index: truncate with an ellipsis past the limit (hermes extract_skill_description)."""
    desc = str(description or "").strip().strip("'\"")
    if len(desc) > SKILL_PROMPT_DESC_LIMIT:
        return desc[: SKILL_PROMPT_DESC_LIMIT - 3] + "..."
    return desc


def is_skill_description_truncated(description):
    """Whether this description would be truncated in the index (used by the linter and /learn)."""
    return len(str(description or "").strip().strip("'\"")) > SKILL_PROMPT_DESC_LIMIT


def parse_skill_markdown(content):
    """Hermes' runtime SKILL.md parser: BOM-safe YAML with a key:value fallback.

    Mutation and lint paths deliberately keep using the strict global parser; this
    lenient parser is only for advertising and loading already-present skills.
    """
    content = content.removeprefix("\ufeff")
    body = content
    if not content.startswith("---"):
        return {}, body
    end = _FRONTMATTER_END.search(content, 3)
    if end is None:
        return {}, body
    yaml_content = content[3:end.start()]
    body = content[end.end():]
    try:
        import yaml
        parsed = yaml.load(yaml_content, Loader=getattr(yaml, "CSafeLoader", None) or yaml.SafeLoader)
        return (parsed if isinstance(parsed, dict) else {}), body
    except Exception:  # noqa: BLE001 - Hermes recovers useful metadata from malformed YAML
        frontmatter = {}
        for line in yaml_content.strip().split("\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                frontmatter[key.strip()] = value.strip()
        return frontmatter, body


def list_skill_description(frontmatter, body):
    """Hermes ``skills_list`` description: frontmatter, then first prose line."""
    description = frontmatter.get("description", "")
    if not description:
        description = next((line for raw in body.strip().split("\n")
                            if (line := raw.strip()) and not line.startswith("#")), "")
    description = str(description or "")
    if len(description) > SKILL_LIST_DESC_LIMIT:
        return description[: SKILL_LIST_DESC_LIMIT - 3] + "..."
    return description


def skill_matches_platform(frontmatter):
    """Hermes' top-level ``platforms`` offer/load filter."""
    platforms = frontmatter.get("platforms")
    if not platforms:
        return True
    if not isinstance(platforms, list):
        platforms = [platforms]
    termux = bool(os.environ.get("TERMUX_VERSION")
                  or "com.termux/files/usr" in os.environ.get("PREFIX", ""))
    for platform in platforms:
        mapped = _PLATFORM_MAP.get(str(platform).lower().strip(), str(platform).lower().strip())
        if sys.platform.startswith(mapped):
            return True
        if termux and mapped in ("linux", "termux", "android"):
            return True
    return False


def _snapshot_dir():
    return os.path.join(home(), "cache", "skills")


def slug(name):
    """``Git_Helper`` -> ``git-helper``: how /skill and skill_view accept a name."""
    value = name.lower().replace(" ", "-").replace("_", "-")
    return _MULTI_HYPHEN.sub("-", _INVALID.sub("", value)).strip("-")


def invalidate():
    """Forget the in-process index. Called after this session writes a skill.

    A tree edited from outside (git, an editor) does not need this: ``build`` revalidates
    each layer's mtime/size manifest on every call, so a change shows on the next turn
    without throwing the cache away -- which is what made the cache never hit.
    """
    _CACHE.clear()


# ── one layer ──────────────────────────────────────────────────────────────

def _documents(path):
    """Return strict prompt metadata and tolerant runtime metadata from one read."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return ({}, ""), ({}, "")
    try:
        prompt = parse_skill_markdown(data.decode("utf-8"))
    except UnicodeDecodeError:
        prompt = ({}, "")              # prompt_builder is strict UTF-8: advertise only the directory name
    runtime = parse_skill_markdown(data.decode("utf-8-sig", errors="replace"))
    return prompt, runtime


def _frontmatter(path):
    return _documents(path)[0][0]


def _category(parts):
    """``("finance", "fmp-data", "SKILL.md")`` -> ``finance``; right under the root -> general."""
    return "/".join(parts[:-2]) if len(parts) > 2 else "general"


def _scan_root(root, skill_files=None):
    """Every skill under one layer root, plus the layer's category descriptions, as stored
    in a snapshot: ``{"skills": [...], "categories": {...}}``."""
    skills, categories = [], {}
    for skill_md in iter_skill_files(root) if skill_files is None else skill_files:
        (prompt_fm, _), (runtime_fm, runtime_body) = _documents(skill_md)
        rel = skill_md.relative_to(root).parts
        skills.append({"name": str(prompt_fm.get("name") or skill_md.parent.name).strip(),
                       "runtime_name": str(runtime_fm.get("name") or skill_md.parent.name).strip(),
                       "description": truncate_skill_description(str(prompt_fm.get("description") or "")),
                       "list_description": list_skill_description(runtime_fm, runtime_body),
                       "prompt_compatible": skill_matches_platform(prompt_fm),
                       "runtime_compatible": skill_matches_platform(runtime_fm),
                       "category": _category(rel), "rel": "/".join(rel[:-1]),
                       "dir": str(skill_md.parent), "path": str(skill_md)})
    for desc_md in iter_skill_files(root, "DESCRIPTION.md"):      # a category's own one-liner
        text = str(_frontmatter(desc_md).get("description") or "").strip().strip("'\"")
        parts = desc_md.relative_to(root).parts
        category = "/".join(parts[:-1]) if len(parts) > 1 else "general"
        if text and category not in categories:
            categories[category] = text
    return {"skills": skills, "categories": categories}


def _manifest(root):
    """mtime/size of every SKILL.md and DESCRIPTION.md under the root (hermes
    _build_skills_manifest): the snapshot is valid exactly while this is unchanged."""
    manifest = {}
    for here, files in walk_skill_tree(root):
        for name in ("SKILL.md", "DESCRIPTION.md"):
            if name in files:
                path = os.path.join(here, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                manifest[os.path.relpath(path, root)] = [st.st_mtime_ns, st.st_size]
    return manifest


def _snapshot_path(root):
    digest = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:16]
    return os.path.join(_snapshot_dir(), f"{digest}.json")


def _load_snapshot(root, manifest):
    try:
        with open(_snapshot_path(root), encoding="utf-8") as f:
            snapshot = json.load(f)
    except (OSError, ValueError):
        return None
    if (not isinstance(snapshot, dict) or snapshot.get("version") != SNAPSHOT_VERSION
            or snapshot.get("manifest") != manifest):
        return None
    return snapshot


def _write_snapshot(root, manifest, scanned):
    path = _snapshot_path(root)
    try:
        os.makedirs(_snapshot_dir(), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"version": SNAPSHOT_VERSION, "root": str(root), "manifest": manifest, **scanned}, f)
        os.replace(path + ".tmp", path)
    except OSError:
        pass                                       # best effort: the next start scans again


def _layer(layer, root):
    """A layer's skills: from its snapshot when the tree is unchanged (personal layers), else
    a scan -- written back as the new snapshot."""
    if layer == "project":
        return _scan_root(root, iter_project_skill_files(root))
    if layer not in PERSONAL_LAYERS:
        return _scan_root(root)
    manifest = _manifest(root)
    snapshot = _load_snapshot(root, manifest)
    if snapshot is None:
        snapshot = _scan_root(root)
        _write_snapshot(root, manifest, snapshot)
    return snapshot


# ── the index ─────────────────────────────────────────────────────────────

def _assemble(roots, disabled):
    entries, runtime_entries, categories, all_entries = [], [], {}, []
    prompt_names, runtime_names = set(), set()
    for layer, root in roots:
        scanned = _layer(layer, root)
        for skill in scanned["skills"]:
            entry = {**skill, "layer": layer}
            if ({entry["name"], entry.get("runtime_name", entry["name"]), Path(entry["dir"]).name}
                    & disabled):
                continue
            all_entries.append(entry)
            if entry.get("prompt_compatible", True) and entry["name"] not in prompt_names:
                prompt_names.add(entry["name"])
                entries.append(entry)
            runtime_name = entry.get("runtime_name", entry["name"])
            if entry.get("runtime_compatible", True) and runtime_name not in runtime_names:
                runtime_names.add(runtime_name)
                runtime_entries.append(entry)
        for category, text in scanned["categories"].items():
            categories.setdefault(category, text)
    return entries, categories, all_entries, runtime_entries


def _key(roots):
    return tuple((str(layer), str(root)) for layer, root in roots), tuple(sorted(disabled_skill_names()))


def _cached(roots):
    """Assemble once per (roots, disabled, on-disk state).

    Personal/external roots use the metadata-file manifest; project roots fingerprint the
    whole bundle so a support-file edit re-runs quarantine. An unchanged tree pays only
    filesystem metadata reads instead of YAML parsing and security scans.
    """
    normalized_roots, disabled = _key(roots)
    key = (normalized_roots, disabled,
           tuple(sorted((layer, _root_fingerprint(layer, root))
                        for layer, root in normalized_roots)))
    entry = _CACHE.get(key)
    if entry is None:
        entry = _CACHE[key] = _assemble(normalized_roots, set(disabled))
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
    else:
        _CACHE.pop(key)
        _CACHE[key] = entry
    return entry


def _manifest_digest(root):
    """A stable fingerprint of one layer's skill files, cheap enough to take every turn."""
    return hashlib.sha256(
        json.dumps(_manifest(root), sort_keys=True).encode()
    ).hexdigest()


def _root_fingerprint(layer, root):
    """Project quarantine tracks whole bundles; other indexes need metadata files only."""
    return (project_skill_tree_fingerprint(root) if layer == "project"
            else _manifest_digest(root))


def build(roots):
    """Index entries ``{"name", "description", "category", "layer", "dir", "path"}`` for the
    layer roots (``(layer, root)`` in precedence order): names first-wins, so a project skill
    shadows a role, shared, or external one of the same name; the ``disabled`` list honoured;
    the description cut to the prompt limit; the category read from the path inside the
    layer (``finance/fmp-data`` -> ``finance``, a skill right under the root -> ``general``)."""
    return _cached(roots)[0]


def runtime_build(roots):
    """Entries for list/view/invocation, decoded like Hermes' runtime tools."""
    return _cached(roots)[3]


def categories(roots):
    """``{category: description}`` from the layers' DESCRIPTION.md files (hermes)."""
    return _cached(roots)[1]


def candidates(roots, name):
    """Every skill a name could mean, across the layers -- shadowed ones included.
    ``skill_view`` refuses to guess between them (hermes collision rule); the index itself
    keeps the first."""
    wanted = (name or "").strip()
    key = slug(wanted)
    all_entries = _cached(roots)[2]
    exact = [entry for entry in all_entries
             if wanted in (entry["name"], entry.get("runtime_name", entry["name"]), entry["rel"])]
    return exact or [entry for entry in all_entries
                     if key and any(slug(candidate) == key for candidate in
                                    (entry["name"], entry.get("runtime_name", entry["name"])))]


def find(entries, name):
    """The index entry a name refers to."""
    wanted = (name or "").strip()
    key = slug(wanted)
    exact = next((entry for entry in entries
                  if wanted in (entry["name"], entry.get("runtime_name", entry["name"]), entry["rel"])), None)
    return exact or next((entry for entry in entries if key and any(
        slug(candidate) == key for candidate in
        (entry["name"], entry.get("runtime_name", entry["name"])))), None)


# ── the system-prompt section ─────────────────────────────────────────────

def index_lines(entries, category_descriptions=None, compact=(), *,
                name_key="name", description_key="description"):
    """The ``<available_skills>`` body, categories sorted, skills sorted inside each
    (hermes: ``  category: desc`` then ``    - name: description``); a project skill's
    description wears ``[project]``. A category whose top-level segment is in ``compact``
    is demoted to one names-only line -- nothing is ever hidden."""
    by_category = {}
    for entry in entries:
        by_category.setdefault(entry["category"], []).append(entry)
    lines = []
    for category in sorted(by_category):
        if category.split("/", 1)[0] in compact:
            names = sorted({entry.get(name_key, entry["name"]) for entry in by_category[category]})
            lines.append(f"  {category} [names only]: {', '.join(names)}")
            continue
        text = (category_descriptions or {}).get(category, "")
        lines.append(f"  {category}: {text}" if text else f"  {category}:")
        for entry in sorted(by_category[category], key=lambda e: e.get(name_key, e["name"])):
            name = entry.get(name_key, entry["name"])
            desc = entry.get(description_key, entry["description"])
            if entry["layer"] == "project":
                desc = f"[project] {desc}".strip()
            lines.append(f"    - {name}: {desc}" if desc else f"    - {name}")
    return lines


PROMPT_HEAD = (
    "## Skills (mandatory)\n"
    "Before replying, scan the skills below. If a skill matches or is even partially relevant "
    "to your task, you MUST load it with skill_view(name) and follow its instructions. "
    "Err on the side of loading — it is always better to have context you don't need "
    "than to miss critical steps, pitfalls, or established workflows. "
    "Skills contain specialized knowledge — API endpoints, tool-specific commands, "
    "and proven workflows that outperform general-purpose approaches. Load the skill "
    "even if you think you could handle the task with basic tools. "
    "Skills also encode the user's preferred approach, conventions, and quality standards "
    "for tasks like code review, planning, and testing — load them even for tasks you "
    "already know how to do, because the skill defines how it should be done here.\n"
    "If a skill has issues, fix it with skill_manage(action='patch').\n"
    "After difficult/iterative tasks, offer to save as a skill. "
    "If a skill you loaded was missing steps, had wrong commands, or needed "
    "pitfalls you discovered, update it before finishing.\n"
)
PROMPT_FOOT = "Only proceed without loading a skill if genuinely none are relevant to the task."
COMPACT_NOTE = (
    "\n(Categories marked [names only] are outside the current coding "
    "context, so their descriptions are omitted — the skills work "
    "normally and load with skill_view(name) as usual.)"
)


def render_prompt(entries, category_descriptions=None, compact=()):
    """The system-prompt section advertising the index (hermes wording), or "" when there
    is nothing to advertise."""
    if not entries:
        return ""
    lines = index_lines(entries, category_descriptions, compact)
    demoted = any(line.lstrip().split(" [names only]:")[0] != line.lstrip() for line in lines)
    return (PROMPT_HEAD + "\n<available_skills>\n" + "\n".join(lines)
            + "\n</available_skills>\n\n" + PROMPT_FOOT + (COMPACT_NOTE if demoted else ""))


__all__ = ["build", "candidates", "categories", "find", "index_lines", "invalidate",
           "parse_skill_markdown", "render_prompt", "runtime_build", "skill_matches_platform", "slug"]

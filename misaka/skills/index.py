"""The skill index: what a session can load, and how it is advertised.

Port of hermes ``build_skills_system_prompt`` / ``skills_list`` / ``skill_view``
lookups. Like pi and hermes, only each SKILL.md's frontmatter is read -- the body
loads on demand through ``skill_view``. The engine's own skill loading is off for
every MISAKA session: the extension in
:mod:`misaka.extensions.skills` is the one consumer of this index, so a session's
skills are decided in exactly one place.

Caching, as hermes: an in-process cache per roots and disabled list (dropped by
``invalidate()``), and for the personal layers a disk snapshot validated by an
mtime/size manifest of every SKILL.md and DESCRIPTION.md, so a cold start never
re-reads an unchanged tree. Project and external layers are scanned directly.
"""
import hashlib
import json
import os
import re
from pathlib import Path

from misaka.skills.layers import (
    PERSONAL_LAYERS,
    disabled_skill_names,
    home,
    iter_skill_files,
    walk_skill_tree,
)
from misaka.utils.frontmatter import parse_frontmatter

_INVALID = re.compile(r"[^a-z0-9-]")
_MULTI_HYPHEN = re.compile(r"-{2,}")
_CACHE = {}                 # (roots, disabled) -> (entries, categories, candidates)
SNAPSHOT_VERSION = 1

# Cap on description length in the system-prompt skill index (hermes SKILL_PROMPT_DESC_LIMIT).
# The index lives in every session, so longer descriptions are truncated; learn_prompt's hard
# "<= 60 characters" rule comes from this limit.
SKILL_PROMPT_DESC_LIMIT = 60


def truncate_skill_description(description):
    """Description for the index: truncate with an ellipsis past the limit (hermes extract_skill_description)."""
    desc = str(description or "").strip().strip("'\"")
    if len(desc) > SKILL_PROMPT_DESC_LIMIT:
        return desc[: SKILL_PROMPT_DESC_LIMIT - 3] + "..."
    return desc


def is_skill_description_truncated(description):
    """Whether this description would be truncated in the index (used by the linter and /learn)."""
    return len(str(description or "").strip().strip("'\"")) > SKILL_PROMPT_DESC_LIMIT


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

def _frontmatter(path):
    try:
        return parse_frontmatter(Path(path).read_text(encoding="utf-8")).frontmatter or {}
    except Exception:  # noqa: BLE001 - an unreadable or malformed skill still lists by its directory name
        return {}


def _category(parts):
    """``("finance", "fmp-data", "SKILL.md")`` -> ``finance``; right under the root -> general."""
    return "/".join(parts[:-2]) if len(parts) > 2 else "general"


def _scan_root(root):
    """Every skill under one layer root, plus the layer's category descriptions, as stored
    in a snapshot: ``{"skills": [...], "categories": {...}}``."""
    skills, categories = [], {}
    for skill_md in iter_skill_files(root):
        fm = _frontmatter(skill_md)
        rel = skill_md.relative_to(root).parts
        skills.append({"name": str(fm.get("name") or skill_md.parent.name).strip(),
                       "description": truncate_skill_description(str(fm.get("description") or "")),
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
    entries, categories, candidates = [], {}, {}
    for layer, root in roots:
        scanned = _layer(layer, root)
        for skill in scanned["skills"]:
            entry = {**skill, "layer": layer}
            if entry["name"] in disabled or Path(entry["dir"]).name in disabled:
                continue
            candidates.setdefault(entry["name"], []).append(entry)
            if len(candidates[entry["name"]]) == 1:
                entries.append(entry)
        for category, text in scanned["categories"].items():
            categories.setdefault(category, text)
    return entries, categories, candidates


def _key(roots):
    return tuple(roots), tuple(sorted(disabled_skill_names()))


def _cached(roots):
    """Assemble once per (roots, disabled, on-disk state).

    The manifest is the same mtime/size fingerprint the disk snapshots use, so an edit made
    outside this process invalidates the entry by changing the key, and an unchanged tree
    costs one stat per skill file instead of a full re-scan.
    """
    key = (*_key(roots), tuple(sorted((layer, _manifest_digest(root)) for layer, root in roots)))
    entry = _CACHE.get(key)
    if entry is None:
        _CACHE.clear()                       # only the current state is worth keeping
        entry = _CACHE[key] = _assemble(roots, set(key[1]))
    return entry


def _manifest_digest(root):
    """A stable fingerprint of one layer's skill files, cheap enough to take every turn."""
    return hashlib.sha256(
        json.dumps(_manifest(root), sort_keys=True).encode()
    ).hexdigest()


def build(roots):
    """Index entries ``{"name", "description", "category", "layer", "dir", "path"}`` for the
    layer roots (``(layer, root)`` in precedence order): names first-wins, so a project skill
    shadows a role, shared, or external one of the same name; the ``disabled`` list honoured;
    the description cut to the prompt limit; the category read from the path inside the
    layer (``finance/fmp-data`` -> ``finance``, a skill right under the root -> ``general``)."""
    return _cached(roots)[0]


def categories(roots):
    """``{category: description}`` from the layers' DESCRIPTION.md files (hermes)."""
    return _cached(roots)[1]


def _matches(entry, wanted, key):
    """A skill answers to its frontmatter name, its directory path inside the layer
    (``finance/fmp-data``, the unambiguous handle), or the slug of its name."""
    return wanted in (entry["name"], entry["rel"]) or (key and slug(entry["name"]) == key)


def candidates(roots, name):
    """Every skill a name could mean, across the layers -- shadowed ones included.
    ``skill_view`` refuses to guess between them (hermes collision rule); the index itself
    keeps the first."""
    wanted = (name or "").strip()
    key = slug(wanted)
    return [entry for group in _cached(roots)[2].values() for entry in group
            if _matches(entry, wanted, key)]


def find(entries, name):
    """The index entry a name refers to."""
    wanted = (name or "").strip()
    key = slug(wanted)
    return next((entry for entry in entries if _matches(entry, wanted, key)), None)


# ── the system-prompt section ─────────────────────────────────────────────

def index_lines(entries, category_descriptions=None, compact=()):
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
            names = sorted({entry["name"] for entry in by_category[category]})
            lines.append(f"  {category} [names only]: {', '.join(names)}")
            continue
        text = (category_descriptions or {}).get(category, "")
        lines.append(f"  {category}: {text}" if text else f"  {category}:")
        for entry in sorted(by_category[category], key=lambda e: e["name"]):
            desc = entry["description"]
            if entry["layer"] == "project":
                desc = f"[project] {desc}".strip()
            lines.append(f"    - {entry['name']}: {desc}" if desc else f"    - {entry['name']}")
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
           "render_prompt", "slug"]

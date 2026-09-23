"""The skill index: what a session can load, and how it is advertised.

Port of hermes ``build_skills_system_prompt`` / ``skills_list`` / ``skill_view``
lookups. The prompt reads only frontmatter; ``skills_list`` reads the first prose
line only when a description is missing, and the full body otherwise loads on
demand through ``skill_view``. The engine's own skill loading is off for every
MISAKA session: the extension in
:mod:`misaka.core.skills.wiring.skills` is the one consumer of this index, so a session's
skills are decided in exactly one place.

Caching: raw documents per root manifest (dropped by ``invalidate()``); current
disable and relevance rules are evaluated after lookup. Personal layers also
use a versioned disk snapshot keyed by the markdown mtime/size manifest, so a
cold start does not re-parse an unchanged tree. Project entries pass the quarantine scanner after a
whole-bundle fingerprint changes; external layers are scanned directly.
"""
import hashlib
import json
import os
import sys
from pathlib import Path

from misaka.config import home
from misaka.core.skills.layers import (
    PERSONAL_LAYERS,
    disabled_skill_names,
    iter_project_skill_files,
    iter_skill_files,
    parse_skill_name,
    project_skill_tree_fingerprint,
    walk_skill_tree,
)

_CACHE = {}                 # root manifests -> raw documents and category metadata
_CACHE_MAX = 32
SNAPSHOT_VERSION = 7

# Cap on description length in the system-prompt skill index (hermes SKILL_PROMPT_DESC_LIMIT).
# The index lives in every session, so longer descriptions are truncated; learn_prompt's hard
# description-length rule comes from this limit.
from .vendor.metadata import SKILL_PROMPT_DESC_LIMIT

SKILL_LIST_DESC_LIMIT = 1024


def truncate_skill_description(description):
    """Description for the index: truncate with an ellipsis past the limit (hermes extract_skill_description)."""
    from .vendor.metadata import extract_skill_description
    return extract_skill_description({"description": description})


def is_skill_description_truncated(description):
    """Whether this description would be truncated in the index (used by the linter and /learn)."""
    from .vendor.metadata import is_skill_description_truncated_for_prompt
    return is_skill_description_truncated_for_prompt({"description": description})


def parse_skill_markdown(content):
    from .vendor.metadata import parse_frontmatter
    frontmatter, body = parse_frontmatter(content)
    if body != content.removeprefix("\ufeff"):
        return frontmatter, body
    # Keep the already-supported Pi fence spellings readable. Hermes handles
    # all normal input; only the host's older fence grammar is normalized here.
    from misaka.utils.frontmatter import _extract_frontmatter
    yaml_text, legacy_body = _extract_frontmatter(content)
    if yaml_text is not None:
        return parse_frontmatter("---\n" + yaml_text + "\n---\n" + legacy_body)
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
    from .vendor.metadata import skill_matches_platform as matches
    return matches(frontmatter)


def _snapshot_dir():
    return str(home.path("skills_index"))


def slug(name):
    """``Git_Helper`` -> ``git-helper``: how /skill and skill_view accept a name."""
    from .vendor.commands import slugify_skill_name
    return slugify_skill_name(name)


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
        fallback = skill_md.parent.name
        skills.append({"name": str(prompt_fm.get("name") or fallback).strip(),
                       "runtime_name": str(runtime_fm.get("name") or fallback).strip(),
                       "description": truncate_skill_description(str(prompt_fm.get("description") or "")),
                       "list_description": list_skill_description(runtime_fm, runtime_body),
                       "prompt_frontmatter": prompt_fm, "frontmatter": runtime_fm,
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
        for name in files:
            if name.endswith(".md") or name in (".misaka-skill-snapshot.json", ".org-provenance.json", ".active_org"):
                path = os.path.join(here, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                manifest[os.path.relpath(path, root)] = [st.st_mtime_ns, st.st_size]
    return manifest


def _snapshot_path(root):
    digest = hashlib.sha256(os.path.abspath(root).encode()).hexdigest()[:16]
    return os.path.join(_snapshot_dir(), f"{digest}.json")


def _load_snapshot(root, manifest):
    try:
        with open(_snapshot_path(root), encoding="utf-8") as f:
            snapshot = json.load(f)
    except (OSError, ValueError):
        return None
    if (not isinstance(snapshot, dict) or snapshot.get("version") != SNAPSHOT_VERSION
            or snapshot.get("manifest") != manifest
            or snapshot.get("real_root") != os.path.realpath(root)):
        return None
    return snapshot


def _write_snapshot(root, manifest, scanned):
    path = _snapshot_path(root)
    try:
        from misaka.utils.atomic import write_text
        write_text(path, json.dumps({"version": SNAPSHOT_VERSION, "root": str(root),
                                    "real_root": os.path.realpath(root), "manifest": manifest, **scanned}))
    except (OSError, TypeError, ValueError):
        pass                                       # best effort: the next start scans again


def _layer(layer, root):
    """A layer's skills: from its snapshot when the tree is unchanged (personal layers), else
    a scan -- written back as the new snapshot."""
    if layer == "sandbox":
        from .sandbox import read_manifest
        if manifest := read_manifest(root):
            for entry in manifest["entries"]:
                (prompt, _), (runtime, _) = _documents(entry["path"])
                entry["prompt_frontmatter"], entry["frontmatter"] = prompt, runtime
                entry["description"] = truncate_skill_description(str(prompt.get("description") or ""))
            return {"skills": manifest["entries"], "categories": manifest.get("categories", {})}
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

def _assemble(roots):
    categories, all_entries = {}, []
    # A provider label never changes ownership. Reuse the project's admitted
    # collection for aliases (including symlink targets and nested/parent roots),
    # rather than letting a second discovery path bypass its quarantine.
    project_scans = {root: _layer(layer, root) for layer, root in roots if layer == "project"}
    project_roots = [Path(root).resolve() for root in project_scans]
    project_documents = {str(path.resolve()) for root in project_scans for path in iter_skill_files(root)}
    project_admitted = {str(Path(e["path"]).resolve()): e for scan in project_scans.values() for e in scan["skills"]}
    for layer, root in roots:
        scanned = project_scans[root] if layer == "project" else _layer(layer, root)
        for skill in scanned["skills"]:
            owned_path = Path(skill["path"]).resolve()
            project = (str(owned_path) in project_documents or any(owned_path.is_relative_to(p) for p in project_roots))
            owner = project_admitted.get(str(owned_path)) if project else None
            if project and owner is None:
                continue
            namespace = layer.removeprefix("extension:") if layer.startswith("extension:") else None
            entry = {**skill, "identity_path": skill.get("identity_path", os.path.realpath(skill["path"])), "layer": skill.get("origin_layer", "extension" if namespace else layer), "root": str(root),
                     "prompt_compatible": skill_matches_platform(skill.get("prompt_frontmatter", {})),
                     "runtime_compatible": skill_matches_platform(skill.get("frontmatter", {}))}
            if namespace:
                if namespace.startswith("<inline:"):
                    # Use underscore-style public names for inline skills without
                    # rewriting their source documents or directory names.
                    for key in ("name", "runtime_name"):
                        entry[key] = entry[key].replace("-", "_")
                entry.update(namespace=namespace, bare_name=entry["runtime_name"],
                             extension_root=str(root))
                entry["name"] = f"{namespace}:{entry['name']}"
                entry["runtime_name"] = f"{namespace}:{entry['runtime_name']}"
            if owner is not None:
                entry.update(layer="project", origin_layer="project", project_name=owner["runtime_name"], project_rel=owner["rel"])
            if layer != "sandbox":
                from .vendor.org_header import _org_provenance_header
                entry["org_provenance"], entry["org_header"] = _org_provenance_header(Path(entry["dir"]), Path(root))
            all_entries.append(entry)
        for category, text in scanned["categories"].items():
            categories.setdefault(category, text)
    sources, project_sources = {}, {}
    for entry in all_entries:
        if entry.get("runtime_compatible", True):
            for name in {entry["name"], entry.get("runtime_name", entry["name"])}:
                source = os.path.realpath(entry["path"])
                sources.setdefault(name, set()).add(source)
                if entry["layer"] == "project":
                    project_sources.setdefault(name, set()).add(source)
    for entry in all_entries:
        if entry.get("namespace"):
            entry["siblings"] = sorted({e["bare_name"] for e in all_entries
                if e.get("namespace") == entry["namespace"] and e["bare_name"] != entry["bare_name"]})
    for entry in all_entries:
        if any(len(project_sources.get(name) or sources.get(name, ())) > 1 for name in
               (entry["name"], entry.get("runtime_name", entry["name"]))):
            entry["source"] = entry["path"]
    return {"entries": all_entries, "categories": categories}


def _cached(roots):
    """Cache metadata only. Live policy and offer context are evaluated outside it."""
    normalized_roots = tuple((str(layer), str(root)) for layer, root in roots)
    key = (normalized_roots, tuple(os.path.realpath(r) for _, r in normalized_roots), sys.platform,
           os.environ.get("TERMUX_VERSION"), os.environ.get("PREFIX"),
           tuple((layer, _root_fingerprint(layer, root)) for layer, root in normalized_roots))
    entry = _CACHE.get(key)
    if entry is None:
        entry = _CACHE[key] = _assemble(normalized_roots)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
    else:
        _CACHE.pop(key)
        _CACHE[key] = entry
    return entry


def is_disabled(entry, platform="cli", *, disabled=None):
    names = {entry["name"], entry.get("runtime_name", entry["name"])}
    if entry.get("project_name"):
        names.update((entry["project_name"], entry["project_rel"]))
    if entry.get("namespace"):
        names.add(f"{entry['namespace']}:{entry['rel']}")
    else:
        names.update((entry["rel"], Path(entry["dir"]).name, Path(entry.get("origin_dir", entry["dir"])).name))
    return bool(names & (set(disabled_skill_names(platform)) if disabled is None else disabled))



def _manifest_digest(root):
    """A stable fingerprint of one layer's skill files, cheap enough to take every turn."""
    return hashlib.sha256(
        json.dumps(_manifest(root), sort_keys=True).encode()
    ).hexdigest()


def _root_fingerprint(layer, root):
    """Project quarantine tracks whole bundles; other indexes need metadata files only."""
    return (project_skill_tree_fingerprint(root) if layer == "project"
            else _manifest_digest(root))


def _offered_entries(roots, *, prompt, platform="cli", tools=None, toolsets=None, detect=None):
    from .visibility import offered
    seen, out = set(), []
    disabled = set(disabled_skill_names(platform))
    for entry in _cached(roots)["entries"]:
        compatible = "prompt_compatible" if prompt else "runtime_compatible"
        fm = "prompt_frontmatter" if prompt else "frontmatter"
        name = entry["name"] if prompt else entry.get("runtime_name", entry["name"])
        if (not entry.get(compatible, True) or is_disabled(entry, platform, disabled=disabled)
                or not offered(entry.get(fm, {}), tools=tools, toolsets=toolsets, platform=platform,
                               detect=detect, conditions=prompt) or name in seen):
            continue
        seen.add(name)
        out.append(dict(entry))
    return out


def build(roots, *, platform="cli", available_tools=None, available_toolsets=None, detect=None):
    """First visible source wins. Relevance affects advertising, never explicit reads."""
    return _offered_entries(roots, prompt=True, platform=platform, tools=available_tools,
                            toolsets=available_toolsets, detect=detect)


def runtime_build(roots, *, platform="cli", detect=None):
    return _offered_entries(roots, prompt=False, platform=platform, detect=detect)


def categories(roots):
    return dict(_cached(roots)["categories"])


def all_entries(roots, *, platform="cli", include_disabled=False):
    """All admitted read candidates, including unoffered and shadowed objects."""
    disabled = set() if include_disabled else set(disabled_skill_names(platform))
    return [dict(e) for e in _cached(roots)["entries"] if e.get("runtime_compatible", True) and not is_disabled(e, platform, disabled=disabled)]


def candidates(roots, name, *, platform="cli", include_disabled=False):
    from .vendor.metadata import is_valid_namespace
    raw = (name or "").strip()
    disabled = set() if include_disabled else set(disabled_skill_names(platform))
    entries = [e for e in _cached(roots)["entries"] if not is_disabled(e, platform, disabled=disabled)]
    namespace, bare = parse_skill_name(raw)
    if namespace is not None:
        # A known provider owns its prefix even if this particular skill is absent.
        providers = [e for e in _cached(roots)["entries"] if e.get("namespace") == namespace]
        if providers:
            return [e for e in entries if e.get("namespace") == namespace and bare in
                    (e.get("bare_name"), e["rel"], Path(e["dir"]).name)]
        if not is_valid_namespace(namespace):
            return []
    wanted = raw.replace(":", "/")
    key = slug(wanted)
    exact = [e for e in entries if wanted in (e["name"], e.get("runtime_name", e["name"]), e["rel"], e.get("bare_name"))]
    found = exact or [e for e in entries if key and any(slug(n) == key for n in
        (e["name"], e.get("runtime_name", e["name"]), e.get("bare_name", "")))]
    # Preserve old unqualified extension references only when no local skill
    # owns that spelling. Normal advertised extension names are qualified.
    return [e for e in found if not e.get("namespace")] or found


def find(entries, name):
    """The index entry a name refers to."""
    wanted = (name or "").strip().replace(":", "/")
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
    def label(entry):
        name = entry.get(name_key, entry["name"])
        return f"{name} [source={entry['source']}]" if entry.get("source") else name

    lines = []
    for category in sorted(by_category):
        if category.split("/", 1)[0] in compact:
            names = sorted({label(entry) for entry in by_category[category]})
            lines.append(f"  {category} [names only]: {', '.join(names)}")
            continue
        text = (category_descriptions or {}).get(category, "")
        lines.append(f"  {category}: {text}" if text else f"  {category}:")
        for entry in sorted(by_category[category], key=lambda e: e.get(name_key, e["name"])):
            name = label(entry)
            desc = entry.get(description_key, entry["description"])
            if entry["layer"] == "project":
                desc = f"[project] {desc}".strip()
            lines.append(f"    - {name}: {desc}" if desc else f"    - {name}")
    return lines


def render_prompt(entries, category_descriptions=None, compact=(), *, can_manage=True, available_tools=None):
    from .vendor.visibility import _render_skills_index
    grouped = {}
    for entry in entries:
        name = entry["name"]
        if entry.get("source"):
            name += f" [source={entry['source']}]"
        desc = entry["description"]
        if entry["layer"] == "project":
            desc = f"[project] {desc}".strip()
        grouped.setdefault(entry["category"], []).append((name, desc))
    result = _render_skills_index(grouped, category_descriptions or {}, compact, available_tools,
                                 can_manage=can_manage)
    if result and any(e.get("source") for e in entries):
        result = result.replace("## Skills\n", "## Skills\nFor entries marked source=..., pass that path as source to skill_view to resolve the name.\n", 1)
    return result


__all__ = ["SKILL_PROMPT_DESC_LIMIT", "build", "candidates", "categories", "find", "index_lines", "invalidate",
           "parse_skill_markdown", "render_prompt", "runtime_build", "skill_matches_platform", "slug"]


def resolve(roots, name, *, source=None, platform="cli", include_disabled=False, require_compatible=True):
    """Select one admitted identity. Absolute references match indexed origins only."""
    wanted = str(name or "").strip()
    if source or Path(wanted).expanduser().is_absolute():
        selected = os.path.abspath(os.path.expanduser(source or wanted))
        found = [e for e in _cached(roots)["entries"] if selected in {
            e["path"], e["dir"], e.get("origin_path"), e.get("origin_dir"),
            os.path.realpath(e["path"]), os.path.realpath(e["dir"])}]
        if source:
            ids = {e["path"] for e in candidates(roots, wanted, platform=platform, include_disabled=include_disabled)}
            found = [e for e in found if e["path"] in ids]
    else:
        found = candidates(roots, wanted, platform=platform, include_disabled=include_disabled)
        if require_compatible:
            found = [e for e in found if e.get("runtime_compatible", True)] or found
        exact = [e for e in found if e["rel"] == wanted.replace(":", "/")]
        found = exact or found
        found = [e for e in found if e["layer"] == "project"] or found
    unique = {e.get("identity_path", os.path.realpath(e["path"])): e for e in found}
    if len(unique) > 1:
        return None, f"Ambiguous skill name '{name}': pass source with one exact SKILL.md path: " + "; ".join(e["path"] for e in unique.values())
    entry = next(iter(unique.values()), None)
    if entry is None:
        return None, f"Unknown skill '{name}'."
    if not include_disabled and is_disabled(entry, platform):
        return None, f"Skill '{name}' is disabled."
    if require_compatible and not entry.get("runtime_compatible", True):
        return None, f"Skill '{name}' is not supported on this platform."
    return dict(entry), None


def resolve_definition(roots, name, base_dir=None, *, platform="cli"):
    """Trusted definition-local compatibility, never a disabled-layer escape hatch."""
    entry, error = resolve(roots, name, platform=platform)
    if entry is not None or not base_dir or any(layer == "sandbox" for layer, _ in roots):
        return entry, error
    direct = Path(name).expanduser()
    if not direct.is_absolute():
        direct = Path(base_dir) / direct
    path = direct / "SKILL.md" if direct.is_dir() else direct
    if not path.is_file() or path.suffix.lower() != ".md":
        return None, error
    if any(path.resolve().is_relative_to(Path(root).resolve()) for _, root in roots):
        return None, error
    fm, body = parse_skill_markdown(path.read_text(encoding="utf-8-sig", errors="replace"))
    canonical = str(fm.get("name") or path.parent.name)
    if {canonical, path.parent.name, name} & set(disabled_skill_names(platform)) or not skill_matches_platform(fm):
        return None, error
    return {"name": canonical, "runtime_name": canonical, "path": str(path), "dir": str(path.parent),
            "layer": "definition", "rel": path.parent.name, "frontmatter": fm,
            "list_description": list_skill_description(fm, body)}, None

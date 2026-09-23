"""Explicit role/project ownership for transplanted Skill maintenance functions."""
import contextvars
import json
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from misaka.config import home
from misaka.utils import atomic

from . import index, layers

_current = contextvars.ContextVar("misaka_skill_scope", default=None)
_event = contextvars.ContextVar("misaka_skill_event", default=None)
_actor = contextvars.ContextVar("misaka_skill_actor", default="foreground")
ESSENTIAL_SKILLS = frozenset()


@dataclass
class SkillScope:
    profile: Path
    workspace: Path
    roots: list | None = None
    storage: Path | None = None
    bundled_root: Path | None = None
    optional_root: Path | None = None
    environment: dict = field(default_factory=lambda: dict(os.environ))
    origin: str = "foreground"
    generation: str | None = "__capture__"
    hooks: dict = field(default_factory=dict)
    review: object = None
    model: object = None
    model_registry: object = None
    deadline: float | None = None
    stop: object = field(default_factory=threading.Event, repr=False)
    references: object = None
    read_marks: object = None

    def __post_init__(self):
        self.profile = Path(self.profile).expanduser().absolute() if self.profile is not None else None
        self.workspace = Path(self.workspace).expanduser().absolute()
        if self.generation == "__capture__":
            from .release import token
            self.generation = token(self.profile)


@contextmanager
def using_scope(scope, *, event=None):
    from .vendor import skill_manager_guards as guards
    from .vendor import skill_provenance as provenance
    if scope.read_marks is None:
        scope.read_marks = guards._BackgroundReviewReadMarks()
    token = _current.set(scope)
    origin = provenance.set_current_write_origin(scope.origin)
    marks = guards._background_review_read_paths.set(scope.read_marks)
    ev = _event.set(event)
    try:
        yield scope
    finally:
        _event.reset(ev)
        guards._background_review_read_paths.reset(marks)
        provenance.reset_current_write_origin(origin)
        _current.reset(token)


def current_scope():
    scope = _current.get()
    if scope is None:
        raise RuntimeError("Skill maintenance requires an explicit role scope.")
    return scope


def scope_for(profile, workspace):
    """Reuse owner state only when both explicit role and project match."""
    profile = Path(profile).expanduser().absolute() if profile is not None else None
    workspace = Path(workspace).expanduser().absolute()
    scope = _current.get()
    if scope is not None and scope.profile == profile and scope.workspace == workspace:
        return scope
    from .vendor.skill_provenance import get_current_write_origin
    # Actor restrictions and cancellation still apply across an explicit retarget;
    # role storage, pins, references and read approvals must be resolved anew.
    controls = {"origin": get_current_write_origin()}
    if scope is not None:
        controls.update(deadline=scope.deadline, stop=scope.stop)
    return SkillScope(profile, workspace, **controls)


def check_active(scope):
    import time
    if scope.stop.is_set():
        raise RuntimeError("Skill distribution owner stopped.")
    if scope.deadline is not None and time.monotonic() >= scope.deadline:
        raise TimeoutError("Skill distribution operation deadline exceeded.")


def event_id():
    return _event.get()


USAGE_EVENTS_KEY = "\0misaka.ledger"  # Not a legal Skill identifier; native readers skip list-valued metadata.


def usage_events():
    path = _skills_dir() / ".usage.json"
    if not path.exists():
        return set()
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise TypeError("Invalid usage data; existing bytes preserved")
    events = data.get(USAGE_EVENTS_KEY, [])
    if not isinstance(events, list) or any(not isinstance(item, str) for item in events):
        raise ValueError("Invalid usage replay checkpoint; existing bytes preserved")
    return set(events)


def locally_deleted_skills():
    """Image transitions are the deletion authority, including restore/rollback, not basename guesses."""
    from .write import entries
    role = current_scope().profile / 'skills'
    deleted = set()
    for row in entries():
        for change in row.get('changes') or []:
            root = Path(change['root'])
            if not root.is_relative_to(role):
                continue
            images = []
            for side in ('before', 'after'):
                names = {(root / item['path']).parent.relative_to(role).as_posix()
                         for item in change.get(side, {}).get('files', []) if Path(item['path']).name == 'SKILL.md'}
                images.append({name for name in names if not name.startswith('.')})
            before, after = images
            deleted.update(before - after)
            deleted.difference_update(after)
    return deleted


def profile_home():
    root = current_scope().storage or current_scope().profile
    if root is None:
        raise ValueError("Skill maintenance requires a role profile.")
    return root


def _skills_dir():
    return profile_home() / "skills"


def load_config():
    cfg = layers.load_skills_config()
    return {**cfg, "skills": cfg, "agent": {"coding_context": cfg.get("coding_context", "auto")}}


def atomic_write_text(path, content, **kwargs):
    kwargs.pop("tmp_prefix", None)
    kwargs.pop("preserve_mode", None)
    create_mode = kwargs.pop("create_mode", None)
    if create_mode is not None and not Path(path).exists():
        kwargs["mode"] = create_mode
    from .release import check_write
    from .write import _safe_parents, mutation_lock
    path = Path(path)
    with mutation_lock():
        check_write(current_scope().profile)
        _safe_parents(path.parent)
        if path.is_symlink():
            raise ValueError("Skill state target is a symlink: " + str(path))
        if (path.name in (".usage.json", ".curator_state", ".sync_state", ".sync_manifest")
            and path.exists() and not isinstance(json.loads(path.read_text()), dict)):
            raise ValueError("Invalid Skill state; existing bytes preserved: " + str(path))
        return atomic.write_text(path, content, **kwargs)


def atomic_json_write(path, data, **kwargs):
    return atomic_write_text(path, json.dumps(data, ensure_ascii=False, **kwargs))


def get_all_skills_dirs():
    scope = current_scope()
    roots = scope.roots if scope.roots is not None else layers.skill_roots(str(scope.profile), str(scope.workspace))
    return [_skills_dir(), *dict.fromkeys(Path(root) for _, root in roots if Path(root) != scope.profile / "skills")]


def get_external_skills_dirs():
    return get_all_skills_dirs()[1:]


def is_external_skill_path(path):
    return not Path(path).resolve().is_relative_to(_skills_dir().resolve())


def is_excluded_skill_path(path, root=None):
    return bool(set(Path(path).parts) & layers.EXCLUDED_SKILL_DIRS)


def iter_skill_index_files(root, filename="SKILL.md"):
    return layers.iter_skill_files(root, filename)


def _find_skill(name):
    return find_skill(name)


def find_skill(name):
    entry, error = index.resolve([("role", str(_skills_dir()))], name, include_disabled=True, require_compatible=False)
    if error:
        if "Ambiguous" in error:
            raise ValueError(error)
        return None
    return {**entry, "path": Path(entry["dir"])}


def is_org_mirror_path(path, root=None):
    root = Path(root) if root is not None else _skills_dir()
    return Path(path).resolve().is_relative_to((root / "_org").resolve())


def get_plugin_manager():
    return current_scope().hooks["plugins"]


def has_hook(name):
    return name in current_scope().hooks


def invoke_hook(name, **kwargs):
    return current_scope().hooks[name](**kwargs)


def set_ledger_actor(actor):
    return _actor.set(actor)


def reset_ledger_actor(token):
    _actor.reset(token)


def referenced_skill_names():
    scope = current_scope()
    # Protect raw declarations AND all matching physical role identities. Ambiguous
    # names are never guessed, and staged removal cannot erase its own references.
    result = set(scope.references.referenced_skill_names()) if scope.references is not None else set()
    project_dir = home.project_dir(scope.workspace)
    for root in (home.path("subagents", scope.profile),
                 project_dir / home.SUBAGENTS_DIR if project_dir is not None else None):
        if root is None or not root.is_dir():
            continue
        for path in root.rglob("*.md"):
            fm, _ = index.parse_skill_markdown(path.read_text(encoding="utf-8-sig"))
            names = fm.get("skills", [])
            names = names if isinstance(names, list) else [names] if isinstance(names, str) else []
            result.update(n for n in names if isinstance(n, str))
    roots = [("role", str(scope.profile / "skills"))]
    for name in list(result):
        if Path(name).expanduser().is_absolute():
            entry, _ = index.resolve(roots, name, include_disabled=True, require_compatible=False)
            if entry:
                result.add(entry["rel"])
        else:
            result.update(entry["rel"] for entry in index.candidates(roots, name, include_disabled=True))
    return result


def rewrite_skill_refs(*, consolidated, pruned):
    refs = current_scope().references
    if refs is not None:
        return refs.rewrite_skill_refs(consolidated=consolidated, pruned=pruned)
    # No scheduler is fabricated. Referenced Skills are protected BEFORE removal;
    # fixed card snapshots do not need their dispatch metadata rewritten.
    affected = referenced_skill_names() & (set(consolidated) | set(pruned))
    return {"rewrites": [], "jobs_updated": 0, "jobs_scanned": 0,
            **({"error": "Protected definition references remain: " + ", ".join(sorted(affected))} if affected else {})}


def snapshot_references(dest):
    refs = current_scope().references
    if refs is not None:
        return refs.snapshot(dest)
    return {"backed_up": False, "jobs_count": 0, "reason": "MISAKA fixed Board snapshots; definitions protected, not rewritten"}


def restore_references(source):
    refs = current_scope().references
    if refs is not None:
        return refs.restore(source)
    return {"restored": False, "jobs_updated": 0, "reason": "No mutable scheduler references in this adapter"}


get_hermes_home = profile_home


def get_secret(name, default=None):
    from .runtime import SkillRuntime
    scope = current_scope()
    return SkillRuntime(scope.profile, env=scope.environment).load_env().get(name, default)


def _asset_dir(kind):
    scope = current_scope()
    configured = getattr(scope, kind + "_root") or load_config().get(kind + "_dir")
    root = Path(configured).expanduser().absolute() if configured else Path(__file__).parent / "assets" / kind
    if not root.is_dir():
        raise ValueError("Missing explicit Skill " + kind + " source: " + str(root))
    return root


def get_bundled_skills_dir(default=None):
    return _asset_dir("bundled")


def get_optional_skills_dir(default=None):
    return _asset_dir("optional")


def _external_dirs_cache_clear():
    index.invalidate()


from .vendor.host_match import (
    base_url_host_matches as base_url_host_matches,  # noqa: PLC0414 - public compatibility export
)
from .vendor.org_visibility import (
    read_active_org_id as read_active_org_id,  # noqa: PLC0414 - public compatibility export
)


def manifest_names(names):
    """Project a native basename registry onto MISAKA's unambiguous role-relative IDs."""
    from .vendor.skill_usage import _read_skill_name
    root = _skills_dir()
    matches = {}
    for md in iter_skill_index_files(root):
        matches.setdefault(_read_skill_name(md, md.parent.name), []).append(md.parent.relative_to(root).as_posix())
    result = set(names)
    for name in names:
        paths = matches.get(name, [])
        if len(paths) > 1:
            # A basename registry cannot establish which same-name source it owns.
            # All candidates stay protected until provenance is made explicit.
            result.update(paths)
        elif paths:
            result.add(paths[0])
    return result

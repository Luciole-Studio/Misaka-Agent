"""User-controlled skill write gate, provenance, ledger, and rollback support."""
import contextvars
import hashlib
import json
import os
import time
import uuid
from pathlib import Path

WRITE_MODES = ("off", "forbid", "ask", "allow")
DEFAULT_WRITE_MODE = "forbid"

_ORIGIN = contextvars.ContextVar("misaka_skill_write_origin", default="user")


def _root():
    return Path(os.path.expanduser("~/.misaka"))


def _config():
    try:
        with open(_root() / "skills.json", encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_mode():
    """Return the current mode, defaulting safely when configuration is invalid."""
    mode = str(_config().get("skill_write_mode") or DEFAULT_WRITE_MODE).strip().lower()
    return mode if mode in WRITE_MODES else DEFAULT_WRITE_MODE


# Origin tracking

def set_origin(origin):
    """Set the write origin for this context and return its reset token."""
    return _ORIGIN.set(str(origin or "user"))


def reset_origin(token):
    _ORIGIN.reset(token)


def current_origin():
    """Return the actor responsible for the current write."""
    return _ORIGIN.get()


# Review ledger

def _ledger_path():
    return _root() / "skills" / ".ledger.jsonl"


def _blob_dir():
    return _root() / "cache" / "skill_blobs"


def _store_blob(path):
    """Store a file by content hash and return its SHA-256 digest."""
    data = Path(path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    blob = _blob_dir() / digest
    if not blob.exists():
        blob.parent.mkdir(parents=True, exist_ok=True)
        tmp = blob.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, blob)
    return digest


def snapshot(root):
    """Snapshot a skill directory as relative paths and content hashes."""
    root = Path(root)
    if not root.is_dir():
        return []
    out = []
    for f in sorted(root.rglob("*")):
        if f.is_file() and not f.is_symlink():
            out.append({"path": str(f.relative_to(root)), "sha256": _store_blob(f)})
    return out


def record(action, skill, *, before=None, after_root=None, evidence=None):
    """Record a change without allowing ledger failure to block the change itself."""
    try:
        entry = {
            "id": uuid.uuid4().hex[:12],
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "actor": current_origin(),
            "action": action,
            "skill": str(skill),
            "evidence": evidence or {},
            "before": before if before is not None else [],
            "after": snapshot(after_root) if after_root else [],
        }
        path = _ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry["id"]
    except Exception:  # noqa: BLE001 - ledger telemetry is not a write gate
        return None


def entries(limit=None):
    """Read valid ledger entries from oldest to newest."""
    out = []
    try:
        with open(_ledger_path(), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return out[-limit:] if limit else out


def rollback(entry_id, skill_root):
    """Restore a skill directory to the ``before`` snapshot of a ledger entry."""
    target = next((e for e in entries() if e["id"] == entry_id), None)
    if target is None:
        return False, f"Skill ledger entry not found: {entry_id}"
    root = Path(skill_root)
    # Verify every required blob before changing the live skill.
    for item in target["before"]:
        if not (_blob_dir() / item["sha256"]).exists():
            return False, f"Missing rollback blob for {item['path']} ({item['sha256'][:12]})."
    if root.exists() and not str(root.resolve()).startswith(str(_root().resolve())):
        return False, "Rollback target must be inside ~/.misaka."
    record("pre-rollback", target["skill"], after_root=root,
           evidence={"rollback_of": entry_id})
    root.mkdir(parents=True, exist_ok=True)
    keep = set()
    for item in target["before"]:
        dest = root / item["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((_blob_dir() / item["sha256"]).read_bytes())
        keep.add(dest.resolve())
    for item in target["after"]:
        dest = root / item["path"]
        if dest.is_file() and dest.resolve() not in keep:
            dest.unlink()
    record("rollback", target["skill"], after_root=root,
           evidence={"rollback_of": entry_id})
    return True, f"Rolled back {target['skill']} ({len(target['before'])} files)."


# ── Write gate (after hermes write_approval.py) ─────────────────────────────

def _pending_dir():
    return _root() / "pending" / "skills"


def evaluate_gate():
    """Return the configured write decision and a user-facing explanation."""
    mode = write_mode()
    if mode == "allow":
        return "allow", ""
    if mode == "off":
        return "off", (
            "Skill writing is disabled globally by the user. Do not retry or bypass the gate with shell or file tools. "
            "Only the user may enable it again."
        )
    return "stage", (
        f"Skill writing is pending user review (skill_write_mode={mode}); nothing was written to the live skill tree. "
        "The user can inspect it with `misaka skills pending` and approve it with `misaka skills approve <id>`."
    )


def stage(payload, *, summary):
    """Save a pending skill write for user review and return the pending record."""
    record_id = uuid.uuid4().hex[:8]
    item = {
        "id": record_id,
        "summary": (summary or "").strip(),
        "origin": current_origin(),
        "created_at": time.time(),
        "payload": payload,
    }
    d = _pending_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{record_id}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return item


def list_pending():
    try:
        files = sorted(_pending_dir().glob("*.json"))
    except OSError:
        return []
    out = []
    for f in files:
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda r: r.get("created_at", 0))


def get_pending(pending_id):
    return next((r for r in list_pending() if r["id"] == pending_id), None)


def discard_pending(pending_id):
    try:
        (_pending_dir() / f"{pending_id}.json").unlink()
        return True
    except OSError:
        return False


def pending_diff(item):
    """Unified diff between the live skill tree and the pending write, for the user to review."""
    from misaka.skills import manage
    return manage.pending_diff(item.get("payload") or {})

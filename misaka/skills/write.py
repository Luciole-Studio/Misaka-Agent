"""User-controlled skill workflow, provenance, ledger, and rollback support.

This is not an OS security boundary: a process with unrestricted shell access can edit
its environment and the same files directly.
"""
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from pathlib import Path

from misaka.utils import atomic

WRITE_MODES = ("off", "forbid", "ask", "allow")
DEFAULT_WRITE_MODE = "forbid"
MUTATING_ACTIONS = ("create", "edit", "patch", "delete", "write_file", "remove_file", "approve")


def agent_session():
    """Best-effort workflow marker set by Sister, card, and child processes."""
    return bool(os.environ.get("MISAKA_WHO") or os.environ.get("MISAKA_USAGE_TASK_ID"))

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


def current_origin():
    """Return the process role responsible for the current write."""
    return "agent" if agent_session() else "user"


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
        atomic.write_bytes(blob, data)
    return digest


def snapshot(root, *, store=True):
    """A skill directory as relative paths and content hashes.

    ``store`` writes each file into the rollback blob store. Taking a fingerprint does not
    need that, and doing it anyway meant every staged or approved write grew an ungarbaged
    store by a copy of the whole tree.
    """
    root = Path(root)
    if not root.is_dir():
        return []
    out = []
    for f in sorted(root.rglob("*")):                     # rglob does not descend symlinked dirs
        rel = str(f.relative_to(root))
        if f.is_symlink():
            # Recorded rather than followed. A role's skill is often a link into a library
            # (layers.py:84), so a snapshot that did not name the link left `restore` with
            # nothing to put back after its cleanup pass unlinked it.
            out.append({"path": rel, "symlink": os.readlink(f)})
        elif f.is_file():
            sha = _store_blob(f) if store else hashlib.sha256(f.read_bytes()).hexdigest()
            # The permission bits are content too: scripts/*.sh that came back without its
            # executable bit is a rollback that did not roll back.
            out.append({"path": rel, "sha256": sha, "mode": f.stat().st_mode & 0o777})
    return out


def digest(root):
    """One hash of a skill tree's content: what a pending write was reviewed against, checked
    again at approval so a tree that changed in between is not patched blind. Read-only."""
    return hashlib.sha256(json.dumps(snapshot(root, store=False), sort_keys=True).encode()).hexdigest()


BLOB_LIMIT = 500        # beyond this, sweep what no ledger entry can restore


def _collect_blobs():
    """Delete blobs no ledger entry references. Rollback keeps only what it can name."""
    try:
        blobs = {path.name for path in _blob_dir().iterdir() if path.is_file()}
    except OSError:
        return
    if len(blobs) <= BLOB_LIMIT:
        return
    referenced = {item["sha256"] for entry in entries()
                  for item in (entry.get("before") or []) + (entry.get("after") or [])
                  if isinstance(item, dict) and item.get("sha256")}
    for name in blobs - referenced:
        try:
            (_blob_dir() / name).unlink()
        except OSError:                    # one stubborn blob must not stop the sweep
            continue


def restore(root, before):
    """Put a skill tree back to a snapshot: its files from the blobs, everything else gone."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    keep = set()
    for item in before:
        dest = root / item["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        if item.get("symlink") is not None:
            if dest.is_symlink() or dest.exists():
                dest.unlink()
            os.symlink(item["symlink"], dest)
        else:
            dest.write_bytes((_blob_dir() / item["sha256"]).read_bytes())
            if item.get("mode") is not None:              # absent in ledger entries from before this
                os.chmod(dest, int(item["mode"]))
        # Relative paths, not `resolve()`: resolving a restored symlink would name its target
        # outside the tree, and the cleanup below would then delete the link it just made.
        keep.add(str(Path(item["path"])))
    for f in sorted(root.rglob("*"), reverse=True):       # deepest first, so emptied directories go too
        if str(f.relative_to(root)) in keep:
            continue
        if f.is_symlink() or f.is_file():                 # is_symlink first: is_file/is_dir follow it,
            f.unlink()                                    # and rmdir on a link to a dir raises
        elif f.is_dir() and not any(f.iterdir()):
            f.rmdir()
    if not before and not any(root.iterdir()):
        root.rmdir()


def mutation_lock():
    """The one lock every live-skill mutation (apply, approve, rollback) runs under, so snapshot,
    write, scan and ledger happen as a unit. ponytail: one lock for all roles; per role if it contends."""
    from filelock import FileLock
    path = _root() / "skills" / ".write.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(str(path))


def record(action, skill, *, before=None, after_root=None, evidence=None):
    """Append a change to the ledger and return the entry id. Raises OSError when the ledger
    cannot be written: callers say so instead of pretending the change was recorded."""
    entry = {
        "id": uuid.uuid4().hex[:12],
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "actor": current_origin(),
        "action": action,
        "skill": str(skill),
        "evidence": evidence or {},
        "before": before if before is not None else [],
        "after": snapshot(after_root) if after_root else [],
        "rollbackable": action in MUTATING_ACTIONS,     # bookkeeping entries restore nothing
    }
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    _collect_blobs()
    return entry["id"]


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
    if not target.get("rollbackable", target["action"] in MUTATING_ACTIONS):
        return False, f"Ledger entry {entry_id} records '{target['action']}', not a change that can be rolled back."
    root = Path(skill_root)
    try:
        root.resolve().relative_to(_root().resolve())    # whether or not it exists yet
    except ValueError:
        return False, "Rollback target must be inside ~/.misaka."
    # Verify every required blob before changing the live skill.
    for item in target["before"]:
        if item.get("symlink") is not None:
            continue            # a link is restored from its recorded target, not from a blob
        if not (_blob_dir() / item["sha256"]).exists():
            return False, f"Missing rollback blob for {item['path']} ({item['sha256'][:12]})."
    with mutation_lock():
        try:
            record("pre-rollback", target["skill"], after_root=root, evidence={"rollback_of": entry_id})
        except OSError as error:
            return False, f"The skill ledger cannot be written ({error}); nothing was rolled back."
        restore(root, target["before"])
        try:
            record("rollback", target["skill"], after_root=root, evidence={"rollback_of": entry_id})
        except OSError as error:
            return True, f"Rolled back {target['skill']} ({len(target['before'])} files), but the ledger could not record it: {error}"
    return True, f"Rolled back {target['skill']} ({len(target['before'])} files)."


# ── Write gate (after hermes write_approval.py) ─────────────────────────────

def _pending_dir():
    return _root() / "pending" / "skills"


_PENDING_FILE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_PAYLOAD_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def canonical_payload(payload):
    """Canonical JSON bytes used by review, approval, and execution."""
    if not isinstance(payload, dict):
        raise TypeError("Pending skill payload must be an object.")
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def payload_sha256(payload):
    return hashlib.sha256(canonical_payload(payload)).hexdigest()


def pending_integrity_error(item, *, file_id=None):
    """Return why a pending record is not bound to its payload, else ``None``."""
    if not isinstance(item, dict):
        return "Pending skill record is not an object."
    expected = item.get("payload_sha256")
    if not isinstance(expected, str) or not _PAYLOAD_SHA256.fullmatch(expected):
        return "Pending skill record has no valid payload SHA-256."
    try:
        actual = payload_sha256(item.get("payload"))
    except (TypeError, ValueError):
        return "Pending skill payload is not valid canonical JSON."
    if not hmac.compare_digest(actual, expected):
        return "Pending skill payload changed after it was staged."
    if item.get("id") != expected or (file_id is not None and file_id != expected):
        return "Pending skill record ID does not match its payload SHA-256."
    return None


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
    record_id = payload_sha256(payload)
    item = {
        "id": record_id,
        "payload_sha256": record_id,
        "summary": (summary or "").strip(),
        "origin": current_origin(),
        "created_at": time.time(),
        "payload": payload,
    }
    d = _pending_dir()
    d.mkdir(parents=True, exist_ok=True)
    atomic.write_text(d / f"{record_id}.json", json.dumps(item, ensure_ascii=False, indent=2))
    return item


def list_pending():
    try:
        files = sorted(_pending_dir().glob("*.json"))
    except OSError:
        return []
    out = []
    for f in files:
        try:
            item = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(item, dict):
            continue
        item["_pending_file_id"] = f.stem
        item["_integrity_error"] = pending_integrity_error(item, file_id=f.stem)
        out.append(item)
    return sorted(out, key=lambda r: r.get("created_at", 0)
                  if isinstance(r.get("created_at", 0), (int, float)) else 0)


def get_pending(pending_id):
    return next((r for r in list_pending()
                 if r.get("_pending_file_id") == pending_id or r.get("id") == pending_id), None)


def discard_pending(pending_id):
    if not isinstance(pending_id, str) or not _PENDING_FILE_ID.fullmatch(pending_id):
        return False
    try:
        (_pending_dir() / f"{pending_id}.json").unlink()
        return True
    except OSError:
        return False


def pending_diff(item):
    """Unified diff between the live skill tree and the pending write, for the user to review."""
    from misaka.skills import manage
    error = pending_integrity_error(
        item, file_id=item.get("_pending_file_id") if isinstance(item, dict) else None)
    if error:
        return f"(cannot preview: {error})"
    return manage.pending_diff(item.get("payload") or {})

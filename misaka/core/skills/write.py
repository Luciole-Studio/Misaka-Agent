"""User-controlled skill workflow, provenance, ledger, and rollback support.

This is not an OS security boundary: a process with unrestricted shell access can edit
its environment and the same files directly.
"""
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
from pathlib import Path

from misaka.config import home
from misaka.utils import atomic

WRITE_MODES = ("off", "forbid", "ask", "allow")
DEFAULT_WRITE_MODE = "forbid"
MUTATING_ACTIONS = ("create", "edit", "patch", "delete", "write_file", "remove_file", "approve")


def agent_session():
    """Best-effort workflow marker set by Sister, card, and child processes."""
    return bool(os.environ.get("MISAKA_WHO") or os.environ.get("MISAKA_USAGE_TASK_ID"))


def _config():
    from .layers import load_skills_config
    return load_skills_config()


def write_mode():
    """Return the current mode, defaulting safely when configuration is invalid."""
    mode = str(_config().get("skill_write_mode") or DEFAULT_WRITE_MODE).strip().lower()
    return mode if mode in WRITE_MODES else DEFAULT_WRITE_MODE


def current_origin():
    """Return the process role responsible for the current write."""
    return "agent" if agent_session() else "user"


# Review ledger

def _ledger_path():
    return home.path("skills_state") / ".ledger.jsonl"


def _blob_dir():
    return home.path("skill_blobs")


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
    for entry in entries():
        referenced.update(item["sha256"] for change in entry.get("changes") or []
                          for side in ("before", "after")
                          for item in change.get(side, {}).get("files", []) if item.get("sha256"))
    for path in _journal_dir().glob("*.json"):
        try:
            journal = json.loads(path.read_text())
            referenced.update(item["sha256"] for change in journal["changes"]
                              for side in ("before", "after")
                              for item in change.get(side, {}).get("files", []) if item.get("sha256"))
        except (OSError, ValueError, KeyError, TypeError):
            return  # Fail closed: an unreadable journal may be the only reference.
    for pending in list_pending():
        if pending.get("_integrity_error"):
            return  # Corrupt review may be the only remaining reference.
        payload = pending.get("payload") or {}
        for side in ("before", "after"):
            referenced.update(item["sha256"] for item in payload.get(side, {}).get("files", []) if item.get("sha256"))
    for name in blobs - referenced:
        try:
            (_blob_dir() / name).unlink()
        except OSError:                    # one stubborn blob must not stop the sweep
            continue


def _safe_parents(path, *, create=False):
    """Reject mutable symlink ancestors, including a redirected managed root."""
    path = Path(os.path.abspath(path))
    for component in reversed((path, *path.parents)):
        # macOS's OS-owned /var and /tmp aliases are not managed tree links.
        if str(component) in ("/var", "/tmp") and os.path.realpath(component) in ("/private/var", "/private/tmp"):
            continue
        try:
            mode = component.lstat().st_mode
        except FileNotFoundError:
            if not create:
                continue
            component.mkdir()
            mode = component.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise ValueError(f"Skill directory ancestry is not a real directory: {component}")


def _checked_snapshot(before):
    """Validate ALL paths/blobs before touching live data; never follow snapshot links."""
    from .manage import lookup_path_error
    if not isinstance(before, list):
        raise TypeError("Skill snapshot must be a list.")
    paths = set()
    for item in before:
        if not isinstance(item, dict) or lookup_path_error(item.get("path")):
            raise ValueError("Invalid path in skill snapshot.")
        rel = Path(item["path"])
        if str(rel) in ("", ".") or "\\" in str(rel) or str(rel) in paths:
            raise ValueError("Duplicate or invalid path in skill snapshot.")
        if any(str(parent) in paths for parent in rel.parents):
            raise ValueError("A snapshot file is another file's parent.")
        paths.add(str(rel))
        mode = item.get("mode", 0o644)
        if type(mode) is not int or mode < 0 or mode > 0o777:
            raise ValueError("Invalid mode in skill snapshot.")
        if item.get("symlink") is not None:
            if not isinstance(item["symlink"], str) or "\0" in item["symlink"]:
                raise ValueError("Invalid symlink in skill snapshot.")
            continue
        sha = item.get("sha256")
        if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise ValueError("Invalid blob identity in skill snapshot.")
        blob = _blob_dir() / sha
        if blob.is_symlink():
            raise ValueError(f"Rollback blob is a symlink: {sha}")
        data = blob.read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            raise ValueError(f"Corrupt rollback blob: {sha}")
    for rel in paths:
        if any(str(parent) in paths for parent in Path(rel).parents):
            raise ValueError("A snapshot file is another file's parent.")


def _remove_tree(path):
    """Clean only an owned detached tree; never chmod through symlinks."""
    path = Path(path)
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        for root, dirs, _files in os.walk(path):
            if not Path(root).is_symlink():
                os.chmod(root, stat.S_IMODE(os.stat(root).st_mode) | 0o700)
        shutil.rmtree(path)


def tree_image(root, *, store=True):
    root = Path(root)
    _safe_parents(root)
    exists = root.is_dir()
    return {"exists": exists, "files": snapshot(root, store=store),
            "mode": stat.S_IMODE(root.stat().st_mode) if exists else 0o755,
            "directories": {str(p.relative_to(root)): stat.S_IMODE(p.stat().st_mode)
                            for p in sorted(root.rglob("*")) if p.is_dir() and not p.is_symlink()} if exists else {}}


def _materialize(root, image):
    """Build a fresh tree. The caller owns root; all blob/path checks precede writes."""
    _checked_snapshot(image["files"])
    mode = image.get("mode", 0o755)
    if type(mode) is not int or not 0 <= mode <= 0o777:
        raise ValueError("Invalid root mode in skill snapshot.")
    from .manage import lookup_path_error
    directories = image.get("directories", {})
    if not isinstance(directories, dict):
        raise TypeError("Invalid snapshot directories.")
    for rel, mode in directories.items():
        if lookup_path_error(rel) or str(Path(rel)) == "." or type(mode) is not int or not 0 <= mode <= 0o777:
            raise ValueError("Invalid directory in skill snapshot.")
        if any(str(Path(rel)) == item["path"] or Path(rel).is_relative_to(item["path"]) for item in image["files"]):
            raise ValueError("Snapshot directory is beneath a file or symlink.")
    if not image["exists"]:
        if image["files"] or directories:
            raise ValueError("Absent tree snapshot contains files.")
        return
    root.mkdir(parents=True, exist_ok=False)
    for rel in directories:
        (root / rel).mkdir(parents=True, exist_ok=True)
    for item in image["files"]:
        dest = root / item["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        if item.get("symlink") is not None:
            os.symlink(item["symlink"], dest)
        else:
            atomic.write_bytes(dest, (_blob_dir() / item["sha256"]).read_bytes(), mode=item.get("mode", 0o644))
    for rel, mode in sorted(directories.items(), key=lambda i: len(Path(i[0]).parts), reverse=True):
        os.chmod(root / rel, mode)
    os.chmod(root, image.get("mode", 0o755))


def restore(root, before, *, image=None):
    """Restore by replacement, never in-place writes through current links.

    With an enclosing transaction the durable journal owns crash recovery.
    Standalone compatibility calls still validate all blobs before moving live data.
    """
    root = Path(os.path.abspath(root))
    _safe_parents(root.parent, create=True)
    if root.is_symlink():
        raise ValueError(f"Skill root is a symlink: {root}")
    image = image if image is not None else {"exists": bool(before), "files": before}
    container = root.parent / ".misaka-skill-transactions"
    _safe_parents(container, create=True)
    stage = Path(tempfile.mkdtemp(prefix=_restore_prefix(root), dir=container))
    new, old = stage / "new", stage / "old"
    retain = False
    try:
        _materialize(new, image)
        _safe_parents(root.parent)
        if root.is_symlink():
            raise ValueError(f"Skill root is a symlink: {root}")
        if root.exists():
            os.replace(root, old)
        try:
            if new.exists():
                os.replace(new, root)
        except BaseException:
            if old.exists():
                try:
                    os.replace(old, root)
                except OSError as error:
                    retain = True
                    raise OSError(f"Skill replacement and restoration failed; original retained at {old}: {error}") from error
            raise
    finally:
        if not retain:
            _remove_tree(stage)


def _restore_prefix(root):
    # Only a recovery for THIS root may clean its interrupted replace stages.
    identity = hashlib.sha256(str(Path(root).absolute()).encode()).hexdigest()[:16]
    return f"restore-{identity}-"


def mutation_lock():
    """The one lock every live-skill mutation (apply, approve, rollback) runs under, so snapshot,
    write, scan and ledger happen as a unit. ponytail: one lock for all roles; per role if it contends."""
    from filelock import FileLock
    path = home.path("skills_lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(str(path), is_singleton=True)  # Native reentrancy for nested metadata transactions.


def record(action, skill, *, before=None, after_root=None, evidence=None, changes=None, entry_id=None):
    """Append a change to the ledger and return the entry id. Raises OSError when the ledger
    cannot be written: callers say so instead of pretending the change was recorded."""
    entry = {
        "id": entry_id or uuid.uuid4().hex[:12],
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "actor": current_origin(),
        "action": action,
        "skill": str(skill),
        "evidence": evidence or {},
        "before": before if before is not None else [],
        "after": snapshot(after_root) if after_root else [],
        "version": 2,
        "root": str(Path(after_root).absolute()) if after_root is not None else None,
        "changes": changes,
        "rollbackable": action in MUTATING_ACTIONS or bool(changes),     # bookkeeping entries restore nothing
    }
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    try:
        _collect_blobs()
    except Exception:
        logging.getLogger(__name__).warning("Skill blob cleanup failed after commit", exc_info=True)
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
                    item = json.loads(line)
                    if isinstance(item, dict):
                        out.append(item)
                except ValueError:
                    continue
    except OSError:
        pass
    return out[-limit:] if limit else out


def _journal_dir():
    return home.path("skills_state") / ".transactions"


def _save_journal(journal):
    atomic.write_text(_journal_dir() / (journal["id"] + ".json"), json.dumps(journal, ensure_ascii=False))


def _recover(journal):
    # The fsynced ledger entry is the commit point; a crash before updating the
    # journal must not undo an already committed write on restart.
    committed = any(e.get("id") == journal["id"] for e in entries())
    for change in journal["changes"]:
        image = change["after"] if committed else change["before"]
        restore(change["root"], image["files"], image=image)
    journal["state"] = "committed" if committed else "aborted"
    _save_journal(journal)
    for change in journal["changes"]:
        container = Path(change["root"]).parent / ".misaka-skill-transactions"
        _safe_parents(container)
        if container.is_dir():
            for stage in container.glob(_restore_prefix(change["root"]) + "*"):
                _remove_tree(stage)


def recover_transactions():
    """Replay unsettled durable journals under mutation_lock before new writes."""
    for path in sorted(_journal_dir().glob("*.json")):
        journal = json.loads(path.read_text())
        if not isinstance(journal, dict) or journal.get("version") != 2 or path.stem != journal.get("id"):
            raise ValueError(f"Invalid Skill transaction journal: {path}")
        if journal.get("state") == "prepared":
            _recover(journal)


def prepare_transaction(roots, *, action, skill, evidence=None):
    """Durable before-images precede staging, scanning, and every live mutation."""
    if evidence and evidence.get("profile_dir"):
        from .release import check_write
        check_write(evidence["profile_dir"], activating=action == "generation-activate")
    recover_transactions()
    changes = [{"root": str(Path(root).absolute()), "before": tree_image(root)} for root in roots]
    journal = {"version": 2, "id": uuid.uuid4().hex[:12], "state": "prepared",
               "action": action, "skill": skill, "evidence": evidence or {}, "changes": changes}
    _save_journal(journal)
    return journal


def commit_transaction(journal, after):
    for change, image in zip(journal["changes"], after, strict=True):
        change["after"] = image
    _save_journal(journal)
    try:
        for change in journal["changes"]:
            restore(change["root"], change["after"]["files"], image=change["after"])
        first = journal["changes"][0]
        record(journal["action"], journal["skill"], before=first["before"]["files"],
               after_root=first["root"], evidence=journal["evidence"],
               changes=journal["changes"], entry_id=journal["id"])
    except BaseException:
        _recover(journal)
        raise
    journal["state"] = "committed"
    try:
        _save_journal(journal)
    except OSError:
        # The fsynced ledger already committed. Recovery recognizes the ID and
        # republishes this journal; do not report an applied write as unapplied.
        pass
    return journal["id"]


def abort_transaction(journal):
    journal["state"] = "aborted"
    _save_journal(journal)


def rollback(entry_id, skill_root=None):
    """Restore the recorded physical identities; caller-selected role is irrelevant.

    Unbound v1 records remain readable, but need explicit provenance rebinding.
    A current name or matching bytes do not establish the original owner.
    """
    with mutation_lock():
        recover_transactions()
        target = next((e for e in entries() if e.get("id") == entry_id), None)
        if target is None:
            return False, f"Skill ledger entry not found: {entry_id}"
        if target.get("action") == "generation-activate":
            return False, "Writer generations require a new quiescent cutover, not ledger rollback."
        if not target.get("rollbackable"):
            return False, f"Ledger entry {entry_id} is not a change that can be rolled back."
        changes = target.get("changes")
        if not changes:
            return False, f"Ledger entry {entry_id} records no file changes to restore."
        try:
            # Validate before-images before creating the rollback journal.
            for change in changes:
                _checked_snapshot(change["before"]["files"])
                if tree_image(change["root"], store=False) != change.get("after"):
                    return False, "Skill changed since this ledger entry; inspect the newer changes before rollback."
            journal = prepare_transaction([c["root"] for c in changes], action="rollback", skill=target["skill"], evidence={"rollback_of": entry_id, "profile_dir": (target.get("evidence") or {}).get("profile_dir")})
            commit_transaction(journal, [c["before"] for c in changes])
        except (OSError, ValueError, KeyError, TypeError) as error:
            return False, f"Skill rollback did not complete: {error}"
    return True, f"Rolled back {target['skill']}."


# ── Write gate (after hermes write_approval.py) ─────────────────────────────

def _pending_dir():
    return home.path("skills_pending")


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
    from misaka.core.skills import manage
    error = pending_integrity_error(
        item, file_id=item.get("_pending_file_id") if isinstance(item, dict) else None)
    if error:
        return f"(cannot preview: {error})"
    payload = item.get("payload") or {}
    if payload.get("version") == 3 and payload.get("kind") == "distribution":
        import difflib
        old = json.dumps(payload["before"], indent=2, sort_keys=True).splitlines()
        new = json.dumps(payload["after"], indent=2, sort_keys=True).splitlines()
        parts = ["\n".join(difflib.unified_diff(old, new, fromfile="reviewed before", tofile="immutable after", lineterm=""))]
        try:
            for side in ('before', 'after'):
                _checked_snapshot(payload[side]['files'])
            before, after = ({entry['path']: entry for entry in payload[side]['files']} for side in ('before', 'after'))
            for path in sorted(before.keys() | after.keys()):
                if before.get(path) == after.get(path):
                    continue
                bodies = []
                for entry in (before.get(path, {}), after.get(path, {})):
                    data = (_blob_dir() / entry['sha256']).read_bytes() if entry.get('sha256') else b''
                    # The full immutable hashes/modes/links are always listed above.
                    # Bound terminal output for binary or very large content; never silently truncate.
                    try:
                        bodies.append(data.decode('utf-8') if b'\0' not in data and len(data) <= 512 * 1024 else None)
                    except UnicodeDecodeError:
                        bodies.append(None)
                if None in bodies:
                    parts.append(f"{path}: binary/large content; inspect immutable blobs in {_blob_dir()} using the hashes above.")
                else:
                    parts.append("\n".join(difflib.unified_diff(bodies[0].splitlines(), bodies[1].splitlines(),
                        fromfile='reviewed before/' + path, tofile='immutable after/' + path, lineterm='')))
        except (OSError, ValueError, TypeError, KeyError) as error:
            return f"(cannot preview immutable content: {error})"
        return "\n\n".join(part for part in parts if part)
    return manage.pending_diff(payload)

"""The panel's session inventory: discover files first, annotate their owners second.

No session-kind allowlist. Lifecycle records also locate custom stores and live
in-memory sessions; they are pointers/status, never a second transcript. Missing
files and dead ephemeral owners are filtered at read time, not resurrected.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from misaka.ai.session_resources import registerSessionResourceCleanup
from misaka.config import get_sessions_dir, home, sessions
from misaka.core.platform import owners, processes
from misaka.core.session_manager import iter_session_files, readSessionHeader
from misaka.core.wiring import KINDS, sender_address
from misaka.utils import atomic, file_lock, local_socket

SESSION_KINDS = KINDS


def _object(path):
    """One record. Its transcript pointer is kept home-relative on disk and is a real path here."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    if isinstance(value.get("path"), str):
        value["path"] = str(home.from_stored(value["path"]))
    return value


def _write(record, value):
    stored = {**value, "path": home.stored(value["path"])} if value.get("path") else value
    atomic.write_text(str(record), json.dumps(stored, ensure_ascii=False), mode=0o600)


def _role(value):
    name = str(value or "unknown").rsplit("/", 1)[-1]
    return "last-order" if name == "last_order" else name


def _index_dir():
    return Path(sessions.sessions_root()) / ".catalog"


def owner_record(path):
    key = os.path.realpath(path)
    return _object(_index_dir() / (hashlib.sha256(key.encode()).hexdigest() + ".json"))


@contextmanager
def deletable(path):
    """Hermes deletes no session with a queued or running turn (``require_idle``,
    hermes_state_mutation_guards.py): ``hermes sessions delete`` names it and deletes nothing. Yields
    that refusal, or None while the file may go.

    Running is a live owner of the file: a window, a card, a contact turn. A role's contact session is
    also queued while a delivery holds its lock (``misaka.cli.dm``) or mail addressed to it waits, and
    it is deleted under that lock, so no delivery starts until the file is gone: the next one begins a
    new Bot Chat, and a writer still holding the old file cannot put it back."""
    session_id = readSessionHeader(path).get("id") or os.path.basename(path)
    refusal = (f"Cannot delete session '{session_id}': it has a queued or running turn. "
               "Wait for it to finish (or stop it), then retry.")
    role = sessions.contact_role(path)
    fd = None
    try:
        if role is not None:
            lock = sessions.contact_lock_path(role)
            os.makedirs(os.path.dirname(lock), exist_ok=True)
            fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
            if not file_lock.lock_fd(fd, blocking=False):
                yield refusal
                return
        owner = owner_record(path)
        if owner.get("state") != "saved" and processes.identity_is_alive(owner.get("pid"), owner.get("identity")):
            yield refusal
            return
        if role is not None:
            from misaka.core.network import messages

            con = messages.connect()
            try:
                waiting = con.execute("SELECT 1 FROM messages WHERE to_session=? AND delivered_at IS NULL LIMIT 1",
                                      (session_id,)).fetchone()
            finally:
                con.close()
            if waiting:
                yield refusal
                return
        yield None
    finally:
        if fd is not None:
            os.close(fd)   # Closing releases the lock.


# Where SessionControl puts its socket directory (a short path: macOS sockaddr_un is 104 bytes).
# Windows has neither /tmp nor that limit; its temp directory is the user's own.
_CONTROL_PARENT = tempfile.gettempdir() if sys.platform == "win32" else "/tmp"
_CONTROL_PARENTS = {_CONTROL_PARENT, os.path.realpath(_CONTROL_PARENT)}


def _release_control(value):
    """Remove the control socket directory a session left behind, once its process is gone."""
    control = str(value.get("control") or "")
    if not control:
        return
    if os.path.islink(os.path.dirname(control)):
        return              # the recorded name, not whatever it currently points at
    directory = os.path.dirname(os.path.realpath(control))
    if (os.path.basename(directory).startswith("misaka-session-")
            and os.path.dirname(directory) in _CONTROL_PARENTS):
        shutil.rmtree(directory, ignore_errors=True)


def _live_socket(path):
    """Whether a session is still listening on this control socket."""
    if not os.path.exists(path):
        return False
    try:
        local_socket.connect(path, 0.2).close()
        return True
    except OSError:
        return False        # refused or unreachable: the process that bound it is gone


def _sweep_orphan_controls(spoken_for):
    """Remove control directories no record names and nothing is listening in.

    A record is unlinked as soon as its session has no transcript, which leaves its socket
    directory with nobody to name it -- 24 of the 27 directories in /tmp on the day this was
    written. A session binds its socket straight after making the directory, so one younger
    than a minute is left alone whatever the socket says.
    """
    removed, cutoff = 0, time.time() - 60
    for parent in {os.path.realpath(p) for p in _CONTROL_PARENTS}:
        try:
            names = os.listdir(parent)
        except OSError:
            continue
        for name in names:
            directory = os.path.join(parent, name)
            if not name.startswith("misaka-session-") or directory in spoken_for:
                continue
            try:
                if not os.path.isdir(directory) or os.path.getmtime(directory) > cutoff:
                    continue
            except OSError:
                continue
            if _live_socket(os.path.join(directory, "control.sock")):
                continue
            shutil.rmtree(directory, ignore_errors=True)
            removed += 1
    return removed


def sweep_dead():
    """Retire the records of sessions whose process is gone; return how many were swept.

    A pane the panel closes is sent SIGTERM and killed two seconds later, and a runtime that
    has not finished `session_shutdown` by then never unlinks its own record. The index filled
    up with pointers to dead processes and their control sockets stayed in /tmp
    (2026-09-18, B3: 18 of 19 records were dead, with eight abandoned socket directories).

    Only an owner that is *gone* is swept. An identity that merely disagrees is left alone:
    that verdict has been wrong before (B20) and the cost of being wrong here -- deleting a
    live session's pointer -- is higher than the cost of one stale record.
    """
    swept, spoken_for = 0, set()
    for file in _index_dir().glob("*.json"):
        value = _object(file)
        pid = value.get("pid")
        control = str(value.get("control") or "")
        path = value.get("path")
        if not pid:
            continue
        if value.get("state") == "saved" and not control and path and os.path.isfile(path):
            continue                          # already retired by its own runtime: no probe needed
        alive, reason = processes.explain_liveness(pid, value.get("identity"))
        if alive or reason != "pid gone":
            if control:
                spoken_for.add(os.path.dirname(os.path.realpath(control)))
            continue
        if _object(file) != value:
            continue                          # republished while we looked: its new owner wins
        if path and os.path.isfile(path):
            _release_control(value)
            value["state"] = "saved"
            value.pop("control", None)
            value.pop("paused", None)
            _write(file, value)
        else:
            _release_control(value)
            file.unlink(missing_ok=True)      # a pointer to a transcript that was never written
        swept += 1
    _sweep_orphan_controls(spoken_for)
    return swept


def _inbox(spec):
    """The mailbox a session's pump reads, or None when it runs no pump."""
    if spec is None or not getattr(spec, "receive_messages", False):
        return None
    return sender_address(spec)


def _live_records():
    """The records of sessions whose process is still there."""
    for file in _index_dir().glob("*.json"):
        value = _object(file)
        if value.get("state") == "saved":
            continue
        if processes.identity_is_alive(value.get("pid"), value.get("identity")):
            yield value


def live_readers(inbox, space):
    """The live sessions in panel space ``space`` whose pump reads ``inbox``, one per session id.
    A name sent from that space means exactly one of them; a session with no space is nobody's
    neighbour."""
    if not space:
        return []
    found = {}
    for value in _live_records():
        if value.get("inbox") == inbox and value.get("space") == space and value.get("id"):
            found.setdefault(value["id"], value)
    return list(found.values())


def live_contact(role):
    """Whether ``role``'s contact session (``misaka dm``) is running a turn right now."""
    return any(value.get("kind") == "dm" and value.get("inbox") == role for value in _live_records())


def find_session(session_id):
    """The catalog record of this session, live or saved (a live one first), or None."""
    if not session_id:
        return None
    saved = None
    for file in _index_dir().glob("*.json"):
        value = _object(file)
        if value.get("id") != session_id:
            continue
        if value.get("state") != "saved" and processes.identity_is_alive(value.get("pid"), value.get("identity")):
            return value
        saved = saved or value
    return saved


def spaces_in_use():
    """Every panel space some session still belongs to."""
    return {value["space"] for value in map(_object, _index_dir().glob("*.json")) if value.get("space")}


def live_session(session_id):
    """The catalog record of a live session with this id, or None.

    A session is the most precise address there is -- a specific Last Order window in a
    workflow's tree, one attempt at one card -- and its id is what the panel shows and what a
    message carries as its sender, so a reply can go back to exactly that conversation."""
    if not session_id:
        return None
    for file in _index_dir().glob("*.json"):
        value = _object(file)
        if value.get("id") != session_id or value.get("state") == "saved":
            continue
        if processes.identity_is_alive(value.get("pid"), value.get("identity")):
            return value
    return None


def live_card_session(task_id):
    """The catalog record of the live session running this card, or None."""
    if not task_id:
        return None
    for file in _index_dir().glob("*.json"):
        value = _object(file)
        if value.get("task_id") != task_id or value.get("state") == "saved" or not value.get("inbox"):
            continue
        if processes.identity_is_alive(value.get("pid"), value.get("identity")):
            return value
    return None


class CatalogPart:
    """Register every product session, including bare calls and headless children."""

    def __init__(self, spec=None):
        self.tools, self.commands = [], []
        self.spec = spec
        self.pid = os.getpid()
        self.identity = processes.identity(self.pid)
        self.instance = secrets.token_hex(8)
        self.record = None
        self.session_id = None
        self._unregister = None
        self.session = self.control = None
        # False for a reader that takes mail only between turns (an ally over ACP); SendMessage
        # tells the sender so.
        self.steer = True

    def attach(self, session):
        from misaka.core.session_control import SessionControl

        if self.control is not None:
            self.control.close()
        if self._unregister:
            self._unregister()
        self.session = session
        self.control = SessionControl(session, self)
        self.session_id = session.sessionId
        self._register_cleanup()

    def _register_cleanup(self):
        def cleanup(session_id):
            if (session_id is None or session_id == self.session_id
                    or self.session is not None and session_id == self.session.sessionId):
                self._close()
        self._unregister = registerSessionResourceCleanup(cleanup)

    def _publish(self, ctx, state):
        manager = ctx.sessionManager
        self.session_id = manager.getSessionId()
        path = manager.getSessionFile() if manager.isPersisted() else None
        key = os.path.realpath(path) if path else f"memory:{self.identity}:{manager.getSessionId()}"
        record = _index_dir() / (hashlib.sha256(key.encode()).hexdigest() + ".json")
        if record != self.record:
            self._retire()
        kind = self.spec.kind if self.spec else "session"
        # A session belongs to the panel space it first ran in: a record that already names
        # one keeps it, wherever the session is opened again. A contact session is nobody's
        # neighbour.
        space = None if kind == "dm" else (_object(record).get("space")
                                           or os.environ.get("MISAKA_NET_SPACE") or None)
        # A reserved filename is not history. Keep Pi's first-message flush;
        # only this live-owner pointer makes an unsaved session visible.
        value = {"id": manager.getSessionId(), "path": os.path.realpath(path) if path else None,
                 "pending": bool(path and not manager.flushed and not os.path.exists(path)),
                 "cwd": manager.getCwd(), "role": _role(self.spec.role) if self.spec else "unknown",
                 "kind": kind, "task_id": self.spec.task_id if self.spec else None,
                 "workspace": getattr(self.spec, "workspace", None), "inbox": _inbox(self.spec),
                 "space": space,
                 "pid": self.pid, "identity": self.identity, "instance": self.instance, "state": state}
        if not self.steer:
            value["steer"] = False
        if self.control is not None and self.control.path:
            value.update(control=self.control.path, paused=self.control.paused)
        _write(record, value)
        self.record = record

    async def session_start(self, _event, ctx):
        if self.control is not None:
            if self._unregister is None:
                self._register_cleanup()
            await self.control.start()
        self._publish(ctx, "idle")

    def refresh(self):
        if self.session is not None:
            self._publish(self.session, "idle" if self.session.isIdle else "working")

    async def context(self, _event, _ctx):
        if self.control is not None:
            await self.control.wait()

    async def tool_call(self, _event, _ctx):
        if self.control is not None:
            await self.control.wait()

    async def agent_start(self, _event, ctx):
        self._publish(ctx, "working")

    async def agent_settled(self, _event, ctx):
        self._publish(ctx, "idle")

    async def session_shutdown(self, _event, _ctx):
        self._close()

    def _close(self):
        if self.control is not None:
            self.control.close()
        if self._unregister:
            self._unregister()
            self._unregister = None
        self._retire()

    def _retire(self):
        record, self.record = self.record, None
        if record is None:
            return
        value = _object(record)
        if value.get("instance") != self.instance:
            return  # another runtime now owns this file; do not overwrite its state
        if value.get("path") and os.path.isfile(value["path"]):
            value["state"] = "saved"
            value.pop("control", None)
            value.pop("paused", None)
            _write(record, value)
        else:
            record.unlink(missing_ok=True)


def part(spec):
    return CatalogPart(spec)


def _relative(path, root):
    try:
        return Path(path).relative_to(root).parts
    except ValueError:
        return ()


def available(entry):
    """Only saved files or unsaved sessions with a live owner are openable."""
    if not os.path.isdir(entry.get("workspace") or "") or not os.path.isdir(entry.get("cwd") or ""):
        return False
    if entry.get("path") and os.path.isfile(entry["path"]):
        return True
    record = _object(entry.get("catalog_file") or "")
    return bool(record and (not entry.get("path") or record.get("pending"))
                and record.get("state") != "saved"
                and processes.identity_is_alive(record.get("pid"), record.get("identity")))


def list_entries(con, *, extra_paths=()):
    """All extant session files and live ephemeral sessions, without changing any store."""
    product = os.path.realpath(sessions.sessions_root())
    roots = {product, get_sessions_dir()}
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")} if con else set()
    # The runs, nodes and card links of the workflows that own cards, as their owners list them
    # (``platform.owners``).
    run_rows, nodes, links = {}, {}, {}
    own_dirs = {"cards", "subagents"}.union(*(owner.session_dirs() for owner in owners.registered()))
    if con is not None:
        for owner in owners.registered():
            scopes, units, cards = owner.catalog(con)
            run_rows.update(scopes)
            nodes.update(units)
            links.update(cards)
    cards = {}
    if "tasks" in tables:
        for row in con.execute("SELECT * FROM tasks"):
            directory = os.path.realpath(sessions.card_session_dir(row))
            roots.add(directory)
            cards[directory] = dict(row)

    records, ephemeral = {}, []
    alive = {}
    for file in _index_dir().glob("*.json"):
        value = _object(file)
        owner = (value.get("pid"), value.get("identity"))
        if value.get("state") != "saved" and owner not in alive:
            alive[owner] = processes.identity_is_alive(*owner)
        live = value.get("state") != "saved" and alive.get(owner, False)
        value["state"] = value.get("state", "idle") if live else "saved"
        value["catalog_file"] = str(file)
        if value.get("path"):
            records[os.path.realpath(value["path"])] = value
        elif live:
            ephemeral.append(value)
    paths = {os.path.realpath(p) for p in extra_paths if p}
    paths.update(records)
    # Persisted pointers locate historical/custom conversations without an alternate run store.
    node_files = {os.path.realpath(n["session_file"]): n for n in nodes.values() if n.get("session_file")}
    paths.update(node_files)
    paths.update(os.path.realpath(r["root_session"]) for r in run_rows.values() if r.get("root_session"))
    # Nested/custom roots can overlap. One canonical file is always one row.
    for root in roots:
        paths.update(os.path.realpath(p) for p in iter_session_files(root, recursive=True))
    entries = []
    for path in sorted(paths):
        record = records.get(path, {})
        pending = record.get("pending") and record.get("state") != "saved" and not os.path.exists(path)
        header = record if pending else readSessionHeader(path)
        if not isinstance(header.get("id"), str) or not isinstance(header.get("cwd"), str):
            continue
        try:
            modified = 0 if pending else os.path.getmtime(path)
        except OSError:
            continue
        row = {"id": header["id"], "path": path, "cwd": header["cwd"], "workspace": header["cwd"],
               "role": record.get("role", "unknown"), "kind": record.get("kind", "session"),
               "state": record.get("state", "saved"), "modified": modified, "space": record.get("space")}
        if record.get("catalog_file"):
            row["catalog_file"] = record["catalog_file"]
        if record.get("state") != "saved" and record.get("control"):
            row.update(control=record["control"], paused=record.get("paused", False))
        parts = _relative(path, product)
        if len(parts) > 1 and parts[0] not in own_dirs:
            row["role"] = record.get("role", _role(parts[0]))
            row["kind"] = "dm" if parts[1] == "dm" else record.get("kind", "foreground")
        card = next((r for directory, r in cards.items() if _relative(path, directory)), None)
        run, node = None, None
        if card:
            if not os.path.isdir(card["workspace"] or ""):
                continue
            row.update(task_id=card["id"], title=card["title"], role=_role(card["assignee"]),
                       kind="card", workspace=card["workspace"], cwd=card["workspace"], task_status=card["status"])
            link = links.get(card["id"], {})
            run, node = run_rows.get(link.get("run_id")), nodes.get(link.get("branch_id"))
        # A workflow's own conversation, placed by its owner: the run, and the node whose Last
        # Order it is.
        place = next((found for owner in owners.registered()
                      if (found := owner.session_place(parts, nodes)) is not None), None)
        if place is not None:
            scope, unit, coordinator = place
            run = run_rows.get(scope)
            if coordinator:
                node = nodes.get(unit) if unit else None
                row.update(kind="node", role="last-order")
        if path in node_files:
            node = node_files[path]
            run = run_rows.get(node["run_id"])
            if node["depth"] != 0 or row["kind"] not in {"foreground", "dm"}:
                row.update(kind="node", role="last-order")
        if run:
            row.update(workspace=run["workspace"], run_id=run["id"], run_status=run["status"])
            if not os.path.isdir(run["workspace"]):
                continue
        if node and run:
            cwd = run["workspace"]
            row.update(node_id=node["id"], depth=node["depth"], node_status=node["status"],
                       cwd=cwd if os.path.isdir(cwd) else row["workspace"])
        child = _object(Path(path).with_suffix(".meta.json"))
        if child or "subagents" in Path(path).parts:
            row.update(kind="child", role=_role(child.get("agentType") or record.get("role")),
                       title=child.get("description") or Path(path).stem,
                       child_status=child.get("status"))
            if "task_status" in row:
                row["parent_task_status"] = row.pop("task_status")
            worktree = child.get("worktree")
            if isinstance(worktree, dict) and worktree.get("repo"):
                row["workspace"] = worktree["repo"]
                if not os.path.isdir(row["cwd"] or ""):
                    row["cwd"] = worktree["repo"]
        if not available(row):
            continue
        row.update(workspace=os.path.realpath(row["workspace"]), cwd=os.path.realpath(row["cwd"]))
        entries.append(row)
    entries.extend({**e, "workspace": os.path.realpath(e["cwd"]), "modified": 0}
                   for e in ephemeral if os.path.isdir(e.get("cwd") or ""))
    entries.sort(key=lambda e: e["modified"], reverse=True)
    keyed = set()
    for entry in entries:
        key = ("card", entry["task_id"]) if entry["kind"] == "card" and entry.get("task_id") else None
        if key and key not in keyed:
            entry["live_key"] = key
            keyed.add(key)
    return entries

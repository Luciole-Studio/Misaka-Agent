"""A reading's state: one graph in the board's database (``misaka.core.graph``), kept by its schema.

The run, its materials and its readings are nodes, and so is each section an action writes into
a reading (``part_of`` it; written once, never changed, so a node's history does not copy its
text). How a reading came to be is a ``derives`` edge (fork, compare or reread; they form a DAG and
never change); what one reading says about another is a ``relation`` edge, which orders nothing.
Every operation is one journal entry, its sections with it, so the graph keeps the history of the
understanding as well as its latest state, and nothing an operation wrote can be half recorded.
The project holds what the graph renders (:mod:`.views`), notes included, as research does.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import re
import secrets
import socket
import sqlite3
from contextlib import asynccontextmanager, contextmanager

GRAPH_KIND = "reading"
ACTIONS = ("overview", "reconstruct", "presuppose", "transform", "compare", "translate", "reread", "generate")
MODES = ("continue", "independent", "compare", "reread")
RELATIONS = ("corresponds", "differs", "shares-concept", "depends-on", "resonates", "contradicts")
SCHEMA = {
    "nodes": {
        "run": {"max": 1, "frozen": ["question"]},
        "source": {"frozen": ["doc_id"]},
        "reading": {"frozen": ["question", "lens"]},
        "section": {"frozen": ["action", "mode", "at", "text", "model"]},
    },
    "edges": {
        "derives": {"from": ["reading"], "to": ["reading"], "acyclic": True, "frozen": True},
        "relation": {"from": ["reading"], "to": ["reading"]},
        "part_of": {"from": ["section"], "to": ["reading"], "max_out": 1, "frozen": True},
    },
}
ROOT = "N0"
BIND = "reading-bind"      # the conversation entry that makes it a reading's leader (window, cli.chat)
LEASE_SECONDS = 300
RENEW_SECONDS = 60


class ReadingError(RuntimeError):
    """A request the reading refuses, in words for the reader."""


def now():
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def same_path(a, b):
    return bool(a and b) and os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def graph_id(run_id):
    return f"reading:{run_id}"


_CONNECTIONS = {}


def connect():
    """The board's database, one connection per process (and per home), as research keeps its own."""
    from misaka.config import CFG
    from misaka.core import graph
    from misaka.core.platform import tasks

    path = CFG["db"]
    if path not in _CONNECTIONS:
        con = tasks.connect(path)
        graph.init(con)
        _CONNECTIONS[path] = con
    return _CONNECTIONS[path]


def new_run_id(con, question, parent):
    """``YYYYMMDD-HHMMSS-<slug>`` from the clock and the question; one taken already (the same
    question in the same second), as a reading or as a folder in ``parent``, gets ``-2``, ``-3``..."""
    from misaka.core import graph

    stamp = datetime.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    # No trailing dot or space either: Windows drops them from a folder's name, and the reading's
    # files would then go to a folder that is not there.
    slug = re.sub(r'[\\/:*?"<>|\s]+', "-", question).strip("-. ")[:24].strip("-. ")
    base = f"{stamp}-{slug or 'reading'}"
    run_id, n = base, 1
    while graph.get(con, graph_id(run_id)) is not None or os.path.exists(os.path.join(parent, run_id)):
        n += 1
        run_id = f"{base}-{n}"
    return run_id


def _token():
    """``reading:<host>:<pid>:<nonce>``: who holds a reading's lease, so a dead holder can be told."""
    return f"reading:{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"


def _ours(holder):
    parts = holder.split(":")
    return len(parts) == 4 and parts[1] == socket.gethostname() and parts[2] == str(os.getpid())


def _dead(holder):
    """A lease holder on this host whose process is gone (research's ``driver`` tokens likewise)."""
    import psutil

    parts = holder.split(":")
    if len(parts) != 4 or parts[0] != "reading" or parts[1] != socket.gethostname() or not parts[2].isdigit():
        return False
    return not psutil.pid_exists(int(parts[2]))


_LIVE = set()      # the lease tokens this process's operations and changes hold right now


def _stale(holder):
    """A holder that is gone: its process has ended, or it is this process's and nothing here holds
    it any more (a release the board was too busy to take)."""
    return _dead(holder) or (_ours(holder) and holder not in _LIVE)


@contextmanager
def _leased(con, gid, run_id):
    """A reading's lease, taken for the block and given back after it however it ends."""
    from misaka.core import graph

    token = _token()
    try:
        acquired = graph.acquire(con, gid, token, ttl=LEASE_SECONDS, stale=_stale).acquired
    except sqlite3.Error as error:
        raise ReadingError(f"The board is too busy to take reading {run_id} up ({error}); try again.") from error
    if not acquired:
        raise ReadingError(f"Reading {run_id} is busy with another operation; try again when it has ended.")
    _LIVE.add(token)
    try:
        yield token
    finally:
        _LIVE.discard(token)
        try:
            graph.release(con, gid, token)
        except sqlite3.Error:
            # What the block did stands; a lease the board was too busy to take back is stale to
            # this process at once (``_stale``) and runs out for the others (LEASE_SECONDS).
            pass


class Run:
    """One reading as its graph holds it now."""

    def __init__(self, con, run_id):
        from misaka.core import graph

        self.con, self.id, self.gid = con, run_id, graph_id(run_id)
        if graph.get(con, self.gid) is None:
            raise ReadingError(f"No reading run {run_id}.")

    @property
    def view(self):
        from misaka.core import graph

        return graph.snapshot(self.con, self.gid)

    @property
    def info(self):
        return self.view.node("run").attrs

    @property
    def updated(self):
        """When the reading last changed: the latest change its graph recorded."""
        from misaka.core import graph

        applied = [entry.applied_at for entry in graph.entries(self.con, self.gid) if entry.applied_at]
        if not applied:
            return self.info["created"]
        return datetime.datetime.fromtimestamp(max(applied), datetime.UTC).isoformat(timespec="seconds")

    @property
    def directory(self):
        return self.info["directory"]

    @property
    def workspace(self):
        return self.info["workspace"]

    def sources(self):
        return sorted(self.view.nodes("source"), key=lambda node: node.ordinal)

    def readings(self):
        return sorted(self.view.nodes("reading"), key=lambda node: node.ordinal)

    def reading(self, node_id):
        found = self.view.node(node_id)
        if found is None or found.kind != "reading":
            known = ", ".join(node.id for node in self.readings()) or "none"
            raise ReadingError(f"No node {node_id} in run {self.id}; the graph has {known}.")
        return found

    def parents(self, node_id):
        return [(edge.source, edge.attrs) for edge in self.view.in_edges(node_id, "derives")]

    def relations(self):
        return sorted(self.view.edges("relation"), key=lambda edge: edge.ordinal)

    def next_ids(self, count):
        """The ids ``count`` new readings take, ``N<k>`` after the highest so far."""
        top = max((int(node.id[1:]) for node in self.readings() if re.fullmatch(r"N\d+", node.id)), default=-1)
        return [f"N{top + 1 + i}" for i in range(count)]

    def sections(self, node_id):
        """What the actions on a reading wrote into it, in order."""
        view = self.view
        return sorted((view.node(edge.source) for edge in view.in_edges(node_id, "part_of")),
                      key=lambda node: node.ordinal)

    def note_path(self, node_id):
        return os.path.join(self.directory, "nodes", f"{node_id}.md")

    def sessions_directory(self):
        return os.path.join(self.directory, "sessions")

    # -- writing ---------------------------------------------------------------------------

    def commit(self, token, op, payload, apply):
        """One journal entry ``op``: ``apply(change)`` makes it, the schema checks it, all or nothing.
        A change the graph refuses (its schema, a lost lease, a board too busy to write) is the
        reader's to know, and nothing of it is recorded."""
        from misaka.core import graph

        try:
            return graph.commit(self.con, self.gid, op, payload, token=token,
                                handlers={op: lambda change, _entry: apply(change)})
        except graph.LeaseError as error:
            raise ReadingError(f"Reading {self.id} was taken over while this operation ran; nothing of it "
                               "was recorded.") from error
        except (graph.GraphError, sqlite3.Error) as error:
            raise ReadingError(f"Reading {self.id} could not record this: {error}") from error

    @contextmanager
    def hold(self):
        """The lease for a short change outside an operation (binding the leader, say): the lease of
        an operation of this process when one holds it, else one taken for the change. While another
        process's operation holds it, the reading is busy."""
        from misaka.core import graph

        # The lease is what excludes others, so no board transaction is held open meanwhile: what
        # is done under it (writing the reading's files) must not keep every other board writer waiting.
        try:
            holder = graph.get(self.con, self.gid).lease_token
        except sqlite3.Error as error:
            raise ReadingError(f"The board is too busy to read reading {self.id} ({error}); try again.") from error
        if holder in _LIVE:
            yield holder
            return
        with _leased(self.con, self.gid, self.id) as token:
            yield token

    def write(self, op, payload, apply):
        with self.hold() as token:
            return self.commit(token, op, payload, apply)

    @asynccontextmanager
    async def operation(self):
        """Hold the reading for one operation, so two never interleave, across processes too.
        The lease is renewed while the model works and given back at the end, however it ends. A
        lease another process took meanwhile (this one's ran out: the machine asleep, say) stops
        the operation at the next renewal, as research's driver stops (``_keep_lease``), rather
        than letting the model work on for a commit that would refuse it."""
        from misaka.core import graph

        with _leased(self.con, self.gid, self.id) as token:
            owner, lost = asyncio.current_task(), False
            cancelling = owner.cancelling()

            async def keep():
                nonlocal lost
                # A renewal the board is too busy for is tried again: the lease is lost only once
                # another holder has it, and the commit refuses a writer without it either way.
                while True:
                    await asyncio.sleep(RENEW_SECONDS)
                    try:
                        if graph.renew(self.con, self.gid, token, ttl=LEASE_SECONDS):
                            continue
                    except sqlite3.Error:
                        continue
                    lost = True
                    owner.cancel()
                    return

            renewer = asyncio.create_task(keep())
            try:
                yield token
            except asyncio.CancelledError:
                # As ``asyncio.timeout``: the cancellation made here is taken back, and becomes the
                # reason; one the caller made as well (a stop) stands.
                if lost and owner.uncancel() <= cancelling:
                    raise ReadingError(f"Reading {self.id} was taken over while this operation ran; it was "
                                       "stopped and nothing of it was recorded.") from None
                raise
            finally:
                renewer.cancel()


def create(con, run_id, *, question, intent, workspace, directory, sources):
    """A new reading: its run node, its materials, and ``N0``, the reader's question (no reading
    yet, no model call), so a first action has a node to start from."""
    from misaka.core import graph
    from misaka.core.platform.tasks import write_txn

    at = now()

    def apply(change):
        change.add_node("run", {"question": question, "intent": intent or "", "workspace": workspace,
                                "directory": directory, "current": ROOT, "leader": None, "created": at},
                        id="run")
        for source in sources:
            change.add_node("source", {**source, "added": at}, id=source["doc_id"])
        change.add_node("reading", reading_attrs(question, None, [{"doc_id": s["doc_id"]} for s in sources], at),
                        id=ROOT)

    with write_txn(con):                      # the graph and its first entry, or neither
        graph.create(con, GRAPH_KIND, SCHEMA, id=graph_id(run_id))
        run = Run(con, run_id)
        run.write("open", {"question": question, "sources": [s["doc_id"] for s in sources]}, apply)
    return run


def reading_attrs(question, lens, refs, at):
    return {"question": question, **({"lens": lens} if lens else {}), "summary": "", "refs": refs,
            "actions": [], "questions": [], "session": None, "created": at}

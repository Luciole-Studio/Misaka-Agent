"""Graphs and the governance of how they grow.

A graph is typed nodes and edges carrying JSON attributes, kept in the board's database beside the
cards. Its user declares a :class:`Schema` when making it -- the kinds of node and edge, what each
kind of edge may join, and the shapes its edges must keep (degree limits, no duplicates, no cycles,
a depth bound) -- and every change is checked against it, whoever makes the change.

A graph changes only through its journal. An entry is proposed (by anyone the graph's user lets
propose), approved when it has to wait for that, and applied by whoever holds the graph's lease,
which records a receipt. Applying runs a handler, passed in by the applier, on a :class:`Change`;
the result is checked as a whole and becomes the graph's next version, or nothing happens at all.
No version is ever overwritten: :func:`snapshot` reads the graph as of any application.

What the graph does not do is just as deliberate. It runs no algorithms -- they run on snapshots,
written by whoever needs them. It executes nothing and knows no cards, sessions or processes.
Kind names and ops mean nothing to it. Nothing here imports research, the board's logic or the
extensions, and nothing outside this package touches the graph tables except through it.

Every write joins the caller's transaction when there is one (``platform.tasks.write_txn``), so a
user can change its graph and its own tables in one commit. Call :func:`init` once per connection.
"""

from .journal import (
    APPLIED,
    APPROVED,
    PROPOSED,
    VOID,
    Change,
    Handler,
    apply,
    approve,
    commit,
    current,
    entries,
    entry,
    propose,
    withdraw,
)
from .lease import acquire, release, renew
from .model import (
    ConstraintError,
    Edge,
    EdgeKind,
    Entry,
    Graph,
    GraphError,
    JournalError,
    Lease,
    LeaseError,
    Node,
    NodeKind,
    Schema,
    SchemaError,
    Violation,
    thaw,
)
from .snapshot import Snapshot
from .store import create, get, init, locate, snapshot, version

__all__ = [
    "APPLIED",
    "APPROVED",
    "PROPOSED",
    "VOID",
    "Change",
    "ConstraintError",
    "Edge",
    "EdgeKind",
    "Entry",
    "Graph",
    "GraphError",
    "Handler",
    "JournalError",
    "Lease",
    "LeaseError",
    "Node",
    "NodeKind",
    "Schema",
    "SchemaError",
    "Snapshot",
    "Violation",
    "acquire",
    "apply",
    "approve",
    "commit",
    "create",
    "current",
    "entries",
    "entry",
    "get",
    "init",
    "locate",
    "propose",
    "release",
    "renew",
    "snapshot",
    "thaw",
    "version",
    "withdraw",
]

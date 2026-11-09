"""Who may apply changes to a graph: one lease holder at a time.

A holder names itself with a token of its own choosing and keeps the lease by renewing it. Every
application checks the token inside its transaction, so a holder that lost the lease -- it expired
and someone took it -- writes nothing more (a fencing token). What makes a holder dead before its
lease runs out (its process exited, say) is the user's knowledge, passed in as ``stale``.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable

from misaka.core.platform.tasks import write_txn

from .model import GraphError, Lease, LeaseError
from .store import require


def _token(token: str) -> str:
    if not isinstance(token, str) or not token:
        raise GraphError("a lease token is a non-empty name")
    return token


def acquire(con: sqlite3.Connection, graph_id: str, token: str, *, ttl: int,
            stale: Callable[[str], bool] | None = None) -> Lease:
    """Take the lease for ``ttl`` seconds, or renew it for its holder. Granted when nobody holds it, its
    holder's time ran out, or ``stale`` says its holder is gone; ``previous`` names whoever held it."""
    _token(token)
    with write_txn(con):
        graph = require(con, graph_id)
        holder, now = graph.lease_token, int(time.time())
        free = (holder is None or holder == token or graph.lease_expires is None or graph.lease_expires < now
                or (stale is not None and stale(holder)))
        if not free:
            return Lease(acquired=False, holder=holder, previous=holder)
        con.execute("UPDATE graphs SET lease_token=?, lease_expires=? WHERE id=?", (token, now + int(ttl), graph_id))
    return Lease(acquired=True, holder=token, previous=holder)


def renew(con: sqlite3.Connection, graph_id: str, token: str, *, ttl: int) -> bool:
    """Extend the lease by ``ttl`` seconds from now; False when ``token`` no longer holds it."""
    _token(token)
    with write_txn(con):
        changed = con.execute("UPDATE graphs SET lease_expires=? WHERE id=? AND lease_token=?",
                              (int(time.time()) + int(ttl), graph_id, token)).rowcount
    return changed == 1


def release(con: sqlite3.Connection, graph_id: str, token: str) -> None:
    """Give the lease up, if ``token`` still holds it."""
    _token(token)
    with write_txn(con):
        con.execute("UPDATE graphs SET lease_token=NULL, lease_expires=NULL WHERE id=? AND lease_token=?",
                    (graph_id, token))


def fence(con: sqlite3.Connection, graph_id: str, token: str) -> None:
    """Refuse, inside the caller's transaction, a writer whose token does not hold the lease. Expiry
    alone does not refuse: a holder nobody has replaced may still finish what it started."""
    if require(con, graph_id).lease_token != _token(token):
        raise LeaseError(f"graph {graph_id} is held by another writer; this one's lease was lost or never taken")

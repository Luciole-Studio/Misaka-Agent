"""The token ledger: what was spent, what is in flight, and the token cap.

Every model request a metered session makes (``misaka.core.platform.metering``) leases its worst
case here before it is sent and, when it ends, records what it used and gives the lease back in
one transaction. So at any moment ``spent + in flight <= cap`` for the scope the request belongs
to -- a workflow's run, as its card owner says (``platform.owners``) -- across processes.
Spending is recorded per request, as it happens; outside any scope it is recorded and never capped.
"""
import contextvars
import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager, nullcontext

# How long a lease outlives its last renewal. A request renews it while it streams; a process that
# died mid-request leaves it to expire, and it is then charged in full.
LEASE_TTL_SECONDS = 900
# Said when a request does not fit under the cap. Every supervisor matches this text to stop work
# as "reached the cap" (resumable) rather than failed, and the retry classifier never matches it.
EXHAUSTED_MESSAGE = "token_cap reached"


def default_cap():
    """The token cap of one scope (0 = none), as the card owners set it (``platform.owners``)."""
    from misaka.core.platform import owners

    return owners.token_cap()


def beast_at():
    """The share of the cap at which cards switch to beast mode, as the card owners set it."""
    from misaka.core.platform import owners

    return owners.beast_at()

BEAST_SUFFIX = """

---
⚠️ **The token budget is nearly exhausted (Beast Mode).** Only coordination tools remain.
Use only information already in context and end with an honest, concise summary. If required work is incomplete,
use `SendMessage` to tell Last Order exactly what remains and why, then stop; do not claim completion.
On a task card, set `request_input=true` so incomplete work is parked rather than submitted as complete.
"""


def _locked(con):
    """Hold the connection lock for a whole transaction, as ``tasks._write_txn`` does.

    The board connection is shared: Last Order runs nearly every board call through
    ``asyncio.to_thread`` on one ``SerializedConnection``. A transaction opened outside that
    lock is invisible to the other threads' *bookkeeping*: ``tasks._write_txn`` reads
    ``con.in_transaction``, sees True, decides it is not the owner, and returns True for a
    ``submit_task``/``claim``/``mark_failed`` it never committed. Whether that transition
    survives is then up to whoever did open the transaction -- and every rollback path here
    (``reserved``'s OperationalError branch included) would erase it after the caller was told
    it succeeded. Taking the lock first makes this transaction the only one in flight.
    """
    serialized = getattr(con, "serialized", None)
    return serialized() if serialized else nullcontext()


def spent(con, *, task_ids=None):
    """Total token usage in the current transaction's event ledger.

    Task deletion and transaction rollback can remove already-seen rows, including writes
    from other connections. An append-only connection cache is therefore not authoritative.
    """
    # ponytail: recount usage rows; add transaction-aware storage only if this is measured hot.
    total = 0
    scope = None if task_ids is None else set(task_ids)
    for task_id, kind, payload in con.execute(
            "SELECT task_id,kind,payload FROM events "
            "WHERE (kind='budget_usage' OR kind='harn_event' AND payload LIKE '%\"agent_end\"%') "
            "AND payload LIKE '%totalTokens%'"):
        if scope is not None and task_id not in scope:
            continue
        try:
            d = json.loads(payload)
        except (ValueError, TypeError):
            continue
        if kind == "budget_usage":
            if isinstance(d.get("totalTokens"), int):
                total += d["totalTokens"]
            continue
        if d.get("type") != "agent_end":
            continue
        for m in d.get("messages") or []:
            u = m.get("usage") if isinstance(m, dict) else None
            if isinstance(u, dict) and isinstance(u.get("totalTokens"), int):
                total += u["totalTokens"]
    return total


def _charge_expired_reservations(con, now):
    """Charge expired reservations as usage (conservatively, in full) before dropping them."""

    rows = list(
        con.execute(
            "SELECT id,task_id,generation,tokens FROM budget_reservations "
            "WHERE expires_at<?",
            (now,),
        )
    )
    for reservation_id, task_id, generation, tokens in rows:
        total = max(0, int(tokens or 0))
        if total:
            con.execute(
                "INSERT INTO events (task_id,kind,payload,generation,created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    str(task_id),
                    "budget_usage",
                    json.dumps(
                        {"totalTokens": total, "expiredReservation": reservation_id},
                        separators=(",", ":"),
                    ),
                    int(generation),
                    int(now),
                ),
            )
    if rows:
        con.executemany(
            "DELETE FROM budget_reservations WHERE id=?",
            ((row[0],) for row in rows),
        )


def scope(con, task_id):
    """The ledger rows ``task_id`` is capped with -- for a workflow's card, its run and every card of
    that run, as its owner says (``platform.owners``) -- or None when it is never capped (an ordinary
    card, a contact session)."""
    from misaka.core.platform import owners
    return owners.budget_scope(con, task_id)


def _held(con, ids, now):
    if ids is None:
        rows = con.execute("SELECT COALESCE(SUM(tokens),0) FROM budget_reservations WHERE expires_at>=?", (now,))
    else:
        ids = sorted(ids)
        rows = con.execute(
            f"SELECT COALESCE(SUM(tokens),0) FROM budget_reservations WHERE expires_at>=? "
            f"AND task_id IN ({','.join('?' * len(ids))})", (now, *ids))
    return int(rows.fetchone()[0] or 0)


def reserved(con, *, task_id=None):
    """Tokens in flight: the whole board's, or the scope ``task_id`` belongs to."""

    now = int(time.time())
    with _locked(con):
        # The in_transaction read has to be inside the lock too: outside it, the answer is
        # already stale by the time BEGIN runs, and the "nested" branch is precisely the case
        # this function used to create for everyone else.
        nested = bool(getattr(con, "in_transaction", False))
        try:
            if nested:
                con.execute("SAVEPOINT misaka_budget_expiry")
            else:
                con.execute("BEGIN IMMEDIATE")
            _charge_expired_reservations(con, now)
            held = _held(con, scope(con, task_id) if task_id else None, now)
            if nested:
                con.execute("RELEASE SAVEPOINT misaka_budget_expiry")
            else:
                con.commit()
        except sqlite3.OperationalError:
            try:
                if nested:
                    con.execute("ROLLBACK TO SAVEPOINT misaka_budget_expiry")
                    con.execute("RELEASE SAVEPOINT misaka_budget_expiry")
                else:
                    con.rollback()
            except Exception:  # noqa: BLE001, S110 - preserve the original database error
                pass
            raise
    return held


def _cap_refused(con, ids, cap):
    """Whether a request of these tasks was refused under a cap no higher than ``cap``: the run has
    reached it even though ``spent`` is short of it -- its next request did not fit. Raising the
    cap clears it."""
    if not ids:
        return False
    ids = sorted(ids)
    for (payload,) in con.execute(
            f"SELECT payload FROM events WHERE kind='token_cap_reached' AND task_id IN ({','.join('?' * len(ids))})", ids):
        try:
            if int(json.loads(payload).get("cap") or 0) >= int(cap):
                return True
        except (ValueError, TypeError, AttributeError):
            continue
    return False


def cap_reached(con, task_id, cap):
    """Record that a request of ``task_id`` did not fit under ``cap``: every driver and card of its
    scope stops as reached-the-cap from here (``exhausted``), until the cap is raised."""
    with _locked(con):
        con.execute("INSERT INTO events (task_id,kind,payload,generation,created_at) VALUES (?,?,?,?,?)",
                    (str(task_id), "token_cap_reached", json.dumps({"cap": int(cap)}), 0, int(time.time())))


def exhausted(con, cap=None, *, task_id=None):
    """Whether the scope ``task_id`` belongs to has reached its cap: spent it, or had a request
    refused under it. Without ``task_id``: whether the board has spent it (a status reading, never
    a reason to stop a run)."""
    cap = default_cap() if cap is None else cap
    if not cap:
        return False
    ids = scope(con, task_id) if task_id else None
    if task_id and ids is None:
        return False
    return spent(con, task_ids=ids) >= cap or _cap_refused(con, ids, cap)


def status(con, cap=None, *, task_id=None):
    """Spent, in flight and the cap, for the scope ``task_id`` belongs to (``mode`` normal,
    beast past ``beast_at`` of the cap, stop at the cap); a task outside any scope is never capped.
    The mode follows what is spent, as ``exhausted`` does: a lease is a request's worst case and
    mostly comes back, and one large request can hold all the room left (0.18.9 counted leases
    in, so a busy run read "stop" with nothing spent and its planner failed)."""
    cap = default_cap() if cap is None else cap
    ids = scope(con, task_id) if task_id else None
    held = reserved(con, task_id=task_id)
    used = spent(con, task_ids=ids)
    if not cap or (task_id and ids is None):
        return {"mode": "normal", "used": used, "reserved": held, "cap": 0, "ratio": 0.0}
    ratio = used / cap
    mode = "stop" if ratio >= 1.0 or _cap_refused(con, ids, cap) else ("beast" if ratio >= beast_at() else "normal")
    return {
        "mode": mode,
        "used": used,
        "reserved": held,
        "cap": cap,
        "ratio": round(ratio, 3),
    }


def _usage_row(con, task_id, generation, tokens, now, **facts):
    con.execute(
        "INSERT INTO events (task_id,kind,payload,generation,created_at) VALUES (?,?,?,?,?)",
        (str(task_id), "budget_usage", json.dumps({"totalTokens": int(tokens), **facts}, separators=(",", ":")),
         int(generation) if str(generation).isdigit() else 0, int(now)))


def lease_request(con, cap, task_id, generation, need, want, ttl_seconds=LEASE_TTL_SECONDS):
    """Lease room for one model request: ``need`` is the least it can run with, ``want`` its whole
    worst case. Returns ``{"status": "granted", "lease", "tokens", "used", "held", "cap"}``;
    ``"wait"`` when only other requests in flight stand in the way (they end, or expire);
    ``"exhausted"`` when what is already spent does. Outside any scope, or with no cap, the
    whole ``want`` is granted with no lease (``lease`` None): it is only recorded."""
    cap = int(cap or 0)
    now = int(time.time())
    with _locked(con):
        con.execute("BEGIN IMMEDIATE")
        try:
            _charge_expired_reservations(con, now)
            ids = scope(con, task_id) if cap else None
            if ids is None:
                con.commit()
                return {"status": "granted", "lease": None, "tokens": int(want), "used": 0, "held": 0, "cap": 0}
            used = spent(con, task_ids=ids)
            held = _held(con, ids, now)
            reading = {"used": used, "held": held, "cap": cap}
            if cap - used < need:
                con.rollback()
                return {"status": "exhausted", **reading}
            room = cap - used - held
            if room < need:
                con.rollback()
                return {"status": "wait", **reading}
            granted = int(min(want, room))
            lease = f"bl_{secrets.token_hex(12)}"
            con.execute(
                "INSERT INTO budget_reservations (id,task_id,generation,tokens,expires_at,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (lease, str(task_id), int(generation) if str(generation).isdigit() else 0, granted,
                 now + max(60, int(ttl_seconds)), now))
            con.commit()
            return {"status": "granted", "lease": lease, "tokens": granted, **reading, "held": held + granted}
        except BaseException:
            con.rollback()
            raise


def settle_request(con, lease, task_id, generation, tokens):
    """Record what one request used and give its lease back, in one transaction. A lease that has
    already expired was charged in full when it did: nothing more is recorded (False)."""
    now = int(time.time())
    with _locked(con):
        con.execute("BEGIN IMMEDIATE")
        try:
            if lease and con.execute("DELETE FROM budget_reservations WHERE id=?", (lease,)).rowcount != 1:
                con.commit()
                return False
            if int(tokens or 0) > 0:
                _usage_row(con, task_id, generation, tokens, now)
            con.commit()
            return True
        except BaseException:
            con.rollback()
            raise


def renew_request(con, lease, ttl_seconds=LEASE_TTL_SECONDS):
    """Keep a streaming request's lease alive; False once it has expired."""
    if not lease:
        return False
    with _locked(con):
        return con.execute("UPDATE budget_reservations SET expires_at=? WHERE id=?",
                           (int(time.time()) + max(60, int(ttl_seconds)), lease)).rowcount == 1


# Characters of the sha256 kept for the URL or query one external call was made
# against. Same 16 as the repo's other privacy digests (the task fingerprints in
# platform/tasks.py): enough that two different pages never collide in
# one run's ledger, short enough that the row stays readable.
_SUBJECT_DIGEST_CHARS = 16


_USAGE_CONTEXT = contextvars.ContextVar("misaka_usage_context", default=None)


@contextmanager
def usage_context(path, task_id, generation, cap=None):
    """Bill a window turn's model requests and tools to ``task_id`` without changing other
    sessions' process environment; ``cap`` None means the owners' cap (``default_cap``)."""
    token = _USAGE_CONTEXT.set((path, task_id, str(generation), cap))
    try:
        yield
    finally:
        _USAGE_CONTEXT.reset(token)


def ledger():
    """``(path, task_id, generation, cap)`` this turn is billed to, or None: a workflow's window turn
    supplies an async-local ``usage_context``; other metered processes the ``MISAKA_USAGE_*``
    environment a worker exports for its card. A session with neither (an interactive chat) has
    nothing to bill. No cap given (an empty ``MISAKA_USAGE_TOKEN_CAP``, the drivers' choice) is
    the owners' cap as settings.json reads at this request: a driver and its cards that each
    kept the value they started with disagreed once it was edited, and the driver relaunched cards
    the cap refused, for ever (0.18.9 sweep)."""
    context = _USAGE_CONTEXT.get()
    if context is None:
        raw_cap = os.environ.get("MISAKA_USAGE_TOKEN_CAP", "")
        context = (os.environ.get("MISAKA_USAGE_DB"), os.environ.get("MISAKA_USAGE_TASK_ID"),
                   os.environ.get("MISAKA_USAGE_GENERATION", ""), int(raw_cap) if raw_cap.isdigit() else None)
    path, task_id, generation, cap = context
    if not path or not task_id:
        return None
    return path, task_id, str(generation or ""), default_cap() if cap is None else int(cap or 0)


def record_external_call(service, *, subject="", **facts):
    """Charge one outbound third-party call to the ledger this turn is billed to.

    Model tokens are only half of what a run spends: a search, a page fetch and
    a download each cost money or quota at somebody's API, and without a row apiece the
    run's real cost cannot be reconstructed afterwards. The row lands in the same
    ``events`` table as ``budget_usage`` -- durable, cross-process, exported with the rest
    of the ledger -- under its own ``kind``, so :func:`spent`'s token arithmetic never
    sees it. This is accounting only: nothing here throttles or refuses a call.

    *subject* is the URL or query the call was made against and is stored ONLY as a
    sha256 prefix: a query is whatever the user typed, and a URL routinely carries a
    session token or a presigned signature that the ledger must not keep. *facts* (a
    backend name, a result count, a byte count) are stored verbatim, so nothing that
    identifies a person may be passed as one.

    A workflow's window turn supplies an async-local usage context. Other callers use
    the environment because these tools hold no board handle: ``MISAKA_USAGE_DB`` / ``_TASK_ID`` / ``_GENERATION`` are
    what a worker already exports to charge the turn's tokens to a card, and an external
    call rides the same three. A session with none of them (an interactive chat) has no
    card to bill and records nothing, exactly as its tokens are not recorded either.

    Never raises: bookkeeping that can fail a tool call is worse than no bookkeeping.
    """

    current = ledger()
    if current is None:
        return False
    path, task_id, generation, _cap = current
    payload = {"service": str(service), **facts}
    if subject:
        payload["subject_sha256"] = hashlib.sha256(
            str(subject).encode("utf-8", "surrogatepass")
        ).hexdigest()[:_SUBJECT_DIGEST_CHARS]
    try:
        from misaka.core.platform import tasks

        con = tasks.connect(path)
        try:
            # Written straight rather than through ``tasks.add_event``, for the same
            # reason ``settle_request`` is: that helper drops any event whose
            # ``task_id`` has no row in ``tasks``, and a workflow's run charges its usage
            # to a run id that lives in another database.
            con.execute(
                "INSERT INTO events (task_id,kind,payload,generation,created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    str(task_id),
                    "external_call",
                    json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
                    int(generation) if generation.isdigit() else None,
                    int(time.time()),
                ),
            )
        finally:
            con.close()
    except Exception:  # noqa: BLE001 - a lost ledger row must never cost the call it accounts for
        return False
    return True


def _on(path, fn, *args, **kwargs):
    from misaka.core.platform import tasks

    con = tasks.connect(path)
    try:
        return fn(con, *args, **kwargs)
    finally:
        con.close()


def lease_request_path(path, *args, **kwargs):
    return _on(path, lease_request, *args, **kwargs)


def settle_request_path(path, *args, **kwargs):
    return _on(path, settle_request, *args, **kwargs)


def renew_request_path(path, *args, **kwargs):
    return _on(path, renew_request, *args, **kwargs)


def cap_reached_path(path, *args, **kwargs):
    return _on(path, cap_reached, *args, **kwargs)


def status_path(path, *args, **kwargs):
    return _on(path, status, *args, **kwargs)

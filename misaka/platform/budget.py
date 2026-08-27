"""Persistent token budgets and the Beast Mode cutoff."""
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import time

logger = logging.getLogger(__name__)

DEFAULT_CAP = int(os.environ.get("MISAKA_TOKEN_CAP", "0"))
BEAST_AT = float(os.environ.get("MISAKA_BEAST_AT", "0.85"))
SUBAGENT_RESERVATION = int(os.environ.get("MISAKA_SUBAGENT_TOKEN_RESERVATION", "32768"))

BEAST_SUFFIX = """

---
⚠️ **The token budget is nearly exhausted (Beast Mode).** No more tools are available.
Use only information already in context and submit an honest final report. If deliverables could not be written,
put every useful conclusion in the `summary` and `notes` fields of `report.json`, set `artifacts` to an empty array,
set `status` to `blocked`, and explain that the budget ended before disk artifacts were completed. Never submit an
empty report or claim `done` when the deliverables do not exist.
"""


def spent(con):
    """Total token usage in the event ledger, counted incrementally.

    The ledger only grows, so re-reading and re-parsing every historical usage event on each
    call -- and the research loop checks the budget on every tick -- is work already done.
    Only rows past the last seen id are parsed. The running total rides on the connection
    object, so it dies with the connection; a plain ``sqlite3.Connection`` cannot carry an
    attribute and simply recounts.
    """
    last_id, total = getattr(con, "_misaka_spent", (0, 0))
    highest = last_id
    for event_id, kind, payload in con.execute(
            "SELECT id,kind,payload FROM events "
            "WHERE id>? AND kind IN ('harn_event','budget_usage') AND payload LIKE '%totalTokens%'",
            (last_id,)):
        highest = max(highest, int(event_id))
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
    try:
        con._misaka_spent = (highest, total)
    except AttributeError:                   # a bare sqlite3.Connection: recount next time
        pass
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


def reserved(con):
    """Return capacity reserved by active agent turns."""

    now = int(time.time())
    nested = bool(getattr(con, "in_transaction", False))
    try:
        if nested:
            con.execute("SAVEPOINT misaka_budget_expiry")
        else:
            con.execute("BEGIN IMMEDIATE")
        _charge_expired_reservations(con, now)
        row = con.execute(
            "SELECT COALESCE(SUM(tokens),0) FROM budget_reservations WHERE expires_at>=?",
            (now,),
        ).fetchone()
        if nested:
            con.execute("RELEASE SAVEPOINT misaka_budget_expiry")
        else:
            con.commit()
    except sqlite3.OperationalError as e:
        try:
            if nested:
                con.execute("ROLLBACK TO SAVEPOINT misaka_budget_expiry")
                con.execute("RELEASE SAVEPOINT misaka_budget_expiry")
            else:
                con.rollback()
        except Exception:  # noqa: BLE001, S110 - preserve the original database error
            pass
        if "no such table" in str(e):
            return 0   # caller-owned legacy/in-memory ledgers may lack the table
        raise
    return int(row[0] or 0)


def status(con, cap=None):
    cap = DEFAULT_CAP if cap is None else cap
    held = reserved(con)
    used = spent(con)
    if not cap:
        return {"mode": "normal", "used": used, "reserved": held, "cap": 0, "ratio": 0.0}
    ratio = (used + held) / cap
    mode = "stop" if ratio >= 1.0 else ("beast" if ratio >= BEAST_AT else "normal")
    return {
        "mode": mode,
        "used": used,
        "reserved": held,
        "cap": cap,
        "ratio": round(ratio, 3),
    }


def reserve_agent(con, cap, task_id, generation, ttl_seconds=1800):
    """Atomically reserve capacity before a nested Agent starts.

    Reservations let parallel and recursive launches share one ledger. In Beast
    Mode the whole remaining budget goes to a single reservation. The final usage
    event is written before the reservation is released.
    """

    cap = DEFAULT_CAP if cap is None else int(cap or 0)
    if not cap:
        return {"allowed": True, "token": None, "tokens": 0, "mode": "normal"}
    now = int(time.time())
    token = f"br_{secrets.token_hex(12)}"
    con.execute("BEGIN IMMEDIATE")
    try:
        _charge_expired_reservations(con, now)
        used = spent(con)
        held = int(
            con.execute(
                "SELECT COALESCE(SUM(tokens),0) FROM budget_reservations"
            ).fetchone()[0]
            or 0
        )
        remaining = cap - used - held
        if remaining <= 0:
            con.rollback()
            return {
                "allowed": False,
                "token": None,
                "tokens": 0,
                "mode": "stop",
                "used": used,
                "reserved": held,
                "cap": cap,
            }
        ratio = (used + held) / cap
        if ratio >= BEAST_AT:
            amount = remaining
            mode = "beast"
        else:
            # Keep normal-mode reservations useful for small caps too, while
            # never allowing concurrent reservations to exceed the hard cap.
            normal_slice = max(1, SUBAGENT_RESERVATION)
            amount = min(normal_slice, remaining)
            mode = "normal"
        con.execute(
            "INSERT INTO budget_reservations "
            "(id,task_id,generation,tokens,expires_at,created_at) VALUES (?,?,?,?,?,?)",
            (token, str(task_id), int(generation), amount, now + max(60, int(ttl_seconds)), now),
        )
        con.commit()
        return {
            "allowed": True,
            "token": token,
            "tokens": amount,
            "mode": mode,
            "used": used,
            "reserved": held + amount,
            "cap": cap,
        }
    except BaseException:
        con.rollback()
        raise


def release_agent(con, token):
    if not token:
        return False
    return con.execute("DELETE FROM budget_reservations WHERE id=?", (token,)).rowcount == 1


def touch_agent(con, token, ttl_seconds=1800):
    if not token:
        return False
    return con.execute(
        "UPDATE budget_reservations SET expires_at=? WHERE id=?",
        (int(time.time()) + max(60, int(ttl_seconds)), token),
    ).rowcount == 1


def commit_agent_usage(con, token, task_id, generation, total_tokens):
    """Record the turn's token usage and release its reservation in one transaction."""

    con.execute("BEGIN IMMEDIATE")
    try:
        total = max(0, int(total_tokens or 0))
        if total:
            con.execute(
                "INSERT INTO events (task_id,kind,payload,generation,created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    str(task_id),
                    "budget_usage",
                    json.dumps({"totalTokens": total}, separators=(",", ":")),
                    int(generation),
                    int(time.time()),
                ),
            )
        if token:
            con.execute("DELETE FROM budget_reservations WHERE id=?", (token,))
        con.commit()
        return True
    except BaseException:
        con.rollback()
        raise


# Characters of the sha256 kept for the URL or query one external call was made
# against. Same 16 as the repo's other privacy digests (extensions/mcp.py, the task
# fingerprints in platform/tasks.py): enough that two different pages never collide in
# one run's ledger, short enough that the row stays readable.
_SUBJECT_DIGEST_CHARS = 16


def record_external_call(service, *, subject="", **facts):
    """Charge one outbound third-party call to the ledger this turn is billed to.

    Model tokens are only half of what a research run spends: a search, a page fetch and
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

    Addressed by environment rather than by argument because the tools that make these
    calls hold no board handle: ``MISAKA_USAGE_DB`` / ``_TASK_ID`` / ``_GENERATION`` are
    what a worker already exports to charge the turn's tokens to a card, and an external
    call rides the same three. A session with none of them (an interactive chat) has no
    card to bill and records nothing, exactly as its tokens are not recorded either.

    Never raises: bookkeeping that can fail a tool call is worse than no bookkeeping.
    """

    path = os.environ.get("MISAKA_USAGE_DB")
    task_id = os.environ.get("MISAKA_USAGE_TASK_ID")
    if not path or not task_id:
        return False
    generation = os.environ.get("MISAKA_USAGE_GENERATION", "")
    payload = {"service": str(service), **facts}
    if subject:
        payload["subject_sha256"] = hashlib.sha256(
            str(subject).encode("utf-8", "surrogatepass")
        ).hexdigest()[:_SUBJECT_DIGEST_CHARS]
    try:
        from misaka.platform import tasks

        con = tasks.connect(path)
        try:
            # Written straight rather than through ``tasks.add_event``, for the same
            # reason ``commit_agent_usage`` is: that helper drops any event whose
            # ``task_id`` has no row in ``tasks``, and a research run charges its usage
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
    except Exception as error:  # noqa: BLE001 - a lost ledger row must never cost the call it accounts for
        logger.debug("external call not accounted (%s): %s", service, error)
        return False
    return True


def reserve_agent_path(path, cap, task_id, generation, ttl_seconds=1800):
    from misaka.platform import tasks

    con = tasks.connect(path)
    try:
        return reserve_agent(con, cap, task_id, generation, ttl_seconds)
    finally:
        con.close()


def release_agent_path(path, token):
    from misaka.platform import tasks

    con = tasks.connect(path)
    try:
        return release_agent(con, token)
    finally:
        con.close()


def touch_agent_path(path, token, ttl_seconds=1800):
    from misaka.platform import tasks

    con = tasks.connect(path)
    try:
        return touch_agent(con, token, ttl_seconds)
    finally:
        con.close()


def commit_agent_usage_path(path, token, task_id, generation, total_tokens):
    from misaka.platform import tasks

    con = tasks.connect(path)
    try:
        return commit_agent_usage(con, token, task_id, generation, total_tokens)
    finally:
        con.close()

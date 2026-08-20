"""预算硬顶 + Beast Mode（宪法⑦）。

用量从 harn 的 agent_end 事件里累加（totalTokens），不另建记账通道。
三档：normal 正常跑｜beast 预算将尽→禁工具强制用已有信息交卷｜stop 耗尽→拒开新卡。
"""
import json
import os
import secrets
import sqlite3
import time

DEFAULT_CAP = int(os.environ.get("MISAKA_TOKEN_CAP", "0"))  # 0 = 不设顶
BEAST_AT = float(os.environ.get("MISAKA_BEAST_AT", "0.85"))  # 用掉这个比例后进 Beast Mode
SUBAGENT_RESERVATION = int(os.environ.get("MISAKA_SUBAGENT_TOKEN_RESERVATION", "32768"))

BEAST_SUFFIX = """

---
⚠️ **预算将尽（Beast Mode）**：本次不给你任何工具。用你已经知道的信息**直接交卷**——
把结论写进产物文件是做不到了，所以：把全部结论写进 report.json 的 summary 与 notes 字段，
artifacts 留空数组，status 填 "blocked"，notes 说明"预算耗尽，未落盘产物"。**决不空手，也不许假报 done。**
"""


def spent(con):
    """已花 token 总量（从事件流累加，事件即账本）。"""
    total = 0
    for kind, payload in con.execute(
            "SELECT kind,payload FROM events "
            "WHERE kind IN ('harn_event','budget_usage') AND payload LIKE '%totalTokens%'"):
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
    """Turn abandoned slices into conservative usage before releasing them."""

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
    """仍在运行的 agent 已原子占用、尚未落成 usage 的预算。"""

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
        except Exception:  # noqa: BLE001 - 回滚失败不掩盖原错
            pass
        if "no such table" in str(e):
            return 0   # caller-owned legacy/in-memory ledgers may lack the table
        raise   # 锁竞争/IO 错吞成 0 会让账本最忙时对预留量失明（审查 2026-08-20）
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

    Reservations make parallel/recursive launches observe one shared ledger;
    an entire remaining Beast budget is single-flight.  The final usage event
    is written before the reservation is released.
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
    """Atomically persist numeric usage and release its reservation."""

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


def reserve_agent_path(path, cap, task_id, generation, ttl_seconds=1800):
    from misaka.extensions.board import db

    con = db.connect(path)
    try:
        return reserve_agent(con, cap, task_id, generation, ttl_seconds)
    finally:
        con.close()


def release_agent_path(path, token):
    from misaka.extensions.board import db

    con = db.connect(path)
    try:
        return release_agent(con, token)
    finally:
        con.close()


def touch_agent_path(path, token, ttl_seconds=1800):
    from misaka.extensions.board import db

    con = db.connect(path)
    try:
        return touch_agent(con, token, ttl_seconds)
    finally:
        con.close()


def commit_agent_usage_path(path, token, task_id, generation, total_tokens):
    from misaka.extensions.board import db

    con = db.connect(path)
    try:
        return commit_agent_usage(con, token, task_id, generation, total_tokens)
    finally:
        con.close()


if __name__ == "__main__":
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE events (kind TEXT, payload TEXT)")
    ev = json.dumps({"type": "agent_end", "messages": [
        {"role": "assistant", "usage": {"totalTokens": 6000}},
        {"role": "assistant", "usage": {"totalTokens": 4000}}]})
    con.execute("INSERT INTO events VALUES ('harn_event', ?)", (ev,))
    con.execute("INSERT INTO events VALUES ('harn_event', ?)",
                (json.dumps({"type": "turn_end", "messages": [{"usage": {"totalTokens": 999}}]}),))
    assert spent(con) == 10000, spent(con)  # 只认 agent_end，不重复计中途帧
    assert status(con, 0)["mode"] == "normal"
    assert status(con, 100000)["mode"] == "normal"
    assert status(con, 11000)["mode"] == "beast", status(con, 11000)
    assert status(con, 9000)["mode"] == "stop"
    assert "决不空手" in BEAST_SUFFIX
    print("budget selfcheck ok — 10k token；无顶=normal，11k顶=beast(91%)，9k顶=stop")

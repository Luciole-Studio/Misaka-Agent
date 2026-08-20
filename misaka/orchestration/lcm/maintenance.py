"""LCM 运维面：status / doctor / backup（hermes-lcm command 面的 misaka 精简版，MIT）。

doctor 分级哲学原样继承（上游免费经验）：绝大多数警告映射「inspect」而非
「cleanup」；检查跑不了＝warning-only 并明说「这不是损坏的证据」——只在有
可操作证据时才降级健康度，防 doctor 变成没人看的常红噪声源。全部只读。
backup＝SQLite Online Backup API 时间戳快照，永不覆盖旧份。
"""
import sqlite3
import time
from pathlib import Path


def _connect_ro(db_path):
    return sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True, timeout=5.0)


def status(db_path):
    """全库统计。库不存在＝空读数不炸。"""
    path = Path(db_path)
    out = {"db": str(path), "exists": path.is_file(),
           "size_bytes": path.stat().st_size if path.is_file() else 0,
           "sessions": 0, "messages": 0, "nodes": 0, "per_session": {}}
    if not out["exists"]:
        return out
    con = _connect_ro(path)
    try:
        out["messages"] = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        out["nodes"] = con.execute("SELECT COUNT(*) FROM summary_nodes").fetchone()[0]
        rows = con.execute(
            """SELECT m.session_id, COUNT(*),
                      (SELECT COUNT(*) FROM summary_nodes n
                       WHERE n.session_id = m.session_id)
               FROM messages m GROUP BY m.session_id ORDER BY m.session_id"""
        ).fetchall()
        out["per_session"] = {r[0]: {"messages": r[1], "nodes": r[2]} for r in rows}
        out["sessions"] = len(out["per_session"])
    finally:
        con.close()
    return out


def doctor(db_path):
    """只读体检。返回 [{check, status: pass|warn|fail, detail, action}]。
    action 三档：safe/ignore ｜ inspect ｜ backup-first repair。"""
    path = Path(db_path)
    checks = []

    def add(check, ok_status, detail, action="safe/ignore"):
        checks.append({"check": check, "status": ok_status,
                       "detail": detail, "action": action})

    if not path.is_file():
        add("database_exists", "pass", "库还没建（首次压缩时自动创建）")
        return checks
    try:
        con = _connect_ro(path)
    except sqlite3.Error as exc:
        add("database_open", "fail", f"打不开：{exc}", "backup-first repair")
        return checks
    try:
        verdict = con.execute("PRAGMA integrity_check").fetchone()[0]
        if verdict == "ok":
            add("database_integrity", "pass", "integrity_check ok")
        else:
            add("database_integrity", "fail", str(verdict)[:200],
                "backup-first repair（先 misaka lcm backup 再处置）")
    except sqlite3.Error as exc:
        add("database_integrity", "warn",
            f"检查跑不了（{exc}）——这不是索引损坏的证据", "safe/ignore")

    for content, fts in (("messages", "messages_fts"), ("summary_nodes", "nodes_fts")):
        try:
            n_content = con.execute(f"SELECT COUNT(*) FROM {content}").fetchone()[0]
            n_fts = con.execute(f"SELECT COUNT(*) FROM {fts}").fetchone()[0]
            if n_content == n_fts:
                add(f"{fts}_sync", "pass", f"{n_content} 行对齐")
            else:
                add(f"{fts}_sync", "warn",
                    f"{content}={n_content} vs {fts}={n_fts}（检索可能漏行）",
                    "inspect（可 INSERT INTO ...(fts) VALUES('rebuild') 重建）")
        except sqlite3.Error as exc:
            add(f"{fts}_sync", "warn",
                f"检查跑不了（{exc}）——不是损坏证据", "safe/ignore")

    try:
        orphan = con.execute(
            """SELECT COUNT(*) FROM summary_nodes n, json_each(n.source_ids) j
               WHERE n.source_type='messages'
               AND CAST(j.value AS INTEGER) NOT IN (SELECT store_id FROM messages)""").fetchone()[0]
        if orphan:
            add("node_lineage", "warn",
                f"{orphan} 条血统指向不存在的消息（展开会缺行，摘要仍可用）",
                "inspect")
        else:
            add("node_lineage", "pass", "血统全部可下钻")
    except sqlite3.Error as exc:
        add("node_lineage", "warn", f"检查跑不了（{exc}）——不是损坏证据",
            "safe/ignore")
    con.close()
    return checks


def backup(db_path, dest_dir=None):
    """时间戳快照（Online Backup API，热库安全）。返回 (路径, None) 或 (None, 错误)。"""
    path = Path(db_path)
    if not path.is_file():
        return None, "库还不存在，没有可备份的"
    dest_root = Path(dest_dir) if dest_dir else path.parent / "backups" / "lcm"
    dest_root.mkdir(parents=True, exist_ok=True)
    dest = dest_root / f"{path.stem}-{time.strftime('%Y%m%d_%H%M%S')}.sqlite3"
    src = sqlite3.connect(str(path), timeout=5.0)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    except sqlite3.Error as exc:
        return None, f"备份失败：{exc}"
    finally:
        src.close()
    return str(dest), None


if __name__ == "__main__":
    import tempfile

    from misaka.orchestration.lcm.dag import SummaryDAG, SummaryNode
    from misaka.orchestration.lcm.store import MessageStore

    root = Path(tempfile.mkdtemp())
    db = root / "lcm.db"

    empty = status(db)
    assert not empty["exists"] and empty["messages"] == 0, "空库不炸"
    assert doctor(db)[0]["status"] == "pass", "库没建＝pass 不吓人"
    assert backup(db)[1] is not None, "没库说人话"

    store = MessageStore(db)
    dag = SummaryDAG(db)
    ids = store.append_batch("s1", [{"role": "user", "content": f"消息{i}"}
                                    for i in range(3)])
    dag.add_node(SummaryNode(session_id="s1", depth=0, summary="摘要",
                             source_ids=ids, source_type="messages"))
    st = status(db)
    assert st["messages"] == 3 and st["nodes"] == 1 \
        and st["per_session"]["s1"]["messages"] == 3

    checks = {c["check"]: c for c in doctor(db)}
    assert checks["database_integrity"]["status"] == "pass"
    assert checks["messages_fts_sync"]["status"] == "pass"
    assert checks["node_lineage"]["status"] == "pass"

    dag.add_node(SummaryNode(session_id="s1", depth=0, summary="坏血统",
                             source_ids=[9999], source_type="messages"))
    bad = {c["check"]: c for c in doctor(db)}
    assert bad["node_lineage"]["status"] == "warn" \
        and bad["node_lineage"]["action"] == "inspect", \
        "孤儿血统＝inspect 不＝cleanup（分级哲学）"

    dest, err = backup(db)
    assert err is None and Path(dest).is_file() and Path(dest).stat().st_size > 0
    snap = sqlite3.connect(dest)
    assert snap.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3, \
        "快照内容完整"
    snap.close()
    dag.close()
    store.close()
    print("lcm maintenance selfcheck ok — 空库友好/统计/体检分级/热备快照 全对")

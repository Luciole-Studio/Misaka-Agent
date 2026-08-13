"""研究图仓库：节点+边，与任务板同库（ponytail: 一个 SQLite 文件够了，跨库 join 是自找麻烦）。

节点 kind：finding 发现｜gap 缺口（前沿的燃料）｜question 问题｜hypothesis 假说（synthesize 溯因产，恒 invented）。
边 kind：supports 支持｜contradicts 反驳｜refines 细化｜from_task 出自哪张卡｜
expanded_to 缺口扩成卡｜explains 假说解释发现｜tested_by 假说由哪张检验卡检验｜
spike_of 刺指回靶（节点或产物路径，/research 模式的思辨红队产出）。
"""
import json
import secrets
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
  id         TEXT PRIMARY KEY,
  kind       TEXT NOT NULL,
  text       TEXT NOT NULL,
  weight     REAL NOT NULL DEFAULT 1.0,   -- 影响权重：越大越值得挖
  status     TEXT NOT NULL DEFAULT 'open', -- open | expanded | merged | dropped
  task_id    TEXT,                          -- 出自哪张卡
  project    TEXT,                          -- 课题归属（显式，比顺 task_id 推可靠；NULL=未分类）
  canon_id   TEXT,                          -- 判重后的代表节点（DSU 已压路径）
  sightings  INTEGER NOT NULL DEFAULT 1,    -- 被独立撞见次数（Good-Turing 的原料）
  provenance TEXT,                           -- 出处三档 verified|analogy|invented
  embedding  TEXT,                          -- JSON float 数组，判重用
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS edges (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  src        TEXT NOT NULL,
  dst        TEXT NOT NULL,
  kind       TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
"""


def init(con):
    con.executescript(SCHEMA)
    for ddl in ("ALTER TABLE nodes ADD COLUMN sightings INTEGER NOT NULL DEFAULT 1",
                "ALTER TABLE nodes ADD COLUMN provenance TEXT",
                "ALTER TABLE nodes ADD COLUMN project TEXT"):
        try:
            con.execute(ddl)
        except sqlite3.OperationalError:
            pass  # 列已在


def init_all(con):
    """一次建齐图层全部表。任何入口（CLI/dispatch/测试）都该调它——
    ponytail: 幂等 CREATE IF NOT EXISTS，重复调用零成本，好过每处漏一张表炸一次。"""
    from misaka.research.kernel import cdcl, evidence, precedent
    init(con)
    evidence.init(con)
    cdcl.init(con)
    precedent.init(con)


def add_node(con, kind, text, weight=1.0, task_id=None, embedding=None, provenance=None,
             project=None):
    nid = "n_" + secrets.token_hex(3)
    con.execute(
        "INSERT INTO nodes (id, kind, text, weight, task_id, project, embedding, provenance, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (nid, kind, text, float(weight), task_id, project,
         json.dumps(embedding) if embedding else None, provenance, int(time.time())),
    )
    return nid


def add_edge(con, src, dst, kind):
    con.execute("INSERT INTO edges (src, dst, kind, created_at) VALUES (?,?,?,?)",
                (src, dst, kind, int(time.time())))


def nodes(con, kind=None, status=None, project=None):
    q, args = "SELECT * FROM nodes WHERE 1=1", []
    if kind:
        q, _ = q + " AND kind=?", args.append(kind)
    if status:
        q, _ = q + " AND status=?", args.append(status)
    if project:  # None=不过滤(全局)，传了才按课题切——饱和/前沿混算的根治
        q, _ = q + " AND project=?", args.append(project)
    return con.execute(q + " ORDER BY weight DESC, created_at", args).fetchall()


def get(con, nid):
    return con.execute("SELECT * FROM nodes WHERE id=?", (nid,)).fetchone()


def set_status(con, nid, status):
    con.execute("UPDATE nodes SET status=? WHERE id=?", (status, nid))


def merge_into(con, dup_id, canon_id):
    """判重合流：dup 指向 canon，权重累加到 canon（同一发现被多次撞见＝更重要）。"""
    con.execute("UPDATE nodes SET status='merged', canon_id=? WHERE id=?", (canon_id, dup_id))
    con.execute("UPDATE nodes SET weight=weight+(SELECT weight FROM nodes WHERE id=?), "
                "sightings=sightings+(SELECT sightings FROM nodes WHERE id=?) WHERE id=?",
                (dup_id, dup_id, canon_id))


def stats(con):
    rows = con.execute("SELECT kind, status, COUNT(*) n FROM nodes GROUP BY kind, status").fetchall()
    edges = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    return rows, edges

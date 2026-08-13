"""证据内容寻址＋claims 台账（宪法⑧：hash 解析不出＝引用非法）。

产物按内容 sha256 存进证据库；每条主张挂 (证据hash + 逐字引文)。
入库时就验引文真在文件里——**写不进台账的引用，就是没有的引用**。
"""
import hashlib
import os
import shutil

CLAIMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id     TEXT NOT NULL,
  task_id     TEXT,
  evidence_sha TEXT NOT NULL,
  source_file TEXT NOT NULL,
  quote       TEXT NOT NULL,     -- 逐字引文，入库时验过真在文件里
  created_at  INTEGER NOT NULL
);
"""


def root():
    return os.path.expanduser(os.environ.get("MISAKA_EVIDENCE", "~/Documents/Misaka/evidence"))


def init(con):
    con.executescript(CLAIMS_SCHEMA)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def store(path):
    """存进证据库，返回 sha。内容寻址：同内容只存一份。"""
    sha = sha256_file(path)
    dst = os.path.join(root(), sha[:2], sha)
    if not os.path.exists(dst):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(path, dst)
    return sha


def blob(sha):
    p = os.path.join(root(), sha[:2], sha)
    return p if os.path.exists(p) else None


def store_artifacts(workspace, artifacts):
    """存一张卡的全部产物。返回 {相对路径: sha}。"""
    out = {}
    for rel in artifacts:
        p = os.path.join(workspace, rel)
        if os.path.isfile(p):
            out[rel] = store(p)
    return out


def _norm(s):
    return "".join(s.split())  # 忽略空白差异——换行/缩进不该让引用失效


def add_claim(con, node_id, task_id, sha, source_file, quote, verify=True):
    """写台账。verify=True 时引文必须真在该 blob 里，否则拒收（返回 False）。"""
    if verify:
        p = blob(sha)
        if not p:
            return False
        with open(p, encoding="utf-8", errors="replace") as f:
            if _norm(quote) not in _norm(f.read()):
                return False
    con.execute(
        "INSERT INTO claims (node_id, task_id, evidence_sha, source_file, quote, created_at)"
        " VALUES (?,?,?,?,?,strftime('%s','now'))",
        (node_id, task_id, sha, source_file, quote))
    return True


def audit(con):
    """全量复核：每条 claim 的 blob 还在吗？引文还对得上吗？返回问题列表。"""
    bad = []
    for r in con.execute("SELECT id, node_id, evidence_sha, source_file, quote FROM claims"):
        p = blob(r[2])
        if not p:
            bad.append((r[0], r[1], "blob 丢失", r[2][:12]))
            continue
        with open(p, encoding="utf-8", errors="replace") as f:
            if _norm(r[4]) not in _norm(f.read()):
                bad.append((r[0], r[1], "引文对不上", r[3]))
    return bad


def coverage(con, store_mod):
    """有几成发现有证据键撑着（宪法⑧的仪表）。"""
    ns = [n for n in store_mod.nodes(con, kind="finding") if n["status"] in ("open", "expanded")]
    backed = {r[0] for r in con.execute("SELECT DISTINCT node_id FROM claims")}
    have = sum(1 for n in ns if n["id"] in backed)
    return {"findings": len(ns), "backed": have,
            "ratio": round(have / len(ns), 3) if ns else 0.0}


if __name__ == "__main__":
    import sqlite3
    import tempfile
    os.environ["MISAKA_EVIDENCE"] = tempfile.mkdtemp()
    ws = tempfile.mkdtemp()
    with open(os.path.join(ws, "a.md"), "w", encoding="utf-8") as f:
        f.write("# 报告\n\n妹妹们的规模是 20000 名克隆体。\n出处：轻小说第3卷。\n")
    con = sqlite3.connect(":memory:")
    init(con)
    shas = store_artifacts(ws, ["a.md", "不存在.md"])
    assert list(shas) == ["a.md"] and len(shas["a.md"]) == 64
    sha = shas["a.md"]
    assert store(os.path.join(ws, "a.md")) == sha, "内容寻址应幂等"
    assert add_claim(con, "n_1", "t_1", sha, "a.md", "规模是 20000 名克隆体"), "真引文该收"
    assert add_claim(con, "n_2", "t_1", sha, "a.md", "妹妹们的规模是\n20000 名克隆体"), "跨行引文该收"
    assert not add_claim(con, "n_3", "t_1", sha, "a.md", "规模是 30000 名"), "假引文必须被拒"
    assert not add_claim(con, "n_4", "t_1", "0" * 64, "x.md", "任意"), "blob 不存在必须被拒"
    assert audit(con) == [], audit(con)
    os.remove(blob(sha))  # 模拟证据丢失
    assert len(audit(con)) == 2, audit(con)
    print("evidence selfcheck ok — 假引文/无 blob 双拒；证据丢失被审计抓出")

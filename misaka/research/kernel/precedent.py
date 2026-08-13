"""判例库（Hyra 评估器共进化的裁剪版）：被推翻的判决沉淀成判例，喂回未来的判官。

只在**人推翻判官**或**安慰剂抓到放水**时立判例——判官自己的日常判决不入库（否则只是复读）。
"""
import json

SCHEMA = """
CREATE TABLE IF NOT EXISTS precedents (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  situation  TEXT NOT NULL,   -- 什么情形
  ruling     TEXT NOT NULL,   -- 正确判法
  source     TEXT,            -- human_override | placebo_leak
  task_id    TEXT,
  embedding  TEXT,
  created_at INTEGER NOT NULL
);
"""
THRESHOLD = 0.45  # 判例检索宁宽勿窄：漏掉判例＝白学，多带一条＝多几十 token


def init(con):
    con.executescript(SCHEMA)


def add(con, situation, ruling, source, task_id, canon):
    vec = canon.embed([situation])
    con.execute("INSERT INTO precedents (situation, ruling, source, task_id, embedding, created_at)"
                " VALUES (?,?,?,?,?,strftime('%s','now'))",
                (situation, ruling, source, task_id, json.dumps(vec[0]) if vec else None))
    return con.execute("SELECT last_insert_rowid()").fetchone()[0]


def relevant(con, card_text, canon, k=3):
    rows = con.execute("SELECT situation, ruling, embedding FROM precedents WHERE embedding IS NOT NULL").fetchall()
    if not rows:
        return []
    vec = canon.embed([card_text])
    if not vec:
        return []
    scored = [(canon.cosine(vec[0], json.loads(e)), s, r) for s, r, e in rows]
    return [(s, r) for sim, s, r in sorted(scored, reverse=True)[:k] if sim >= THRESHOLD]


def as_prompt(cases):
    if not cases:
        return ""
    body = "\n".join(f"- 情形：{s}\n  判法：{r}" for s, r in cases)
    return f"\n# 判例（过去判错被纠正过的同类情形，务必照此掌握尺度）\n{body}\n"


if __name__ == "__main__":
    import sqlite3
    import sys
    sys.path.insert(0, ".")
    from misaka.research.kernel import canon
    con = sqlite3.connect(":memory:")
    init(con)
    if not canon.embed(["x"]):
        print("嵌入服务不可用——判例检索降级为空（设计内）")
    else:
        add(con, "产物缺少验收要求的某一行内容", "缺任一硬性判据即判不通过，不许因整体不错就放行",
            "placebo_leak", "t_x", canon)
        hit = relevant(con, "核对产物是否包含验收要求的所有行", canon)
        miss = relevant(con, "把日文翻译成中文", canon)
        assert hit and not miss, (hit, miss)
        assert "判例" in as_prompt(hit) and as_prompt([]) == ""
        print(f"precedent selfcheck ok — 同类情形召回判例；异域不召回")

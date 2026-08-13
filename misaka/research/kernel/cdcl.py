"""死路学习（CDCL 移植）：一条路线被驳死就做尸检，把死因写成禁令；派卡前先查禁。

同一条死胡同永不进第二次。查禁用语义检索（禁令表小，线性扫；同义复述也拦得住）。
"""
import json

CLAUSES_SCHEMA = """
CREATE TABLE IF NOT EXISTS clauses (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  text       TEXT NOT NULL,      -- 死因：什么样的尝试会失败
  scope      TEXT,               -- 作用域标签（哪类卡适用）
  task_id    TEXT,               -- 源自哪张卡的尸检
  embedding  TEXT,
  hits       INTEGER NOT NULL DEFAULT 0,   -- 拦下过几次
  created_at INTEGER NOT NULL
);
"""

POSTMORTEM = """这张卡失败了。做尸检，只输出 JSON：
{"clause": "一句话说清**什么样的尝试会以同样方式失败**（可迁移的教训，不是这张卡的流水账）",
 "scope": "适用范围标签，如 检索类/写作类/全部"}

规矩：
- clause 要能拦住**未来同类**尝试："对 X 类档案用关键词直搜会被反爬挡，须走 Y 入口"胜过"这次失败了"。
- 只是模型抽风/网络超时这种偶发故障，clause 填空字符串——**偶发故障不该变成禁令**。
- 除 JSON 外不要输出任何字。
"""

# 阈值实测定标（bge-m3；禁令文本 vs 卡片描述是异形对，天然低于 canon 的同义对）：
# 该拦 0.59-0.70，不该拦 0.32-0.43 → 取 0.52。换模型换语种须重量。
THRESHOLD = 0.52


def init(con):
    con.executescript(CLAUSES_SCHEMA)


def learn(con, task, failure_reason, cfg, worker, canon):
    """从失败卡提炼禁令。返回 clause_id 或 None（偶发故障不立禁令）。"""
    from misaka.research.kernel import guard
    prompt = (f"{POSTMORTEM}\n# 卡片\n{task['title']}\n\n{(task['body'] or '')[:800]}\n\n"
              f"# 失败原因\n{guard.untrusted('failure', str(failure_reason)[:800])}")
    obj, _raw, err = worker.run_llm_json(
        f"{cfg['roles_root']}/redteam", prompt,
        cfg["provider"], cfg["default_model"], timeout=180)
    if err or not isinstance(obj, dict):
        return None
    text = (obj.get("clause") or "").strip()
    if len(text) < 10:
        return None  # 偶发故障，不立禁令
    vec = canon.embed([text])
    con.execute(
        "INSERT INTO clauses (text, scope, task_id, embedding, created_at)"
        " VALUES (?,?,?,?,strftime('%s','now'))",
        (text, obj.get("scope"), task["id"], json.dumps(vec[0]) if vec else None))
    return con.execute("SELECT last_insert_rowid()").fetchone()[0]


def check(con, card_text, canon, threshold=THRESHOLD):
    """派卡前查禁。返回命中的 (id, text, sim) 或 None。

    ponytail: **劝告制不是硬拦**——禁令匹配是模糊的，误拦会静默卡住工作。
    命中只把教训注入 prompt + 记事件；真要硬拦等误拦率实测低到可接受再说。
    """
    rows = con.execute("SELECT id, text, embedding FROM clauses WHERE embedding IS NOT NULL").fetchall()
    if not rows:
        return None
    vec = canon.embed([card_text])
    if not vec:
        return None  # 嵌入服务不可用→不拦（降级不阻塞）
    best = None
    for cid, text, emb in rows:
        sim = canon.cosine(vec[0], json.loads(emb))
        if sim >= threshold and (best is None or sim > best[2]):
            best = (cid, text, round(sim, 3))
    if best:
        con.execute("UPDATE clauses SET hits=hits+1 WHERE id=?", (best[0],))
    return best


def clauses(con):
    return con.execute("SELECT id, text, scope, hits FROM clauses ORDER BY hits DESC, id").fetchall()


if __name__ == "__main__":
    import sqlite3
    import sys
    sys.path.insert(0, ".")
    from misaka.research.kernel import canon
    con = sqlite3.connect(":memory:")
    init(con)
    v = canon.embed(["对该馆用关键词直搜会被反爬拦截，须走 OAI 接口"])
    if not v:
        print("嵌入服务不可用——check 会降级为不拦（设计内）")
    else:
        con.execute("INSERT INTO clauses (text, embedding, created_at) VALUES (?,?,0)",
                    ("对该馆用关键词直搜会被反爬拦截，须走 OAI 接口", json.dumps(v[0])))
        hit = check(con, "打算对这个档案馆做关键词直接检索", canon)
        miss = check(con, "把三句日文翻译成中文", canon)
        miss2 = check(con, "说明该档案馆的成立年份与沿革", canon)
        assert hit and hit[2] >= THRESHOLD, hit
        assert miss is None and miss2 is None, (miss, miss2)
        assert con.execute("SELECT hits FROM clauses").fetchone()[0] == 1, "命中该计数"
        print(f"cdcl selfcheck ok — 同义任务被拦(sim {hit[2]})；异域与同域异事均不误拦")

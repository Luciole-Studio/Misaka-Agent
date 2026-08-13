"""底图：人类现成分类表合并成一张网格，供 survey 生成器逐格问"这格与命题通不通"。

首期 3 套（规划 §M5「首期 3-5 套」）：
- OCM（HRAF 文化材料大纲）：人类生活的穷举网格，人文社科最通用
- CAP（比较议程项目）：政府/政策议题的穷举
- JEL：经济学的元素周期表
**只收顶层大类种子**——底图的价值在"提醒你没想到的大陆"，不在穷举末梢。
细目按需再扩（ingest 留了接口）。

诚实边界（D4/规划警告）：每部分类法都是一套冻结的理论，自带政治与盲区
（OCM 是 1930 年代民族志视角；CAP 以现代议会政治为中心；JEL 反映主流经济学疆界）。
底图当审计器（提醒漏了哪块大陆），不当真理表。多表并用即为对冲。
"""
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS basemap (
  id     TEXT PRIMARY KEY,     -- 如 OCM-16
  scheme TEXT NOT NULL,        -- OCM | CAP | JEL
  code   TEXT NOT NULL,
  label  TEXT NOT NULL,
  note   TEXT
);
"""

# 种子：三套分类法的顶层大类（人工录入，来源见各行注释）
SEEDS = [
    # OCM 大类（Outline of Cultural Materials, HRAF；1-8xx 共 79 大类，此处取覆盖面最广的 24）
    ("OCM", "10", "方位与地理", None), ("OCM", "12", "自然环境与资源", None),
    ("OCM", "14", "人口", None), ("OCM", "16", "族群与认同", None),
    ("OCM", "19", "语言", None), ("OCM", "20", "传播与媒介", None),
    ("OCM", "22", "食物与生计", None), ("OCM", "26", "食物消费与仪礼", None),
    ("OCM", "34", "建筑与居住安排", None), ("OCM", "36", "聚落与城镇", None),
    ("OCM", "43", "交换与贸易", None), ("OCM", "46", "劳动与分工", None),
    ("OCM", "47", "商业与金融", None), ("OCM", "48", "交通运输", None),
    ("OCM", "55", "健康与疾病", None), ("OCM", "58", "婚姻", None),
    ("OCM", "59", "亲属与家户", None), ("OCM", "62", "社群组织", None),
    ("OCM", "63", "领土政治组织", None), ("OCM", "67", "法律与制裁", None),
    ("OCM", "69", "冲突与战争", None), ("OCM", "77", "宗教信念与实践", None),
    ("OCM", "81", "知识与科学", None), ("OCM", "87", "生命周期与教育", None),
    # CAP 大类（Comparative Agendas Project，21 个主要议题码）
    ("CAP", "1", "宏观经济", None), ("CAP", "2", "公民权利与少数群体", None),
    ("CAP", "3", "健康", None), ("CAP", "4", "农业", None),
    ("CAP", "5", "劳工与就业", None), ("CAP", "6", "教育", None),
    ("CAP", "7", "环境", None), ("CAP", "8", "能源", None),
    ("CAP", "9", "移民", None), ("CAP", "10", "交通", None),
    ("CAP", "12", "法律犯罪与家庭议题", None), ("CAP", "13", "社会福利", None),
    ("CAP", "14", "住房与城市发展", None), ("CAP", "15", "国内商业与金融", None),
    ("CAP", "16", "国防", None), ("CAP", "17", "科技与通讯", None),
    ("CAP", "18", "对外贸易", None), ("CAP", "19", "国际事务与外援", None),
    ("CAP", "20", "政府运作", None), ("CAP", "21", "公共土地与水资源", None),
    ("CAP", "23", "文化政策", None),
    # JEL 大类（Journal of Economic Literature，20 个一级码）
    ("JEL", "A", "总论与教学", None), ("JEL", "B", "经济思想史与方法论", None),
    ("JEL", "C", "数理与计量方法", None), ("JEL", "D", "微观经济学", None),
    ("JEL", "E", "宏观与货币经济学", None), ("JEL", "F", "国际经济学", None),
    ("JEL", "G", "金融经济学", None), ("JEL", "H", "公共经济学", None),
    ("JEL", "I", "健康教育与福利", None), ("JEL", "J", "劳动与人口经济学", None),
    ("JEL", "K", "法与经济学", None), ("JEL", "L", "产业组织", None),
    ("JEL", "M", "企业管理与商业经济", None), ("JEL", "N", "经济史", None),
    ("JEL", "O", "发展与技术变迁", None), ("JEL", "P", "经济体制", None),
    ("JEL", "Q", "农业与自然资源、环境", None), ("JEL", "R", "城市与区域经济", None),
    ("JEL", "Y", "杂项", None), ("JEL", "Z", "其他专题（文化/体育/旅游）", None),
]


def path():
    return os.path.expanduser(os.environ.get("MISAKA_BASEMAP", "~/.misaka/basemap.db"))


def connect(p=None):
    p = p or path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    con = sqlite3.connect(p, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def load_seeds(con, seeds=None):
    """幂等灌种子。返回入库条数。"""
    n = 0
    for scheme, code, label, note in (seeds or SEEDS):
        cid = f"{scheme}-{code}"
        cur = con.execute(
            "INSERT OR IGNORE INTO basemap (id, scheme, code, label, note) VALUES (?,?,?,?,?)",
            (cid, scheme, code, label, note))
        n += cur.rowcount
    return n


def cells(con, schemes=None):
    q = "SELECT * FROM basemap"
    args = []
    if schemes:
        q += " WHERE scheme IN (%s)" % ",".join("?" * len(schemes))
        args = list(schemes)
    return con.execute(q + " ORDER BY scheme, CAST(code AS INTEGER), code", args).fetchall()


def stats(con):
    return con.execute("SELECT scheme, COUNT(*) n FROM basemap GROUP BY scheme").fetchall()


def survey_body(cells, proposition):
    """网格触达判定卡的合同正文（CLI 与 chat 工具共用同一份模板）。"""
    listing = "\n".join(f"- [{c['id']}] {c['label']}" for c in cells)
    return (f"## 目标\n对命题做**网格触达判定**，写 survey.md。\n\n命题：{proposition}\n\n"
            f"逐格回答「这一格与命题有无实质传导」——**只判通不通，不要在这里展开研究**：\n{listing}\n\n"
            "## 边界\n只做触达判定与机制命名，不做实质考证（那是后续卡的事）。\n"
            "不许为了显得全面而把无关格判成相关。\n\n"
            "## 验收\n- survey.md 存在\n"
            f"- 上列 {len(cells)} 格**每格都有一行判定**（格号 + 通/不通）\n"
            "- 判「通」的格必须写出**具体传导机制**（谁通过什么影响什么），空泛相关一律算不通\n"
            "- 末尾列出「判不通但心里没底」的格（供抽审）")


if __name__ == "__main__":
    import tempfile
    p = os.path.join(tempfile.mkdtemp(), "basemap.db")
    con = connect(p)
    n1 = load_seeds(con)
    n2 = load_seeds(con)  # 幂等
    assert n1 == len(SEEDS) and n2 == 0, (n1, n2)
    got = {r["scheme"] for r in stats(con)}
    assert got == {"OCM", "CAP", "JEL"}, got
    assert len(cells(con, ["JEL"])) == 20
    ids = [r["id"] for r in cells(con)]
    assert len(ids) == len(set(ids)), "格 id 必须唯一"
    b = survey_body(cells(con, ["JEL"]), "试命题")
    assert "## 验收" in b and "20 格" in b and "试命题" in b, b[:80]
    print(f"basemap selfcheck ok — 三套分类法 {n1} 格，幂等灌种，" +
          " ".join(f"{r['scheme']}={r['n']}" for r in stats(con)))

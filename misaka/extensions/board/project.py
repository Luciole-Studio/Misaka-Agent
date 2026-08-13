"""课题（project）＝一个目录:课题说明与原始材料的家。

设计(2026-08-08 用户裁定,一路削到最小):
- 课题的"真相"是**文件系统里的目录**,不是数据库行——`~/Documents/Misaka/projects/<名>/`。
- 里面 `PROJECT.md` 写目标/赌注/边界,谁看谁 read 路径,**不进任何 db**
  (读得完的东西直接读,不 ingest;ingest 是留给读不完的长材料的)。
- 卡靠 `tasks.project` 字段存课题**名**(=目录名)指回来;board 只存指针。
- "目录存在＝课题已注册":建卡时校验目录在,拼错当场挡下(防 assignee 那种化石漂移)。

所以这里只管目录:建/列/校验。零数据库,零 ingest。
"""
import os
import re

from misaka.config import CFG

PROJECT_TEMPLATE = """# {name}

## 目标
（一句话:这个课题要搞明白什么。）

## 赌注
（开工前下的反直觉判断——赌哪个主流说法是错的。要能被证伪。）

## 边界
（不做什么;什么算跑题。）

## 分工
（谁负责什么——建卡分派时照此。示例:10032＝档案与文献;10033＝数据核算。）

## 诚实边界表
（未探 / 已探拿不到 / 结构零——交付物的一部分,随研究更新。）
"""


def root():
    return CFG["projects_root"]


def _valid_name(name):
    return bool(re.fullmatch(r"[\w][\w.-]*", name or ""))


def path(name):
    return os.path.join(root(), name)


def exists(name):
    """课题目录在＝已注册。空名/非法名一律 False。"""
    return bool(_valid_name(name)) and os.path.isdir(path(name))


def listing():
    r = root()
    if not os.path.isdir(r):
        return []
    return sorted(d for d in os.listdir(r)
                  if os.path.isdir(os.path.join(r, d)) and not d.startswith("."))


def create(name):
    """返回 (成功?, 消息)。建目录＋PROJECT.md 骨架;重名/非法拒。"""
    if not _valid_name(name):
        return False, f"课题名「{name}」不合法(字母数字._-)"
    p = path(name)
    if os.path.isdir(p):
        return False, f"课题「{name}」已存在:{p}"
    os.makedirs(p)
    md = os.path.join(p, "PROJECT.md")
    if not os.path.exists(md):
        with open(md, "w", encoding="utf-8") as f:
            f.write(PROJECT_TEMPLATE.format(name=name))
    return True, (f"课题「{name}」已建:{p}\n"
                  f"编辑 {md} 写目标/赌注;建卡时 --project {name} 归属它,"
                  f"原始材料也放这个目录。")


def states(con):
    """{课题名: {"archived": bool, "pinned_at": float|None}}——只含设过状态的。"""
    return {r["name"]: {"archived": bool(r["archived"]), "pinned_at": r["pinned_at"]}
            for r in con.execute("SELECT name,archived,pinned_at FROM projects")}


def set_state(con, name, *, archived=None, pinned=None):
    """改归档/置顶。返回 (成功?, 消息)。课题目录必须存在（防拼错立行）。"""
    if not exists(name):
        return False, f"没有课题「{name}」"
    con.execute("INSERT INTO projects(name) VALUES(?) "
                "ON CONFLICT(name) DO NOTHING", (name,))
    if archived is not None:
        con.execute("UPDATE projects SET archived=? WHERE name=?",
                    (int(archived), name))
    if pinned is not None:
        import time
        con.execute("UPDATE projects SET pinned_at=? WHERE name=?",
                    (time.time() if pinned else None, name))
    verbs = []
    if archived is not None:
        verbs.append("已归档" if archived else "已恢复进行中")
    if pinned is not None:
        verbs.append("已置顶" if pinned else "已取消置顶")
    return True, f"课题「{name}」{'、'.join(verbs)}"


def delete(con, name, *, with_cards=False):
    """删课题：目录软删（改名进 .trash-*，材料可反悔）＋清 projects 状态行。
    还有卡指着时：默认拒删（先归档）；with_cards=True 则连卡一起硬删
    （卡是审计记录，级联删除违留痕——入口必须人显式点头）。返回 (成功?, 消息)。"""
    if not exists(name):
        return False, f"没有课题「{name}」"
    rows = con.execute("SELECT id,status FROM tasks WHERE project=?", (name,)).fetchall()
    if rows and not with_cards:
        return False, (f"课题「{name}」还有 {len(rows)} 张卡挂着，不能删——"
                       f"想收起来用归档，要连卡一起删加 --with-cards")
    active = [r["id"] for r in rows
              if r["status"] in ("running", "verifying", "finalizing")]
    if active:
        return False, f"课题「{name}」有卡在跑（{'、'.join(active)}），先 stop 再删"
    from misaka.extensions.board.db import delete_task
    for r in rows:
        delete_task(con, r["id"], allow_active=True)   # 已排除在跑的
    import time
    trash = os.path.join(root(), f".trash-{name}-{int(time.time())}")
    os.rename(path(name), trash)
    con.execute("DELETE FROM projects WHERE name=?", (name,))
    tail = f"（连删 {len(rows)} 张卡；" if rows else "（"
    return True, f"课题「{name}」已删{tail}材料还在 {trash}，反悔手动挪回来）"


def require(name):
    """建卡入口的校验闸:project 必须指向真实课题目录。None/空＝未分类(放行)。

    渐进采用:不强制每张卡都有课题;但一旦给了名,就必须是注册过的目录,
    否则当场报错——不让"苏联档案"和"苏联档桉"这种拼写漂移悄悄进板。
    """
    if not name:
        return None
    if not exists(name):
        avail = "、".join(listing()) or "(还没有课题,先 misaka project <名> 建一个)"
        raise ValueError(f"没有课题「{name}」。已注册:{avail}")
    return name


if __name__ == "__main__":
    import tempfile
    CFG["projects_root"] = tempfile.mkdtemp()

    assert listing() == []
    ok, msg = create("misaka-network")
    assert ok and exists("misaka-network"), msg
    assert os.path.isfile(os.path.join(path("misaka-network"), "PROJECT.md"))
    template = open(os.path.join(path("misaka-network"), "PROJECT.md"), encoding="utf-8").read()
    assert "赌注" in template and "## 分工" in template, "模板须含赌注与分工节"
    assert not create("misaka-network")[0], "重名该拒"
    assert not create("a/b")[0], "路径穿越该拒"
    assert listing() == ["misaka-network"]

    assert require(None) is None and require("") is None      # 未分类放行
    assert require("misaka-network") == "misaka-network"      # 注册过放行
    try:
        require("苏联档桉")                                    # 拼错当场挡
        raise AssertionError("未注册课题该报错")
    except ValueError as e:
        assert "没有课题" in str(e), e

    # 状态：归档/置顶/删除（软删）
    from misaka.extensions.board import db as bdb
    con = bdb.connect(os.path.join(CFG["projects_root"], "t.db"))
    ok, msg = set_state(con, "misaka-network", archived=True)
    assert ok and "已归档" in msg
    assert states(con)["misaka-network"]["archived"] is True
    ok, msg = set_state(con, "misaka-network", archived=False, pinned=True)
    assert ok and "进行中" in msg and "置顶" in msg
    assert states(con)["misaka-network"]["pinned_at"] is not None
    assert not set_state(con, "不存在的", pinned=True)[0], "拼错当场挡"

    con.execute("INSERT INTO tasks(id,title,assignee,status,project,created_at) "
                "VALUES('t_x','x','10032','ready','misaka-network',0)")
    assert not delete(con, "misaka-network")[0], "有卡挂着默认拒删"
    # 级联删除：连卡带删（--with-cards），卡的 events 也一并抹
    con.execute("INSERT INTO events(task_id,kind,created_at) VALUES('t_x','ready',0)")
    ok, msg = delete(con, "misaka-network", with_cards=True)
    assert ok and not exists("misaka-network"), msg
    assert con.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] == 0, "卡被级联删"
    assert con.execute("SELECT COUNT(*) AS n FROM events WHERE task_id='t_x'"
                       ).fetchone()["n"] == 0, "卡的事件也被清"
    assert any(d.startswith(".trash-misaka-network") for d in os.listdir(CFG["projects_root"])), \
        "软删：材料进 .trash 不丢"
    assert listing() == [], "trash 目录不该出现在列表里"
    # 在跑的卡拦住级联删
    create("赫鲁晓夫")
    con.execute("INSERT INTO tasks(id,title,assignee,status,project,created_at) "
                "VALUES('t_run','跑','10032','running','赫鲁晓夫',0)")
    assert not delete(con, "赫鲁晓夫", with_cards=True)[0], "有卡在跑先 stop 再删"
    print("project selfcheck ok — 建/列/校验 + 归档/置顶/软删 + 级联删卡（拒删在跑）")

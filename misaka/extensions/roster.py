"""名册维护：`/create [编号]` 配置向导建御坂、`/remove [编号]` 除名。

CLI 同名同功：`misaka create 10033`（缺的字段进向导问）、`misaka remove 10033`。

名册＝目录：~/.misaka/profiles/sisters/<编号>/（Step-23 起人格住用户态）。
建＝SOUL.md（向导可注入一句话人格）＋可选 config.json 钉模型＋空 skills/；
删＝整目录移除——卡片、工作区、transcript 都跟板走，除名不动它们；
有活卡（running/verifying/finalizing）时拒删，先 misaka_sister_stop。
"""
import json
import os
import re
import shutil
import sys

ROOT = os.path.expanduser("~/.misaka/profiles/sisters")
ACTIVE = ("running", "verifying", "finalizing")
MODEL_CHOICES = ["默认（跟随全局）", "claude-opus-5", "claude-sonnet-5", "gemini-3.5-flash", "手输…"]

SOUL_TEMPLATE = """# 御坂{sid}

你是御坂网络的 Sister {sid}。{persona}

## 专长
- {specialty}

## 边界
- 只做卡片合同里的事；交卷走 report.json，产物必须是工作区内的真实文件。
"""


def _valid(sid):
    return bool(re.fullmatch(r"[\w][\w.-]*", sid or "")) and sid not in {"last-order", "last_order"}


def roster_names(root=None):
    root = root or ROOT
    if not os.path.isdir(root):
        return []
    return sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))


def card_counts(sid, db_path=None):
    """该御坂名下卡片按状态计数；板不存在＝全零。"""
    from misaka.config import CFG
    from misaka.extensions.board import db as board_db
    path = os.path.expanduser(db_path or CFG["db"])
    if not os.path.exists(path):
        return {}
    con = board_db.connect(path)
    try:
        rows = con.execute(
            "SELECT status, COUNT(*) FROM tasks WHERE assignee=? GROUP BY status", (sid,)
        ).fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        con.close()


def create_sister(sid, root=None, persona=None, model=None):
    """返回 (成功?, 消息)。建目录＋SOUL（含向导注入的人格）＋空 skills/＋可选模型钉。"""
    root = root or ROOT
    if not _valid(sid):
        return False, f"编号「{sid}」不合法（字母数字._-，且不是 last-order）"
    prof = os.path.join(root, sid)
    if os.path.exists(prof):
        return False, f"御坂{sid} 已在册：{prof}"
    os.makedirs(os.path.join(prof, "skills"))
    persona = (persona or "").strip()
    with open(os.path.join(prof, "SOUL.md"), "w", encoding="utf-8") as f:
        f.write(SOUL_TEMPLATE.format(
            sid=sid,
            persona=persona or "（在这里写她的人格、专长与说话方式。）",
            specialty=persona or "-",
        ))
    pinned = ""
    if model:
        with open(os.path.join(prof, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"model": model}, f, ensure_ascii=False, indent=2)
        pinned = f"模型已钉 {model}；"
    return True, (f"御坂{sid} 已在册。{pinned}人格：{prof}/SOUL.md；"
                  f"可选 config.yaml 写 mcp_servers、skills/ 放技能软链。"
                  f"/sister {sid} 即可切过去。")


def remove_sister(sid, root=None, db_path=None):
    """返回 (成功?, 消息)。有活卡拒删；卡片/工作区/transcript 不动。"""
    root = root or ROOT
    prof = os.path.join(root, sid)
    if not _valid(sid) or not os.path.isdir(prof):
        return False, f"没有御坂「{sid}」（名册看 /sisters）"
    counts = card_counts(sid, db_path)
    live = {k: v for k, v in counts.items() if k in ACTIVE}
    if live:
        return False, f"御坂{sid} 有活卡在跑（{live}）——先 misaka_sister_stop 或等验收完，再除名。"
    shutil.rmtree(prof)
    rest = "、".join(f"{k}×{v}" for k, v in sorted(counts.items())) or "无"
    return True, f"御坂{sid} 已除名（目录已删）。板上历史卡（{rest}）与工作区/transcript 原样保留。"


# ── TUI：/create 与 /remove（配置向导）───────────────────────────────


def register(harn):
    async def create_cmd(args, ctx):
        sid = (args or "").strip()
        if not sid:
            sid = await ctx.ui.input("新御坂的编号", "如 10033")
            sid = (sid or "").strip()
            if not sid:
                ctx.ui.notify("取消了（没给编号）", "info")
                return
        if not _valid(sid) or os.path.exists(os.path.join(ROOT, sid)):
            ok, msg = create_sister(sid)      # 借它产出同一套报错文案
            ctx.ui.notify(msg, "error")
            return
        persona = await ctx.ui.input(
            f"御坂{sid} 的一句话人格/专长（回车＝先留骨架自己编辑）",
            "如 专攻苏联档案的文献猎手")
        if persona is None:
            ctx.ui.notify("取消了", "info")
            return
        model_pick = await ctx.ui.select(f"御坂{sid} 钉模型？", MODEL_CHOICES)
        if model_pick is None:
            ctx.ui.notify("取消了", "info")
            return
        model = None
        if model_pick == "手输…":
            model = await ctx.ui.input("模型 ID", "如 claude-opus-5")
            if model is None:
                ctx.ui.notify("取消了", "info")
                return
            model = model.strip() or None
        elif not model_pick.startswith("默认"):
            model = model_pick
        summary = (f"人格：{persona.strip() or '（骨架，稍后自己写）'}｜"
                   f"模型：{model or '跟随全局'}")
        if not await ctx.ui.confirm(f"建御坂{sid}？", summary):
            ctx.ui.notify("取消了", "info")
            return
        ok, msg = create_sister(sid, persona=persona, model=model)
        ctx.ui.notify(msg, "info" if ok else "error")

    async def remove_cmd(args, ctx):
        raw = (args or "").strip()
        force = raw.endswith("!")
        sid = raw.rstrip("!").strip()
        if not sid:
            names = roster_names()
            if not names:
                ctx.ui.notify("名册是空的", "info")
                return
            sid = await ctx.ui.select("除名谁？", names)
            if not sid:
                return
        if sid == (os.environ.get("MISAKA_WHO") or "last-order"):
            ctx.ui.notify(f"不能在御坂{sid} 自己的会话里给她除名——人格是启动时装进内存的，"
                          f"删了目录这个窗口还会照答（幽灵会话）。先 /sister last-order 切走再删。", "error")
            return
        if not force:
            counts = card_counts(sid)
            rest = "、".join(f"{k}×{v}" for k, v in sorted(counts.items())) or "无"
            ok = await ctx.ui.confirm(
                f"除名御坂{sid}？",
                f"将删除 ~/.misaka/profiles/sisters/{sid}/（人格+技能挂载）。"
                f"板上历史卡（{rest}）与工作区不受影响。")
            if not ok:
                return
        ok, msg = remove_sister(sid)
        ctx.ui.notify(msg, "info" if ok else "error")

    harn.registerCommand("create", {
        "description": "新建御坂（配置向导：编号→人格→模型），落到 ~/.misaka/profiles/sisters/",
        "handler": create_cmd,
    })
    harn.registerCommand("remove", {
        "description": "御坂除名：删人格目录（历史卡与工作区保留；活卡在跑时拒绝）",
        "handler": remove_cmd,
    })


# ── CLI：misaka create / misaka remove ──────────────────────────────


def cli_create(sid=None, desc=None, model=None, root=None):
    """缺的字段在 tty 里问；非 tty 缺编号直接报错。返回退出码。"""
    interactive = sys.stdin.isatty()
    if not sid:
        if not interactive:
            print("缺编号：misaka create <编号>")
            return 1
        sid = input("编号（如 10033）：").strip()
        if not sid:
            print("取消了")
            return 1
    if desc is None and interactive:
        desc = input("一句话人格/专长（回车跳过）：").strip()
    if model is None and interactive:
        model = input("钉模型（回车＝跟随全局）：").strip()
    ok, msg = create_sister(sid, root=root, persona=desc or None, model=model or None)
    print(msg)
    return 0 if ok else 1


def cli_remove(sid, yes=False, root=None, db_path=None):
    """非 tty 且没 --yes 一律拒删。返回退出码。"""
    if not yes:
        counts = card_counts(sid, db_path)
        rest = "、".join(f"{k}×{v}" for k, v in sorted(counts.items())) or "无"
        if not sys.stdin.isatty():
            print(f"非交互环境删户口要 --yes（御坂{sid} 历史卡：{rest}）")
            return 1
        answer = input(f"除名御坂{sid}？删 ~/.misaka/profiles/sisters/{sid}/，历史卡（{rest}）保留 [y/N] ")
        if answer.strip().lower() not in {"y", "yes"}:
            print("取消了")
            return 1
    ok, msg = remove_sister(sid, root=root, db_path=db_path)
    print(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    import tempfile

    root = tempfile.mkdtemp()
    ok, msg = create_sister("10777", root=root, persona="专攻苏联档案", model="claude-opus-5")
    assert ok, msg
    soul = open(os.path.join(root, "10777", "SOUL.md"), encoding="utf-8").read()
    assert "专攻苏联档案" in soul, "向导人格该进 SOUL"
    cfgj = json.load(open(os.path.join(root, "10777", "config.json"), encoding="utf-8"))
    assert cfgj == {"model": "claude-opus-5"}, cfgj
    assert os.path.isdir(os.path.join(root, "10777", "skills"))
    ok2, _ = create_sister("10778", root=root)          # 无人格无模型＝纯骨架
    assert ok2 and not os.path.exists(os.path.join(root, "10778", "config.json"))
    assert not create_sister("10777", root=root)[0], "重号该拒"
    assert not create_sister("last-order", root=root)[0], "保留名该拒"
    assert not create_sister("a/b", root=root)[0], "路径穿越该拒"
    assert roster_names(root) == ["10777", "10778"]

    # 活卡守卫：running 拒删，终态可删
    from misaka.extensions.board import db as board_db
    dbp = os.path.join(root, "board.db")
    con = board_db.connect(dbp)
    tid = board_db.create_task(con, "占位卡", body="x", assignee="10777")
    assert remove_sister("没有的", root=root, db_path=dbp)[0] is False
    con.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,)); con.commit()
    ok, msg = remove_sister("10777", root=root, db_path=dbp)
    assert not ok and "活卡" in msg, msg
    con.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,)); con.commit()
    ok, msg = remove_sister("10777", root=root, db_path=dbp)
    assert ok and not os.path.exists(os.path.join(root, "10777")), msg
    assert "done×1" in msg, msg

    # CLI 非交互面：全参建、--yes 删（避开真 tty）
    assert cli_create("10779", desc="测试", model="", root=root) == 0
    assert cli_remove("10779", yes=True, root=root, db_path=dbp) == 0
    assert cli_remove("10779", yes=True, root=root, db_path=dbp) == 1   # 已不存在

    class FakeHarn:
        def __init__(self):
            self.cmds = {}

        def registerCommand(self, name, options):
            assert callable(options.get("handler")), options
            self.cmds[name] = options

    h = FakeHarn()
    register(h)
    assert sorted(h.cmds) == ["create", "remove"], h.cmds

    # 自噬守卫：在御坂自己的会话里删她自己，带 ! 也拒
    import asyncio

    class FakeUI:
        def __init__(self):
            self.notes = []

        def notify(self, msg, typ=None):
            self.notes.append((typ, msg))

    class FakeCtx:
        def __init__(self):
            self.ui = FakeUI()

    os.environ["MISAKA_WHO"] = "10777"
    fctx = FakeCtx()
    asyncio.run(h.cmds["remove"]["handler"]("10777 !", fctx))
    assert fctx.ui.notes and fctx.ui.notes[0][0] == "error" and "切走" in fctx.ui.notes[0][1], fctx.ui.notes
    del os.environ["MISAKA_WHO"]
    print("roster selfcheck ok — 向导字段落盘＋三拒＋活卡守卫＋自噬守卫(!也拒)＋CLI 两面＋/create,/remove 注册 均正确")

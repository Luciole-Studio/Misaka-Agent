"""`/sisters` 看名册（列出全部并可挑）、`/sister <编号>` 直接切到指定御坂。

为什么是换进程而不是换人格：harn 的 `appendSystemPrompt` 是**启动时**字段，
运行时没有 setSystemPrompt/setSkills——同进程内换不了人格。
所以 两者都走 `os.execv` 原地替换进程：同一个终端窗口、同一个 PID，
标题与配色立刻变成目标 agent，对用户就是"在 UI 里切"。

代价（诚实记账）：当前对话不带走（对面是另一个 agent，本来也不该继承）。
切之前会提示，用 `/sister <编号> !` 跳过确认。
"""
import os
import sys



def _roster():
    root = os.path.expanduser("~/.misaka/profiles/sisters")
    sisters = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))) \
        if os.path.isdir(root) else []
    return ["last-order"] + sisters


def _current():
    return os.environ.get("MISAKA_WHO") or "last-order"


def register(harn):
    # harn 实现是 registerCommand(name, {handler, description})——
    # 文档写的 register_command(name, description=...) 不存在（与 registerTool 同款偏差）
    async def misaka_switch(args, ctx):
        roster, cur = _roster(), _current()
        raw = (args or "").strip()
        force = raw.endswith("!")
        name = raw.rstrip("!").strip()

        if not name:  # 没给名字：列出名册让用户挑
            picked = await ctx.ui.select(
                f"当前是 {cur}，切到谁？",
                [f"{n}{'（当前）' if n == cur else ''}" for n in roster])
            if not picked:
                return
            name = picked.split("（")[0]

        if name not in roster:
            ctx.ui.notify(f"没有「{name}」。名册：{', '.join(roster)}", "error")
            return
        if name == cur:
            ctx.ui.notify(f"已经在 {cur} 了", "info")
            return
        if os.environ.get("MISAKA_NET_PANE"):
            # 面板里走 herdr 方式：在旁边开她的格子，当前对话原地保留（不换进程不清屏）
            from misaka.net import client as net
            argv = [sys.executable, "-m", "misaka", "chat"]
            title = "Last Order" if name == "last-order" else name
            if name != "last-order":
                argv += ["--as", name]
            out = net.request("pane.create",
                              {"argv": argv, "cwd": os.getcwd(), "title": title})
            ctx.ui.notify(f"已在旁边开 {name} 的格子（{out['pane_id']}）；"
                          "鼠标点侧边栏或 ctrl+b 数字切过去", "info")
            return
        if not force:
            ok = await ctx.ui.confirm(
                f"切到 {name}？",
                "会重开一个会话——当前对话不会带过去（对面是另一个 agent）。")
            if not ok:
                return

        argv = [sys.executable, "-m", "misaka", "chat"]
        if name != "last-order":
            argv += ["--as", name]
        ctx.ui.notify(f"切到 {name}…", "info")
        os.execv(sys.executable, argv)   # 原地替换本进程：同窗口、同 PID

    async def sisters_roster(args, ctx):
        """/sisters：看名册，选中即切。"""
        await misaka_switch("", ctx)

    async def sister_switch(args, ctx):
        """/sister <编号>：直接切到指定御坂；不给编号就报可用编号。"""
        name = (args or "").strip()
        if not name:
            sisters = [n for n in _roster() if n != "last-order"]
            ctx.ui.notify(f"用法：/sister <编号>，如 /sister {sisters[0] if sisters else '10032'}"
                          f"（在册：{', '.join(sisters) or '无'}；看名册用 /sisters）", "info")
            return
        await misaka_switch(name, ctx)

    harn.registerCommand("sisters", {
        "description": "名册：列出 Last Order 与全部御坂，选中即切换",
        "handler": sisters_roster,
    })
    harn.registerCommand("sister", {
        "description": "切到指定御坂，如 /sister 10032（切回编排官用 /sister last-order）",
        "handler": sister_switch,
    })




if __name__ == "__main__":
    r = _roster()
    assert r[0] == "last-order" and len(r) >= 2, r
    assert "10032" in r, r
    assert _current() == "last-order"
    os.environ["MISAKA_WHO"] = "10032"
    assert _current() == "10032"
    del os.environ["MISAKA_WHO"]

    class FakeHarn:
        def __init__(self):
            self.cmds = {}

        def registerCommand(self, name, options):
            assert callable(options.get("handler")), options
            self.cmds[name] = options

    h = FakeHarn()
    register(h)
    assert sorted(h.cmds) == ["sister", "sisters"], h.cmds
    print(f"switch selfcheck ok — /sisters(名册)+/sister(直切) 已注册；名册 {'/'.join(r)}")

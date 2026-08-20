"""只读查看斜杠命令：/board /graph /trace——人想瞄一眼，不必开终端或劳烦 LO 跑工具。

hermes 的分界精神：斜杠管会话与查看（它的 Info 类 17 个全是只读），工具管做事。
misaka 此前的倒挂是查看类只有 agent 工具（misaka_board/graph/tree），人反而没有
直接入口。三个命令全是薄壳转发现成实现，零花费、不改任何状态。
"""


def register(harn):
    from misaka.config import CFG
    from misaka.extensions.board import db

    def _con():
        con = db.connect(CFG["db"])
        from misaka.research.kernel import store
        store.init_all(con)   # 图表可能还没建（observe/graph 都会查 edges）；幂等零成本
        return con

    async def board_cmd(args, ctx):
        from misaka.extensions.board import tail
        ctx.ui.notify(tail.board_text(_con()) or "(板上无卡)", "info")

    harn.registerCommand("board", {
        "handler": board_cmd,
        "description": "看板：全部课题与卡片状态（只读）"})

    async def graph_cmd(args, ctx):
        from misaka.research.kernel import store
        con = _con()
        rows, nedges = store.stats(con)
        lines = [f"边: {nedges}"]
        lines += [f"  {r['kind']:<9} {r['status']:<9} {r['n']}" for r in rows]
        open_nodes = store.nodes(con, status="open")[:15]
        if open_nodes:
            lines.append("── open 节点（前 15）──")
            lines += [f"  {n['id']}  {n['kind']:<8} w={n['weight']:.2f}  {n['text'][:70]}"
                      for n in open_nodes]
        ctx.ui.notify("\n".join(lines) if rows or open_nodes else "(研究图是空的)", "info")

    harn.registerCommand("graph", {
        "handler": graph_cmd,
        "description": "研究图：节点/边统计与 open 前沿（只读）"})

    async def trace_cmd(args, ctx):
        from misaka.extensions.board import observe
        project = (args or "").strip() or None
        ctx.ui.notify(observe.render(_con(), project), "info")

    harn.registerCommand("trace", {
        "handler": trace_cmd,
        "description": "执行迹：课题→卡→过程脉搏；/trace <课题> 只看某课题（只读）"})


if __name__ == "__main__":
    import asyncio
    import os
    import tempfile
    import types

    tmp = tempfile.mkdtemp()
    os.environ["HOME"] = tmp
    CFG_PATH = os.path.join(tmp, "board.db")
    from misaka.config import CFG
    CFG["db"] = CFG_PATH
    from misaka.extensions.board import db as _db
    con = _db.connect(CFG_PATH)
    cmds = {}
    harn = types.SimpleNamespace(registerCommand=lambda n, o: cmds.setdefault(n, o))
    register(harn)
    assert set(cmds) == {"board", "graph", "trace"}

    notes = []
    ctx = types.SimpleNamespace(ui=types.SimpleNamespace(
        notify=lambda text, level=None: notes.append(text)))
    asyncio.run(cmds["board"]["handler"]("", ctx))
    assert "无卡" in notes[-1], notes[-1]
    _db.create_task(con, "测试卡", body="b", assignee="10032")
    asyncio.run(cmds["board"]["handler"]("", ctx))
    assert "测试卡" in notes[-1] and "10032" in notes[-1]
    asyncio.run(cmds["trace"]["handler"]("", ctx))
    assert "测试卡" in notes[-1] or "课题" in notes[-1]
    asyncio.run(cmds["graph"]["handler"]("", ctx))
    assert "空的" in notes[-1], "无节点无边＝如实说空，不打零表"
    from misaka.research.kernel import store as _store
    _store.add_node(_db.connect(CFG_PATH), "gap", "测试缺口")
    asyncio.run(cmds["graph"]["handler"]("", ctx))
    assert "边" in notes[-1] and "gap" in notes[-1], notes[-1]
    print("views selfcheck ok — /board /graph /trace 三件只读命令")

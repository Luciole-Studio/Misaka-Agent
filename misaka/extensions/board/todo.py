"""微观代办（todos）：sis 卡内子任务树——报联相的数据层。

设计（2026-08-17 用户裁定，docs 见对话）：
- 落 board.db（与宏观代办＝卡同库同视图），但**不碰卡的状态机**——板仍归 LO。
  sis 拿到的是烧死 task_id 的工具（2 期）；这里所有写口都带 task_id 限权，
  越卡写当场拒，权限在数据层就锁死，不指望调用方自觉。
- 树＝parent_id 单链，只有「增」与「标」（无重挂父、无删）——父必先于子存在，
  环在构造上不可能；删除只随卡级联（delete_task）。
- 四态 open/doing/done/blocked；blocked 必须带 note（卡在哪＝相谈的内容）。
- generation 只作审计戳：打回重做沿用同一清单（她接着修），不按代次隔离。
"""
import secrets
import time

STATUSES = ("open", "doing", "done", "blocked")
MAX_TEXT = 200    # ponytail: 一行说清；长论述该进产物，不是清单
MAX_NOTE = 300
MAX_ITEMS = 200   # ponytail: 每卡上限，挡跑飞的拆解循环；真有超大卡再提 CFG


def _flat(s, cap):
    return " ".join(str(s or "").split())[:cap]


def add(con, task_id, text, parent_id=None, owner=None):
    """加一条。返回 (id, None) 或 (None, 错误)。卡必须存在；父条必须同卡。"""
    task = con.execute("SELECT generation FROM tasks WHERE id=?", (task_id,)).fetchone()
    if task is None:
        return None, f"没有这张卡：{task_id}"
    text = _flat(text, MAX_TEXT)
    if not text:
        return None, "todo 文本不能为空"
    n = con.execute("SELECT COUNT(*) AS n FROM todos WHERE task_id=?",
                    (task_id,)).fetchone()["n"]
    if n >= MAX_ITEMS:
        return None, f"该卡 todo 已达上限 {MAX_ITEMS} 条——清单不是流水账，合并同类项"
    if parent_id is not None:
        parent = con.execute("SELECT task_id FROM todos WHERE id=?",
                             (parent_id,)).fetchone()
        if parent is None or parent["task_id"] != task_id:
            return None, f"父条 {parent_id} 不存在或不属于卡 {task_id}"
    tid = "td_" + secrets.token_hex(3)
    now = int(time.time())
    con.execute(
        "INSERT INTO todos (id, task_id, parent_id, text, status, owner, generation,"
        " created_at, updated_at) VALUES (?,?,?,?,'open',?,?,?,?)",
        (tid, task_id, parent_id, text, _flat(owner, 60) or None,
         int(task["generation"]), now, now))
    return tid, None


def mark(con, task_id, todo_id, status, note=None, owner=None):
    """改状态/备注/认领。返回 (成功?, 错误)。条目必须属于该卡（限权）。"""
    if status not in STATUSES:
        return False, f"状态须是 {'/'.join(STATUSES)} 之一"
    if status == "blocked" and not _flat(note, MAX_NOTE):
        return False, "blocked 必须带 note——卡在哪，一句话（这就是相谈）"
    sets, args = ["status=?", "updated_at=?"], [status, int(time.time())]
    if note is not None:
        sets.append("note=?")
        args.append(_flat(note, MAX_NOTE) or None)
    if owner is not None:
        sets.append("owner=?")
        args.append(_flat(owner, 60) or None)
    cur = con.execute(f"UPDATE todos SET {', '.join(sets)} WHERE id=? AND task_id=?",
                      [*args, todo_id, task_id])
    if cur.rowcount == 0:
        return False, f"条目 {todo_id} 不存在或不属于卡 {task_id}"
    return True, None


def items(con, task_id):
    """该卡全部条目（真实插入序＝rowid；created_at 秒级会撞车，靠不住）。
    父必先于子插入（add 校验父在先），所以按此序一趟就能拼树。"""
    return con.execute("SELECT * FROM todos WHERE task_id=? ORDER BY rowid",
                       (task_id,)).fetchall()


def tree(con, task_id):
    """嵌套树 [{...行字段, children: [...]}]。父先于子（无重挂），一趟拼装。"""
    nodes, roots = {}, []
    for r in items(con, task_id):
        node = {**{k: r[k] for k in r.keys()}, "children": []}
        nodes[r["id"]] = node
        parent = nodes.get(r["parent_id"])
        (parent["children"] if parent else roots).append(node)
    return roots


def stats(con, task_id):
    """仪表：{total, done, doing: [文本], blocked: [(文本, note)]}。
    树行徽标（3 期）与交卷 doing 清零闸（2 期）共用一个读数。"""
    rows = items(con, task_id)
    return {
        "total": len(rows),
        "done": sum(1 for r in rows if r["status"] == "done"),
        "doing": [r["text"] for r in rows if r["status"] == "doing"],
        "blocked": [(r["text"], r["note"] or "") for r in rows
                    if r["status"] == "blocked"],
    }


GLYPH = {"open": "[ ]", "doing": "[~]", "done": "[x]", "blocked": "[!]"}


def render(con, task_id):
    """清单树的文本形（sis 工具回显与 3 期树视图共用）。"""
    lines = []

    def walk(nodes, indent):
        for n in nodes:
            owner = f"（{n['owner']}）" if n["owner"] else ""
            note = f" ⚠{n['note']}" if n["status"] == "blocked" and n["note"] else ""
            lines.append(f"{indent}{GLYPH[n['status']]} {n['id']} {n['text']}{owner}{note}")
            walk(n["children"], indent + "  ")

    walk(tree(con, task_id), "")
    return "\n".join(lines) or "（清单还空着）"


NAG_EMPTY_AFTER = 10   # 跑了这么多工具回合还没拆清单 → 提醒一次（琐碎卡可无视）
NAG_STALE_AFTER = 25   # 清单这么久没动而且还挂着 doing → 提醒，至多两次


def tools_for(task_id):
    """sis 会话的微观代办工具工厂：task_id 烧死在闭包里，参数面上没有卡号——
    她物理上只能写自己这张卡（板的状态机照旧只归 LO）。
    附提醒钩子：光干活不记账时塞一句系统提醒——唠叨不是闸；硬闸在交卷
    check_report（doing 清零）。末尾补账防不住，防它的是这份全程可见性。"""

    def register(harn):
        from typing import Literal, Optional

        from pydantic import BaseModel, Field

        from misaka.config import CFG
        from misaka.core.extensions.types import ToolDefinition
        from misaka.extensions.board import db as bdb

        state = {"con": None, "results": 0, "since_write": 0,
                 "empty_nagged": False, "stale_nags": 0}

        def con():
            if state["con"] is None:
                state["con"] = bdb.connect(CFG["db"])
            return state["con"]

        def _text(s):
            return {"content": [{"type": "text", "text": s}], "details": {}}

        class AddOp(BaseModel):
            text: str = Field(description="子任务，一句话")
            parent_id: Optional[str] = Field(None, description="挂在哪条下（td_ 号）；不填＝顶层")
            owner: Optional[str] = Field(None, description="谁来干；自己干留空，派分身写分身任务名")

        class MarkOp(BaseModel):
            id: str = Field(description="条目 td_ 号")
            status: Literal["open", "doing", "done", "blocked"] = Field(description="新状态")
            note: Optional[str] = Field(None, description="blocked 必填：卡在哪，一句话")
            owner: Optional[str] = Field(None, description="改认领（可选）")

        class TodoParams(BaseModel):
            add: list[AddOp] = Field(default_factory=list, description="要新增的条目")
            mark: list[MarkOp] = Field(default_factory=list, description="要改状态的条目")

        async def todo_exec(tool_call_id, raw, signal, on_update, ctx):
            p = raw if isinstance(raw, TodoParams) else TodoParams(**(raw or {}))
            out = []
            for op in p.add:
                tid, err = add(con(), task_id, op.text,
                               parent_id=op.parent_id, owner=op.owner)
                out.append(f"＋ {tid} {op.text}" if tid else f"✗ 加不上「{op.text[:30]}」：{err}")
            for op in p.mark:
                ok, err = mark(con(), task_id, op.id, op.status,
                               note=op.note, owner=op.owner)
                out.append(f"✓ {op.id} → {op.status}" if ok else f"✗ {op.id}：{err}")
            state["since_write"] = 0
            return _text(("\n".join(out) + "\n\n" if out else "")
                         + "当前清单：\n" + render(con(), task_id))

        harn.registerTool(ToolDefinition(
            name="misaka_todo", label="微观代办",
            description="维护你这张卡的子任务清单树（增条目/标进度）。清单实时显示在全局树上，"
                        "LO 和人看它了解你的进度——这是你的報告与連絡。",
            parameters=TodoParams.model_json_schema(), execute=todo_exec,
            promptSnippet="拆解/推进这张卡的子任务清单",
            promptGuidelines=[
                "开工先用 misaka_todo 把卡拆成子任务树再动手；一两步就完的琐碎卡可以不拆。",
                "推进随手标：动手某条先标 doing，做完立刻标 done——不要收工前一把补账。",
                "卡住就标 blocked＋note 写清卡在哪，然后继续别的条目；note 会浮到树上给人看（相谈）。",
                "派分身干某条时：该条 owner 写分身任务名，分身任务描述开头引用该条原文，并把该条标 doing。",
                "交卷前 doing 必须清零：做完的标 done，没做完的退 open 或标 blocked——留着 doing 交卷会被打回。",
            ]))

        class ListParams(BaseModel):
            pass

        async def list_exec(tool_call_id, raw, signal, on_update, ctx):
            return _text("当前清单：\n" + render(con(), task_id))

        harn.registerTool(ToolDefinition(
            name="misaka_todo_list", label="看清单",
            description="看你这张卡的子任务清单树（含状态与卡壳备注）。",
            parameters=ListParams.model_json_schema(), execute=list_exec,
            promptSnippet="查看本卡子任务清单",
            promptGuidelines=["上下文被压缩后先 misaka_todo_list 找回现场，再继续干活。"]))

        def _field(event, key, default=None):
            try:
                return event.get(key, default)
            except AttributeError:
                return getattr(event, key, default)

        def _nag(text):
            try:
                harn.sendMessage(
                    {"customType": "todo-reminder", "display": True,
                     "content": "〔微观代办提醒〕" + text, "details": {}},
                    {"deliverAs": "followUp", "triggerTurn": False})
            except Exception:   # noqa: BLE001 - 唠叨不是闸，发不出去不许影响干活
                pass

        async def on_result(event, _ctx=None):
            name = str(_field(event, "toolName", "") or "")
            if name in ("misaka_todo", "misaka_todo_list"):
                state["since_write"] = 0   # 写口 exec 里也清；这里兜看清单的场
                return
            state["results"] += 1
            state["since_write"] += 1
            s = stats(con(), task_id)
            if (not state["empty_nagged"] and s["total"] == 0
                    and state["results"] >= NAG_EMPTY_AFTER):
                state["empty_nagged"] = True
                _nag(f"干了 {state['results']} 个回合还没有清单——先用 misaka_todo 把这张卡"
                     "拆成子任务树再继续（琐碎卡可无视本提醒）。")
            elif (s["doing"] and state["since_write"] >= NAG_STALE_AFTER
                    and state["stale_nags"] < 2):
                state["stale_nags"] += 1
                state["since_write"] = 0
                _nag("清单很久没动了，还挂着 doing：" + "；".join(s["doing"][:3])
                     + "——推进了就标 done，卡住就标 blocked＋note。")

        async def on_shutdown(_event=None, _ctx=None):
            if state["con"] is not None:
                state["con"].close()

        harn.on("tool_result", on_result)
        harn.on("session_shutdown", on_shutdown)

    return register


if __name__ == "__main__":
    import os
    import tempfile

    from misaka.extensions.board import db

    con = db.connect(os.path.join(tempfile.mkdtemp(), "board.db"))
    card = db.create_task(con, "查入藏簿", assignee="10032")
    other = db.create_task(con, "别人的卡", assignee="10033")

    # 增：正常/空文本/幽灵卡/压平/父子
    root_id, err = add(con, card, "  查  入藏簿\n原件  ")
    assert root_id and err is None, err
    assert items(con, card)[0]["text"] == "查 入藏簿 原件", "多行/连空格必须压平"
    assert add(con, card, "   ")[1] == "todo 文本不能为空"
    assert "没有这张卡" in add(con, "t_没有", "x")[1]
    child_id, err = add(con, card, "联系档案馆", parent_id=root_id, owner="分身·联系馆方")
    assert child_id and err is None, err

    # 限权：跨卡认父、越卡改条目，一律拒
    assert "不属于" in add(con, other, "蹭树", parent_id=root_id)[1]
    assert "不属于" in mark(con, other, child_id, "done")[1]

    # 标：坏状态/裸 blocked 拒；blocked 带 note、done 放行
    assert "状态须是" in mark(con, card, child_id, "finished")[1]
    assert "必须带 note" in mark(con, card, child_id, "blocked")[1]
    ok, err = mark(con, card, child_id, "blocked", note="1954 卷宗申请被拒")
    assert ok, err
    assert mark(con, card, root_id, "doing")[0]
    assert mark(con, card, "td_幽灵", "done")[1].startswith("条目")

    # 树与仪表
    t = tree(con, card)
    assert len(t) == 1 and t[0]["children"][0]["id"] == child_id, "子条要嵌在父条下"
    s = stats(con, card)
    assert s["total"] == 2 and s["done"] == 0 and s["doing"] == ["查 入藏簿 原件"]
    assert s["blocked"] == [("联系档案馆", "1954 卷宗申请被拒")], s["blocked"]
    assert mark(con, card, child_id, "done", note="换邮箱联系成功")[0]
    assert stats(con, card)["done"] == 1 and not stats(con, card)["blocked"]

    # 上限：第 MAX_ITEMS+1 条拒收（不静默）
    for i in range(MAX_ITEMS - 2):
        assert add(con, card, f"条{i}")[0]
    assert "上限" in add(con, card, "溢出")[1]

    # 级联：删卡连微观代办一起抹；别人的卡不受牵连
    assert db.delete_task(con, card)[0]
    assert not items(con, card), "删卡必须级联删 todo"
    assert add(con, other, "别人的还能写")[0]
    print("todo selfcheck ok — 增/标/树/仪表/限权/上限/级联 全对")

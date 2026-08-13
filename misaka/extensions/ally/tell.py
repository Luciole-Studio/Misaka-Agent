"""`misaka tell`：协力者的 SendMessage——它跑一句命令就能给编排官/御坂送信。

为什么不是插件/MCP：协力者跑在 shell 里，而 misaka 本身就是命令行。它执行
`misaka tell "卡住了"` 即可，**它那边零安装、零配置**（合同里告诉它这句话就行）。

对号到谁在说话（三重定位，不把宝押一处——真实 agent 可能有沙箱清环境变量）：
① 环境变量 MISAKA_ALLY / MISAKA_USAGE_TASK_ID（守护进程起格子时已塞好）
② 工作目录反查：cwd 落在 workspaces/<卡号>/ 里 → 查板拿 assignee
③ 都认不出就报错——**宁可拒发，也不冒名顶替**（宪法②：消息身份不能糊）
"""
import os

from misaka.config import CFG


def _task_from_cwd(cwd=None):
    """从工作目录反查卡号：卡的工作区就是 workspaces/<task_id>/。"""
    root = os.path.realpath(os.path.expanduser(CFG["workspaces_root"]))
    here = os.path.realpath(cwd or os.getcwd())
    while len(here) > 1:
        parent, name = os.path.split(here)
        if os.path.realpath(parent) == root and name.startswith("t_"):
            return name
        if parent == here:
            break
        here = parent
    return None


def whoami(cwd=None):
    """我是谁、在跑哪张卡。返回 (发信人, 卡号) —— 认不出发信人则 (None, 卡号|None)。"""
    sender = os.environ.get("MISAKA_ALLY") or None
    task_id = os.environ.get("MISAKA_USAGE_TASK_ID") or _task_from_cwd(cwd)
    if not sender and task_id:          # 环境变量被沙箱清了：按卡号查板
        try:
            from misaka.extensions.board import db
            con = db.connect(os.path.expanduser(CFG["db"]))
            row = db.get(con, task_id)
            con.close()
            if row:
                sender = row["assignee"]
        except Exception:  # noqa: BLE001 - 查不到就认不出，下面会拒发
            pass
    return sender, task_id


def tell(body, *, to_addr="last-order", summary=None, cwd=None):
    """送信。返回 (成功?, 消息)。认不出自己是谁就拒发——不冒名顶替。"""
    if not (body or "").strip():
        return False, "消息不能为空"
    sender, task_id = whoami(cwd)
    if not sender:
        return False, ("认不出你是哪个协力者：环境变量 MISAKA_ALLY 没了，"
                       "工作目录也不在任何卡的工作区里。请在卡的工作区内运行。")
    from misaka.config import sisters
    from misaka.extensions import messages
    con = messages.connect()
    try:
        known = {"last-order"} | set(sisters())    # 与 messages.register 同一份名册
        if to_addr not in known:
            return False, f"没有这个收件人：{to_addr}。在册地址：{', '.join(sorted(known))}"
        messages.send(con, to_addr, body.strip(),
                      summary=summary or f"协力者 {sender} 来信",
                      sender=sender, task_id=task_id)
    finally:
        con.close()
    return True, f"已送给 {to_addr}（发信人 {sender}" + (f"，卡 {task_id}）" if task_id else "）")


if __name__ == "__main__":
    import tempfile
    ws = tempfile.mkdtemp()
    CFG["workspaces_root"] = ws
    os.makedirs(os.path.join(ws, "t_abc123", "sub"), exist_ok=True)
    assert _task_from_cwd(os.path.join(ws, "t_abc123")) == "t_abc123"
    assert _task_from_cwd(os.path.join(ws, "t_abc123", "sub")) == "t_abc123", "子目录也能反查"
    assert _task_from_cwd("/tmp") is None, "工作区外认不出"

    os.environ["MISAKA_ALLY"] = "codex"
    os.environ.pop("MISAKA_USAGE_TASK_ID", None)
    sender, task = whoami(os.path.join(ws, "t_abc123"))
    assert sender == "codex" and task == "t_abc123", (sender, task)
    os.environ.pop("MISAKA_ALLY")
    assert whoami("/tmp") == (None, None), "认不出就是认不出，不瞎猜"

    CFG["messages_db"] = os.path.join(ws, "m.db")
    CFG["db"] = os.path.join(ws, "b.db")
    from misaka.extensions.board import db as bdb
    con = bdb.connect(CFG["db"])
    tid = bdb.create_task(con, "协力者的卡", assignee="codex", executor=["codex", "exec"])
    os.makedirs(os.path.join(ws, tid), exist_ok=True)
    ok, msg = tell("我卡在依赖冲突了", cwd=os.path.join(ws, tid))   # 环境变量已删，靠 cwd
    assert ok, msg
    assert "codex" in msg and tid in msg, msg
    from misaka.extensions import messages
    mcon = messages.connect()
    row = mcon.execute("SELECT sender,to_addr,task_id,body FROM messages").fetchone()
    assert row["sender"] == "codex" and row["to_addr"] == "last-order"
    assert row["task_id"] == tid and "依赖冲突" in row["body"]
    assert not tell("x", cwd="/tmp")[0], "认不出身份要拒发（不冒名顶替）"
    assert not tell("", cwd=os.path.join(ws, tid))[0], "空消息拒发"
    assert not tell("x", to_addr="查无此人", cwd=os.path.join(ws, tid))[0], "收件人不在册要拒"
    print("ally.tell selfcheck ok — 三重定位（环境变量/工作目录反查/拒发）均正确")

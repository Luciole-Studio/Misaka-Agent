"""卡片格子里的交互会话：合同当开场白，工作区当现场。

由网络守护进程在格子（伪终端）里拉起——认领、超时、交卷转态都归守护进程盯，
这里只负责把跑卡装配（worker.card_session_setup 同款）接到交互引擎上。
上一轮验收反馈照 dispatch 口径并入合同。接续现场：卡上记的精确路径优先
（LO 后台跑的也接得上），退回目录里最近的（打回重做沿用原对话，不冒充新 Sister）。

# ponytail: 预约制预算在格子路径未接（v1 已知缺口）——靠 MISAKA_USAGE_* 的
# 事后记账与 token_cap 兜底；要预约再把 _reserve_usage 接进来。
"""
import asyncio
import json
import os
import sys

from misaka.config import CFG
from misaka.extensions.board import db, worker

ACTIVE_STATUSES = ("running", "verifying", "finalizing")


def continue_flags(session_file, session_dir):
    """现场定位：卡上记的精确路径优先（跨落位有效），退回目录里最近的（-c）。
    返回要追加的引擎旗标；None＝没有现场。"""
    if session_file and os.path.isfile(session_file):
        return ["--session", session_file]
    try:
        names = os.listdir(session_dir)
    except OSError:
        return None
    return ["-c"] if any(n.endswith(".jsonl") for n in names) else None


def launch(task_id, resume_only=False):
    con = db.connect(os.path.expanduser(CFG["db"]))
    row = db.get(con, task_id)
    if row is None:
        sys.exit(f"没有这张卡：{task_id}")
    task = dict(row)
    if resume_only and task["status"] in ACTIVE_STATUSES:
        # 只看不动看板；但在跑/验收中的卡现场有人在写——两个进程同写一份会写花
        sys.exit(f"卡 {task_id} 正在「{task['status']}」——现场有人在写，等她跑完再展开")
    feedback = db.latest_payload(con, task_id, "verify_fail", generation=task["generation"])
    if feedback:
        fixes = json.loads(feedback).get("must_fix", [])
        if fixes:
            task["feedback"] = ("⚠️ 上一轮验收未过，必须先修复以下问题（产物按最新要求重写）：\n"
                                + "\n".join(f"- {x}" for x in fixes))
    workspace = task["workspace"] or os.path.join(CFG["workspaces_root"], task_id)
    profile_dir = os.path.join(os.path.expanduser(CFG["profiles_root"]), task["assignee"])
    if not os.path.isdir(profile_dir):
        sys.exit(f"Sister {task['assignee']} 不在可启动名册")

    flags, factories, prompt, _ro_root, role = worker.card_session_setup(
        task, workspace, profile_dir, CFG["provider"], CFG["default_model"]
    )
    session_dir = os.path.join(workspace, "session")
    cont = continue_flags(task["session_file"], session_dir)
    if resume_only:
        # 只展开现场看/手聊，**不重发合同**——否则点开即重跑＝暗中花钱（宪法③）
        if not cont:
            sys.exit(f"卡 {task_id} 还没有会话现场，没法展开")
        flags += cont
    else:
        if cont:
            flags += cont             # 打回重做/复跑：接续原现场（LO 后台跑的也接得上）
        flags.append(prompt)          # 位置参数＝开场消息（引擎原生机制）

    os.environ.update({
        "MISAKA_PROFILE_DIR": profile_dir,
        "MISAKA_WHO": role,
        "MISAKA_MCP_ROLE": role,
        "MISAKA_WORKSPACE": workspace,
        "MISAKA_APP_TITLE": f"MISAKA · {task['assignee']} · {task_id}",
        "MISAKA_TAGLINE": f"卡 {task_id}：{task['title']}",
    })
    os.chdir(workspace)

    from misaka.cli.engine import main as engine_main
    sys.exit(asyncio.run(engine_main(flags, {"extensionFactories": factories or []})))

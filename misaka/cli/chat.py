"""前台会话装配：misaka chat ——和 Last Order（或某位 Sister）对话。

进程内起 engine 的交互模式；工具以注册函数直接进工具表，无 -e 文件挂载。
"""
import asyncio
import os
import sys

from misaka.config import profiles
from misaka.config import CFG, sisters


def _extension_factories(profile_dir, profile_role, workspace, session_role, *, sister):
    """Assemble the named bundled extensions for one foreground role."""

    from functools import partial

    from misaka.extensions import docs, inline, mcp, messages, roster, switch
    from misaka.extensions.board import extension as board
    from misaka.extensions.ally import extension as ally
    from misaka.extensions.subagent import extension as subagent

    role_extensions = (
        [
            inline("doc-tools", docs.register),
            inline(
                "subagent",
                subagent.bind(
                    profile_dir,
                    profile_role,
                    workspace,
                    mcp_role=session_role,
                ),
            ),
            # 统一消息层：妹妹发信＋收信；自己生的分身由 route 短路唤醒
            inline("messages", partial(
                messages.register,
                sender=session_role,
                route=subagent.route_to_children,
                receive=True,
            )),
        ]
        if sister
        else [
            inline("board-tools", board.register),
            # 协力者：跑在格子里的第三方 agent（codex/claude/…）——只给 LO
            inline("ally-tools", ally.register),
            # LO 解禁的唯一子代理类工具：SendMessage（派活仍只能建卡＋验收）
            inline("messages", partial(
                messages.register, sender="last-order", receive=True,
            )),
        ]
    )
    from misaka.extensions import lcm, moa, skill_invoke
    return [
        *role_extensions,
        inline("switch", switch.register),
        inline("roster", roster.register),
        inline("moa", moa.commands_for(profile_dir)),   # /moa：LO 与 sis 各用各的配置
        inline("lcm", lcm.register),                    # 无损压缩接管（fail-open 回原生）
        inline("skill-invoke", skill_invoke.commands_for(profile_dir)),   # /skill 显式调用
        inline("mcp", mcp.bind(profile_dir, session_role)),
    ]


def _migrate_sessions(new, old):
    """一次性搬家（自由聊目录合并为 ~/.misaka/sessions/<角色>/）：
    旧目录在、新目录不在就整体改名过去；搬不动静默沿用旧目录。返回实际用的目录。"""
    new, old = os.path.expanduser(new), os.path.expanduser(old)
    if os.path.isdir(new) or not os.path.isdir(old):
        return new
    try:
        os.makedirs(os.path.dirname(new), exist_ok=True)
        os.rename(old, new)
        return new
    except OSError:
        return old   # ponytail: 跨盘/权限等罕见失败不硬迁，旧位置照用不断档


def _migrate_lo_soul(prof):
    """一次性归一（2026-08-20）：SOUL-chat.md → SOUL.md＝LO 唯一人格档。
    旧 SOUL.md（拆卡合同）已逐字迁入 board/plan.py 的 PLAN_CONTRACT，
    改名留档不删（用户手笔可能在里面）。幂等：SOUL-chat.md 不在＝已迁移。"""
    chat_soul = os.path.join(prof, "SOUL-chat.md")
    if not os.path.isfile(chat_soul):
        return
    try:
        old = os.path.join(prof, "SOUL.md")
        if os.path.isfile(old):
            os.rename(old, os.path.join(prof, "SOUL-plan-retired.md"))
        os.rename(chat_soul, old)
    except OSError:
        pass   # 迁不动不拦启动；assembly 有旧文件名兜底


def assembly(who, *, cwd=None):
    """角色装配的公共部分（前台 chat 与 DM 无头轮共用）。
    返回 (prof, soul, model_default, skill_flags)。who 不在册直接 sys.exit。"""
    if who:  # Sister：人格+技能三层栈
        prof = os.path.join(CFG["profiles_root"], who)
        if not os.path.isdir(prof):
            sys.exit(f"没有这位 Sister：{who}（名册：{', '.join(sorted(sisters()))}）")
        soul = os.path.join(prof, "SOUL.md")
        extra = []
        from misaka.orchestration import skill_layers
        for sk in skill_layers.skills_stack(prof, cwd=cwd or os.getcwd()):
            extra += ["--skill", sk]      # 三层栈：项目（信任＋扫描）→ 角色 → 共享
        return prof, soul, CFG["default_model"], extra   # 尊重 MISAKA_MODEL（与跑卡路径同轨）
    prof = os.path.join(CFG["roles_root"], "last_order")
    _migrate_lo_soul(prof)
    soul = os.path.join(prof, "SOUL.md")
    if not os.path.isfile(soul):   # 迁移失败的兜底：沿用旧对话档名
        legacy = os.path.join(prof, "SOUL-chat.md")
        soul = legacy if os.path.isfile(legacy) else soul
    return prof, soul, "claude-opus-5", []


def launch(who, model=None, cont=False, pick=False, session=None):
    """装配并进入交互模式（阻塞到会话结束）。who=None 表示 Last Order。"""
    prof, soul, model_default, extra = assembly(who)
    if who:  # 找某位 Sister：带她的人格+技能+文献工具，有内置工具（她是干活的）
        title = f"MISAKA · {who}"
        sess = _migrate_sessions(f"~/.misaka/sessions/{who}",
                                 f"~/.misaka/sister-sessions/{who}")
        from misaka.orchestration import skill_layers
        hint = skill_layers.get_untrusted_project_skills_root(cwd=os.getcwd())
        if hint:
            print(f"（本仓有 {hint[1]} 个项目技能未加载——信任它：misaka skills trust）")
    else:  # 找 Last Order：pi 的内置工具照给，只是不给子代理——Sisters 就是她的子代理
        # （2026-08-07 用户勘误：此前 -nbt 把内置工具一并没收，是把"不给子代理"过度执行；
        #  不给子代理靠 _extension_factories 不注册 subagent 扩展，与内置工具无关。）
        title = "MISAKA · Last Order"
        sess = _migrate_sessions("~/.misaka/sessions/last-order",
                                 "~/.misaka/last-order-sessions")
    flags = ["--provider", CFG["provider"], "--model", model or model_default,
             "--append-system-prompt", profiles.shared_soul(),   # 共同魂在前
             "--append-system-prompt", soul,                     # 角色个性在后
             "--session-dir", os.path.expanduser(sess)] + extra
    if session:
        flags += ["--session", session]   # 切入指定会话（引擎支持路径或部分 UUID）
    elif pick:
        flags.append("-r")        # 挑一个历史会话恢复
    elif cont:
        flags.append("-c")        # 显式要求才接续；默认开新会话（对齐 claude 的默认）

    profile_role = profiles.role_of(prof)
    session_role = who or "last-order"
    workspace = os.getcwd()
    tagline = ("御坂网络编排官 Last Order 待命。她会追问、下注、拆卡；你点头后后台调用 Sisters，交卷验收后自动回报。（/sisters 名册 · /sister 10032 直切）"
               if not who else
               f"御坂{who} 在线。可以直接让她读文献、查资料、干活；她的子代理运行情况会实时显示。（/sisters 名册 · /sister 10032 直切）")
    os.environ.update({
        "MISAKA_APP_TITLE": title, "MISAKA_TAGLINE": tagline,
        "MISAKA_WHO": session_role,
        "MISAKA_MCP_ROLE": session_role,
        # MCP 按角色找数据目录 ~/.misaka/profiles/<角色>/config.yaml
        "MISAKA_PROFILE_DIR": prof,
        "MISAKA_WORKSPACE": workspace,
        "MISAKA_INPUT_HISTORY": os.path.expanduser(f"~/.misaka/input-history/{who or 'last-order'}.json"),
        "MISAKA_CODING_AGENT": "true"})

    factories = _extension_factories(
        prof,
        profile_role,
        workspace,
        session_role,
        sister=bool(who),
    )

    from misaka.cli.engine import main as engine_main
    sys.exit(asyncio.run(engine_main(flags, {"extensionFactories": factories})))

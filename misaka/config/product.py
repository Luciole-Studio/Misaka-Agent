"""MISAKA 研究系统配置（产品侧）。

# ponytail: 常量即配置，要配置文件时再加；MISAKA_* 环境变量可覆盖
"""
import os
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[2])

CFG = {
    "db": os.environ.get("MISAKA_DB", "~/.misaka/board.db"),
    "messages_db": os.environ.get("MISAKA_MESSAGES", "~/.misaka/messages.db"),
    # 协力者手敲识别名单：唯一真源是这个文件（首次运行落盘种子；无环境变量旋钮，用户裁定）
    "allies": "~/.misaka/allies.json",
    "net_sock": os.environ.get("MISAKA_NET_SOCK", "~/.misaka/net.sock"),
    "net_snapshot": os.environ.get("MISAKA_NET_SNAPSHOT", "~/.misaka/net.json"),
    "provider": os.environ.get("MISAKA_PROVIDER", "sub2api-claude"),
    "default_model": os.environ.get("MISAKA_MODEL", "claude-sonnet-5"),
    "workspaces_root": os.path.expanduser(os.environ.get("MISAKA_WS", "~/Documents/Misaka/workspaces")),
    # 课题目录：每个课题一个子目录（含 PROJECT.md＋原始材料），目录名＝卡的 project 字段
    "projects_root": os.path.expanduser(os.environ.get("MISAKA_PROJECTS", "~/Documents/Misaka/projects")),
    # 照 pi：人格是用户态数据，与技能/MCP 同居 ~/.misaka/profiles/<角色>/，源码仓不放人格
    "profiles_root": os.path.expanduser("~/.misaka/profiles/sisters"),
    "roles_root": os.path.expanduser("~/.misaka/profiles"),
    "judge_timeout": int(os.environ.get("MISAKA_JUDGE_TIMEOUT", "600")),
    "hooks_dir": os.path.join(REPO, "hooks"),
    "token_cap": int(os.environ.get("MISAKA_TOKEN_CAP", "0")),
    # 上下文引擎：lcm＝无损压缩（原文全落 lcm.db，可回收）；native＝引擎原生一次性摘要。
    # LCM 内部任何失败自动回落 native（fail-open），此开关是显式逃生舱。
    "context_engine": os.environ.get("MISAKA_CONTEXT_ENGINE", "lcm"),
    "lcm_db": os.environ.get("MISAKA_LCM_DB", "~/.misaka/lcm.db"),
}


def sisters():
    """在册 Sister 名单（~/.misaka/profiles/sisters/ 的子目录名）。"""
    root = CFG["profiles_root"]
    if not os.path.isdir(root):
        return set()
    return {d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))}

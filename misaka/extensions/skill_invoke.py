"""`/skill` 显式调用（hermes agent/skill_commands.py 严格移植，MIT）。

上游机制逐段对应：三层栈扫描→frontmatter 名→slug 命令表（去重 first-wins、
非法字符清洗）；`_build_skill_message` 展开＝activation 脚手架（marker 原文
保英文——机器锚点）＋预处理后的正文＋[Skill directory]＋支撑文件清单
（references/templates/scripts/assets）＋用户附带指令；sendUserMessage 注入。
省略并记账：per-skill 自动斜杠命令（引擎命令冲突语义未勘，/skill 主命令全覆盖）、
缓存稳定前缀注册与 bundle（不在三件套）。
"""
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_SKILL_INVALID_CHARS = re.compile(r"[^a-z0-9-]")
_SKILL_MULTI_HYPHEN = re.compile(r"-{2,}")
# 上游 marker 原文（字节级保留——剥壳/识别的机器锚点）
_SKILL_INVOCATION_PREFIX = "[IMPORTANT: The user has invoked the "
_SINGLE_SKILL_MARKER = "The full skill content is loaded below.]"
_SINGLE_SKILL_INSTRUCTION = (
    "The user has provided the following instruction alongside the skill invocation: "
)
_SUPPORT_SUBDIRS = ("references", "templates", "scripts", "assets")


def _slug(name):
    slug = name.lower().replace(" ", "-").replace("_", "-")
    slug = _SKILL_INVALID_CHARS.sub("", slug)
    return _SKILL_MULTI_HYPHEN.sub("-", slug).strip("-")


def scan_skill_commands(profile_dir, cwd=None):
    """三层栈 → {slug: {name, description, skill_md_path, skill_dir}}。
    去重 first-wins（栈序即优先级：project > 角色 > 共享，上游同语义）。"""
    from misaka.orchestration.skill_layers import skills_stack
    from misaka.utils.frontmatter import parse_frontmatter

    commands = {}
    for skill_dir in skills_stack(profile_dir, cwd=cwd):
        skill_md = Path(skill_dir) / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            content = skill_md.read_text(encoding="utf-8")
            parsed = parse_frontmatter(content)
            frontmatter = parsed.frontmatter or {}
            body = parsed.body or ""
        except Exception:  # noqa: BLE001 - 单个坏技能不毁扫描（上游同款）
            logger.warning("技能解析失败，跳过：%s", skill_md, exc_info=True)
            continue
        name = str(frontmatter.get("name") or Path(skill_dir).name)
        slug = _slug(name)
        if not slug or slug in commands:
            continue
        description = str(frontmatter.get("description") or "")
        if not description:
            for line in body.strip().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    description = line[:80]
                    break
        commands[slug] = {"name": name,
                          "description": description or f"调用 {name} 技能",
                          "skill_md_path": str(skill_md),
                          "skill_dir": str(skill_dir)}
    return commands


def build_skill_message(info, *, user_instruction="", session_id=None):
    """上游 _build_skill_message 的忠实移植（activation 脚手架＋正文＋目录＋支撑清单）。"""
    from misaka.orchestration.skill_preprocessing import preprocess_skill_content
    from misaka.utils.frontmatter import parse_frontmatter

    skill_dir = Path(info["skill_dir"])
    raw = Path(info["skill_md_path"]).read_text(encoding="utf-8")
    body = (parse_frontmatter(raw).body or "").strip()
    content = preprocess_skill_content(body, skill_dir, session_id=session_id)

    activation_note = (f'{_SKILL_INVOCATION_PREFIX}"{info["name"]}" skill. '
                       f"{_SINGLE_SKILL_MARKER}")
    parts = [activation_note, "", content.strip()]

    parts.append("")
    parts.append(f"[Skill directory: {skill_dir}]")
    parts.append(
        "Resolve any relative paths in this skill (e.g. `scripts/foo.js`, "
        "`templates/config.yaml`) against that directory, then run them "
        "with the terminal tool using the absolute path.")

    supporting = []
    for subdir in _SUPPORT_SUBDIRS:
        subdir_path = skill_dir / subdir
        if subdir_path.exists():
            for f in sorted(subdir_path.rglob("*")):
                if f.is_file() and not f.is_symlink():
                    supporting.append(str(f.relative_to(skill_dir)))
    if supporting:
        parts.append("")
        parts.append("[This skill has supporting files:]")
        for sf in supporting:
            parts.append(f"- {sf}  ->  {skill_dir / sf}")

    if user_instruction:
        parts.append("")
        parts.append(f"{_SINGLE_SKILL_INSTRUCTION}{user_instruction}")
    return "\n".join(parts)


def commands_for(profile_dir):
    """`/skill [名] [指令]` 命令工厂（角色目录烧进闭包）。"""

    def register(harn):
        async def skill_cmd(args, ctx):
            raw = (args or "").strip()
            cwd = os.getcwd()
            commands = scan_skill_commands(profile_dir, cwd=cwd)
            if not raw:
                if not commands:
                    ctx.ui.notify("没有可用技能（三层栈全空）", "info")
                    return
                lines = [f"/skill {slug} — {info['description']}"
                         for slug, info in sorted(commands.items())]
                ctx.ui.notify("可用技能：\n" + "\n".join(lines), "info")
                return
            name, _, instruction = raw.partition(" ")
            info = commands.get(_slug(name))
            if info is None:
                ctx.ui.notify(f"没有技能「{name}」。可用：{'、'.join(sorted(commands))}",
                              "error")
                return
            try:
                sid = str(ctx.sessionManager.getSessionId())
            except Exception:  # noqa: BLE001 - 无会话号不拦调用
                sid = None
            message = build_skill_message(info, user_instruction=instruction.strip(),
                                          session_id=sid)
            await ctx.sendUserMessage(message)

        harn.registerCommand("skill", {
            "handler": skill_cmd,
            "description": "调用技能：/skill 列清单；/skill <名> [附带指令] 展开进本轮"})

    return register

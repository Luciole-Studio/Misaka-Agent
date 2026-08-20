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

# 路径校验住在 orchestration（skill_manage 也用它）——这里转出口
from misaka.orchestration.skill_manage import lookup_path_error

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

    2026-08-20 合并（用户裁定「两套技能系统合并、对齐 hermes」）：加载层复用引擎的
    `core.skills.load_skills_from_dir`，不再自己重扫一遍——于是 `/skill` 与系统提示里的
    `<available_skills>` 索引**看到同一批技能、用同一套规则**（SKILL.md 门、BOM 剥离、
    description 缺失回落 body 首行、name 取 frontmatter）。身份口径统一为 frontmatter
    name（hermes 同款：name 是身份、目录名只是位置）。
    栈序即优先级（project > 角色 > 共享），首见名胜出。
    """
    from misaka.core.skills import load_skills_from_dir
    from misaka.orchestration.skill_layers import skills_stack

    commands = {}
    seen_names = set()
    for skill_dir in skills_stack(profile_dir, cwd=cwd):
        try:
            result = load_skills_from_dir({"dir": str(skill_dir), "source": "misaka"})
        except Exception:  # noqa: BLE001 - 单个坏技能不毁扫描（上游同款）
            logger.warning("技能加载失败，跳过：%s", skill_dir, exc_info=True)
            continue
        for skill in result.skills:
            if skill.name in seen_names:   # 身份＝frontmatter name
                continue
            slug = _slug(skill.name)
            if not slug or slug in commands:
                continue
            seen_names.add(skill.name)
            commands[slug] = {"name": skill.name,
                              "description": skill.description or f"调用 {skill.name} 技能",
                              "skill_md_path": skill.filePath,
                              "skill_dir": skill.baseDir}
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


_SUPPORT_DIRS = ("references", "templates", "assets", "scripts")


def collect_linked_files(skill_dir):
    """支撑文件按类分组（hermes linked_files 同款）。软链不收（misaka 比上游本地
    路径更严——上游只在 plugin 分支做这层校验）。空类不出现。"""
    root = Path(skill_dir)
    out = {}
    for category in _SUPPORT_DIRS:
        sub = root / category
        if not sub.is_dir():
            continue
        files = [str(f.relative_to(root)) for f in sorted(sub.rglob("*"))
                 if f.is_file() and not f.is_symlink()]
        if files:
            out[category] = files
    return out


def read_support_file(skill_dir, file_path):
    """读技能内的支撑文件。返回 (内容, 错误)——路径必须留在技能目录内
    （解析软链后再校验，hermes validate_within_dir 同款）。"""
    err = lookup_path_error(file_path)
    if err:
        return None, err
    root = Path(skill_dir).resolve()
    target = (root / file_path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None, f"文件不在技能目录内：{file_path}"
    if not target.is_file():
        return None, f"技能里没有这个文件：{file_path}"
    try:
        data = target.read_bytes()
    except OSError as e:
        return None, str(e)
    try:
        return data.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        return f"[二进制文件：{target.name}，{len(data)} 字节]", None


def tools_for(profile_dir):
    """`skills_list` / `skill_view` 两件工具（hermes tools/skills_tool.py 移植）。

    此前 misaka 的技能只有两条路径：系统提示里的静态索引（agent 只能看）与
    `/skill` 斜杠命令（只有人能用）——agent 想用技能只能裸 `read` SKILL.md，
    拿到的是**未预处理**的原文（`${MISAKA_SKILL_DIR}` 还是字面量、没有支撑文件清单）。
    hermes 的三层渐进披露（静态索引 → skills_list → skill_view）由这两件补齐；
    与 `/skill` 复用同一份 scan/build，一份实现两个消费者（上游同构：斜杠命令走
    `preprocess=False` 自己渲染，工具走 `preprocess=True`）。
    """
    from pydantic import BaseModel, ConfigDict, Field

    from misaka.core.extensions.types import ToolDefinition

    class ListParams(BaseModel):
        model_config = ConfigDict(extra="forbid")

    class ViewParams(BaseModel):
        model_config = ConfigDict(extra="forbid")
        name: str = Field(description="技能名（用 skills_list 查）")
        file_path: str = Field(
            "", description="可选：技能内的支撑文件相对路径（如 references/规范.md）；"
                            "留空＝取 SKILL.md 正文")

    def register(harn):
        async def list_execute(tool_call_id, raw, signal, on_update, ctx):
            commands = scan_skill_commands(profile_dir, cwd=os.getcwd())
            if not commands:
                return {"content": [{"type": "text", "text": "当前没有可用技能。"}]}
            lines = ["可用技能（要用哪个就 skill_view 取全文）："]
            for info in sorted(commands.values(), key=lambda i: i["name"]):
                lines.append(f"- {info['name']}：{info['description']}")
            return {"content": [{"type": "text", "text": "\n".join(lines)}],
                    "details": {"count": len(commands)}}

        harn.registerTool(ToolDefinition(
            name="skills_list", label="列技能",
            description="列出可用技能（名字＋一句话描述）。要用某个技能时"
                        "用 skill_view 取它的完整内容。",
            parameters=ListParams.model_json_schema(), execute=list_execute,
            promptSnippet="列出可用技能"))

        async def view_execute(tool_call_id, raw, signal, on_update, ctx):
            args = raw if isinstance(raw, ViewParams) else ViewParams(**(raw or {}))
            err = lookup_path_error(args.name)
            if err:
                return {"content": [{"type": "text", "text": err}], "isError": True}
            commands = scan_skill_commands(profile_dir, cwd=os.getcwd())
            info = commands.get(_slug(args.name)) or next(
                (i for i in commands.values() if i["name"] == args.name.strip()), None)
            if info is None:
                names = "、".join(sorted(i["name"] for i in commands.values())) or "（无）"
                return {"content": [{"type": "text",
                                     "text": f"没有技能「{args.name}」。可用：{names}"}],
                        "isError": True}
            if args.file_path:
                content, err = read_support_file(info["skill_dir"], args.file_path)
                if err:
                    return {"content": [{"type": "text", "text": err}], "isError": True}
                return {"content": [{"type": "text", "text": content}],
                        "details": {"skill": info["name"], "file": args.file_path}}
            try:
                sid = str(ctx.sessionManager.getSessionId())
            except Exception:  # noqa: BLE001 - 无会话号不拦调用
                sid = None
            # 与 /skill 同一份渲染：模板变量已替换、支撑文件清单已附
            message = build_skill_message(info, session_id=sid)
            linked = collect_linked_files(info["skill_dir"])
            if linked:
                message += ("\n\n[支撑文件：再调 skill_view(name=..., file_path=...) 取]\n"
                            + "\n".join(f"- {c}: {', '.join(fs)}" for c, fs in linked.items()))
            return {"content": [{"type": "text", "text": message}],
                    "details": {"skill": info["name"], "linked_files": linked}}

        class ManageParams(BaseModel):
            model_config = ConfigDict(extra="forbid")
            action: str = Field(description="create＝新建技能；write_file＝给技能加支撑文件")
            name: str = Field(description="技能名（小写连字符，与目录名一致）")
            content: str = Field("", description="create 用：完整 SKILL.md 文本"
                                                 "（frontmatter＋正文）")
            file_path: str = Field("", description="write_file 用：技能内相对路径"
                                                   "（如 references/规范.md）")
            file_content: str = Field("", description="write_file 用：文件内容")

        async def manage_execute(tool_call_id, raw, signal, on_update, ctx):
            from misaka.orchestration import skill_manage
            args = raw if isinstance(raw, ManageParams) else ManageParams(**(raw or {}))
            result = skill_manage.manage(
                args.action, args.name, profile_dir=profile_dir,
                content=args.content or None, file_path=args.file_path or None,
                file_content=args.file_content or None)
            lines = [result.get("message") or result.get("error") or ""]
            for key in ("gist", "description_preview", "hint", "lint_hint"):
                if result.get(key):
                    lines.append(str(result[key]))
            for warning in result.get("lint_warnings") or []:
                lines.append(f"  ⚠ [{warning['rule']}] {warning['message']}")
            return {"content": [{"type": "text", "text": "\n".join(x for x in lines if x)}],
                    "isError": not result.get("success"),
                    "details": {k: v for k, v in result.items() if k != "message"}}

        harn.registerTool(ToolDefinition(
            name="skill_manage", label="沉淀技能",
            description="把可复用的做法沉淀成技能——技能是你的程序性记忆。"
                        "action='create' 给完整 SKILL.md；action='write_file' 加参考资料/模板/脚本。"
                        "**写技能只能用这个工具**，不要用通用 write 直接写技能目录"
                        "（写权闸、校验、安全扫描、变更总账都在这条路上）。",
            parameters=ManageParams.model_json_schema(), execute=manage_execute,
            promptSnippet="沉淀/修改技能",
            promptGuidelines=[
                "技能的 description 必须一句话且不超过 60 字符——索引会截断更长的，"
                "路由信号就丢了；细节写进正文。",
                "写技能一律用 skill_manage 工具，不要用 write/edit 直接动技能目录。"]))

        harn.registerTool(ToolDefinition(
            name="skill_view", label="读技能",
            description="取一个技能的完整内容（模板变量已展开、支撑文件已列出）。"
                        "首次调用返回 SKILL.md 正文与支撑文件清单；要读其中某个文件，"
                        "再调一次并给 file_path。",
            parameters=ViewParams.model_json_schema(), execute=view_execute,
            promptSnippet="取某个技能的完整内容",
            promptGuidelines=["任务与某个技能的描述对上时，先 skill_view 取它的全文再动手，"
                              "别凭技能名猜它怎么用。"]))

    return register


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

        async def learn_cmd(args, ctx):
            from misaka.orchestration.learn_prompt import build_learn_prompt
            from misaka.orchestration import skill_write
            target = os.path.join(profile_dir, "skills")
            os.makedirs(target, exist_ok=True)
            prompt = build_learn_prompt(args or "", target_dir=target)
            # 沉淀走 skill_manage 工具（宪法 D2 的闸、校验、扫描、总账都在那条路上）
            decision, note = skill_write.evaluate_gate()
            prompt += (
                "\n\n---\n[沉淀入口] **用 `skill_manage` 工具写技能，不要用 write/edit "
                "直接写技能目录**——写权闸、frontmatter 硬校验、安全扫描、变更总账都在"
                "那条路上。`skill_manage(action='create', name=..., content=<完整 SKILL.md>)`；"
                "参考资料/模板/脚本用 `action='write_file'` 追加。\n"
                "description 必须一句话且 ≤60 字符（索引会截断更长的），细节写进正文。")
            if decision == "stage":
                prompt += (f"\n{note}\n工具会把写入暂存起来并返回 pending_id——"
                           "把技能名与一行摘要报给用户，让他用 `misaka skills pending` "
                           "查看、`misaka skills approve <名>` 批准。")
            await ctx.sendUserMessage(prompt)

        harn.registerCommand("learn", {
            "handler": learn_cmd,
            "description": "把描述的东西学成技能：/learn <目录/链接/「刚做的」＋要求>；"
                           "空参＝沉淀本次对话"})

    return register

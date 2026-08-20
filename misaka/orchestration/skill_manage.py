"""技能沉淀入口（hermes tools/skill_manager_tool.py 的 skill_manage 移植）。

在此之前 misaka 的 agent 是用**通用 write 工具**自由写技能文件，闸只能靠 `/learn`
的 prompt 说服它写去暂存区——正路是软的。hermes 的形态是：所有技能写入走一个工具，
闸在工具第一行，检查全在里面，一个入口保证不漏。

上游链路逐段对应（tools/skill_manager_tool.py:1543-1700）：
    ① 写权闸 `_apply_skill_write_gate` —— stage 时存**意图**（payload）不是产物，
       批准时 `apply_skill_pending` **重放**同一调用，ContextVar 绕过闸防二次触发
    ② 变更前快照 `capture_before`
    ③ dispatch 到各 action handler
    ④ 成功后：记总账 → **清系统提示技能索引缓存** → 使用统计（P2 未做）
创建路径的硬校验（`_create_skill` + `_validate_frontmatter(new_skill=True)`）：
    名字 → frontmatter（含 **60 字符硬拒**）→ 内容大小 → 撞名 → 原子写 →
    **安全扫描，block 就整个删掉回滚** → lint findings 回传给 agent 自己修

有意只做 create / write_file 两个 action：它们构成「沉淀」这条路径。
edit/patch/delete 属维护路径，上游那五个 handler 尚未逐行读完——不基于未读代码猜。
"""
import contextvars
import os
import shutil
from pathlib import Path

from misaka.orchestration import skill_write

MAX_SKILL_CONTENT_CHARS = 40_000     # 上游同量级：超了该拆去 references/
MAX_DESCRIPTION_LENGTH = 1024        # agentskills.io 标准上限
_VALID_NAME = __import__("re").compile(r"^[a-z0-9一-鿿][\w一-鿿-]*$")

# 重放已批准的暂存写入时绕过闸（上游 _skill_gate_bypass 同款）
_bypass = contextvars.ContextVar("misaka_skill_gate_bypass", default=False)


def lookup_path_error(name):
    """技能名的路径安全校验（hermes `_skill_lookup_path_error` 同款）。
    agent 传进来的是原始字符串——绝对路径、`..`、Windows 盘符都不许进搜索根。
    合法返回 None，否则返回人话原因。"""
    from pathlib import PurePosixPath, PureWindowsPath
    if not isinstance(name, str):
        return "技能名必须是字符串"
    value = name.strip()
    if not value:
        return "技能名不能为空"
    if PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute() \
            or PureWindowsPath(value).drive:
        return f"技能名不能是绝对路径：{value}"
    if ".." in PurePosixPath(value).parts or ".." in PureWindowsPath(value).parts:
        return f"技能名不能含 `..`：{value}"
    return None



def _skills_root(profile_dir):
    return Path(profile_dir) / "skills"


def validate_frontmatter(content, *, new_skill=False):
    """SKILL.md 硬校验。返回错误字符串或 None。

    new_skill=True（**仅创建路径**）时 description 还须塞进 60 字符的索引预算——
    新写的技能绝不能一出生就丢路由信号。edit/patch **有意跳过**这条：已经超限的
    老技能得还能被维护（上游 _validate_frontmatter 的注释逐字如此）。"""
    from misaka.core.skills import SKILL_PROMPT_DESC_LIMIT
    from misaka.utils.frontmatter import parse_frontmatter

    if not str(content or "").strip():
        return "内容不能为空。"
    text = str(content).lstrip("﻿")
    if not text.startswith("---"):
        return "SKILL.md 必须以 YAML frontmatter（---）开头。"
    parsed = parse_frontmatter(text)
    fm = parsed.frontmatter
    if not isinstance(fm, dict) or not fm:
        return "frontmatter 没有闭合，或不是 key: value 映射。"
    if "name" not in fm:
        return "frontmatter 必须有 name 字段。"
    if "description" not in fm:
        return "frontmatter 必须有 description 字段。"
    desc = str(fm["description"]).strip().strip("'\"")
    if len(desc) > MAX_DESCRIPTION_LENGTH:
        return f"description 超过 {MAX_DESCRIPTION_LENGTH} 字符。"
    if new_skill and len(desc) > SKILL_PROMPT_DESC_LIMIT:
        return (f"description 有 {len(desc)} 字符——新技能必须塞进 "
                f"{SKILL_PROMPT_DESC_LIMIT} 字符的索引预算（一句话、触发条件在前、"
                f"句号结尾）。索引会把更长的截断成 {SKILL_PROMPT_DESC_LIMIT - 3} 字符"
                "＋省略号，路由信号就毁了。细节挪进正文。")
    if not (parsed.body or "").strip():
        return "frontmatter 之后必须有正文（步骤、流程等）。"
    return None


def validate_content_size(content, label="SKILL.md"):
    if len(str(content or "")) > MAX_SKILL_CONTENT_CHARS:
        return (f"{label} 有 {len(content):,} 字符（上限 {MAX_SKILL_CONTENT_CHARS:,}）。"
                "拆成更小的 SKILL.md ＋ references/ 里的支撑文件。")
    return None


def _security_scan(skill_dir):
    """写完即扫（上游 _security_scan_skill）。返回错误串＝该回滚，None＝放行。
    `ask` 裁决对 agent 写入按 block 处理——没有人在场回答 ask。"""
    try:
        from misaka.orchestration.skills_guard import (
            format_scan_report, scan_skill, should_allow_install)
        result = scan_skill(Path(skill_dir), source="agent-created")
        allowed, reason = should_allow_install(result)
        if allowed is False or allowed is None:
            return f"安全扫描拦下了这个技能（{reason}）：\n{format_scan_report(result)}"
    except Exception:  # noqa: BLE001 - 扫描设施故障不拦沉淀（fail-open，与加载期一致）
        return None
    return None


def _lint_findings(skill_md):
    """顾问层检查，附进返回值给 agent 看——让它当场自己修，而不是等人审才发现。"""
    try:
        from misaka.orchestration.skill_linter import lint_skill
        found = lint_skill(Path(skill_md).parent)
    except Exception:  # noqa: BLE001
        return []
    return [{"severity": f.severity, "rule": f.rule, "message": f.message} for f in found]


def _description_preview(content):
    """description 在索引里会显示成什么（上游 _add_description_prompt_preview）。"""
    from misaka.core.skills import is_skill_description_truncated, truncate_skill_description
    from misaka.utils.frontmatter import parse_frontmatter
    desc = str((parse_frontmatter(content).frontmatter or {}).get("description") or "")
    if not is_skill_description_truncated(desc):
        return None
    return f"索引里会显示成：{truncate_skill_description(desc)}"


def _invalidate_index():
    """技能变了，系统提示里的 <available_skills> 必须失效重建（上游
    clear_skills_system_prompt_cache）。此前 misaka 的索引是会话启动时的快照——
    沉淀完当前会话看不见新技能，而 skills_list 工具能看见，两个来源不一致。"""
    try:
        from misaka.core import skills as _skills
        invalidate = getattr(_skills, "invalidate_skills_cache", None)
        if callable(invalidate):
            invalidate()
    except Exception:  # noqa: BLE001
        pass


def _create(profile_dir, name, content):
    err = None if _VALID_NAME.fullmatch(name) else \
        f"技能名「{name}」非法：小写字母/数字/中文开头，只含 字母数字_-中文"
    err = err or validate_frontmatter(content, new_skill=True)
    err = err or validate_content_size(content)
    if err:
        return {"success": False, "error": err}

    skill_dir = _skills_root(profile_dir) / name
    if skill_dir.exists():
        return {"success": False, "error": f"已经有叫「{name}」的技能了：{skill_dir}"}

    skill_dir.mkdir(parents=True, exist_ok=True)
    md = skill_dir / "SKILL.md"
    tmp = md.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, md)
    os.chmod(md, 0o644)

    scan_error = _security_scan(skill_dir)
    if scan_error:                      # 扫出问题＝整个删掉，不留半成品
        shutil.rmtree(skill_dir, ignore_errors=True)
        return {"success": False, "error": scan_error}

    result = {"success": True, "message": f"技能「{name}」已建立。",
              "skill_md": str(md), "path": str(skill_dir)}
    findings = _lint_findings(md)
    if findings:
        result["lint_warnings"] = findings
        result["lint_hint"] = ("技能已建立。这些是编写约定的顾问意见（不是拦截）——"
                               "用 skill_manage(action='write_file') 或改 SKILL.md 修掉。")
    preview = _description_preview(content)
    if preview:
        result["description_preview"] = preview
    result["hint"] = ("要加参考资料/模板/脚本：skill_manage(action='write_file', "
                      f"name='{name}', file_path='references/例子.md', file_content='...')")
    return result


MAX_SKILL_FILE_BYTES = 1024 * 1024   # 上游同值（1 MiB；字符上限之外的字节闸）


def _resolve_target(skill_dir, file_path):
    """(target, err)：路径校验＋限制在技能目录内（上游 _resolve_skill_target）。"""
    err = lookup_path_error(file_path)
    if err:
        return None, err
    target = (Path(skill_dir) / file_path).resolve()
    try:
        target.relative_to(Path(skill_dir).resolve())
    except ValueError:
        return None, f"路径超出技能目录：{file_path}"
    return target, None


def _require_skill(profile_dir, name):
    """(skill_dir, err)。skill_manage 只写本角色层——项目层是仓的（信任模型只读）、
    共享层是大家的，都不该被单个 agent 的写工具动到（宪法④的所有权边界）。"""
    skill_dir = _skills_root(profile_dir) / name
    if not (skill_dir / "SKILL.md").is_file():
        return None, f"没有叫「{name}」的技能（先 create，或它不在本角色层）"
    return skill_dir, None


def _atomic_write(target, text):
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)


def _write_file(profile_dir, name, file_path, file_content):
    if file_content is None:
        return {"success": False, "error": "write_file 需要 file_content（空文件传空串）"}
    if len(file_content.encode("utf-8")) > MAX_SKILL_FILE_BYTES:
        return {"success": False,
                "error": f"文件超过 {MAX_SKILL_FILE_BYTES:,} 字节（1 MiB）——拆小些"}
    err = validate_content_size(file_content, label=file_path)
    if err:
        return {"success": False, "error": err}
    skill_dir, err = _require_skill(profile_dir, name)
    if err:
        return {"success": False, "error": err}
    target, err = _resolve_target(skill_dir, file_path)
    if err:
        return {"success": False, "error": err}
    if target.name == "SKILL.md":
        # 有意比上游严：hermes 的 write_file 能覆盖 SKILL.md 绕过 frontmatter 校验
        #（第二轮调研点名的上游可疑点，建议不抄）——SKILL.md 只走 create/edit
        return {"success": False, "error": "SKILL.md 请走 create/edit（要过 frontmatter 校验）"}

    target.parent.mkdir(parents=True, exist_ok=True)
    # 备份原内容：覆盖已有文件后扫描拦下时**恢复原文**，不是删掉（上游 _write_file
    # 同款——此前这里 unlink 会把用户原有的文件一并删掉，2026-08-20 对表修正）
    original = target.read_text(encoding="utf-8") if target.exists() else None
    _atomic_write(target, file_content)

    scan_error = _security_scan(skill_dir)
    if scan_error:
        if original is not None:
            _atomic_write(target, original)
        else:
            target.unlink(missing_ok=True)
        return {"success": False, "error": scan_error}
    return {"success": True, "message": f"已写入 {name}/{file_path}", "path": str(target)}


def _edit_skill(profile_dir, name, content):
    """整篇重写 SKILL.md（上游 _edit_skill：只用于大改，小修用 patch）。
    frontmatter 校验不带 new_skill——已超限的老 description 仍可维护。"""
    err = validate_frontmatter(content) or validate_content_size(content)
    if err:
        return {"success": False, "error": err}
    skill_dir, err = _require_skill(profile_dir, name)
    if err:
        return {"success": False, "error": err}
    md = skill_dir / "SKILL.md"
    original = md.read_text(encoding="utf-8")
    _atomic_write(md, content)
    scan_error = _security_scan(skill_dir)
    if scan_error:
        _atomic_write(md, original)
        return {"success": False, "error": scan_error}
    result = {"success": True, "message": f"技能「{name}」已整篇重写。", "path": str(skill_dir)}
    preview = _description_preview(content)
    if preview:
        result["description_preview"] = preview
    return result


def _patch_skill(profile_dir, name, old_string, new_string, file_path=None,
                 replace_all=False):
    """定点替换（上游 _patch_skill：**小修首选**，别动辄整篇重写）。
    缺省打 SKILL.md，file_path 指定则打支撑文件；非 replace_all 要求唯一命中。
    ponytail: 上游用 49KB 的 fuzzy_match 引擎容忍空白/缩进漂移，这里先精确匹配
    ——命中失败会回传文件预览让模型自纠；精确匹配真成瓶颈再引 fuzzy。"""
    if not old_string:
        return {"success": False, "error": "patch 需要 old_string"}
    if new_string is None:
        return {"success": False, "error": "patch 需要 new_string（删除匹配段就传空串）"}
    skill_dir, err = _require_skill(profile_dir, name)
    if err:
        return {"success": False, "error": err}
    if file_path:
        target, err = _resolve_target(skill_dir, file_path)
        if err:
            return {"success": False, "error": err}
    else:
        target = skill_dir / "SKILL.md"
    if not target.exists():
        return {"success": False, "error": f"文件不存在：{file_path or 'SKILL.md'}"}

    content = target.read_text(encoding="utf-8")
    count = content.count(old_string)
    if count == 0:
        return {"success": False,
                "error": "old_string 没有命中（要逐字一致，含空白与缩进）",
                "file_preview": content[:500] + ("..." if len(content) > 500 else "")}
    if count > 1 and not replace_all:
        return {"success": False,
                "error": f"old_string 命中 {count} 处——加长到唯一，或 replace_all=true"}
    new_content = content.replace(old_string, new_string) if replace_all \
        else content.replace(old_string, new_string, 1)

    label = file_path or "SKILL.md"
    err = validate_content_size(new_content, label=label)
    if err:
        return {"success": False, "error": err}
    if not file_path:   # 打的是 SKILL.md：改完结构必须仍然完整（上游同款复验）
        err = validate_frontmatter(new_content)
        if err:
            return {"success": False, "error": f"这个 patch 会打破 SKILL.md 结构：{err}"}

    _atomic_write(target, new_content)
    scan_error = _security_scan(skill_dir)
    if scan_error:
        _atomic_write(target, content)
        return {"success": False, "error": scan_error}
    n = count if replace_all else 1
    return {"success": True, "message": f"已 patch {label}（{n} 处替换）"}


def _delete_skill(profile_dir, name, absorbed_into=None):
    """删技能（上游 _delete_skill 的前台语义——misaka 无 curator 后台 fork，
    无归档路径，硬删）。absorbed_into 三态：None＝未声明（放行但记不出意图）；
    ""＝明确纯剪除；"<伞名>"＝内容已并入那把伞——目标必须真在盘上且不能是自己，
    模型不能声称一个不存在的伞（上游逐字语义）。"""
    skill_dir, err = _require_skill(profile_dir, name)
    if err:
        return {"success": False, "error": err}
    absorbed_target = (absorbed_into or "").strip()
    if absorbed_target:
        if absorbed_target == name:
            return {"success": False, "error": "absorbed_into 不能是被删的技能自己"}
        umbrella = _skills_root(profile_dir) / absorbed_target
        if not (umbrella / "SKILL.md").is_file():
            return {"success": False,
                    "error": f"absorbed_into=「{absorbed_target}」不存在——"
                             "先建/改好那把伞技能，再来删这个"}
    root = _skills_root(profile_dir).resolve()
    resolved = skill_dir.resolve()
    if resolved == root or root not in resolved.parents:
        return {"success": False, "error": "拒绝删除：目标不在本角色技能层内"}

    shutil.rmtree(skill_dir)
    message = f"技能「{name}」已删除。"
    if absorbed_target:
        message += f"内容已并入「{absorbed_target}」。"
    return {"success": True, "message": message}


def _remove_file(profile_dir, name, file_path):
    """删支撑文件（上游 _remove_file）：不存在时回传现有文件清单让模型自纠；
    删完清掉空子目录。"""
    skill_dir, err = _require_skill(profile_dir, name)
    if err:
        return {"success": False, "error": err}
    target, err = _resolve_target(skill_dir, file_path)
    if err:
        return {"success": False, "error": err}
    if target.name == "SKILL.md":
        return {"success": False, "error": "SKILL.md 不能用 remove_file 删（删整个技能用 delete）"}
    if not target.exists():
        available = [str(f.relative_to(skill_dir))
                     for sub in ("references", "templates", "assets", "scripts")
                     if (skill_dir / sub).exists()
                     for f in sorted((skill_dir / sub).rglob("*")) if f.is_file()]
        return {"success": False,
                "error": f"技能「{name}」里没有 {file_path}",
                "available_files": available or None}
    target.unlink()
    parent = target.parent
    if parent != skill_dir and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()
    return {"success": True, "message": f"已从「{name}」删除 {file_path}"}


_ACTIONS = ("create", "edit", "patch", "delete", "write_file", "remove_file")


def _gist(action, name, content="", file_path="", old_string=""):
    """待审列表里那一行摘要（上游 skill_gist 的位置）。"""
    if action == "write_file":
        return f"给技能「{name}」加文件 {file_path}"
    if action == "remove_file":
        return f"从技能「{name}」删文件 {file_path}"
    if action == "patch":
        return f"patch 技能「{name}」{('/' + file_path) if file_path else ''}：{old_string[:40]}…"
    if action == "delete":
        return f"删除技能「{name}」"
    from misaka.utils.frontmatter import parse_frontmatter
    desc = str((parse_frontmatter(content or "").frontmatter or {}).get("description") or "")
    label = "整篇重写" if action == "edit" else "新技能"
    return f"{label}「{name}」：{desc[:60]}" if desc else f"{label}「{name}」"


def manage(action, name, *, profile_dir, content=None, file_path=None,
           file_content=None, old_string=None, new_string=None,
           replace_all=False, absorbed_into=None):
    """技能写入的唯一入口（上游 skill_manage 六个 action 对齐）。
    闸→校验→写→扫描回滚→记账→索引失效。"""
    if action not in _ACTIONS:
        return {"success": False,
                "error": f"未知 action「{action}」。可用：{'、'.join(_ACTIONS)}"}

    if not _bypass.get():
        decision, note = skill_write.evaluate_gate()
        if decision == "stage":
            payload = {"action": action, "name": name, "profile_dir": profile_dir,
                       "content": content, "file_path": file_path,
                       "file_content": file_content, "old_string": old_string,
                       "new_string": new_string, "replace_all": replace_all,
                       "absorbed_into": absorbed_into}
            gist = _gist(action, name, content or "", file_path or "", old_string or "")
            record = skill_write.stage(payload, summary=gist)
            return {"success": True, "staged": True, "pending_id": record["id"],
                    "gist": gist, "message": note}

    skill_dir = _skills_root(profile_dir) / name
    before = skill_write.snapshot(skill_dir)

    if action == "create":
        result = _create(profile_dir, name, content or "")
    elif action == "edit":
        result = _edit_skill(profile_dir, name, content or "")
    elif action == "patch":
        result = _patch_skill(profile_dir, name, old_string or "", new_string,
                              file_path=file_path, replace_all=replace_all)
    elif action == "delete":
        result = _delete_skill(profile_dir, name, absorbed_into=absorbed_into)
    elif action == "remove_file":
        result = _remove_file(profile_dir, name, file_path or "")
    else:
        result = _write_file(profile_dir, name, file_path or "", file_content)

    if result.get("success"):
        # delete 的 after 是空快照（目录已除名）；absorbed_into 进证据（上游同款）
        evidence = {k: v for k, v in (("file_path", file_path),
                                      ("absorbed_into", absorbed_into)) if v is not None}
        skill_write.record(action, name, before=before, after_root=skill_dir,
                           evidence=evidence)
        _invalidate_index()
    return result


def apply_pending(payload):
    """重放一条已批准的暂存写入（上游 apply_skill_pending）：绕过闸，走同一条路
    ——校验、扫描、记账一个都不少。"""
    token = _bypass.set(True)
    try:
        return manage(payload.get("action", ""), payload.get("name", ""),
                      profile_dir=payload.get("profile_dir", ""),
                      content=payload.get("content"),
                      file_path=payload.get("file_path"),
                      file_content=payload.get("file_content"),
                      old_string=payload.get("old_string"),
                      new_string=payload.get("new_string"),
                      replace_all=bool(payload.get("replace_all")),
                      absorbed_into=payload.get("absorbed_into"))
    finally:
        _bypass.reset(token)


if __name__ == "__main__":
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    os.environ["HOME"] = str(tmp)
    (tmp / ".misaka").mkdir()
    (tmp / ".misaka" / "skills.json").write_text('{"skill_write_mode":"allow"}', encoding="utf-8")
    prof = tmp / ".misaka" / "profiles" / "sisters" / "10032"
    prof.mkdir(parents=True)

    ok = ("---\nname: archive-tool\ndescription: 按入藏簿规范归档。\n---\n"
          "# 归档\n\n## 何时用\n拿到新材料时。\n")
    r = manage("create", "archive-tool", profile_dir=str(prof), content=ok)
    assert r["success"], r
    assert (prof / "skills" / "archive-tool" / "SKILL.md").is_file()

    # 60 字符：创建硬拒（不是警告）
    long_desc = "把材料按规范整理归档并逐条核对出处与年份是否可靠" * 3
    r = manage("create", "too-long", profile_dir=str(prof),
               content=f"---\nname: too-long\ndescription: {long_desc}\n---\n正文\n")
    assert not r["success"] and "索引预算" in r["error"], r
    assert not (prof / "skills" / "too-long").exists(), "拒绝就不该留目录"

    # 撞名／空正文／缺字段
    assert not manage("create", "archive-tool", profile_dir=str(prof), content=ok)["success"]
    assert "正文" in manage("create", "no-body", profile_dir=str(prof),
                            content="---\nname: no-body\ndescription: x。\n---\n")["error"]
    assert "name" in manage("create", "no-name", profile_dir=str(prof),
                            content="---\ndescription: x。\n---\n正文\n")["error"]

    # 内容上限
    big = f"---\nname: big\ndescription: 大。\n---\n" + "字" * (MAX_SKILL_CONTENT_CHARS + 1)
    assert "上限" in manage("create", "big", profile_dir=str(prof), content=big)["error"]

    # write_file：路径穿越被拒、SKILL.md 走 create
    r = manage("write_file", "archive-tool", profile_dir=str(prof),
               file_path="references/规范.md", file_content="规范正文")
    assert r["success"] and (prof / "skills" / "archive-tool" / "references" / "规范.md").is_file()
    assert not manage("write_file", "archive-tool", profile_dir=str(prof),
                      file_path="../../逃逸.md", file_content="x")["success"]
    assert not manage("write_file", "archive-tool", profile_dir=str(prof),
                      file_path="SKILL.md", file_content="x")["success"]

    # lint findings 回传给 agent（作者名不对 → 顾问意见，但技能照建）
    r = manage("create", "lint-me", profile_dir=str(prof),
               content="---\nname: lint-me\ndescription: 做事。\nauthor: makiko\n---\n"
                       "# x\n\n## 何时用\n用它。\n")
    assert r["success"] and any(f["rule"] == "author-value" for f in r["lint_warnings"])

    # 闸：forbid 档下存意图不落盘，批准时重放
    (tmp / ".misaka" / "skills.json").write_text('{"skill_write_mode":"forbid"}', encoding="utf-8")
    r = manage("create", "gated", profile_dir=str(prof),
               content="---\nname: gated\ndescription: 待审的。\n---\n# g\n\n## 何时用\n看情况。\n")
    assert r.get("staged") and r["pending_id"], r
    assert not (prof / "skills" / "gated").exists(), "暂存阶段绝不落盘"
    assert "gated" in r["gist"]
    pending = skill_write.get_pending(r["pending_id"])
    replayed = apply_pending(pending["payload"])
    assert replayed["success"] and (prof / "skills" / "gated" / "SKILL.md").is_file(), replayed

    kinds = [e["action"] for e in skill_write.entries()]
    assert kinds.count("create") >= 2 and "write_file" in kinds, kinds

    # ═ 维护路径（上游五 handler 逐行读完后补，2026-08-20）═
    (tmp / ".misaka" / "skills.json").write_text('{"skill_write_mode":"allow"}', encoding="utf-8")
    # edit：整篇重写；不带 new_skill（超限老 description 仍可维护）
    r = manage("edit", "archive-tool", profile_dir=str(prof),
               content="---\nname: archive-tool\ndescription: 归档（新版）。\n---\n# v2\n\n## 何时用\n换新流程。\n")
    assert r["success"] and "v2" in (prof / "skills" / "archive-tool" / "SKILL.md").read_text(encoding="utf-8")
    assert not manage("edit", "不存在的", profile_dir=str(prof), content=ok)["success"]

    # patch：唯一命中；歧义拒绝；SKILL.md 改后复验结构；no-match 回传预览
    r = manage("patch", "archive-tool", profile_dir=str(prof),
               old_string="换新流程", new_string="换更新流程")
    assert r["success"] and "1 处" in r["message"]
    md_text = (prof / "skills" / "archive-tool" / "SKILL.md").read_text(encoding="utf-8")
    assert "换更新流程" in md_text
    r = manage("patch", "archive-tool", profile_dir=str(prof),
               old_string="没有的字样", new_string="x")
    assert not r["success"] and r.get("file_preview"), "no-match 要回预览让模型自纠"
    r = manage("patch", "archive-tool", profile_dir=str(prof),
               old_string="---", new_string="x")
    assert not r["success"] and "命中" in r["error"], "多处命中必须拒绝"
    r = manage("patch", "archive-tool", profile_dir=str(prof),
               old_string="name: archive-tool\n", new_string="")
    assert not r["success"] and "打破" in r["error"], "patch 不许打破 frontmatter 结构"

    # write_file 覆盖回滚 bug 修正证明：覆盖已有文件失败时恢复原文（不是删掉）
    manage("write_file", "archive-tool", profile_dir=str(prof),
           file_path="references/规范.md", file_content="第一版")
    _orig_scan = _security_scan
    globals()["_security_scan"] = lambda d: "假装扫出问题"   # -m 跑时本模块是 __main__
    r = manage("write_file", "archive-tool", profile_dir=str(prof),
               file_path="references/规范.md", file_content="第二版")
    globals()["_security_scan"] = _orig_scan
    assert not r["success"]
    kept = (prof / "skills" / "archive-tool" / "references" / "规范.md").read_text(encoding="utf-8")
    assert kept == "第一版", f"扫描拦下必须恢复原文而非删掉（曾是真 bug）: {kept!r}"

    # remove_file：不存在回传清单；删后清空目录
    r = manage("remove_file", "archive-tool", profile_dir=str(prof), file_path="references/没有.md")
    assert not r["success"] and r.get("available_files") == ["references/规范.md"]
    r = manage("remove_file", "archive-tool", profile_dir=str(prof), file_path="references/规范.md")
    assert r["success"] and not (prof / "skills" / "archive-tool" / "references").exists(), "空目录该清掉"

    # delete：absorbed_into 三态（伞必须在盘、不能是自己）
    r = manage("delete", "archive-tool", profile_dir=str(prof), absorbed_into="不存在的伞")
    assert not r["success"] and "不存在" in r["error"]
    r = manage("delete", "archive-tool", profile_dir=str(prof), absorbed_into="archive-tool")
    assert not r["success"] and "自己" in r["error"]
    r = manage("delete", "archive-tool", profile_dir=str(prof), absorbed_into="gated")
    assert r["success"] and "并入" in r["message"]
    assert not (prof / "skills" / "archive-tool").exists()

    # 重放通道带全六 action 的参数
    (tmp / ".misaka" / "skills.json").write_text('{"skill_write_mode":"forbid"}', encoding="utf-8")
    r = manage("patch", "gated", profile_dir=str(prof),
               old_string="看情况", new_string="看局势")
    assert r.get("staged")
    replayed = apply_pending(skill_write.get_pending(r["pending_id"])["payload"])
    assert replayed["success"] and "看局势" in (prof / "skills" / "gated" / "SKILL.md").read_text(encoding="utf-8")

    print("skill_manage selfcheck ok — 六 action：60硬拒/撞名/上限/穿越/扫描回滚(恢复原文)/"
          "lint回传/闸与重放/记账/patch唯一命中+结构复验/delete三态/remove清单")

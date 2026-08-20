"""SKILL.md 结构检查（hermes tools/skill_linter.py 移植，按 misaka 改三处）。

存在理由：`learn_prompt.py` 的 HARDLINE 编写标准此前**只以散文形式讲给 LLM 听**，
照做与否无人验——DoD 第 3 条「有可跑自检」在这里是空的。这个模块把其中可机检的
条款变成检查，接在写权闸的人审口（`misaka skills pending`）上。

与上游的三处有意差异（第三份调研点名，照抄会执行不存在的约束）：
  ① description ≤60：**misaka 2026-08-20 才补上截断机制**（core.skills
     SKILL_PROMPT_DESC_LIMIT），所以这条现在才真正成立，直接复用那个判定
  ② 节名 `## When to Use` → `## 何时用`（learn_prompt 的中文节序）
  ③ shell 工具映射值换成 misaka 会话的真实工具名

纪律照上游：**顾问层，永不阻断**。ERROR 是结构性破损（名字非法/name 与目录不符），
WARNING 是约定。命令行跑：`python -m misaka.orchestration.skill_linter <技能目录>`，
**有 ERROR 才退 1**——CI 能卡住破损，不被约定小事绊住。
"""
import os
import re
from pathlib import Path

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
# learn_prompt 的禁用营销词（与其散文标准逐条对应）
_MARKETING = ("强大", "全面", "无缝", "先进", "健壮", "革命性", "业界领先",
              "powerful", "comprehensive", "seamless", "advanced", "robust")
# 会话里已被包装的裸命令 → 该用的工具（learn_prompt「工具口吻」那节的可机检部分）
_SHELL_TO_TOOL = {
    "cat": "read", "head": "read", "tail": "read", "sed": "edit",
    "awk": "bash", "find": "find", "ls": "ls", "rg": "grep", "grep": "grep",
}
_EXPECTED_SECTION = "## 何时用"
_FORBIDDEN_FILES = {"README.md", "CHANGELOG.md", "install.sh", ".env", ".env.example"}
_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_REF_RE = re.compile(r"(references|templates|assets)/[\w./\-一-鿿]+")


class Finding:
    __slots__ = ("rule", "severity", "message")

    def __init__(self, rule, severity, message):
        self.rule, self.severity, self.message = rule, severity, message

    def __repr__(self):
        mark = "✗" if self.severity == "error" else "⚠"
        return f"{mark} [{self.rule}] {self.message}"


def lint_content(content, skill_dir=None):
    """检查一份 SKILL.md 文本。skill_dir 给了才做落盘类检查（引用/禁用文件）。
    纯函数：除读被指向的文件外零 I/O（上游同款契约，创建路径可在落盘前先跑）。"""
    from misaka.core.skills import SKILL_PROMPT_DESC_LIMIT, is_skill_description_truncated
    from misaka.utils.frontmatter import parse_frontmatter

    findings = []
    parsed = parse_frontmatter(content)
    fm = parsed.frontmatter or {}
    body = parsed.body or ""

    name = str(fm.get("name") or "")
    if not name:
        findings.append(Finding("name-missing", "error", "frontmatter 缺 name"))
    elif not _NAME_RE.fullmatch(name):
        findings.append(Finding("name-format", "error",
                                f"name「{name}」须小写字母/数字开头，只含 a-z0-9_-"))
    elif len(name) > 64:
        findings.append(Finding("name-format", "error", f"name 超 64 字符（{len(name)}）"))

    if skill_dir and name and name != Path(skill_dir).name:
        findings.append(Finding("name-dir-mismatch", "error",
                                f"name「{name}」与目录名「{Path(skill_dir).name}」不符"
                                "——身份按 name 走，不符会让人对不上账"))

    desc = str(fm.get("description") or "").strip()
    if not desc:
        findings.append(Finding("description-missing", "error", "frontmatter 缺 description"))
    else:
        if is_skill_description_truncated(desc):
            findings.append(Finding(
                "description-length", "warning",
                f"description {len(desc)} 字符，超 {SKILL_PROMPT_DESC_LIMIT} "
                "的部分会在技能索引里被截断（每会话常驻，超出部分路由不到）"))
        low = desc.lower()
        hits = [w for w in _MARKETING if w in desc or w in low]
        if hits:
            findings.append(Finding("description-marketing", "warning",
                                    f"description 含营销词 {hits}——写能力不写形容"))
        if name and name in desc:
            findings.append(Finding("description-echo", "warning",
                                    "description 复读了技能名，浪费索引预算"))

    author = str(fm.get("author") or "")
    if author and author != "Misaka":
        findings.append(Finding("author-value", "warning",
                                f"author 应为字面值 Misaka（现为「{author}」）"
                                "——技能会被分享，环境身份是没同意过的隐私泄漏"))

    if _EXPECTED_SECTION not in body:
        findings.append(Finding("missing-section", "warning",
                                f"缺「{_EXPECTED_SECTION}」节——路由靠它判断何时该用"))

    prose = _CODE_FENCE.sub("", body)   # 围栏代码块里的命令是示例，不算
    bare = sorted({cmd for token in _INLINE_CODE.findall(prose)
                   for cmd in (token.strip().split()[:1] or [""])
                   if cmd in _SHELL_TO_TOOL})
    if bare:
        pairs = "、".join(f"{c}→{_SHELL_TO_TOOL[c]}" for c in bare)
        findings.append(Finding("shell-utility-reference", "warning",
                                f"正文用裸命令口吻（{pairs}）——会话里这些已被工具包装"))

    if skill_dir:
        root = Path(skill_dir)
        for ref in sorted({m.group(0) for m in _REF_RE.finditer(body)}):
            if not (root / ref).exists():
                findings.append(Finding("dangling-reference", "warning",
                                        f"正文引用了不存在的文件：{ref}"))
        for f in sorted(_FORBIDDEN_FILES):
            if (root / f).exists():
                findings.append(Finding("forbidden-file", "warning",
                                        f"技能里不该带 {f}"))
    return findings


def lint_skill(skill_dir):
    """检查一个技能目录。SKILL.md 不在＝一条 error。"""
    md = Path(skill_dir) / "SKILL.md"
    if not md.is_file():
        return [Finding("skill-md-missing", "error", f"{skill_dir} 里没有 SKILL.md")]
    try:
        content = md.read_text(encoding="utf-8-sig")
    except OSError as e:
        return [Finding("read-failed", "error", str(e))]
    return lint_content(content, skill_dir=skill_dir)


def format_findings(findings):
    if not findings:
        return "  ✓ 无问题"
    return "\n".join(f"  {f!r}" for f in findings)


if __name__ == "__main__":
    import sys
    import tempfile

    if len(sys.argv) > 1:      # 命令行模式：有 ERROR 才退 1
        got = lint_skill(sys.argv[1])
        print(format_findings(got))
        sys.exit(1 if any(f.severity == "error" for f in got) else 0)

    tmp = Path(tempfile.mkdtemp())
    good = tmp / "档案整理"
    good.mkdir()
    (good / "SKILL.md").write_text(
        "---\nname: 档案整理\ndescription: 按入藏簿规范归档。\nauthor: Misaka\n---\n"
        "# 档案整理\n\n## 何时用\n拿到新入藏材料时。\n\n## 怎么跑\n用 `read` 打开簿子。\n",
        encoding="utf-8")
    # 中文 name 不匹配 _NAME_RE（上游规则），这里只验其余规则不误报
    got = lint_skill(good)
    rules = {f.rule for f in got}
    assert "description-length" not in rules and "description-marketing" not in rules
    assert "missing-section" not in rules and "author-value" not in rules
    assert "shell-utility-reference" not in rules, f"围栏外的 `read` 是工具名不是裸命令: {got}"

    bad = tmp / "wrong-dir"
    bad.mkdir()
    (bad / "SKILL.md").write_text(
        "---\nname: other-name\ndescription: " + "一个强大而全面的技能，" * 6 + "\n"
        "author: makiko\n---\n# x\n\n用 `cat` 看文件，见 references/缺失.md。\n",
        encoding="utf-8")
    (bad / "README.md").write_text("x", encoding="utf-8")
    got = lint_skill(bad)
    rules = {f.rule for f in got}
    for expected in ("name-dir-mismatch", "description-length", "description-marketing",
                     "author-value", "missing-section", "shell-utility-reference",
                     "dangling-reference", "forbidden-file"):
        assert expected in rules, f"漏检 {expected}：{got}"
    assert any(f.severity == "error" for f in got), "name 与目录不符是 ERROR"

    assert lint_skill(tmp / "不存在")[0].rule == "skill-md-missing"
    print("skill_linter selfcheck ok — 名字/描述/营销词/作者/节序/工具口吻/悬空引用/禁用文件")

"""Structural and editorial checks for SKILL.md files."""
import re
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")     # the one rule for a skill's name, which is its directory
_MARKETING = ("powerful", "comprehensive", "seamless", "advanced", "robust",
              "cutting-edge", "state-of-the-art", "revolutionary", "industry-leading")
_SHELL_TO_TOOL = {
    "cat": "read", "head": "read", "tail": "read", "sed": "edit",
    "awk": "bash", "find": "find", "ls": "ls", "rg": "grep", "grep": "grep",
}
_EXPECTED_SECTION = "## When to Use"
_FORBIDDEN_FILES = {"README.md", "CHANGELOG.md", "install.sh", ".env", ".env.example", ".gitignore"}
_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_REF_RE = re.compile(r"(references|templates|assets)/[\w./\-\u4e00-\u9fff]+")


class Finding:
    __slots__ = ("message", "rule", "severity")

    def __init__(self, rule, severity, message):
        self.rule, self.severity, self.message = rule, severity, message

    def __repr__(self):
        mark = "✗" if self.severity == "error" else "⚠"
        return f"{mark} [{self.rule}] {self.message}"


def lint_content(content, skill_dir=None):
    """Check one SKILL.md document and its optional support directory."""
    from misaka.core.skills.index import (
        SKILL_PROMPT_DESC_LIMIT,
        is_skill_description_truncated,
    )
    from misaka.utils.frontmatter import FrontmatterError, parse_frontmatter

    findings = []
    try:
        parsed = parse_frontmatter(content)
    except FrontmatterError as error:
        return [Finding("frontmatter-yaml", "error", f"frontmatter is not valid YAML: {error}")]
    fm = parsed.frontmatter or {}
    body = parsed.body or ""

    name = fm.get("name") if isinstance(fm.get("name"), str) else ""
    if not name:
        findings.append(Finding("name-missing", "error", "frontmatter is missing a `name` string"))
    elif not NAME_RE.fullmatch(name):
        findings.append(Finding("name-format", "error",
                                f"name '{name}' must start with a lowercase letter or digit and use only a-z, 0-9, _, or -"))
    elif len(name) > 64:
        findings.append(Finding("name-format", "error", f"name exceeds 64 characters ({len(name)})"))

    if skill_dir and name and name != Path(skill_dir).name:
        findings.append(Finding(
            "name-dir-mismatch", "error",
            f"name '{name}' does not match directory '{Path(skill_dir).name}'",
        ))

    desc = fm.get("description").strip() if isinstance(fm.get("description"), str) else ""
    if not desc:
        findings.append(Finding("description-missing", "error", "frontmatter is missing a `description` string"))
    else:
        if is_skill_description_truncated(desc):
            findings.append(Finding(
                "description-length", "warning",
                f"description has {len(desc)} characters; the skill index truncates it after {SKILL_PROMPT_DESC_LIMIT}",
            ))
        low = desc.lower()
        hits = [w for w in _MARKETING if re.search(rf"\b{re.escape(w)}\b", low)]
        if hits:
            findings.append(Finding("description-marketing", "warning",
                                    f"description contains promotional wording: {hits}"))
        if name and name in desc:
            findings.append(Finding("description-echo", "warning",
                                    "description repeats the skill name without adding useful routing detail"))

    author = str(fm.get("author") or "")
    if author and author != "Misaka":
        findings.append(Finding(
            "author-value", "warning",
            f"author should be 'Misaka', not '{author}'; skills are shared and should not expose a role identity",
        ))

    if _EXPECTED_SECTION not in body:
        findings.append(Finding("missing-section", "warning",
                                f"missing `{_EXPECTED_SECTION}` section with concrete trigger conditions"))

    prose = _CODE_FENCE.sub("", body)
    bare = sorted({cmd for token in _INLINE_CODE.findall(prose)
                   for cmd in (token.strip().split()[:1] or [""])
                   if cmd in _SHELL_TO_TOOL})
    if bare:
        pairs = ','.join(f"{c}→{_SHELL_TO_TOOL[c]}" for c in bare)
        findings.append(Finding("shell-utility-reference", "warning",
                                f"prose names shell commands directly ({pairs}); prefer available session tools"))

    if skill_dir:
        root = Path(skill_dir)
        for ref in sorted({m.group(0) for m in _REF_RE.finditer(body)}):
            if not (root / ref).exists():
                findings.append(Finding("dangling-reference", "warning",
                                        f"referenced support file does not exist: {ref}"))
        for f in sorted(_FORBIDDEN_FILES):
            if (root / f).exists():
                findings.append(Finding("forbidden-file", "warning",
                                        f"skill directory should not contain {f}"))
    return findings


def lint_skill(skill_dir):
    """Lint the SKILL.md inside ``skill_dir``."""
    md = Path(skill_dir) / "SKILL.md"
    if not md.is_file():
        return [Finding("skill-md-missing", "error", f"SKILL.md not found in {skill_dir}")]
    try:
        content = md.read_text(encoding="utf-8-sig")
    except OSError as e:
        return [Finding("read-failed", "error", str(e))]
    return lint_content(content, skill_dir=skill_dir)


def format_findings(findings):
    if not findings:
        return "  ✓ No issues found."
    return "\n".join(f"  {f!r}" for f in findings)

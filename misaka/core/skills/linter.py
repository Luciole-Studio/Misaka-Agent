"""Hermes advisory rules plus MISAKA's typed mutation/privacy contract."""

from pathlib import Path

from .vendor import linter as native
from .vendor.manager import VALID_NAME_RE as NAME_RE


def Finding(rule, severity, message):
    return native.LintFinding(severity, rule, message)


def lint_content(content, skill_dir=None):
    from misaka.utils.frontmatter import FrontmatterError, parse_frontmatter

    try:
        fm = parse_frontmatter(content).frontmatter or {}
    except FrontmatterError as error:
        return [
            Finding(
                "frontmatter-yaml", "error", f"frontmatter is not valid YAML: {error}"
            )
        ]
    findings = native.lint_content(
        content, skill_dir=Path(skill_dir) if skill_dir is not None else None
    )
    for key in ("name", "description"):
        if not isinstance(fm.get(key), str) or not fm[key].strip():
            findings.append(
                Finding(
                    key + "-missing",
                    "error",
                    f"frontmatter is missing a `{key}` string",
                )
            )
    name = fm.get("name")
    if isinstance(name, str) and len(name) > 64:
        findings.append(Finding("name-format", "error", "name exceeds 64 characters"))
    # The upstream manager permits dots but its advisory linter still rejects
    # them. Keep the manager's rule; do not turn an advisory inconsistency into
    # an inaccessible write path.
    if isinstance(name, str) and NAME_RE.fullmatch(name) and len(name) <= 64:
        findings = [f for f in findings if f.rule != "name-format"]
    findings = [f for f in findings if f.rule != "author-caps"]
    if fm.get("author") and fm["author"] != "Misaka":
        findings.append(
            Finding(
                "author-value",
                "warning",
                "author should be 'Misaka'; never infer a login or role identity",
            )
        )
    return findings


def lint_skill(skill_dir):
    path = Path(skill_dir) / "SKILL.md"
    try:
        return lint_content(path.read_text(encoding="utf-8-sig"), skill_dir)
    except (OSError, UnicodeError) as error:
        return [Finding("read-failed", "error", str(error))]


def format_findings(findings):
    return (
        "\n".join(
            f"  {'✗' if f.severity == 'error' else '⚠'} [{f.rule}] {f.message}"
            for f in findings
        )
        or "  ✓ No issues found."
    )

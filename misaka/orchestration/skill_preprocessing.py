"""SKILL.md 预处理（hermes agent/skill_preprocessing.py 近逐字移植，MIT）。

与上游逐段对应：模板变量（MISAKA_SKILL_DIR/MISAKA_SESSION_ID——上游 HERMES_ 前缀
改名，未解析的原样留给作者排查）；行内 shell !`cmd`（单行、默认关、超时与输出
截断、单条失败不毁整篇）。省略：Windows 兼容层（misaka 单机 darwin）。
配置读 ~/.misaka/skills.json（skill_layers.load_skills_config）。
"""
import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Matches ${MISAKA_SKILL_DIR} / ${MISAKA_SESSION_ID} tokens in SKILL.md.
# Tokens that don't resolve are left as-is so the user can debug them.
_SKILL_TEMPLATE_RE = re.compile(r"\$\{(MISAKA_SKILL_DIR|MISAKA_SESSION_ID)\}")

# Matches inline shell snippets like:  !`date +%Y-%m-%d`
# Non-greedy, single-line only -- no newlines inside the backticks.
_INLINE_SHELL_RE = re.compile(r"!`([^`\n]+)`")

# Cap inline-shell output so a runaway command can't blow out the context.
_INLINE_SHELL_MAX_OUTPUT = 4000


def load_skills_config():
    from misaka.orchestration.skill_layers import load_skills_config as _load
    return _load()


def substitute_template_vars(content, skill_dir, session_id):
    """Replace ${MISAKA_SKILL_DIR} / ${MISAKA_SESSION_ID} in skill content.

    Only substitutes tokens for which a concrete value is available --
    unresolved tokens are left in place so the author can spot them.
    """
    if not content:
        return content
    skill_dir_str = str(skill_dir) if skill_dir else None

    def _replace(match):
        token = match.group(1)
        if token == "MISAKA_SKILL_DIR" and skill_dir_str:
            return skill_dir_str
        if token == "MISAKA_SESSION_ID" and session_id:
            return str(session_id)
        return match.group(0)

    return _SKILL_TEMPLATE_RE.sub(_replace, content)


def run_inline_shell(command, cwd, timeout):
    """Execute a single inline-shell snippet and return its stdout (trimmed).

    Failures return a short ``[inline-shell error: ...]`` marker instead of
    raising, so one bad snippet can't wreck the whole skill message.
    """
    try:
        completed = subprocess.run(
            ["bash", "-c", command],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=max(1, int(timeout)),
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return f"[inline-shell timeout after {timeout}s: {command}]"
    except FileNotFoundError:
        return "[inline-shell error: bash not found]"
    except Exception as exc:  # noqa: BLE001 - 上游同款：单条失败不毁整篇
        return f"[inline-shell error: {exc}]"

    output = (completed.stdout or "").rstrip("\n")
    if not output and completed.stderr:
        output = completed.stderr.rstrip("\n")
    if len(output) > _INLINE_SHELL_MAX_OUTPUT:
        output = output[:_INLINE_SHELL_MAX_OUTPUT] + "...[truncated]"
    return output


def expand_inline_shell(content, skill_dir, timeout):
    """Replace every !`cmd` snippet in ``content`` with its stdout.

    Runs each snippet with the skill directory as CWD so relative paths in
    the snippet work the way the author expects.
    """
    if "!`" not in content:
        return content

    def _replace(match):
        cmd = match.group(1).strip()
        if not cmd:
            return ""
        return run_inline_shell(cmd, skill_dir, timeout)

    return _INLINE_SHELL_RE.sub(_replace, content)


def preprocess_skill_content(content, skill_dir, session_id=None, skills_cfg=None):
    """Apply configured SKILL.md template and inline-shell preprocessing."""
    if not content:
        return content
    cfg = skills_cfg if isinstance(skills_cfg, dict) else load_skills_config()
    if cfg.get("template_vars", True):
        content = substitute_template_vars(content, skill_dir, session_id)
    if cfg.get("inline_shell", False):
        timeout = int(cfg.get("inline_shell_timeout", 10) or 10)
        content = expand_inline_shell(content, skill_dir, timeout)
    return content


if __name__ == "__main__":
    import tempfile

    d = Path(tempfile.mkdtemp()) / "某技能"
    d.mkdir()
    text = "目录在 ${MISAKA_SKILL_DIR}，会话 ${MISAKA_SESSION_ID}，未知 ${OTHER}"
    out = substitute_template_vars(text, d, "s-1")
    assert str(d) in out and "s-1" in out and "${OTHER}" in out, \
        "解析得了的换，解析不了的原样留（作者可排查）"
    assert substitute_template_vars(text, None, None) == text

    assert run_inline_shell("echo 入藏簿", None, 5) == "入藏簿"
    assert "timeout" in run_inline_shell("sleep 5", None, 1)
    assert "error" in run_inline_shell("exit 3", None, 5) or \
        run_inline_shell("exit 3", None, 5) == "", "无输出失败不炸"
    long = run_inline_shell("yes x | head -c 9000", None, 5)
    assert long.endswith("...[truncated]") and len(long) <= 4000 + 20

    body = "今天是 !`echo 2026-08-18`，完。"
    assert expand_inline_shell(body, None, 5) == "今天是 2026-08-18，完。"
    assert expand_inline_shell("无标记", None, 5) == "无标记"

    cfg_off = {"template_vars": True, "inline_shell": False}
    assert "!`echo x`" in preprocess_skill_content(
        "a !`echo x` ${MISAKA_SKILL_DIR}", d, "s", cfg_off), \
        "inline_shell 默认关（上游同默认）"
    cfg_on = {"template_vars": True, "inline_shell": True}
    done = preprocess_skill_content("a !`echo x` ${MISAKA_SKILL_DIR}", d, "s", cfg_on)
    assert "a x" in done and str(d) in done
    print("skill_preprocessing selfcheck ok — 变量/行内shell/截断/默认关 全对")

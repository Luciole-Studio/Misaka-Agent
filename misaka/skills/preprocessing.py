"""Expand supported variables and includes in SKILL.md content."""
import logging
import re
import subprocess

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
    from misaka.skills.layers import load_skills_config as _load
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
    except Exception as exc:  # noqa: BLE001 - one bad snippet must not take down the whole skill
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

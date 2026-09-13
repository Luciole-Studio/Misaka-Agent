# Hermes f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140 / agent/coding_context.py; see PROVENANCE.json and LICENSE.
import os
import tempfile
from pathlib import Path
from dataclasses import dataclass
from typing import Optional
CODING_TOOLSET = "coding"
CODING_AGENT_GUIDANCE = ""

INTERACTIVE_CODING_PLATFORMS = {"cli", "tui", "acp", "desktop", ""}


_PROJECT_MARKERS = (
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "package.json", "tsconfig.json", "deno.json",
    "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "build.gradle.kts", "Gemfile", "composer.json", "mix.exs",
    "pubspec.yaml", "CMakeLists.txt", "Makefile", "Dockerfile", "AGENTS.md", "CLAUDE.md", ".cursorrules",
)


_CODE_EXTENSIONS = frozenset({
    ".py", ".pyi", ".ipynb", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".kt", ".kts",
    ".scala", ".rb", ".php", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".swift", ".m", ".mm", ".dart", ".ex", ".exs",
    ".lua", ".sh", ".bash", ".zsh", ".sql", ".vue", ".svelte", ".r", ".jl", ".hs", ".clj", ".erl", ".pl",
})


_CODE_SCAN_SKIP_DIRS = frozenset({
    ".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build", "target", ".next", ".turbo", "vendor",
})


_CODE_SCAN_MAX_ENTRIES = 500


_NON_CODING_SKILL_CATEGORIES = (
    "apple", "communication", "cooking", "creative", "email", "finance", "gaming", "gifs", "health", "media",
    "music", "note-taking", "productivity", "shopping", "smart-home", "social-media", "travel", "yuanbao",
)


_MODE_ALIASES = {
    **dict.fromkeys(("focus", "strict", "lean"), "focus"),
    **dict.fromkeys(("on", "true", "yes", "1", "always"), "on"),
    **dict.fromkeys(("off", "false", "no", "0", "never"), "off"),
}


@dataclass(frozen=True)
class ContextProfile:
    """A named operating posture (pure data). ``toolset``: collapse target under ``focus``
    (``None`` keeps the platform default). ``compact_skill_categories``: DEMOTED to
    names-only under ``focus`` — deny-list, never hidden, so recall keeps working."""

    name: str
    toolset: Optional[str] = None
    guidance: str = ""
    model_hint: Optional[str] = None
    compact_skill_categories: tuple[str, ...] = ()


GENERAL_PROFILE = ContextProfile(name="general")


CODING_PROFILE = ContextProfile(
    name="coding", toolset=CODING_TOOLSET, guidance=CODING_AGENT_GUIDANCE, model_hint="coding",
    compact_skill_categories=_NON_CODING_SKILL_CATEGORIES,
)


def _git_root(cwd: Path) -> Optional[Path]:
    current = cwd.resolve()
    return next((p for p in (current, *current.parents) if (p / ".git").exists()), None)


def _home() -> Optional[Path]:
    try:
        return Path.home().resolve()
    except (OSError, RuntimeError):
        return None


def _marker_root(cwd: Path) -> Optional[Path]:
    """Nearest ancestor (≤6 levels) that looks like a project root, or ``None``. ``$HOME``
    and the shared temp root are skipped: a Makefile/AGENTS.md in the home dir is global
    config, and a stray manifest in /tmp must not flip every session under it."""
    current = cwd.resolve()
    try:
        temp_root = Path(tempfile.gettempdir()).resolve()
    except Exception:
        temp_root = None
    skip = (_home(), temp_root)
    for parent in (current, *current.parents)[:7]:
        if parent not in skip and any((parent / marker).exists() for marker in _PROJECT_MARKERS):
            return parent
    return None


def _has_code_files(root: Path) -> bool:
    """Bounded check for source files in the root and its immediate subdirs."""
    seen = 0
    stack = [(root, True)]
    while stack:
        directory, is_root = stack.pop()
        try:
            entries = os.scandir(directory)
        except OSError:
            continue
        with entries:
            for entry in entries:
                seen += 1
                if seen > _CODE_SCAN_MAX_ENTRIES:
                    return False
                try:
                    if entry.is_file():
                        if os.path.splitext(entry.name)[1].lower() in _CODE_EXTENSIONS:
                            return True
                    elif is_root and entry.is_dir() and entry.name not in _CODE_SCAN_SKIP_DIRS and not entry.name.startswith("."):
                        stack.append((Path(entry.path), False))
                except OSError:
                    continue
    return False


def _detect_profile(mode: str, platform: str, cwd: Path) -> ContextProfile:
    """``auto``/``focus``: coding when the surface is interactive AND the cwd is a code
    workspace (project root, or a git repo that actually holds code; a repo rooted at
    ``$HOME`` is NOT a signal). ``on``/``off`` force. Not memoized: one gateway serves many cwds."""
    if mode == "off":
        return GENERAL_PROFILE
    if mode == "on":
        return CODING_PROFILE
    if platform and platform.strip().lower() not in INTERACTIVE_CODING_PLATFORMS:
        return GENERAL_PROFILE
    if _marker_root(cwd) is not None:
        return CODING_PROFILE
    git_root = _git_root(cwd)
    if git_root is not None and git_root != _home() and _has_code_files(git_root):
        return CODING_PROFILE
    return GENERAL_PROFILE


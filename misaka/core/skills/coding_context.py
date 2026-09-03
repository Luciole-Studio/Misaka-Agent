"""The coding posture, as far as the skill index cares (hermes agent/coding_context.py).

Under the opt-in ``focus`` mode, a session sitting in a code workspace gets the
non-coding skill categories demoted to names-only in the index -- never hidden:
every name stays visible and loadable, only the descriptions are dropped.
``auto`` (the default) and ``on`` leave the index untouched; ``off`` disables
detection. The mode is ``coding_context`` in ``~/.misaka/core/skills.json``.
"""
import os
import tempfile
from pathlib import Path

from misaka.core.skills.layers import load_skills_config

# Project-root signals that mark a directory as a code workspace even when it
# isn't (yet) a git repo. Cheap filename checks -- no parsing.
_PROJECT_MARKERS = (
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "package.json", "tsconfig.json", "deno.json",
    "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "build.gradle.kts",
    "Gemfile", "composer.json", "mix.exs", "pubspec.yaml",
    "CMakeLists.txt", "Makefile", "Dockerfile",
    "AGENTS.md", "CLAUDE.md", ".cursorrules",
)

# Source-file extensions that make a git repo a *code* workspace even with no
# manifest: `git init` on a notes or research folder must not flip the posture.
_CODE_EXTENSIONS = frozenset({
    ".py", ".pyi", ".ipynb", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".kts", ".scala", ".rb", ".php", ".c", ".h",
    ".cc", ".cpp", ".hpp", ".cs", ".swift", ".m", ".mm", ".dart", ".ex", ".exs",
    ".lua", ".sh", ".bash", ".zsh", ".sql", ".vue", ".svelte", ".r", ".jl",
    ".hs", ".clj", ".erl", ".pl",
})

# Dirs never worth scanning for the code check (deps/build/vcs/venv noise).
_CODE_SCAN_SKIP_DIRS = frozenset({
    ".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build",
    "target", ".next", ".turbo", "vendor",
})
_CODE_SCAN_MAX_ENTRIES = 500   # a code workspace reveals itself in the first handful of entries

# Skill categories that are clearly not part of a coding workflow (deny-list: anything
# not listed here, custom categories included, keeps its full entry).
NON_CODING_SKILL_CATEGORIES = frozenset((
    "apple", "communication", "cooking", "creative", "email", "finance",
    "gaming", "gifs", "health", "media", "music", "note-taking",
    "productivity", "shopping", "smart-home", "social-media", "travel",
    "yuanbao",
))


def coding_mode():
    """The normalized ``coding_context`` mode: auto / focus / on / off."""
    raw = str(load_skills_config().get("coding_context", "auto")).strip().lower()
    if raw in {"focus", "strict", "lean"}:
        return "focus"
    if raw in {"on", "true", "yes", "1", "always"}:
        return "on"
    if raw in {"off", "false", "no", "0", "never"}:
        return "off"
    return "auto"


def _home():
    try:
        return Path.home().resolve()
    except (OSError, RuntimeError):
        return None


def _has_code_files(root):
    """Bounded check for source files in the repo's top two levels."""
    seen, stack = 0, [(Path(root), True)]
    while stack:
        directory, is_root = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    seen += 1
                    if seen > _CODE_SCAN_MAX_ENTRIES:
                        return False
                    try:
                        if entry.is_file():
                            if os.path.splitext(entry.name)[1].lower() in _CODE_EXTENSIONS:
                                return True
                        elif (is_root and entry.is_dir() and entry.name not in _CODE_SCAN_SKIP_DIRS
                              and not entry.name.startswith(".")):
                            stack.append((Path(entry.path), False))
                    except OSError:
                        continue
        except OSError:
            continue
    return False


def _git_root(cwd):
    current = Path(cwd).resolve()
    return next((p for p in (current, *current.parents) if (p / ".git").exists()), None)


def _marker_root(cwd):
    """Nearest ancestor (at most six levels up) carrying a project marker. ``$HOME`` and the
    temp root never count: a Makefile in the home directory is user config, not a project."""
    current, home = Path(cwd).resolve(), _home()
    try:
        temp_root = Path(tempfile.gettempdir()).resolve()
    except Exception:  # noqa: BLE001
        temp_root = None
    for depth, parent in enumerate((current, *current.parents)):
        if depth > 6:
            break
        if parent == home or parent == temp_root:
            continue
        if any((parent / marker).exists() for marker in _PROJECT_MARKERS):
            return parent
    return None


def is_code_workspace(cwd):
    """A recognised project root, or a git repo (not the dotfiles one at ``$HOME``) that
    actually holds code."""
    if _marker_root(cwd) is not None:
        return True
    git_root = _git_root(cwd)
    if git_root is None or git_root == _home():
        return False
    return _has_code_files(git_root)


def compact_skill_categories(cwd):
    """The categories the index demotes to names-only: the non-coding ones, only under the
    opt-in ``focus`` mode and only in a code workspace; empty otherwise."""
    if coding_mode() != "focus" or not is_code_workspace(cwd):
        return frozenset()
    return NON_CODING_SKILL_CATEGORIES

"""The one root of user data, and the table of everything that lives under it.

Every path MISAKA owns on the user's machine is declared once in ``LAYOUT`` and read through
``path(name)``. Nothing outside ``misaka/config`` spells the root, reads a layout environment
variable, or derives a project's ``.misaka`` directory on its own -- ``tests/test_home_governance.py``
holds that line. ``MISAKA_HOME`` is the only knob, which is what lets a test run (or a second
installation) live in a throwaway directory without touching the real one.

An entry's kind (``KINDS``) says who writes it and what may be done with it.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ENV_HOME = "MISAKA_HOME"
# The default home's name under $HOME, and the name of a project's own config directory.
DIR_NAME = ".misaka"
# Sub-agent type definitions, under this one name at every level: the home, a role, a project.
SUBAGENTS_DIR = "subagents"
# sockaddr_un.sun_path: 104 bytes on the BSDs and macOS, 108 on Linux, terminator included. On
# Windows the socket is a named pipe named after the path (utils/local_socket), and a pipe name
# is at most 256 characters, the pipe namespace prefix included.
_SOCKET_PATH_MAX = 103 if sys.platform == "darwin" else 247 if sys.platform == "win32" else 107


KINDS = {
    "edit": "your own configuration and role files",
    "secret": "credentials; owner-only",
    "state": "what the program writes and you would back up",
    "shared": "the one place inside the home an agent may create things of its own",
    "cache": "caches; rebuilt on demand",
    "logs": "logs",
    "run": "sockets and locks; meaningless once every process has exited",
}


@dataclass(frozen=True)
class Entry:
    rel: str
    kind: str
    mode: int | None = None


LAYOUT: dict[str, Entry] = {
    # What you edit sits shallow at the root. It is also what pi calls the agent directory:
    # its own joins (settings, models, keybindings, themes, prompts, extensions) land here.
    # Every other setting is a section of settings.json (allies, skills, moa, web), read through
    # SettingsManager; a role's settings.json holds what ROLE_KEYS lists. An extension reads its own
    # section the same way and keeps its files under plugins/<name>/: nothing here names one.
    "agent": Entry("", "edit", 0o700),
    "settings": Entry("settings.json", "edit"),
    "models": Entry("models.json", "edit"),
    "keybindings": Entry("keybindings.json", "edit"),
    # A role's MCP servers (`mcpServers`), in her role directory only (`profiles.mcp_dir`): the home keeps
    # none. A trusted project may add its own.
    "mcp_config": Entry("mcp.json", "edit"),
    # The shared layer: what every role sees. A role directory overlays it, first match winning.
    "shared_soul": Entry("MISAKA.md", "edit"),
    "shared_skills": Entry("skills", "edit"),
    "skill_bundles": Entry("skill-bundles", "edit"),
    "themes": Entry("themes", "edit"),
    "prompts": Entry("prompts", "edit"),
    "extensions": Entry("extensions", "edit"),
    "subagents": Entry(SUBAGENTS_DIR, "edit"),
    "roles_root": Entry("profiles", "edit"),
    "profiles_root": Entry("profiles/sisters", "edit"),
    # Environment for code that is not MISAKA (a plugin's knobs, an SDK's variables, the keys a
    # skill's script expects); a role may overlay it. Loaded by ``config.env`` at process start.
    # The user edits it, so it sits at the root like settings.json -- but owner-only, as it holds keys.
    "env": Entry(".env", "edit", 0o600),
    "credentials": Entry("credentials", "secret", 0o700),
    "auth": Entry("credentials/auth.json", "secret", 0o600),
    "vault": Entry("credentials/vault", "secret", 0o700),
    # A role's MCP sign-ins, beside her `mcp.json` in her role directory; the home keeps none.
    "mcp_auth": Entry("credentials/mcp-auth.json", "secret", 0o600),
    "db": Entry("state/board.db", "state"),
    "messages_db": Entry("state/messages.db", "state"),
    # The panel's spaces by durable id -> folder and name, for every space a session belongs to.
    "spaces": Entry("state/spaces.json", "state"),
    "trust": Entry("state/trust.json", "state"),
    "models_store": Entry("state/models-store.json", "state"),
    "sessions": Entry("state/sessions", "state"),
    "tasks_root": Entry("state/tasks", "state"),
    "input_history": Entry("state/input-history", "state"),
    "office_intent": Entry("state/office-intent", "state"),
    "agent_memory": Entry("state/agent-memory", "state"),
    "worktrees": Entry("state/worktrees", "state"),
    "plugins": Entry("state/plugins", "state"),
    "skills_state": Entry("state/skills", "state"),
    "skills_pending": Entry("state/skills-pending", "state"),
    "web_evidence": Entry("state/web-evidence", "state"),
    # What `misaka update` copies before it changes anything (``cli.backup``); owner-only when made.
    "backups": Entry("state/backups", "state"),
    "shared": Entry("shared", "shared"),
    "bin": Entry("cache/bin", "cache"),
    "engine_cache": Entry("cache/engine", "cache"),
    "web_cache": Entry("cache/web", "cache"),
    "web_tools": Entry("cache/web-tools", "cache"),
    "office_cache": Entry("cache/office", "cache"),
    "vision_cache": Entry("cache/vision", "cache"),
    "skill_blobs": Entry("cache/skill-blobs", "cache"),
    "skills_index": Entry("cache/skills", "cache"),
    "dm_protocol": Entry("cache/dm-protocol.md", "cache"),
    "log": Entry("logs/misaka.log", "logs"),
    "debug_log": Entry("logs/misaka-debug.log", "logs"),
    "crash_log": Entry("logs/misaka-crash.log", "logs"),
    "warnings_log": Entry("logs/misaka-warnings.log", "logs"),
    "panel_crash_log": Entry("logs/panel-crash.log", "logs"),
    "stall_logs": Entry("logs/stalls", "logs"),
    # Messages MCP servers send with `notifications/message`, one file for every process (the MCP extension).
    "mcp_log": Entry("logs/mcp/mcp.log", "logs"),
    "web_logs": Entry("logs/web", "logs"),
    "moa_traces": Entry("logs/moa-traces", "logs"),
    "net_sock": Entry("run/net.sock", "run"),
    "net_snapshot": Entry("run/net.json", "run"),
    "locks": Entry("run/locks", "run"),
    "skills_lock": Entry("run/skills-write.lock", "run"),
}


def home() -> Path:
    """The root, read at call time so a process (or a test) can be pointed elsewhere.

    Canonical (symlinks resolved), so every path derived from it compares equal to the
    ``realpath`` of the same file -- which is what the workspace and trust checks compare against.
    """
    override = os.environ.get(ENV_HOME, "").strip()
    return Path(os.path.realpath(Path(override).expanduser() if override else Path.home() / DIR_NAME))


def path(name: str, role_dir: str | os.PathLike[str] | None = None) -> Path:
    """Where ``name`` lives -- in the home, or in ``role_dir``, which lays out whatever it keeps
    of its own the same way: a role directory is a partial overlay of the home.

    A socket whose place in the home is too long a path for the kernel lives in a short private
    directory instead, named after the user and the home, so a daemon and its clients still
    agree on it without being told.
    """
    if role_dir:
        return Path(role_dir) / LAYOUT[name].rel
    target = home() / LAYOUT[name].rel
    if target.suffix == ".sock" and len(os.fsencode(target)) > _SOCKET_PATH_MAX:
        tag = hashlib.sha256(os.fsencode(home())).hexdigest()[:8]
        if sys.platform == "win32":        # the temp directory is already this user's alone
            return Path(tempfile.gettempdir()) / f"misaka-{tag}" / target.name
        return Path("/tmp") / f"misaka-{os.geteuid()}-{tag}" / target.name  # windows-footgun: ok - POSIX branch
    return target


def ensure() -> None:
    """Hold the home to the modes the table asks for. Idempotent; called at process start.

    Owner-only directories are created; an owner-only file is only ever narrowed, never made.
    """
    for entry in LAYOUT.values():
        target = home() / entry.rel
        if entry.mode == 0o700:
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
        if entry.mode is not None and target.exists() and target.stat().st_mode & 0o777 & ~entry.mode:
            target.chmod(entry.mode)


def private_dir(directory: str | os.PathLike[str]) -> None:
    """Make ``directory`` exist as ours alone, or refuse it.

    A socket is a code-execution door, and one outside the home sits in a directory anyone
    may create first: bind or connect only once it is a real directory, owned by this user,
    closed to everyone else.
    """
    os.makedirs(directory, mode=0o700, exist_ok=True)
    info = os.lstat(directory)
    if sys.platform == "win32":
        # Windows has no owner or mode bits to check here: the directory sits in the user's
        # profile, and the pipe behind the socket path carries an owner-only DACL of its own
        # (herdr ipc.rs: restrict_socket_permissions is a no-op there).
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(f"{directory} is not a directory; refusing to use it.")
        return
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():  # windows-footgun: ok - POSIX branch
        raise RuntimeError(f"{directory} is not a directory owned by this user; refusing to use it.")
    if info.st_mode & 0o077:
        os.chmod(directory, 0o700)


def display(target: str | os.PathLike[str] | None = None) -> str:
    """``target`` (default: the home) the way a user would type it, for messages and help text."""
    shown = Path(target) if target is not None else home()
    try:
        return "~/" + shown.relative_to(Path.home()).as_posix()
    except (ValueError, RuntimeError):
        return str(shown)


def project_dir(directory: str | os.PathLike[str]) -> Path | None:
    """``directory``'s own config directory, or ``None`` when that would be the home itself.

    The home is never a project: run from ``$HOME``, ``<cwd>/.misaka`` *is* the home, and
    treating it as project scope would load the global settings twice and ask the user to
    trust their own configuration. Hermes draws the same line (``find_project_root`` returns
    ``None`` at the home; a candidate that resolves to the home's own directory is dropped).
    """
    candidate = Path(directory) / DIR_NAME
    return None if Path(os.path.realpath(candidate)) == home() else candidate


def strays() -> list[str]:
    """Top-level names in the home that no row of the table accounts for."""
    known = {entry.rel.split("/", 1)[0] for entry in LAYOUT.values()} | {".DS_Store"}   # Finder's, not ours
    sidecars = (".lock", "-wal", "-shm", "-journal")
    try:
        names = os.listdir(home())
    except OSError:
        return []
    return sorted(name for name in names if name not in known
                  and not any(name.endswith(tail) and name[:-len(tail)] in known for tail in sidecars))


# The file systems MISAKA's users run fold case (APFS, NTFS): ``~/.MISAKA/Credentials`` is
# ``~/.misaka/credentials`` there, and a check that compared names exactly let the variant through
# (0.18.9 sweep). Linux's do not, and there a variant is another file.
FOLDS_CASE = sys.platform in ("darwin", "win32")
FOLDS_UNICODE = sys.platform == "darwin"     # APFS: one name for its NFC and NFD spellings; NTFS: two


def fold(text: str) -> str:
    """``text`` as this platform's file system compares names: APFS also takes a name's composed
    and decomposed Unicode spellings (NFC, NFD) for one, NTFS does not."""
    if FOLDS_UNICODE:
        import unicodedata
        text = unicodedata.normalize("NFC", text)
    return text.casefold() if FOLDS_CASE else text


def folded(target: str | os.PathLike[str]) -> Path:
    """``target`` resolved and folded as the file system folds it, for a refusal to compare."""
    return Path(fold(os.path.realpath(Path(target).expanduser())))


def agent_may_write(target: str | os.PathLike[str], granted: tuple[str | None, ...] = ()) -> bool:
    """Whether an agent's file tools may create or change ``target``.

    Outside the home this has no opinion. Inside it an agent owns ``shared`` and the directories
    its session was handed (its workspace, a card's output directory, a sub-agent's memory) --
    when those lie inside the home. A workspace that merely *contains* the home, such as the
    user's home directory, grants nothing in it: the rest belongs to the program and the user.
    """
    resolved = folded(target)
    root = folded(home())
    if resolved != root and not resolved.is_relative_to(root):
        return True
    allowed = [folded(path("shared"))]
    for directory in granted:
        if directory:
            candidate = folded(directory)
            if candidate != root and candidate.is_relative_to(root):
                allowed.append(candidate)
    return any(resolved == directory or resolved.is_relative_to(directory) for directory in allowed)


def stored(target: str | os.PathLike[str]) -> str:
    """How a pointer into the home is written to a database or a state file: home-relative.

    A path outside the home (a user's project) stays absolute; it has no other name.
    """
    resolved = Path(os.path.realpath(Path(target).expanduser()))
    try:
        return resolved.relative_to(home()).as_posix()
    except ValueError:
        return str(resolved)


def from_stored(value: str) -> Path:
    candidate = Path(value)
    return candidate if candidate.is_absolute() else home() / candidate


__all__ = ["DIR_NAME", "ENV_HOME", "KINDS", "LAYOUT", "SUBAGENTS_DIR", "Entry", "agent_may_write", "display", "ensure", "from_stored", "home", "path",
           "private_dir", "project_dir", "stored", "strays"]

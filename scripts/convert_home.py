"""One-off: move a pre-governance ``~/.misaka`` into the layout of ``misaka/config/home.py``.

Not part of the package and not run by it -- MISAKA carries no compatibility code for the old
layout. Run it once, by hand, with every MISAKA process stopped:

    python scripts/convert_home.py            # show what would happen
    python scripts/convert_home.py --apply    # back up, then do it

What it does: copies the home to ``<home>.bak-<timestamp>`` first; renames each old entry to
its place in the table; rewrites the board's session pointers from absolute paths to
home-relative ones; files everything the table cannot account for (what agents created there
with a shell: ``skill-library``, ``audits``, ...) under ``shared/``; and folds the separate
configuration files into ``settings.json`` -- ``allies.json``, ``skills.json``, ``moa.json`` and
the settings half of ``web.json`` into the home's, a role's ``config.json`` (its model pin),
``config.yaml`` (its MCP servers) and ``web.json`` into the role's own. Vendor credentials
go to ``.env`` -- the home's, or the role's own -- as does a role's ``.skill-secrets.json``
(a home converted before ``.env`` existed has them in ``credentials/web.json``; those are
folded the same way). Then it follows the move into
what agents left behind: symlinks (in the home and in ``~/.local/bin``) that name a moved
directory are repointed, and absolute old-home paths inside every text file of the agent-
written trees (``shared/``, the profiles, the shared skills) are rewritten -- ``sys.path``
insertions, venv shebangs, sandbox profiles, usage ledgers keyed by path -- when the path
has a new place; a path that was already dead is left alone. What it cannot do: session
transcripts and card contracts are history and are left byte-identical, so absolute paths the
model wrote or read in them still name the old places. Finish or drop unfinished research
runs before converting.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from misaka.config import home

# old path (relative to the home) -> name in the layout table, or (name, path under it) for what
# a plugin keeps in its own directory (the table names the plugins dir, never a plugin's files)
MOVES = {
    # First: the old skill ledger sat at <home>/skills, where the shared skills now live.
    "skills": "skills_state",
    "agent/settings.json": "settings", "agent/models.json": "models", "agent/keybindings.json": "keybindings",
    "agent/themes": "themes", "agent/prompts": "prompts", "agent/extensions": "extensions",
    "agent/agents": "subagents", "agent/auth.json": "auth", "agent/trust.json": "trust",
    "agent/models-store.json": "models_store", "agent/plugins": "plugins", "agent/bin": "bin",
    "agent/cache": "engine_cache", "agent/mcp": "mcp_logs", "agent/misaka.log": "log",
    "agent/misaka-debug.log": "debug_log", "agent/misaka-crash.log": "crash_log",
    "agent/misaka-warnings.log": "warnings_log",
    "profiles/MISAKA.md": "shared_soul", "profiles/skills": "shared_skills",
    "profiles/skill-bundles": "skill_bundles",
    "board.db": "db", "messages.db": "messages_db", "sessions": "sessions",
    "tasks": "tasks_root", "input-history": "input_history", "office_intent": "office_intent",
    "agent-memory": "agent_memory", "worktrees": "worktrees", "pending/skills": "skills_pending",
    "lcm": ("plugins", "misaka-lcm/lcm"), "lcm.gate": ("plugins", "misaka-lcm/lcm.gate"),
    "lcm.activity.sqlite": ("plugins", "misaka-lcm/lcm.activity.sqlite"),
    "cache/skill_blobs": "skill_blobs", "cache/mcp_schema_cache.json": "mcp_schema_cache",
    "moa-traces": "moa_traces", "panel-crash.log": "panel_crash_log", "locks": "locks",
}
# Configuration files the merge phase folds into settings.json / .env: left where they are
# by the relayout, consumed afterwards.
FOLDED = ("allies.json", "skills.json", "moa.json", "web.json")
# Rebuilt or meaningless once nothing is running: removed rather than moved.
DISPOSABLE = ("net.sock", "net.sock.lock", "net.sock.log", "net.json", "dm-protocol.md", ".skills-write.lock",
              "agent/auth.json.lock", "agent/settings.json.lock", "agent/models-store.json.lock", "web.json.lock",
              "board.db-wal", "board.db-shm", "messages.db-wal", "messages.db-shm", ".DS_Store", "agent/.DS_Store")
POINTER_COLUMNS = (("tasks", "session_file"), ("tasks", "session_dir"), ("task_runs", "session_file"),
                   ("research_actions", "session_file"), ("research_branches", "session_file"),
                   ("research_runs", "root_session"))


def running() -> list[str]:
    """Other MISAKA processes. A daemon or a session holds the board, the socket and auth.json."""
    listing = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, check=False).stdout
    mine = str(os.getpid())
    return [line.strip() for line in listing.splitlines()
            if ("misaka" in line and ("-m misaka" in line or "/bin/misaka" in line or "misaka.ui.panel.daemon" in line))
            and line.split(None, 1)[0] != mine and "convert_home" not in line]


def _new_rel(target) -> str:
    """A MOVES target as a path relative to the home."""
    if isinstance(target, tuple):
        name, sub = target
        return f"{home.LAYOUT[name].rel}/{sub}"
    return home.LAYOUT[target].rel


def plan(root: Path):
    moves, strays = [], []
    for old, target in MOVES.items():
        source = root / old
        if source.exists():
            moves.append((source, root / _new_rel(target)))
    known = {entry.rel.split("/", 1)[0] for entry in home.LAYOUT.values()} | {"agent", "pending"}
    claimed = {source.relative_to(root).parts[0] for source, _ in moves}
    shared = root / home.LAYOUT["shared"].rel
    for entry in sorted(root.iterdir()):
        if entry.name == "state":
            # The old layout had no state/ of its own: whatever is in one, an agent put there.
            strays += [(child, shared / child.name) for child in sorted(entry.iterdir())]
        elif entry.name not in known and entry.name not in claimed and entry.name not in DISPOSABLE and entry.name not in FOLDED:
            strays.append((entry, shared / entry.name))
    return moves, strays


def checkpoint(database: Path) -> None:
    """Fold the write-ahead log into the database so the file alone is the whole board."""
    if database.exists():
        con = sqlite3.connect(database)
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            con.close()


def relativise(database: Path, old_root: Path) -> int:
    """Absolute pointers under the old home -> home-relative pointers in the new layout."""
    renamed = {old: _new_rel(target) for old, target in MOVES.items()}
    changed = 0
    con = sqlite3.connect(database)
    try:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, column in POINTER_COLUMNS:
            if table not in tables:
                continue
            for rowid, value in con.execute(f'SELECT rowid, "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL').fetchall():
                path = Path(value)
                if not path.is_absolute() or not path.is_relative_to(old_root):
                    continue
                relative = path.relative_to(old_root)
                top = relative.parts[0]
                target = Path(renamed.get(top, top), *relative.parts[1:]).as_posix()
                con.execute(f'UPDATE "{table}" SET "{column}"=? WHERE rowid=?', (target, rowid))
                changed += 1
        con.commit()
    finally:
        con.close()
    return changed


def _moved(root: Path, target: str) -> Path | None:
    """Where an absolute path under the old home now lives, or None when it has no new place."""
    if not target.startswith(str(root) + os.sep):
        return None
    rel = Path(target[len(str(root)) + 1:])
    # The longest old prefix in the move table wins (``profiles/skills`` before ``profiles``).
    for old in sorted(MOVES, key=len, reverse=True):
        old_parts = Path(old).parts
        if rel.parts[:len(old_parts)] == old_parts:
            return root / _new_rel(MOVES[old]) / Path(*rel.parts[len(old_parts):])
    known = {entry.rel.split("/", 1)[0] for entry in home.LAYOUT.values()}
    if rel.parts[0] not in known:                          # an agent-made directory: filed under shared/
        return root / home.LAYOUT["shared"].rel / rel
    return None


USER_BIN = Path.home() / ".local" / "bin"     # where skills link the CLIs they install into the library


def relink(root: Path, *, apply: bool) -> tuple[int, int]:
    """Repoint every symlink under the home (and in the user's bin) whose target sat in the old layout.

    Renaming a directory moves the links in it but not what they say: a skill kept as a
    link into ``skill-library`` (now ``shared/skill-library``) dangled after the move, and
    so did the ``~/.local/bin`` commands the skills linked to the library's runtimes.
    Returns ``(repointed, still dangling)``.
    """
    repointed = dangling = 0
    outside = USER_BIN.iterdir() if USER_BIN.is_dir() else ()
    for link in (*root.rglob("*"), *outside):
        if not link.is_symlink() or link.exists():
            continue
        target = os.readlink(link)
        new = _moved(root, target) if os.path.isabs(target) else None
        if new is not None and new.exists():
            if apply:
                parent = link.parent
                mode = parent.stat().st_mode & 0o777
                if not os.access(parent, os.W_OK):      # a read-only library snapshot: open it for the swap
                    parent.chmod(mode | 0o200)
                try:
                    link.unlink()
                    link.symlink_to(new)
                finally:
                    if not mode & 0o200:
                        parent.chmod(mode)
            repointed += 1
        else:
            dangling += 1
    return repointed, dangling


_HOME_SPELLINGS = ("~/.misaka", "$HOME/.misaka", "${HOME}/.misaka")


def _text(path: Path) -> str | None:
    """The file's text when it is text (git's rule: no NUL byte, decodes as UTF-8), else None."""
    try:
        if path.stat().st_size > 4_000_000:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def rewrite_paths(root: Path, *, apply: bool) -> tuple[int, int]:
    """Rewrite absolute old-home paths inside the text files agents left in the home.

    The agents wrote their skills with the home's absolute paths in them -- ``sys.path``
    insertions, manifests, sandbox profiles, usage ledgers keyed by path, prose telling the
    model where a file is -- so a moved directory breaks them the way it broke the symlinks.
    Every text file under the agent-written trees is covered, whatever its suffix; only paths
    that now exist somewhere else are rewritten, a path that was already dead stays as it
    was. Returns ``(files, replacements)``.
    """
    import re

    pattern = re.compile(r"(" + "|".join(re.escape(str(root)) if i == 0 else re.escape(spelling)
                                            for i, spelling in enumerate(("", *_HOME_SPELLINGS)))
                         + r")(/[A-Za-z0-9_./@-]*)")
    files = replacements = 0
    for base in (root / home.LAYOUT["shared"].rel, root / home.LAYOUT["roles_root"].rel, root / home.LAYOUT["shared_skills"].rel):
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            text = _text(path)
            if text is None:
                continue
            count = 0

            def swap(match):
                nonlocal count
                spelling, tail = match.group(1), match.group(2)
                # Prose writes `<dir>/...` for "and so on"; the dots are not a path component.
                tail, ellipsis = (tail[:-4], tail[-4:]) if tail.endswith("/...") else (tail, "")
                new = _moved(root, str(root) + tail)
                if new is None or not new.exists():
                    return match.group(0)
                count += 1
                return spelling + "/" + new.relative_to(root).as_posix() + ellipsis

            rewritten = pattern.sub(swap, text)
            if not count:
                continue
            files += 1
            replacements += count
            if apply:
                mode = path.stat().st_mode & 0o777
                parent_mode = path.parent.stat().st_mode & 0o777
                if not parent_mode & 0o200:
                    path.parent.chmod(parent_mode | 0o200)
                if not mode & 0o200:
                    path.chmod(mode | 0o200)
                try:
                    path.write_text(rewritten, encoding="utf-8")
                finally:
                    if not mode & 0o200:
                        path.chmod(mode)
                    if not parent_mode & 0o200:
                        path.parent.chmod(parent_mode)
    return files, replacements


def seeded_readmes(root: Path) -> dict[Path, str]:
    """The READMEs the package writes into the home, with their current text.

    They describe the layout, so the ones an older MISAKA seeded describe the old one
    (``config.yaml`` for MCP servers, ``agent/`` for credentials). They are the program's
    text, not the user's: the converter rewrites them.
    """
    from misaka.config import layout

    return {
        root / home.LAYOUT["roles_root"].rel / "README.md": layout.README,
        root / home.LAYOUT["shared_skills"].rel / "README.md": layout.SKILLS_README.format(who="every role"),
        root / home.LAYOUT["shared"].rel / "README.md": layout.SHARED_README,
        root / home.LAYOUT["roles_root"].rel / "last_order" / "skills" / "README.md":
            layout.SKILLS_README.format(who="Last Order only"),
    }


def refresh_readmes(root: Path, *, apply: bool) -> list[Path]:
    """Rewrite every seeded README whose text is not the current one; returns the ones touched."""
    touched = []
    for path, text in seeded_readmes(root).items():
        if not path.parent.is_dir():
            continue
        try:
            if path.read_text(encoding="utf-8") == text:
                continue
        except FileNotFoundError:
            pass
        touched.append(path)
        if apply:
            path.write_text(text, encoding="utf-8")
    return touched


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _merge_into_settings(settings_path: Path, key: str, value) -> None:
    """Set ``key`` in a settings.json, creating the file; an existing key is left alone."""
    document = _read_json(settings_path)
    if key in document:
        print(f"  ! {settings_path} already has {key!r}; the old file's value was not copied")
        return
    document[key] = value
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _split_web(document: dict) -> tuple[dict, dict]:
    """A web.json document -> (the settings half, the credentials half)."""
    from misaka.core.web.config import is_credential_var

    settings = dict(document)
    env = settings.pop("env", None) if isinstance(document.get("env"), dict) else None
    credentials = {}
    if env:
        credentials = {name: value for name, value in env.items() if is_credential_var(name)}
        rest = {name: value for name, value in env.items() if not is_credential_var(name)}
        if rest:
            settings["env"] = rest
    return settings, credentials


def plan_merges(root: Path) -> list[tuple[str, str]]:
    """(source, destination) descriptions for the configuration files that fold into settings.json."""
    merges = []
    for name, key in (("allies.json", "allies"), ("skills.json", "skills"), ("moa.json", "moa")):
        if (root / name).exists():
            merges.append((name, f'settings.json "{key}"'))
    if (root / "web.json").exists():
        merges.append(("web.json", 'settings.json "web" + .env (keys only)'))
    for role in _role_dirs(root):
        rel = role.relative_to(root)
        if (role / "config.json").exists():
            merges.append((f"{rel}/config.json", f"{rel}/settings.json defaultProvider/defaultModel"))
        if (role / "config.yaml").exists():
            merges.append((f"{rel}/config.yaml", f'{rel}/settings.json "mcpServers"'))
        if (role / "web.json").exists():
            merges.append((f"{rel}/web.json", f'{rel}/settings.json "web" + {rel}/.env'))
    return merges


def plan_env_folds(root: Path) -> list[tuple[Path, Path]]:
    """(source, target .env) for the credential stores that predate ``.env``: the vendor keys
    a home or a role kept in ``credentials/web.json`` and a role's ``.skill-secrets.json``."""
    folds = []
    if (root / "credentials" / "web.json").exists():
        folds.append((root / "credentials" / "web.json", root / ".env"))
    for role in _role_dirs(root):
        for name in ("credentials/web.json", ".skill-secrets.json"):
            if (role / name).exists():
                folds.append((role / name, role / ".env"))
    return folds


def apply_env_folds(root: Path) -> None:
    from misaka.config import env as env_file

    for source, target in plan_env_folds(root):
        role = None if target == root / ".env" else target.parent
        data = _read_json(source)
        values = data.get("env") if source.name == "web.json" else data
        values = {name: value for name, value in (values or {}).items() if isinstance(value, str) and value}
        if values:
            env_file.write(values, role)
        source.unlink()
        (source.parent / (source.name + ".lock")).unlink(missing_ok=True)
        if source.name == ".skill-secrets.json":
            (source.parent / ".skill-secrets.lock").unlink(missing_ok=True)
        if source.parent.name == "credentials" and role is not None and not any(source.parent.iterdir()):
            source.parent.rmdir()          # a role's now-empty credentials/ dir


def _role_dirs(root: Path) -> list[Path]:
    roles = root / home.LAYOUT["roles_root"].rel
    found = [roles / "last_order"] if (roles / "last_order").is_dir() else []
    sisters = root / home.LAYOUT["profiles_root"].rel
    if sisters.is_dir():
        found += sorted(child for child in sisters.iterdir() if child.is_dir())
    return found


def apply_merges(root: Path) -> None:
    import yaml

    settings = root / home.LAYOUT["settings"].rel
    allies = root / "allies.json"
    if allies.exists():
        commands = _read_json(allies).get("commands")
        if isinstance(commands, list):
            _merge_into_settings(settings, "allies", commands)
        allies.unlink()
    for name, key in (("skills.json", "skills"), ("moa.json", "moa")):
        source = root / name
        if source.exists():
            _merge_into_settings(settings, key, _read_json(source))
            source.unlink()
    layers = [(root / "web.json", settings, None)]
    for role in _role_dirs(root):
        role_settings = role / "settings.json"
        pin = role / "config.json"
        if pin.exists():
            reference = str(_read_json(pin).get("model") or "")
            provider, slash, model = reference.partition("/")
            if slash and provider and model:
                _merge_into_settings(role_settings, "defaultProvider", provider)
                _merge_into_settings(role_settings, "defaultModel", model)
            elif reference and reference != "inherit":
                print(f"  ! {pin}: {reference!r} is not provider/model; not carried over")
            pin.unlink()
            (role / "config.json.lock").unlink(missing_ok=True)
        servers = role / "config.yaml"
        if servers.exists():
            try:
                loaded = yaml.safe_load(servers.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                loaded = {}
            if isinstance(loaded, dict) and isinstance(loaded.get("mcp_servers"), dict) and loaded["mcp_servers"]:
                _merge_into_settings(role_settings, "mcpServers", loaded["mcp_servers"])
            servers.unlink()
        layers.append((role / "web.json", role_settings, role))
    for source, target, role in layers:
        if not source.exists():
            continue
        web_settings, credentials = _split_web(_read_json(source))
        if web_settings:
            _merge_into_settings(target, "web", web_settings)
        if credentials:
            from misaka.config import env as env_file
            env_file.write(credentials, role)
        source.unlink()
        (source.parent / (source.name + ".lock")).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="make the changes (default: only show them)")
    args = parser.parse_args()

    root = home.home()
    if not root.is_dir():
        print(f"{root} does not exist; nothing to convert.")
        return 0
    relayout = (root / "agent").is_dir() or (root / "board.db").exists()
    merges = plan_merges(root)
    links, _ = relink(root, apply=False)
    texts, _ = rewrite_paths(root, apply=False)
    readmes = refresh_readmes(root, apply=False)
    env_folds = plan_env_folds(root)
    if not relayout and not merges and not links and not texts and not readmes and not env_folds:
        print(f"{root} is already in the new layout.")
        return 0
    alive = running()
    if alive:
        print("MISAKA is still running; stop it first (`misaka net stop`, close every session):")
        print("\n".join(f"  {line}" for line in alive))
        return 2

    moves, strays = plan(root) if relayout else ([], [])
    for label, pairs in (("move", moves), ("to shared/ (nothing in the code owns these)", strays)):
        if pairs:
            print(f"\n{label}:")
            for source, target in pairs:
                print(f"  {source.relative_to(root)}  ->  {target.relative_to(root)}")
    gone = [name for name in DISPOSABLE if (root / name).exists()] if relayout else []
    if gone:
        print("\nremove (rebuilt on demand):\n  " + "\n  ".join(gone))
    if env_folds:
        print("\nfold into .env (credential stores that predate it):")
        for source, target in env_folds:
            print(f"  {source.relative_to(root)}  ->  {target.relative_to(root)}")
    if merges:
        print("\nfold into settings.json:")
        for source, destination in merges:
            print(f"  {source}  ->  {destination}")
    if links:
        print(f"\nrepoint {links} symlink(s) that still name the old layout")
    if texts:
        print(f"\nrewrite old absolute paths inside {texts} text file(s) agents left in the home")
    if readmes:
        print("\nrewrite the layout READMEs the package seeded: " + ", ".join(str(p.relative_to(root)) for p in readmes))
    if not args.apply:
        print("\nNothing was changed. Re-run with --apply.")
        return 0

    backup = root.with_name(f"{root.name}.bak-{time.strftime('%Y%m%dT%H%M%S')}")
    for name in ("board.db", "messages.db"):
        checkpoint(root / name)
    print(f"\nbacking up to {backup} ...")
    if sys.platform == "darwin":        # an APFS clone: instant, and costs no space until something changes
        subprocess.run(["cp", "-cR", str(root), str(backup)], check=True)
    else:
        shutil.copytree(root, backup, symlinks=True)

    for name in gone:
        (root / name).unlink(missing_ok=True)
    for source, target in (*moves, *strays):
        if target.exists():
            print(f"  ! {target.relative_to(root)} already exists; left {source.relative_to(root)} where it is")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
    ledger = root / home.LAYOUT["skills_state"].rel / ".ledger-v2.jsonl"   # same entries, current name
    if ledger.exists() and not ledger.with_name(".ledger.jsonl").exists():
        ledger.rename(ledger.with_name(".ledger.jsonl"))
    for leftover in ("agent", "pending"):
        try:
            (root / leftover).rmdir()
        except OSError:
            pass
    board = root / home.LAYOUT["db"].rel
    if relayout and board.exists():
        print(f"board pointers made home-relative: {relativise(board, root)}")
    if plan_merges(root):
        apply_merges(root)
        print("configuration folded into settings.json")
    if plan_env_folds(root):
        apply_env_folds(root)
        print("credential stores folded into .env")
    repointed, dangling = relink(root, apply=True)
    if repointed or dangling:
        print(f"symlinks repointed: {repointed}; still dangling (target gone before the move): {dangling}")
    files, replacements = rewrite_paths(root, apply=True)
    if files:
        print(f"old absolute paths rewritten: {replacements} in {files} file(s)")
    for path in refresh_readmes(root, apply=True):
        print(f"README rewritten: {path.relative_to(root)}")
    home.ensure()
    left = sorted(entry.name for entry in (root / "agent").iterdir()) if (root / "agent").is_dir() else []
    if left:
        print(f"  ! agent/ still holds: {', '.join(left)} -- nothing reads it any more; file these by hand")
    print(f"done. The old tree is intact at {backup}; delete it once you are satisfied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

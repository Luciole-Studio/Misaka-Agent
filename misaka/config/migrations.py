"""One-time migrations of the agent directory, run by ``misaka init --migrate`` (never at startup)."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

from misaka.config import CONFIG_DIR_NAME, get_agent_dir, get_bin_dir
from misaka.core.keybindings import migrateKeybindingsConfig
from misaka.core.session_manager import get_default_session_dir
from misaka.utils import atomic

_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_DIM = "\x1b[2m"
_RESET = "\x1b[0m"


def migrate_auth_to_auth_json() -> list[str]:
    """oauth.json and settings.apiKeys into auth.json, one provider at a time: a provider the store
    already holds is left as it is, so a run that failed halfway simply continues; the old sources
    go only once every provider *the source listed* is in -- an entry that could not even be parsed
    keeps oauth.json where it is. A failed write raises and leaves them in place."""
    agent_dir = Path(get_agent_dir())
    auth_path = agent_dir / "auth.json"
    oauth_path = agent_dir / "oauth.json"
    settings_path = agent_dir / "settings.json"

    # Read every old source first; nothing is renamed or rewritten until the new store holds it.
    migrated: dict[str, object] = {}
    oauth_incomplete = False
    settings: dict | None = None
    if oauth_path.exists():
        try:
            entries = json.loads(oauth_path.read_text(encoding="utf-8-sig")).items()
        except Exception:  # noqa: BLE001 - an unreadable oauth.json is left in place
            entries = ()
            oauth_incomplete = True
        # One entry at a time: a hand-edited record that is not a mapping used to raise inside
        # the loop, and the `except` around the loop swallowed it *after* the loop had already
        # stopped -- every provider listed after the bad one silently failed to migrate while
        # oauth.json was renamed away regardless. A bad entry is now skipped, and the flag
        # below keeps the source file in place so nothing is left unreadable.
        for provider, credential in entries:
            try:
                migrated[str(provider)] = {"type": "oauth", **credential}
            except Exception:  # noqa: BLE001 - a credential that is not a mapping is left behind
                oauth_incomplete = True
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
            api_keys = settings.get("apiKeys") if isinstance(settings, dict) else None
            for provider, key in (api_keys or {}).items():
                if provider not in migrated and isinstance(key, str):
                    migrated[str(provider)] = {"type": "api_key", "key": key}
        except Exception:  # noqa: BLE001
            settings = None
    if not migrated:
        return []

    from misaka.core.auth_storage import AuthStorage
    storage = AuthStorage.create(str(auth_path))
    if storage.loadError is not None:
        return []                                              # an unreadable store is never overwritten
    for provider, credential in migrated.items():
        if not storage.has(provider):
            storage.set(provider, credential)
    storage.reload()
    if not all(storage.has(p) for p in migrated):
        return []                                              # the store did not take it: old sources stay untouched

    if oauth_path.exists():
        if oauth_incomplete:
            print(
                f"{_YELLOW}Warning: {oauth_path} has entries this migration could not read; "
                f"it is left in place so they are not lost.{_RESET}"
            )
        else:
            oauth_path.rename(oauth_path.with_suffix(".json.migrated"))
    if settings is not None and isinstance(settings, dict) and "apiKeys" in settings:
        settings.pop("apiKeys", None)
        atomic.write_text(settings_path, json.dumps(settings, indent=2))
    return list(migrated)


def migrate_sessions_from_agent_root() -> None:
    agent_dir = Path(get_agent_dir())
    try:
        files = [path for path in agent_dir.iterdir() if path.is_file() and path.suffix == ".jsonl"]
    except OSError:
        return

    for session_file in files:
        try:
            first_line = session_file.read_text(encoding="utf-8-sig").splitlines()[0]
            header = json.loads(first_line)
            if header.get("type") != "session" or not isinstance(header.get("cwd"), str):
                continue
            target_dir = Path(get_default_session_dir(header["cwd"], str(agent_dir)))
            target_dir.mkdir(parents=True, exist_ok=True)
            target_path = target_dir / session_file.name
            if target_path.exists():
                continue
            session_file.rename(target_path)
        except (OSError, ValueError, IndexError):
            continue


def migrate_legacy_session_buckets() -> int:
    """Move sessions out of the pre-canonical ``--path-with-dashes--`` buckets.

    The bucket name became a slug plus a hash of the canonical path (two different projects
    could collide before that). This used to run inside every session-directory lookup --
    twice per ``misaka chat`` launch, and once per panel session-list refresh.
    """
    from misaka.core.session_manager import (
        _canonical_cwd,
        _legacy_encode_cwd,
        encode_cwd,
        read_session_header,
    )

    # Two roots hold cwd buckets: the engine's own (agent/sessions) and the product's
    # per-role tree (~/.misaka/sessions/<role>/). Both were named the old way.
    roots = [Path(get_agent_dir()) / "sessions", Path.home() / CONFIG_DIR_NAME / "sessions"]
    role_dirs = [entry for root in roots if root.is_dir()
                 for entry in sorted(root.iterdir()) if entry.is_dir()]
    moved = 0
    for role_dir in role_dirs:
        for bucket in sorted(role_dir.iterdir()):
            if not bucket.is_dir() or not bucket.name.startswith("--"):
                continue
            for session_file in sorted(bucket.glob("*.jsonl")):
                header = read_session_header(str(session_file))
                cwd = header.get("cwd")
                if not isinstance(cwd, str) or not cwd:
                    continue
                canonical = _canonical_cwd(cwd)
                if bucket.name not in {_legacy_encode_cwd(cwd), _legacy_encode_cwd(canonical)}:
                    continue                       # already canonical, or someone else's bucket
                target_dir = role_dir / encode_cwd(canonical)
                if target_dir == bucket:
                    continue
                target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                target = target_dir / session_file.name
                if target.exists():
                    target = target_dir / f"legacy-{session_file.stem}.jsonl"
                if target.exists():
                    continue
                atomic.write_bytes(target, session_file.read_bytes(), mode=0o600)
                session_file.unlink()
                moved += 1
            with contextlib.suppress(OSError):
                bucket.rmdir()                     # only when it is empty
    return moved


def migrate_settings_file() -> bool:
    """Persist pre-release global settings shapes proactively.

    Runtime loading applies them only to the in-memory view. An already-requested
    settings write canonicalizes the locked latest file. This command remains the only
    path that rewrites an otherwise untouched global settings file; startup never invokes it.
    """
    from misaka.core.settings_manager import SettingsManager

    path = Path(get_agent_dir()) / "settings.json"
    try:
        current = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    if not isinstance(current, dict):
        return False
    migrated = SettingsManager.migrateSettings(current)
    if migrated == current:
        return False
    atomic.write_text(path, json.dumps(migrated, indent=2, ensure_ascii=False))
    return True


def migrate_commands_to_prompts(base_dir: str, label: str) -> bool:
    commands_dir = Path(base_dir) / "commands"
    prompts_dir = Path(base_dir) / "prompts"
    if commands_dir.exists() and not prompts_dir.exists():
        try:
            commands_dir.rename(prompts_dir)
            print(f"{_GREEN}Migrated {label} commands/ → prompts/{_RESET}")
            return True
        except OSError as error:
            print(f"{_YELLOW}Warning: Could not migrate {label} commands/ to prompts/: {error}{_RESET}")
    return False


def migrate_keybindings_config_file() -> None:
    config_path = Path(get_agent_dir()) / "keybindings.json"
    if not config_path.exists():
        return
    try:
        parsed = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return
    if not isinstance(parsed, dict):
        return
    migration = migrateKeybindingsConfig(parsed)
    if not migration["migrated"]:
        return
    atomic.write_text(config_path, json.dumps(migration["config"], indent=2) + "\n")


def migrate_tools_to_bin() -> None:
    tools_dir = Path(get_agent_dir()) / "tools"
    bin_dir = Path(get_bin_dir())
    if not tools_dir.exists():
        return

    moved_any = False
    for name in ("fd", "rg", "fd.exe", "rg.exe"):
        old_path = tools_dir / name
        new_path = bin_dir / name
        if not old_path.exists():
            continue
        if not bin_dir.exists():
            bin_dir.mkdir(parents=True, exist_ok=True)
        if not new_path.exists():
            try:
                old_path.rename(new_path)
                moved_any = True
            except OSError:
                continue
        else:
            try:
                old_path.unlink()
            except OSError:
                pass

    if moved_any:
        print(f"{_GREEN}Migrated managed binaries tools/ → bin/{_RESET}")


def check_deprecated_extension_dirs(base_dir: str, label: str) -> list[str]:
    warnings: list[str] = []
    tools_dir = Path(base_dir) / "tools"
    if tools_dir.exists():
        try:
            custom_tools = [
                entry.name
                for entry in tools_dir.iterdir()
                if entry.name.lower() not in {"fd", "rg", "fd.exe", "rg.exe"} and not entry.name.startswith(".")
            ]
        except OSError:
            custom_tools = []
        if custom_tools:
            warnings.append(
                f"{label} tools/ directory contains custom tools. "
                "Custom tools have been merged into extensions."
            )
    return warnings


def migrate_extension_system(cwd: str) -> list[str]:
    agent_dir = get_agent_dir()
    project_dir = str(Path(cwd) / CONFIG_DIR_NAME)
    migrate_commands_to_prompts(agent_dir, "Global")
    migrate_commands_to_prompts(project_dir, "Project")
    return [
        *check_deprecated_extension_dirs(agent_dir, "Global"),
        *check_deprecated_extension_dirs(project_dir, "Project"),
    ]


def run_migrations(cwd: str) -> dict[str, list[str] | int]:
    migrated_auth_providers = migrate_auth_to_auth_json()
    migrate_sessions_from_agent_root()
    moved_sessions = migrate_legacy_session_buckets()
    migrate_settings_file()
    migrate_tools_to_bin()
    migrate_keybindings_config_file()
    deprecation_warnings = migrate_extension_system(cwd)
    return {
        "migratedAuthProviders": migrated_auth_providers,
        "movedSessions": moved_sessions,
        "deprecationWarnings": deprecation_warnings,
    }


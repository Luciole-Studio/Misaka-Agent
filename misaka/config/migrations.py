"""One-time migrations that run on startup."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from misaka.config import CONFIG_DIR_NAME, get_agent_dir, get_bin_dir
from misaka.core.keybindings import migrateKeybindingsConfig
from misaka.core.session_manager import get_default_session_dir

_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_DIM = "\x1b[2m"
_RESET = "\x1b[0m"


def migrate_auth_to_auth_json() -> list[str]:
    agent_dir = Path(get_agent_dir())
    auth_path = agent_dir / "auth.json"
    oauth_path = agent_dir / "oauth.json"
    settings_path = agent_dir / "settings.json"

    if auth_path.exists() and auth_path.stat().st_size > 2:     # "{}" is an empty store, not a finished migration
        return []

    # Read every old source first; nothing is renamed or rewritten until the new store holds it.
    migrated: dict[str, object] = {}
    settings: dict | None = None
    if oauth_path.exists():
        try:
            for provider, credential in json.loads(oauth_path.read_text(encoding="utf-8")).items():
                migrated[str(provider)] = {"type": "oauth", **credential}
        except Exception:  # noqa: BLE001 - an unreadable oauth.json is left in place
            pass
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
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
    for provider, credential in migrated.items():
        storage.set(provider, credential)
    storage.reload()
    if storage.loadError is not None or storage.errors or not all(storage.has(p) for p in migrated):
        return []                                              # the store did not take it: old sources stay untouched

    if oauth_path.exists():
        oauth_path.rename(oauth_path.with_suffix(".json.migrated"))
    if settings is not None and isinstance(settings, dict) and "apiKeys" in settings:
        settings.pop("apiKeys", None)
        settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return list(migrated)


def migrate_sessions_from_agent_root() -> None:
    agent_dir = Path(get_agent_dir())
    try:
        files = [path for path in agent_dir.iterdir() if path.is_file() and path.suffix == ".jsonl"]
    except OSError:
        return

    for session_file in files:
        try:
            first_line = session_file.read_text(encoding="utf-8").splitlines()[0]
            header = json.loads(first_line)
            if header.get("type") != "session" or not isinstance(header.get("cwd"), str):
                continue
            target_dir = Path(get_default_session_dir(header["cwd"], str(agent_dir)))
            target_dir.mkdir(parents=True, exist_ok=True)
            target_path = target_dir / session_file.name
            if target_path.exists():
                continue
            session_file.rename(target_path)
        except Exception:
            continue


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
        parsed = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(parsed, dict):
        return
    migration = migrateKeybindingsConfig(parsed)
    if not migration["migrated"]:
        return
    config_path.write_text(json.dumps(migration["config"], indent=2) + "\n", encoding="utf-8")


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


async def show_deprecation_warnings(warnings: list[str]) -> None:
    if not warnings:
        return
    for warning in warnings:
        print(f"{_YELLOW}Warning: {warning}{_RESET}")
    print(f"{_YELLOW}\nMove your extensions to the extensions/ directory.{_RESET}")
    print(f"{_DIM}\nPress any key to continue...{_RESET}")
    if sys.stdin.isatty():
        await asyncio.to_thread(_read_single_keypress)
    print()


def _read_single_keypress() -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.getch()
        return

    import termios
    import tty

    stream = sys.stdin
    try:
        fileno = stream.fileno()
    except OSError:
        stream.read(1)
        return

    original_settings = termios.tcgetattr(fileno)
    try:
        tty.setraw(fileno)
        stream.read(1)
    finally:
        termios.tcsetattr(fileno, termios.TCSADRAIN, original_settings)


def run_migrations(cwd: str) -> dict[str, list[str]]:
    migrated_auth_providers = migrate_auth_to_auth_json()
    migrate_sessions_from_agent_root()
    migrate_tools_to_bin()
    migrate_keybindings_config_file()
    deprecation_warnings = migrate_extension_system(cwd)
    return {
        "migratedAuthProviders": migrated_auth_providers,
        "deprecationWarnings": deprecation_warnings,
    }


migrateAuthToAuthJson = migrate_auth_to_auth_json
migrateSessionsFromAgentRoot = migrate_sessions_from_agent_root
showDeprecationWarnings = show_deprecation_warnings
runMigrations = run_migrations

__all__ = [
    "migrateAuthToAuthJson",
    "migrateSessionsFromAgentRoot",
    "showDeprecationWarnings",
    "runMigrations",
]

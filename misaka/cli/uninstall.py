"""``misaka uninstall``: remove what this install put on the machine, and nothing else.

The counterpart to ``misaka setup``. Everything MISAKA owns lives under one directory
(``~/.misaka`` unless the ``MISAKA_*`` path variables moved a piece of it), so the honest
form of this command is: resolve the paths this install actually uses, show what they hold
and what they cost, say plainly what is lost, and remove them on a yes.

Two things it deliberately does not do:

- **Project folders are never touched.** They are the point of the product: research
  products, their sources, and the git history that dates them. They live wherever the user
  ran ``misaka init``, and the board knows where -- so they are listed, by way of promising
  not to touch them.
- **The package is not uninstalled.** A process cannot reliably remove the code it is
  running from, and pip, uv and pipx each want their own command. The right one is printed.
"""
from __future__ import annotations

import os
import shutil

from misaka.cli import setup_ui as ui
from misaka.cli.setup_ui import SetupCancelled, prompt_choice

# What each top-level entry under the config root is for, in the order a person cares.
CONTENTS = (
    ("agent", "credentials, settings.json, the model catalog and custom themes"),
    ("profiles", "Last Order, the Sisters, their skills, and saved conversations"),
    ("board.db", "the task board: every card and every research run's record"),
    ("lcm.db", "the context engine's memory of past sessions"),
    ("lcm-large-outputs", "tool results the context engine moved out of the transcript"),
    ("messages.db", "the message queue between roles"),
    ("pageindex", "extracted text and outlines of the documents you indexed"),
    ("cache", "web and office caches; rebuilt on demand"),
    ("tasks", "per-card locks and read-only skill copies"),
    ("sessions", "Last Order's own conversations"),
    ("input-history", "what you have typed at the prompts"),
)


def _known_paths() -> dict[str, str]:
    """Every path this install owns, resolved through the environment overrides."""
    from misaka.config import CFG, get_agent_dir, get_sessions_dir
    paths: dict[str, str] = {}

    def add(value: str | None) -> None:
        if value:
            paths[os.path.realpath(os.path.expanduser(str(value)))] = str(value)

    for key in ("db", "messages_db", "lcm_db", "allies", "web_config", "web_cache",
                "office_cache", "office_intent", "net_sock", "net_snapshot",
                "roles_root", "profiles_root", "tasks_root"):
        add(CFG.get(key))
    add(get_agent_dir())
    try:
        add(get_sessions_dir())
    except Exception as error:  # noqa: BLE001 - an unresolvable session store is not a reason to stop
        ui.print_warning(f"The session store could not be resolved ({error}); it is left alone.")
    return paths


def _root_of(paths: dict[str, str]) -> str | None:
    """The one directory that holds every known path, when there is one.

    A default install keeps everything under ``~/.misaka``; redirect any single path with
    its ``MISAKA_*`` variable and there is no longer a single root, so the command falls
    back to naming each path instead of removing a tree it cannot vouch for.
    """
    default = os.path.realpath(os.path.expanduser("~/.misaka"))
    if all(path == default or path.startswith(default + os.sep) for path in paths):
        return default
    return None


def _size(path: str) -> int:
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for here, _dirs, files in os.walk(path, onerror=lambda _error: None):
        for name in files:
            try:
                total += os.lstat(os.path.join(here, name)).st_size
            except OSError:
                pass
    return total


def _megabytes(count: int) -> str:
    return f"{count / 1048576:.1f} MB" if count >= 1048576 else f"{count / 1024:.0f} KB"


def _projects(db_path: str) -> list[str]:
    """The project folders the board has seen, minus any inside the tree being removed."""
    import sqlite3
    if not os.path.isfile(db_path):
        return []
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
            rows = con.execute(
                "SELECT DISTINCT workspace FROM tasks WHERE workspace IS NOT NULL").fetchall()
    except sqlite3.Error:
        return []
    root = os.path.realpath(os.path.expanduser("~/.misaka"))
    home = os.path.realpath(os.path.expanduser("~"))
    found = []
    for (workspace,) in rows:
        path = os.path.realpath(os.path.expanduser(str(workspace)))
        # The home directory turns up when a card was created from a shell sitting in it, and
        # it is not a project: `misaka init` refuses it. Listing it here would read as a claim
        # that MISAKA considers the whole home directory one.
        if path in (root, home) or path.startswith(root + os.sep) or not os.path.isdir(path):
            continue
        found.append(path)
    return sorted(set(found))


def _stop_daemon() -> None:
    """Ask the panel daemon to stop, the way ``misaka net stop`` does. A daemon that is not
    running raises on connect, which is the same outcome as stopping it."""
    from misaka.ui.panel import client
    try:
        client.request("server.stop", timeout=3)
    except (ConnectionError, FileNotFoundError, OSError, RuntimeError):
        return
    ui.print_success("Stopped the panel daemon.")


def _remove(path: str) -> tuple[bool, str]:
    """Directories go whole; everything else is unlinked, sockets and lock files included."""
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        elif os.path.lexists(path):
            os.unlink(path)
        else:
            return True, "already gone"
    except OSError as error:
        return False, str(error)
    return True, ""


def run(*, assume_yes: bool = False, dry_run: bool = False) -> int:
    from misaka.config import CFG
    ui.print_header("Uninstall")
    paths = _known_paths()
    root = _root_of(paths)
    home = os.path.realpath(os.path.expanduser("~"))
    targets = [root] if root else sorted(paths)
    for target in targets:
        if target == home or target == os.path.dirname(target):
            ui.print_error(f"{target} is your home directory or a filesystem root; refusing.")
            return 2
    present = [target for target in targets if os.path.lexists(target)]
    if not present:
        ui.print_success("Nothing to remove: this machine has no MISAKA data.")
        return 0

    if root:
        ui.print_info(f"Everything this install owns is under {ui.color(ui.tilde(root), ui.BOLD)}:", "")
        described = {name for name, _purpose in CONTENTS}
        for name, purpose in CONTENTS:
            entry = os.path.join(root, name)
            if os.path.exists(entry):
                ui.print_check(True, name, f"{_megabytes(_size(entry)):>9}   {purpose}")
        # Whatever else the tree has picked up goes too, so it is named rather than implied.
        rest = sorted(entry.name for entry in os.scandir(root)
                      if entry.name not in described and not entry.name.startswith("."))
        if rest:
            ui.print_check(True, "and the rest", f"{'':>9}   {', '.join(rest)}")
        ui.print_info("", f"Total: {ui.color(_megabytes(_size(root)), ui.BOLD)}")
    else:
        ui.print_info("Paths have been redirected, so these are removed one by one:", "")
        for target in present:
            ui.print_check(True, os.path.basename(target) or target, ui.tilde(target))

    projects = _projects(os.path.expanduser(CFG["db"]))
    ui.print_info("")
    if projects:
        ui.print_success(f"Your {len(projects)} project folder(s) are NOT touched:")
        for path in projects[:8]:
            ui.print_info(f"    {ui.tilde(path)}")
        if len(projects) > 8:
            ui.print_info(f"    ... and {len(projects) - 8} more")
        ui.print_info("  Research products, their sources and their git history stay where they are.")
    else:
        ui.print_info("No project folder is recorded; anything you made with `misaka init` stays "
                      "where it is either way.")

    ui.print_info("")
    ui.print_warning("This removes stored API keys, every Sister, and the record of every research run.")
    ui.print_warning("The reports themselves live in your projects and survive; the board that indexes them does not.")

    if dry_run:
        ui.print_info("", "--dry-run: nothing was removed.")
        return 0
    if not assume_yes:
        ui.print_info("")
        try:
            if prompt_choice("Remove it?", ["No, leave everything alone", "Yes, remove it"], 0) != 1:
                ui.print_info("Nothing was removed.")
                return 1
        except SetupCancelled:
            print()
            ui.print_info("Nothing was removed.")
            return 1

    _stop_daemon()
    failed = False
    for target in present:
        ok, note = _remove(target)
        if ok:
            ui.print_success(f"removed {ui.tilde(target)}" + (f" ({note})" if note else ""))
        else:
            ui.print_error(f"{target}: {note}")
            failed = True

    ui.print_info("", "The package itself is your installer's to remove:",
                  "  pip uninstall misaka           (or `uv tool uninstall misaka`, `pipx uninstall misaka`)")
    return 1 if failed else 0

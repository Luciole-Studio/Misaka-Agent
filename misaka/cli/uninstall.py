"""``misaka uninstall``: remove what this install put on the machine, and nothing else.

The counterpart to ``misaka setup``. Everything MISAKA owns lives under one directory, the
home (:mod:`misaka.config.home`), so the honest form of this command is: show what it holds
and what it costs, say plainly what is lost, and remove it on a yes.

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
from contextlib import closing
from pathlib import Path

from misaka.cli import setup_ui as ui
from misaka.cli.setup_ui import SetupCancelled, prompt_choice
from misaka.config import home


def _contents(root: str) -> list[tuple[str, str]]:
    """Each top-level entry of the home with what it is, as the layout table declares it."""
    kinds: dict[str, set[str]] = {}
    for entry in home.LAYOUT.values():
        kinds.setdefault(entry.rel.split("/", 1)[0], set()).add(entry.kind)
    return [(name, "; ".join(home.KINDS[kind] for kind in home.KINDS if kind in kinds.get(name, ())))
            for name in sorted(os.listdir(root)) if not name.startswith(".")]


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
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as con:
            rows = con.execute(
                "SELECT DISTINCT workspace FROM tasks WHERE workspace IS NOT NULL").fetchall()
    except sqlite3.Error:
        return []
    root = str(home.home())
    user_home = os.path.realpath(os.path.expanduser("~"))
    found = []
    for (workspace,) in rows:
        path = os.path.realpath(os.path.expanduser(str(workspace)))
        # The home directory turns up when a card was created from a shell sitting in it, and
        # it is not a project: `misaka init` refuses it. Listing it here would read as a claim
        # that MISAKA considers the whole home directory one.
        if path in (root, user_home) or path.startswith(root + os.sep) or not os.path.isdir(path):
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
    ui.print_header("Uninstall")
    root = str(home.home())
    if root in (os.path.realpath(os.path.expanduser("~")), os.path.dirname(root)):
        ui.print_error(f"{root} is your home directory or a filesystem root; refusing.")
        return 2
    if not os.path.lexists(root):
        ui.print_success("Nothing to remove: this machine has no MISAKA data.")
        return 0

    ui.print_info(f"Everything this install owns is under {ui.color(ui.tilde(root), ui.BOLD)}:", "")
    for name, purpose in _contents(root):
        ui.print_check(True, name, f"{_megabytes(_size(os.path.join(root, name))):>9}   {purpose}")
    ui.print_info("", f"Total: {ui.color(_megabytes(_size(root)), ui.BOLD)}")

    projects = _projects(str(home.path("db")))
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
    ok, note = _remove(root)
    if ok:
        ui.print_success(f"removed {ui.tilde(root)}" + (f" ({note})" if note else ""))
    else:
        ui.print_error(f"{root}: {note}")
        failed = True

    ui.print_info("", "The package itself is your installer's to remove:",
                  "  pip uninstall misaka           (or `uv tool uninstall misaka`, `pipx uninstall misaka`)")
    return 1 if failed else 0

"""``misaka uninstall``: remove what this install put on the machine, and nothing else.

The counterpart to ``misaka setup``, shaped after Hermes's uninstaller in intent rather than detail:

- **The program and your data are separate choices, and the default keeps the data**, so a
  reinstall picks up where you left off. ``--full`` removes both; ``--data`` removes only the data.
- **Everything that would go is listed first**, and ``--dry-run`` stops there. That includes what
  MISAKA left outside its home: links on the PATH that point into it (tools an agent installed
  under ``shared/``), and what a project's ``.misaka/`` holds that MISAKA derived (a plugin's store).
- **Before data goes, a backup archive is offered** (``cli.backup``): the home less its caches,
  logs and sockets, kept outside it.
- **Running work is not cut off**: an active research run, a card, or a session open in the panel
  refuses the command until it is finished or closed.
- **The program is removed with the tool that installed it** (``uv tool``, pipx, pip), as the
  last step, since it is the code this command runs from.

Never touched: a project's own content, the configuration a person wrote into its ``.misaka/``
(``settings.json``, ``subagents/``, ...), and a source checkout, which is a repository of yours.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

from misaka.cli import setup_ui as ui
from misaka.cli import update
from misaka.cli.setup_ui import SetupCancelled, SetupGoBack, prompt_choice
from misaka.config import home

_USER_CONFIG = {entry.rel.split("/", 1)[0] for entry in home.LAYOUT.values() if entry.kind == "edit" and entry.rel}


def _contents(root: str) -> list[tuple[str, str]]:
    """Each top-level entry of the home with what it is, as the layout table declares it."""
    kinds: dict[str, set[str]] = {}
    for entry in home.LAYOUT.values():
        kinds.setdefault(entry.rel.split("/", 1)[0], set()).add(entry.kind)
    return [(name, "; ".join(home.KINDS[kind] for kind in home.KINDS if kind in kinds.get(name, ())))
            for name in sorted(os.listdir(root)) if not name.startswith(".")]


def _size(path: str) -> int:
    if os.path.islink(path) or os.path.isfile(path):
        return os.lstat(path).st_size
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
    rows = []
    try:
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as con:
            for table in ("tasks", "research_runs"):
                try:
                    rows += con.execute(f"SELECT DISTINCT workspace FROM {table} WHERE workspace IS NOT NULL").fetchall()
                except sqlite3.OperationalError:
                    pass
    except sqlite3.Error:
        return []
    root = str(home.home())
    user_home = os.path.realpath(os.path.expanduser("~"))
    found = []
    for (workspace,) in rows:
        path = os.path.realpath(os.path.expanduser(str(workspace)))
        if path in (root, user_home) or path.startswith(root + os.sep) or not os.path.isdir(path):
            continue
        found.append(path)
    return sorted(set(found))


def _derived(projects: list[str]) -> list[str]:
    """What MISAKA wrote into each project's ``.misaka/`` that no person did."""
    found = []
    for project in projects:
        config = home.project_dir(project)
        if config is None or not config.is_dir() or config.is_symlink():
            continue
        found += [str(entry) for entry in sorted(config.iterdir())
                  if entry.name not in _USER_CONFIG and entry.name != ".DS_Store"]
    return found


def _links_into(root: Path) -> list[str]:
    """Links on the PATH (and in ``~/.local/bin``) that point into ``root``. They are tools an agent
    installed under the home; removing the home would leave them dangling."""
    folders = dict.fromkeys([*os.environ.get("PATH", "").split(os.pathsep), os.path.expanduser("~/.local/bin")])
    found = []
    for folder in folders:
        if not folder or not os.path.isdir(folder) or Path(os.path.realpath(folder)).is_relative_to(root):
            continue
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in names:
            link = os.path.join(folder, name)
            if os.path.islink(link) and Path(os.path.realpath(link)).is_relative_to(root):
                found.append(link)
    return found


def _program_command(install: update.Install) -> list[str] | None:
    """The command that removes the program, or None when this command must not: a source
    checkout is a repository of yours, and a package of unknown origin has no known remover."""
    if install.installer == "uv tool":
        return ["uv", "tool", "uninstall", "misaka"]
    if install.installer == "pipx":
        return ["pipx", "uninstall", "misaka"]
    if install.installer == "Homebrew":
        return ["brew", "uninstall", "misaka"]
    if install.installer == "winget":
        return ["winget", "uninstall", "Luciole-Studio.Misaka"]
    if install.kind == "checkout":
        return None
    if install.installer == "uv":
        return ["uv", "pip", "uninstall", "--python", sys.executable, "misaka"]
    if install.installer == "pip":
        return [sys.executable, "-m", "pip", "uninstall", "-y", "misaka"]
    return None


def _writable_retry(function, path, _error) -> None:
    """Windows will not delete a read-only file (git's objects are): clear the flag, try again."""
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _remove(path: str) -> tuple[bool, str]:
    """Directories go whole; everything else is unlinked, sockets and lock files included."""
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, onexc=_writable_retry if os.name == "nt" else None)
        elif os.path.lexists(path):
            os.unlink(path)
        else:
            return True, "already gone"
    except OSError as error:
        return False, str(error)
    return True, ""


def _this_install(exe: str | None) -> bool:
    """Whether ``exe`` belongs to this install: its environment's interpreter, or a launcher."""
    exe = os.path.normcase(exe or "")
    return bool(exe) and (exe.startswith(os.path.normcase(sys.prefix) + os.sep)
                          or exe in {os.path.normcase(str(path)) for path in update._launchers()})


def _holders(*, daemon: bool) -> list[str]:
    """Other processes running this install, as ``pid  command``: a ``misaka`` open in another
    window, and with ``daemon`` the panel daemon too. Removing the program or the home under one
    stops half-way on Windows and leaves a broken install, the reason Hermes refuses to update
    over its own (``list_venv_holders``)."""
    import psutil
    mine = {os.getpid(), *(parent.pid for parent in psutil.Process().parents())}
    found = {}
    for proc in psutil.process_iter(["pid", "ppid", "exe", "cmdline"]):
        command = " ".join(proc.info.get("cmdline") or [])
        if proc.info["pid"] in mine or not _this_install(proc.info.get("exe")) or (
                not daemon and "misaka.ui.panel.daemon" in command):
            continue
        found[proc.info["pid"]] = (proc.info["ppid"], command or proc.info.get("exe"))
    return [f"{pid}  {command}" for pid, (parent, command) in found.items() if parent not in found]


def _release_own_files(root: Path) -> None:
    """Close what this process holds open in the home: the log file every MISAKA process keeps."""
    import logging
    logger = logging.getLogger()
    for handler in list(logger.handlers):
        filename = getattr(handler, "baseFilename", None)
        if filename and Path(filename).is_relative_to(root):
            logger.removeHandler(handler)
            handler.close()


def _remove_after_exit(remover: list[str]) -> Path | None:
    """Run ``remover`` once this command's processes have exited, since Windows will not delete
    the program they run: a detached PowerShell waits for them, runs it and logs what it said,
    the way Hermes's desktop app uninstalls through a detached cleanup script. The log's path, or
    None when the helper could not start."""
    import base64

    import psutil
    executable = shutil.which(remover[0])
    if executable is None:
        return None
    waits = [os.getpid(), *(parent.pid for parent in psutil.Process().parents() if _this_install(parent.exe()))]
    log = Path(tempfile.gettempdir()) / f"misaka-uninstall-{time.strftime('%Y%m%d-%H%M%S')}.log"

    def quoted(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"
    script = (f"Wait-Process -Id {','.join(map(str, waits))} -Timeout 120 -ErrorAction SilentlyContinue; "
              f"& {quoted(executable)} {' '.join(quoted(arg) for arg in remover[1:])} 2>&1 "
              f"| ForEach-Object {{ \"$_\" }} | Out-File -FilePath {quoted(str(log))} -Encoding utf8")
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand",
             base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)  # windows-footgun: ok - Windows only
    except OSError:
        return None
    return log


def _choose(mode: str | None, assume_yes: bool) -> str | None:
    """``keep`` | ``full`` | ``data``, or None for no. ``--yes`` alone keeps the data, as Hermes's does."""
    if mode or assume_yes:
        return mode or "keep"
    ui.print_info("")
    choice = prompt_choice("What should go?", [
        "Keep data - remove the program only; the home stays for a reinstall (recommended)",
        "Full uninstall - the program, the home, and the MISAKA files listed outside it",
        "Data only - the home and those files; the program stays installed",
        "Cancel"], 0)
    return ("keep", "full", "data", None)[choice]


def _offer_backup(root: Path) -> bool:
    """Offer the archive; False when one was wanted and could not be written."""
    from misaka.cli import backup
    target = Path.home() / f"misaka-backup-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    if prompt_choice(f"Save a backup of {ui.tilde(str(root))} first?",
                     [f"Yes, to {ui.tilde(str(target))}", "No, delete it without one"], 0) != 0:
        return True
    try:
        skipped = backup.archive(target)
    except OSError as error:
        ui.print_error(f"The backup could not be written ({error}); nothing was removed.")
        return False
    ui.print_success(f"Backed up to {ui.tilde(str(target))} (it holds your credentials: keep it private).")
    for path in skipped:
        ui.print_warning(f"not in the backup (unreadable): {ui.tilde(path)}")
    return True


def run(*, mode: str | None = None, assume_yes: bool = False, dry_run: bool = False) -> int:
    ui.print_header("Uninstall")
    root = home.home()
    if str(root) in (os.path.realpath(os.path.expanduser("~")), os.path.dirname(str(root))):
        ui.print_error(f"{root} is your home directory or a filesystem root; refusing.")
        return 2

    install = update.describe()
    remover = _program_command(install)
    ui.print_info(ui.color("The program", ui.BOLD))
    ui.print_check(True, "misaka", f"v{install.version}" + (f", installed by {install.installer}" if install.installer else ""))
    if remover:
        ui.print_info(f"    removed with: {' '.join(remover)}")
    elif install.kind == "checkout" and install.path:
        ui.print_info(f"    a source checkout ({ui.tilde(str(install.path))}): yours to delete, never removed here")
    elif install.kind == "bundle" and install.path:
        ui.print_info(f"    a release archive: delete {ui.tilde(str(install.path))}, and the `misaka` link the",
                      "    installer made (~/.local/bin/misaka), or on Windows its bin folder on your PATH")
    else:
        ui.print_info("    removed with the tool that installed it: `pip uninstall misaka`, `uv tool uninstall misaka`",
                      "    or `pipx uninstall misaka`")

    has_home = os.path.lexists(root)
    projects = _projects(str(home.path("db"))) if has_home else []
    derived = _derived(projects)
    links = _links_into(root) if has_home else []
    ui.print_info("", ui.color(f"Your data, in {ui.tilde(str(root))}", ui.BOLD))
    if has_home:
        for name, purpose in _contents(str(root)):
            ui.print_check(True, name, f"{_megabytes(_size(str(root / name))):>9}   {purpose}")
        ui.print_info(f"    Total: {ui.color(_megabytes(_size(str(root))), ui.BOLD)}")
    else:
        ui.print_info("    none on this machine")
    if derived:
        ui.print_info("", ui.color(f"Derived in your projects' {home.DIR_NAME}/ (removed with the data; your settings there stay)", ui.BOLD),
                      *[f"    {ui.tilde(path)}   {_megabytes(_size(path))}" for path in derived])
    if links:
        ui.print_info("", ui.color("Links into the home (removed with the data; they would dangle)", ui.BOLD),
                      *[f"    {ui.tilde(path)} -> {ui.tilde(os.readlink(path))}" for path in links])
    ui.print_info("")
    if projects:
        ui.print_success(f"Your {len(projects)} project folder(s) keep everything you put in them:")
        for path in projects[:8]:
            ui.print_info(f"    {ui.tilde(path)}")
        if len(projects) > 8:
            ui.print_info(f"    ... and {len(projects) - 8} more")
        ui.print_info("  Research products, their sources and their git history stay where they are.")
    else:
        ui.print_info("No project folder is recorded; anything you made with `misaka init` stays where it is either way.")

    if dry_run:
        ui.print_info("", "--dry-run: nothing was removed. By default the program goes and the data stays;",
                      "--full removes both, --data only the data.")
        return 0

    blocking, open_panes = update.running_work()
    if blocking or open_panes:
        ui.print_error("MISAKA is still running here:")
        ui.print_info(*[f"    {item}" for item in blocking],
                      *[f"    pane {pane.get('id')}: {pane.get('title') or 'a session'}" for pane in open_panes], "",
                      "Let research runs and cards finish, or stop them (`/research stop` in the run's Last Order",
                      "window). Then close the panel from a plain terminal with `misaka net stop`, and run this again.")
        return 2
    others = _holders(daemon=False) if os.name == "nt" else []
    if others:
        ui.print_error("MISAKA is still running in another window:")
        ui.print_info(*[f"    {line}" for line in others], "",
                      "Windows cannot remove a program or files that are in use. Close those, then run this again.")
        return 2

    try:
        mode = _choose(mode, assume_yes)
        if mode is None:
            ui.print_info("Nothing was removed.")
            return 1
        removes_data = mode in ("full", "data") and has_home
        if removes_data:
            ui.print_info("")
            ui.print_warning("This removes stored API keys, every Sister, and the record of every research run.")
            ui.print_warning("The reports themselves live in your projects and survive; the board that indexes them does not.")
            if not assume_yes:
                if prompt_choice("Remove the data?", ["No, leave everything alone", "Yes, remove it"], 0) != 1:
                    ui.print_info("Nothing was removed.")
                    return 1
                if not _offer_backup(root):
                    return 1
    except (SetupCancelled, SetupGoBack):
        print()
        ui.print_info("Nothing was removed.")
        return 1

    update.stop_daemon()
    if os.name == "nt":
        deadline = time.monotonic() + 10
        while (others := _holders(daemon=True)) and time.monotonic() < deadline:
            time.sleep(0.2)
        if others:
            ui.print_error("These MISAKA processes did not exit; nothing was removed:")
            ui.print_info(*[f"    {line}" for line in others])
            return 1
        if removes_data:
            _release_own_files(root)
    failed = False
    if removes_data:
        for path in [*links, *derived, str(root)]:
            ok, note = _remove(path)
            if ok:
                ui.print_success(f"removed {ui.tilde(path)}" + (f" ({note})" if note else ""))
            else:
                ui.print_error(f"{path}: {note}")
                failed = True
    if mode == "data":
        return 1 if failed else 0
    if remover is None:
        ui.print_info("", "The program stays: " + (
            f"{ui.tilde(str(install.path))} is a source checkout, yours to delete." if install.kind == "checkout" and install.path
            else "remove it with the tool that installed it."))
        return 1 if failed else 0
    ui.print_info("", f"Removing the program: {' '.join(remover)}")
    if os.name == "nt":
        log = _remove_after_exit(remover)
        if log is None:
            ui.print_error(f"{remover[0]} could not be started after this command; the program is still installed.")
            return 1
        ui.print_success("MISAKA is uninstalled as soon as this command exits"
                         + ("." if removes_data else f"; your data stays in {ui.tilde(str(root))}."))
        ui.print_info(f"    What {remover[0]} reports goes to {ui.tilde(str(log))}.")
        return 1 if failed else 0
    try:
        result = subprocess.run(remover, check=False)
    except OSError as error:
        ui.print_error(f"{remover[0]} could not start: {error}")
        return 1
    if result.returncode != 0:
        ui.print_error(f"{remover[0]} exited with {result.returncode}; the program is still installed.")
        return 1
    ui.print_success("MISAKA is uninstalled." + ("" if removes_data else f" Your data stays in {ui.tilde(str(root))}."))
    return 1 if failed else 0

"""``misaka update``: is this install behind the repository, and what would bring it level.

Shaped after how Hermes updates itself, in intent rather than detail. Follow the branch head
rather than release tags -- main, or the branch the install was made from -- fast-forward only,
and never guess: refuse on a dirty tree, a diverged branch, or an install shape it cannot vouch
for, and say what to run by hand. Checking and applying are separate (``hermes update --check``);
nothing here runs on its own. Applying keeps Hermes's promises too:

- the install method decides the command, and what the install holds survives it: uv and pipx
  upgrade from their own records, extras included; pip is handed the extras found installed;
- running work is not cut off: an active research run or a card in a pane refuses the update,
  and a panel with sessions open is left running for its owner to restart;
- before anything changes, the settings, credentials and state databases are copied aside
  (``cli.backup``), because a code rollback is not a data rollback;
- the updated code must start. When it cannot, a checkout is put back where it was; any other
  install is told the command that puts it back.

How the install was made is read, not guessed: PEP 610 writes ``direct_url.json`` into the
dist-info, ``INSTALLER`` records which tool did it, and uv and pipx leave a receipt in the
environment they manage.
"""
from __future__ import annotations

import contextlib
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from misaka.cli import setup_ui as ui
from misaka.cli.setup_ui import SetupCancelled, SetupGoBack, prompt_choice

REPO = "Luciole-Studio/Misaka-Agent"
REPO_URL = f"https://github.com/{REPO}.git"
BRANCH = "main"
API = f"https://api.github.com/repos/{REPO}"
TIMEOUT = 10


@dataclass(frozen=True)
class Install:
    """Where this install's code comes from, and what would update it."""

    kind: str                 # "checkout" | "git" | "wheel" | "bundle" (a release archive)
    editable: bool
    installer: str            # "uv tool" | "pipx" | "uv" (uv pip) | "pip" | "Homebrew" | "winget" | ""
    path: Path | None         # the checkout, when there is one
    commit: str | None        # the commit a git install pinned
    version: str
    revision: str | None = None   # what a git install was asked for (PEP 610 ``requested_revision``): a branch, a tag

    @property
    def follows(self) -> str:
        """What this install tracks: the revision it was installed from, else the main branch. A git
        install made from another branch is that branch's, not behind or beside main."""
        return self.revision or BRANCH

    @property
    def source(self) -> str:
        """The git requirement that makes this install again, at the revision it follows."""
        return f"git+{REPO_URL}" + (f"@{self.revision}" if self.revision else "")


def _manager(installer: str) -> str:
    """The tool that owns this environment. ``INSTALLER`` alone cannot say: pipx installs through
    pip, and uv writes one name for ``uv tool install`` and ``uv pip install`` alike. The two
    tools that manage whole environments leave a receipt in them."""
    prefix = Path(sys.prefix)
    if (prefix / "uv-receipt.toml").is_file():
        return "uv tool"
    if (prefix / "pipx_metadata.json").is_file():
        return "pipx"
    return installer


# What a release archive's installer is run with; a new one is the way to update it.
INSTALL_SH = f"curl -fsSL https://raw.githubusercontent.com/{REPO}/main/scripts/install.sh | sh"
INSTALL_PS1 = f"irm https://raw.githubusercontent.com/{REPO}/main/scripts/install.ps1 | iex"


# A release archive Homebrew or winget put in place is theirs to replace (the installer script
# would add a second copy beside it).
PACKAGE_UPGRADE = {"Homebrew": "brew upgrade misaka", "winget": "winget upgrade Luciole-Studio.Misaka"}


def describe() -> Install:
    from importlib.metadata import PackageNotFoundError, distribution

    from misaka.config import VERSION
    from misaka.config.engine import bundle_installer, bundle_root
    bundle = bundle_root()
    if bundle is not None:
        # pip and git have nothing to do with an archive: a new archive is how it is updated.
        return Install("bundle", False, bundle_installer(bundle), bundle, None, VERSION)
    try:
        dist = distribution("misaka")
    except PackageNotFoundError:
        return Install("wheel", False, "", None, None, VERSION)
    installer = _manager((dist.read_text("INSTALLER") or "").strip())
    try:
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
    except ValueError:
        direct = {}
    url = str(direct.get("url") or "")
    vcs = direct.get("vcs_info") or {}
    if vcs.get("vcs") == "git":
        return Install("git", False, installer, None, vcs.get("commit_id"), dist.version,
                       vcs.get("requested_revision") or None)
    if url.startswith("file://"):
        parsed = urlsplit(url)
        editable = bool((direct.get("dir_info") or {}).get("editable"))
        if parsed.netloc not in ("", "localhost"):
            return Install("wheel", editable, installer, None, None, dist.version)
        path = Path(url2pathname(parsed.path))
        if (path / ".git").exists():
            return Install("checkout", editable, installer, path, None, dist.version)
        return Install("wheel", editable, installer, path, None, dist.version)
    return Install("wheel", False, installer, None, None, dist.version)


def installed_extras() -> list[str]:
    """The extras this environment holds in full, named the widest way: ``providers``, not its parts.

    Nothing but uv's and pipx's receipts records which extras an install asked for. What is
    installed is the next best record, and it is what a reinstall must not take away."""
    import re
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        requires = distribution("misaka").requires or []
    except PackageNotFoundError:
        return []
    needs: dict[str, set[str]] = {}
    for line in requires:
        requirement, _, marker = line.partition(";")
        extra = re.search(r"extra\s*==\s*['\"]([^'\"]+)['\"]", marker)
        name = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
        if extra and name:
            needs.setdefault(extra.group(1), set()).add(name.group(1))

    def present(name: str) -> bool:
        try:
            distribution(name)
        except PackageNotFoundError:
            return False
        return True

    held = {extra for extra, names in needs.items() if all(present(name) for name in names)}
    return sorted(extra for extra in held if not any(needs[extra] < needs[other] for other in held))


def active_runs() -> list[str]:
    """Research runs in flight, as ``<run id>  <project>``, read without taking a write lock: a run
    whose driver holds a live lease. One whose driver died is paused, not in flight -- it resumes
    as well after the update -- and it used to block every update until resumed, since stopping
    it from a window leaves it "stopping" with no driver to finish the stop. A board that cannot
    be read reports none: this is a courtesy check, not the board's guard."""
    import sqlite3
    import time
    from contextlib import closing

    from misaka.config import home
    from misaka.core import graph
    from misaka.core.research.runs import ACTIVE
    board = home.path("db")
    if not board.is_file():
        return []
    try:
        with closing(sqlite3.connect(board.resolve().as_uri() + "?mode=ro", uri=True)) as con:
            con.row_factory = sqlite3.Row
            active = con.execute(f"SELECT id, workspace FROM research_runs WHERE status IN ({','.join('?' * len(ACTIVE))})",
                                 ACTIVE).fetchall()
            now, rows = int(time.time()), []
            # A run is driven while its graph's lease is held; a board research has not opened since
            # an upgrade keeps the lease on the run row still (schema v18, migrated on that open).
            before_migration = "driver_lock" in {row[1] for row in con.execute('PRAGMA table_info("research_runs")')}
            for run_id, workspace in active:
                if before_migration:
                    token, expires = con.execute("SELECT driver_lock, driver_expires FROM research_runs WHERE id=?",
                                                 (run_id,)).fetchone()
                else:
                    lease = graph.get(con, run_id)
                    token, expires = (lease.lease_token, lease.lease_expires) if lease else (None, None)
                if token is not None and (expires is None or expires >= now):
                    rows.append((run_id, workspace))
    except sqlite3.Error:
        return []
    return [f"{run_id}  {home.display(workspace)}" for run_id, workspace in rows]


def live_panes() -> list[dict]:
    """The panel daemon's panes that still run something; none when no daemon answers."""
    from misaka.ui.panel import client
    try:
        panes = client.request("panes.list", timeout=3)["panes"]
    except (ConnectionError, FileNotFoundError, OSError, RuntimeError, KeyError, TypeError):
        return []
    return [pane for pane in panes if pane.get("alive")]


def running_work() -> tuple[list[str], list[dict]]:
    """``(what must finish first, panes left open)``. The first is research runs in flight and
    cards running in panes: stopping the daemon under them is losing work. The second is every
    other live pane: a session its owner has open."""
    panes = live_panes()
    blocking = [f"research run {run}" for run in active_runs()]
    blocking += [f"card {pane['card']} in pane {pane.get('id')}" for pane in panes if pane.get("card")]
    return blocking, [pane for pane in panes if not pane.get("card")]


def stop_daemon() -> None:
    """Ask the panel daemon to stop, the way ``misaka net stop`` does. A daemon that is not
    running raises on connect, which is the same outcome as stopping it."""
    from misaka.ui.panel import client
    try:
        client.request("server.stop", timeout=3)
    except (ConnectionError, FileNotFoundError, OSError, RuntimeError):
        return
    ui.print_success("Stopped the panel daemon.")


def _git(path: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(path), *args],
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            check=False, timeout=60)
    if check and result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _remote_head_via_git(path: Path, branch: str = BRANCH) -> str | None:
    """The branch head straight from the remote. No API, no token, no rate limit."""
    try:
        line = _git(path, "ls-remote", "origin", f"refs/heads/{branch}")
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return None
    return line.split()[0] if line else None


def github_token() -> tuple[str | None, str]:
    """A token for the API and where it came from, or ``(None, "")``.

    Two sources, in the order every GitHub tool uses them: the standard environment
    variables, then whatever ``gh`` is already signed in as. Both are credentials the user
    has already arranged for their own reasons, so nothing is prompted for or stored here.

    The skills hub has a richer resolver, but it reads secrets through a Skill role scope and
    cannot run outside that subsystem; duplicating two tiers is cheaper than lending this
    command a scope it has no other use for.
    """
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value, name
    try:
        found = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None, ""
    token = found.stdout.strip()
    return (token, "gh auth token") if found.returncode == 0 and token else (None, "")


def _api(path: str) -> tuple[dict | None, str | None]:
    """``(payload, failure)``. A private repository answers 404 to an anonymous caller, which
    is worth telling apart from being offline: one is fixable with a token, the other is not."""
    import urllib.error
    import urllib.request

    from misaka.ai.utils.pi_user_agent import getPiUserAgent
    token, _source = github_token()
    headers = {"User-Agent": getPiUserAgent(), "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{API}{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.load(response), None
    except urllib.error.HTTPError as error:
        if error.code == 404:
            # GitHub answers 404 for a repository it will not show and for a commit it does
            # not have, without saying which; the message has to hold either way.
            return None, ("the repository is private or gone, and no token was found"
                          if not token else "the repository or that commit is not visible to this token")
        if error.code in (401, 403):
            remaining = error.headers.get("x-ratelimit-remaining") if error.headers else None
            if remaining == "0":
                return None, "GitHub's rate limit is used up; set GITHUB_TOKEN to raise it"
            return None, f"GitHub refused the request ({error.code})"
        return None, f"GitHub answered {error.code}"
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        # Offline or the host is unreachable. An update check is never a reason to fail the
        # command that asked for it.
        return None, "the network is unreachable"


def _bundle_update(install: Install) -> int:
    """A release archive is not on the branch: it is a release, replaced by the next one.

    Running pip here, as the advice for a built package says, would install into whatever
    Python is on PATH and leave this archive exactly as it was."""
    import re
    _report(install, None, None)
    latest, failure = _api("/releases/latest")
    tag = str((latest or {}).get("tag_name") or "").lstrip("v")

    def order(version):
        return tuple(int(part) for part in re.findall(r"\d+", version)[:3])
    if failure or not tag:
        ui.print_info("", f"Could not ask GitHub for the latest release ({failure or 'no tag'}).")
    elif order(tag) == order(install.version):
        ui.print_success(f"v{install.version} is the latest release.")
        return 0
    elif order(tag) < order(install.version):
        # A pre-release is not "latest" on GitHub until it is published.
        ui.print_success(f"v{install.version} is newer than the latest release (v{tag}).")
        return 0
    else:
        ui.print_info("", f"v{tag} is out; this is v{install.version}.")
    if install.installer in PACKAGE_UPGRADE:
        ui.print_info("", f"{install.installer} installed this release; it updates it:",
                      f"  {PACKAGE_UPGRADE[install.installer]}",
                      "Settings, credentials and research stay where they are.")
        return 0
    ui.print_info("", "A release archive is updated by installing the new one -- the installer replaces it:",
                  f"  {INSTALL_PS1 if os.name == 'nt' else INSTALL_SH}",
                  "Settings, credentials and research stay where they are.")
    return 0


def _behind(base: str, head: str = BRANCH) -> tuple[int | None, str | None]:
    """``(commits ``head`` is ahead of base, why not)``, via one compare call."""
    data, failure = _api(f"/compare/{base}...{head}")
    if data is None:
        return None, failure
    if data.get("status") not in {"ahead", "identical"}:
        return None, f"this install's commit is not an ancestor of {head}"
    ahead = data.get("ahead_by")
    return (ahead, None) if isinstance(ahead, int) else (None, "GitHub did not report a distance")


def _upstream(path: Path) -> str | None:
    """The branch of ``origin`` the checkout's branch tracks, or None (no upstream, or another remote's)."""
    try:
        upstream = _git(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return None
    remote, _, branch = upstream.partition("/")
    return branch if remote == "origin" and branch else None


def _checkout_state(install: Install) -> dict:
    """What a checkout can say about itself, and whether it is safe to fast-forward -- along the
    branch of origin it tracks, else main."""
    path = install.path
    assert path is not None
    state: dict = {"head": None, "remote": None, "behind": None, "dirty": None, "reason": None, "follows": BRANCH}
    try:
        state["head"] = _git(path, "rev-parse", "HEAD")
        state["dirty"] = bool(_git(path, "status", "--porcelain"))
        branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        state["reason"] = str(error)
        return state
    follows = state["follows"] = _upstream(path) or BRANCH
    remote = _remote_head_via_git(path, follows)
    state["remote"] = remote
    if remote is None:
        state["reason"] = "the remote could not be reached"
        return state
    if remote == state["head"]:
        state["behind"] = 0
        return state
    # `--ff-only` is the whole safety model: it is a fast-forward or it is nothing.
    try:
        _git(path, "fetch", "origin", follows, "--quiet")
        can_ff = subprocess.run(["git", "-C", str(path), "merge-base", "--is-ancestor", "HEAD", remote],
                                capture_output=True, check=False).returncode == 0
        state["behind"] = int(_git(path, "rev-list", "--count", f"HEAD..{remote}") or 0)
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as error:
        state["reason"] = str(error)
        return state
    if not can_ff:
        state["reason"] = f"this checkout has commits {follows} does not; a fast-forward is not possible"
    elif state["dirty"]:
        state["reason"] = "the working tree has uncommitted changes"
    elif branch == "HEAD":
        state["reason"] = "the checkout is on a detached HEAD"
    return state


def _install_commands(install: Install) -> list[list[str]] | None:
    """What reinstalls this install in place, keeping its extras, or None when nothing safely can.

    uv and pipx upgrade a tool from their own record of it, extras included, and follow a git
    source to its new head. pip keeps no record, and it does not reinstall a git requirement whose
    version string has not moved, so it takes two steps: the package alone, forced, and then
    whatever its new metadata asks for, with the extras found installed."""
    if install.kind == "wheel":
        return None
    if install.installer == "uv tool":
        return [["uv", "tool", "upgrade", "misaka"]]
    if install.installer == "pipx":
        return [["pipx", "upgrade", "misaka"]]
    extras = installed_extras()
    wanted = f"[{','.join(extras)}]" if extras else ""
    if install.kind == "git":
        source = f"misaka{wanted} @ {install.source}"
        if install.installer == "uv":
            return [["uv", "pip", "install", "--python", sys.executable, "--reinstall-package", "misaka", source]]
        return [[sys.executable, "-m", "pip", "install", "--force-reinstall", "--no-deps", f"misaka @ {install.source}"],
                [sys.executable, "-m", "pip", "install", source]]
    if install.kind == "checkout" and install.path is not None:
        # Dependencies move with the code; the pull alone leaves the environment behind.
        if install.installer == "uv":
            if (install.path / "uv.lock").exists() and Path(sys.prefix) == install.path / ".venv":
                return [["uv", "sync", *[f"--extra={extra}" for extra in extras]]]
            return [["uv", "pip", "install", "--python", sys.executable, "-e", f".{wanted}"]]
        return [[sys.executable, "-m", "pip", "install", "-e", f".{wanted}"]]
    return None


def adding_extras(install: Install, adding: list[str]) -> list[str] | None:
    """The command that gives this install ``adding`` as well, made the way the install was and
    keeping every extra it holds; None for a built package of unknown origin."""
    extras = ",".join(sorted({*installed_extras(), *adding}))
    if install.kind == "checkout" and install.path is not None:
        target = ["-e", f"{install.path}[{extras}]"]
    elif install.kind == "git":
        target = [f"misaka[{extras}] @ {install.source}"]      # the revision it follows, not main
    else:
        return None
    return {"uv tool": ["uv", "tool", "install", "--force", *target],
            "pipx": ["pipx", "install", "--force", *target],
            "uv": ["uv", "pip", "install", "--python", sys.executable, *target],
            }.get(install.installer, [sys.executable, "-m", "pip", "install", *target])


def _starts() -> str | None:
    """Why the code now installed cannot start, or None. The import is the one every command makes."""
    try:
        result = subprocess.run([sys.executable, "-c", "import misaka.cli.app"], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", check=False, timeout=300)
    except (OSError, subprocess.SubprocessError) as error:
        return str(error)
    if result.returncode == 0:
        return None
    lines = (result.stderr or result.stdout).strip().splitlines()
    return lines[-1] if lines else f"exit {result.returncode}"


def _launchers() -> list[Path]:
    """Windows: the ``misaka.exe`` launchers an upgrade rewrites and a running ``misaka`` holds
    open -- the ones uv's receipt lists (a panel open in another window runs those too) and the
    one this command was started through."""
    import tomllib

    import psutil
    found: set[Path] = set()
    try:
        receipt = tomllib.loads((Path(sys.prefix) / "uv-receipt.toml").read_text(encoding="utf-8-sig"))
        found.update(Path(entry["install-path"]) for entry in receipt["tool"].get("entrypoints", []))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    try:
        found.update(Path(parent.exe()) for parent in psutil.Process().parents()
                     if parent.name().lower() == "misaka.exe")
    except psutil.Error:
        pass
    return sorted(path for path in found if path.is_file())


def _set_aside(launchers: list[Path]) -> list[tuple[Path, Path]]:
    """Rename each launcher out of the way, so the upgrade can write its replacement.

    Windows will not overwrite a running executable, and uv then fails on the entrypoint after the
    package itself has been replaced; it will rename one, which is how uv's own self-update gets
    past the same lock (the self-replace crate). A launcher an earlier update set aside goes now,
    unless something still runs it."""
    moved = []
    for launcher in launchers:
        for stale in launcher.parent.glob(f"{launcher.name}.*.old"):
            with contextlib.suppress(OSError):
                stale.unlink()
        aside = launcher.with_name(f"{launcher.name}.{os.getpid()}.old")
        try:
            launcher.rename(aside)
        except OSError:
            continue
        moved.append((launcher, aside))
    return moved


def _put_back(moved: list[tuple[Path, Path]]) -> None:
    """Drop each launcher the upgrade replaced, or restore the one it did not write (an upgrade
    that failed first, or had nothing to do). One still running stays set aside until next time."""
    for launcher, aside in moved:
        with contextlib.suppress(OSError):
            if launcher.exists():
                aside.unlink()
            else:
                aside.rename(launcher)


def _run_all(commands: list[list[str]], cwd: Path | None) -> str | None:
    """Run each command in turn; the first failure, or None."""
    moved = _set_aside(_launchers()) if os.name == "nt" else []
    try:
        for command in commands:
            ui.print_info(ui.color("  " + shlex.join(command), ui.DIM))
            try:
                result = subprocess.run(command, cwd=str(cwd) if cwd else None, check=False)
            except OSError as error:
                return f"{command[0]} could not start: {error}"
            if result.returncode != 0:
                return f"{command[0]} exited with {result.returncode}"
        return None
    finally:
        _put_back(moved)


def _restore_command(install: Install) -> str:
    """How to put a non-checkout install back on the commit it had, when there was one."""
    if not install.commit:
        return ""
    extras = installed_extras()
    wanted = f"[{','.join(extras)}]" if extras else ""
    source = f"'misaka{wanted} @ git+{REPO_URL}@{install.commit}'"
    return {"uv tool": f"uv tool install --force {source}",
            "pipx": f"pipx install --force {source}",
            "uv": f"uv pip install --python {sys.executable} --reinstall-package misaka {source}",
            }.get(install.installer, f"{sys.executable} -m pip install --force-reinstall {source}")


def _report(install: Install, state: dict | None, behind: int | None, follows: str = BRANCH) -> None:
    shape = {"checkout": "a git checkout" + (" (editable)" if install.editable else ""),
             "git": f"installed from {REPO_URL}",
             "wheel": "installed from a built package",
             "bundle": "installed from a release archive"}[install.kind]
    ui.print_check(True, "installed", f"v{install.version}   {shape}"
                   + (f", by {install.installer}" if install.installer else ""))
    if install.path:
        ui.print_check(True, "source", ui.tilde(str(install.path)))
    if install.commit:
        ui.print_check(True, "installed commit", install.commit[:12])
    if state and state.get("head"):
        ui.print_check(True, "checkout head", state["head"][:12] + ("   (uncommitted changes)" if state.get("dirty") else ""))
    if install.kind != "checkout":
        # Only the API path needs one; a checkout asks git, which brings its own credentials.
        _token, source = github_token()
        ui.print_check(bool(_token) or None, "github token",
                       f"from {source}" if source else "none found; only a public repository can be checked")
    if install.kind == "bundle":
        return                              # a release, compared with releases (_bundle_update)
    if behind is None:
        ui.print_check(None, follows, "could not be compared" + (f": {state['reason']}" if state and state.get("reason") else ""))
    elif behind == 0:
        ui.print_success(f"Up to date with {follows}.")
    else:
        ui.print_warning(f"{behind} commit(s) behind {follows}.")


def run(*, apply: bool = False) -> int:
    ui.print_header("Update")
    install = describe()
    if install.kind == "bundle":
        return _bundle_update(install)
    state = _checkout_state(install) if install.kind == "checkout" else None
    follows = (state or {}).get("follows") or install.follows
    if follows == BRANCH:
        ui.print_info(f"Tracking the {BRANCH} branch of {REPO}, the way Hermes tracks its own:",
                      "a fast-forward or nothing. Releases are cut rarely; the branch is the product.", "")
    else:
        ui.print_info(f"Tracking {follows} of {REPO}, which this install was made from:",
                      "a fast-forward or nothing.", "")
    if state is not None:
        behind = state.get("behind")
    elif install.commit:
        behind, failure = _behind(install.commit, follows)
        if failure:
            state = {"reason": failure}
    else:
        behind = None
    _report(install, state, behind, follows)

    commands = _install_commands(install)
    if behind == 0 and not apply:
        return 0
    if commands is None:
        ui.print_info("", "This install was not made from the repository, so there is nothing to pull into.",
                      f"  pip install --upgrade 'misaka @ git+{REPO_URL}'")
        return 0

    blocked = state.get("reason") if state else None
    if blocked:
        ui.print_error(f"Cannot update in place: {blocked}.")
        ui.print_info("Resolve it in the checkout and run this again; nothing here will do it for you."
                      if install.kind == "checkout" else "Nothing was changed; run this again once it can be compared.")
        return 1

    steps = ([f"git -C {ui.tilde(str(install.path))} pull --ff-only origin {follows}"] if install.kind == "checkout" else []) \
        + [shlex.join(command) for command in commands]
    ui.print_info("", "Updating would run:", *[f"  {step}" for step in steps])
    if not apply:
        ui.print_info("", "`misaka update --apply` runs it.")
        return 0

    blocking, open_panes = running_work()
    if blocking:
        ui.print_error("Work is running that an update would cut off:")
        ui.print_info(*[f"    {item}" for item in blocking], "",
                      "Let it finish, or stop it (`/research stop` in the run's Last Order window; a card from",
                      "the panel), then run this again.")
        return 1

    try:
        if prompt_choice("Run it now?", ["No, leave this install alone", "Yes, update"], 0) != 1:
            ui.print_info("Nothing was changed.")
            return 1
    except (SetupCancelled, SetupGoBack):
        print()
        ui.print_info("Nothing was changed.")
        return 1

    import sqlite3

    from misaka.cli import backup
    try:
        ui.print_success(f"Saved settings, credentials and the boards to {ui.tilde(str(backup.snapshot('update')))}")
    except (OSError, sqlite3.Error) as error:      # best-effort, as Hermes's is: warned, never fatal
        ui.print_warning(f"Could not save a copy of the home first ({error}); updating anyway.")
    if not open_panes:
        stop_daemon()

    before = state.get("head") if state else None
    if install.kind == "checkout" and install.path is not None:
        try:
            _git(install.path, "pull", "--ff-only", "origin", follows)
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            ui.print_error(f"git pull failed: {error}")
            return 1
        ui.print_success(f"Fast-forwarded to {follows}.")
    failure = _run_all(commands, install.path) or _starts()
    if failure:
        ui.print_error(f"The update did not finish: {failure}.")
        if install.kind == "checkout" and install.path is not None and before:
            ui.print_info(f"Putting the checkout back on {before[:12]}.")
            try:
                _git(install.path, "reset", "--hard", before)
            except (RuntimeError, OSError, subprocess.SubprocessError) as error:
                ui.print_error(f"git reset failed: {error}")
                return 1
            again = _run_all(commands, install.path) or _starts()
            (ui.print_error if again else ui.print_success)(
                f"The checkout is back on {before[:12]}" + (f", but it still does not start: {again}." if again else ", as it was."))
        elif _restore_command(install):
            ui.print_info("To put it back on the commit it had:", f"  {_restore_command(install)}")
        return 1
    ui.print_success("Updated.")
    if open_panes:
        ui.print_info("", f"The panel is still open with {len(open_panes)} session(s) on the old code. Close them,",
                      "then run `misaka net stop` so the panel comes back on the new code.")
    else:
        ui.print_info("", "Restart MISAKA if it is running: this process is still on the old code.")
    return 0

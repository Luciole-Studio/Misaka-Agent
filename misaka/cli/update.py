"""``misaka update``: is this install behind the repository, and what would bring it level.

Shaped after how Hermes updates itself (``hermes-lcm/scripts/update.sh``): follow the
branch head rather than release tags, fast-forward only, and never guess. Its install
script sets the tone for the rest -- a preflight that refuses on anything unexpected and
prints the manual fix instead of clobbering -- so this command refuses on a dirty tree, a
diverged branch, or an install shape it cannot vouch for, and says what to run by hand.

Checking and applying are separate, as they are in the Hermes skills hub (``hub-update-check``
against ``hub-update``). Nothing here runs on its own: no background poll, no footer nag. The
one ambient mention is the wizard's environment section, which already reports the state of
this machine.

How the install was made is read, not guessed: PEP 610 writes ``direct_url.json`` into the
dist-info, and ``INSTALLER`` records which tool did it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from misaka.cli import setup_ui as ui
from misaka.cli.setup_ui import SetupCancelled, prompt_choice

REPO = "Luciole-Studio/Misaka-Agent"
REPO_URL = f"https://github.com/{REPO}.git"
BRANCH = "main"
API = f"https://api.github.com/repos/{REPO}"
TIMEOUT = 10


@dataclass(frozen=True)
class Install:
    """Where this install's code comes from, and what would update it."""

    kind: str                 # "checkout" | "git" | "wheel"
    editable: bool
    installer: str            # pip | uv | pipx | ""
    path: Path | None         # the checkout, when there is one
    commit: str | None        # the commit a git install pinned
    version: str


def describe() -> Install:
    from importlib.metadata import PackageNotFoundError, distribution

    from misaka.config import VERSION
    try:
        dist = distribution("misaka")
    except PackageNotFoundError:
        return Install("wheel", False, "", None, None, VERSION)
    installer = (dist.read_text("INSTALLER") or "").strip()
    try:
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
    except ValueError:
        direct = {}
    url = str(direct.get("url") or "")
    vcs = direct.get("vcs_info") or {}
    if vcs.get("vcs") == "git":
        return Install("git", False, installer, None, vcs.get("commit_id"), dist.version)
    if url.startswith("file://"):
        path = Path(url[len("file://"):])
        editable = bool((direct.get("dir_info") or {}).get("editable"))
        if (path / ".git").exists():
            return Install("checkout", editable, installer, path, None, dist.version)
        return Install("wheel", editable, installer, path, None, dist.version)
    return Install("wheel", False, installer, None, None, dist.version)


def _git(path: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(path), *args],
                            capture_output=True, text=True, check=False, timeout=60)
    if check and result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _remote_head_via_git(path: Path) -> str | None:
    """The branch head straight from the remote. No API, no token, no rate limit."""
    try:
        line = _git(path, "ls-remote", "origin", f"refs/heads/{BRANCH}")
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
        found = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True,
                               timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None, ""
    token = found.stdout.strip()
    return (token, "gh auth token") if found.returncode == 0 and token else (None, "")


def _api(path: str) -> tuple[dict | None, str | None]:
    """``(payload, failure)``. A private repository answers 404 to an anonymous caller, which
    is worth telling apart from being offline: one is fixable with a token, the other is not."""
    import urllib.error
    import urllib.request

    from misaka.ai.utils.user_agent import get_misaka_user_agent
    token, _source = github_token()
    headers = {"User-Agent": get_misaka_user_agent(), "Accept": "application/vnd.github+json"}
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


def _behind(base: str) -> tuple[int | None, str | None]:
    """``(commits ahead of base, why not)``, via one compare call."""
    data, failure = _api(f"/compare/{base}...{BRANCH}")
    if data is None:
        return None, failure
    if data.get("status") not in {"ahead", "identical"}:
        return None, f"this install's commit is not an ancestor of {BRANCH}"
    ahead = data.get("ahead_by")
    return (ahead, None) if isinstance(ahead, int) else (None, "GitHub did not report a distance")


def _checkout_state(install: Install) -> dict:
    """What a checkout can say about itself, and whether it is safe to fast-forward."""
    path = install.path
    assert path is not None
    state: dict = {"head": None, "remote": None, "behind": None, "dirty": None, "reason": None}
    try:
        state["head"] = _git(path, "rev-parse", "HEAD")
        state["dirty"] = bool(_git(path, "status", "--porcelain"))
        branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        state["reason"] = str(error)
        return state
    remote = _remote_head_via_git(path)
    state["remote"] = remote
    if remote is None:
        state["reason"] = "the remote could not be reached"
        return state
    if remote == state["head"]:
        state["behind"] = 0
        return state
    # `--ff-only` is the whole safety model: it is a fast-forward or it is nothing.
    try:
        _git(path, "fetch", "origin", BRANCH, "--quiet")
        can_ff = subprocess.run(["git", "-C", str(path), "merge-base", "--is-ancestor", "HEAD", remote],
                                capture_output=True, check=False).returncode == 0
        state["behind"] = int(_git(path, "rev-list", "--count", f"HEAD..{remote}") or 0)
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as error:
        state["reason"] = str(error)
        return state
    if not can_ff:
        state["reason"] = f"this checkout has commits {BRANCH} does not; a fast-forward is not possible"
    elif state["dirty"]:
        state["reason"] = "the working tree has uncommitted changes"
    elif branch == "HEAD":
        state["reason"] = "the checkout is on a detached HEAD"
    return state


def _install_command(install: Install) -> list[str] | None:
    """The command that reinstalls this install in place, or None when there is no safe one."""
    if install.kind == "git":
        requirement = f"misaka @ git+{REPO_URL}"
        if install.installer == "uv":
            return ["uv", "tool", "install", "--force", requirement]
        if install.installer == "pipx":
            return ["pipx", "install", "--force", requirement]
        return [sys.executable, "-m", "pip", "install", "--upgrade", requirement]
    if install.kind == "checkout" and install.path is not None:
        # Dependencies move with the code; the pull alone leaves the environment behind.
        if install.installer == "uv" and (install.path / "uv.lock").exists():
            return ["uv", "sync"]
        return [sys.executable, "-m", "pip", "install", "-e", "."]
    return None


def _report(install: Install, state: dict | None, behind: int | None) -> None:
    shape = {"checkout": "a git checkout" + (" (editable)" if install.editable else ""),
             "git": f"installed from {REPO_URL}",
             "wheel": "installed from a built package"}[install.kind]
    ui.print_check(True, "installed", f"v{install.version}   {shape}"
                   + (f", by {install.installer}" if install.installer else ""))
    if install.path:
        ui.print_check(True, "source", ui.tilde(str(install.path)))
    if install.commit:
        ui.print_check(True, "pinned commit", install.commit[:12])
    if state and state.get("head"):
        ui.print_check(True, "checkout head", state["head"][:12] + ("   (uncommitted changes)" if state.get("dirty") else ""))
    if install.kind != "checkout":
        # Only the API path needs one; a checkout asks git, which brings its own credentials.
        _token, source = github_token()
        ui.print_check(bool(_token) or None, "github token",
                       f"from {source}" if source else "none found; only a public repository can be checked")
    if behind is None:
        ui.print_check(None, BRANCH, "could not be compared" + (f": {state['reason']}" if state and state.get("reason") else ""))
    elif behind == 0:
        ui.print_success(f"Up to date with {BRANCH}.")
    else:
        ui.print_warning(f"{behind} commit(s) behind {BRANCH}.")


def run(*, apply: bool = False) -> int:
    ui.print_header("Update")
    install = describe()
    ui.print_info(f"Tracking the {BRANCH} branch of {REPO}, the way Hermes tracks its own:",
                  "a fast-forward or nothing. Releases are cut rarely; the branch is the product.", "")

    state = _checkout_state(install) if install.kind == "checkout" else None
    if state is not None:
        behind = state.get("behind")
    elif install.commit:
        behind, failure = _behind(install.commit)
        if failure:
            state = {"reason": failure}
    else:
        behind = None
    _report(install, state, behind)

    command = _install_command(install)
    if behind == 0 and not apply:
        return 0
    if command is None:
        ui.print_info("", "This install was not made from the repository, so there is nothing to pull into.",
                      f"  pip install --upgrade 'misaka @ git+{REPO_URL}'")
        return 0

    blocked = state.get("reason") if state else None
    if blocked:
        ui.print_error(f"Cannot update in place: {blocked}.")
        ui.print_info("Resolve it in the checkout and run this again; nothing here will do it for you.")
        return 1

    steps = ([f"git -C {ui.tilde(str(install.path))} pull --ff-only origin {BRANCH}"] if install.kind == "checkout" else []) \
        + [" ".join(command)]
    ui.print_info("", "Updating would run:", *[f"  {step}" for step in steps])
    if not apply:
        ui.print_info("", "`misaka update --apply` runs it.")
        return 0

    try:
        if prompt_choice("Run it now?", ["No, leave this install alone", "Yes, update"], 0) != 1:
            ui.print_info("Nothing was changed.")
            return 1
    except SetupCancelled:
        print()
        ui.print_info("Nothing was changed.")
        return 1

    from misaka.cli.uninstall import _stop_daemon
    _stop_daemon()
    if install.kind == "checkout" and install.path is not None:
        try:
            _git(install.path, "pull", "--ff-only", "origin", BRANCH)
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            ui.print_error(f"git pull failed: {error}")
            return 1
        ui.print_success(f"Fast-forwarded to {BRANCH}.")
    ui.print_info(ui.color("  " + " ".join(command), ui.DIM))
    try:
        result = subprocess.run(command, cwd=str(install.path) if install.path else None, check=False)
    except OSError as error:
        ui.print_error(f"{command[0]} could not start: {error}")
        return 1
    if result.returncode != 0:
        ui.print_error(f"{command[0]} exited with {result.returncode}; the code is updated but the environment may not be.")
        return 1
    ui.print_success("Updated.")
    ui.print_info("", "Restart MISAKA if it is running: this process is still on the old code,",
                  "and the panel daemon was stopped so it comes back on the new one.")
    return 0

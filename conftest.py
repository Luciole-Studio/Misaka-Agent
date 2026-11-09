"""Every test, under tests/ or beside the code it covers, runs against a throwaway home.

``MISAKA_HOME`` is the one knob the product has, so it is the one thing to redirect. It is
assigned (not ``setdefault``): a developer's exported value must not get a vote, or the suite
runs against a live board. The directory is short on purpose -- a unix socket path is capped
at 104 bytes on macOS, and pytest's own ``tmp_path`` is already most of that.
"""
from __future__ import annotations

import os
import shutil
import tempfile

import pytest

# Module-level constants and import-time lookups land here rather than in the real home.
_SESSION_HOME = tempfile.mkdtemp(prefix="mh-", dir="/tmp" if os.path.isdir("/tmp") else None)
os.environ["MISAKA_HOME"] = _SESSION_HOME
os.environ["MISAKA_OFFLINE"] = "1"
# Every Python process `misaka` starts on Windows runs in UTF-8 mode (misaka/windows_bootstrap.py
# sets it for them); the children a test starts get the same, not the ANSI code page of this one.
if os.name == "nt":
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


@pytest.fixture(autouse=True)
def misaka_home(monkeypatch):
    """A fresh home for each test, removed afterwards.

    Whatever the code under test wrote into it has to be something the layout table declares:
    a stray name here is a path that bypassed the table, which is how ``~/.misaka`` filled up
    with things nobody could account for.
    """
    root = tempfile.mkdtemp(prefix="mh-", dir=os.path.dirname(_SESSION_HOME))
    monkeypatch.setenv("MISAKA_HOME", root)
    yield root
    from misaka.config import home

    monkeypatch.setenv("MISAKA_HOME", root)          # a test may have pointed it elsewhere
    strays = home.strays()
    shutil.rmtree(root, ignore_errors=True)
    assert not strays, f"written into the home without a row in misaka.config.home.LAYOUT: {strays}"


@pytest.fixture(autouse=True)
def unwatched_loops():
    """A test that runs a command entry in this process (``app._cmd_research``) configures the
    loop watchdog for it; the next test must not inherit that, nor have its stalls dumped into a
    home that is gone (and ``faulthandler``'s one timer is pytest's too)."""
    yield
    from misaka.utils import loop_watchdog
    if loop_watchdog._settings is not None:
        import faulthandler
        faulthandler.cancel_dump_traceback_later()
        loop_watchdog._settings, loop_watchdog._depth = None, 0
        if loop_watchdog._file is not None:
            loop_watchdog._file.close()
            loop_watchdog._file = None


@pytest.fixture(autouse=True)
def default_tui_keybindings():
    """Each test starts with the TUI's default keybindings, as each of pi's test files does in its own process: an
    interactive session under test installs the app's keybindings globally (``setKeybindings``), and on Windows those
    bind undo differently."""
    from misaka.ui.tui import keybindings

    keybindings.globalKeybindings = None
    yield


@pytest.fixture(autouse=True)
def fresh_shared_auth_read_state():
    """Each test starts with no auth.json read state shared between stores, as each of pi's test files does in its own
    process: the first store a test builds on a path claims the shared slot (``_sharedAuthFileReadState``)."""
    from misaka.core import auth_storage

    auth_storage._sharedAuthFileReadState = None
    yield


@pytest.fixture(autouse=True)
def default_http_dispatcher():
    """Each test starts with the default HTTP idle timeout: a session under test configures the process-wide
    dispatcher from its settings (``configureHttpDispatcher``), as pi's does in its own process."""
    from misaka.core import http_dispatcher

    http_dispatcher.configureHttpDispatcher()
    yield


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_SESSION_HOME, ignore_errors=True)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """A test that needs a symlink this Windows account may not create (no Developer Mode, not
    elevated: ERROR_PRIVILEGE_NOT_HELD) is skipped rather than failed, as herdr's suite does."""
    report = yield
    error = call.excinfo.value if call.excinfo is not None else None
    if isinstance(error, OSError) and getattr(error, "winerror", None) == 1314:
        report.outcome = "skipped"
        report.longrepr = (str(item.path), item.location[1] or 0,
                           "Skipped: creating a symlink needs Developer Mode on Windows")
    return report


@pytest.fixture(autouse=True, scope="session")
def card_owners():
    """Research owns its cards in every test process, as ``misaka.cli.bootstrap`` makes it in every
    real one."""
    from misaka.core.research import owner

    owner.install()

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


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_SESSION_HOME, ignore_errors=True)

"""Which LCM implementation serves this install, and whether the database fits it.

Two implementations share one file name (``~/.misaka/lcm.db``) and cannot share a file:
the pre-port mini one and the ported upstream engine both call their tables ``messages``
and ``summary_nodes`` while meaning different things by them. So the switch is not only
"which code runs" but "does the database on disk match the code that is about to open
it" -- opening the wrong one does not fail loudly, it quietly grows a hybrid schema that
neither implementation can read.

Deliberately dependency-light: the extension loader consults this on every session, and
must not have to import the engine, its config bridge and misaka's provider stack to
learn that this install is not using them.
"""

from __future__ import annotations

import os
import sqlite3

from misaka.config import CFG

# The `context_engine` value that hands compaction to the vendored engine. `lcm` stays
# the pre-port mini implementation and `native` stays the escape hatch, so opting in is
# one environment variable and backing out is the same variable again.
ENGINE_VALUE = "hermes-lcm"


def selected() -> bool:
    """Whether this install has asked the vendored engine to serve compaction."""
    return str(CFG.get("context_engine") or "").strip().lower() == ENGINE_VALUE


def misaka_database_path() -> str:
    """The database misaka's own settings name, before any upstream override."""
    return os.path.expanduser(str(CFG.get("lcm_db") or "~/.misaka/lcm.db"))


def database_path() -> str:
    """The database in use: upstream's own environment name first, then misaka's."""
    return os.environ.get("LCM_DATABASE_PATH") or misaka_database_path()


def schema(db_path: str) -> str:
    """``"mini"``, ``"ported"``, or ``""`` when there is nothing to tell apart yet.

    Upstream's ``messages`` table carries a ``conversation_id`` column and the mini
    implementation's never did, which makes one column the whole discriminator -- no
    guessing from table names the two happen to share.
    """
    if not os.path.isfile(db_path):
        return ""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return ""
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "messages" not in tables:
            return ""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    except sqlite3.Error:
        return ""
    finally:
        conn.close()
    return "ported" if "conversation_id" in columns else "mini"

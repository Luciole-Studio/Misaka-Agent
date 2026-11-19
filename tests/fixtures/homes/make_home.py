"""Write the sample home a release leaves on a user's machine, for tests/test_upgrade_from_releases.py.

    .venv/bin/python tests/fixtures/homes/make_home.py 0.18.0

Run it at the release commit, with that release's code: what it writes is what a user of that
release has on disk (a board with a card and a finished research run, a queued message, a
conversation, settings, a Sister, a project), and every later build must open it. That is the
release gate of docs/plans/upgrade-install-uninstall-2026-09-29.md. Until a release is tagged,
regenerating its sample is how a format change made before the release is recorded; after it,
a format change ships its migration instead, and the sample never changes again.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEFT_OUT = ("cache", "logs", "run")


def build(version: str) -> Path:
    scratch = Path(tempfile.mkdtemp(prefix="misaka-home-sample-"))
    os.environ["MISAKA_HOME"] = str(scratch / "home")
    sys.path.insert(0, str(HERE.parents[2]))

    from misaka.config import CFG, home, layout
    from misaka.core.network import messages, roster
    from misaka.core.platform import cards, tasks
    from misaka.core.research import runs
    from misaka.core.session_manager import SessionManager

    home.ensure()
    layout.ensure()
    home.path("settings").write_text(json.dumps({"defaultProvider": "anthropic", "defaultModel": "claude-sonnet-4-5"}, indent=2), encoding="utf-8")
    ok, note = roster.create_sister("10032", specialty="archival sources")
    assert ok, note
    project = scratch / "project"
    project.mkdir()
    cards.init_project(str(project), git=False)
    config = home.project_dir(project)
    config.mkdir(exist_ok=True)
    (config / "settings.json").write_text(json.dumps({"research": {"plan_approval": True}}, indent=2), encoding="utf-8")

    con = tasks.connect(CFG["db"])
    try:
        tasks.create_task(con, "Read the 1905 memorials", body="Summarise what they argue.",
                          assignee="10032", workspace=str(project))
        run = runs.create(con, workspace=str(project), question="Why was the examination system abolished in 1905?")
        con.execute("UPDATE research_runs SET status='done', phase='done' WHERE id=?", (run["id"],))
    finally:
        con.close()
    mail = messages.connect()
    try:
        messages.send(mail, "10032", "The memorials are in sources/.", sender="last-order")
    finally:
        mail.close()
    session = SessionManager.create(str(project), str(home.path("sessions") / "last_order"))
    session.appendMessage({"role": "user", "content": "What did the 1905 edict say?", "timestamp": 1})
    session.appendMessage({
        "role": "assistant", "content": [{"type": "text", "text": "It ended the examinations."}],
        "api": "anthropic-messages", "provider": "anthropic", "model": "claude-sonnet-4-5", "stopReason": "stop",
        "timestamp": 2, "usage": {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 2,
                                  "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}},
    })

    target = HERE / version
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(scratch / "home", target / "home", symlinks=True,
                    ignore=lambda folder, names: [name for name in names
                                                  if (Path(folder) == scratch / "home" and name in LEFT_OUT)
                                                  or name.endswith(("-wal", "-shm"))])
    shutil.copytree(project, target / "project", symlinks=True, ignore=shutil.ignore_patterns(".gitignore"))
    shutil.rmtree(scratch)
    return target


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print(build(sys.argv[1]))

"""A panel card pane that sits idle is the daemon's while the daemon lives: any reconcile pass used
to read its `net:` claim as no live claimer, take the idle pane for wedged, and kill its group --
a person chatting in a card window who stepped away for an hour lost it (0.18.10 sweep)."""
import os
import secrets
import socket
import subprocess
import sys
import time

import pytest

from misaka.core.network import dispatch
from misaka.core.platform import cards, processes, tasks


@pytest.mark.skipif(os.name == "nt", reason="a pane leads its own process group on POSIX")
def test_an_idle_panel_card_is_left_to_its_live_daemon(tmp_path):
    con = tasks.connect(str(tmp_path / "board.db"))
    tid = cards.create(con, str(tmp_path), "T", "body", "10032")
    lock = f"net:{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"
    assert tasks.claim(con, tid, lock, generation=1, pid=os.getpid())
    pane = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        identity = processes.identity(pane.pid)
        assert tasks.set_pid(con, tid, pane.pid, worker_identity=f"process-group|{identity}", generation=1,
                             claim_lock=lock)
        con.execute("UPDATE tasks SET heartbeat_at=? WHERE id=?", (int(time.time()) - 7200, tid))
        con.commit()
        assert tasks.heartbeat(con, tid, lock, generation=1, progress=False)
        dispatch.reconcile(con, {})
        assert pane.poll() is None and tasks.get(con, tid)["status"] == "running"
    finally:
        pane.kill()
        pane.wait()
        con.close()

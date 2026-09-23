"""Putting an index row back to what its card file says is on the record.

2026-09-18 (B24): Last Order edited `tasks.status` by SQL twice and saw it undone twice, with
no trace of who did it, because `reconcile_one` mirrors the file silently. The file is still
the contract and still wins; the put-back now leaves a `reconciled` event."""
from misaka.core.platform import cards, tasks

BODY = "## deliverable\nnotes.md\n"


def _events(con, task_id):
    return [(row["kind"], row["payload"]) for row in
            con.execute("SELECT kind, payload FROM events WHERE task_id=? ORDER BY id", (task_id,))]


def test_a_hand_edited_status_is_put_back_with_a_record(tmp_path, request):
    con = tasks.connect(str(tmp_path / "board.db"))
    request.addfinalizer(con.close)
    tid = cards.create(con, str(tmp_path), "T4 crossover", BODY, "10032")
    assert tasks.mark_stopped(con, tid)
    con.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))       # what Last Order did
    con.commit()
    assert cards.reconcile_one(con, str(tmp_path), tid) is not None
    assert tasks.get(con, tid)["status"] == "stopped"
    kinds = [kind for kind, _ in _events(con, tid)]
    assert kinds.count("reconciled") == 1
    payload = next(payload for kind, payload in _events(con, tid) if kind == "reconciled")
    assert '"from": "ready"' in payload and '"to": "stopped"' in payload and "card file" in payload


def test_an_agreeing_row_leaves_no_event(tmp_path, request):
    con = tasks.connect(str(tmp_path / "board.db"))
    request.addfinalizer(con.close)
    tid = cards.create(con, str(tmp_path), "T4 crossover", BODY, "10032")
    before = _events(con, tid)
    assert cards.reconcile_one(con, str(tmp_path), tid) is not None
    assert _events(con, tid) == before
    assert "reconciled" not in [kind for kind, _ in _events(con, tid)]

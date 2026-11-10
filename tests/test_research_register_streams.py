"""A card that ran a crash experiment declared its scratch folder with its report: 1,183 files,
361 of them SQLite databases of 400-560 MB (2026-11-10, r_21c1c76e68). Registration read each
declared file whole to hash it, then decoded it to tell text from binary, so the node's Last Order
went from 170 MB to 8 GB within twenty seconds of the card settling. A declared file is now hashed
a piece at a time; its digest and its text-or-binary verdict are what the whole file gave."""
import hashlib
import json
import random
import tracemalloc
from contextlib import closing
from pathlib import Path

import pytest

from misaka.core.network import dispatch
from misaka.core.platform import cards, repo, tasks
from misaka.core.research import runs, workflow


def _whole(data):
    """What registration computed when it read the file in one piece."""
    try:
        data.decode("utf-8")
        binary = False
    except UnicodeDecodeError:
        binary = True
    return hashlib.sha256(data).hexdigest(), binary


CASES = {
    "empty": b"",
    "ascii": b"plain notes\n",
    "three-byte characters across pieces": "ab中文日志cd".encode(),
    "four-byte character across pieces": "x\U0001f600y\U0001f600".encode(),
    "byte order mark": b"\xef\xbb\xbfwith a mark\n",
    "nul bytes are still text": b"a\x00b\x00",
    "ends inside a character": "report 中".encode()[:-1],
    "ends inside a four-byte character": "ok \U0001f600".encode()[:-2],
    "invalid byte in the first piece": b"\xff starts badly",
    "invalid byte in a later piece": b"fine so far, then \xff and more",
    "lone continuation byte": b"abc\x80def",
    "overlong encoding": b"abc\xc0\xafdef",
    "encoded surrogate": b"abc\xed\xa0\x80def",
    "sqlite header": b"SQLite format 3\x00\x10\x00\x01\x01\x00\x40\x20\x20\xff\xfe",
}


@pytest.mark.parametrize("name", CASES)
@pytest.mark.parametrize("piece", [1, 2, 3, 4, 5, 7, 1024 * 1024])
def test_a_file_read_in_pieces_gives_the_whole_file_s_digest_and_verdict(tmp_path, monkeypatch, name, piece):
    data = CASES[name]
    path = tmp_path / "declared.bin"
    path.write_bytes(data)
    monkeypatch.setattr(workflow, "_DIGEST_CHUNK", piece)
    assert workflow._digest_and_kind(path) == _whole(data)


def test_random_text_and_bytes_agree_at_any_piece_size(tmp_path, monkeypatch):
    rng = random.Random(20261110)
    alphabet = "aé中\U0001f600\n "
    path = tmp_path / "declared.bin"
    for _ in range(300):
        if rng.random() < 0.5:
            data = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 40))).encode()
            if data and rng.random() < 0.3:
                data = data[:rng.randrange(0, len(data))]      # may now end inside a character
        else:
            data = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 40)))
        path.write_bytes(data)
        monkeypatch.setattr(workflow, "_DIGEST_CHUNK", rng.randrange(1, 9))
        assert workflow._digest_and_kind(path) == _whole(data), data


def test_a_large_declared_file_is_never_held_whole(tmp_path):
    """16 MB of text: read whole it was 16 MB of bytes and its decoded text beside them."""
    path = tmp_path / "run.log"
    line = "行 ok 一条日志 a line of the log\n".encode()
    with path.open("wb") as handle:
        for _ in range(16 * 1024 * 1024 // (len(line) * 256)):
            handle.write(line * 256)
    size = path.stat().st_size
    assert size > 15 * 1024 * 1024
    tracemalloc.start()
    try:
        digest, binary = workflow._digest_and_kind(path)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert not binary
    with path.open("rb") as handle:
        assert digest == hashlib.file_digest(handle, "sha256").hexdigest()
    assert peak < size / 2, f"held {peak} bytes while hashing a {size}-byte file"


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(repo, "enabled", lambda *a: False)
    monkeypatch.setattr(workflow, "_bundle", lambda *a, **k: None)
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        run = runs.create(con, workspace=str(tmp_path), question="Fixture question")
        yield con, run, runs.nodes(con, run["id"])[0]


def _accept(con, run, root, files):
    """A card accepted with ``files`` ({name in its folder: bytes}) declared, as a Sister's own
    process declares them (``dispatch.prepare_submission`` hashes there, in pieces already)."""
    tid = cards.create(con, run["workspace"], "card with large files", "fixture body", "fixture")
    runs.link_task(con, run["id"], tid, kind="research", node=root, local_id="large")
    task = tasks.get(con, tid)
    rels = []
    for name, data in files.items():
        path = Path(task["output_dir"], name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        rels.append(str(path.relative_to(run["workspace"])))
    con.execute("UPDATE tasks SET status='running',claim_lock='fixture',claim_expires=? WHERE id=?", (10**12, tid))
    assert dispatch.accept_state(con, tasks.get(con, tid), {"summary": "fixture", "artifacts": rels},
                                 generation=task["generation"], claim_lock="fixture")
    return tid


def test_settling_a_card_registers_its_files_without_reading_one_whole(state, monkeypatch):
    con, run, root = state
    files = {
        "report.md": "# 报告\n\nthe write-up\n".encode(),
        "exp/work/A_WAL_OFF_1.db": b"SQLite format 3\x00" + bytes(range(256)) * 9000,     # 2.3 MB, not text
        "exp/runA.log": "DELETE FULL ok 第一轮\n".encode() * 90000,                          # 2.4 MB of text
    }
    tid = _accept(con, run, root, files)
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail(f"read whole: {self}"))

    workflow.settle_done_tasks(con, run_id=run["id"])

    assert tasks.get(con, tid)["status"] == "done"
    assert tasks.latest_payload(con, tid, "research_artifact_drift", generation=1) is None
    registered = {Path(row["path"]).name: row for row in runs.artifacts(con, run["id"], task_id=tid)}
    assert set(registered) == {"report.md", "A_WAL_OFF_1.db", "runA.log"}
    for name, data in files.items():
        row = registered[Path(name).name]
        assert row["sha256"] == hashlib.sha256(data).hexdigest()
        assert json.loads(row["metadata_json"]).get("binary", False) is name.endswith(".db")

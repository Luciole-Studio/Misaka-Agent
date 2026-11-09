"""One content, one reading, whoever brings it and however many at once.

A document's ID is its content's hash, so the same book handed in twice ends as one document. Two
ingests at the same moment both found it missing, though, and both read it: two cards finishing
together paid for the whole OCR twice to keep one result. The second now waits and takes the first
one's reading."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from misaka.core.documents import index as corpus

PAGE = "第一頁的正文足夠長，足以算作這一頁確實讀到了文字。" * 3

REPO = Path(__file__).resolve().parent.parent

# A second process, because cards settle in processes of their own: a lock that only held inside
# one process would pass the threaded tests and read the book twice in a real run.
CARD = f"""
import glob, os, sys, time
from misaka.core.documents import index as corpus

workspace, path, tally, name = sys.argv[1:5]


def read(_path, meta=None):
    with open(tally, "a", encoding="utf-8") as f:
        f.write("read\\n")
    time.sleep(1)
    return [{PAGE!r}]


corpus.extract_pages = read
open(os.path.join(workspace, name + ".ready"), "w").close()
deadline = time.time() + 30
while len(glob.glob(os.path.join(workspace, "*.ready"))) < 2 and time.time() < deadline:
    time.sleep(0.01)
print(corpus.ingest(path, with_tree=False, task_id=name, workspace=workspace)[0])
"""


def book(folder, name, content=b"%PDF-1.4 the same scan\n"):
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return str(path)


def meta_of(workspace, doc_id):
    return json.loads((Path(corpus.corpus_root(str(workspace))) / doc_id / "meta.json").read_text(encoding="utf-8"))


def at_once(*jobs):
    """Run ``jobs`` together: what each returned, or the exception it raised, in order."""
    results = [None] * len(jobs)

    def run(index, job):
        try:
            results[index] = job()
        except Exception as error:  # noqa: BLE001 - the test reads what each one ended with
            results[index] = error

    threads = [threading.Thread(target=run, args=item) for item in enumerate(jobs)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads), "an ingest never came back"
    return results


def test_the_same_content_arriving_together_is_read_once(tmp_path, monkeypatch):
    reads = []

    def read(path, meta=None):
        reads.append(path)
        time.sleep(0.5)
        return [PAGE]

    monkeypatch.setattr(corpus, "extract_pages", read)
    first, second = book(tmp_path, "a/scan.pdf"), book(tmp_path, "b/same_scan.pdf")

    found = at_once(lambda: corpus.ingest(first, with_tree=False, task_id="t_a", workspace=str(tmp_path)),
                    lambda: corpus.ingest(second, with_tree=False, task_id="t_b", workspace=str(tmp_path)))

    assert len(reads) == 1
    assert found[0] == found[1]
    meta = meta_of(tmp_path, found[0][0])
    assert sorted(meta["paths"]) == sorted([first, second])
    assert sorted(meta["task_ids"]) == ["t_a", "t_b"]


def test_two_processes_handing_in_the_same_content_read_it_once(tmp_path):
    tally = tmp_path / "reads.txt"
    first, second = book(tmp_path, "a/scan.pdf"), book(tmp_path, "b/same_scan.pdf")
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    cards = [subprocess.Popen([sys.executable, "-c", CARD, str(tmp_path), path, str(tally), name],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
             for path, name in ((first, "t_a"), (second, "t_b"))]

    ended = [card.communicate(timeout=60) for card in cards]

    assert [card.returncode for card in cards] == [0, 0], ended
    assert tally.read_text(encoding="utf-8").count("read") == 1
    ids = {out.strip().splitlines()[-1] for out, _ in ended}
    assert len(ids) == 1
    assert sorted(meta_of(tmp_path, ids.pop())["task_ids"]) == ["t_a", "t_b"]


def test_different_content_does_not_wait_for_each_other(tmp_path, monkeypatch):
    both_reading = threading.Barrier(2, timeout=10)

    def read(_path, meta=None):
        both_reading.wait()              # breaks, and fails the ingest, if one waits for the other
        return [PAGE]

    monkeypatch.setattr(corpus, "extract_pages", read)
    first = book(tmp_path, "one.pdf", b"%PDF-1.4 one scan\n")
    second = book(tmp_path, "other.pdf", b"%PDF-1.4 another scan\n")

    found = at_once(lambda: corpus.ingest(first, with_tree=False, workspace=str(tmp_path)),
                    lambda: corpus.ingest(second, with_tree=False, workspace=str(tmp_path)))

    assert not [result for result in found if isinstance(result, Exception)], found
    assert found[0][0] != found[1][0]


def test_a_reading_that_fails_lets_the_one_waiting_try(tmp_path, monkeypatch):
    reads = []

    def read(path, meta=None):
        reads.append(path)
        time.sleep(0.3)
        return [] if len(reads) == 1 else [PAGE]

    monkeypatch.setattr(corpus, "extract_pages", read)
    first, second = book(tmp_path, "a/scan.pdf"), book(tmp_path, "b/same_scan.pdf")

    found = at_once(lambda: corpus.ingest(first, with_tree=False, workspace=str(tmp_path)),
                    lambda: corpus.ingest(second, with_tree=False, workspace=str(tmp_path)))

    assert len(reads) == 2
    assert sum(isinstance(result, ValueError) for result in found) == 1
    assert sum(isinstance(result, tuple) for result in found) == 1


def test_a_failed_reading_leaves_the_file_free_to_try_again(tmp_path, monkeypatch):
    path = book(tmp_path, "scan.pdf")
    monkeypatch.setattr(corpus, "extract_pages", lambda p, meta=None: [])
    with pytest.raises(ValueError):
        corpus.ingest(path, with_tree=False, workspace=str(tmp_path))

    monkeypatch.setattr(corpus, "extract_pages", lambda p, meta=None: [PAGE])

    assert corpus.ingest(path, with_tree=False, workspace=str(tmp_path))[1] == 1


def test_a_known_document_is_linked_without_reading_it_again(tmp_path, monkeypatch):
    reads = []
    monkeypatch.setattr(corpus, "extract_pages", lambda p, meta=None: reads.append(p) or [PAGE])
    first, later = book(tmp_path, "a/scan.pdf"), book(tmp_path, "b/same_scan.pdf")

    doc_id, _pages = corpus.ingest(first, with_tree=False, task_id="t_a", workspace=str(tmp_path))
    assert corpus.ingest(later, with_tree=False, task_id="t_b", workspace=str(tmp_path))[0] == doc_id

    assert reads == [first]
    assert sorted(meta_of(tmp_path, doc_id)["paths"]) == sorted([first, later])

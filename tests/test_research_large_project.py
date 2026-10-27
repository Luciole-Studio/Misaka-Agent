"""A research project grows large: run r_77d38618b4 (2026-10-25/26) held 33,308 indexed documents,
most of them one-page OCR files its cards declared. Its window froze for five minutes and more,
again and again (R45): the driver ran the run's settle, bundles and workspace index on the event
loop, every card's bundle read the whole corpus again (27 s each), and the workspace index listed
each page of each document and rendered it with a join per node -- 214,587 nodes in 746 s (R33).
"""
import hashlib
import os
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest
from test_research_artifact_drift import _accept
from test_research_root_lifecycle import (
    root_run,  # noqa: F401 - the fixture is used by name
)

from misaka import workspace
from misaka.core.platform import repo, tasks
from misaka.core.research import bundle, runs, workflow


def test_rendering_a_large_tree_joins_once():
    tree = {"node_id": "ws", "title": "Workspace", "nodes": [
        {"node_id": f"n{i}", "title": f"Node {i}", "summary": "s", "nodes": [
            {"node_id": f"n{i}.{j}", "title": f"Leaf {j}", "path": f"/p/{i}/{j}"} for j in range(500)]}
        for i in range(400)]}
    started = time.perf_counter()
    text = workspace.render(tree)
    assert time.perf_counter() - started < 5          # a join per node took minutes here
    lines = text.split("\n")
    assert len(lines) == 1 + 400 + 400 * 500 * 2
    assert lines[:4] == ["Workspace  [ws]", "  Node 0  [n0]  — s", "    Leaf 0  [n0.0]", "      Path: /p/0/0"]


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(repo, "enabled", lambda *a: False)
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        run = runs.create(con, workspace=str(tmp_path), question="Fixture question")
        yield con, run, runs.nodes(con, run["id"])[0]


def test_a_document_without_structure_is_a_leaf(board, monkeypatch):
    con, run, _root = board
    docs = [{"doc_id": "flat", "title": "One page of OCR", "pages": 1, "orig_path": "/w/ocr/p1.txt"},
            {"doc_id": "book", "title": "A structured book", "pages": 300, "orig_path": "/w/book.pdf"}]
    monkeypatch.setattr(workspace.corpus, "docs", lambda **k: docs)
    monkeypatch.setattr(workspace.corpus, "_tree",
                        lambda did, **k: [{"node_id": "0001", "title": "Chapter 1"}] if did == "book" else None)

    def no_page_reads(*a, **k):
        raise AssertionError("the workspace index read a document's pages")

    monkeypatch.setattr(workspace.corpus, "page_heads", no_page_reads)
    tree = workspace.outline(con, workspace=run["workspace"], run_id=run["id"], research_store=runs)
    materials = next(n for n in tree["nodes"] if n["node_id"] == "materials")["nodes"]
    flat, book = materials
    assert flat == {"node_id": "doc:flat", "title": "One page of OCR", "summary": "1 pages",
                    "source": "/w/ocr/p1.txt", "nodes": []}
    assert book["nodes"] == [{"node_id": "0001", "title": "Chapter 1"}]
    assert book["summary"] == "300 pages (PageIndex structure)"


def test_the_task_board_comes_before_the_runs(board, monkeypatch):
    con, run, _root = board
    monkeypatch.setattr(workspace.corpus, "docs", lambda **k: [])
    tree = workspace.outline(con, workspace=run["workspace"], run_id=run["id"], research_store=runs)
    assert [n["node_id"] for n in tree["nodes"]] == ["board", "materials", "runs", "ledger"]


def _files(prefix, kind, folder, count):
    if kind == "doc":
        return [{"node_id": f"doc:{prefix}{i}", "title": f"page {i}", "summary": "1 pages",
                 "source": os.path.join(folder, f"{prefix}{i}.txt"), "nodes": []} for i in range(count)]
    return [{"node_id": f"artifact:{prefix}{i}", "title": f"file {i}", "summary": "task_output",
             "path": os.path.join(folder, f"{prefix}{i}.txt"), "nodes": []} for i in range(count)]


def _accounted(text, files):
    """Each file is listed, or counted on the line of the folder it is in; nothing else."""
    kind = "indexed documents" if files[0]["node_id"].startswith("doc:") else "registered files"
    folded = {}
    for line in text.split("\n"):
        stripped = line.strip()
        if "not listed here" in stripped and kind in stripped:
            folder, rest = stripped.split("  — ", 1)
            assert folder not in folded
            folded[folder] = int(rest.split()[0])
    listed = [f for f in files if f"[{f['node_id']}]" in text]
    counted = {}
    for f in files:
        if f not in listed:
            where = os.path.join(os.path.dirname(f.get("path") or f["source"]), "")
            counted[where] = counted.get(where, 0) + 1
    return listed, folded, counted


def test_a_large_file_list_is_listed_by_folder_and_no_file_goes_missing():
    root, deep, other = (os.path.join(os.sep, "w", *parts) for parts in
                         (("cards", "t_1"), ("cards", "t_1", "work", "ocr"), ("cards", "t_1", "raw")))
    docs = _files("r", "doc", root, 5) + _files("o", "doc", deep, 140) + _files("w", "doc", other, 11)
    artifacts = _files("a", "artifact", root, 8) + _files("b", "artifact", deep, 112)
    card = {"node_id": "task:t_1", "title": "[t_1] card", "summary": "done",
            "nodes": [{"node_id": "task:t_1#contract", "title": "Task contract"}, *docs]}
    branch = {"node_id": "branch:b_1", "title": "Node b_1", "nodes": artifacts}
    text = workspace.render({"node_id": "ws", "title": "Workspace", "nodes": [card, branch]})
    for files in (docs, artifacts):
        listed, folded, counted = _accounted(text, files)
        assert folded == counted and len(listed) + sum(folded.values()) == len(files)
    assert "[task:t_1#contract]" in text
    assert {f["node_id"] for f in _accounted(text, docs)[0]} == {f"doc:r{i}" for i in range(5)}
    assert f"{os.path.join(deep, '')}  — 140 indexed documents, not listed here; doc_list with this folder" in text
    assert f"{os.path.join(other, '')}  — 11 indexed documents" in text
    assert f"{os.path.join(deep, '')}  — 112 registered files, not listed here; find in this folder" in text


def test_a_list_of_a_hundred_files_is_listed_whole():
    files = _files("o", "doc", os.path.join(os.sep, "w", "ocr"), 100)
    text = workspace.render({"node_id": "ws", "title": "Workspace", "nodes": files})
    assert all(f"[{f['node_id']}]" in text for f in files) and "not listed here" not in text


async def test_doc_list_finds_a_document_by_any_of_its_source_folders(tmp_path, monkeypatch):
    """The folder a large card's documents are listed under finds every one of them, though a
    document's title is the name it was first indexed under."""
    from types import SimpleNamespace

    from misaka.core.documents.wiring import documents
    from misaka.core.wiring import ToolCollector
    first, card = tmp_path / "downloads" / "gaz.pdf", tmp_path / "cards" / "t_1" / "sources" / "gaz.pdf"
    rows = [{"doc_id": "d1", "title": "gaz.pdf", "pages": 3, "orig_path": str(first), "paths": [str(first), str(card)]},
            {"doc_id": "d2", "title": "Unrelated", "pages": 1, "orig_path": str(tmp_path / "x.md")}]
    monkeypatch.setattr(documents.corpus, "docs", lambda **k: rows)
    collector = ToolCollector()
    documents.register(collector)
    doc_list = next(t for t in collector.tools if t.name == "doc_list")

    async def listed(query):
        result = await doc_list.execute("call", {"query": query}, None, None, SimpleNamespace(cwd=str(tmp_path)))
        return result["content"][0]["text"]

    assert "d1" in await listed(os.path.join(str(card.parent), "")) and "d2" not in await listed("cards")
    assert "d1" in await listed("GAZ.PDF")                       # titles match as before
    assert "No indexed document's title or source path" in await listed("nowhere")


def test_one_settle_reads_the_project_index_once(board, monkeypatch):
    con, run, root = board
    for name in ("a", "b", "c"):
        _accept(con, run, root, name, {f"{name}.md": f"{name}\n"})
    reads, used = [], []

    def project_index(path):
        reads.append(path)
        return object()

    monkeypatch.setattr(bundle, "project_index", project_index)
    monkeypatch.setattr(bundle, "card_bundle", lambda db, current, task, index=None: used.append(index))
    workflow.settle_done_tasks(con, run_id=run["id"])
    assert reads == [run["workspace"]]
    assert len(used) == 3 and len({id(index) for index in used}) == 1


def test_a_failed_shared_read_leaves_each_card_its_own(board, monkeypatch):
    con, run, root = board
    for name in ("a", "b"):
        _accept(con, run, root, name, {f"{name}.md": f"{name}\n"})
    used = []

    def failing(path):
        raise OSError("fixture: corpus unreadable")

    monkeypatch.setattr(bundle, "project_index", failing)
    monkeypatch.setattr(bundle, "card_bundle", lambda db, current, task, index=None: used.append(index))
    workflow.settle_done_tasks(con, run_id=run["id"])
    assert used == [None, None]


def test_a_shared_index_checks_each_card_against_the_registry_as_it_stands(board, monkeypatch):
    """One index serves every card of a settle. A file registered again between two cards'
    collectors (a card settled again, or a round's file declared anew) is checked against its
    new digest, not the one the earlier collector saw."""
    con, run, _root = board
    monkeypatch.setattr(bundle.corpus, "docs", lambda **k: [])
    path = Path(run["workspace"], "nodes", "b_1", "notes.md")
    path.parent.mkdir(parents=True)
    path.write_text("first\n", encoding="utf-8")
    runs.register_file(con, run["id"], "task_output", "notes", str(path),
                       sha256=hashlib.sha256(b"first\n").hexdigest())
    index = bundle.project_index(run["workspace"])
    earlier = bundle._Collector(con, run, index)
    real = str(path.resolve())
    assert earlier.checked_digest(real)
    path.write_text("second\n", encoding="utf-8")
    runs.register_file(con, run["id"], "task_output", "notes", str(path),
                       sha256=hashlib.sha256(b"second\n").hexdigest())
    later = bundle._Collector(con, run, index)
    assert later.checked_digest(real) == hashlib.sha256(b"second\n").hexdigest()
    assert index.digests == {}                        # the registry stays with each collector


async def test_a_halt_settles_and_reports_off_the_event_loop(root_run, monkeypatch):  # noqa: F811
    con, run, _root, _owner, _events = root_run
    loop_thread = threading.get_ident()
    threads = {}
    real_partial = workflow._partial_result

    def partial_result(*args, **kwargs):
        threads["report"] = threading.get_ident()
        return real_partial(*args, **kwargs)

    monkeypatch.setattr(workflow, "settle_done_tasks",
                        lambda db, *, run_id, **kw: threads.setdefault("settle", threading.get_ident()))
    monkeypatch.setattr(workflow, "_partial_result", partial_result)

    async def expand(*args, **kwargs):
        return "stopped"

    monkeypatch.setattr(workflow, "_expand", expand)
    assert (await workflow.run(con, {}, object(), run_id=run["id"]))["reason"] == "stopped"
    assert set(threads) == {"settle", "report"}
    assert loop_thread not in threads.values()


async def test_a_node_closes_off_the_event_loop(board, monkeypatch):
    con, run, root = board
    loop_thread = threading.get_ident()
    closed = []
    monkeypatch.setattr(workflow, "_close", lambda *a: closed.append(threading.get_ident()) or "closed")
    deepest = {**dict(root), "depth": runs.current_limits(con, run)["max_depth"]}
    assert await workflow._after_review(con, run, deepest, lambda **k: None) == "closed"
    assert closed and closed[0] != loop_thread

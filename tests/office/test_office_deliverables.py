"""An Office deliverable a card produced reaches the run's artifact list.

``workflow._register_task_artifacts`` used to drop anything that did not decode as UTF-8,
on the stated grounds that "binary originals are handled by corpus/PageIndex ingestion".
That was true of a PDF and false of everything else: the corpus refused .docx and .xlsx
outright until the Office reader landed, so a card that produced the report it was asked
for had its deliverable dropped on the floor -- with no error anywhere, because the
registration loop simply moved to the next file.

Registering it is safe for every consumer of ``runs.artifact_text``: each already handles a
decode failure, and ``UnicodeDecodeError`` is a ``ValueError``. ``artifact_text`` itself
stays strict -- a .docx is not quotable text, and its quotable form is the document the
corpus indexed.
"""
from __future__ import annotations

import hashlib
import importlib
import json

import pytest

importlib.import_module("docx")


def _run_with_artifact(tmp_path, monkeypatch, filename, write):
    from misaka.core.platform import tasks as db
    from misaka.core.research import runs, workflow

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "final").mkdir()
    filename = "final/" + filename
    write(workspace / filename)

    monkeypatch.setenv("MISAKA_RUNS_HOME", str(tmp_path / "runs"))
    con = db.connect(str(tmp_path / "board.db"))
    runs.init(con)
    run = runs.create(con, workspace=str(workspace), question="Did shipments rise?")
    node = runs.nodes(con, run["id"])[0]
    con.execute(
        "INSERT INTO research_run_tasks (task_id,run_id,branch_id,kind,wave,created_at) "
        "VALUES (?,?,?,?,0,strftime('%s','now'))",
        ("t1", run["id"], node["id"], "explore"))
    digest = hashlib.sha256((workspace / filename).read_bytes()).hexdigest()
    workflow._register_task_artifacts(            # what dispatch._submitted records for every card
        con, run, {"id": "t1", "workspace": str(workspace)},
        {"artifacts": [filename], "artifact_digests": {filename: digest}})
    return con, run


def _docx(path):
    from misaka.core.tools._office import docx as writer
    writer.write(str(path), "create", {"blocks": [
        {"type": "heading", "text": "Findings", "level": 1},
        {"type": "paragraph", "text": "Shipments rose to 1,240 units in March."}]})


def test_a_docx_deliverable_is_registered(tmp_path, monkeypatch):
    from misaka.core.research import runs

    con, run = _run_with_artifact(tmp_path, monkeypatch, "report.docx", _docx)
    rows = runs.artifacts(con, run["id"], kind="task_output")
    assert len(rows) == 1
    assert rows[0]["title"].endswith("report.docx")
    con.close()


def test_it_is_marked_binary_so_a_reader_knows_not_to_quote_it(tmp_path, monkeypatch):
    from misaka.core.research import runs

    con, run = _run_with_artifact(tmp_path, monkeypatch, "report.docx", _docx)
    metadata = json.loads(runs.artifacts(con, run["id"], kind="task_output")[0]["metadata_json"])
    assert metadata["binary"] is True
    con.close()


def test_a_text_deliverable_is_not_marked_binary(tmp_path, monkeypatch):
    from misaka.core.research import runs

    con, run = _run_with_artifact(
        tmp_path, monkeypatch, "notes.md",
        lambda path: path.write_text("# Findings\n", encoding="utf-8"))
    metadata = json.loads(runs.artifacts(con, run["id"], kind="task_output")[0]["metadata_json"])
    assert "binary" not in metadata
    con.close()


def test_artifact_text_still_refuses_to_quote_it(tmp_path, monkeypatch):
    """Strict on purpose: a .docx is a zip, and "text" decoded out of one with replacement
    characters would let the ledger verify a quotation against mojibake."""
    from misaka.core.research import runs

    con, run = _run_with_artifact(tmp_path, monkeypatch, "report.docx", _docx)
    with pytest.raises(UnicodeDecodeError):     # a subclass of ValueError, which is what
        runs.artifact_text(runs.artifacts(con, run["id"], kind="task_output")[0])   # every consumer catches
    con.close()


def test_the_ledger_records_declarations_without_certifying_binary_quotes(tmp_path, monkeypatch):
    """Current ledger stores declarations; corpus verification remains a separate API."""
    from misaka.core.platform import tasks as db
    from misaka.core.research import ledger

    con, run = _run_with_artifact(tmp_path, monkeypatch, "report.docx", _docx)
    got = ledger.ingest_report(con, run, {"id": "t1"}, {
        "schema_version": 1,
        "findings": [{"text": "a claim", "claim_type": "fact",
                      "source_file": "final/report.docx",
                      "quote": "Shipments rose to 1,240 units in March."}]})
    assert got["claims"] == 1
    finding = ledger.findings(con, run["id"])[0]
    claim = ledger.claims(con, finding["id"])[0]
    assert claim["artifact_id"] is not None
    con.close()
    _ = db


def test_the_docx_is_quotable_through_the_corpus_instead(tmp_path, monkeypatch):
    """Which is the point of registering it at all: the deliverable is on the run's list,
    and the same sentence verifies once the file is in the corpus."""
    monkeypatch.setenv("MISAKA_PAGEINDEX", str(tmp_path / "corpus"))
    from misaka.core.documents import index as corpus

    path = tmp_path / "report.docx"
    _docx(path)
    doc_id, _pages = corpus.ingest(str(path))
    assert corpus.verify_quote(doc_id, "Shipments rose to 1,240 units in March.")


def test_structure_does_not_blame_pageindex_for_a_format_with_no_outline(tmp_path, monkeypatch):
    """Outlines come from PageIndex, which reads PDFs. Telling a user to install the extra
    for a workbook sends them to fix something that would change nothing, and leaves them
    believing their corpus is broken."""
    monkeypatch.setenv("MISAKA_PAGEINDEX", str(tmp_path / "corpus"))
    from misaka.core.documents import index as corpus

    path = tmp_path / "report.docx"
    _docx(path)
    doc_id, _pages = corpus.ingest(str(path))
    shape = corpus.structure(doc_id)
    assert "pageindex" not in shape["mode"]
    assert "no outline" in shape["mode"]

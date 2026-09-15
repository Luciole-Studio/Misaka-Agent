"""LibreOffice when the machine has it, and an honest refusal when it does not.

Owner decision D3 rules out bundling an external program, so every capability here is
optional. The rule that shapes all of it: **never pretend**. A formula whose value was not
computed reads back as ``uncached`` rather than blank; a .doc that cannot be converted is
refused by name with the command that fixes it; an export that did not run does not report
a PDF nobody wrote.

The subprocess is stubbed throughout. A test that needed LibreOffice installed would be a
test that silently does not run on most machines -- and the branch that matters most is the
one where it is absent.
"""
from __future__ import annotations

import importlib
import subprocess

import pytest

from misaka.core.documents.office import soffice


class _Recorder:
    """Stands in for ``subprocess.run``, recording the commands and faking the output."""

    def __init__(self, *, returncode=0, produce=None, stderr="", raises=None):
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.returncode = returncode
        self.produce = produce            # (suffix) -> write a file next to --outdir
        self.stderr = stderr
        self.raises = raises

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.kwargs.append(kwargs)
        if self.raises is not None:
            raise self.raises
        if self.produce and "--outdir" in argv:
            import os
            outdir = argv[argv.index("--outdir") + 1]
            source = argv[-1]
            stem = os.path.splitext(os.path.basename(source))[0]
            os.makedirs(outdir, exist_ok=True)
            with open(os.path.join(outdir, f"{stem}.{self.produce}"), "wb") as handle:
                handle.write(b"converted")
        return subprocess.CompletedProcess(argv, self.returncode, "", self.stderr)


@pytest.fixture
def installed(monkeypatch):
    """LibreOffice present, with its subprocess replaced."""
    monkeypatch.setattr(soffice.shutil, "which", lambda name: "/usr/bin/soffice")
    recorder = _Recorder(produce="xlsx")
    monkeypatch.setattr(soffice.subprocess, "run", recorder)
    return recorder


@pytest.fixture
def absent(monkeypatch):
    """No LibreOffice, patched at ``binary`` rather than under it.

    Patching ``soffice.os.path.exists`` would reach every other module: ``os`` is one
    object, so the stub answers for the whole process and the code under test then copies
    files that are not there.
    """
    monkeypatch.setattr(soffice, "binary", lambda: None)


# ---- the probe ---------------------------------------------------------------------------

def test_the_binary_is_found_on_the_path(monkeypatch):
    monkeypatch.setattr(soffice.shutil, "which", lambda name: "/usr/bin/soffice")
    assert soffice.binary() == "/usr/bin/soffice"


def test_the_macos_app_bundle_is_found_when_nothing_is_on_the_path(monkeypatch):
    """``brew install --cask libreoffice`` puts nothing on PATH, which is how most macOS
    users end up with it installed and undetectable."""
    monkeypatch.setattr(soffice.shutil, "which", lambda name: None)
    monkeypatch.setattr(soffice, "_MACOS_PATH", str(__file__))     # a path that exists
    assert soffice.binary() == str(__file__)


def test_no_libreoffice_anywhere_is_none_not_an_exception(monkeypatch):
    monkeypatch.setattr(soffice.shutil, "which", lambda name: None)
    monkeypatch.setattr(soffice, "_MACOS_PATH", "/nonexistent/soffice")
    assert soffice.binary() is None


def test_the_probe_is_not_cached(monkeypatch):
    """A user who installs LibreOffice mid-session should not have to restart to get the
    capability, and the probe is one PATH lookup."""
    answers = iter([None, "/usr/bin/soffice"])
    monkeypatch.setattr(soffice.shutil, "which", lambda name: next(answers))
    monkeypatch.setattr(soffice, "_MACOS_PATH", "/nonexistent/soffice")
    assert soffice.binary() is None
    assert soffice.binary() == "/usr/bin/soffice"


# ---- how it is invoked --------------------------------------------------------------------

def test_every_call_gets_its_own_profile(installed, tmp_path):
    """LibreOffice keeps one profile per user and locks it, so two concurrent conversions
    deadlock -- and concurrent is the normal case here, with research cards running N-way
    parallel."""
    source = tmp_path / "a.xlsx"
    source.write_bytes(b"x")
    soffice.convert(source, "xlsx", into=tmp_path / "one")
    soffice.convert(source, "xlsx", into=tmp_path / "two")

    profiles = [next(part for part in call if part.startswith("-env:UserInstallation="))
                for call in installed.calls]
    assert len(profiles) == 2
    assert profiles[0] != profiles[1]
    assert all(profile.startswith("-env:UserInstallation=file://") for profile in profiles)


def test_the_command_is_headless_and_bounded(installed, tmp_path):
    source = tmp_path / "a.xlsx"
    source.write_bytes(b"x")
    soffice.convert(source, "xlsx", into=tmp_path / "out", timeout=42)
    argv = installed.calls[0]
    assert "--headless" in argv
    assert "--norestore" in argv
    assert installed.kwargs[0]["timeout"] == 42


def test_a_nonzero_exit_records_the_reason_and_returns_none(monkeypatch, tmp_path):
    """"conversion failed" with no reason is the dead end this whole path exists to remove."""
    monkeypatch.setattr(soffice.shutil, "which", lambda name: "/usr/bin/soffice")
    monkeypatch.setattr(soffice.subprocess, "run",
                        _Recorder(returncode=1, stderr="Error: source file could not be loaded"))
    source = tmp_path / "a.xlsx"
    source.write_bytes(b"x")
    meta = {}
    assert soffice.convert(source, "xlsx", into=tmp_path / "out", meta=meta) is None
    assert "could not be loaded" in meta["soffice_error"]


def test_a_timeout_is_recorded_and_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(soffice.shutil, "which", lambda name: "/usr/bin/soffice")
    monkeypatch.setattr(soffice.subprocess, "run",
                        _Recorder(raises=subprocess.TimeoutExpired("soffice", 300)))
    source = tmp_path / "a.xlsx"
    source.write_bytes(b"x")
    meta = {}
    assert soffice.convert(source, "xlsx", into=tmp_path / "out", meta=meta) is None
    assert "soffice" in meta["soffice_error"]


def test_an_exit_of_zero_that_produced_nothing_is_still_a_failure(monkeypatch, tmp_path):
    """LibreOffice exits 0 having done nothing more often than it should."""
    monkeypatch.setattr(soffice.shutil, "which", lambda name: "/usr/bin/soffice")
    monkeypatch.setattr(soffice.subprocess, "run", _Recorder(produce=None))
    source = tmp_path / "a.xlsx"
    source.write_bytes(b"x")
    meta = {}
    assert soffice.convert(source, "xlsx", into=tmp_path / "out", meta=meta) is None
    assert "no xlsx produced" in meta["soffice_error"]


def test_nothing_runs_at_all_when_it_is_not_installed(absent, tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(soffice.subprocess, "run", lambda *a, **k: ran.append(a))
    source = tmp_path / "a.xlsx"
    source.write_bytes(b"x")
    assert soffice.convert(source, "xlsx", into=tmp_path) is None
    assert soffice.recalc(source, into=tmp_path) is None
    assert soffice.export_pdf(source, tmp_path / "a.pdf") is False
    assert ran == []


# ---- recalculation --------------------------------------------------------------------------

def test_recalc_works_on_a_copy_and_never_touches_the_source(installed, tmp_path):
    """The corpus identifies a document by the sha256 of the file on disk, so recalculating
    in place would change a document's identity as a side effect of reading it."""
    source = tmp_path / "book.xlsx"
    source.write_bytes(b"original")
    staging = tmp_path / "staging"
    staging.mkdir()

    fresh = soffice.recalc(source, into=staging)
    assert fresh is not None
    assert str(staging) in str(fresh)
    assert source.read_bytes() == b"original"


def test_export_pdf_puts_the_file_where_it_was_asked(monkeypatch, tmp_path):
    monkeypatch.setattr(soffice.shutil, "which", lambda name: "/usr/bin/soffice")
    monkeypatch.setattr(soffice.subprocess, "run", _Recorder(produce="pdf"))
    source = tmp_path / "report.docx"
    source.write_bytes(b"x")
    out = tmp_path / "deliverables" / "report.pdf"
    assert soffice.export_pdf(source, out)
    assert out.read_bytes() == b"converted"


# ---- what the rest of the system does with it -------------------------------------------------

def test_a_workbook_with_uncomputed_formulas_is_recalculated_on_read(monkeypatch, tmp_path):
    """Without this the reader shows every formula as ``uncached`` even on a machine that
    could work it out."""
    openpyxl = importlib.import_module("openpyxl")
    from misaka.core.documents.office import xlsx as reader
    from misaka.core.tools._office import xlsx as office_writer

    path = tmp_path / "sums.xlsx"
    office_writer.write(str(path), "create", {"sheets": [
        {"name": "S", "headers": ["v"], "rows": [[10], [32], ["=SUM(A2:A3)"]]}]})
    assert office_writer.cache_empty(str(path))

    def fake_recalc(source, *, into, timeout=None, meta=None):
        # What LibreOffice does: reopen, compute, save. Done here with openpyxl so the
        # test asserts on the wiring rather than on a program that may not be installed.
        import os
        book = openpyxl.load_workbook(str(source))
        book["S"]["A4"] = 42
        target = os.path.join(str(into), "recalculated.xlsx")
        book.save(target)
        return target

    monkeypatch.setattr(soffice, "binary", lambda: "/usr/bin/soffice")
    monkeypatch.setattr(soffice, "recalc", fake_recalc)
    before = path.read_bytes()
    out = reader.render(str(path))
    assert "42" in out
    assert "uncached" not in out.replace("`uncached` means", "")   # not the legend
    assert path.read_bytes() == before                             # source untouched


def test_without_libreoffice_the_readout_says_what_would_fix_it(absent, tmp_path):
    importlib.import_module("openpyxl")
    from misaka.core.documents.office import xlsx as reader
    from misaka.core.tools._office import xlsx as office_writer

    path = tmp_path / "sums.xlsx"
    office_writer.write(str(path), "create", {"sheets": [
        {"name": "S", "headers": ["v"], "rows": [[10], [32], ["=SUM(A2:A3)"]]}]})
    out = reader.render(str(path))
    assert "`uncached`" in out
    assert soffice.INSTALL_HINT in out


def test_the_write_receipt_says_the_values_are_not_in_the_file(absent, tmp_path):
    importlib.import_module("openpyxl")
    from misaka.core.tools import _office

    receipt = _office.run_ops(str(tmp_path / "s.xlsx"), [
        {"create": {"sheets": [{"name": "S", "rows": [[1], [2], ["=SUM(A1:A2)"]]}]}}])
    assert "cached values are empty" in receipt
    assert soffice.INSTALL_HINT in receipt


def test_export_pdf_without_libreoffice_names_what_to_install(absent, tmp_path):
    importlib.import_module("openpyxl")
    from misaka.core.tools import _office

    path = tmp_path / "s.xlsx"
    _office.run_ops(str(path), [{"create": {"sheets": [{"name": "S", "rows": [[1]]}]}}])
    receipt = _office.run_ops(str(path), [{"export_pdf": {}}])
    assert "export_pdf needs LibreOffice" in receipt
    assert soffice.INSTALL_HINT in receipt


# ---- legacy formats ----------------------------------------------------------------------------

def test_a_legacy_file_without_libreoffice_is_refused_by_name(absent, tmp_path, monkeypatch):
    """OLE compound files: nothing in the Office stack reads them, and a model told only
    "cannot index .doc" hands the same file back."""
    monkeypatch.setenv("MISAKA_PAGEINDEX", str(tmp_path / "corpus"))
    from misaka.core.documents import index as corpus

    path = tmp_path / "old.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0not really a doc")
    with pytest.raises(ValueError) as caught:
        corpus.ingest(str(path))
    message = str(caught.value)
    assert soffice.INSTALL_HINT in message
    assert "save it as .docx" in message


def test_a_legacy_file_is_converted_and_then_read_normally(monkeypatch, tmp_path):
    importlib.import_module("docx")
    monkeypatch.setenv("MISAKA_PAGEINDEX", str(tmp_path / "corpus"))
    from misaka.core.documents import index as corpus
    from misaka.core.tools._office import docx as office_writer

    modern = tmp_path / "made.docx"
    office_writer.write(str(modern), "create", {"blocks": [
        {"type": "heading", "text": "Chapter One", "level": 1},
        {"type": "paragraph", "text": "the body of a converted document"}]})

    def fake_convert(source, target_suffix, *, into, timeout=None, meta=None):
        import os
        import shutil
        target = os.path.join(str(into), "converted." + target_suffix)
        shutil.copy2(str(modern), target)
        return target

    monkeypatch.setattr(soffice, "binary", lambda: "/usr/bin/soffice")
    monkeypatch.setattr(soffice, "convert", fake_convert)

    legacy = tmp_path / "old.doc"
    legacy.write_bytes(b"\xd0\xcf\x11\xe0")
    doc_id, pages = corpus.ingest(str(legacy))
    assert pages == 1
    assert corpus.verify_quote(doc_id, "the body of a converted document") is not None


def test_a_folder_walk_does_not_collect_legacy_files(tmp_path):
    """Reading one costs a LibreOffice process, so a walk past a directory of 1990s
    attachments would spend minutes converting files nobody asked for."""
    from misaka.core.documents.index import _EXTRACTORS, SCAN_SUFFIXES

    for suffix in (".doc", ".xls", ".ppt"):
        assert suffix in _EXTRACTORS            # named one by one, they are read
        assert suffix not in SCAN_SUFFIXES      # swept up by a net, they are not

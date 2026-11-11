"""MinerU as the program that reads scans, chosen with ``documents.ocr_engine``.

ocrmypdf turned vertical Chinese and Japanese into Latin noise: a 1914 gazette came back with 7% of
its characters CJK, where MinerU gives 95%. These tests run a stand-in ``mineru-kit`` on PATH, so the
real command line, the real output file and the real failure paths are what is exercised."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

from misaka.cli import setup_wizard
from misaka.config import home, product
from misaka.core.documents import index as corpus
from misaka.core.documents import mineru
from tests.test_pdf_ocr_routing import _layer

PAGE = "第{n}頁的正文足夠長，足以算作這一頁確實讀到了文字，而且每一頁都不相同。"
REAL_TURNS = mineru._turns

# Reads its orders from the file named by $MINERU_STAND_IN, writes the output the real program
# writes, and logs what it was run with.
STAND_IN = """#!{python}
import json, os, sys

orders = json.load(open(os.environ["MINERU_STAND_IN"], encoding="utf-8"))
args = sys.argv[1:]
source = args[1]
turned = []
if "-p" in args:
    wanted = [int(n) for n in args[args.index("-p") + 1].split(",")]
elif os.path.isfile(source):
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(source)
    wanted = list(range(1, len(pdf) + 1))
    turned = [pdf[index].get_rotation() for index in range(len(pdf))]
else:
    wanted = orders["whole"]
tier = args[args.index("--tier") + 1]
with open(orders["log"], "a", encoding="utf-8") as f:
    f.write(json.dumps({{"argv": args, "turned": turned, "telemetry_off": os.environ.get("MINERU_TELEMETRY_DISABLED")}}) + "\\n")
if set(wanted) & set(orders.get("fail_on", [])):
    print(orders.get("stderr", "boom"), file=sys.stderr)
    sys.exit(1)
pages = []
for position, n in enumerate(wanted):
    text = orders.get("stuck", {{}}).get(str(n)) if tier != "basic" else None
    if turned and turned[position] and orders.get("reads_at") not in (None, turned[position]):
        text = "短"
    blocks = orders.get("blocks", {{}}).get(str(n))
    if blocks is None:
        blocks = [{{"type": "text", "index": 0, "content": [{{"type": "text", "content": text or orders["page"].format(n=n)}}]}}]
    pages.append({{"page_idx": n - 1, "blocks": blocks}})
document = {{"schema": "docvortex.middle", "schema_version": "2.0", "pages": pages}}
if orders.get("no_pages"):
    document = {{"schema": "something.else"}}
with open(args[args.index("-o") + 1], "w", encoding="utf-8") as f:
    json.dump(document, f, ensure_ascii=False)
if orders.get("leave_behind"):
    import subprocess
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
"""

# Answers as ``tesseract <image> stdout --psm 0`` does: $TESSERACT_STAND_IN maps "<page>-<turn>" to
# (turn still needed, confidence). A page it has no answer for has too little to go on.
OSD = """#!{python}
import json, os, sys

answers = json.load(open(os.environ["TESSERACT_STAND_IN"], encoding="utf-8"))
if answers.get("fail"):
    sys.exit(1)
answer = answers.get(os.path.basename(sys.argv[1]).removesuffix(".png"))
if answer is None:
    print("Too few characters. Skipping this page", file=sys.stderr)
    sys.exit(1)
print("Page number: 0")
print("Orientation in degrees: 0")
print(f"Rotate: {{answer[0]}}")
print(f"Orientation confidence: {{answer[1]}}")
print("Script: Han")
"""


@pytest.fixture
def stand_in(tmp_path, monkeypatch):
    if os.name == "nt":
        # MinerU is looked for and started by its name, and by name Windows finds and starts only
        # an .exe. A ``#!`` script is not one: where MinerU is installed the real program ran in
        # the stand-in's place (2026-11-10: 39 of these failed and 6 passed for that reason alone).
        pytest.skip("a `#!` script cannot stand in for mineru-kit on Windows")
    folder = tmp_path / "bin"
    folder.mkdir()
    program = folder / mineru.BINARY
    program.write_text(STAND_IN.format(python=sys.executable), encoding="utf-8")
    program.chmod(0o755)
    orders_file, log = tmp_path / "orders.json", tmp_path / "runs.log"
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("MINERU_STAND_IN", str(orders_file))

    def order(**orders):
        orders_file.write_text(json.dumps({"page": PAGE, "log": str(log), "whole": [1], **orders}, ensure_ascii=False),
                               encoding="utf-8")

    def runs():
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

    order()
    monkeypatch.setattr(mineru, "_turns", lambda path, numbers: {})
    return order, runs


@pytest.fixture
def judged(tmp_path, monkeypatch, stand_in):
    """A real three-page PDF and a stand-in tesseract, with ``_turns`` itself at work."""
    _order, runs = stand_in
    monkeypatch.setattr(mineru, "_turns", REAL_TURNS)
    folder = tmp_path / "bin"
    osd = folder / mineru.TESSERACT
    osd.write_text(OSD.format(python=sys.executable), encoding="utf-8")
    osd.chmod(0o755)
    answers = tmp_path / "answers.json"
    monkeypatch.setenv("TESSERACT_STAND_IN", str(answers))
    book = tmp_path / "book.pdf"
    blank = [Image.new("RGB", (120, 170), "white") for _ in range(3)]
    blank[0].save(book, save_all=True, append_images=blank[1:])

    def answer(**votes):
        answers.write_text(json.dumps(votes), encoding="utf-8")

    answer()
    return str(book), answer, runs


def settings(**documents):
    path = home.path("settings")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"documents": documents}), encoding="utf-8")


def pages_asked(run):
    args = run["argv"]
    return [int(n) for n in args[args.index("-p") + 1].split(",")]


# -- which program reads ---------------------------------------------------------------------------

def test_mineru_reads_where_it_is_installed(stand_in):
    assert corpus._ocr_engine() == mineru.BINARY


def test_ocrmypdf_reads_where_mineru_is_not(monkeypatch):
    monkeypatch.setattr(corpus.shutil, "which", lambda name: None)
    assert corpus._ocr_engine() == corpus.OCR_BINARY


def test_the_setting_chooses_over_what_is_installed(stand_in):
    settings(ocr_engine="ocrmypdf")
    assert corpus._ocr_engine() == corpus.OCR_BINARY


def test_a_chosen_engine_that_is_missing_is_not_swapped_for_the_other(monkeypatch):
    settings(ocr_engine="mineru")
    monkeypatch.setattr(corpus.shutil, "which", lambda name: "/usr/bin/ocrmypdf" if name == corpus.OCR_BINARY else None)
    assert corpus._ocr_pages("scan.pdf", {}, [1], 1) is None
    assert corpus._ocr_missing() == mineru.MISSING


def test_a_scan_with_no_program_to_read_it_says_which_to_install(tmp_path, monkeypatch):
    settings(ocr_engine="mineru")
    monkeypatch.setattr(corpus.shutil, "which", lambda name: None)
    assert mineru.MISSING in str(corpus._no_text_error(str(tmp_path / "scan.pdf"), []))


@pytest.mark.parametrize("document, wrong", [
    ({"ocr_engine": "tesseract"}, "documents.ocr_engine"),
    ({"mineru_tier": "flash"}, "documents.mineru_tier"),
])
def test_a_word_the_setting_does_not_know_is_refused(document, wrong):
    settings(**document)
    assert any(wrong in error for error in product.validate_settings())


def test_a_word_edited_in_later_reads_as_the_default(stand_in):
    settings(ocr_engine="tesseract")
    assert product.setting("documents", "ocr_engine", "auto", str) == "auto"


# -- how it is run ---------------------------------------------------------------------------------

def test_a_page_comes_back_under_its_own_number(stand_in):
    assert corpus._ocr_pages("scan.pdf", {}, [2, 5], 9) == {2: PAGE.format(n=2), 5: PAGE.format(n=5)}


def test_it_runs_on_this_machine_with_its_reporting_off(stand_in):
    _order, runs = stand_in
    corpus._ocr_pages("scan.pdf", {}, [1], 1)
    run = runs()[0]
    assert run["telemetry_off"] == "1"
    assert "--remote" not in run["argv"] and "--remote-url" not in run["argv"] and "--api-key" not in run["argv"]
    assert run["argv"][run["argv"].index("--ocr-mode") + 1] == "ocr"


def test_it_reads_at_its_strongest_unless_told_otherwise(stand_in):
    _order, runs = stand_in
    corpus._ocr_pages("scan.pdf", {}, [1], 1)
    settings(mineru_tier="basic")
    corpus._ocr_pages("scan.pdf", {}, [1], 1)
    tiers = [run["argv"][run["argv"].index("--tier") + 1] for run in runs()]
    assert tiers == ["advanced", "basic"]
    assert "--disable-image-analysis" not in runs()[0]["argv"]


def test_a_long_book_is_read_in_batches(stand_in):
    _order, runs = stand_in
    read = corpus._ocr_pages("scan.pdf", {}, None, 120)
    assert sorted(read) == list(range(1, 121))
    assert [len(pages_asked(run)) for run in runs()] == [25, 25, 25, 25, 20]


def test_a_failed_batch_costs_only_its_own_pages(stand_in):
    order, _runs = stand_in
    order(fail_on=[60], stderr="out of memory")
    meta = {}
    read = corpus._ocr_pages("scan.pdf", meta, None, 120)
    assert sorted(read) == list(range(1, 51)) + list(range(76, 121))
    assert "out of memory" in meta["ocr_error"]


def test_every_batch_failing_is_no_reading_at_all(stand_in):
    order, _runs = stand_in
    order(fail_on=[1], stderr="out of memory")
    meta = {}
    assert corpus._ocr_pages("scan.pdf", meta, [1], 1) is None
    assert "out of memory" in meta["ocr_error"]


def test_output_it_does_not_recognise_is_a_failure_not_empty_pages(stand_in):
    order, _runs = stand_in
    order(no_pages=True)
    meta = {}
    assert corpus._ocr_pages("scan.pdf", meta, [1], 1) is None
    assert "pages" in meta["ocr_error"]


def test_a_run_that_outlasts_its_limit_gets_one_more_chance(stand_in, monkeypatch):
    real, runs = mineru._parse, []

    def hangs_once(path, numbers, tier, take=None):
        runs.append(numbers)
        if len(runs) == 1:
            raise subprocess.TimeoutExpired("mineru-kit", mineru.TIMEOUT)
        return real(path, numbers, tier, take)

    monkeypatch.setattr(mineru, "_parse", hangs_once)
    assert corpus._ocr_pages("scan.pdf", {}, [1], 1) == {1: PAGE.format(n=1)}
    assert runs == [[1], [1]]


def test_a_run_that_outlasts_its_limit_twice_says_so(stand_in, monkeypatch):
    def too_long(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(mineru.subprocess, "run", too_long)
    meta = {}
    assert corpus._ocr_pages("scan.pdf", meta, [1], 1) is None
    assert "timed out" in meta["ocr_error"]


def test_a_worker_left_behind_does_not_hold_a_finished_run_open(stand_in, tmp_path):
    order, _runs = stand_in
    order(leave_behind=True)
    started = time.monotonic()
    assert corpus._ocr_pages("scan.pdf", {}, [1], 1) == {1: PAGE.format(n=1)}
    assert time.monotonic() - started < 10


# -- what a page holds -----------------------------------------------------------------------------

def span(text):
    return {"type": "text", "content": text}


def test_the_blocks_of_a_page_keep_their_order_and_a_line_of_their_own(stand_in):
    order, _runs = stand_in
    order(blocks={"1": [
        {"type": "page_number", "index": 0, "content": [span("三")]},
        {"type": "paragraph_title", "index": 1, "level": 2, "content": [span("第一章")]},
        {"type": "text", "index": 2, "content": [span("正文"), {"type": "equation_inline", "content": "^{25}"}, span("繼續")]},
        {"type": "page_footnote", "index": 3, "content": [span("注一")]},
    ]})
    assert corpus._ocr_pages("scan.pdf", {}, [1], 1)[1] == "三\n\n第一章\n\n正文^{25}繼續\n\n注一"


def test_a_table_keeps_its_rows_and_its_caption(stand_in):
    order, _runs = stand_in
    table = "<table><tr><td>華文</td><td>英文</td></tr><tr><td colspan=\"2\">法文 &amp; 德文</td></tr></table>"
    order(blocks={"1": [{"type": "table", "index": 0, "content": [
        {"type": "table_caption", "index": 0, "content": [span("譯名表")]},
        {"type": "table_body", "index": 1, "content": table},
    ]}]})
    assert corpus._ocr_pages("scan.pdf", {}, [1], 1)[1] == "譯名表\n華文\t英文\n法文 & 德文"


def test_a_table_without_rows_keeps_its_words(stand_in):
    order, _runs = stand_in
    order(blocks={"1": [{"type": "table", "index": 0, "content": [
        {"type": "table_body", "index": 0, "content": "<div>華文 &amp; 英文</div>"}]}]})
    assert corpus._ocr_pages("scan.pdf", {}, [1], 1)[1] == "華文 & 英文"


def test_the_model_finding_nothing_on_a_picture_is_no_text(stand_in):
    order, _runs = stand_in
    order(blocks={"1": [{"type": "image", "index": 0, "content": [
        {"type": "image_body", "index": 0, "content": "[NO TEXT]"},
        {"type": "image_caption", "index": 1, "content": [span("七/8-6")]},
    ]}]})
    assert corpus._ocr_pages("scan.pdf", {}, [1], 1)[1] == "七/8-6"


def test_a_picture_leaves_its_caption_and_nothing_else(stand_in):
    order, _runs = stand_in
    order(blocks={"1": [{"type": "image", "index": 0, "content": [
        {"type": "image_body", "index": 0, "content": "", "image_base64": "iVBORw0KGgo="},
        {"type": "image_caption", "index": 1, "content": [span("七/8-6")]},
    ]}]})
    assert corpus._ocr_pages("scan.pdf", {}, [1], 1)[1] == "七/8-6"


def test_what_is_read_off_a_picture_is_kept(stand_in):
    order, _runs = stand_in
    order(blocks={"1": [{"type": "image", "index": 0, "sub_type": "text_image", "content": [
        {"type": "image_body", "index": 0, "content": "已傳\n此批"},
        {"type": "image_caption", "index": 1, "content": [span("七/8-5")]},
    ]}]})
    assert corpus._ocr_pages("scan.pdf", {}, [1], 1)[1] == "已傳\n此批\n七/8-5"


def test_a_page_with_nothing_on_it_is_an_empty_reading(stand_in):
    order, _runs = stand_in
    order(blocks={"1": []})
    assert corpus._ocr_pages("scan.pdf", {}, [1], 1) == {1: ""}


# -- a reading that ran away -----------------------------------------------------------------------

RUNAWAY = "〇三一" * 300


def test_a_page_the_model_got_stuck_on_is_read_with_basic(stand_in):
    order, runs = stand_in
    order(stuck={"2": RUNAWAY})

    read = corpus._ocr_pages("scan.pdf", {}, [1, 2, 3], 3)

    assert read == {n: PAGE.format(n=n) for n in (1, 2, 3)}
    second = runs()[1]
    assert second["argv"][second["argv"].index("--tier") + 1] == "basic" and pages_asked(second) == [2]


def test_a_stuck_page_that_cannot_be_read_again_is_left_unread(stand_in, monkeypatch):
    order, _runs = stand_in
    order(stuck={"2": RUNAWAY})
    real = mineru._parse

    def only_standard(path, numbers, tier, take=None):
        if tier == "basic":
            raise ValueError("no second reading")
        return real(path, numbers, tier, take)

    monkeypatch.setattr(mineru, "_parse", only_standard)
    meta = {}
    assert sorted(corpus._ocr_pages("scan.pdf", meta, [1, 2], 2)) == [1]
    assert "no second reading" in meta["ocr_error"]


def test_a_phrase_a_gazette_repeats_is_not_a_reading_that_ran_away():
    assert not mineru._stuck("部主管之全國收支 " * 8 + "其餘各項照舊辦理")


# -- pages lying on their side ---------------------------------------------------------------------

UPRIGHT = [0, 3.2]


def final(runs):
    return runs()[-1]


def test_a_page_on_its_side_is_turned_and_keeps_its_number(judged, stand_in):
    book, answer, runs = judged
    order, _runs = stand_in
    order(reads_at=270)
    answer(**{"2-270": UPRIGHT})

    read = mineru.read(book, [1, 2, 3], "advanced")[0]

    assert read == {n: PAGE.format(n=n) for n in (1, 2, 3)}
    assert final(runs)["turned"] == [0, 270, 0]


def test_which_way_up_is_what_basic_reads_more_of(judged, stand_in):
    """tesseract finds a page on its side, and on handwriting its direction is not to be trusted."""
    book, answer, runs = judged
    order, _runs = stand_in
    order(reads_at=270)
    answer(**{"2-90": UPRIGHT})

    mineru.read(book, [1, 2, 3], "advanced")

    assert [run["argv"][run["argv"].index("--tier") + 1] for run in runs()] == ["basic", "basic", "advanced"]
    assert [run["turned"] for run in runs()[:2]] == [[90], [270]]
    assert final(runs)["turned"] == [0, 270, 0]


def test_a_page_that_reads_the_same_both_ways_stays_as_it_lies(judged):
    book, answer, runs = judged
    answer(**{"2-270": UPRIGHT})

    mineru.read(book, [1, 2, 3], "advanced")

    assert final(runs)["argv"][1] == book


def test_a_book_of_upright_pages_goes_to_the_reader_as_it_is(judged):
    book, answer, runs = judged
    answer(**{"1-0": UPRIGHT, "2-0": UPRIGHT, "3-0": UPRIGHT})

    mineru.read(book, [1, 2, 3], "advanced")

    assert len(runs()) == 1 and runs()[0]["argv"][1] == book and pages_asked(runs()[0]) == [1, 2, 3]


def test_a_page_stays_as_it_is_on_weak_evidence(judged):
    book, answer, runs = judged
    answer(**{"2-90": [0, 0.3]})

    mineru.read(book, [1, 2, 3], "advanced")

    assert len(runs()) == 1 and runs()[0]["argv"][1] == book


def test_a_page_surer_as_it_lies_stays_as_it_lies(judged):
    book, answer, runs = judged
    answer(**{"2-0": [0, 2.6], "2-270": [0, 0.8]})

    mineru.read(book, [1, 2, 3], "advanced")

    assert len(runs()) == 1 and runs()[0]["argv"][1] == book


def test_a_stuck_page_is_read_again_turned(judged, stand_in):
    book, answer, runs = judged
    order, _runs = stand_in
    order(stuck={"1": RUNAWAY}, reads_at=270)
    answer(**{"1-270": UPRIGHT})

    read = mineru.read(book, [1, 2, 3], "advanced")[0]

    assert not mineru._stuck(read[1])
    last = final(runs)
    assert last["argv"][last["argv"].index("--tier") + 1] == "basic" and last["turned"] == [270]


def test_no_tesseract_leaves_every_page_as_it_is(judged, monkeypatch):
    book, answer, runs = judged
    answer(**{"2-270": UPRIGHT})
    monkeypatch.setattr(mineru, "find_tool", lambda name: None)

    mineru.read(book, [1, 2, 3], "basic")

    assert len(runs()) == 1 and runs()[0]["argv"][1] == book


def test_a_tesseract_off_the_path_is_run_from_where_it_was_found(judged, stand_in, tmp_path, monkeypatch):
    """Windows 11, 2026-11-10: the installer the wizard names leaves tesseract off PATH, so a machine
    that had it never ran it."""
    book, answer, runs = judged
    order, _runs = stand_in
    order(reads_at=270)
    answer(**{"2-90": UPRIGHT})
    elsewhere = tmp_path / "Tesseract-OCR"
    elsewhere.mkdir()
    program = (tmp_path / "bin" / mineru.TESSERACT).rename(elsewhere / mineru.TESSERACT)
    assert mineru.shutil.which(mineru.TESSERACT) != str(program)        # PATH no longer leads to it
    monkeypatch.setattr(mineru, "find_tool", lambda name: str(program) if name == mineru.TESSERACT else None)

    mineru.read(book, [1, 2, 3], "advanced")

    assert final(runs)["turned"] == [0, 270, 0]


def test_a_tesseract_that_fails_leaves_every_page_as_it_is(judged):
    book, answer, runs = judged
    answer(fail=True)

    mineru.read(book, [1, 2, 3], "basic")

    assert len(runs()) == 1 and runs()[0]["argv"][1] == book


def test_a_file_that_cannot_be_rendered_is_read_as_it_is(tmp_path, monkeypatch):
    monkeypatch.setattr(mineru, "find_tool", lambda name: "/usr/bin/tesseract")
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf")
    assert mineru._turns(str(broken), [1, 2]) == {}


# -- the headings a scan prints --------------------------------------------------------------------

def titled(page, *titles):
    return {str(page): [{"type": "paragraph_title", "index": i, "level": 2,
                         "content": [span(t)]} for i, t in enumerate(titles)]}


def test_the_titles_of_a_scan_come_back_by_page(stand_in):
    order, runs = stand_in
    order(whole=[1, 2], blocks={**titled(1, "第一編 總論"), **titled(2, "第二編 政治制度")})

    assert mineru.headings("scan.pdf", 2) == {1: [(2, "第一編 總論")], 2: [(2, "第二編 政治制度")]}
    assert runs()[0]["argv"][runs()[0]["argv"].index("--tier") + 1] == "basic"


def test_titles_are_read_at_basic_whatever_the_reading_tier_is(stand_in):
    """advanced marks a plate's caption, a leaf number and half a line as titles; basic marks the
    headings. Of 14 it gave on a gazetteer 4 were headings, where basic gave 12 of 12."""
    _order, runs = stand_in
    settings(mineru_tier="advanced")

    mineru.headings("scan.pdf", 1)

    assert runs()[-1]["argv"][runs()[-1]["argv"].index("--tier") + 1] == "basic"


def test_a_running_head_read_a_little_differently_each_page_is_not_a_heading(stand_in):
    order, _runs = stand_in
    order(whole=[1, 2, 3, 4], blocks={**titled(1, "第五關於督飭訴訟事項"), **titled(2, "第五關於督動訴訟事項"),
                                      **titled(3, "第五閣於督飭訴訟事項"), **titled(4, "附表六")})

    assert mineru.headings("scan.pdf", 4) == {4: [(2, "附表六")]}


def test_headings_that_differ_by_their_number_are_each_their_own(stand_in):
    order, _runs = stand_in
    order(whole=[1, 2, 3], blocks={**titled(1, "附表六"), **titled(2, "附表八"), **titled(3, "附表一六")})

    assert sorted(mineru.headings("scan.pdf", 3)) == [1, 2, 3]


def test_a_title_of_one_character_is_not_a_heading(stand_in):
    order, _runs = stand_in
    order(whole=[1, 2], blocks={**titled(1, "量"), **titled(2, "郵政")})

    assert mineru.headings("scan.pdf", 2) == {2: [(2, "郵政")]}


def test_a_scan_that_prints_no_titles_has_none(stand_in):
    order, _runs = stand_in
    order(whole=[1], blocks={"1": [{"type": "text", "index": 0, "content": [span("正文")]}]})

    assert mineru.headings("scan.pdf", 1) == {}


def test_a_failed_run_costs_the_titles_of_its_batch_only(stand_in):
    order, _runs = stand_in
    order(whole=list(range(1, 31)), fail_on=[1], blocks=titled(30, "兵事"))

    assert mineru.headings("scan.pdf", 30) == {30: [(2, "兵事")]}


# -- an outline from those headings ------------------------------------------------------------------

def outlined(tmp_path, monkeypatch, found):
    """``corpus.ingest`` of a scan whose pages MinerU marks with ``found`` ({page: [titles]})."""
    monkeypatch.setattr(corpus, "build_tree", lambda p, reason=None: corpus._note(reason, "tree", "none_found"))
    monkeypatch.setattr(mineru, "headings",
                        lambda path, count: {n: [(2, t) for t in titles] for n, titles in found.items()})
    scan = tmp_path / "scan.pdf"
    scan.write_bytes(b"%PDF-1.4 a scan\n")
    _layer(monkeypatch, [""] * 24)
    doc_id, _pages = corpus.ingest(str(scan), workspace=str(tmp_path))
    tree = Path(corpus.corpus_root(str(tmp_path)), doc_id, "tree.json")
    return json.loads(tree.read_text(encoding="utf-8")) if tree.exists() else None


def test_a_scan_with_no_outline_of_its_own_gets_one_from_its_headings(tmp_path, monkeypatch, stand_in):
    tree = outlined(tmp_path, monkeypatch, {3: ["郵政"], 10: ["教育分會"], 18: ["軍禮"]})

    assert [(n["title"], n["start_index"], n["end_index"]) for n in tree] == [
        ("郵政", 3, 9), ("教育分會", 10, 17), ("軍禮", 18, 24)]


def test_a_scan_whose_headings_are_too_few_keeps_page_navigation(tmp_path, monkeypatch, stand_in):
    assert outlined(tmp_path, monkeypatch, {3: ["郵政"], 10: ["教育分會"]}) is None


def test_an_outline_of_its_own_is_not_replaced(tmp_path, monkeypatch, stand_in):
    own = [{"title": "第一編 總論", "node_id": "0000", "start_index": 1, "end_index": 24}]
    monkeypatch.setattr(corpus, "build_tree", lambda p, reason=None: json.dumps(own, ensure_ascii=False))
    asked = []
    monkeypatch.setattr(mineru, "headings", lambda path, count: asked.append(path) or {})
    scan = tmp_path / "scan.pdf"
    scan.write_bytes(b"%PDF-1.4 a scan\n")
    _layer(monkeypatch, [""] * 24)

    doc_id, _pages = corpus.ingest(str(scan), workspace=str(tmp_path))

    assert json.loads(Path(corpus.corpus_root(str(tmp_path)), doc_id, "tree.json").read_text(encoding="utf-8")) == own
    assert asked == []


def test_a_document_ocrmypdf_read_is_not_asked_for_headings(tmp_path, monkeypatch, stand_in):
    settings(ocr_engine="ocrmypdf")
    asked = []
    monkeypatch.setattr(mineru, "headings", lambda path, count: asked.append(path) or {})
    monkeypatch.setattr(corpus, "build_tree", lambda p, reason=None: corpus._note(reason, "tree", "none_found"))
    monkeypatch.setattr(corpus, "_ocrmypdf_pages", lambda *a, **k: {n: PAGE.format(n=n) for n in range(1, 25)})
    scan = tmp_path / "scan.pdf"
    scan.write_bytes(b"%PDF-1.4 a scan\n")
    _layer(monkeypatch, [""] * 24)

    corpus.ingest(str(scan), workspace=str(tmp_path))

    assert asked == []


# -- through ingest --------------------------------------------------------------------------------

def test_a_scan_enters_the_corpus_with_the_pages_mineru_read(tmp_path, monkeypatch, stand_in):
    scan = tmp_path / "scan.pdf"
    scan.write_bytes(b"%PDF-1.4 a scan\n")
    _layer(monkeypatch, ["", "", ""])

    doc_id, pages = corpus.ingest(str(scan), with_tree=False, workspace=str(tmp_path))

    assert pages == 3
    stored = Path(corpus.corpus_root(str(tmp_path)), doc_id, "pages", "p0002.txt").read_text(encoding="utf-8")
    assert stored == PAGE.format(n=2)
    meta = json.loads(Path(corpus.corpus_root(str(tmp_path)), doc_id, "meta.json").read_text(encoding="utf-8"))
    assert meta["ocr"] is True


def test_a_page_a_failed_batch_lost_is_named_with_the_reason(tmp_path, monkeypatch, stand_in):
    order, _runs = stand_in
    order(fail_on=[3], stderr="out of memory")
    pages = [PAGE.format(n=n) for n in range(1, 56)]
    pages[1] = pages[52] = ""
    _layer(monkeypatch, pages)
    meta = {}

    corpus._pdf_pages("scan.pdf", meta)

    assert meta["ocr"] is True
    assert any("out of memory" in why for why in meta["unread_pages"].values())


def test_a_book_read_whole_keeps_its_numbers_past_a_page_it_lost(monkeypatch):
    monkeypatch.setattr(corpus, "_pdf_text_layer", lambda p: [])
    monkeypatch.setattr(corpus, "_ocr_pages", lambda *a, **k: {1: "one", 3: "three"})
    assert corpus._pdf_pages("scan.pdf", {}) == ["one", "", "three"]


def test_setup_names_the_command_that_installs_it():
    assert setup_wizard._install_command(mineru.BINARY) == "uv tool install mineru"


def test_a_page_waiting_for_the_chosen_program_counts_as_waiting_for_ocr(monkeypatch):
    settings(ocr_engine="mineru")
    monkeypatch.setattr(corpus.shutil, "which", lambda name: None)
    assert corpus._why_not_read({}) == mineru.MISSING
    assert mineru.MISSING.endswith(corpus._NO_OCR)

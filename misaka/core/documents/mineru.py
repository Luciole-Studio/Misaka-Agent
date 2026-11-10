"""MinerU, when the machine has it: the reader of scans that ocrmypdf is not.

ocrmypdf turned vertical Chinese and Japanese into Latin noise (a 1914 gazette: 7% of its characters
CJK, where MinerU gives 95%), which sent Sisters off to run OCR of their own. MinerU needs no
language named and reads a page's blocks in order. Like ocrmypdf it stays a program of its own,
never a dependency: it brings PyTorch and its models, and fetches those on first use.

Only ``mineru-kit parse`` without ``--remote`` is run: that is the local parser, and a page never
leaves the machine. The shape follows ``index._ocr_pages``: a timeout, a temporary directory, and
the failure handed back so a refusal can quote it.

MinerU does not stand a page up. Plates in a scanned book often lie on their side, and read that
way the model invents fluent text, so each page is judged first where the machine has tesseract.
Taking the longest of the model's readings does not work: it invents as much in a wrong direction,
and on 16 pages of such a book the longest was the right one on four.

It also marks the headings a page prints, which is where a scan's outline comes from when the PDF
itself states none (``index._outline_for``).
"""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter

from misaka.utils.tools_manager import find_tool

BINARY = "mineru-kit"
MISSING = "mineru-kit is not installed (uv tool install mineru)"
# Its strongest reading, and the only one that reads what it takes for a picture: a handwritten
# judgment on a plate came back at 85% of its characters, where the others returned none of it.
ADVANCED = "advanced"
BASIC = "basic"
# A failed run costs its batch, not the book, and the slowest pages seen (19 s each) leave a batch
# well inside the limit.
BATCH = 25
TIMEOUT = 900
# The picture-reading model behind standard and advanced can fall into repeating itself on a table
# or a contents page: 4 of 54 pages of two scans on standard, one of them 4,000 characters of a
# three-character run. basic has no such model. A phrase a gazette really repeats came to 8 on a page.
STUCK_RUN = 8
STUCK_REPEATS = 15
TESSERACT = "tesseract"
# Its orientation detection never took an upright page for one on its side, in 159 pages of print,
# manuscript and handwriting, and found six of nine pages that were. Which way up it does not know:
# on handwriting its answer changes with the resolution. basic does: the right way read more on all
# nine, three times as much on eight. A page upside down is left alone, being rare, and the answer
# five upright pages wrongly got.
SIDEWAYS = (90, 270)
TURN_CONFIDENCE = 0.6
TURN_TIMEOUT = 60
# What the model writes for a picture it finds nothing to read on.
NO_TEXT = "[NO TEXT]"
TITLE_BLOCKS = ("doc_title", "paragraph_title")
# A title this short is a leaf number or a stray character, not a heading.
TITLE_LEAST = 2
# The same heading on this many pages is the running head of the book, not a run of sections. OCR
# reads one a little differently each time, so near enough counts -- scaled by length, or "附表六"
# and "附表八" would be the same heading.
HEAD_PAGES = 3
HEAD_EDITS = 4

_RENDER_CHILD = """
import json, sys
import pypdfium2 as pdfium
source, folder, numbers = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
pdf = pdfium.PdfDocument(source)
for number in numbers:
    page = pdf[number - 1]
    for turn in (0, 90, 270):
        page.render(scale=2, rotation=turn, grayscale=True).to_pil().save(f"{folder}/{number}-{turn}.png")
"""
# A copy, not pixels: the reader gets the scan at its own resolution, only its rotation changed.
_TURNED_COPY_CHILD = """
import json, sys
import pypdfium2 as pdfium
source, target, turns = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
pdf, copy = pdfium.PdfDocument(source), pdfium.PdfDocument.new()
copy.import_pages(pdf, [number - 1 for number, _turn in turns])
for index, (_number, turn) in enumerate(turns):
    page = copy[index]
    page.set_rotation((page.get_rotation() + turn) % 360)
copy.save(target)
"""


def read(path, numbers, tier):
    """``({page: text}, why a run failed or None)`` for pages ``numbers`` of a PDF, every page when
    None. A page the model got stuck on is read again with ``basic``, and left unread if that
    fails: no reading is better than a page of noise that searches and citations would trust."""
    turns = _turns(path, numbers)
    pages, error = _read_batches(path, numbers, tier, turns)
    stuck = [number for number, text in sorted(pages.items()) if tier != BASIC and _stuck(text)]
    if stuck:
        again, second_error = _read_batches(path, stuck, BASIC, turns)
        pages.update(again)
        for number in set(stuck) - set(again):
            del pages[number]
        error = error or second_error
    return pages, error


def _turns(path, numbers):
    """``{page: clockwise degrees}`` for the pages of ``numbers`` lying on their side. tesseract says
    which they are and basic which way up: the way that reads more. Nothing here is worth failing a
    reading over, and a page that cannot be judged is read as it lies."""
    # ``find_tool``: on Windows its installer leaves it off PATH, and the path found is the one run.
    tesseract = None if numbers is None else find_tool(TESSERACT)
    if not tesseract:
        return {}
    try:
        lying = [number for start in range(0, len(numbers), BATCH)
                 for number in _lying(path, numbers[start:start + BATCH], tesseract)]
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    if not lying:
        return {}
    read = {turn: _read_batches(path, lying, BASIC, dict.fromkeys(lying, turn))[0] for turn in SIDEWAYS}
    turns = {}
    for number in lying:
        ink = {turn: len("".join(read[turn].get(number, "").split())) for turn in SIDEWAYS}
        if ink[90] != ink[270]:
            turns[number] = max(ink, key=ink.get)
    return turns


def _lying(path, numbers, tesseract):
    """Which of ``numbers`` tesseract finds surer of on their side than as they lie."""
    with tempfile.TemporaryDirectory(prefix="misaka-turns-") as tmp:
        subprocess.run([sys.executable, "-c", _RENDER_CHILD, path, tmp, json.dumps(numbers)],
                       capture_output=True, timeout=TIMEOUT, check=True)
        lying = []
        for number in numbers:
            sure = {turn: _upright(tesseract, os.path.join(tmp, f"{number}-{turn}.png")) for turn in (0, *SIDEWAYS)}
            sideways = max(sure[turn] for turn in SIDEWAYS)
            if sideways >= TURN_CONFIDENCE and sideways > sure[0]:
                lying.append(number)
    return lying


def _upright(tesseract, image):
    """How sure tesseract is that ``image`` stands the right way up: 0 when it says otherwise, or
    finds too little to say."""
    out = subprocess.run([tesseract, image, "stdout", "--psm", "0"], capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=TURN_TIMEOUT, check=False)
    turn = re.search(r"^Rotate: (\d+)", out.stdout, flags=re.MULTILINE)
    sure = re.search(r"^Orientation confidence: ([\d.]+)", out.stdout, flags=re.MULTILINE)
    return float(sure[1]) if turn and sure and int(turn[1]) == 0 else 0.0


def headings(path, count):
    """``{page: [(level, title)]}`` for the headings printed in a PDF, read at ``BASIC``.

    Always at ``BASIC``, whatever tier reads the text: ``advanced`` marks a plate's caption, a leaf
    number and half a line as titles, where ``basic`` marks the headings. On a 51-page gazetteer
    advanced gave 14 titles of which 4 were headings; basic gave 12, all of them headings.
    """
    if not shutil.which(BINARY):
        return {}
    numbers = list(range(1, count + 1))
    found, _error = _read_batches(path, numbers, BASIC, _turns(path, numbers), take=_titles)
    return _without_running_heads({page: titles for page, titles in found.items() if titles})


def _titles(document):
    """The heading blocks of each page, in the order they stand on it."""
    return {page: [(min(6, max(1, int(block.get("level") or 2))), " ".join(_text(block).split()))
                   for block in page_blocks if block.get("type") in TITLE_BLOCKS
                   and len(" ".join(_text(block).split())) >= TITLE_LEAST]
            for page, page_blocks in _page_blocks(document).items()}


def _without_running_heads(found):
    """Headings with the book's running head dropped, and a heading printed again on a later page
    kept only where it was last seen: a section's name standing on every page of it is one section."""
    seen = [(page, title) for page, titles in sorted(found.items()) for _level, title in titles]
    running, last = set(), {}
    for page, title in seen:
        on = {other for other, text in seen if _alike(title, text)}
        if len(on) >= HEAD_PAGES:
            running.add(title)
        last[title] = page
    kept = {page: [(level, title) for level, title in titles
                   if title not in running and last[title] == page]
            for page, titles in found.items()}
    return {page: titles for page, titles in kept.items() if titles}


def _alike(one, other):
    """Whether two headings are the same one, read a little differently by OCR."""
    allowed = min(len(one), len(other)) // HEAD_EDITS
    if abs(len(one) - len(other)) > allowed:
        return False
    row = list(range(len(other) + 1))
    for index, a in enumerate(one, 1):
        nxt = [index]
        for position, b in enumerate(other, 1):
            nxt.append(min(row[position - 1] + (a != b), row[position] + 1, nxt[position - 1] + 1))
        row = nxt
    return row[-1] <= allowed


def _read_batches(path, numbers, tier, turns, take=None):
    pages, error = {}, None
    batches = [None] if numbers is None else [numbers[i:i + BATCH] for i in range(0, len(numbers), BATCH)]
    for batch in batches:
        try:
            pages.update(_read_batch(path, batch, tier, turns, take))
        except subprocess.TimeoutExpired:
            error = f"timed out after {TIMEOUT} s"
        except (OSError, ValueError, subprocess.SubprocessError) as failure:
            error = str(failure)[:200]
    return pages, error


def _read_batch(path, batch, tier, turns, take=None):
    """A batch with a page on its side is read from a copy of its pages, the turned ones stood up."""
    if not batch or not turns.keys() & set(batch):
        return _parse_twice_if_it_hangs(path, batch, tier, take)
    with tempfile.TemporaryDirectory(prefix="misaka-turned-") as tmp:
        copy = os.path.join(tmp, "turned.pdf")
        subprocess.run([sys.executable, "-c", _TURNED_COPY_CHILD, os.path.abspath(path), copy,
                        json.dumps([[number, turns.get(number, 0)] for number in batch])],
                       capture_output=True, timeout=TIMEOUT, check=True)
        read = _parse_twice_if_it_hangs(copy, None, tier, take)
    return {batch[index - 1]: text for index, text in read.items()}


def _parse_twice_if_it_hangs(path, numbers, tier, take=None):
    """One run of 40 sat out the whole limit while three others finished (2026-11-08), and neither
    it nor its cause showed again in 36 more. Run alone the same pages took half a minute."""
    try:
        return _parse(path, numbers, tier, take)
    except subprocess.TimeoutExpired:
        return _parse(path, numbers, tier, take)


def _parse(path, numbers, tier, take=None):
    """``{page: text}`` from one run of ``mineru-kit`` over ``numbers``, every page when None. What
    it prints goes to a file: a pipe stays open until the last process it started lets go of it."""
    with tempfile.TemporaryDirectory(prefix="misaka-mineru-") as tmp:
        reading, printed = os.path.join(tmp, "reading.json"), os.path.join(tmp, "printed.log")
        argv = [BINARY, "parse", os.path.abspath(path), "-o", reading, "-f", "middle_json", "--tier", tier,
                "--ocr-mode", "ocr"]
        if numbers is not None:
            argv += ["-p", ",".join(str(number) for number in numbers)]
        with open(printed, "wb") as output:
            out = subprocess.run(argv, stdout=output, stderr=subprocess.STDOUT, timeout=TIMEOUT, check=False,
                                 cwd=tmp, env={**os.environ, "MINERU_TELEMETRY_DISABLED": "1"})
        if out.returncode != 0:
            with open(printed, encoding="utf-8", errors="replace") as f:
                raise ValueError(" ".join(f.read().split())[-200:] or f"exit {out.returncode}")
        with open(reading, encoding="utf-8") as f:
            return (take or _pages)(json.load(f))


def _page_blocks(document):
    """``{page: [block]}`` from one run's output."""
    pages = document.get("pages") if isinstance(document, dict) else None
    if not isinstance(pages, list):
        raise ValueError("its output holds no pages: not a MinerU version this reader knows")  # noqa: TRY004 - another program's output, not a caller's mistake
    try:
        return {int(page["page_idx"]) + 1: page.get("blocks") or [] for page in pages}
    except (AttributeError, KeyError, TypeError) as error:
        raise ValueError(f"its output has a page this reader cannot follow: {error!r}") from error


def _pages(document):
    return {page: "\n\n".join(filter(None, map(_text, blocks)))
            for page, blocks in _page_blocks(document).items()}


def _text(block):
    """What ``block`` says. Its content is a string, runs of one line, or blocks of its own (a
    table's caption and body), and those keep a line each. A picture's content is the text read off
    it, its pixels being elsewhere."""
    content = block.get("content")
    if isinstance(content, str):
        return _rows(content) if block.get("type") == "table_body" else "" if content == NO_TEXT else content
    if not isinstance(content, list):
        return ""
    parts = [item for item in content if isinstance(item, dict)]
    separator = "\n" if any("index" in item or "bbox" in item for item in parts) else ""
    return separator.join(filter(None, map(_text, parts)))


def _rows(table):
    """An HTML table as lines of tab-separated cells: a statistic means nothing without its row."""
    rows = []
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", table, flags=re.DOTALL | re.IGNORECASE):
        cells = re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row, flags=re.DOTALL | re.IGNORECASE)
        rows.append("\t".join(html.unescape(re.sub(r"<[^>]+>", "", cell)).strip() for cell in cells))
    return "\n".join(rows) or html.unescape(re.sub(r"<[^>]+>", " ", table)).strip()


def _stuck(text):
    """Whether a reading ran away: one run of characters over and over."""
    ink = "".join(text.split())
    runs = Counter(ink[start:start + STUCK_RUN] for start in range(len(ink) - STUCK_RUN + 1))
    return bool(runs) and runs.most_common(1)[0][1] >= STUCK_REPEATS

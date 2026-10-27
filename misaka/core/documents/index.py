"""Document ingestion, PageIndex navigation, literal search, and quote verification."""
import contextlib
import functools
import hashlib
import itertools
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile

from misaka.core.documents import htmltext, text_outline
from misaka.core.documents.pageindex import PageIndexUnavailable
from misaka.utils import atomic

DOC_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def corpus_root(workspace=None):
    """Project-local content-addressed store. Explicit workspace always wins over configuration."""
    if workspace is None:
        workspace = os.getcwd()
        override = os.environ.get("MISAKA_PAGEINDEX")
        if override:
            return os.path.expanduser(override)
    root = os.path.join(os.path.realpath(workspace), ".pageindex")
    if not under(root, workspace):
        raise ValueError("Document store resolves outside the workspace.")
    return root


def _real_directory(path, root):
    """True for a real directory below ``root``; redirects are not corpus data."""
    try:
        mode = os.stat(path, follow_symlinks=False).st_mode
        resolved, resolved_root = os.path.realpath(path), os.path.realpath(root)
        return (stat.S_ISDIR(mode) and resolved != resolved_root
                and os.path.commonpath((resolved_root, resolved)) == resolved_root)
    except (OSError, ValueError):
        return False


def _real_file(path, root):
    """True for a regular, non-symlink file contained by ``root``."""
    try:
        mode = os.stat(path, follow_symlinks=False).st_mode
        resolved, resolved_root = os.path.realpath(path), os.path.realpath(root)
        return (stat.S_ISREG(mode) and resolved != resolved_root
                and os.path.commonpath((resolved_root, resolved)) == resolved_root)
    except (OSError, ValueError):
        return False


def resolve_doc(doc_id, workspace=None):
    """Return one valid corpus directory, optionally owned by ``workspace``."""
    if not isinstance(doc_id, str) or not DOC_ID_RE.fullmatch(doc_id):
        return None
    root = os.path.realpath(corpus_root(workspace))
    ddir = os.path.join(root, doc_id)
    if not _real_directory(ddir, root):
        return None
    ddir = os.path.realpath(ddir)
    meta = _read_meta_at(ddir)
    if not isinstance(meta, dict):
        return None
    sha = str(meta.get("sha256") or "")
    if meta.get("doc_id") != doc_id or not re.fullmatch(r"[0-9a-f]{64}", sha) \
            or not sha.startswith(doc_id):
        return None
    paths = meta.get("paths")
    sources = paths if isinstance(paths, list) and paths else [meta.get("orig_path")]
    if workspace and not any(under(path, workspace) for path in sources):
        return None
    return ddir


def under(path, workspace):
    """True when ``path`` lives inside the folder ``workspace`` (symlinks resolved, whole path
    components: ``/`` contains ``/tmp/a``, ``/tmp/ab`` is not under ``/tmp/a``)."""
    if not path or not workspace:
        return False
    try:
        p, w = os.path.realpath(os.fspath(path)), os.path.realpath(os.fspath(workspace))
        return p != w and os.path.commonpath([p, w]) == w
    except (TypeError, ValueError):
        return False


# Content extraction and addressing

def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def claim_hash(doc_id, page, offset, quote):
    return hashlib.sha256(f"{doc_id}:{page}:{offset}:{quote}".encode()).hexdigest()


def _pdf_text_layer(p):
    """The text a PDF already carries, page by page; empty for a scan."""
    try:
        out = subprocess.run(["pdftotext", "-layout", p, "-"], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=300, check=False)
        if out.returncode == 0 and out.stdout.strip():
            return _form_feed_pages(out.stdout)
    except (OSError, subprocess.SubprocessError):
        pass
    # The pdfium fallback runs in a child process: a native fault inside the library on a
    # malformed PDF (a double free, seen once on a downloaded broker report) then ends the
    # child and the extraction reports no text, instead of taking the Sister's whole process
    # -- her session, her card -- down with SIGABRT.
    try:
        out = subprocess.run([sys.executable, "-c", _PDFIUM_TEXT_CHILD, p], capture_output=True,
                             text=True, encoding="utf-8", timeout=300, check=False)
        if out.returncode == 0 and out.stdout.strip():
            pages = json.loads(out.stdout)
            if isinstance(pages, list) and all(isinstance(page, str) for page in pages):
                return pages
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return []


_PDFIUM_TEXT_CHILD = """
import json, sys
import pypdfium2 as pdfium
pdf = pdfium.PdfDocument(sys.argv[1])
try:
    pages = [page.get_textpage().get_text_bounded() for page in pdf]
finally:
    pdf.close()
json.dump(pages, sys.stdout)
"""


def _form_feed_pages(text):
    """Split page-separated output into pages. pdftotext ends every page with a form feed, so the
    tail after the last one is no page. (ocrmypdf's sidecar puts one between pages instead, and
    stands one placeholder for a run of skipped pages: ``_sidecar_pages`` reads it.)"""
    pages = text.split("\f")
    if len(pages) > 1 and not pages[-1].strip():
        pages.pop()
    return pages


def _solid(pages):
    """How many pages carry more than a caption's worth of text."""
    return sum(1 for t in pages if len(t.strip()) > 20)


def _has_text_layer(pages):
    """True when extraction found a real text layer rather than a scan's stray page numbers.

    This is the check ``ingest`` refuses a document on, after OCR has had its turn. It used to be
    the switch for OCR as well, and as a whole-document rule it was the wrong one: a fifth of the
    pages carrying text sent none of the others to OCR (see ``_route``).
    """
    return bool(pages) and _solid(pages) >= max(1, len(pages) * 0.2)


def _note(meta, key, value):
    """Record one fact an extractor learned about the source, when the caller asked for them."""
    if meta is not None:
        meta[key] = value


# -- which pages need OCR -------------------------------------------------------------------------
#
# Page by page, after the gate in FrontierAgent's PDF reader (plugins/tools/_reader_pdf.py at
# 9e533db, the upstream of the Office port; THIRD_PARTY_NOTICES.md). The corpus used to ask one
# question of a whole file -- do a fifth of its pages carry text? -- and OCR all of it or none on
# the answer. A scanned book with a typeset introduction, a document collection whose facsimiles
# follow the editor's pages, and a scan stamped "Downloaded from ..." on every page all answered
# yes, and their scanned pages entered the corpus empty, without a word to anyone.
#
# Three of the upstream rules are taken: a page with no text, a page whose text layer is garbled,
# and a page dominated by a raster image. The last is narrowed to pages that also carry little
# text: upstream sends any image-dominated page to a layout-aware OCR service, and here the engine
# is tesseract, whose reading would stand in for a publisher's text layer -- the layer doc_verify
# locates quotations in. Its maths-font and vector-drawing rules route a page to vision, which in
# the corpus is doc_page_image's job, and are not taken.
EMPTY_PAGE_CHARS = 10    # fewer characters than this, running lines set aside: no text of its own
GARBLE_RATIO = 0.15      # replacement and private-use characters above this share: a broken font encoding
IMAGE_COVER = 1 / 6      # rasters covering more of the page than this ...
SPARSE_TEXT = 200        # ... over less text than this: a scan under a typeset line, or a plate and its caption

ROUTE_EMPTY = "no text layer"
ROUTE_GARBLED = "a garbled text layer"
ROUTE_IMAGE = "a scanned image with little text"


def _norm_line(line):
    return re.sub(r"\d+", "#", " ".join(line.split()))


def _running_lines(pages):
    """First and last lines the document repeats on half its pages or more -- a running head, a
    folio, a "Downloaded from ... on <date>" stamp -- with digits read as ``#``. Upstream strips
    these for display; here they only keep a stamp from passing for a page's own text. The stored
    page keeps them."""
    if len(pages) < 3:
        return frozenset()
    counts = {}
    for text in pages:
        lines = [line for line in text.splitlines() if line.strip()]
        for line in ({_norm_line(lines[0]), _norm_line(lines[-1])} if lines else ()):
            counts[line] = counts.get(line, 0) + 1
    return frozenset(line for line, n in counts.items() if n >= max(3, len(pages) // 2))


RUNNING_LINES_AT_MOST = 2   # a running head, a folio, a stamp: a line or two at either end


def _own_text(text, running):
    """A page's text without the running lines it shares with the rest of the document -- at most
    ``RUNNING_LINES_AT_MOST`` at each end: a table's numeric rows all read "# # #", and stripping
    while they matched took a whole statistical table for a running head (0.18.9 sweep)."""
    lines = [line for line in text.splitlines() if line.strip()]
    for _ in range(RUNNING_LINES_AT_MOST):
        if lines and _norm_line(lines[0]) in running:
            lines.pop(0)
    for _ in range(RUNNING_LINES_AT_MOST):
        if lines and _norm_line(lines[-1]) in running:
            lines.pop()
    return "\n".join(lines).strip()


def _garble_ratio(text):
    """The share of U+FFFD and private-use characters, over the characters that are not space:
    ``pdftotext -layout`` pads lines with spaces, which would dilute a share of the whole."""
    chars = [c for c in text if not c.isspace()]
    bad = sum(1 for c in chars if c == "�" or 0xE000 <= ord(c) <= 0xF8FF)
    return bad / len(chars) if chars else 0.0


def _route(pages, visuals=None):
    """``{page: reason}`` for the pages whose text layer does not carry what is printed on them.

    ``visuals(numbers)`` answers ``{page: (image_cover, marked)}`` from the file's own objects, and
    is asked only about pages short of ``SPARSE_TEXT``. Without an answer -- a DjVu, whose every
    rendered page is one picture, or a PDF pdfium cannot open -- only the text decides, and a page
    with none goes to OCR on the chance it holds some. A page that is known to print nothing at
    all, no image and no drawing, is blank rather than unread.
    """
    running = _running_lines(pages)
    own = [_own_text(text, running) for text in pages]
    sparse = [number for number, text in enumerate(own, 1) if len(text) < SPARSE_TEXT]
    seen = (visuals(sparse) if visuals and sparse else None) or {}
    routed = {}
    for number, text in enumerate(own, 1):
        cover, marked = seen.get(number, (None, True))
        if len(text) < EMPTY_PAGE_CHARS:
            if marked:
                routed[number] = ROUTE_EMPTY
        elif _garble_ratio(text) > GARBLE_RATIO:
            routed[number] = ROUTE_GARBLED
        elif cover is not None and cover > IMAGE_COVER and len(text) < SPARSE_TEXT:
            routed[number] = ROUTE_IMAGE
    return routed


# Top-level objects only: pdfium reports an object nested in a form XObject in the form's own
# coordinates, so a scan or a chart wrapped in a form (a common producer habit) is measured by the
# form's placement on the page instead. Boxes are counted from the crop box's lower-left corner,
# where a render's crop is counted from. Run in a child for the reason ``_PDFIUM_TEXT_CHILD`` is.
_PDFIUM_VISUALS_CHILD = """
import json, sys
import pypdfium2 as pdfium
import pypdfium2.raw as raw
IMAGE, PATH, FORM = raw.FPDF_PAGEOBJ_IMAGE, raw.FPDF_PAGEOBJ_PATH, raw.FPDF_PAGEOBJ_FORM
pdf = pdfium.PdfDocument(sys.argv[1])
out = {}
try:
    for number in json.loads(sys.argv[2]):
        page = pdf[number - 1]
        left, bottom, right, top = page.get_cropbox()
        width, height = max(right - left, 1.0), max(top - bottom, 1.0)

        def clip(box):
            x0, y0, x1, y1 = max(box[0], left) - left, max(box[1], bottom) - bottom, min(box[2], right) - left, min(box[3], top) - bottom
            return [x0, y0, x1, y1] if x1 > x0 and y1 > y0 else None

        images, strokes, ink, marked, form, counted = [], 0, None, False, None, False
        for obj in page.get_objects(max_depth=2):
            if obj.level == 0:
                form, counted, placed = (obj if obj.type == FORM else None), False, obj
            elif form is None:
                continue
            else:
                placed = form
            if obj.type == IMAGE:
                marked = True
                if obj.level == 0 or not counted:            # a form holding images is one figure
                    box = clip(placed.get_pos())
                    if box:
                        images.append(box)
                    counted = obj.level > 0
            elif obj.type == PATH:
                marked = True
                strokes += max(raw.FPDFPath_CountSegments(obj.raw), 0)
                box = clip(placed.get_pos())
                if box and (box[2] - box[0]) * (box[3] - box[1]) < 0.9 * width * height:   # not a frame round the page
                    ink = box if ink is None else [min(ink[0], box[0]), min(ink[1], box[1]),
                                                   max(ink[2], box[2]), max(ink[3], box[3])]
        cover = sum((b[2] - b[0]) * (b[3] - b[1]) for b in images) / (width * height)
        out[number] = {"cover": min(cover, 1.0), "marked": marked, "images": images, "strokes": strokes,
                       "ink": ink, "size": [width, height], "rotation": page.get_rotation()}
finally:
    pdf.close()
json.dump(out, sys.stdout)
"""


def _pdf_visuals(p, numbers):
    """What pages ``numbers`` of a PDF print besides text, ``{page: signals}``, or None when the
    file cannot be read: ``cover`` (the share of the page under rasters), ``marked`` (anything
    printed at all), ``images`` (each raster's box), ``strokes`` (path segments drawn) and ``ink``
    (the box round the drawing), with the page's ``size`` and ``rotation``."""
    try:
        out = subprocess.run([sys.executable, "-c", _PDFIUM_VISUALS_CHILD, p, json.dumps(numbers)],
                             capture_output=True, text=True, encoding="utf-8", timeout=300, check=False)
        if out.returncode == 0:
            return {int(number): signals for number, signals in json.loads(out.stdout).items()}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        pass
    return None


def _cover_and_marked(p, learned=None):
    """``_route``'s question about a PDF: ``{page: (image_cover, marked)}``. When pdfium cannot
    answer, ``learned["visuals_unread"]`` says so: the routing then went by text alone."""
    def ask(numbers):
        seen = _pdf_visuals(p, numbers)
        if seen is None:
            if learned is not None:
                learned["visuals_unread"] = True
            return None
        return {n: (s["cover"], s["marked"]) for n, s in seen.items()}
    return ask


# -- figures the text layer does not carry ----------------------------------------------------------
#
# FrontierAgent's reader marks, on every page it reads from the text layer, a picture the text
# cannot carry -- "figure not read: embedded image (covers 35% of page)" -- and counts a page's
# drawing operators without concluding anything from them, so the model knows where to look and
# looks only there. Its thresholds are taken as they are. A box the document repeats on half its
# pages or more (a page background, a letterhead) is set aside the way a running line is.
#
# A raster covering the whole page is the page itself: a scan, with a text layer OCR'd under it
# (archive.org's books, most of a humanities corpus) -- often twice over, a background and a
# foreground layer of the same scan. Upstream finds the figures inside such a page with a
# layout-aware OCR service; tesseract does not, so here a scanned page is named instead when it
# carries far less text than the book's scanned pages usually do -- which is what a map, a plate
# or a table of figures looks like from its text layer.
FIGURE_COVER = 0.08      # rasters over this share of a page: a figure ("_PDF_INLINE_IMG_COVER")
FIGURE_WIDTH = 0.15      # a raster narrower than this share of the page is a logo or an icon ("_OCR_FIG_MIN_WPCT")
FIGURE_STROKES = 100     # past this many path segments a page is drawn on, not only ruled ("_PDF_DRAW_OPS_FLOOR")
FIGURE_PAGE = 0.9        # a raster over this share of the page is the page's own scan
SCAN_SPARSE = 0.25       # a scanned page with under this share of the book's usual text
SCAN_USUAL_MIN = 400     # ... in a book whose scanned pages usually carry this much
FIGURES_VERSION = 1


def _figure_inventory(signals, count, texts=None):
    """``{page: {"cover", "images", "strokes", "boxes", "size", "rotation", "sparse"}}`` for the
    pages that carry what their text does not. ``boxes`` lists the figures in the order
    doc_page_image numbers them -- each image, then the drawing; ``sparse`` is a scanned page's
    text as a share of the book's usual, when it is low enough to say so."""
    def key(box):
        return tuple(round(v) for v in box)

    def area(box):
        return (box[2] - box[0]) * (box[3] - box[1])

    repeated = {}
    for s in signals.values():
        for box in {key(b) for b in s["images"]}:
            repeated[box] = repeated.get(box, 0) + 1
    scanned, out = {}, {}
    for page, s in sorted(signals.items()):
        width, height = s["size"]
        unique = list({key(b): b for b in s["images"]}.values())
        if any(area(b) >= FIGURE_PAGE * width * height for b in unique):
            scanned[page] = s
            continue
        images = [b for b in unique
                  if b[2] - b[0] >= FIGURE_WIDTH * width and repeated[key(b)] < max(3, count // 2)]
        cover = min(sum(area(b) for b in images) / (width * height), 1.0)
        pictured = cover > FIGURE_COVER
        drawn = s["strokes"] > FIGURE_STROKES and s["ink"] is not None
        if pictured or drawn:
            out[page] = {"cover": round(cover, 3) if pictured else 0.0, "images": len(images) if pictured else 0,
                         "strokes": s["strokes"] if drawn else 0,
                         "boxes": (images if pictured else []) + ([s["ink"]] if drawn else []),
                         "size": [width, height], "rotation": s["rotation"]}
    if texts and scanned:
        running = _running_lines(texts)
        lengths = {page: len(_own_text(texts[page - 1], running)) for page in scanned if page <= len(texts)}
        usual = sorted(lengths.values())[len(lengths) // 2] if lengths else 0
        for page, length in lengths.items():
            if usual >= SCAN_USUAL_MIN and length < SCAN_SPARSE * usual:
                width, height = scanned[page]["size"]
                out[page] = {"cover": 1.0, "images": 0, "strokes": 0, "boxes": [], "size": [width, height],
                             "rotation": scanned[page]["rotation"], "sparse": round(length / usual, 2)}
    return dict(sorted(out.items()))


_SLIDE_HEADING = re.compile(r"^## Slide (\d+)\b", re.MULTILINE)


def _slide_figures(pages):
    """The pages of a deck holding a slide the reader marked ``needs_vlm: true`` -- a picture, a
    grouped drawing, an arrow tied to nothing: meaning its text cannot carry (office/pptx.py,
    after FrontierAgent's "needs VLM"). ``{page: {"slide": N}}``."""
    out = {}
    for number, text in enumerate(pages, 1):
        heading = _SLIDE_HEADING.search(text)
        if heading and "needs_vlm: true" in text:
            out[number] = {"slide": int(heading.group(1))}
    return out


def _embedded_inventory(pages, listed):
    """``{page: {"embedded": [{"alt", "member"}]}}``: each picture a Word document or an EPUB
    embeds, put on the page its ``![...]`` marker is on, in marker order. Where the pages stop
    agreeing with the list -- a marker cut in two by a page break -- the rest is left out rather
    than put on the wrong page."""
    out, page, position = {}, 0, 0
    for image in listed:
        while page < len(pages):
            found = pages[page].find(image["marker"], position)
            if found >= 0:
                out.setdefault(page + 1, {"embedded": []})["embedded"].append(
                    {"alt": image["alt"], "member": image["member"]})
                position = found + len(image["marker"])
                break
            page, position = page + 1, 0
        else:
            break
    return out


def _stored_source(ddir):
    """The original file ingest copied beside the pages (``source`` and its suffix), or None."""
    for name in sorted(os.listdir(ddir)):
        if name.startswith("source.") and _real_file(os.path.join(ddir, name), ddir):
            return os.path.join(ddir, name)
    return None


def figures(doc_id, workspace=None):
    """What a document holds that its text does not carry, by page: ``{page: record}``.

    A PDF: its images and drawings (``_figure_inventory``), read once from the stored source --
    so a document indexed before this existed has them too -- and kept in ``figures.json``;
    pages read by OCR, or left unread, are left out when asked, since the whole page is the
    picture there and doc_read already says so of them. A deck: its slides marked as needing a
    vision model. A Word document or an EPUB: the pictures it embeds. Anything else: ``{}``.
    """
    ddir = resolve_doc(doc_id, workspace=workspace)
    source = _stored_source(ddir) if ddir else None
    if not source:
        return {}
    kind = os.path.splitext(source)[1].lower()
    meta = _read_meta_at(ddir) or {}
    count = int(meta.get("pages") or 0)
    if kind in (".pptx", ".ppt"):
        return _slide_figures(_stored_pages(ddir, count))
    if kind not in (".pdf", ".docx", ".epub"):
        return {}
    path = os.path.join(ddir, "figures.json")
    data = None
    if _real_file(path, ddir):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = None
    if not isinstance(data, dict) or data.get("version") != FIGURES_VERSION:
        if kind == ".pdf":
            signals = _pdf_visuals(source, list(range(1, count + 1)))
            if signals is None:
                return {}
            found = _figure_inventory(signals, count, _stored_pages(ddir, count))
        else:
            try:
                if kind == ".docx":
                    from misaka.core.documents.office import docx
                    listed = docx.images(source)
                else:
                    listed = _epub_images(source)
                    if int(meta.get("extract_version") or 1) < 3:
                        listed = _without_framed(listed)
            except (OSError, ValueError, zipfile.BadZipFile):
                return {}
            found = _embedded_inventory(_stored_pages(ddir, count), listed)
        data = {"version": FIGURES_VERSION, "pages": {str(page): v for page, v in found.items()}}
        atomic.write_text(path, json.dumps(data, ensure_ascii=False))
    pictured = set()
    if kind == ".pdf":
        pictured = _ocr_set(meta, count) | {int(n) for n in (meta.get("unread_pages") or {}) if str(n).isdigit()}
    return {int(n): v for n, v in data.get("pages", {}).items() if str(n).isdigit() and int(n) not in pictured}


def embedded_image(doc_id, page, figure, workspace=None):
    """``(bytes, the archive member)`` of picture ``figure`` (1-based) on ``page`` of a Word
    document or an EPUB, read out of the stored source; raises ValueError saying why not."""
    ddir = resolve_doc(doc_id, workspace=workspace)
    source = _stored_source(ddir) if ddir else None
    record = figures(doc_id, workspace=workspace).get(page) or {}
    listed = record.get("embedded") or []
    if not 1 <= figure <= len(listed):
        raise ValueError(f"Page {page} has {len(listed) or 'no'} picture{'' if len(listed) == 1 else 's'} "
                         f"the document embeds{f' (1..{len(listed)})' if listed else ''}; doc_read lists them.")
    member = listed[figure - 1]["member"]
    if not member:
        raise ValueError(f"Picture {figure} on page {page} is not stored as an image in the file "
                         f"(a chart or a shape the program draws, or a picture linked from outside).")
    try:
        with zipfile.ZipFile(source) as archive:
            data = _zip_read(archive, member, limit=64 * 1024 * 1024)
    except (OSError, zipfile.BadZipFile) as error:
        raise ValueError(f"Cannot read {member} from the stored file: {error}") from error
    if data is None:
        raise ValueError(f"{member} is missing from the stored file, or larger than 64 MiB.")
    return data, member


# -- the number printed on a page -------------------------------------------------------------------
#
# A page here is a page of the file, counted from 1, and that is the locator every tool and every
# citation uses -- it never shifts. What a reader looks up in the book is the number printed on the
# page, and the two part company at a cover, Roman-numbered front matter, an unnumbered plate, or a
# journal article whose pages start at 1361 (GitHub issue #9: a scanned book thirteen pages out, a
# delivered card citing the file's pages as the book's). So the printed number is found where it
# can be, and shown beside the file's page -- never in place of it.
#
# Two sources, in this order. A PDF that says what each page is labelled (/PageLabels: 32 of the 98
# PDFs in one real corpus, front matter and journal offsets alike). Otherwise the numbers at the top
# or bottom of the pages themselves, believed only where many pages nearby agree on the same
# distance from the file's page, so a year in a running head or a footnote number never passes.
LABELS_VERSION = 1
FOLIO_WINDOW = 12            # pages either side that are asked to agree
FOLIO_SUPPORT = 4            # ... and how many of them must
FOLIO_MIN_PAGES = 10         # fewer agreeing pages than this in a book: no printed numbers claimed
FOLIO_GAP = 6                # an unnumbered page between two that agree takes their number

_PDFIUM_LABELS_CHILD = """
import json, sys
import pypdfium2 as pdfium
pdf = pdfium.PdfDocument(sys.argv[1])
try:
    json.dump([pdf.get_page_label(i) or "" for i in range(len(pdf))], sys.stdout)
finally:
    pdf.close()
"""
_FOLIO_ALONE = re.compile(r"^[\s\-–—·•.\[(（]*(?:第\s*)?(\d{1,4})(?:\s*[页頁])?[\s\-–—·•.\])）]*$")
_FOLIO_LEADS = re.compile(r"^(\d{1,4})\s+\S")      # "338  EAST JIN": a head on a left-hand page
_FOLIO_ENDS = re.compile(r"\S\s+(\d{1,4})$")       # "The Mind in the Machine  219"


def _pdf_page_labels(p):
    """A PDF's own page labels, one per page (``""`` for an unlabelled one), or None."""
    try:
        out = subprocess.run([sys.executable, "-c", _PDFIUM_LABELS_CHILD, p], capture_output=True,
                             text=True, encoding="utf-8", timeout=120, check=False)
        labels = json.loads(out.stdout) if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return labels if isinstance(labels, list) and all(isinstance(x, str) for x in labels) else None


def _folios(text):
    """The numbers a page prints at its top or bottom: alone on one of its first or last two
    lines ("338", "- 338 -", "第338页"), at either end of a head ("338  East Jin", "East Jin
    338"), or at the end of a foot. A foot line that *starts* with a number is a footnote
    ("87 Zhuravsky, ..."): its numbers climb a page at a time often enough to pass for folios."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    found = set()
    for line, head in [(line, True) for line in lines[:2]] + [(line, False) for line in lines[-2:]]:
        match = _FOLIO_ALONE.match(line) or _FOLIO_ENDS.search(line) or (head and _FOLIO_LEADS.match(line))
        if match:
            found.add(int(match.group(1)))
    return found


def _inferred_labels(pages):
    """``{page: printed number}`` read off the pages, where it can be believed (see above)."""
    offsets = {n: {folio - n for folio in _folios(text)} for n, text in enumerate(pages, 1)}
    chosen = {}
    for n, candidates in offsets.items():
        best = None
        for d in candidates:
            support = sum(1 for m in range(n - FOLIO_WINDOW, n + FOLIO_WINDOW + 1) if d in offsets.get(m, ()))
            if support >= FOLIO_SUPPORT and (best is None or support > best[1]):
                best = (d, support)
        if best:
            chosen[n] = best[0]
    if len(chosen) < FOLIO_MIN_PAGES:
        return {}
    numbered = sorted(chosen)
    for left, right in itertools.pairwise(numbered):
        if chosen[left] == chosen[right] and 1 < right - left <= FOLIO_GAP:
            for n in range(left + 1, right):
                chosen[n] = chosen[left]
    return {n: str(n + d) for n, d in chosen.items() if n + d >= 1}


def page_labels(doc_id, workspace=None):
    """``({page: printed number}, where it came from)`` for a PDF or a DjVu, only where the printed
    number differs from the file's page; ``({}, None)`` when nothing is known. ``where`` is "the
    PDF's page labels" or "the page headers and footers". Found once and kept in ``labels.json``."""
    ddir = resolve_doc(doc_id, workspace=workspace)
    source = _stored_source(ddir) if ddir else None
    if not source or os.path.splitext(source)[1].lower() not in (".pdf", ".djvu"):
        return {}, None
    path = os.path.join(ddir, "labels.json")
    data = None
    if _real_file(path, ddir):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = None
    if not isinstance(data, dict) or data.get("version") != LABELS_VERSION:
        labels, where = {}, None
        own = _pdf_page_labels(source) if source.lower().endswith(".pdf") else None
        if own and any(label and label != str(n) for n, label in enumerate(own, 1)):
            labels, where = {n: label for n, label in enumerate(own, 1) if label}, "the PDF's page labels"
        else:
            meta = _read_meta_at(ddir) or {}
            labels = _inferred_labels(_stored_pages(ddir, int(meta.get("pages") or 0)))
            where = "the page headers and footers" if labels else None
        data = {"version": LABELS_VERSION, "where": where,
                "labels": {str(n): label for n, label in labels.items() if label != str(n)}}
        atomic.write_text(path, json.dumps(data, ensure_ascii=False))
    return {int(n): label for n, label in data.get("labels", {}).items() if str(n).isdigit()}, data.get("where")


def printed(doc_id, page, workspace=None):
    """``"printed p. 338"`` for a page whose printed number differs from its file page, else ``""``."""
    labels, _where = page_labels(doc_id, workspace=workspace)
    return f"printed p. {labels[page]}" if page in labels else ""


# -- OCR: an optional external binary, fail-closed ------------------------------------------------
#
# ingest used to refuse a scan with "run OCR first" -- advice the product could not carry out,
# because there was no OCR anywhere in it. Archival scans, pre-2000 books and 影印本 therefore
# could not enter the corpus at all. ocrmypdf is not a dependency and never becomes one: when it
# is absent the refusal stands, and it now names the install instead of an imperative into thin
# air.
OCR_BINARY = "ocrmypdf"
OCR_LANGS_DEFAULT = "eng+chi_sim+jpn"
OCR_MISSING = "ocrmypdf is not installed (brew install ocrmypdf)"
OCR_FOUND_NOTHING = "OCR found no text on it"      # a blank scan, a photograph, a map without names
# A book-length scan is minutes of work per hundred pages; the bound is what keeps one stuck OCR
# from owning an ingest forever (pdftotext above is bounded the same way, smaller).
OCR_TIMEOUT = 900
# ocrmypdf accepts --sidecar together with --pages from 11.7 on; an older one OCRs every page.
OCR_PAGES_SINCE = (11, 7)
_SKIPPED = re.compile(r"\[OCR skipped on page\(s\) (\d+)(?:-(\d+))?\]")


@functools.lru_cache(maxsize=1)
def _ocr_version():
    try:
        out = subprocess.run([OCR_BINARY, "--version"], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=60, check=False)
        found = re.match(r"\s*v?(\d+)\.(\d+)", out.stdout or "")
        return (int(found.group(1)), int(found.group(2))) if found else None
    except (OSError, subprocess.SubprocessError):
        return None


def _page_spec(numbers):
    """``[1, 2, 3, 7]`` -> ``"1-3,7"``, as ``--pages`` reads it."""
    runs = []
    for number in sorted(set(numbers)):
        if runs and number == runs[-1][1] + 1:
            runs[-1][1] = number
        else:
            runs.append([number, number])
    return ",".join(f"{a}-{b}" if a != b else f"{a}" for a, b in runs)


def _sidecar_pages(text, count=None):
    """``{page: text}`` from an ocrmypdf sidecar, or None when it does not line up with ``count``.

    The sidecar has one entry per page OCR read, and one placeholder for each run of pages it did
    not: ``[OCR skipped on page(s) 4-10]`` stands for seven pages (``merge_sidecars`` in
    ocrmypdf's _pipeline.py, worded the same from 11.7 to 17). Reading entries as pages, as this
    module once did, stored the placeholder as a page in place of the typeset pages it stood for
    and numbered every page after it one too low -- and doc_verify then located quotations on
    the wrong page.
    """
    entries = text.split("\f")
    for _attempt in range(2):
        out, page, aligned = {}, 1, True
        for entry in entries:
            run = _SKIPPED.fullmatch(entry.strip())
            if run:
                first, last = int(run.group(1)), int(run.group(2) or run.group(1))
                aligned = aligned and first == page
                page = last + 1
            else:
                out[page] = entry
                page += 1
        if aligned and (count is None or page - 1 == count):
            return out
        if len(entries) < 2 or entries[-1].strip():
            return None
        entries = entries[:-1]          # a closing form feed, written by some versions
    return None


def _ocr_pages(p, meta=None, numbers=None, count=None):
    """OCR pages ``numbers`` of a PDF (every page when None): ``{page: text}``, or None when that
    cannot be done.

    ``--force-ocr`` reads the named pages whatever text they carry -- a stamp or a garbled layer
    is exactly what ``--skip-text`` used to leave alone -- and every other page is skipped. The
    OCR'd PDF is discarded: the corpus stores the original file, whose sha256 is the document's
    identity, so rasterising a page here changes nothing a reader sees. The sidecar is the only
    output kept, and the reading is taken from it rather than from the OCR'd PDF's text layer,
    which reads Chinese and Japanese back with a space between characters.

    A run that fails writes why into ``meta['ocr_error']``. The most likely failure by far is a
    language pack that is not installed (``brew install tesseract-lang`` for the chi_sim and jpn
    defaults), and "OCR failed" without the reason would be the same dead end this whole path
    exists to remove. ``ingest`` quotes it in a refusal, and otherwise drops it from meta.json:
    the pages it cost are listed in ``unread_pages`` with the reason.
    """
    if not shutil.which(OCR_BINARY):
        return None
    from misaka.config.product import setting

    langs = setting("documents", "ocr_langs", OCR_LANGS_DEFAULT, str) or OCR_LANGS_DEFAULT
    # The system's temporary folder, not the source's: a library folder may be read-only, and since
    # one routed page is enough to OCR, such a folder refused whole books it used to index.
    with tempfile.TemporaryDirectory(prefix="misaka-ocr-") as tmp:
        sidecar = os.path.join(tmp, "sidecar.txt")
        argv = [OCR_BINARY, "--force-ocr", "--output-type", "pdf", "--sidecar", sidecar, "-l", langs]
        version = _ocr_version()
        if numbers is not None and (version is None or version >= OCR_PAGES_SINCE):
            argv += ["--pages", _page_spec(numbers)]
        try:
            out = subprocess.run(argv + [p, os.path.join(tmp, "ocr.pdf")],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=OCR_TIMEOUT, check=False)
            if out.returncode != 0:
                _note(meta, "ocr_error",
                      " ".join((out.stderr or "").split())[-200:] or f"exit {out.returncode}")
                return None
            with open(sidecar, encoding="utf-8") as f:
                text = f.read()
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            _note(meta, "ocr_error", str(error)[:200])   # missing or unreadable sidecar, timeout
            return None
    pages = _sidecar_pages(text, count)
    if pages is None:
        _note(meta, "ocr_error", f"the OCR output did not line up with the file's {count} pages")
    elif numbers is not None:
        pages = {number: pages[number] for number in numbers if number in pages}
    return pages


def _reads_as_text(text):
    """True when OCR read words or figures, not the specks tesseract finds in a photograph."""
    chars = [c for c in text if not c.isspace()]
    readable = sum(1 for c in chars if unicodedata.category(c)[0] in "LN")
    return len(chars) >= EMPTY_PAGE_CHARS and readable >= len(chars) / 2


def _merge_ocr(pages, ocr, routed, why):
    """``(pages, the pages OCR read, {page: why a page that needed OCR was not read})``.

    A routed page keeps its text layer and takes OCR's reading after it. A facsimile under an
    editor's typeset heading and a plate under its caption look alike to every signal ``_route``
    has, and only one of them prints more than its layer holds; keeping both loses neither, and
    doc_verify marks a quotation found on such a page as OCR. A garbled layer is the exception:
    it is the noise OCR is there to replace.
    """
    merged, ocred, unread = list(pages), [], {}
    for number, reason in sorted(routed.items()):
        reading = (ocr or {}).get(number)
        if reading is None:
            unread[number] = f"{reason}; {why}"
        elif not _reads_as_text(reading):
            unread[number] = f"{reason}; {OCR_FOUND_NOTHING}"
        else:
            layer = pages[number - 1]
            merged[number - 1] = (reading if reason == ROUTE_GARBLED or not layer.strip()
                                  else layer.rstrip() + "\n\n" + reading)
            ocred.append(number)
    return merged, ocred, unread


def _record_reading(meta, merged, ocred, unread):
    """``ocr`` and ``ocr_pages`` as doc_list/doc_read/doc_verify read them, and ``unread_pages``.

    ``ocr_pages`` is written only when the text layer survived somewhere, so the common case -- a
    scan, every page of it OCR'd -- does not carry a list of every page number.
    """
    if ocred:
        _note(meta, "ocr", True)
        if len(ocred) < len(merged):
            _note(meta, "ocr_pages", ocred)
    if unread:
        _note(meta, "unread_pages", {str(number): why for number, why in sorted(unread.items())})


def _why_not_read(learned):
    if not shutil.which(OCR_BINARY):
        return OCR_MISSING
    return f"OCR failed: {learned['ocr_error']}" if learned.get("ocr_error") else "OCR did not read it"


def _pdf_pages(p, meta=None):
    learned = meta if meta is not None else {}
    pages = _pdf_text_layer(p)
    if not pages:
        # Neither extractor could so much as count the pages: OCR the file whole, as it reads.
        read = _ocr_pages(p, learned)
        if not read:
            return []
        _note(meta, "ocr", True)
        return [read[number] for number in sorted(read)]
    routed = _route(pages, _cover_and_marked(p, learned))
    if not routed:
        return pages                        # a real text layer on every page: never OCR over it
    ocr = _ocr_pages(p, learned, sorted(routed), len(pages))
    merged, ocred, unread = _merge_ocr(pages, ocr, routed, _why_not_read(learned))
    _record_reading(meta, merged, ocred, unread)
    return merged


def _no_text_error(p, pages, ocr_error=None):
    """The refusal for a file whose extracted text is not a usable text layer.

    For a PDF the advice has to be one the reader can carry out: with ocrmypdf installed the scan
    has already been through it by the time this runs, so what is left to say is either why that
    failed or where to get it -- not the old "run OCR first", which named no way to.
    """
    name, solid = os.path.basename(p), _solid(pages)
    if os.path.splitext(p)[1].lower() != ".pdf":
        return ValueError(f"No text found in {name}: the file has no readable content.")
    if ocr_error:
        tried = f"ocrmypdf failed ({ocr_error})"
    elif shutil.which(OCR_BINARY):
        tried = "OCR produced no text either"
    else:
        tried = "this is a scan -- install ocrmypdf to index it (brew install ocrmypdf)"
    if pages and solid:
        return ValueError(
            f"Incomplete text layer: only {solid} of {len(pages)} pages contain text "
            f"({solid / len(pages):.0%}). This is probably a scanned document with a few "
            f"text pages; {tried}: {name}")
    return ValueError(f"No text layer found; {tried}: {name}")


# -- decoding text that is not UTF-8 --------------------------------------------------------------
#
# Strictly, or not at all. Text files used to be opened with errors='replace', which never fails
# and never says so: a Shift-JIS 青空文庫 book and a GB18030 file both entered the corpus as pages
# of '��y�͔L�ł���', and passed the text check because replacement characters are characters.
# Everything downstream -- doc_find, doc_verify, the ledger's quote check -- then operated on
# garbage in silence, and a ledger that "verifies" a quotation against mojibake is worse than one
# that cannot read the book at all.
#
# The order below is not the obvious one, and the reason is measured rather than theoretical.
# These encodings are not mutually exclusive: decoding one language's bytes under another's codec
# usually *succeeds*. Measured on a paragraph of each (the fixtures in
# tests/test_documents_encoding.py):
#
#   bytes \ codec   cp932       cp949            gb18030             cp950
#   Japanese        correct     refuses          clean, wrong Han    refuses
#   Korean          refuses     correct          clean, wrong Han    refuses
#   Chinese         refuses     Hangul/Han mix   correct             clean, wrong Han
#   Big5            refuses     refuses          private-use junk    correct
#
# So first-clean-wins is only as honest as its order, and no order suffices on its own: cp950
# accepts Chinese and gb18030 accepts Big5, so "must be tried first" has a cycle. Ordering does
# the work it can (cp932 is the pickiest and goes first; gb18030 accepts nearly any byte stream
# and goes late), and the coherence rule below catches the two mis-decodes that would otherwise
# win their slot.
#
# The codecs are the vendor supersets, not the bare standards, because the supersets are what the
# files are actually written in. Python's ``shift_jis`` is JIS X 0208 and *rejects* the NEC/IBM
# extension rows -- ① № Ⅰ ㈱ ℡ ㍉ 髙 﨑 -- which is to say it rejects what Japanese Windows and
# 青空文庫 write; the decode then fell through to gb18030, which accepts nearly anything, and a
# Japanese book entered the corpus as mojibake that the ledger would later "verify" quotations
# against. Each swap was checked exhaustively over the whole one- and two-byte space (the codecs
# are two-byte, so that is all of them) and each wide codec accepts every sequence its narrow one
# accepts -- zero new rejections. They disagree on 6 sequences (shift_jis/cp932), 0 (euc_kr/cp949)
# and 11 (big5/cp950), and every disagreement is the vendor variant of one glyph (wave dash vs.
# fullwidth tilde, ¢ vs. ￠), never a different character. big5hkscs was rejected for this: it
# reassigns 249 sequences in the ETen kana rows to HKSCS characters, so it is not a superset.
# tests/test_documents_encoding.py re-runs that check.
#
# Character *frequency* cannot help, however tempting: two two-byte codecs over the same bytes
# give the same frequency profile, only different characters. Telling rare Han from common Han
# needs a per-language character table -- that is a charset detector, and this is not one.
TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "cp932", "cp949", "gb18030", "cp950")

# The character ranges a document in any of these encodings is made of -- which is to say, what
# these charsets can actually encode, enumerated from the codecs themselves rather than guessed:
# ASCII and Latin letters, punctuation and currency, the Greek and Cyrillic rows (JIS X 0208 rows
# 6-7, KS X 1001, GB 2312 all carry them), arrows and mathematical operators, the enclosed and
# squared forms 青空文庫 and Japanese Windows are full of (① ㈱ ㍉ Ⅰ), box drawing and the
# geometric shapes that rule a Japanese table (■ ● ★), Bopomofo, radicals, CJK punctuation and
# forms, kana, Hangul, Han.
#
# Running that census over every one- and two-byte sequence of all four codecs leaves the private
# use areas as essentially the only thing outside these ranges -- which is the point. Private use
# is what a wrong codec produces, and control bytes are what any codec produces from a file that
# is not text. Big5 read as GB18030 is still 39% outside, eight times the rule's bar.
_TEXT_RANGES = ((0x09, 0x0D), (0x20, 0x7E), (0xA0, 0x24F), (0x370, 0x4FF), (0x1100, 0x11FF),
                (0x2000, 0x206F), (0x20A0, 0x20CF), (0x2100, 0x22FF), (0x2460, 0x24FF),
                (0x2500, 0x26FF), (0x2E80, 0x2FDF), (0x3000, 0x30FF), (0x3100, 0x318F),
                (0x31F0, 0x31FF), (0x3200, 0x33FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF),
                (0xAC00, 0xD7A3), (0xF900, 0xFAFF), (0xFE30, 0xFE6F), (0xFF00, 0xFFEF),
                (0x20000, 0x2FA1F))
# A wrong codec is wrong on every line, so a sample settles it; a book pays for its decode, not
# for a second pass over itself to guess the language.
_COHERENCE_SAMPLE = 64 * 1024

# The half-width katakana block U+FF61-FF9F is where a wrong codec lands most often, because the
# single bytes 0xA1-0xDF cp932 spends on it are exactly the trail bytes GB 2312, KS X 1001 and Big5
# spend on ordinary characters -- and cp932 is tried before all three. Quantity cannot separate
# that from a real half-width file (they exist: old data files, receipt and EDI records), because
# both are wall-to-wall kana. Orthography can: the syllabary's modifiers attach to a fixed set of
# bases, and a codec that is scattering bytes attaches them at random. Measured over 910 Chinese
# samples read as cp932, 786 carry a modifier and 49% of those modifiers are illegally placed;
# over real half-width Japanese, 47 modifiers and not one violation.
_KANA_BASE = frozenset(range(0xFF71, 0xFF9E))                       # ｱ..ﾝ, a full kana
_KANA_DAKUTEN = frozenset({0xFF66, 0xFF73, 0xFF9C}                  # ｦ ｳ ﾜ
                          | set(range(0xFF76, 0xFF85))              # ｶ..ﾄ
                          | set(range(0xFF8A, 0xFF8F)))             # ﾊ..ﾎ
_KANA_HANDAKUTEN = frozenset(range(0xFF8A, 0xFF8F))                 # ﾊ..ﾎ and nothing else
# How much better a later codec has to look before it takes a document away from an earlier one.
# The wrong readings measured here score 0.29 to 1.00 against a right reading's 0.00, so the bar
# can sit low without being reachable by noise -- a Japanese file with a stray gaiji in the private
# use area scores a thousandth and keeps its own codec.
_CODEC_MARGIN = 0.05

# The encoding an HTML or XML file names for itself -- `<meta charset>`, the `http-equiv`
# content type, or the XML declaration. Archive pages in windows-1251 or koi8-r are none of the
# guessed codecs and were refused (2026-09-28, the 1812 manifesto).
_DECLARED_CHARSET = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?([A-Za-z0-9._:-]+)|<\?xml[^>]+encoding\s*=\s*["']([A-Za-z0-9._:-]+)""",
    re.IGNORECASE)


def _mojibake(text, cjk_codec=True):
    """How much of a decode is evidence that the codec was wrong -- the share of the sample that
    only a wrong codec produces, ``0.0`` for a decode that reads as writing throughout -- or
    ``None`` when it is not writing at all and has to be refused outright.

    Refusal comes first. One rule holds for every decode, UTF-8 included:

    0. a NUL is not a character in any document. It is a valid UTF-8 *byte*, though, so a BOM-less
       UTF-16LE stream of Latin text decodes as UTF-8 without an error and enters the corpus with
       every second character a NUL. Self-validating means the bytes are well-formed UTF-8, not
       that the file was UTF-8.

    The other three apply only to a legacy CJK codec (``cjk_codec``), because they are measured
    against the table above -- the character ranges *those* encodings can express. A UTF-8 file is
    not a guess and must not be judged by that table: Cyrillic, Greek, Arabic, Devanagari and
    emoji are all outside it, and a Russian book is not incoherent.

    1. more than 5% of the sample outside the ranges of written text -- Big5 read as GB18030 is
       39% private-use characters, and a binary file read as anything is mostly control bytes;
    2. CJK characters that stand alone rather than in runs. This is the one that catches a wrong
       codec over mostly-ASCII text, where rule 1 sees almost nothing: Latin-1 Swedish read as
       GB18030 comes out 'H鋜 鋜 gudarnas 鋘gar' -- 13% of the sample, no private-use characters,
       and 100% of the non-ASCII tail is Han, so counting the tail cannot separate it either. What
       separates it is that every one of those Han characters is alone between ASCII letters,
       because each accented byte ate the letter after it. Measured as the fraction of CJK
       characters with a CJK neighbour: Japanese 1.00, Chinese 1.00, Big5 1.00, Korean 0.97, a
       Shift-JIS README that is 88% ASCII 1.00, an all-citations Japanese file (every character
       between digits, the worst real case) 0.50 -- against 0.00 for Swedish under cp932, gb18030
       and cp950 alike. The bar is half, and a sample that is majority CJK is exempt outright, so
       no real CJK book can ever be turned away by this rule;
    3. Hangul beside kana or beside Han, either one in bulk -- Chinese read as EUC-KR comes out
       58% Hangul and 42% Han, while Korean prose is Hangul with hanja as a garnish. The threshold
       is a fifth for both, which admits ordinary hanja and the Japanese terms Korean scholarship
       on Japan quotes in kana -- KS X 1001 encodes kana, and a bare truthiness test on it handed
       correct EUC-KR documents to GB18030 over a single quoted word.

    A decode that survives all four is still only a candidate, because these encodings overlap:
    the same bytes read as two of them can both come out looking like writing, and the loser is
    then decided by whichever codec ``TEXT_ENCODINGS`` happens to try first. So what is left is
    counted rather than refused -- every character that only a wrong codec would have produced:

    4. private-use characters, the same ones rule 1 counts. Under the 5% bar they were free; two
       of them in a seven-character sample are not, and that is exactly what Big5 read as GB18030
       looks like: '第一章 緒論。' comes out as '材?彻 狐阶?' with a U+E5E6 and a U+E4C9
       standing where the '?' are. This is the only signal that sees the wrong-Han-for-Han theft
       at all: the rest of that output *is* ordinary Han in U+4E00-9FFF, so no rule about what
       CJK looks like can tell it from the real thing;
    5. half-width kana in a decode that also holds full-width script or private use. Real writing
       does not mix them in that proportion -- a legacy half-width file is half-width throughout,
       and a modern Japanese document that quotes half-width kana is full-width throughout. A
       Chinese or Korean sentence read as cp932 is 73%-91% half-width kana studded with a few
       stray Han, which is neither;
    6. a character standing where its own script never puts it. Two of those are worth counting:
       a kana modifier on a base that cannot take it (see ``_KANA_DAKUTEN`` above), which is what
       is left for the half-width file that carries no other script at all and where rule 5 is
       blind by construction -- it is what keeps genuine half-width Japanese decodable instead of
       refused and handed to GB18030 as a wall of Han; and a Han character immediately behind a
       hangul syllable, which is how Chinese read as cp949 gives itself away when it comes out
       mostly hangul and rule 3 therefore says nothing.

    What it does not catch, written down rather than papered over: a file too short to carry the
    evidence (a fragment of two or three characters is decided by codec order and nothing else), a
    BOM-less UTF-16 file whose text is CJK (its bytes carry no NULs), a two-byte sequence that is
    also valid UTF-8 ('为 none' in GB18030 is), and a Korean document in heavy 국한문혼용 (a fifth
    or more hanja), which rule 3 turns away and GB18030 then reads as Han. Those cases buy correct
    Simplified Chinese and correct modern Korean, which are the common ones; ``meta['encoding']``
    records the choice on every document, so a reader who sees the wrong one can convert the file
    and re-ingest.
    """
    sample = text[:_COHERENCE_SAMPLE]
    alien = han = kana = hangul = halfwidth = cjk = alone = run = misplaced = 0
    prev = 0
    for ch in sample:
        o = ord(ch)
        here = False
        if 0x20 <= o <= 0x7E or 0x09 <= o <= 0x0D:                      # ASCII, the common case
            pass
        elif o == 0:
            return None                                                 # rule 0
        elif 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0xF900 <= o <= 0xFAFF:
            han += 1
            here = True
            # Korean writes the stem in hanja and the particle after it in hangul, never the other
            # way round, so a Han character *behind* a hangul syllable is not Korean orthography.
            # Chinese read as cp949 comes out mostly hangul with hanja wedged in at random: 51% of
            # its Han sit behind a syllable, against 0 of real Korean's.
            misplaced += 0xAC00 <= prev <= 0xD7A3                       # rule 6
        elif 0x3040 <= o <= 0x30FF or 0x31F0 <= o <= 0x31FF:
            kana += 1
            here = True
        elif 0xAC00 <= o <= 0xD7A3 or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F:
            hangul += 1
            here = True
        elif 0xFF61 <= o <= 0xFF9F:               # half-width kana
            halfwidth += 1
            here = True
            if 0xFF67 <= o <= 0xFF6F:             # a small kana or ｯ, after a kana or a mark (ｳﾞｧ)
                misplaced += prev not in _KANA_BASE and prev not in (0xFF9E, 0xFF9F)
            elif o == 0xFF70:                     # ｰ, after any of them (ﾃﾞｰﾀ, ﾌｧｰｽﾄ)
                misplaced += not 0xFF67 <= prev <= 0xFF9F
            elif o == 0xFF9E:
                misplaced += prev not in _KANA_DAKUTEN
            elif o == 0xFF9F:
                misplaced += prev not in _KANA_HANDAKUTEN
        elif 0xFF00 <= o <= 0xFFEF:               # full-width forms
            here = True
        elif not any(lo <= o <= hi for lo, hi in _TEXT_RANGES):
            alien += 1
        if here:
            cjk += 1
            run += 1                              # how long the CJK run ending here is so far
        else:
            alone += run == 1
            run = 0
        prev = o
    alone += run == 1                             # a sample that ends mid-run
    if not cjk_codec:
        return 0.0
    if alien > 2 and alien * 20 > len(sample):
        return None
    if cjk and cjk * 2 < len(sample) and (cjk - alone) * 2 < cjk:
        return None
    script = han + hangul + kana
    if hangul and (kana * 5 > script or han * 5 > script):
        return None
    wrong = alien + misplaced
    if halfwidth * 2 > cjk and script + alien:                          # rule 5
        wrong += halfwidth
    return wrong / max(1, len(sample))


def _decode_bytes(raw, what):
    """``(text, encoding)`` for bytes that are supposed to be text, decoded strictly -- never with
    replacements. Every text-shaped format in the corpus comes through here, files and archive
    members alike, so there is one answer to "what is this written in" and one refusal.

    UTF-8 is self-validating, so it is not a guess; every legacy codec after it is a guess and has
    to come out looking like writing. When none does, the bytes are refused and ``scan`` reports
    the file skipped the way it reports a scan that needs OCR.

    Taking the first guess that looked like writing was the bug: these codecs overlap, so a short
    Chinese sentence is *also* a clean-looking run of half-width katakana and a short Big5 one is
    *also* clean-looking Simplified Han, and whichever codec came first in ``TEXT_ENCODINGS`` took
    them. Every candidate is scored instead and the least mojibake-shaped one wins, with the
    tabulated order left to break ties -- so a document only ever changes hands to a codec that
    reads it visibly better, never merely later. A file written in the codec tried first still
    costs exactly one decode: scoring zero ends the loop, and real writing scores zero.
    """
    declared = _DECLARED_CHARSET.search(raw[:4096])
    if declared:
        encoding = (declared.group(1) or declared.group(2)).decode("ascii").lower()
        try:
            return raw.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            pass                                  # a false declaration falls back to the guesses
    best = best_text = best_encoding = None
    for encoding in TEXT_ENCODINGS:
        if encoding == "utf-8" and raw.startswith(b"\xef\xbb\xbf"):
            continue          # plain utf-8 decodes a BOM into the text; utf-8-sig drops it
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        wrong = _mojibake(text, cjk_codec=not encoding.startswith("utf-8"))
        if wrong is None:
            continue
        if not wrong:
            return text, encoding
        if best is None or wrong < best - _CODEC_MARGIN:
            best, best_text, best_encoding = wrong, text, encoding
    if best_text is None:
        if what.lower().endswith((".htm", ".html")):
            try:                                  # the HTML standard's default for a page that names none
                return raw.decode("cp1252"), "cp1252"
            except UnicodeDecodeError:
                pass
        raise ValueError(f"not UTF-8 text; re-encode or name the encoding: {what}")
    return best_text, best_encoding


def _decode_text(p):
    """``(text, encoding)`` for one text file on disk."""
    with open(p, "rb") as f:
        raw = f.read()
    return _decode_bytes(raw, os.path.basename(p))


def _read_text(p, meta=None):
    """One text file's contents. Every text-shaped format goes through here."""
    text, encoding = _decode_text(p)
    _note(meta, "encoding", encoding)
    return text


PAGE_CEILING = 4          # a page may run this many times over ``chars`` before it is cut anyway


def _paginate(s, chars=3000):
    """Cut running text into pages at paragraph breaks -- a format without pages still needs
    somewhere for a citation to point.

    ``chars`` is where a paragraph break becomes a page break, so it is a floor, not a ceiling:
    a CSV, a .jsonl, a log -- all readable formats here -- have no blank line anywhere and used to
    come out as one page the size of the whole file. That page is not a display problem but a
    quadratic-feeling one: ``_locate`` builds one span entry per character of the page it hits, so
    a single ``doc_verify`` against a 9.7 MB page cost seconds and gigabytes. Past
    ``chars * PAGE_CEILING`` the page is cut at the last line break in the ceiling's back half
    instead (mid-line if there is none), so every page is at least half the ceiling. Pages still
    concatenate back to the source exactly."""
    if not s.strip():
        return []
    ceiling = max(1, int(chars)) * PAGE_CEILING
    out, buf = [], []

    def flush():
        page = "".join(buf)
        buf.clear()
        while len(page) > ceiling:
            # Only the back half is searched for a line break: a heading's newline near the start
            # would otherwise cut a one-line page off the front of a book chapter.
            cut = page.rfind("\n", ceiling // 2, ceiling)
            cut = cut + 1 if cut > 0 else ceiling
            out.append(page[:cut])
            page = page[cut:]
        if page:
            out.append(page)

    size = 0
    for para in re.split(r"(\n\s*\n)", s):
        buf.append(para)
        size += len(para)
        if size >= chars:
            flush()
            size = 0
    if buf:
        flush()
    return out


# Every extractor takes the same two arguments: the file, and a dict to record what it learned
# about the source in (the encoding it had to decode, whether the pages came out of OCR). ingest
# writes that into meta.json, so what a page is made of stays on the record.

def _text_pages(p, chars=3000, meta=None):
    return _paginate(_read_text(p, meta), chars)


def _html_pages(p, chars=3000, meta=None):
    """A saved web page or an archival HTML file as the text a reader sees. Tags, scripts and
    stylesheets are not text anybody quotes, and they used to enter the corpus verbatim."""
    return _paginate(htmltext.readable(_read_text(p, meta))[0], chars)


def _office_pages(p, chars=3000, meta=None):
    """A workbook, deck or data file as pages, cut at its own boundaries.

    The rendering itself is ``documents/office``'s and is shared with ``core/tools/read.py``
    verbatim -- which is the point: what a model reads in the working directory is what it
    later cites out of the corpus, so a quotation copied from one verifies against the
    other. See that package's docstring.

    Paging is two steps rather than ``_paginate`` alone. A sheet is the unit a citation can
    name, so blocks are cut there first; only a block that is still oversized goes on to
    ``_paginate``, and every page it produces after the first opens with the block's own
    title and column header, or 400 lines of numbers arrive with nothing saying what they
    are. The import is local because ``office`` is only needed for these suffixes and
    openpyxl is not free to import.
    """
    from misaka.core.documents import office

    fmt = office.format_of(p) or office.soffice.LEGACY.get(os.path.splitext(p)[1].lower())
    rendered = office.render(p, meta=meta)
    _note(meta, "office_format", fmt)
    pages = []
    for block in office.blocks(rendered, fmt):
        parts = _paginate(block, chars)
        header = office.resume_context(block, fmt)
        if header and len(parts) > 1:
            parts = [parts[0], *[header + part for part in parts[1:]]]
        pages.extend(parts)
    return pages


def _legacy_office_pages(p, chars=3000, meta=None):
    """Legacy files share the read tool's renderer and the corpus' normal pagination."""
    return _office_pages(p, chars, meta)


def _xls_pages(p, chars, meta):
    """A pre-2007 workbook through ``xlrd``, or ``None`` when that cannot read it either.

    Values only: BIFF keeps formulas in a form xlrd does not evaluate and the styling is
    not worth reconstructing. The rows go through the same relational rendering a csv
    takes, so a quotation of a row verifies the way every other one in this corpus does.
    """
    try:
        import xlrd
    except ImportError:                                             # pragma: no cover
        return None
    try:
        book = xlrd.open_workbook(p)
    except Exception as error:              # noqa: BLE001 - xlrd raises its own hierarchy
        _note(meta, "xlrd_error", str(error)[:200])
        return None
    from misaka.core.documents.office import xlsx as renderer

    _note(meta, "office_format", "xls")
    pages = []
    for sheet in book.sheets():
        rows = [[sheet.cell_value(r, c) for c in range(sheet.ncols)]
                for r in range(sheet.nrows)]
        if not rows:
            continue
        pages.extend(_paginate(renderer.render_rows(sheet.name, rows), chars))
    return pages or None


# -- EPUB ---------------------------------------------------------------------------------------
#
# An EPUB is a zip holding XHTML chapters plus a package document that says which of them the
# book consists of and in what order. Reading it means reading those three things -- the
# container, the package document, the spine -- with the standard library and nothing else.
#
# Every member is read by the name the package document gives, out of the archive; nothing is
# ever extracted to a path. A crafted href of "../../etc/passwd" is therefore a lookup that
# misses, not a file on this machine.

# An archive member declares its uncompressed size in the central directory, so a "book" that
# would decompress to a gigabyte is skipped before it is read -- an EPUB now also arrives by
# download and is indexed on arrival, and the reader that opens whatever it is handed is the
# one that gets handed a zip bomb. 16 MiB is several million words of XHTML; a chapter that
# large is not a chapter.
_EPUB_MEMBER_BYTES = 16 * 1024 * 1024

# The member cap bounds one chapter; these bound the book, which is the number an archive can
# multiply. Many manifest items may share one href and the spine may list them all, so a ~250 KB
# archive holding a single 1 MB member can name it a thousand times: deduplication below removes
# that amplification, and these two are what remains true when the members are all distinct.
#
# 16 Mi characters is the whole book's markup, counted as it is decoded. Every codec here yields
# at most one character per byte read, so it bounds the decompression too, and markup is a third
# to a half of a chapter file -- so this is roughly a ten-million-character book. War and Peace is
# 3.2M characters and the complete Shakespeare 5.5M: the ceiling holds either one twice over, and
# a "book" that does not fit is not one. 10,000 spine entries is the same judgement about work
# rather than memory: one archive read each, and a page-per-file scan of a 1,000-page book uses a
# tenth of it.
_EPUB_BOOK_CHARS = 16 * 1024 * 1024
_EPUB_SPINE_MAX = 10_000

# What a spine entry has to be declared as to be read as text. EPUB content documents are
# XHTML; a spine that points at an image would otherwise render as junk.
_EPUB_TEXT_TYPES = ("html", "xml")


def _zip_read(archive, name, limit=_EPUB_MEMBER_BYTES):
    """One archive member's bytes, or None when it is missing, oversized, or damaged."""
    try:
        if archive.getinfo(name).file_size > limit:
            return None
        with archive.open(name) as member:
            return member.read(limit)       # the declared size is attacker-written; this is not
    except Exception:  # noqa: BLE001 - a missing or corrupt member is not a chapter; the rest of the book still reads
        return None


def _epub_local(tag):
    """An XML tag without its namespace. Books in the wild declare the container and package
    namespaces inconsistently or not at all, and a missing prefix is not a reason to refuse a
    book every reader opens."""
    return str(tag).rsplit("}", 1)[-1]


def _epub_xml(data, what):
    """Parse one of the book's own XML documents, or say which one is broken.

    Entity declarations are refused rather than expanded: expat expands internal entities, so
    a dozen nested ones are a megabyte of memory and thirty are the machine. No package
    document has ever needed one.
    """
    if data is None:
        raise ValueError(f"Not a readable EPUB: {what} is missing from the archive")
    if b"<!ENTITY" in data:
        raise ValueError(f"Not a readable EPUB: {what} declares XML entities")
    try:
        return ET.fromstring(data)
    except ET.ParseError as error:
        raise ValueError(f"Not a readable EPUB: {what} is malformed XML ({error})") from error


def _epub_member(base, href):
    """The archive member a manifest href names, relative to the package document, or ""."""
    href = urllib.parse.unquote(href.split("#", 1)[0].strip())
    if not href or "://" in href:                     # a remote chapter is not part of the book
        return ""
    return posixpath.normpath(posixpath.join(base, href)).lstrip("/")


def _epub_spine(archive):
    """``(title, [member names in reading order])`` for an open EPUB.

    Raises ValueError naming what is wrong with anything that is not one: a file renamed to
    .epub, a zip with no container, a package document that lists nothing readable.
    """
    container = _epub_xml(_zip_read(archive, "META-INF/container.xml"), "META-INF/container.xml")
    opf_path = next((element.get("full-path") for element in container.iter()
                     if _epub_local(element.tag) == "rootfile" and element.get("full-path")), None)
    if not opf_path:
        raise ValueError("Not a readable EPUB: container.xml names no package document")
    opf_path = opf_path.lstrip("/")
    package = _epub_xml(_zip_read(archive, opf_path), opf_path)
    base, title, manifest, spine = posixpath.dirname(opf_path), "", {}, []
    for element in package.iter():
        tag = _epub_local(element.tag)
        if tag == "item" and element.get("id"):
            manifest[element.get("id")] = (element.get("href") or "",
                                           (element.get("media-type") or "").lower())
        elif tag == "itemref" and element.get("idref"):
            spine.append(element.get("idref"))
        elif tag == "title" and not title:
            title = " ".join("".join(element.itertext()).split())
    present, members, seen = set(archive.namelist()), [], set()
    for idref in spine:
        href, media_type = manifest.get(idref, ("", ""))
        member = _epub_member(base, href)
        # One member is read once however many manifest items point at it. A book that genuinely
        # printed a chapter twice reads the same either way; an archive that names one member a
        # thousand times is multiplying itself, and this is where that stops.
        if member in seen:
            continue
        seen.add(member)
        # A spine entry the archive does not carry is skipped rather than fatal: half a book is
        # what a reader gets from a damaged file too, and it is worth more than none of it.
        if member in present and (not media_type or any(t in media_type for t in _EPUB_TEXT_TYPES)):
            members.append(member)
    if not members:
        raise ValueError("Not a readable EPUB: the spine lists no document the archive carries")
    if len(members) > _EPUB_SPINE_MAX:
        raise ValueError(f"Not a readable EPUB: the spine lists {len(members)} documents, "
                         f"more documents than a book has (at most {_EPUB_SPINE_MAX})")
    return title, members


def _epub_pages(p, chars=3000, meta=None):
    """An EPUB read as the book it is: chapter after chapter, in spine order, its pictures
    marked where they stand (``![alt]``) so doc_read can name them and doc_page_image show them.

    ``meta`` goes unused: EPUB content is UTF-8 or UTF-16 by specification, so there is no
    encoding here that had to be guessed and recorded."""
    return _epub_read(p, chars, [])


def _without_framed(listed):
    """The pictures an EPUB read before extraction version 3 marked: none framed in <svg>, and an
    alt-less one numbered without them -- its stored pages carry those markers, and matching them
    against today's list showed the cover as figure 1 (0.18.9 sweep)."""
    kept = [dict(image) for image in listed if not image.get("svg")]
    for number, image in enumerate(kept, 1):
        if not image["alt"]:
            image["marker"] = f"![image {number}]"
    return kept


def _epub_images(p):
    """The pictures behind an EPUB's ``![...]`` markers, in marker order: ``[{"marker", "alt",
    "member"}]``, ``member`` the archive member the chapter's <img src> names, or None."""
    images = []
    _epub_read(p, 3000, images)
    return images


def _epub_read(p, chars, images):
    try:
        with zipfile.ZipFile(p) as archive:
            _title, members = _epub_spine(archive)
            pages, budget = [], _EPUB_BOOK_CHARS
            for name in members:
                data = _zip_read(archive, name)
                if data is None:
                    continue
                markup = _decode_markup(data)
                budget -= len(markup)
                if budget < 0:
                    # Counted as it is decoded, so the refusal happens before the memory is spent
                    # rather than after: this is the shape a zip bomb arrives in, and an EPUB is
                    # downloaded and indexed on arrival without anybody looking at it first.
                    raise ValueError(
                        f"Refused {os.path.basename(p)}: its spine expands past "
                        f"{_EPUB_BOOK_CHARS // (1024 * 1024)} MiB of markup, larger than any book")
                start = len(images)                  # one list for the book: "image N" counts book-wide
                pages.extend(_paginate(htmltext.readable(markup, images=images)[0], chars))
                for image in images[start:]:
                    src = urllib.parse.unquote(image.pop("src").split("#")[0].split("?")[0])
                    inside = src and not src.startswith(("data:", "http:", "https:"))
                    image["member"] = posixpath.normpath(posixpath.join(posixpath.dirname(name), src)) if inside else None
    except zipfile.BadZipFile as error:
        raise ValueError(f"Not a readable EPUB: the file is not a zip archive ({error})") from error
    return pages


def _decode_markup(data, what="EPUB chapter"):
    """One markup document out of an archive, decoded by the same rule as every file on disk.

    EPUB content is UTF-8 or UTF-16 by specification, and the BOM is the only honest signal of
    the second -- but a specification is not what a file is written in. Japanese e-texts ship as
    Shift-JIS inside the zip, and this used to decode with errors='replace': the strict door that
    every other text format goes through had an archive-shaped hole beside it, and a book came
    through it as pages of mojibake. Markup bytes now go through ``_decode_bytes`` too, so an
    EPUB chapter is decoded for real or the book is refused by name.
    """
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16")             # a BOM is a declaration, not a guess
        except UnicodeDecodeError as error:
            raise ValueError(
                f"not UTF-16 text despite its byte order mark: {what} ({error})") from error
    return _decode_bytes(data, what)[0]


# -- what the corpus can read -------------------------------------------------------------------
#
# One table, and a suffix with no extractor is refused by name instead of being read as text: a
# single-file ingest used to accept anything, so an EPUB entered the corpus as pages of
# "PK\x03\x04..." -- which passed the text check, because the XHTML inside the archive leaks
# through the compression -- and an HTML page entered with its tags and its <script> counted as
# prose.
#
DJVU_TEXT_BINARY = "djvused"
DJVU_RENDER_BINARY = "ddjvu"
DJVU_TIMEOUT = 300


def _djvu_run(argv, meta, stdin=None):
    """stdout of one djvulibre command, or None with ``meta['djvu_error']`` saying why."""
    try:
        out = subprocess.run(argv, input=stdin, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=DJVU_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        _note(meta, "djvu_error", str(error)[:200])
        return None
    if out.returncode != 0:
        _note(meta, "djvu_error", " ".join((out.stderr or "").split())[-200:] or f"exit {out.returncode}")
        return None
    return out.stdout


def _djvu_page_count(p, meta):
    out = _djvu_run([DJVU_TEXT_BINARY, p, "-e", "n"], meta)
    return int(out) if out and out.strip().isdigit() else None


def _djvu_text_layer(p, meta):
    """The hidden text of every page, ``""`` for a page that has none; None when unreadable.

    ``djvutxt`` was the reader here, and it prints nothing at all -- not even the form feed -- for
    a page without hidden text, so the text of a book with one blank or plate page in it was
    stored a page early from there on. ``print-pure-txt`` per selected page closes every page with
    a form feed, empty or not, and prints the same characters ``djvutxt`` does; the newline
    ``djvutxt`` put before each form feed is kept, so a book it read correctly reads the same.
    """
    count = _djvu_page_count(p, meta)
    if count is None:
        return None
    script = "".join(f"select {n}; print-pure-txt\n" for n in range(1, count + 1))
    out = _djvu_run([DJVU_TEXT_BINARY, "-u", p], meta, stdin=script)
    if out is None:
        return None
    pages = out.split("\f")[:count]
    if len(pages) != count:
        _note(meta, "djvu_error", f"{DJVU_TEXT_BINARY} printed {len(pages)} pages of {count}")
        return None
    return [text + "\n" if text else text for text in pages]


def _djvu_pages(p, meta=None):
    """A DjVu book: its own text layer, with the pages that need it OCR'd from a rendering.

    CADAL and Wikimedia carry a large part of the scanned Chinese classics as DjVu
    (2026-09-18, B10: download_file refused the format, a Sister fetched it by hand, and the
    corpus then skipped it at submission). Pages are chosen the way a PDF's are (``_route``),
    on their text alone, and only those are rendered -- to a working PDF whose page ``i`` is the
    ``i``-th chosen page -- and OCR'd. The DjVu's own bytes stay the document's identity.
    """
    if not shutil.which(DJVU_TEXT_BINARY):
        _note(meta, "djvu_error", f"{DJVU_TEXT_BINARY} is not installed (brew install djvulibre)")
        return []
    learned = meta if meta is not None else {}
    pages = _djvu_text_layer(p, learned)
    if not pages:
        return []
    routed = _route(pages)
    if not routed:
        return pages
    numbers, ocr = sorted(routed), None
    if not shutil.which(DJVU_RENDER_BINARY):
        why = f"{DJVU_RENDER_BINARY} is not installed (brew install djvulibre)"
    else:
        with tempfile.TemporaryDirectory(prefix="misaka-djvu-") as tmp:
            rendered = os.path.join(tmp, "scan.pdf")
            done = _djvu_run([DJVU_RENDER_BINARY, "-format=pdf", f"-page={_page_spec(numbers)}", p, rendered],
                             learned)
            read = (_ocr_pages(rendered, learned, None, len(numbers))
                    if done is not None and os.path.exists(rendered) else None)
        if read is not None:
            ocr = {number: read.get(i) for i, number in enumerate(numbers, 1)}
        why = (_why_not_read(learned) if done is not None
               else f"rendering failed: {learned.get('djvu_error', 'no output')}")
    merged, ocred, unread = _merge_ocr(pages, ocr, routed, why)
    _record_reading(meta, merged, ocred, unread)
    return merged


# The last group is text with no format of its own: a card's analysis.csv, results.json, run.log
# or paper.tex. These entered the corpus as text before this table existed, and the path that
# carries them (``documents/workspace.ingest_artifacts``) swallows a ValueError without a word --
# so refusing a suffix here dropped the card's deliverable in silence. What was actually wrong
# with reading them as text was never the suffix: it was errors='replace', and _decode_bytes has
# closed that door for every format at once.

_EXTRACTORS = {
    ".pdf": _pdf_pages,
    ".djvu": _djvu_pages,
    ".epub": _epub_pages,
    ".html": _html_pages, ".htm": _html_pages, ".xhtml": _html_pages,
    ".xlsx": _office_pages, ".xlsm": _office_pages,
    ".docx": _office_pages, ".docm": _office_pages,
    ".pptx": _office_pages, ".pptm": _office_pages,
    ".doc": _legacy_office_pages, ".xls": _legacy_office_pages,
    ".ppt": _legacy_office_pages,
    # A csv moved off ``_text_pages``: read as running text it is a wall of commas with no
    # header row named and no column typed, and the corpus can say all three. It stays a
    # swept suffix, so ``doc scan`` picks up a folder of data files as it always did.
    ".csv": _office_pages, ".tsv": _office_pages,
    ".md": _text_pages, ".markdown": _text_pages, ".txt": _text_pages,
    ".bib": _text_pages, ".rst": _text_pages,
    ".tex": _text_pages,
    ".json": _text_pages, ".jsonl": _text_pages, ".ndjson": _text_pages, ".log": _text_pages,
    ".yaml": _text_pages, ".yml": _text_pages,
}

# "Can the corpus read this file" and "should a folder walk collect it by itself" are not the
# same question, and one answer to both is what makes the second one wrong. ``scan`` walks
# whatever it is pointed at -- ``misaka doc scan`` defaults to the working directory -- and a
# working tree is full of package.json, config.yml and run.log that nobody meant as materials.
# Named one by one they are read; swept up by a net they are noise, and download_file indexes
# arrivals by this set too. Everything else in the table is document-shaped enough for both.
# The legacy three join them for a different reason: reading one costs a LibreOffice
# process, so a folder walk that happens to pass a directory of 1990s attachments would
# spend minutes converting files nobody asked for. Named one by one they are read.
_NOT_SWEPT = frozenset({".json", ".jsonl", ".ndjson", ".log", ".yaml", ".yml",
                        ".doc", ".xls", ".ppt"})
READABLE_SUFFIXES = frozenset(_EXTRACTORS)       # what ingest reads when a file is named
SCAN_SUFFIXES = READABLE_SUFFIXES - _NOT_SWEPT


def _extractor(p):
    """The extractor for this file's suffix. The refusal names what the corpus does read: a
    model told only "no" hands the same file back."""
    ext = os.path.splitext(p)[1].lower()
    extract = _EXTRACTORS.get(ext)
    if extract is None:
        raise ValueError(
            f"Cannot index {ext or os.path.basename(p)}: the corpus reads "
            f"{' '.join(sorted(_EXTRACTORS))}. Convert the file first."
        )
    return extract


def extract_pages(p, meta=None):
    """One file's text pages, dispatched on its suffix; ``meta`` collects what the extractor
    learned about the source (see the extractor protocol above)."""
    return _extractor(p)(p, meta=meta)


def source_title(p):
    """The title a document carries inside itself (EPUB/Word ``dc:title``, HTML ``<title>``,
    a deck's first slide title), or None.

    Preferred over the file name, which for a downloaded book is whatever the URL ended in.
    """
    from misaka.core.documents import office

    ext = os.path.splitext(p)[1].lower()
    try:
        if ext == ".epub":
            with zipfile.ZipFile(p) as archive:
                title = _epub_spine(archive)[0]
        elif _EXTRACTORS.get(ext) is _html_pages:
            title = htmltext.readable(_read_text(p))[1]
        elif office.format_of(p) in office.TITLE_OF:
            # A .docx carries ``dc:title`` and a .pptx carries its first slide's title.
            title = office.TITLE_OF[office.format_of(p)](p)
        else:
            return None
    except (ValueError, OSError, zipfile.BadZipFile):
        return None
    return htmltext.clip(title.strip(), htmltext.MAX_TITLE_CHARS) or None


# How many pdfium subprocesses one outline build may open. PageIndex's own default is
# "CPU count - 1", which is the right answer for a lone command and the wrong one here: this
# runs in the tail of a research card's settlement, and the cards are already N-way parallel
# (sister_parallel per node, and the nodes run in parallel too), so the auto default
# multiplies -- 3 nodes x 4 cards x 7 workers is 84 pdfium processes on an 8-core machine.
# A caller that owns the whole machine passes its own `workers`.
TREE_WORKERS = 2

# Below this a document is short enough to navigate by page, so ingestion does not pay for an
# outline. The backfill on re-ingest uses the same threshold, or it would keep retrying for
# documents the first pass deliberately skipped.
TREE_MIN_PAGES = 20

_warned_no_pageindex = False


def pageindex_available():
    """Whether this install can do structure extraction at all (the ``pageindex`` extra)."""
    from .pageindex import available
    return available()


def _warn_no_pageindex(p):
    """Say once, out loud, that every outline in this run was skipped for the same reason.

    Without this the extra's absence is invisible: ingestion succeeds, the corpus gets pages
    and no `tree.json`, and the only trace is a `(pages)` marker in `doc tree` three commands
    later. Once per process, because a scan hits it for every PDF in the folder.
    """
    global _warned_no_pageindex
    if _warned_no_pageindex:
        return
    _warned_no_pageindex = True
    from .pageindex import INSTALL_HINT
    print(f"Warning: no PageIndex structure extraction for {os.path.basename(p)} (and any other "
          f"document in this run): PageIndex's packages are missing from this install, so documents "
          f"are split into pages only, with no outline.\n         To fix it: {INSTALL_HINT}\n"
          f"         Then re-run the same `misaka doc add`/`doc scan` to backfill the outline.",
          file=sys.stderr)


# Formats whose outline is read from the text itself (documents/text_outline) rather than from a
# PDF's layout: transcribed books and saved pages, the long ones of which had no tree at all.
TEXT_TREE_SUFFIXES = frozenset({".txt", ".md", ".markdown"})


def _text_tree(pages, ext, reason):
    """The text outline as JSON, or None. A miss is settled (``none_found``): the reader is
    deterministic over the stored pages, so asking again would only repeat the answer."""
    nodes = text_outline.build_text_tree(pages, markdown=ext in {".md", ".markdown"})
    if nodes:
        return json.dumps(nodes, ensure_ascii=False)
    _note(reason, "tree", "none_found")
    return None


def _stored_pages(ddir, count):
    """The page texts a document was indexed with, in order -- the numbers an outline must use."""
    pages = []
    for number in range(1, int(count) + 1):
        try:
            with open(_page_path(ddir, number), encoding="utf-8", errors="replace") as f:
                pages.append(f.read())
        except OSError:
            pages.append("")
    return pages


def _outline_for(p, ext, pages, reason):
    """One outline call for either family: the text reader over the stored pages, or PageIndex
    over the PDF. ``pages`` is only consulted for the text family."""
    if ext in TEXT_TREE_SUFFIXES:
        return _text_tree(pages, ext, reason)
    return _build_tree_with_reason(p, reason)


# PDFium is not thread-safe. Text extraction and page renders run it in child processes; the
# outline is its one caller inside this process, and ingests run on worker threads: two of them
# in PDFium at once corrupted the heap and killed a Sister (2026-09-28, SIGTRAP in its font scan).
_PDFIUM = threading.Lock()


def _build_tree_with_reason(p, reason):
    """`build_tree(p, reason=...)`, tolerating a stand-in that predates the keyword.

    Tests monkeypatch `build_tree` with plain `lambda p: ...`, and so may callers outside this
    module. Requiring the keyword would turn those into TypeErrors during ingestion, which is
    the one thing outline extraction must never do; a stand-in that cannot report simply
    reports nothing, and an unknown reason is treated as unsettled.
    """
    try:
        return build_tree(p, reason=reason)
    except TypeError:
        return build_tree(p)


def build_tree(p, *, workers=TREE_WORKERS, reason=None):
    """Return the PageIndex outline of a PDF as JSON text, or None for other formats or on failure.

    None has four meanings and the caller needs them apart: re-ingest should stop asking a
    document that genuinely has no outline, but must keep asking one that was skipped because
    the extra was missing -- installing it is exactly how a user repairs their corpus. Pass a
    dict as ``reason`` to be told which happened, the way ``extract_pages`` reports what it
    learned through ``meta``.

    ``reason["tree"]`` is one of ``not_pdf``, ``none_found``, ``extra_missing``, ``failed``,
    and is absent when an outline was returned. Only ``none_found`` is settled: the others can
    all change without the document changing.
    """
    if os.path.splitext(p)[1].lower() != ".pdf":
        _note(reason, "tree", "not_pdf")
        return None
    try:
        from .pageindex import build_tree as pageindex_tree
        with _PDFIUM:
            nodes = pageindex_tree(os.path.abspath(p), workers=workers)
        if nodes:
            return json.dumps(nodes, ensure_ascii=False)
        _note(reason, "tree", "none_found")
        return None
    except PageIndexUnavailable:
        # Actionable and identical for every document, unlike a parse failure: name it.
        _warn_no_pageindex(p)
        _note(reason, "tree", "extra_missing")
        return None
    except Exception:  # noqa: BLE001 - outline extraction must not block ingestion
        _note(reason, "tree", "failed")
        return None


# File-tree storage

def _page_path(ddir, page):
    # ponytail: four digits keep the names aligned up to 9,999 pages; readers sort by number, so more still works.
    return os.path.join(ddir, "pages", f"p{page:04d}.txt")


# Bumping this re-asks `build_tree` for every document that had no outline last time. Raise it
# when the extractor gains the ability to find one where it previously could not.
TREE_ATTEMPT_VERSION = 1


# Bumping this re-reads a PDF, a DjVu or an EPUB indexed by an older extractor the next time it is
# ingested (``_reread``). 2: pages chosen for OCR one by one (``_route``), OCR sidecars and DjVu
# text lined up with the file's own page numbers, and an EPUB's pictures marked where they stand.
# 3: a statistical table is no longer taken for running lines and sent to OCR, and an EPUB's
# pictures framed in <svg> are marked too.
EXTRACT_VERSION = 3
REREAD_SUFFIXES = (".pdf", ".djvu", ".epub")
REREAD_TRIES = 3            # the re-read pdfium cannot route on this many ingests in a row is given up


def _needs_reread(pages):
    """Whether stored pages show what version 1 could get wrong: an OCR placeholder stored as a
    page, or a page with little or garbled text of its own, on which the file may print something
    the old extractor never read. A book whose every page carries text of its own is left alone."""
    running = _running_lines(pages)
    return any(_SKIPPED.search(text) or len(own) < SPARSE_TEXT or _garble_ratio(own) > GARBLE_RATIO
               for text in pages for own in (_own_text(text, running),))


def _ocr_set(meta, count):
    if not meta.get("ocr"):
        return set()
    listed = meta.get("ocr_pages")
    return set(listed) if isinstance(listed, list) else set(range(1, count + 1))


def _reread(ddir, p, count):
    """Bring a document indexed by an older extractor up to this one; returns its page count.

    Only what the new extractor reads differently is rewritten: every page when the count changed
    (the old pages were numbered wrong), otherwise the pages it OCR'd and any stored placeholder.
    Every other page keeps its stored text byte for byte, so a quotation already located on it
    stays where it was. The pages that did change are listed in ``meta['reread']``, and doc_read
    and doc_verify say so on them.

    A document with pages that need an OCR this machine does not have is not stamped as current:
    it is read again once ocrmypdf is installed.
    """
    meta = _read_meta_at(ddir) or {}
    version = int(meta.get("extract_version") or 1)
    if version >= EXTRACT_VERSION:
        return count
    ext = os.path.splitext(p)[1].lower()
    old = _stored_pages(ddir, count) if ext in REREAD_SUFFIXES else None
    pages, learned = None, {}
    # djvutxt skipped a page with no hidden text outright, so a DjVu stored out of step can have
    # nothing but full pages to show for it: only its page count gives it away. An EPUB's text
    # used to drop its pictures (all of them before 2, those framed in <svg> before 3), so one
    # with fewer ``![...]`` markers than pictures is read again. Version 2 sent a statistical
    # table's pages to OCR: a document with some pages OCR'd is routed again.
    if ext == ".epub":
        stale = old is not None and sum(text.count("![") for text in old) < len(_epub_images(p))
    else:
        stale = old is not None and (_needs_reread(old) or (ext == ".djvu" and shutil.which(DJVU_TEXT_BINARY)
                                                            and _djvu_page_count(p, {}) not in (None, count))
                                     or (version == 2 and 0 < len(_ocr_set(meta, count)) < count))
    if stale:
        pages = extract_pages(p, meta=learned)
        if not _has_text_layer(pages):
            return count                    # nothing better to offer; asked again next time
        if learned.pop("visuals_unread", False):
            # Routed by text alone, a plate OCR read by its image cover would go back to its
            # caption for good: read again when pdfium answers (0.18.9 sweep) -- up to
            # REREAD_TRIES times; a file pdfium never reads keeps what is stored, stamped current.
            # Not given up while it holds pages waiting for an OCR this machine lacks: those are
            # read once ocrmypdf is installed, as everywhere else in a re-read.
            with _meta_lock(ddir):
                m = _read_meta_at(ddir) or {}
                put_off = m.get("reread_put_off") if isinstance(m.get("reread_put_off"), dict) else {}
                tries = int(put_off.get("tries") or 0) + 1 if put_off.get("version") == EXTRACT_VERSION else 1
                waits_for_ocr = not shutil.which(OCR_BINARY) and any(
                    str(why).endswith(OCR_MISSING) for why in (m.get("unread_pages") or {}).values())
                if tries >= REREAD_TRIES and not waits_for_ocr:
                    m.pop("reread_put_off", None)
                    m["extract_version"] = EXTRACT_VERSION
                else:
                    m["reread_put_off"] = {"version": EXTRACT_VERSION, "tries": tries}
                atomic.write_text(os.path.join(ddir, "meta.json"), json.dumps(m, ensure_ascii=False, indent=2))
            return count
    unread = {int(n): why for n, why in (learned.get("unread_pages") or {}).items()}
    ocr_missing = any(why.endswith(OCR_MISSING) for why in unread.values())
    was_ocr, now_ocr = _ocr_set(meta, count), _ocr_set(learned, len(pages or ()))
    changed, final_ocr = [], was_ocr
    if pages is not None and len(pages) != count:
        if ocr_missing and was_ocr:
            return count                    # the stored pages hold OCR this machine cannot redo
        changed, final_ocr = list(range(1, len(pages) + 1)), now_ocr
    elif pages is not None:
        # A page OCR'd before and not routed now (a table version 2 took for a scan) goes back to
        # its text layer; one still routed whose OCR this machine cannot redo keeps its reading.
        changed = [n for n in range(1, count + 1)
                   if (ext == ".epub" or n in now_ocr or (n in was_ocr and n not in unread)
                       or _SKIPPED.search(old[n - 1])) and pages[n - 1] != old[n - 1]]
        final_ocr = (was_ocr - set(changed)) | (now_ocr & set(changed))
        # A page still holding an earlier OCR's reading has been read, whatever this run managed.
        unread = {n: why for n, why in unread.items() if n in changed or n not in was_ocr}
    total = len(pages) if changed else count
    with _meta_lock(ddir):
        for n in changed:
            atomic.write_text(_page_path(ddir, n), pages[n - 1])
        for n in range(total + 1, count + 1):           # a count that shrank
            with contextlib.suppress(FileNotFoundError):
                os.unlink(_page_path(ddir, n))
        if changed:
            for derived in ("figures.json", "labels.json"):   # read off the old pages: found again on asking
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(os.path.join(ddir, derived))
        m = _read_meta_at(ddir) or {}
        if pages is not None:
            for key in ("ocr", "ocr_pages", "unread_pages"):
                m.pop(key, None)
            m["pages"] = total
            _record_reading(m, [None] * total, sorted(final_ocr), unread)
        if changed:
            m["reread"] = {"at": int(time.time()), "pages": changed, "pages_before": count}
        if not ocr_missing:
            m["extract_version"] = EXTRACT_VERSION
            m.pop("reread_put_off", None)       # a later try that pdfium answered
        atomic.write_text(os.path.join(ddir, "meta.json"), json.dumps(m, ensure_ascii=False, indent=2))
    return total


def _read_meta_at(ddir):
    """Read metadata from a known document directory."""
    path = os.path.join(ddir, "meta.json")
    if not _real_file(path, ddir):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _meta(doc_id, workspace=None):
    ddir = resolve_doc(doc_id, workspace=workspace)
    return _read_meta_at(ddir) if ddir else None


def _tree(doc_id, workspace=None):
    ddir = resolve_doc(doc_id, workspace=workspace)
    if not ddir:
        return None
    tp = os.path.join(ddir, "tree.json")
    if not _real_file(tp, ddir):
        return None
    try:
        with open(tp, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


STAGE_SUFFIX = ".part-"     # an in-progress document: "<doc_id>.part-<pid>-<thread>", never listed
STAGE_GRACE = 2 * OCR_TIMEOUT   # untouched for this long: the process that owned it is not coming back
_swept = False                  # the reclaim below runs once per process, not once per ingested file


def _sweep_stale_stages(root):
    """Delete staging directories a killed ingest left behind.

    ``ingest`` cleans its own stage up on both the success and the failure path, but SIGKILL runs
    neither -- and research is where that happens most (``processes.terminate`` takes a node's whole
    tree down, and a card's ingest is the tail of its settlement). What is left holds a full copy of
    the source file, under a name ``docs()`` filters out, so nothing ever notices or reclaims it.
    A stage belonging to this process is never touched, nor is one still being written to.
    """
    global _swept
    if _swept:                     # once per process: `scan` ingests a whole directory in a loop
        return
    _swept = True
    mine, cutoff = f"{STAGE_SUFFIX}{os.getpid()}-", time.time() - STAGE_GRACE
    for name in (os.listdir(root) if os.path.isdir(root) else []):
        if STAGE_SUFFIX not in name or mine in name:
            continue
        path = os.path.join(root, name)
        try:
            stale = os.stat(path).st_mtime < cutoff
        except OSError:
            continue
        if stale:
            shutil.rmtree(path, ignore_errors=True)


def _fsync_stage(stage):
    """Push a staging directory's contents to disk before the rename that publishes it.

    Renaming a directory makes the document *visible* atomically; it does not make it *durable*.
    The rename's metadata can reach disk ahead of the page files, so a power loss leaves the
    corpus a document that passes every check ``resolve_doc`` makes (meta.json intact) and holds
    empty or truncated pages. Everything else in this module writes through ``utils.atomic``,
    which fsyncs; ingest builds its directory first and moves it, so the sync happens here.
    """
    for base, _dirs, names in os.walk(stage):
        for name in names:
            try:
                fd = os.open(os.path.join(base, name), os.O_RDONLY)
            except OSError:
                continue
            try:
                os.fsync(fd)
            except OSError:      # a filesystem that refuses fsync on this file: nothing to do
                pass
            finally:
                os.close(fd)
    for base, _dirs, _names in os.walk(stage, topdown=False):
        try:
            fd = os.open(base, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            continue
        try:
            os.fsync(fd)
        except OSError:          # directory fsync is unsupported on some platforms
            pass
        finally:
            os.close(fd)


def _meta_lock(ddir):
    """One lock per document for meta.json read-modify-write: two ingests of the same content
    (two cards, two panes) must not lose each other's task or path link."""
    from filelock import FileLock
    locks = os.path.join(os.path.dirname(ddir), ".locks")
    os.makedirs(locks, exist_ok=True)
    return FileLock(os.path.join(locks, os.path.basename(ddir) + ".lock"))


def _link(ddir, p, task_id):
    """Known content: link ``task_id`` and remember this path too, so the same book used by
    two project folders belongs to both. Returns the page count."""
    with _meta_lock(ddir):
        m = _read_meta_at(ddir) or {}
        ids = list(m.get("task_ids") or ([m["task_id"]] if m.get("task_id") else []))
        paths = list(m.get("paths") or ([m["orig_path"]] if m.get("orig_path") else []))
        changed = False
        if task_id and task_id not in ids:
            ids.append(task_id); m["task_ids"] = ids; changed = True
        if p not in paths:
            paths.append(p); m["paths"] = paths; changed = True
        if changed:
            atomic.write_text(os.path.join(ddir, "meta.json"), json.dumps(m, ensure_ascii=False, indent=2))
    return int(m.get("pages", 0))


def ingest(p, title=None, with_tree=True, task_id=None, workspace=None):
    """Index a file under its content hash and return ``(doc_id, page_count)``.

    Re-ingesting a known document links the new ``task_id`` and backfills an outline the first
    pass could not build. A suffix the corpus has no extractor for raises ValueError naming the
    formats it does read.
    """
    p = os.path.abspath(os.path.expanduser(p))
    from misaka.core.web.evidence import check_material_read
    check_material_read(p)
    if workspace is not None and not under(p, workspace):
        raise ValueError("Document source resolves outside the workspace.")
    _extractor(p)          # refuse an unreadable format before hashing, and before extracting
    sha = sha256_file(p)
    doc_id = sha[:12]
    ddir = os.path.join(corpus_root(workspace), doc_id)
    existing = resolve_doc(doc_id, workspace=workspace)
    if existing:
        if (_read_meta_at(existing) or {}).get("sha256") != sha:
            raise ValueError(f"Document ID collision: {doc_id}")
        count = _link(existing, p, task_id)
        count = _reread(existing, p, count)
        # Without this, installing the pageindex extra repairs nothing: every document already
        # in the corpus stays outline-less forever, because re-ingest used to stop at the link.
        #
        # "No tree.json" is not the same as "not tried yet". Plenty of PDFs have no outline to
        # extract, and for those `build_tree` returns None however many times it is asked --
        # so keying the backfill on the file's absence alone re-ran the whole PageIndex parse
        # on every scan. That is 14 seconds for a 758-page document, and a process pool past 64
        # pages. The attempt is recorded the way this module already records what an extractor
        # learned (`meta['ocr_error']`), and only a newer extractor changes the answer.
        meta = _read_meta_at(existing) or {}
        if (with_tree and count >= TREE_MIN_PAGES
                and not _real_file(os.path.join(existing, "tree.json"), existing)
                and meta.get("tree_attempted_version") != TREE_ATTEMPT_VERSION):
            why = {}
            ext = os.path.splitext(p)[1].lower()
            tree = _outline_for(p, ext, _stored_pages(existing, count) if ext in TEXT_TREE_SUFFIXES else None, why)
            if tree:
                atomic.write_text(os.path.join(existing, "tree.json"), tree)
            elif why.get("tree") == "none_found":
                # Settled: this document has no outline to find. A missing extra or a failed
                # parse is not settled -- installing the extra is how a corpus gets repaired,
                # so those keep asking.
                with _meta_lock(existing):
                    meta = _read_meta_at(existing) or {}
                    meta["tree_attempted_version"] = TREE_ATTEMPT_VERSION
                    atomic.write_text(os.path.join(existing, "meta.json"),
                                      json.dumps(meta, ensure_ascii=False, indent=1))
        return doc_id, count
    if os.path.lexists(ddir):
        raise ValueError(f"Invalid or colliding corpus entry: {doc_id}")
    extracted = {}                          # what the extractor learned: encoding, OCR
    pages = extract_pages(p, meta=extracted)
    if not _has_text_layer(pages):
        raise _no_text_error(p, pages, extracted.pop("ocr_error", None))
    extracted.pop("ocr_error", None)        # the pages it cost are in unread_pages, with the reason
    extracted.pop("visuals_unread", None)   # only a re-read asks it
    tree_wanted = with_tree and len(pages) >= TREE_MIN_PAGES
    tree_reason = {}
    tree = _outline_for(p, os.path.splitext(p)[1].lower(), pages, tree_reason) if tree_wanted else None
    # Build the document beside its final place and move it in with one rename: the corpus holds
    # a complete document or none, never a half-written directory that reads as "already indexed".
    _sweep_stale_stages(corpus_root(workspace))      # whatever a killed ingest left behind, before adding ours
    stage = f"{ddir}{STAGE_SUFFIX}{os.getpid()}-{threading.get_ident()}"
    shutil.rmtree(stage, ignore_errors=True)
    try:
        os.makedirs(os.path.join(stage, "pages"))
        for i, t in enumerate(pages):
            with open(_page_path(stage, i + 1), "w", encoding="utf-8") as f:
                f.write(t)
        shutil.copy2(p, os.path.join(stage, "source" + os.path.splitext(p)[1].lower()))
        if tree:
            with open(os.path.join(stage, "tree.json"), "w", encoding="utf-8") as f:
                f.write(tree)
        # Asked for an outline and got none: record that, so re-ingest links the document
        # instead of paying for the same parse again (see the backfill branch above).
        attempted = ({"tree_attempted_version": TREE_ATTEMPT_VERSION}
                     if tree_reason.get("tree") == "none_found" else {})
        meta = {"doc_id": doc_id, "title": title or source_title(p) or os.path.basename(p),
                "orig_path": p, "paths": [p], **attempted, "extract_version": EXTRACT_VERSION,
                "sha256": sha, "pages": len(pages), "task_id": task_id,
                "task_ids": [task_id] if task_id else [], "added_at": int(time.time()),
                **extracted}
        with open(os.path.join(stage, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        _fsync_stage(stage)     # durability, before the rename hands the directory its real name
        try:
            os.replace(stage, ddir)
        except OSError:
            existing = resolve_doc(doc_id, workspace=workspace)
            if not existing or (_read_meta_at(existing) or {}).get("sha256") != sha:
                # Not a concurrent ingest of the same content.
                raise
            _link(existing, p, task_id)          # the loser still owns this task's link to the document
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    shutil.rmtree(stage, ignore_errors=True)    # left only when another ingest won the rename
    return doc_id, len(pages)


def scan(directory, task_id=None, with_tree=True, workspace=None):
    """Ingest every file under ``directory`` the corpus can read (``SCAN_SUFFIXES``), skipping
    hidden entries.

    Returns ``(ingested, skipped)`` as ``[(doc_id, path)]`` and ``[(path, reason)]``.
    """
    ingested, skipped = [], []
    for base, dirs, files in os.walk(os.path.abspath(os.path.expanduser(directory))):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for fn in sorted(files):
            if fn.startswith(".") or os.path.splitext(fn)[1].lower() not in SCAN_SUFFIXES:
                continue
            p = os.path.join(base, fn)
            try:
                ingested.append((ingest(p, task_id=task_id, with_tree=with_tree, workspace=workspace)[0], p))
            except (ValueError, OSError) as e:
                skipped.append((p, str(e)))
    return ingested, skipped


def doc_row(doc_id, workspace=None):
    """One document's ``docs`` row, or None when ``doc_id`` is no document of ``workspace``.
    A tool after one document reads it here: finding it in ``docs`` read every document's
    metadata, 17 s on a 33,308-document project (2026-10-27)."""
    ddir = resolve_doc(doc_id, workspace=workspace)
    if not ddir:
        return None
    m = dict(_read_meta_at(ddir))
    m["has_tree"] = _real_file(os.path.join(ddir, "tree.json"), ddir)
    return m


def docs(workspace=None):
    """List indexed documents oldest first; ``workspace`` keeps only those whose source file lives under that folder."""
    root = corpus_root(workspace)
    rows = (doc_row(name, workspace=workspace) for name in (os.listdir(root) if os.path.isdir(root) else []))
    return sorted((m for m in rows if m is not None), key=lambda m: m.get("added_at", 0))


def _iter_pages(doc_id, lo=None, hi=None, workspace=None):
    """Yield selected pages in order without loading the entire document."""
    ddir = resolve_doc(doc_id, workspace=workspace)
    if not ddir:
        return
    pdir = os.path.join(ddir, "pages")
    if not _real_directory(pdir, ddir):
        return
    numbered = [(int(m.group(1)), fn) for fn in os.listdir(pdir) if (m := re.match(r"p(\d+)\.txt$", fn))]
    for pg, fn in sorted(numbered):                       # by number: p10000 comes after p9999
        if (lo is not None and pg < lo) or (hi is not None and pg > hi):
            continue
        page_path = os.path.join(pdir, fn)
        if not _real_file(page_path, pdir):
            continue
        with open(page_path, encoding="utf-8", errors="replace") as f:
            yield pg, f.read()


def read_page(doc_id, page, workspace=None):
    ddir = resolve_doc(doc_id, workspace=workspace)
    if not ddir:
        return None
    pdir = os.path.join(ddir, "pages")
    if not _real_directory(pdir, ddir):
        return None
    fp = _page_path(ddir, page)
    if not _real_file(fp, pdir):
        return None
    with open(fp, encoding="utf-8", errors="replace") as f:
        return f.read()


def page_heads(doc_id, limit=200, workspace=None):
    """Return the first nonempty line of each page as a fallback outline."""
    out = []
    for page, text in _iter_pages(doc_id, workspace=workspace):
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        out.append({"page": page, "head": first[:60]})
        if len(out) >= limit:
            break
    return out


# Quote matching
#
# One rule, used by every literal comparison against a document: corpus search, corpus
# verification, and the research ledger's quote check (it imports normalize_for_quote_match).
# Two normalizers meant two answers to "is this passage in the book", and both of the old ones
# only stripped whitespace -- so a quotation a model copied correctly was reported missing.

_WHITESPACE = re.compile(r"\s+")
# pdftotext breaks a word across lines with a trailing hyphen ("exam-\nple"), on nearly every line
# of a real book; the hyphen belongs to the layout, not to the word.
_HYPHEN_BREAK = re.compile(r"-[^\S\r\n]*\r?\n")

# NFKC compatibility classes whose folding merges notation onto plain text the page never prints:
# superscript and subscript footnote markers, circled and parenthesized list numbers, and vulgar
# fractions all decompose to real digits. Folding them mints numbers ("享年52①" -> "享年521") that
# verify_quote then swears the document states. Width folds (Ａ -> A, ｶ -> カ) and ligatures
# (ﬁ -> fi) are genuine extraction artefacts and must keep folding.
_NOTATION_TAGS = ("<super>", "<sub>", "<circle>", "<fraction>")


@functools.lru_cache(maxsize=4096)
def _keeps_notation(ch):
    """True for a character whose NFKC fold would visually change it into other text: it stays
    unfolded, so a quote has to reproduce it. The tag is the leading ``<...>`` token of the
    character's compatibility decomposition."""
    decomp = unicodedata.decomposition(ch)
    if decomp.startswith(_NOTATION_TAGS):
        return True
    # ⑴ and ⒈ carry the generic <compat> tag yet fold to "(1)" and "1." -- "3⒈" would become
    # "31." and match the quote "31", the same minted-digit bug as the tagged classes. Keep any
    # <compat> form that folds to a digit; digit-free <compat> folds (ﬁ -> fi, compat jamo ㄱ ->
    # choseong) are the artefacts the folding exists for and still fold.
    return decomp.startswith("<compat>") and any(
        "0" <= c <= "9" for c in unicodedata.normalize("NFKD", ch))


def _nfkc_keep_notation(text):
    """NFKC with the notation classes above left raw. Splitting into runs around the kept
    characters preserves NFKC's multi-character compositions (``ｶﾞ`` -> ``ガ``) inside each run."""
    out, run = [], []
    for ch in text:
        if _keeps_notation(ch):
            if run:
                out.append(unicodedata.normalize("NFKC", "".join(run)))
                run.clear()
            out.append(ch)
        else:
            run.append(ch)
    if run:
        out.append(unicodedata.normalize("NFKC", "".join(run)))
    return "".join(out)


def normalize_for_quote_match(text, keep_break_hyphens=False):
    """Fold the extraction artefacts that make a true quotation fail a literal comparison.

    Applied to both sides of every comparison. Stored quotes and claim hashes stay raw -- only the
    matching loosens; nothing here fuzzes, ranks, or stems. In order:

    1. hyphen at a line break -- pdftotext hyphenates every word that crosses a line, so
       ``exam-\\nple`` is the normal shape of a word in any PDF-sourced page. The same ``-\\n``
       is also how pdftotext prints a genuinely hyphenated compound ("well-\\nknown"), so
       ``keep_break_hyphens=True`` gives the other reading: the hyphen stays and only the break
       goes (with the whitespace rule below). Matchers try the default reading first;
    2. U+00AD soft hyphen -- EPUB and HTML sources carry invisible break opportunities inside
       words, and a model copying the passage will not reproduce them;
    3. NFKC -- folds full-width punctuation and digits onto ASCII (``，`` ``１``, exactly what a
       model transcribing CJK produces), half-width kana onto composed kana, and the ligatures
       (``ﬁ`` -> ``fi``) that PDF fonts leave sitting in the text layer. Notation that folds
       onto digits (``¹`` ``①`` ``½``) stays raw: folding it would verify numbers the page
       never states, so a quote must reproduce it;
    4. all whitespace -- extraction inserts spaces between CJK glyphs and breaks lines mid-phrase.
    """
    folded = str(text or "")
    if not keep_break_hyphens:
        folded = _HYPHEN_BREAK.sub("", folded)
    folded = folded.replace("\u00ad", "")
    return _WHITESPACE.sub("", _nfkc_keep_notation(folded))


@functools.lru_cache(maxsize=4096)
def _attaches(ch):
    """True when NFKC can fold ``ch`` into the character before it: a combining mark, a
    compatibility form that decomposes to one (half-width ``ﾞ`` after ``ｶ`` composes to ``ガ``),
    or a trailing Hangul jamo -- raw, or reached through a compatibility form (compat vowel jamo
    NFKD-decompose to jungseong, so whole-string NFKC composes a consonant-vowel jamo pair into
    one syllable). Such a character must be normalized together with its predecessor."""
    first = (unicodedata.normalize("NFKD", ch) or ch)[0]
    return (unicodedata.combining(ch) != 0
            or unicodedata.combining(first) != 0
            or "\u1160" <= ch <= "\u11ff"       # Hangul jungseong/jongseong
            or "\u1160" <= first <= "\u11ff")


def _folded_spans(text, keep_break_hyphens=False):
    """Return ``(folded, spans)``: ``folded == normalize_for_quote_match(text)`` under the same
    ``keep_break_hyphens`` reading, and ``spans[i]`` is the ``(start, end)`` slice of the raw
    ``text`` that produced ``folded[i]``.

    Normalization is not length preserving -- NFKC turns one ``ﬁ`` into two characters, the hyphen
    rule deletes two, whitespace removal deletes many -- so a position in ``folded`` is not an
    index into ``text`` and cannot be recovered by counting. Walking the raw text one normalization
    segment at a time keeps the correspondence exact: a segment begins at every character NFKC
    cannot fold backwards, which is precisely where normalizing a piece on its own gives the same
    answer as normalizing the whole string.
    """
    dropped = (set() if keep_break_hyphens
               else {i for m in _HYPHEN_BREAK.finditer(text) for i in range(*m.span())})
    segments = []                                    # [start, end, raw characters]
    for i, ch in enumerate(text):
        if i in dropped or ch == "\u00ad":
            continue
        if segments and _attaches(ch):
            segments[-1][1], segments[-1][2] = i + 1, segments[-1][2] + ch
        else:
            segments.append([i, i + 1, ch])
    folded, spans = [], []
    for start, end, raw in segments:
        piece = _WHITESPACE.sub("", _nfkc_keep_notation(raw))
        folded.append(piece)
        spans.extend([(start, end)] * len(piece))
    return "".join(folded), spans


@functools.lru_cache(maxsize=4096)
def _normalized_page(text, keep):
    """A page as quotations are matched against it, worked out once: a run's every unfound
    quotation read and normalised every page of its document again -- half a minute for 200 of
    them in an 800-page book, on the event loop (0.18.10 sweep)."""
    return normalize_for_quote_match(text, keep_break_hyphens=keep)


def _locate(text, needle):
    """Return the ``(start, end)`` slice of the raw ``text`` holding an already normalized
    ``needle``, or None. Callers get raw offsets: what is stored and shown is always the page's
    own text, never the query echoed back.

    A line-break hyphen is ambiguous -- pdftotext prints a soft break ("exam-\\nple") and a
    printed compound ("well-\\nknown") identically -- so when the default reading (hyphen
    deleted) misses, the walk runs once more with the hyphens kept. The default reading always
    wins when it matches, keeping today's matches and offsets unchanged."""
    readings = (False, True) if _HYPHEN_BREAK.search(text) else (False,)
    for keep in readings:
        if needle not in _normalized_page(text, keep):
            continue           # cheap reject: one C call per page, the span walk runs only on a hit
        folded, spans = _folded_spans(text, keep_break_hyphens=keep)
        pos = folded.find(needle)
        if pos < 0:
            continue           # the segment walk folded less than the whole-string rule did
        return spans[pos][0], spans[pos + len(needle) - 1][1]
    return None


def search_literal(q, limit=10, doc_id=None, workspace=None):
    """Find exact text across indexed pages (scoped to ``workspace`` when given and no ``doc_id``).

    Matching follows ``normalize_for_quote_match``, so this agrees with ``verify_quote``: a search
    that reported "no matches" for a passage verification then confirmed used to send the model
    away from material that was there. Snippets are cut from the raw page.
    """
    needle = normalize_for_quote_match(q)
    if not needle:
        return []
    targets = [doc_id] if doc_id else [m["doc_id"] for m in docs(workspace)]
    hits = []
    for did in targets:
        labels = None
        for page, text in _iter_pages(did, workspace=workspace):
            span = _locate(text, needle)
            if span:
                pos, end = span
                snip = text[max(0, pos - 12):pos] + "<<" + text[pos:end] + ">>" + text[end:end + 12]
                if labels is None:
                    labels = page_labels(did, workspace=workspace)[0]
                hits.append({"doc_id": did, "page": page, "printed": labels.get(page, ""),
                             "s": snip.replace("\n", " ")})
                if len(hits) >= limit:
                    return hits
    return hits


def _pages_holding(doc_id, workspace, needle):
    """The pages whose normalised text holds ``needle`` (either reading of a line-break hyphen)."""
    ddir = resolve_doc(doc_id, workspace=workspace)
    try:
        stamp = os.stat(os.path.join(ddir, "meta.json")).st_mtime_ns if ddir else None
    except OSError:
        stamp = None
    return {pg for pg, readings in _normalized_doc(doc_id, workspace, stamp) if any(needle in r for r in readings)}


@functools.lru_cache(maxsize=8)
def _normalized_doc(doc_id, workspace, _stamp):
    """``(page, (its normalised readings))`` for every page of a document, at one index version."""
    return tuple((pg, tuple(_normalized_page(text, keep) for keep in
                            ((False, True) if _HYPHEN_BREAK.search(text) else (False,))))
                 for pg, text in _iter_pages(doc_id, workspace=workspace))


def verify_quote(doc_id, quote, page=None, workspace=None):
    """Verify an exact quotation, optionally on one page, ignoring the differences
    ``normalize_for_quote_match`` folds. The returned offset indexes the raw page, and the claim
    hash binds the quotation as the caller wrote it."""
    needle = normalize_for_quote_match(quote)
    if not needle:
        return None
    candidates = None
    if page is None:
        # The whole document: pages that cannot hold it are set aside on its normalised text,
        # read once per version of the index, instead of reading and folding every page per quotation.
        candidates = _pages_holding(doc_id, workspace, needle)
        if not candidates:
            return None
    lo, hi = (page, page) if candidates is None else (min(candidates), max(candidates))
    for pg, text in _iter_pages(doc_id, lo=lo, hi=hi, workspace=workspace):
        if candidates is not None and pg not in candidates:
            continue
        span = _locate(text, needle)
        if not span:
            continue
        real = span[0]
        labels, where = page_labels(doc_id, workspace=workspace)
        return {"page": pg, "offset": real, "claim_hash": claim_hash(doc_id, pg, real, quote),
                "printed": labels.get(pg, ""), "printed_from": where if pg in labels else None}
    return None


def _across_page_break(doc_id, quote, page, workspace):
    """A quotation that begins on the cited ``page`` and runs onto the next: the two pages read as
    one text (0.18.9 sweep: it was marked "not found" and listed as not where it was cited). A
    running head or folio printed between the two pages still breaks it -- a locator, not a verdict."""
    needle = normalize_for_quote_match(quote)
    if page is None or not needle:
        return None
    pages = list(_iter_pages(doc_id, lo=page, hi=page + 1, workspace=workspace))
    if len(pages) != 2:
        return None
    (first, head), (_second, tail) = pages
    span = _locate(head + "\n" + tail, needle)
    if not span or span[0] >= len(head):
        return None
    labels, where = page_labels(doc_id, workspace=workspace)
    return {"page": first, "offset": span[0], "claim_hash": claim_hash(doc_id, first, span[0], quote),
            "printed": labels.get(first, ""), "printed_from": where if first in labels else None,
            "continues_on": page + 1}


def locate_quote(doc_id, quote, page=None, workspace=None):
    """Where a quotation cited on ``page`` actually is: ``{"status": ...}`` with ``status`` one of
    ``"on_page"`` (where it was cited, or anywhere when no page was given), ``"elsewhere"`` (not
    on the cited page; ``page`` says where it is), ``"not_found"``, or ``"no_document"``.

    A locator, never a verdict (prompt.QUOTATION_LOCATOR_GUIDELINE): OCR and typography make
    a true quotation miss. What it rules out is the quiet case GitHub issue #9 found, where a
    quotation matched somewhere in a book and the page cited for it was never compared.
    """
    if not resolve_doc(doc_id, workspace=workspace):
        return {"status": "no_document", "cited_page": page}
    found = verify_quote(doc_id, quote, page=page, workspace=workspace)
    if found:
        return {"status": "on_page", "cited_page": page, **found}
    found = _across_page_break(doc_id, quote, page, workspace)
    if found:
        return {"status": "on_page", "cited_page": page, **found}
    if page is not None:
        found = verify_quote(doc_id, quote, workspace=workspace)
        if found:
            return {"status": "elsewhere", "cited_page": page, **found}
    return {"status": "not_found", "cited_page": page}


def tree_outline(doc_id, max_nodes=120, workspace=None):
    tree = _tree(doc_id, workspace=workspace)
    m = _meta(doc_id, workspace=workspace)
    if not tree or not m:
        return None
    out, n = [f"# {m['title']}"], [0]

    def walk(nodes, depth=0):
        for x in nodes:
            if n[0] >= max_nodes:
                out.append("… (outline truncated)")
                return
            n[0] += 1
            a, b = x.get("start_index"), x.get("end_index")
            span = f"p{a}-{b}" if a else ""
            out.append(f"{'  ' * depth}- [{x.get('node_id')}] {(x.get('title') or '')[:70]}  {span}")
            walk(x.get("nodes") or [], depth + 1)

    walk(tree)
    return "\n".join(out)


def node_pages(doc_id, node_id, workspace=None):
    tree = _tree(doc_id, workspace=workspace)
    if not tree:
        return None
    found = []

    def walk(nodes):
        for x in nodes:
            if str(x.get("node_id")) == str(node_id):
                found.append((x.get("start_index"), x.get("end_index")))
                return True
            if walk(x.get("nodes") or []):
                return True
        return False

    walk(tree)
    return found[0] if found and found[0][0] else None


def pages_have_text(doc_id, start, end, workspace=None):
    """Whether any page from ``start`` to ``end`` carries text. ``read_pages`` is never empty --
    each page has its ``--- pN ---`` line -- so it cannot answer this itself."""
    return any(text.strip() for _page, text in _iter_pages(doc_id, lo=start, hi=end, workspace=workspace))


def read_pages(doc_id, start, end, max_chars=12000, offset=0, workspace=None):
    """The pages' text as one window: ``offset`` characters in, ``max_chars`` long, with a note
    on how to continue when there is more -- so a single page longer than the window is read
    in successive calls rather than never. Pages are read only up to the window's end."""
    offset = max(0, int(offset or 0))
    stop = offset + max_chars
    pieces, seen, more = [], 0, False
    labels, _where = page_labels(doc_id, workspace=workspace)
    for page, t in _iter_pages(doc_id, lo=start, hi=end, workspace=workspace):
        head = f"--- p{page} (printed p. {labels[page]}) ---" if page in labels else f"--- p{page} ---"
        chunk = f"\n{head}\n{t}" if t.strip() else f"\n{head} (no text on this page)\n"
        if seen + len(chunk) > offset:
            pieces.append(chunk[max(0, offset - seen):stop - seen])
        seen += len(chunk)
        if seen > stop:
            more = True
            break
    window = "".join(pieces)
    if more:
        window += f"\n… (more; call again with offset={stop} to continue)"
    return window


def structure(doc_id, workspace=None):
    m = _meta(doc_id, workspace=workspace)
    if not m:
        return None
    tree = _tree(doc_id, workspace=workspace)
    if tree:
        return {"mode": "tree", "title": m["title"], "tree": tree}
    # ``mode`` is a display string -- its only reader prints it -- so the reason the outline is
    # missing rides along with it. "(pages)" on its own reads as a property of the document
    # rather than as "structure extraction never ran on this machine".
    mode = "pages only, no structure tree"
    # Outlines come from PageIndex, which reads PDFs. A workbook or a deck has no outline
    # to miss, so naming the pageindex extra there sends a user to install something that
    # would change nothing -- and leaves them believing their corpus is broken.
    if os.path.splitext(m.get("orig_path") or "")[1].lower() != ".pdf":
        mode += " (this format has no outline)"
    elif not pageindex_available():
        from .pageindex import INSTALL_HINT
        mode += (f": the pageindex extra is not installed. {INSTALL_HINT}, "
                 "then re-run `misaka doc add <file>` to build it")
    return {"mode": mode, "title": m["title"],
            "pages": page_heads(doc_id, limit=10000, workspace=workspace)}

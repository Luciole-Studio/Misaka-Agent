"""Document ingestion, PageIndex navigation, literal search, and quote verification."""
import functools
import hashlib
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile

from misaka.documents import htmltext
from misaka.utils import atomic

DOC_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def corpus_root():
    """Content-addressed PageIndex store shared by every project folder."""
    return os.path.expanduser(os.environ.get("MISAKA_PAGEINDEX", "~/.misaka/pageindex"))


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
    root = os.path.realpath(corpus_root())
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
                             text=True, timeout=300, check=False)
        if out.returncode == 0 and out.stdout.strip():
            return _form_feed_pages(out.stdout)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(p)
        try:
            return [page.get_textpage().get_text_bounded() for page in pdf]
        finally:
            pdf.close()
    except Exception:  # noqa: BLE001
        return []


def _form_feed_pages(text):
    """Split page-separated output into pages. pdftotext and ocrmypdf's sidecar both end every
    page with a form feed, so the tail after the last one is no page."""
    pages = text.split("\f")
    if len(pages) > 1 and not pages[-1].strip():
        pages.pop()
    return pages


def _solid(pages):
    """How many pages carry more than a caption's worth of text."""
    return sum(1 for t in pages if len(t.strip()) > 20)


def _has_text_layer(pages):
    """True when extraction found a real text layer rather than a scan's stray page numbers.

    One rule, in one place: this is the check ``ingest`` refuses on, so a PDF is sent to OCR
    exactly when it would otherwise be turned away -- the two can never disagree.
    """
    return bool(pages) and _solid(pages) >= max(1, len(pages) * 0.2)


def _note(meta, key, value):
    """Record one fact an extractor learned about the source, when the caller asked for them."""
    if meta is not None:
        meta[key] = value


# -- OCR: an optional external binary, fail-closed ------------------------------------------------
#
# ingest used to refuse a scan with "run OCR first" -- advice the product could not carry out,
# because there was no OCR anywhere in it. Archival scans, pre-2000 books and 影印本 therefore
# could not enter the corpus at all. ocrmypdf is not a dependency and never becomes one: when it
# is absent the refusal stands, and it now names the install instead of an imperative into thin
# air.
OCR_BINARY = "ocrmypdf"
OCR_LANGS_DEFAULT = "eng+chi_sim+jpn"
# A book-length scan is minutes of work per hundred pages; the bound is what keeps one stuck OCR
# from owning an ingest forever (pdftotext above is bounded the same way, smaller).
OCR_TIMEOUT = 900


def _ocr_pages(p, meta=None):
    """OCR a scanned PDF into pages, or None when that cannot be done.

    ``--skip-text`` leaves any page that already carries text alone: OCR must never be written
    over a real text layer. ``--sidecar`` is the only output kept -- the corpus stores the
    original file, whose sha256 is the document's identity, so the OCR'd PDF is discarded.

    A run that fails writes why into ``meta['ocr_error']``, and the refusal quotes it. The most
    likely failure by far is a language pack that is not installed (``brew install tesseract-lang``
    for the chi_sim and jpn defaults), and "OCR failed" without the reason would be the same
    dead end this whole path exists to remove. The key never reaches meta.json: it is only ever
    set on the way to a raise.
    """
    if not shutil.which(OCR_BINARY):
        return None
    langs = os.environ.get("MISAKA_OCR_LANGS") or OCR_LANGS_DEFAULT
    with tempfile.TemporaryDirectory(prefix="misaka-ocr-") as tmp:
        sidecar = os.path.join(tmp, "sidecar.txt")
        try:
            out = subprocess.run(
                [OCR_BINARY, "--sidecar", sidecar, "--skip-text", "-l", langs,
                 p, os.path.join(tmp, "ocr.pdf")],
                capture_output=True, text=True, timeout=OCR_TIMEOUT, check=False)
            if out.returncode != 0:
                _note(meta, "ocr_error",
                      " ".join((out.stderr or "").split())[-200:] or f"exit {out.returncode}")
                return None
            with open(sidecar, encoding="utf-8") as f:
                text = f.read()
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            _note(meta, "ocr_error", str(error)[:200])   # missing or unreadable sidecar, timeout
            return None
    return _form_feed_pages(text)


def _merge_ocr(pages, ocr):
    """``(pages, the 1-based numbers of the pages taken from OCR)``.

    A PDF reaches OCR when fewer than a fifth of its pages carry text, so up to a fifth of them
    *do* -- and ``--skip-text`` deliberately leaves those alone, which means the sidecar's entry
    for such a page is ocrmypdf's placeholder rather than its words. Taking the sidecar whole
    therefore wrote a placeholder over the real text of exactly the pages a mostly-scanned book
    still had: the handful of typeset pages in a scan of a printed book, which are usually the
    front matter a citation needs.

    So the two are merged page by page, on the same "more than a caption's worth" rule the rest
    of this module uses. Both sequences are one entry per page of the PDF and align by index;
    when they do not (pdftotext and ocrmypdf disagreeing about the page count means one of them
    read a damaged file), OCR is taken as it stands rather than pasted against page numbers it
    does not belong to -- a citation that lands on the wrong page is worse than a scan.
    """
    if len(ocr) != len(pages):
        return ocr, list(range(1, len(ocr) + 1))
    merged, ocred = [], []
    for number, (extracted, scanned) in enumerate(zip(pages, ocr), 1):
        if len(extracted.strip()) > 20:
            merged.append(extracted)        # a real text layer: never OCR over it
        else:
            merged.append(scanned)
            ocred.append(number)
    return merged, ocred


def _pdf_pages(p, meta=None):
    pages = _pdf_text_layer(p)
    if _has_text_layer(pages):
        return pages                        # a real text layer: never OCR over it
    ocr = _ocr_pages(p, meta)
    if ocr and _has_text_layer(ocr):
        merged, ocred = _merge_ocr(pages, ocr)
        # doc_list/doc_outline badge OCR text as lower fidelity, and doc_verify names the page.
        # ``ocr_pages`` is written only when the text layer survived somewhere, so the common
        # case -- a scan, every page of it OCR'd -- does not carry a list of every page number.
        _note(meta, "ocr", True)
        if len(ocred) < len(merged):
            _note(meta, "ocr_pages", ocred)
        return merged
    return pages                            # ingest turns it away, naming the install


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


def _paginate(s, chars=3000):
    """Cut running text into pages at paragraph breaks -- a format without pages still needs
    somewhere for a citation to point."""
    if not s.strip():
        return []
    out, buf, size = [], [], 0
    for para in re.split(r"(\n\s*\n)", s):
        buf.append(para)
        size += len(para)
        if size >= chars:
            out.append("".join(buf))
            buf, size = [], 0
    if buf:
        out.append("".join(buf))
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
    """An EPUB read as the book it is: chapter after chapter, in spine order.

    ``meta`` goes unused: EPUB content is UTF-8 or UTF-16 by specification, so there is no
    encoding here that had to be guessed and recorded."""
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
                pages.extend(_paginate(htmltext.readable(markup)[0], chars))
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
# The last group is text with no format of its own: a card's analysis.csv, results.json, run.log
# or paper.tex. These entered the corpus as text before this table existed, and the path that
# carries them (``documents/workspace.ingest_artifacts``) swallows a ValueError without a word --
# so refusing a suffix here dropped the card's deliverable in silence. What was actually wrong
# with reading them as text was never the suffix: it was errors='replace', and _decode_bytes has
# closed that door for every format at once.

_EXTRACTORS = {
    ".pdf": _pdf_pages,
    ".epub": _epub_pages,
    ".html": _html_pages, ".htm": _html_pages, ".xhtml": _html_pages,
    ".md": _text_pages, ".markdown": _text_pages, ".txt": _text_pages,
    ".bib": _text_pages, ".csv": _text_pages, ".tsv": _text_pages, ".rst": _text_pages,
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
_NOT_SWEPT = frozenset({".json", ".jsonl", ".ndjson", ".log", ".yaml", ".yml"})
SCAN_SUFFIXES = frozenset(_EXTRACTORS) - _NOT_SWEPT


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
    """The title a document carries inside itself (EPUB ``dc:title``, HTML ``<title>``), or None.

    Preferred over the file name, which for a downloaded book is whatever the URL ended in.
    """
    ext = os.path.splitext(p)[1].lower()
    try:
        if ext == ".epub":
            with zipfile.ZipFile(p) as archive:
                title = _epub_spine(archive)[0]
        elif _EXTRACTORS.get(ext) is _html_pages:
            title = htmltext.readable(_read_text(p))[1]
        else:
            return None
    except (ValueError, OSError, zipfile.BadZipFile):
        return None
    return htmltext.clip(title.strip(), htmltext.MAX_TITLE_CHARS) or None


def build_tree(p):
    """Return the PageIndex outline of a PDF as JSON text, or None for other formats or on failure."""
    if os.path.splitext(p)[1].lower() != ".pdf":
        return None
    try:
        from .pageindex import build_tree as pageindex_tree
        nodes = pageindex_tree(os.path.abspath(p))
        return json.dumps(nodes, ensure_ascii=False) if nodes else None
    except Exception:  # noqa: BLE001 - outline extraction must not block ingestion
        return None


# File-tree storage

def _page_path(ddir, page):
    # ponytail: four digits keep the names aligned up to 9,999 pages; readers sort by number, so more still works.
    return os.path.join(ddir, "pages", f"p{page:04d}.txt")


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


def ingest(p, title=None, with_tree=True, task_id=None):
    """Index a file under its content hash and return ``(doc_id, page_count)``.

    Re-ingesting a known document only links the new ``task_id``. A suffix the corpus has no
    extractor for raises ValueError naming the formats it does read.
    """
    p = os.path.abspath(os.path.expanduser(p))
    _extractor(p)          # refuse an unreadable format before hashing, and before extracting
    sha = sha256_file(p)
    doc_id = sha[:12]
    ddir = os.path.join(corpus_root(), doc_id)
    existing = resolve_doc(doc_id)
    if existing:
        if (_read_meta_at(existing) or {}).get("sha256") != sha:
            raise ValueError(f"Document ID collision: {doc_id}")
        return doc_id, _link(existing, p, task_id)
    if os.path.lexists(ddir):
        raise ValueError(f"Invalid or colliding corpus entry: {doc_id}")
    extracted = {}                          # what the extractor learned: encoding, OCR
    pages = extract_pages(p, meta=extracted)
    if not _has_text_layer(pages):
        raise _no_text_error(p, pages, extracted.pop("ocr_error", None))
    tree = build_tree(p) if (with_tree and len(pages) >= 20) else None
    # Build the document beside its final place and move it in with one rename: the corpus holds
    # a complete document or none, never a half-written directory that reads as "already indexed".
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
        meta = {"doc_id": doc_id, "title": title or source_title(p) or os.path.basename(p),
                "orig_path": p, "paths": [p],
                "sha256": sha, "pages": len(pages), "task_id": task_id,
                "task_ids": [task_id] if task_id else [], "added_at": int(time.time()),
                **extracted}
        with open(os.path.join(stage, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        try:
            os.replace(stage, ddir)
        except OSError:
            existing = resolve_doc(doc_id)
            if not existing or (_read_meta_at(existing) or {}).get("sha256") != sha:
                # Not a concurrent ingest of the same content.
                raise
            _link(existing, p, task_id)          # the loser still owns this task's link to the document
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    shutil.rmtree(stage, ignore_errors=True)    # left only when another ingest won the rename
    return doc_id, len(pages)


def scan(directory, task_id=None, with_tree=True):
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
                ingested.append((ingest(p, task_id=task_id, with_tree=with_tree)[0], p))
            except (ValueError, OSError) as e:
                skipped.append((p, str(e)))
    return ingested, skipped


def docs(workspace=None):
    """List indexed documents oldest first; ``workspace`` keeps only those whose source file lives under that folder."""
    root, out = corpus_root(), []
    for name in (os.listdir(root) if os.path.isdir(root) else []):
        ddir = resolve_doc(name, workspace=workspace)
        if not ddir:
            continue
        m = _read_meta_at(ddir)
        m = dict(m)
        m["has_tree"] = _real_file(os.path.join(ddir, "tree.json"), ddir)
        out.append(m)
    return sorted(out, key=lambda m: m.get("added_at", 0))


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
        if needle not in normalize_for_quote_match(text, keep_break_hyphens=keep):
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
        for page, text in _iter_pages(did, workspace=workspace):
            span = _locate(text, needle)
            if span:
                pos, end = span
                snip = text[max(0, pos - 12):pos] + "<<" + text[pos:end] + ">>" + text[end:end + 12]
                hits.append({"doc_id": did, "page": page, "s": snip.replace("\n", " ")})
                if len(hits) >= limit:
                    return hits
    return hits


def verify_quote(doc_id, quote, page=None, workspace=None):
    """Verify an exact quotation, optionally on one page, ignoring the differences
    ``normalize_for_quote_match`` folds. The returned offset indexes the raw page, and the claim
    hash binds the quotation as the caller wrote it."""
    needle = normalize_for_quote_match(quote)
    if not needle:
        return None
    for pg, text in _iter_pages(doc_id, lo=page, hi=page, workspace=workspace):
        span = _locate(text, needle)
        if not span:
            continue
        real = span[0]
        return {"page": pg, "offset": real, "claim_hash": claim_hash(doc_id, pg, real, quote)}
    return None


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


def read_pages(doc_id, start, end, max_chars=12000, offset=0, workspace=None):
    """The pages' text as one window: ``offset`` characters in, ``max_chars`` long, with a note
    on how to continue when there is more -- so a single page longer than the window is read
    in successive calls rather than never. Pages are read only up to the window's end."""
    offset = max(0, int(offset or 0))
    stop = offset + max_chars
    pieces, seen, more = [], 0, False
    for page, t in _iter_pages(doc_id, lo=start, hi=end, workspace=workspace):
        chunk = f"\n--- p{page} ---\n{t}"
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
    return {"mode": "pages", "title": m["title"],
            "pages": page_heads(doc_id, limit=10000, workspace=workspace)}

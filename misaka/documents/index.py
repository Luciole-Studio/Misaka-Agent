"""Document ingestion, PageIndex navigation, literal search, and quote verification."""
import functools
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import unicodedata

from misaka.utils import atomic

SCAN_SUFFIXES = {".pdf", ".md", ".markdown", ".txt"}
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


def _pdf_pages(p):
    try:
        out = subprocess.run(["pdftotext", "-layout", p, "-"], capture_output=True,
                             text=True, timeout=300, check=False)
        if out.returncode == 0 and out.stdout.strip():
            pages = out.stdout.split("\f")
            if len(pages) > 1 and not pages[-1].strip():   # pdftotext ends every page with \f: the tail is no page
                pages.pop()
            return pages
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


def _text_pages(p, chars=3000):
    with open(p, encoding="utf-8", errors="replace") as f:
        s = f.read()
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


def extract_pages(p):
    ext = os.path.splitext(p)[1].lower()
    return _pdf_pages(p) if ext == ".pdf" else _text_pages(p)


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

    Re-ingesting a known document only links the new ``task_id``.
    """
    p = os.path.abspath(os.path.expanduser(p))
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
    pages = extract_pages(p)
    solid = sum(1 for t in pages if len(t.strip()) > 20)
    if not pages or not solid:
        raise ValueError(f"No text layer found; run OCR first: {os.path.basename(p)}")
    if solid < len(pages) * 0.2:
        raise ValueError(
            f"Incomplete text layer: only {solid} of {len(pages)} pages contain text "
            f"({solid / len(pages):.0%}). This is probably a scanned document with a few "
            f"text pages. Run OCR before indexing: {os.path.basename(p)}"
        )
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
        meta = {"doc_id": doc_id, "title": title or os.path.basename(p), "orig_path": p, "paths": [p],
                "sha256": sha, "pages": len(pages), "task_id": task_id,
                "task_ids": [task_id] if task_id else [], "added_at": int(time.time())}
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
    """Ingest every PDF / Markdown / text file under ``directory``, skipping hidden entries.

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

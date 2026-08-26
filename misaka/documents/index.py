"""Document ingestion, PageIndex navigation, literal search, and quote verification."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time

from misaka.utils import atomic

SCAN_SUFFIXES = {".pdf", ".md", ".markdown", ".txt"}


def corpus_root():
    """Content-addressed PageIndex store shared by every project folder."""
    return os.path.expanduser(os.environ.get("MISAKA_PAGEINDEX", "~/.misaka/pageindex"))


def doc_dir(doc_id):
    d = os.path.join(corpus_root(), doc_id)
    return d if os.path.isdir(d) else None


def _under(path, workspace):
    """True when ``path`` lives inside the folder ``workspace`` (symlinks resolved)."""
    return bool(path) and os.path.realpath(path).startswith(os.path.realpath(workspace) + os.sep)


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
    # ponytail: four digits cover 9,999 pages; expand only for a real larger document.
    return os.path.join(ddir, "pages", f"p{page:04d}.txt")


def _read_meta_at(ddir):
    """Read metadata from a known document directory."""
    try:
        with open(os.path.join(ddir, "meta.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _meta(doc_id):
    ddir = doc_dir(doc_id)
    return _read_meta_at(ddir) if ddir else None


def _tree(doc_id):
    ddir = doc_dir(doc_id)
    if not ddir:
        return None
    tp = os.path.join(ddir, "tree.json")
    if not os.path.exists(tp):
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
    if os.path.isdir(ddir):
        return doc_id, _link(ddir, p, task_id)
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
            if not os.path.isdir(ddir):          # not a concurrent ingest of the same content
                raise
            _link(ddir, p, task_id)              # the loser still owns this task's link to the document
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


def set_task_id(doc_id, task_id):
    """Associate an indexed document with a task card."""
    ddir = doc_dir(doc_id)
    if not ddir:
        return
    with _meta_lock(ddir):
        m = _meta(doc_id) or {}
        m["task_id"] = task_id
        ids = list(m.get("task_ids") or [])
        if task_id and task_id not in ids:
            ids.append(task_id)
        m["task_ids"] = ids
        atomic.write_text(os.path.join(ddir, "meta.json"), json.dumps(m, ensure_ascii=False, indent=2))


def docs(workspace=None):
    """List indexed documents oldest first; ``workspace`` keeps only those whose source file lives under that folder."""
    root, out = corpus_root(), []
    for name in (os.listdir(root) if os.path.isdir(root) else []):
        if STAGE_SUFFIX in name:
            continue
        ddir = os.path.join(root, name)
        m = _read_meta_at(ddir)
        if not m:
            continue
        if workspace and not any(_under(x, workspace) for x in (m.get("paths") or [m.get("orig_path")])):
            continue
        m = dict(m)
        m["has_tree"] = os.path.exists(os.path.join(ddir, "tree.json"))
        out.append(m)
    return sorted(out, key=lambda m: m.get("added_at", 0))


def _iter_pages(doc_id, lo=None, hi=None):
    """Yield selected pages in order without loading the entire document."""
    ddir = doc_dir(doc_id)
    if not ddir:
        return
    pdir = os.path.join(ddir, "pages")
    if not os.path.isdir(pdir):
        return
    for fn in sorted(os.listdir(pdir)):
        m = re.match(r"p(\d+)\.txt$", fn)
        if not m:
            continue
        pg = int(m.group(1))
        if (lo is not None and pg < lo) or (hi is not None and pg > hi):
            continue
        with open(os.path.join(pdir, fn), encoding="utf-8", errors="replace") as f:
            yield pg, f.read()


def read_page(doc_id, page):
    ddir = doc_dir(doc_id)
    if not ddir:
        return None
    fp = _page_path(ddir, page)
    if not os.path.exists(fp):
        return None
    with open(fp, encoding="utf-8", errors="replace") as f:
        return f.read()


def page_heads(doc_id, limit=200):
    """Return the first nonempty line of each page as a fallback outline."""
    out = []
    for page, text in _iter_pages(doc_id):
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        out.append({"page": page, "head": first[:60]})
        if len(out) >= limit:
            break
    return out


def search_literal(q, limit=10, doc_id=None, workspace=None):
    """Find exact text across indexed pages (scoped to ``workspace`` when given and no ``doc_id``)."""
    if not q:
        return []
    targets = [doc_id] if doc_id else [m["doc_id"] for m in docs(workspace)]
    hits = []
    for did in targets:
        for page, text in _iter_pages(did):
            pos = text.find(q)
            if pos >= 0:
                lo = max(0, pos - 12)
                snip = text[lo:pos] + "<<" + q + ">>" + text[pos + len(q):pos + len(q) + 12]
                hits.append({"doc_id": did, "page": page, "s": snip.replace("\n", " ")})
                if len(hits) >= limit:
                    return hits
    return hits


def verify_quote(doc_id, quote, page=None):
    """Verify an exact quotation, optionally on one page, ignoring whitespace differences."""
    norm = lambda s: re.sub(r"\s+", "", s)
    nq = norm(quote)
    if not nq:
        return None
    for pg, text in _iter_pages(doc_id, lo=page, hi=page):
        pos = norm(text).find(nq)
        if pos < 0:
            continue
        seen, real = 0, 0
        for i, ch in enumerate(text):
            if not ch.isspace():
                if seen == pos:
                    real = i
                    break
                seen += 1
        return {"page": pg, "offset": real, "claim_hash": claim_hash(doc_id, pg, real, quote)}
    return None


def tree_outline(doc_id, max_nodes=120):
    tree = _tree(doc_id)
    m = _meta(doc_id)
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


def node_pages(doc_id, node_id):
    tree = _tree(doc_id)
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


def read_pages(doc_id, start, end, max_chars=12000, offset=0):
    """The pages' text as one window: ``offset`` characters in, ``max_chars`` long, with a note
    on how to continue when there is more -- so a single page longer than the window is read
    in successive calls rather than never."""
    text = "".join(f"\n--- p{page} ---\n{t}" for page, t in _iter_pages(doc_id, lo=start, hi=end))
    offset = max(0, int(offset or 0))
    window = text[offset:offset + max_chars]
    if offset + max_chars < len(text):
        window += (f"\n… ({len(text) - offset - max_chars:,} more characters; call again with "
                   f"offset={offset + max_chars} to continue)")
    return window


def structure(doc_id):
    m = _meta(doc_id)
    if not m:
        return None
    tree = _tree(doc_id)
    if tree:
        return {"mode": "tree", "title": m["title"], "tree": tree}
    return {"mode": "pages", "title": m["title"], "pages": page_heads(doc_id, limit=10000)}

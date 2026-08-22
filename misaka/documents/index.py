"""Document ingestion, PageIndex navigation, literal search, and quote verification."""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time

from misaka.config import CFG


def _fallback_root():
    return os.path.expanduser(os.environ.get("MISAKA_CORPUS", "~/.misaka/corpus"))


def corpus_roots():
    """Return project-local corpus roots followed by the global fallback root."""
    roots = []
    database = os.path.expanduser(CFG["db"])
    if os.path.isfile(database):
        try:
            with sqlite3.connect(database) as con:
                paths = [row[0] for row in con.execute("SELECT path FROM projects")]
            roots.extend(os.path.join(path, "pageindex") for path in paths
                         if os.path.isdir(os.path.join(path, "pageindex")))
        except sqlite3.Error:
            pass
    fb = _fallback_root()
    if fb not in roots:
        roots.append(fb)
    return roots


def corpus_root(project_path=None):
    """Return the storage root for a project or the global fallback corpus."""
    if project_path:
        return os.path.join(os.path.realpath(project_path), "pageindex")
    return _fallback_root()


def doc_dir(doc_id):
    """Locate an indexed document across all corpus roots."""
    for r in corpus_roots():
        d = os.path.join(r, doc_id)
        if os.path.isdir(d):
            return d
    return None


# Content extraction and addressing

def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def claim_hash(doc_id, page, offset, quote):
    return hashlib.sha256(f"{doc_id}:{page}:{offset}:{quote}".encode("utf-8")).hexdigest()


def _pdf_pages(p):
    try:
        out = subprocess.run(["pdftotext", "-layout", p, "-"], capture_output=True,
                             text=True, timeout=300)
        if out.returncode == 0 and out.stdout.strip():
            return [t for t in out.stdout.split("\f")]
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        import fitz
        with fitz.open(p) as doc:
            return [pg.get_text() for pg in doc]
    except Exception:  # noqa: BLE001
        return []


def _text_pages(p, chars=3000):
    s = open(p, encoding="utf-8", errors="replace").read()
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
        return json.load(open(os.path.join(ddir, "meta.json"), encoding="utf-8"))
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
        return json.load(open(tp, encoding="utf-8"))
    except (OSError, ValueError):
        return None


def ingest(p, title=None, with_tree=True, project=None, project_path=None, task_id=None):
    """Index a file under its content hash and return ``(doc_id, page_count)``.

    Re-ingesting a known document only links the new ``task_id``; a document
    already indexed under another root is copied rather than rebuilt.
    """
    p = os.path.abspath(os.path.expanduser(p))
    sha = sha256_file(p)
    doc_id = sha[:12]
    ddir = os.path.join(corpus_root(project_path), doc_id)
    existing = ddir if os.path.isdir(ddir) else doc_dir(doc_id)
    if os.path.isdir(ddir):
        m = _read_meta_at(ddir) or {}
        ids = list(m.get("task_ids") or ([m["task_id"]] if m.get("task_id") else []))
        if task_id and task_id not in ids:
            ids.append(task_id)
            m["task_ids"] = ids
            with open(os.path.join(ddir, "meta.json"), "w", encoding="utf-8") as f:
                json.dump(m, f, ensure_ascii=False, indent=2)
        return doc_id, int(m.get("pages", 0))
    if existing:
        # Reuse an existing content-addressed index instead of rebuilding the tree.
        os.makedirs(os.path.dirname(ddir), exist_ok=True)
        shutil.copytree(existing, ddir)
        m = _read_meta_at(ddir) or {}
        m.update({"title": title or os.path.basename(p), "orig_path": p,
                  "project": project, "task_id": task_id,
                  "task_ids": [task_id] if task_id else [], "added_at": int(time.time())})
        with open(os.path.join(ddir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False, indent=2)
        return doc_id, int(m.get("pages", 0))
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
    os.makedirs(os.path.join(ddir, "pages"), exist_ok=True)
    for i, t in enumerate(pages):
        with open(_page_path(ddir, i + 1), "w", encoding="utf-8") as f:
            f.write(t)
    shutil.copy2(p, os.path.join(ddir, "source" + os.path.splitext(p)[1].lower()))
    if tree:
        with open(os.path.join(ddir, "tree.json"), "w", encoding="utf-8") as f:
            f.write(tree)
    meta = {"doc_id": doc_id, "title": title or os.path.basename(p), "orig_path": p,
            "sha256": sha, "pages": len(pages), "task_id": task_id,
            "task_ids": [task_id] if task_id else [], "project": project,
            "added_at": int(time.time())}
    with open(os.path.join(ddir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return doc_id, len(pages)


def set_task_id(doc_id, task_id):
    """Associate an indexed document with a task card."""
    ddir = doc_dir(doc_id)
    if not ddir:
        return
    m = _meta(doc_id) or {}
    m["task_id"] = task_id
    ids = list(m.get("task_ids") or [])
    if task_id and task_id not in ids:
        ids.append(task_id)
    m["task_ids"] = ids
    with open(os.path.join(ddir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)


def docs():
    """List metadata for every indexed document across all corpus roots, oldest first."""
    out, seen = [], set()
    for r in corpus_roots():
        if not os.path.isdir(r):
            continue
        for name in os.listdir(r):
            ddir = os.path.join(r, name)
            m = _read_meta_at(ddir)
            key = (m.get("project"), m.get("doc_id")) if m else None
            if not m or key in seen:
                continue
            seen.add(key)
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
        yield pg, open(os.path.join(pdir, fn), encoding="utf-8", errors="replace").read()


def read_page(doc_id, page):
    ddir = doc_dir(doc_id)
    if not ddir:
        return None
    fp = _page_path(ddir, page)
    return open(fp, encoding="utf-8", errors="replace").read() if os.path.exists(fp) else None


def page_heads(doc_id, limit=200):
    """Return the first nonempty line of each page as a fallback outline."""
    out = []
    for page, text in _iter_pages(doc_id):
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        out.append({"page": page, "head": first[:60]})
        if len(out) >= limit:
            break
    return out


def search_literal(q, limit=10, doc_id=None):
    """Find exact text across indexed pages."""
    if not q:
        return []
    targets = [doc_id] if doc_id else [m["doc_id"] for m in docs()]
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
    norm = lambda s: re.sub(r"\s+", "", s)  # noqa: E731
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


def read_pages(doc_id, start, end, max_chars=12000):
    buf, n = [], 0
    for page, text in _iter_pages(doc_id, lo=start, hi=end):
        chunk = f"\n--- p{page} ---\n{text}"
        if n + len(chunk) > max_chars:
            buf.append(
                f"\n… (section truncated; use doc:{doc_id}#pN to read remaining pages)"
            )
            break
        buf.append(chunk)
        n += len(chunk)
    return "".join(buf)


def structure(doc_id):
    m = _meta(doc_id)
    if not m:
        return None
    tree = _tree(doc_id)
    if tree:
        return {"mode": "tree", "title": m["title"], "tree": tree}
    return {"mode": "pages", "title": m["title"], "pages": page_heads(doc_id, limit=10000)}

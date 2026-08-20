"""工作区文献层:**文件树即正典**——真相就是能直接打开读的文件,不是 sqlite blob。

布局(2026-08-08 真希子裁决⑥,废弃 corpus.db):
    <corpus_root>/<doc_id>/
        source.<ext>       原始文件(入库时拷入,离开 ~/Downloads 这种易失区)
        pages/p0001.txt     逐页全文——宪法①:全文本身就是文件,可 grep 可 cat
        tree.json           PageIndex 结构树(可空=降级为按页导航)
        meta.json           {doc_id,title,orig_path,sha256,pages,task_id,project,added_at}

corpus_root 跟课题走:有 project → ~/Documents/Misaka/projects/<课题>/corpus/;
无 → ~/.misaka/corpus/(兜底,仍稳定)。查一个 doc 遍历这几个根(课题数个位数,快)。

为什么不再是 SQLite(爱酱审计①⑤⑥):FTS5 默认存了全文副本,却自称"只存坐标的索引";
源在 ~/Downloads 易失,库反成唯一副本、篡位真相。两本书/~2MB 规模,grep 毫秒级、
逐字复核只需读文件——SQLite/FTS5 是高射炮打蚊子。文件树既够用又天然合宪法①。

claim_hash = SHA256(doc_id:page:char_offset:exact_quote)——锚到第几页第几字,内容寻址,
doc_id 稳定则跨存储迁移不失效(所以从 corpus.db 迁文件树保持 doc_id 不变,已产出的引用不断)。
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import time

from misaka.config import CFG


def _fallback_root():
    return os.path.expanduser(os.environ.get("MISAKA_CORPUS", "~/.misaka/corpus"))


def corpus_roots():
    """所有 corpus 根:各课题的 + 全局兜底。查 doc 时遍历它们。"""
    roots = []
    proot = CFG.get("projects_root")
    if proot and os.path.isdir(proot):
        for name in sorted(os.listdir(proot)):
            c = os.path.join(proot, name, "corpus")
            if os.path.isdir(c):
                roots.append(c)
    fb = _fallback_root()
    if fb not in roots:
        roots.append(fb)
    return roots


def corpus_root(project=None):
    """入库落点:课题给了就落课题目录(源随课题走,稳定);否则兜底。"""
    proot = CFG.get("projects_root")
    if project and proot:
        return os.path.join(proot, project, "corpus")
    return _fallback_root()


def doc_dir(doc_id):
    """定位一个 doc 的目录(遍历各根);找不到返回 None。"""
    for r in corpus_roots():
        d = os.path.join(r, doc_id)
        if os.path.isdir(d):
            return d
    return None


# ── 纯函数:内容寻址与解析(与存储无关,原样保留)────────────────────

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
    """PageIndex flash 建树,返回 JSON 字符串或 None。装不上/失败即降级。"""
    from misaka.config import REPO   # 仓根 third_party/（审查 2026-08-20：曾指错到
    pi = os.path.join(REPO, "third_party", "PageIndex")   # misaka/research/ 下，树从未建成）
    py = os.path.join(pi, ".venv", "bin", "python")
    if not os.path.exists(py) or os.path.splitext(p)[1].lower() != ".pdf":
        return None
    try:
        r = subprocess.run([py, "run_pageindex.py", "--pdf_path", os.path.abspath(p),
                            "--flash", "--no-summary"],
                           cwd=pi, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            return None
        stem = os.path.basename(p).rsplit(".", 1)[0]
        out = os.path.join(pi, "results", f"{stem}_structure_flash.json")
        if not os.path.exists(out):
            return None
        d = json.load(open(out, encoding="utf-8"))
        nodes = d.get("structure") if isinstance(d, dict) else d
        return json.dumps(nodes, ensure_ascii=False) if nodes else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


# ── 文件树存储 ────────────────────────────────────────────────────

def _page_path(ddir, page):
    return os.path.join(ddir, "pages", f"p{page:04d}.txt")


def _read_meta_at(ddir):
    """直接读某个 doc 目录的 meta（不做定位）。docs() 遍历时用它,避免 doc_dir 全根重扫。"""
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


def ingest(p, title=None, with_tree=True, project=None, task_id=None):
    """入库:拷源 + 逐页写文件 + 树 + meta。返回 (doc_id, 页数)。同内容幂等。"""
    p = os.path.abspath(os.path.expanduser(p))
    sha = sha256_file(p)
    doc_id = sha[:12]
    existing = doc_dir(doc_id)
    if existing:  # 幂等:已在(任何根)就返回
        m = _meta(doc_id) or {}
        return doc_id, int(m.get("pages", 0))
    pages = extract_pages(p)
    solid = sum(1 for t in pages if len(t.strip()) > 20)
    if not pages or not solid:
        raise ValueError(f"抽不出文本层(扫描件?需先 OCR):{os.path.basename(p)}")
    if solid < len(pages) * 0.2:
        raise ValueError(
            f"文字层残缺:{len(pages)} 页里只有 {solid} 页有文字({solid/len(pages):.0%})"
            f"——多半是扫描件夹带少量文字页,先 OCR 再入库:{os.path.basename(p)}")
    tree = build_tree(p) if (with_tree and len(pages) >= 20) else None
    ddir = os.path.join(corpus_root(project), doc_id)
    os.makedirs(os.path.join(ddir, "pages"), exist_ok=True)
    for i, t in enumerate(pages):
        with open(_page_path(ddir, i + 1), "w", encoding="utf-8") as f:
            f.write(t)
    shutil.copy2(p, os.path.join(ddir, "source" + os.path.splitext(p)[1].lower()))  # 源随库,离开易失区
    if tree:
        with open(os.path.join(ddir, "tree.json"), "w", encoding="utf-8") as f:
            f.write(tree)
    meta = {"doc_id": doc_id, "title": title or os.path.basename(p), "orig_path": p,
            "sha256": sha, "pages": len(pages), "task_id": task_id, "project": project,
            "added_at": int(time.time())}
    with open(os.path.join(ddir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return doc_id, len(pages)


def set_task_id(doc_id, task_id):
    """把 doc 归到某张卡(产物入库后)。改 meta.json。"""
    ddir = doc_dir(doc_id)
    if not ddir:
        return
    m = _meta(doc_id) or {}
    m["task_id"] = task_id
    with open(os.path.join(ddir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)


def docs():
    """所有 doc 的 meta(按 added_at)。就地读当前根,不经 doc_dir 全根重扫（O(根×doc) 而非 O(根²)）。
    同 doc_id 万一落两根（ingest 幂等本应挡住）时按首见去重,不重复列出。"""
    out, seen = [], set()
    for r in corpus_roots():
        if not os.path.isdir(r):
            continue
        for name in os.listdir(r):
            ddir = os.path.join(r, name)
            m = _read_meta_at(ddir)
            if not m or m.get("doc_id") in seen:
                continue
            seen.add(m.get("doc_id"))
            m = dict(m)
            m["has_tree"] = os.path.exists(os.path.join(ddir, "tree.json"))
            out.append(m)
    return sorted(out, key=lambda m: m.get("added_at", 0))


def _iter_pages(doc_id, lo=None, hi=None):
    """(page, text) 逐页产出,按页序。lo/hi 限定页区间——**只 open 区间内的文件**,
    不把整份文献读进内存再过滤(read_pages 取 p50-55 不该把 464 页全读一遍)。"""
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
    """每页首行(给按页导航/树降级用)。"""
    out = []
    for page, text in _iter_pages(doc_id):
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        out.append({"page": page, "head": first[:60]})
        if len(out) >= limit:
            break
    return out


def search_literal(q, limit=10, doc_id=None):
    """逐字检索:遍历页文件子串匹配。文件树规模下直接扫,不需要 FTS5/trigram≥3 的限制。"""
    if not q:
        return []
    targets = [doc_id] if doc_id else [m["doc_id"] for m in docs()]
    hits = []
    for did in targets:
        for page, text in _iter_pages(did):
            pos = text.find(q)
            if pos >= 0:
                lo = max(0, pos - 12)
                snip = text[lo:pos] + "《" + q + "》" + text[pos + len(q):pos + len(q) + 12]
                hits.append({"doc_id": did, "page": page, "s": snip.replace("\n", " ")})
                if len(hits) >= limit:
                    return hits
    return hits


def search_semantic(q, canon, limit=5, doc_id=None):
    """语义检索——**未启用**:嵌入服务(8080)不在则返回 None,调用方回退词面。
    保留是给服务起来后的路径,当前生产零向量(爱酱审计②)。"""
    vq = canon.embed([q]) if canon else None
    if not vq:
        return None
    targets = [doc_id] if doc_id else [m["doc_id"] for m in docs()]
    rows = [(did, page, text) for did in targets for page, text in _iter_pages(did)]
    if not rows:
        return []
    vecs = canon.embed([t[:1500] for _, _, t in rows])
    if not vecs:
        return None
    scored = sorted(((canon.cosine(vq[0], v), r) for v, r in zip(vecs, rows)), key=lambda x: -x[0])
    return [{"doc_id": r[0], "page": r[1], "score": round(s, 3), "s": r[2][:120]}
            for s, r in scored[:limit]]


def verify_quote(doc_id, quote, page=None):
    """单正典复核:这句话真在这份文献里吗、第几页第几字?忽略空白差异。"""
    norm = lambda s: re.sub(r"\s+", "", s)  # noqa: E731
    nq = norm(quote)
    if not nq:
        return None
    for pg, text in _iter_pages(doc_id, lo=page, hi=page):   # 给了页就只读那页
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
                out.append("…(目录过长已截断)")
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
    for page, text in _iter_pages(doc_id, lo=start, hi=end):   # 只读区间内的页文件
        chunk = f"\n--- p{page} ---\n{text}"
        if n + len(chunk) > max_chars:
            buf.append(f"\n…(本节剩余页未取,用 doc:{doc_id}#pN 逐页读)")
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


if __name__ == "__main__":
    import tempfile
    tmp = tempfile.mkdtemp()
    os.environ["MISAKA_CORPUS"] = os.path.join(tmp, "corpus")
    CFG["projects_root"] = os.path.join(tmp, "projects")   # 隔离,不碰真数据

    p = os.path.join(tmp, "demo.md")
    open(p, "w", encoding="utf-8").write(
        "# 第一章\n\n御坂网络由两万名克隆体构成。\n\n" + "填充段落,用来把文档撑过一页。\n\n" * 300 +
        "# 第二章\n\n实验在第 10031 次后被终止,\n幸存 9969 名。\n")
    doc_id, n = ingest(p, title="测试文献")
    assert n >= 2, f"该切成多页,实得 {n}"
    assert ingest(p)[0] == doc_id, "同内容入库须幂等"
    # 全文确实是可直接读的文件(宪法①)
    assert os.path.isfile(os.path.join(doc_dir(doc_id), "pages", "p0001.txt"))
    assert os.path.isfile(os.path.join(doc_dir(doc_id), "source.md")), "源须拷入"
    assert os.path.isfile(os.path.join(doc_dir(doc_id), "meta.json"))

    hits = search_literal("两万名克隆体")
    assert hits and hits[0]["doc_id"] == doc_id, hits
    v = verify_quote(doc_id, "御坂网络由两万名克隆体构成")
    assert v and v["page"] == 1 and len(v["claim_hash"]) == 64, v
    v2 = verify_quote(doc_id, "实验在第 10031 次后被终止,幸存 9969 名")   # 跨行引文
    assert v2 and v2["page"] == n, v2
    assert verify_quote(doc_id, "实验在第 30000 次后被终止") is None, "假引文必须核不出"
    assert claim_hash(doc_id, 1, 0, "x") != claim_hash(doc_id, 2, 0, "x")

    # 落课题目录:project 给了就进课题 corpus,不进兜底
    proj_p = os.path.join(tmp, "book.md")
    open(proj_p, "w", encoding="utf-8").write("# 甲\n\n课题材料内容。\n\n" + "段。\n\n" * 100)
    CFG["projects_root"] = os.path.join(tmp, "projects")
    os.makedirs(os.path.join(CFG["projects_root"], "alpha", "corpus"), exist_ok=True)
    did2, _ = ingest(proj_p, project="alpha")
    assert doc_dir(did2).startswith(os.path.join(CFG["projects_root"], "alpha")), doc_dir(did2)
    assert did2 in [m["doc_id"] for m in docs()], "跨根 docs() 该列出课题里的"

    blank = os.path.join(tmp, "blank.txt")
    open(blank, "w", encoding="utf-8").write("\n\n".join(["  "] * 5) + "\n")
    try:
        ingest(blank)
        raise AssertionError("空白文档竟入库成功")
    except ValueError as e:
        assert "抽不出文本层" in str(e), e

    st = structure(doc_id)
    assert st["mode"] in ("tree", "pages") and st["title"] == "测试文献"
    set_task_id(doc_id, "t_xxx")
    assert _meta(doc_id)["task_id"] == "t_xxx"

    # 真希子疣②:read_pages 只 open 区间内的页,不整份读进内存再过滤
    opened = []
    _orig_open = open
    import builtins
    def _spy_open(fp, *a, **k):
        if str(fp).endswith(".txt") and "pages" in str(fp):
            opened.append(os.path.basename(fp))
        return _orig_open(fp, *a, **k)
    builtins.open = _spy_open
    try:
        read_pages(doc_id, 1, 1)          # 只该 open p0001.txt
    finally:
        builtins.open = _orig_open
    assert opened == ["p0001.txt"], f"read_pages(1,1) 不该读别的页,实开:{opened}"

    # 疣③:docs() 就地读,不因根数翻倍重扫(同 doc_id 去重)
    assert len({m["doc_id"] for m in docs()}) == len(docs()), "docs() 不该重复列同一 doc"
    print(f"indexer(文件树) selfcheck ok — {n} 页写成 pXXXX.txt;源拷入;真引文锚 p{v['page']}:off{v['offset']};"
          f"假引文核不出;跨课题根 docs/定位;read_pages 只读区间;扫描件拒收")

"""工作区导航层：把**任务板 + 研究材料 + 研究产物**摊成同一棵可导航的树。

设计源头（用户最初需求）："每次研究任务皆独立开设工作区，由 PageIndex 或类似结构
维护整个工作区"——所以树不只覆盖源文献，卡片与产物同在树上。

- 树 = PageIndex 形状（node_id/title/summary/nodes），单文献内部直接挂它自己的结构树。
- 叶子文本一律进同一个文件树正典（indexer.index，corpus/<doc>/pages/），材料与产物同一套引文复核。
- agent 的用法：outline() 看目录 → read(node_id) 取那一节原文，不必 ls/grep 摸黑。
"""
import json
import os

from misaka.research.indexer import index as corpus


def ingest_artifacts(bcon, task, artifacts=None):
    """把一张卡的产物收进正典。返回 [(doc_id, 文件名)]。空文件/抽不出文本的跳过并记账。

    artifacts：验收方已校验过的产物清单（相对路径）。给了就不再重读 report.json
    ——判卷与入库之间隔着分钟级窗口，盘上的单子可能已被改。没给（老调用方）
    才回落读盘。无论哪条路，入库前都再过一遍路径栅栏：绝对路径、`../`、
    软链越界一律不收（check_report 同款纪律，纵深防御）。
    """
    ws = task["workspace"] or ""
    if artifacts is None:
        try:
            with open(os.path.join(ws, "report.json"), encoding="utf-8") as f:
                artifacts = json.load(f).get("artifacts", [])
        except (OSError, ValueError):
            return []
    proj = task["project"] if "project" in task.keys() else None
    root = os.path.realpath(ws) if ws else ""
    out = []
    for rel in artifacts:
        rel = str(rel)
        p = os.path.realpath(os.path.join(root, rel))
        if (
            not root
            or os.path.isabs(rel)
            or not p.startswith(root + os.sep)
            or not os.path.isfile(p)
        ):
            continue  # 路径栅栏：越界产物绝不入共享正典
        try:  # 产物随卡的课题走;入库即带 task_id(不再事后 UPDATE)
            doc_id, _n = corpus.ingest(p, title=f"[{task['id']}] {rel}",
                                       project=proj, task_id=task["id"])
        except ValueError:
            continue  # 空文件/无文字层：不入正典（诚实拒收，同 index.ingest 纪律）
        out.append((doc_id, rel))
    return out


def _doc_node(m):
    """一份文献(meta dict) → 树节点。有 PageIndex 树挂结构，否则挂页码清单。"""
    did = m["doc_id"]
    node = {"node_id": f"doc:{did}", "title": m["title"],
            "summary": f"{m['pages']} 页", "nodes": []}
    tree = corpus._tree(did)
    if tree:
        node["nodes"] = tree
        node["summary"] += "（PageIndex 结构树）"
        return node
    node["nodes"] = [{"node_id": f"doc:{did}#p{h['page']}", "title": f"p{h['page']}",
                      "summary": h["head"].replace("\n", " ")}
                     for h in corpus.page_heads(did)]
    return node


def outline(bcon, task_id=None):
    """整个工作区的目录树。给 task_id 则只出那张卡的子树。"""
    tasks = ([bcon.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()] if task_id
             else bcon.execute("SELECT * FROM tasks ORDER BY created_at").fetchall())
    tasks = [t for t in tasks if t]
    by_task = {}
    all_docs = corpus.docs()
    for m in all_docs:
        if m.get("task_id"):
            by_task.setdefault(m["task_id"], []).append(m)

    card_nodes = []
    for t in tasks:
        kids = [{"node_id": f"task:{t['id']}#contract", "title": "交接单（目标/边界/验收）",
                 "summary": (t["body"] or "")[:80].replace("\n", " ")}]
        for m in by_task.get(t["id"], []):
            kids.append(_doc_node(m))
        card_nodes.append({"node_id": f"task:{t['id']}", "title": f"[{t['id']}] {t['title']}",
                           "summary": f"{t['status']} · {t['assignee']} · 产物 {len(by_task.get(t['id'], []))} 件",
                           "nodes": kids})

    materials = [_doc_node(m) for m in all_docs if not m.get("task_id")]

    tree = {"node_id": "ws", "title": "工作区", "nodes": [
        {"node_id": "board", "title": "任务板", "summary": f"{len(card_nodes)} 张卡",
         "nodes": card_nodes},
        {"node_id": "materials", "title": "研究材料", "summary": f"{len(materials)} 份",
         "nodes": materials},
        {"node_id": "graph", "title": "研究图", "summary": "发现/缺口/裁决（用 misaka graph 看）"},
    ]}
    return tree


def read(bcon, node_id, max_chars=6000):
    """取一个节点的原文。node_id 形如 task:t_x / task:t_x#contract / doc:<id> / doc:<id>#pN。"""
    if node_id.startswith("task:"):
        tid, _, part = node_id[5:].partition("#")
        t = bcon.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if not t:
            return None
        if part == "contract" or not part:
            return f"# [{t['id']}] {t['title']}\n状态 {t['status']} · 负责 {t['assignee']}\n\n{t['body'] or ''}"
        return None
    if node_id.startswith("doc:"):
        did, _, page = node_id[4:].partition("#p")
        if page:
            txt = corpus.read_page(did, int(page))
            return txt[:max_chars] if txt else None
        return corpus.read_pages(did, 1, 10 ** 9, max_chars=max_chars) or None
    return None


def render(tree, depth=0, out=None):
    """树 → 缩进文本（给人看，也给 agent 当目录读）。"""
    out = [] if out is None else out
    pad = "  " * depth
    s = tree.get("summary")
    out.append(f"{pad}{tree.get('title','')}  [{tree.get('node_id','')}]" + (f"  — {s}" if s else ""))
    for kid in (tree.get("nodes") or [])[:60]:
        render(kid, depth + 1, out)
    return "\n".join(out)


if __name__ == "__main__":
    import tempfile
    from misaka.extensions.board import db

    tmp = tempfile.mkdtemp()
    os.environ["MISAKA_CORPUS"] = os.path.join(tmp, "corpus")
    from misaka.config import CFG
    CFG["projects_root"] = os.path.join(tmp, "projects")
    bcon = db.connect(os.path.join(tmp, "board.db"))

    ws = os.path.join(tmp, "ws")
    os.makedirs(ws)
    open(os.path.join(ws, "out.md"), "w", encoding="utf-8").write(
        "# 结论\n\n御坂网络由两万名克隆体构成。\n\n" + "正文段落。\n\n" * 200)
    json.dump({"schema_version": 1, "status": "done", "summary": "写了结论",
               "artifacts": ["out.md"], "uncertain": []},
              open(os.path.join(ws, "report.json"), "w", encoding="utf-8"))
    tid = db.create_task(bcon, "试作卡", body="## 目标\nx\n## 边界\ny\n## 验收\n- out.md", assignee="s")
    db.set_workspace(bcon, tid, ws)

    got = ingest_artifacts(bcon, db.get(bcon, tid))
    assert len(got) == 1, got
    tree = outline(bcon)
    text = render(tree)
    assert "任务板" in text and "研究材料" in text and tid in text, text
    assert f"[{tid}] out.md" in text, "产物该在树上"

    contract = read(bcon, f"task:{tid}#contract")
    assert "## 验收" in contract, contract
    doc_id = got[0][0]
    page1 = read(bcon, f"doc:{doc_id}#p1")
    assert "两万名克隆体" in page1
    whole = read(bcon, f"doc:{doc_id}")
    assert "--- p1 ---" in whole
    # 产物与材料共用一套引文复核
    v = corpus.verify_quote(doc_id, "御坂网络由两万名克隆体构成")
    assert v and len(v["claim_hash"]) == 64, v
    assert read(bcon, "doc:nonexistent") is None
    print(f"workspace selfcheck ok — 卡/交接单/产物同树；产物入正典可锚 p{v['page']}；整份取文带截断标记")

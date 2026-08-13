"""刺（spike）：思辨红队标出的不稳点 → 校验 → 逐字核验 → 入图为缺口节点。

/research 模式的辩证生成器内核件（D5 预留的那一翼；设计见 docs/design/research-mode.md）。
只做校验/核验/入图，零 LLM 调用——critic 的调用封装在 board 层（2 期）。

硬闸（R14，金标不变量）：quote 必须逐字（忽略空白）出现在靶里——
靶＝图节点文本 或 卡产物文件（同交卷的路径栅栏）；核不上的刺整条丢弃，绝不入图。
判重（canon.dedup）由调用方随后执行，与收割同一先例。
"""
from pathlib import Path, PurePosixPath, PureWindowsPath

SPIKE_KINDS = ("臆想", "偏见", "出处弱", "过度概括", "矛盾", "覆盖缺口")
MAX_SPIKES = 16
MAX_QUOTE_CHARS = 500
MAX_TEXT_CHARS = 2000
MAX_TARGET_FILE_BYTES = 512 * 1024   # ponytail: 产物靶上限半兆；更大的先不当靶


def _norm(s):
    return "".join((s or "").split())   # 与证据台账同一口径：空白差异不影响核验


def validate_spikes(obj):
    """critic 输出 → (spikes, errors)。手搓校验（照 validate.py 先例）。"""
    if isinstance(obj, dict):
        obj = obj.get("spikes")
    if not isinstance(obj, list):
        return [], ["spikes 须是数组"]
    # 空数组合法：宁缺毋滥——critic 找不到真刺时不许被格式逼着造伪刺
    errors = []
    if len(obj) > MAX_SPIKES:
        errors.append(f"超出 {MAX_SPIKES} 条上限，尾部忽略")   # 有界但不静默
    spikes = []
    for i, s in enumerate(obj[:MAX_SPIKES]):
        if not isinstance(s, dict):
            errors.append(f"刺{i}: 不是对象")
            continue
        target, quote, kind = s.get("target"), s.get("quote"), s.get("kind")
        why, suggest = s.get("why"), s.get("suggest")
        bad = []
        if not (isinstance(target, str) and target.strip()):
            bad.append("target 缺失")
        if not (isinstance(quote, str) and quote.strip()) or len(quote or "") > MAX_QUOTE_CHARS:
            bad.append("quote 缺失或超长")
        if kind not in SPIKE_KINDS:
            bad.append("kind 须是 " + "/".join(SPIKE_KINDS) + " 之一")
        if not (isinstance(why, str) and why.strip()):
            bad.append("why 缺失")
        if not (isinstance(suggest, str) and len(suggest.strip()) >= 8):
            bad.append("suggest 缺失或太短（它将成为缺口节点，至少 8 字）")
        if bad:
            errors.append(f"刺{i}: " + "；".join(bad))
            continue
        try:
            weight = min(max(float(s.get("weight", 0.5)), 0.05), 1.0)
        except (TypeError, ValueError):
            weight = 0.5
        spikes.append({
            "target": target.strip(),
            "quote": quote.strip(),
            "kind": kind,
            "why": why.strip()[:MAX_TEXT_CHARS],
            "suggest": suggest.strip()[:MAX_TEXT_CHARS],
            "weight": weight,
        })
    return spikes, errors


def _file_target(workspace, target):
    """产物靶原文：工作区内相对路径普通文件（与交卷同一路径栅栏，纵深防御）。"""
    if not workspace:
        return None
    posix, windows = PurePosixPath(target), PureWindowsPath(target)
    if (posix.is_absolute() or bool(windows.drive or windows.root)
            or ".." in posix.parts or ".." in windows.parts):
        return None
    try:
        root = Path(workspace).resolve(strict=True)
        candidate = (root / target).resolve(strict=True)
        candidate.relative_to(root)
        if not candidate.is_file() or candidate.stat().st_size > MAX_TARGET_FILE_BYTES:
            return None
        return candidate.read_text(encoding="utf-8", errors="replace")
    except (OSError, RuntimeError, ValueError):
        return None


def verify_spike(con, store, spike, workspace=None):
    """R14 硬闸：逐字核验。返回 (ok, 丢弃理由)。靶找不到＝核不上。"""
    target = spike["target"]
    row = store.get(con, target)
    text = row["text"] if row is not None else _file_target(workspace, target)
    if text is None:
        return False, f"靶不存在或不可读：{target}"
    if _norm(spike["quote"]) not in _norm(text):
        return False, f"引用核不上：{target}"
    return True, None


def ingest(con, store, obj, *, workspace=None, project=None, task_id=None):
    """校验→核验→入图。返回 (gap_ids, dropped)；dropped 逐条给理由（记账不静默）。

    节点文本＝研究问题＋刺注（单行），前沿生卡时合同自然带上"为什么不稳"；
    刺型记入 provenance 位（R 设计 §四），spike_of 边指回靶。
    """
    spikes, errors = validate_spikes(obj)
    dropped = list(errors)
    gap_ids = []
    for s in spikes:
        ok, why_dropped = verify_spike(con, store, s, workspace)
        if not ok:
            dropped.append(why_dropped)
            continue
        text = f"{s['suggest']}［{s['kind']}·{s['why']}］"
        nid = store.add_node(con, "gap", text, weight=s["weight"],
                             task_id=task_id, provenance=s["kind"], project=project)
        store.add_edge(con, nid, s["target"], "spike_of")
        gap_ids.append(nid)
    return gap_ids, dropped


if __name__ == "__main__":
    import sqlite3
    import tempfile
    from misaka.research.kernel import store

    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    store.init(con)
    finding = store.add_node(con, "finding", "该馆 1953 年完成了系统性档案迁移", project="alpha")
    ws = tempfile.mkdtemp()
    (Path(ws) / "argument.md").write_text(
        "# 立论\n\n主流叙事认为迁移是行政决定，\n本稿下注：实为政治清洗的一环。\n",
        encoding="utf-8")

    def spike(**kw):
        base = {"target": finding, "quote": "1953 年完成了系统性档案迁移",
                "kind": "出处弱", "why": "只有一处二手转引",
                "suggest": "核对该馆入藏簿原件确认迁移年份", "weight": 0.8}
        base.update(kw)
        return base

    # 校验面：坏 kind / 短 suggest / 超上限记账
    _, errs = validate_spikes({"spikes": [spike(kind="胡说")]})
    assert any("kind" in e for e in errs), errs
    _, errs = validate_spikes([spike(suggest="太短")])
    assert any("suggest" in e for e in errs), errs
    _, errs = validate_spikes([spike()] * (MAX_SPIKES + 1))
    assert any("上限" in e for e in errs), "有界必须不静默"
    assert validate_spikes({"spikes": []}) == ([], []), "空数组合法（宁缺毋滥）"
    assert validate_spikes({"没有spikes键": 1})[1], "缺 spikes 键该报错"
    ok_spikes, _ = validate_spikes([spike(weight=99)])
    assert ok_spikes[0]["weight"] == 1.0, "weight 该被钳位"

    # 核验面（R14 金标不变量）：核不上的伪刺进不了图
    ids, dropped = ingest(con, store, [spike(quote="1955 年完成了迁移")], project="alpha")
    assert not ids and any("核不上" in d for d in dropped), (ids, dropped)
    assert not store.nodes(con, kind="gap"), "伪刺绝不入图"

    # 靶不存在＝核不上
    ids, dropped = ingest(con, store, [spike(target="n_没有的")])
    assert not ids and any("靶不存在" in d for d in dropped)

    # 路径栅栏：越界产物靶一律不读
    for bad in ("../外面.md", "/etc/passwd"):
        ids, dropped = ingest(con, store, [spike(target=bad, quote="随便")], workspace=ws)
        assert not ids, bad

    # 真刺入图：节点文本带刺注、刺型进 provenance、spike_of 边指回靶
    ids, dropped = ingest(con, store, [
        spike(),
        spike(target="argument.md", quote="实为政治清洗的一环", kind="臆想",
              why="无档案佐证", suggest="查找清洗决策链的原始文件依据"),
    ], workspace=ws, project="alpha", task_id="t_arg")
    assert len(ids) == 2 and not dropped, (ids, dropped)
    n = store.get(con, ids[0])
    assert "入藏簿" in n["text"] and "出处弱" in n["text"], n["text"]
    assert n["provenance"] == "出处弱" and n["project"] == "alpha"
    edges = {(r["src"], r["dst"]) for r in
             con.execute("SELECT src, dst FROM edges WHERE kind='spike_of'")}
    assert (ids[0], finding) in edges and (ids[1], "argument.md") in edges, edges
    print(f"spike selfcheck ok — 校验四拒/伪刺零入图/路径栅栏/真刺 {len(ids)} 条入图带刺注与回边")

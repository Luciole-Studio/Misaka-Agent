"""权重校准：harvester 预估 weight vs 实际回报的对账 + 桶级收缩修正。

兑现的是 frontier.py docstring 预注册的目标态（"VoI/PUCT/Thompson 等有了
『挖过之后回报如何』的历史数据再上"）——数据其实图里一直在攒，这里补上那条 join。

回报链（两跳边）：gap --expanded_to--> 卡 --from_task--> findings。
样本判据：gap 已扩为卡，且卡已**定局**——已收割（有 from_task 边）或 failed
（挖了没成果＝回报 0）。done 未收割/还在跑的卡不算样本（"还没打分"≠"零回报"）。

回报口径（**≠证据口径**）：verified 计 1.0×weight、analogy 计 0.5×weight、
invented/未标 计 0——这是"这次挖掘值不值"的折扣，不改任何发现的证据档位。

修正用**收缩**而非 UCB/Thompson：correction = (hits + K) / (n + K)，朝中性 1 收缩。
零样本＝1.0（行为不变），样本少＝接近中性（仍按先验被挑，样本自然涨——自愈饥饿环），
样本足＝逼近实际兑现率。K=5。
# ponytail: 节点级 bandit 不适用（gap 挖完即 expanded，臂不重复）；桶级乐观探索项
# 等桶样本上百再议，收缩在当前规模已同时覆盖冷启动与饥饿两个坑。
"""
from misaka.research.kernel import precedent

K = 5                      # 收缩强度：等效"先验里塞 K 个兑现样本"
DISCOUNT = {"verified": 1.0, "analogy": 0.5}   # 回报折扣；invented/未标=0
BUCKETS = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.001)]


def bucket_of(w):
    for i, (lo, hi) in enumerate(BUCKETS):
        if lo <= w < hi:
            return i
    return len(BUCKETS) - 1


def samples(con, project=None):
    """已定局的 (gap 预估, 实际回报) 样本。线性扫（规模纪律：破万再上索引）。"""
    q = ("SELECT g.id gid, g.weight est, e.dst tid FROM nodes g"
         " JOIN edges e ON e.src=g.id AND e.kind='expanded_to' WHERE g.kind='gap'")
    args = ()
    if project:
        q, args = q + " AND g.project=?", (project,)
    out = []
    for gid, est, tid in con.execute(q, args):
        harvested = con.execute(
            "SELECT 1 FROM edges WHERE src=? AND kind='from_task' LIMIT 1", (tid,)).fetchone()
        if harvested:
            payoff = sum(
                DISCOUNT.get(prov or "", 0.0) * w
                for w, prov in con.execute(
                    "SELECT n.weight, n.provenance FROM edges e JOIN nodes n"
                    " ON n.id=e.dst AND n.kind='finding'"
                    " WHERE e.src=? AND e.kind='from_task'", (tid,)))
        else:
            # kernel 读 tasks 表（同库）判定局；只看 status 一列，不 import extensions
            t = con.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
            if not t or t["status"] != "failed":
                continue   # 未定局：不算样本
            payoff = 0.0
        out.append({"gap_id": gid, "est": float(est), "task_id": tid,
                    "payoff": round(payoff, 3), "hit": payoff > 0})
    return out


def table(con, project=None):
    """校准账：按预估分桶的 n/预估均值/兑现率/平均回报/Brier。"""
    rows = []
    per = {i: [] for i in range(len(BUCKETS))}
    for s in samples(con, project):
        per[bucket_of(s["est"])].append(s)
    for i, (lo, hi) in enumerate(BUCKETS):
        ss = per[i]
        if not ss:
            rows.append({"bucket": f"{lo:.1f}-{hi:.1f}", "n": 0})
            continue
        n = len(ss)
        est_mean = sum(s["est"] for s in ss) / n
        hit_rate = sum(s["hit"] for s in ss) / n
        rows.append({"bucket": f"{lo:.1f}-{hi:.1f}", "n": n,
                     "est_mean": round(est_mean, 2), "hit_rate": round(hit_rate, 2),
                     "payoff_mean": round(sum(s["payoff"] for s in ss) / n, 2),
                     "brier": round(sum((s["est"] - s["hit"]) ** 2 for s in ss) / n, 3),
                     "correction": round((sum(s["hit"] for s in ss) + K) / (n + K), 2)})
    return rows


def corrections(con, project=None):
    """桶号 → 收缩修正系数。frontier.score 的乘子；缺桶按 1.0 处理。"""
    agg = {}
    for s in samples(con, project):
        b = bucket_of(s["est"])
        n, hits = agg.get(b, (0, 0))
        agg[b] = (n + 1, hits + (1 if s["hit"] else 0))
    return {b: (hits + K * 1.0) / (n + K) for b, (n, hits) in agg.items()}


def view(con, project=None):
    """给人/LO 看的文本表。"""
    lines = [f"{'预估档':<10}{'n':>4}{'预估均值':>8}{'兑现率':>7}{'均回报':>7}{'Brier':>7}{'修正':>6}"]
    for r in table(con, project):
        if r["n"] == 0:
            lines.append(f"{r['bucket']:<11}{0:>4}{'—':>9}")
            continue
        lines.append(f"{r['bucket']:<11}{r['n']:>4}{r['est_mean']:>9}{r['hit_rate']:>8}"
                     f"{r['payoff_mean']:>8}{r['brier']:>7}{r['correction']:>6}")
    total = sum(r["n"] for r in table(con, project))
    lines.append(f"（样本 {total}；修正=收缩式 (hits+{K})/(n+{K})，样本少自动趋中性 1.0）")
    return "\n".join(lines)


def drift_precedents(con, canon, min_n=8, thresh=0.3):
    """系统性高/低估的桶 → 立判例喂回 harvester（source=calibration_drift，查重防复读）。"""
    made = []
    for r in table(con):
        if r["n"] < min_n:
            continue
        gap = r["est_mean"] - r["hit_rate"]
        if abs(gap) < thresh:
            continue
        direction = "高估" if gap > 0 else "低估"
        situation = (f"harvester 对预估权重 {r['bucket']} 档的 gap 系统性{direction}"
                     f"（n={r['n']}，预估均值 {r['est_mean']} vs 实际兑现率 {r['hit_rate']}）")
        if con.execute("SELECT 1 FROM precedents WHERE source='calibration_drift'"
                       " AND situation=?", (situation,)).fetchone():
            continue   # 同一漂移只立一次
        ruling = (f"给这一档缺口打 weight 时参照实际兑现率 {r['hit_rate']}"
                  f"{'下调' if gap > 0 else '上调'}预估，别按惯性给分")
        made.append(precedent.add(con, situation, ruling, "calibration_drift", None, canon))
    return made


if __name__ == "__main__":
    import sqlite3
    from misaka.research.kernel import frontier, store

    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    store.init_all(con)
    con.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT)")  # 最小任务表

    def mk(est, tid, findings, tstatus="done"):
        g = store.add_node(con, "gap", f"缺口 {tid}", weight=est)
        store.add_edge(con, g, tid, "expanded_to")
        con.execute("INSERT INTO tasks VALUES (?,?)", (tid, tstatus))
        for w, prov in findings:
            f = store.add_node(con, "finding", f"发现 {tid} {w}", weight=w, provenance=prov)
            store.add_edge(con, tid, f, "from_task")
        return g

    mk(0.9, "t_hi1", [(0.8, "verified")])            # 高档兑现
    mk(0.9, "t_hi2", [(0.6, "analogy")])             # 高档半兑现(0.3>0 仍 hit)
    mk(0.9, "t_hi3", [(0.9, "invented")])            # invented 计 0 → 未兑现
    mk(0.9, "t_hi4", [], tstatus="failed")           # 失败卡 → 回报 0 样本
    mk(0.3, "t_lo1", [(0.9, "verified")])            # 低档兑现(低估证据)
    mk(0.9, "t_run", [], tstatus="running")          # 未定局 → 不算样本
    g_pending = mk(0.9, "t_done_unharv", [], tstatus="done")  # done 未收割 → 不算样本

    ss = samples(con)
    assert len(ss) == 5, f"未定局卡混进样本: {ss}"
    by_task = {s["task_id"]: s for s in ss}
    assert by_task["t_hi1"]["payoff"] == 0.8 and by_task["t_hi1"]["hit"]
    assert by_task["t_hi2"]["payoff"] == 0.3, "analogy 该减半"
    assert by_task["t_hi3"]["payoff"] == 0.0 and not by_task["t_hi3"]["hit"], "invented 不计回报"
    assert by_task["t_hi4"]["payoff"] == 0.0, "failed 卡是合法零回报样本"

    cs = corrections(con)
    hi = bucket_of(0.9)
    # 高档 4 样本 2 兑现:(2+5)/(4+5)=0.778;低档 1 样本 1 兑现:(1+5)/(1+5)=1.0
    assert abs(cs[hi] - 7 / 9) < 1e-9, cs
    assert cs[bucket_of(0.3)] == 1.0
    assert bucket_of(0.5) not in cs, "无样本桶不出现(frontier 按 1.0 兜底)"

    # frontier 接线:修正全 1=行为不变;高档修正<1 时排序真的会掉
    n_hi = store.get(con, mk(0.9, "t_x", [], tstatus="failed"))
    n_lo = store.get(con, mk(0.72, "t_y", [], tstatus="failed"))
    s_plain = frontier.score(n_hi)
    assert frontier.score(n_hi, corrections={}) == s_plain, "空修正必须=现状"
    assert frontier.score(n_hi, corrections={hi: 0.5}) < frontier.score(n_lo, corrections={hi: 0.5}), \
        "高档打五折后该输给 0.72 档"

    # 漂移判例:高档预估 0.9 vs 兑现 0.5 → gap 0.4>0.3,但 n=6<8 不立;补样本到 8 再立且只立一次
    class _NoEmbed:
        def embed(self, xs):
            return None
    from misaka.research.kernel import precedent as _p
    _p.init(con)
    mk(0.9, "t_hi5", [], tstatus="failed")
    mk(0.9, "t_hi6", [], tstatus="failed")
    mk(0.9, "t_hi7", [], tstatus="failed")   # 凑满 min_n=8(含 frontier 段的 t_x)
    made1 = drift_precedents(con, _NoEmbed())
    made2 = drift_precedents(con, _NoEmbed())
    assert len(made1) == 1 and made2 == [], (made1, made2)
    print(f"calibration selfcheck ok — 样本判据(未定局剔除)/analogy 减半/invented 零计/"
          f"failed 零回报/收缩 {cs[hi]:.3f}/空修正=现状/漂移判例只立一次")

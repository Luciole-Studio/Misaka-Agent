"""饱和仪表：挖够了没有——用统计回答，不靠感觉。

Good-Turing 缺失质量（图灵在布莱切利园发明的那个）：
    P(下一铲挖出全新东西) ≈ n1 / N
n1 = 只被撞见过一次的发现数，N = 发现的总观测次数（含重复撞见）。
读数高＝还有富矿；读数低＝**只能说"至少还剩这么少"**，不是"可以停"的证书
（相关采样器共享盲区对估计量不可见——停机许可永远是预算和人的决定）。
"""


def _counts(con, kind="finding", project=None):
    q = "SELECT sightings FROM nodes WHERE kind=? AND status IN ('open','expanded')"
    args = [kind]
    if project:  # None=全局；传了才按课题算——否则三个课题的缺口混在一起，读数是假的
        q, _ = q + " AND project=?", args.append(project)
    return [r[0] for r in con.execute(q, args).fetchall()]


def reading(con, kind="finding", project=None):
    """返回仪表 dict。distinct=互异发现数，observations=总撞见次数。"""
    c = _counts(con, kind, project)
    distinct, N = len(c), sum(c)
    n1 = sum(1 for x in c if x == 1)
    p_new = (n1 / N) if N else 1.0
    # 覆盖率下界估计：已见质量 = 1 - P(新)，只是下界，别当承诺
    return {"distinct": distinct, "observations": N, "singletons": n1,
            "p_new": round(p_new, 3), "coverage_lower_bound": round(1 - p_new, 3)}


def verdict(r, target=0.10):
    """给人看的一句话。target=可接受的"下一铲出新概率"。"""
    if r["observations"] < 5:
        return "样本太少（<5 次观测），读数不可信——继续挖"
    if r["p_new"] > target:
        return f"还有富矿：下一铲出新概率 ≈ {r['p_new']:.0%} > 目标 {target:.0%}，继续挖"
    return (f"边际收益已低：下一铲出新概率 ≈ {r['p_new']:.0%} ≤ {target:.0%}。"
            "注意这只是下界——共享盲区看不见，停不停是预算决定")


if __name__ == "__main__":
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE nodes (kind TEXT, status TEXT, sightings INT)")
    # 全是新面孔 → 概率 1.0
    con.executemany("INSERT INTO nodes VALUES ('finding','open',?)", [(1,)] * 6)
    r = reading(con)
    assert r["p_new"] == 1.0 and r["distinct"] == 6, r
    # 反复撞见同几条 → 概率掉下来
    con.execute("DELETE FROM nodes")
    con.executemany("INSERT INTO nodes VALUES ('finding','open',?)", [(9,), (8,), (7,), (5,), (1,)])
    r2 = reading(con)
    assert r2["p_new"] < 0.05 and r2["singletons"] == 1, r2
    assert "边际收益已低" in verdict(r2) and "继续挖" in verdict(reading(con, "gap"))
    print(f"saturation selfcheck ok — 全新面孔 p_new={r['p_new']}；反复撞见 p_new={r2['p_new']}")

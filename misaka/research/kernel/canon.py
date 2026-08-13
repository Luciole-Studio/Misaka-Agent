"""判重：本地 bge-m3 嵌入 + 余弦 + 并查集合流。

ponytail: 线性扫描比对——几百个节点时 numpy 都不用，破万再上 HNSW。
嵌入服务不在时整体降级为"不判重"，绝不因此卡住流程。
"""
import json
import math
import os
import urllib.error
import urllib.request

EMBED_URL = os.environ.get("MISAKA_EMBED_URL", "http://127.0.0.1:8080/v1/embeddings")
# 阈值实测定标（bge-m3，中文研究陈述）：真同义改写 0.81，互异节点最高 0.74。
# 取 0.78 落在两者之间；换嵌入模型或换语种必须重新量，别沿用。
THRESHOLD = float(os.environ.get("MISAKA_DEDUP_THRESHOLD", "0.78"))


def embed(texts, timeout=60):
    """返回向量列表；服务不可用时返回 None（判重降级）。"""
    if not texts:
        return []
    req = urllib.request.Request(
        EMBED_URL, data=json.dumps({"input": texts}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return [d["embedding"] for d in json.loads(r.read())["data"]]
    except (urllib.error.URLError, OSError, KeyError, ValueError):
        return None


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def dedup(con, store, new_ids):
    """给新节点算嵌入，与同类既有节点比对；超阈值即合流。返回 [(dup, canon, sim)]。"""
    merged = []
    for nid in new_ids:
        n = store.get(con, nid)
        if not n or n["status"] != "open":
            continue
        vec = json.loads(n["embedding"]) if n["embedding"] else None
        if vec is None:
            got = embed([n["text"]])
            if got is None:
                return merged  # 服务不可用：本轮不判重（降级不阻塞）
            vec = got[0]
            con.execute("UPDATE nodes SET embedding=? WHERE id=?", (json.dumps(vec), nid))
        best, best_sim = None, 0.0
        for other in store.nodes(con, kind=n["kind"], status="open"):
            if other["id"] == nid or not other["embedding"]:
                continue
            sim = cosine(vec, json.loads(other["embedding"]))
            if sim > best_sim:
                best, best_sim = other, sim
        if best and best_sim >= THRESHOLD:
            store.merge_into(con, nid, best["id"])
            merged.append((nid, best["id"], round(best_sim, 3)))
    return merged


if __name__ == "__main__":
    a, b, c = "御坂网络是妹妹们的脑波网络", "御坂网络＝Sisters 之间的脑波连接网", "苏联国家档案馆藏有政治局文件"
    vs = embed([a, b, c])
    if vs is None:
        print("嵌入服务不可用——判重会降级（这是设计内的）")
    else:
        near, far = cosine(vs[0], vs[1]), cosine(vs[0], vs[2])
        assert near >= THRESHOLD > far, (near, THRESHOLD, far)  # 阈值须真能分开这两类
        print(f"canon selfcheck ok — 同义 {near:.3f} ≥ 阈值 {THRESHOLD} > 异义 {far:.3f}")

"""Small opt-in Recall@k harness for choosing an LCM embedding model."""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from misaka.extensions.lcm.dag import SummaryDAG, SummaryNode
from misaka.extensions.lcm.semantic import SemanticIndex
from misaka.extensions.lcm.store import MessageStore

SMOKE = {
    "documents": [
        {"id": "history", "text": "Local bureaucracies sustain their power through archival registration, budget allocation, and control over appointments."},
        {"id": "code", "text": "The SQLite lock was never released, so the next writer timed out and the process crashed."},
        {"id": "method", "text": "The study should separately examine how the original records were produced, how they survived, and how they were catalogued."},
        {"id": "noise", "text": "The afternoon is clear and pleasant; a good time for a walk."},
    ],
    "queries": [
        {"query": "How does institutional power persist over time?", "relevant_ids": ["history"]},
        {"query": "Why do several processes writing at once fail?", "relevant_ids": ["code"]},
        {"query": "How should the reliability of archival sources be assessed?", "relevant_ids": ["method"]},
    ],
}


def _rrf(lexical, semantic, k):
    scores = {}
    for ranking in (lexical, semantic):
        for rank, item in enumerate(ranking, 1):
            scores[item] = scores.get(item, 0.0) + 1 / (60 + rank)
    return sorted(scores, key=scores.get, reverse=True)[:k]


def run(dataset, model, k=3):
    root = Path(tempfile.mkdtemp(prefix="misaka-lcm-eval-"))
    db = root / "lcm.db"
    store, dag = MessageStore(db), SummaryDAG(db)
    node_to_doc = {}
    try:
        for document in dataset["documents"]:
            node = SummaryNode(session_id="eval", summary=document["text"])
            dag.add_node(node)
            node_to_doc[node.node_id] = document["id"]
        semantic = SemanticIndex(db, model)
        totals = {"fts": 0, "vector": 0, "hybrid": 0}
        details = []
        try:
            for case in dataset["queries"]:
                relevant = set(case["relevant_ids"])
                lexical = [node_to_doc[node.node_id]
                           for node in dag.search(case["query"], session_id="eval", limit=k)]
                vector_hits, _ = semantic.search(case["query"], session_id="eval", limit=k)
                vector = [node_to_doc[node_id] for node_id, _score in vector_hits]
                hybrid = _rrf(lexical, vector, k)
                row = {"query": case["query"], "relevant": sorted(relevant),
                       "fts": lexical, "vector": vector, "hybrid": hybrid}
                for arm in totals:
                    hit = bool(relevant.intersection(row[arm]))
                    totals[arm] += int(hit)
                    row[f"{arm}_hit"] = hit
                details.append(row)
        finally:
            semantic.close()
    finally:
        dag.close(); store.close()
    count = len(dataset["queries"]) or 1
    return {"model": model, "k": k, "queries": len(dataset["queries"]),
            "recall_at_k": {arm: hits / count for arm, hits in totals.items()},
            "details": details}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, help="FastEmbed model id")
    parser.add_argument("--dataset", type=Path,
                        help="JSON: documents[{id,text}], queries[{query,relevant_ids}]")
    parser.add_argument("-k", type=int, default=3)
    args = parser.parse_args()
    dataset = json.loads(args.dataset.read_text(encoding="utf-8")) if args.dataset else SMOKE
    print(json.dumps(run(dataset, args.model, args.k), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Deterministic synthetic recall@k smoke evaluation for LCM retrieval modes."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import random
import re
import sys
from collections import defaultdict
from pathlib import Path
from types import ModuleType
from typing import Protocol, Sequence


SEED = 386
RRF_K = 60
DIM = 64
ROOT = Path(__file__).resolve().parents[1]
QUERY_FIXTURE = ROOT / "tests" / "fixtures" / "retrieval_recall_queries.json"

TOPICS = {
    "astronomy": ("cobalt telescope", ("starwatcher", "deep-sky lens")),
    "botany": ("amber orchid", ("rare bloom", "glasshouse flower")),
    "cryptography": ("violet cipher", ("secret code", "encrypted phrase")),
    "geology": ("basalt compass", ("volcanic rock", "stone navigator")),
    "logistics": ("harbor manifest", ("cargo ledger", "shipping record")),
    "medicine": ("silver vaccine", ("immune dose", "protective injection")),
    "music": ("cedar concerto", ("orchestral piece", "woodland composition")),
    "oceanography": ("coral current", ("reef flow", "undersea stream")),
    "robotics": ("quartz actuator", ("machine joint", "robot motion unit")),
    "weather": ("indigo cyclone", ("spiraling storm", "tropical wind system")),
}

TOKEN_RE = re.compile(r"[a-z0-9]+")
STOPWORDS = {
    "a", "an", "and", "by", "did", "for", "in", "is", "of", "on", "or",
    "record", "the", "to", "was", "what", "which", "with",
}


class Embedder(Protocol):
    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


def _tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


def _normalize(vector: Sequence[float]) -> list[float]:
    magnitude = math.sqrt(sum(value * value for value in vector))
    return [float(value) / magnitude for value in vector]


def _topic_for(text: str) -> str | None:
    folded = text.lower()
    for topic, (fact, aliases) in TOPICS.items():
        markers = (topic, fact, *aliases)
        if any(marker in folded for marker in markers):
            return topic
    return None


class DeterministicMockEmbedder:
    """Hash-based unit vectors with a deliberately strong topic cluster."""

    def _embed(self, text: str, *, document: bool) -> list[float]:
        vector = [0.0] * DIM
        topic = _topic_for(text)
        if topic is not None:
            topic_index = list(TOPICS).index(topic)
            is_fact = "planted fact" in text.lower()
            vector[topic_index] = 10.0 if (not document or is_fact) else 5.0
        noise_weight = 0.12 if document and "planted fact" in text.lower() else 0.45
        for token in _tokens(text):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = 10 + int.from_bytes(digest[:2], "big") % (DIM - 10)
            sign = 1.0 if digest[2] % 2 == 0 else -1.0
            vector[index] += sign * noise_weight
        if not any(vector):
            vector[-1] = 1.0
        return _normalize(vector)

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(str(text), document=True) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(str(text), document=False)


def build_corpus() -> list[dict[str, str]]:
    rng = random.Random(SEED)
    corpus: list[dict[str, str]] = []
    filler = [
        "routine maintenance note", "weekly planning fragment", "archived status update",
        "ordinary meeting recap", "background reference material",
    ]
    for topic, (fact, _aliases) in TOPICS.items():
        corpus.append({
            "id": f"{topic}-fact",
            "text": (
                f"Planted fact for {topic}: the {fact} uses calibration code "
                f"{topic[:3].upper()}-{len(topic) * 17}."
            ),
        })
        shuffled = list(filler)
        rng.shuffle(shuffled)
        for index, phrase in enumerate(shuffled):
            corpus.append({
                "id": f"{topic}-noise-{index:02d}",
                "text": f"{topic.title()} archive {index}: {phrase}; no planted instrument fact.",
            })
    return corpus


def load_queries() -> list[dict[str, object]]:
    queries = json.loads(QUERY_FIXTURE.read_text(encoding="utf-8"))
    if len(queries) != 30:
        raise ValueError(f"expected 30 stratified queries, found {len(queries)}")
    return queries


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def full_text_rank(query: str, corpus: Sequence[dict[str, str]]) -> list[str]:
    query_terms = {token for token in _tokens(query) if token not in STOPWORDS}
    ranked: list[tuple[int, str]] = []
    for document in corpus:
        document_terms = set(_tokens(document["text"]))
        score = len(query_terms & document_terms)
        if score:
            ranked.append((score, document["id"]))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return [document_id for _score, document_id in ranked]


def semantic_rank(
    query: str,
    corpus: Sequence[dict[str, str]],
    document_vectors: Sequence[Sequence[float]],
    embedder: Embedder,
) -> list[str]:
    query_vector = embedder.embed_query(query)
    ranked = [
        (_dot(query_vector, vector), document["id"])
        for document, vector in zip(corpus, document_vectors)
    ]
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return [document_id for _score, document_id in ranked]


def hybrid_rank(fts: Sequence[str], semantic: Sequence[str]) -> list[str]:
    scores: dict[str, float] = defaultdict(float)
    ranks: dict[str, list[int]] = defaultdict(list)
    for arm in (fts[:50], semantic[:50]):
        for rank, document_id in enumerate(arm, start=1):
            scores[document_id] += 1.0 / (RRF_K + rank)
            ranks[document_id].append(rank)
    return sorted(
        scores,
        key=lambda document_id: (
            -scores[document_id],
            min(ranks[document_id]),
            document_id,
        ),
    )


def evaluate(embedder: Embedder) -> dict[str, dict[str, dict[str, float]]]:
    corpus = build_corpus()
    queries = load_queries()
    document_vectors = embedder.embed_documents([document["text"] for document in corpus])
    values: dict[str, dict[str, dict[str, list[float]]]] = {
        mode: {
            stratum: {"recall@5": [], "recall@10": []}
            for stratum in ("exact-term", "paraphrase", "multi-hop-ish")
        }
        for mode in ("full_text", "semantic", "hybrid")
    }

    for item in queries:
        query = str(item["query"])
        stratum = str(item["stratum"])
        relevant = {str(value) for value in item["relevant_doc_ids"]}
        fts = full_text_rank(query, corpus)
        semantic = semantic_rank(query, corpus, document_vectors, embedder)
        rankings = {
            "full_text": fts,
            "semantic": semantic,
            "hybrid": hybrid_rank(fts, semantic),
        }
        for mode, ranking in rankings.items():
            for k in (5, 10):
                recalled = len(relevant & set(ranking[:k])) / len(relevant)
                values[mode][stratum][f"recall@{k}"].append(recalled)

    return {
        mode: {
            stratum: {
                metric: round(sum(samples) / len(samples), 6)
                for metric, samples in metrics.items()
            }
            for stratum, metrics in strata.items()
        }
        for mode, strata in values.items()
    }


def _load_live_provider() -> Embedder:
    if os.environ.get("LCM_RECALL_EVAL_ALLOW_LIVE_PROVIDER") != "1":
        raise RuntimeError(
            "live-provider evaluation requires LCM_RECALL_EVAL_ALLOW_LIVE_PROVIDER=1"
        )
    package_name = "hermes_lcm"
    if package_name not in sys.modules:
        package = ModuleType(package_name)
        package.__path__ = [str(ROOT)]
        package.__package__ = package_name
        sys.modules[package_name] = package
    for module_name in ("config", "message_content", "tokens", "embedding_provider"):
        qualified = f"{package_name}.{module_name}"
        if qualified in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(qualified, ROOT / f"{module_name}.py")
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load {module_name}")
        module = importlib.util.module_from_spec(spec)
        module.__package__ = package_name
        sys.modules[qualified] = module
        spec.loader.exec_module(module)
    config_module = sys.modules[f"{package_name}.config"]
    provider_module = sys.modules[f"{package_name}.embedding_provider"]
    config = config_module.LCMConfig.from_env()
    provider = provider_module.resolve_provider(config)
    if provider is None:
        raise RuntimeError("LCM_EMBEDDING_PROVIDER and LCM_EMBEDDING_MODEL are required")
    return provider


def _markdown(metrics: dict[str, dict[str, dict[str, float]]]) -> str:
    lines = [
        "| Mode | Stratum | Recall@5 | Recall@10 |",
        "|---|---|---:|---:|",
    ]
    for mode in ("full_text", "semantic", "hybrid"):
        for stratum in ("exact-term", "paraphrase", "multi-hop-ish"):
            row = metrics[mode][stratum]
            lines.append(
                f"| {mode} | {stratum} | {row['recall@5']:.3f} | {row['recall@10']:.3f} |"
            )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit stable JSON instead of markdown")
    parser.add_argument(
        "--live-provider",
        action="store_true",
        help="use the env-configured provider (requires explicit network/spend gate)",
    )
    args = parser.parse_args()
    try:
        embedder = _load_live_provider() if args.live_provider else DeterministicMockEmbedder()
    except RuntimeError as exc:
        parser.error(str(exc))
    metrics = evaluate(embedder)
    if args.json:
        print(json.dumps(metrics, sort_keys=True, separators=(",", ":")))
    else:
        print(_markdown(metrics))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

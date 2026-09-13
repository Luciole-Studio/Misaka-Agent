"""Offline LongMemEval retrieval-quality harness for hermes-lcm.

This module ingests each LongMemEval question's conversation history into a
fresh temporary LCM store (reusing ``store``/``dag``/``vector_store`` APIs
directly, with no live Hermes host), builds one deterministic summary per
session, optionally backfills summary embeddings, then scores each retrieval
arm against the dataset's labeled evidence sessions.

It is retrieval-only: LongMemEval labels the evidence session(s) per question,
so recall@k / NDCG@k are computable offline without an LLM judge.

Dataset: LongMemEval_S (Wu et al., ICLR 2025), canonical Hugging Face dataset
``xiaowu0162/longmemeval``, file ``longmemeval_s``, pinned to a fixed revision
(see :data:`DATASET_REPO_ID` / :data:`DATASET_REVISION`). The dataset is
downloaded once by an explicit operator command and never during a run.

Export hygiene mirrors ``scripts/lcm_benchmark.py``: output is aggregate-only.
It contains no transcript content, session ids, or local paths.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Sequence

from .standalone import ensure_agent_context_engine_importable

_REPO_ROOT = Path(__file__).resolve().parents[1]

BENCHMARK_VERSION = 1
SCHEMA_VERSION = 1
RRF_K = 60

# F7: embed a question's session summaries in one batched ``embed_documents`` call
# instead of one call per session. Sub-batching only guards against a pathologically
# large haystack tripping the provider's per-call deadline; for a typical LongMemEval
# question (tens of sessions) this collapses to the single call the F7 item asks for.
EMBED_BATCH_SIZE = 64

# Canonical LongMemEval dataset coordinates. The revision is PINNED so a run is
# reproducible: `longmemeval_s` has been byte-stable since it was introduced;
# this is the current `main` commit at implementation time (2026-07-17).
DATASET_REPO_ID = "xiaowu0162/longmemeval"
DATASET_REVISION = "2ec2a557f339b6c0369619b1ed5793734cc87533"
DATASET_FILENAME = "longmemeval_s"

PROVIDERS = ("stub", "fastembed", "voyage", "ollama")
# ``chunk_vectors`` scores the raw-chunk KNN corpus; ``hybrid_rrf3`` fuses it as a
# third arm alongside FTS + summary vectors. ``lcm_recall`` scores the ACTUAL
# production tool (weighted RRF + scope/recency prior + chunk-vs-FTS dedup), not a
# harness reimplementation. All are appended so the earlier arms keep byte-identical
# outputs and report ordering.
ARMS = (
    "fts",
    "summary_vectors",
    "hybrid_rrf",
    "hybrid_rerank",
    "chunk_vectors",
    "hybrid_rrf3",
    "lcm_recall",
)

# LongMemEval `question_type` -> reported category label. Abstention questions
# (``question_id`` ends with ``_abs``) are excluded from recall scoring and
# reported separately as an ``abstention`` count.
CATEGORY_LABELS = {
    "single-session-user": "single-session-user",
    "single-session-assistant": "single-session-assistant",
    "single-session-preference": "single-session-preference",
    "multi-session": "multi-session",
    "temporal-reasoning": "temporal",
    "knowledge-update": "knowledge-update",
}

_WHITESPACE_RE = re.compile(r"\s+")
_STUB_MODEL = "stub-hash-64"
_STUB_DIM = 64


def _ensure_hermes_lcm_package() -> None:
    """Make this source checkout importable as ``hermes_lcm`` (no plugin registration)."""
    ensure_agent_context_engine_importable()
    if "hermes_lcm" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "hermes_lcm",
        _REPO_ROOT / "__init__.py",
        submodule_search_locations=[str(_REPO_ROOT)],
    )
    if spec is None:
        raise RuntimeError("could not create hermes_lcm package spec")
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(_REPO_ROOT)]
    module.__package__ = "hermes_lcm"
    sys.modules["hermes_lcm"] = module


# --------------------------------------------------------------------------- #
# Deterministic stub embedder (pure plumbing; scores are meaningless with it).
# --------------------------------------------------------------------------- #


class StubEmbedder:
    """Hash-based unit vectors for offline plumbing tests. No provider calls."""

    provider_id = "stub"

    def __init__(self, dim: int = _STUB_DIM):
        self.model_id = _STUB_MODEL
        self.dim = int(dim)

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for token in _WHITESPACE_RE.sub(" ", str(text).lower()).split():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:2], "big") % self.dim
            sign = 1.0 if digest[2] % 2 == 0 else -1.0
            vector[index] += sign
        magnitude = math.sqrt(sum(value * value for value in vector))
        if magnitude == 0.0:
            vector[-1] = 1.0
            return vector
        return [value / magnitude for value in vector]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


def _fastembed_cache_dir() -> str | None:
    """Cache dir for FastEmbed models, honoring an env override.

    The provider default is ``~/.cache/fastembed``; ``LCM_LONGMEMEVAL_FASTEMBED_CACHE``
    (or ``FASTEMBED_CACHE_PATH``) redirects it, e.g. to a roomy volume.
    """
    import os

    override = os.environ.get("LCM_LONGMEMEVAL_FASTEMBED_CACHE") or os.environ.get(
        "FASTEMBED_CACHE_PATH"
    )
    return override or None


def resolve_harness_provider(provider: str, model: str, *, timeout: float = 300.0):
    """Return a WARMED embedder for ``provider``. ``stub`` stays fully offline.

    Non-stub providers are warmed up once here so ``.dim`` is populated (FastEmbed
    reports dim only after the first embed) and any model download happens before
    the scoring loop rather than inside a per-question deadline.
    """
    if provider == "stub":
        return StubEmbedder()
    _ensure_hermes_lcm_package()
    if not model:
        raise ValueError(f"--model is required for --provider {provider}")
    if provider in {"fastembed", "fast-embed"}:
        from hermes_lcm.embedding_provider import EmbeddingSpendGuard, FastembedProvider

        # max_calls=0 disables the per-minute call-rate guard, matching the
        # bulk-backfill contract (resolve_provider(for_backfill=True)); the
        # harness embeds thousands of summaries in one pass.
        embedder = FastembedProvider(
            model,
            cache_dir=_fastembed_cache_dir(),
            timeout=timeout,
            spend_guard=EmbeddingSpendGuard(max_calls=0),
        )
        embedder.warmup()
        return embedder
    from hermes_lcm.config import LCMConfig
    from hermes_lcm.embedding_provider import resolve_provider

    config = LCMConfig(
        embedding_provider=provider,
        embedding_model=model,
        embedding_backfill_timeout_s=timeout,
    )
    resolved = resolve_provider(config, for_backfill=True)
    if resolved is None:
        raise ValueError(f"could not resolve embedding provider {provider!r}")
    if int(getattr(resolved, "dim", 0)) == 0:
        resolved.embed_query("warmup")
    return resolved


# --------------------------------------------------------------------------- #
# Dataset loading + question shape.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Question:
    """A single LongMemEval question with its haystack and labeled evidence."""

    question_id: str
    question_type: str
    question: str
    haystack_session_ids: list[str]
    haystack_sessions: list[list[dict[str, Any]]]
    answer_session_ids: list[str]

    @property
    def is_abstention(self) -> bool:
        return self.question_id.endswith("_abs")

    @property
    def category(self) -> str:
        return CATEGORY_LABELS.get(self.question_type, self.question_type)


def parse_question(raw: dict[str, Any]) -> Question:
    return Question(
        question_id=str(raw["question_id"]),
        question_type=str(raw["question_type"]),
        question=str(raw["question"]),
        haystack_session_ids=[str(s) for s in raw.get("haystack_session_ids", [])],
        haystack_sessions=list(raw.get("haystack_sessions", [])),
        answer_session_ids=[str(s) for s in raw.get("answer_session_ids", [])],
    )


def load_questions(path: str | Path, *, limit: int | None = None) -> list[Question]:
    """Load LongMemEval questions from the downloaded JSON file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("LongMemEval dataset must be a JSON array of questions")
    questions = [parse_question(row) for row in data]
    if limit is not None:
        if limit <= 0:
            raise ValueError("--limit must be a positive integer")
        questions = questions[:limit]
    return questions


def evidence_sessions(question: Question) -> set[str]:
    """Session-level evidence set (empty for abstention questions)."""
    if question.is_abstention:
        return set()
    return set(question.answer_session_ids)


def evidence_turns(question: Question) -> set[tuple[str, int]]:
    """Turn-level evidence: ``(session_id, turn_index)`` where ``has_answer``."""
    turns: set[tuple[str, int]] = set()
    if question.is_abstention:
        return turns
    for session_id, session in zip(question.haystack_session_ids, question.haystack_sessions):
        for index, turn in enumerate(session):
            if isinstance(turn, dict) and turn.get("has_answer"):
                turns.add((str(session_id), index))
    return turns


# --------------------------------------------------------------------------- #
# Deterministic summarization stub (no LLM, content-derived, offline).
# --------------------------------------------------------------------------- #


def deterministic_session_summary(turns: Sequence[dict[str, Any]], *, max_chars: int = 1200) -> str:
    """Condense a session's turns into a deterministic, content-bearing summary.

    Lexical content is preserved (collapsed whitespace, truncated) so the FTS
    and embedding arms both see meaningful text. No provider is consulted.
    """
    parts: list[str] = []
    for turn in turns:
        role = str(turn.get("role", "unknown")) if isinstance(turn, dict) else "unknown"
        content = turn.get("content", "") if isinstance(turn, dict) else str(turn)
        parts.append(f"{role}: {content}")
    condensed = _WHITESPACE_RE.sub(" ", " ".join(parts)).strip()
    if not condensed:
        # LongMemEval_S contains empty haystack sessions; cloud embedding
        # endpoints (voyage) reject empty inputs with HTTP 400, so an empty
        # session gets a deterministic non-empty placeholder instead.
        return "(empty session)"
    return condensed[:max_chars]


# --------------------------------------------------------------------------- #
# Metric math (pure, testable).
# --------------------------------------------------------------------------- #


def recall_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """Set recall@k: fraction of relevant items present in the top-k retrieved."""
    relevant_set = set(relevant)
    if not relevant_set:
        return 0.0
    top_k = list(dict.fromkeys(retrieved))[:k]
    hits = sum(1 for item in top_k if item in relevant_set)
    return hits / len(relevant_set)


def ndcg_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """Binary-relevance NDCG@k over a deduplicated ranked list."""
    relevant_set = set(relevant)
    if not relevant_set:
        return 0.0
    top_k = list(dict.fromkeys(retrieved))[:k]
    dcg = 0.0
    for rank, item in enumerate(top_k, start=1):
        if item in relevant_set:
            dcg += 1.0 / math.log2(rank + 1)
    ideal_hits = min(len(relevant_set), k)
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    if idcg == 0.0:
        return 0.0
    return dcg / idcg


# Turn-level relevance is coverage-based: each retrieved item covers a
# ``(session, turn_index)`` range, and a hit is an item whose range intersects the
# labeled evidence turns. Precise items (raw-message FTS rows, raw chunks) cover a
# single turn ``(session, index)``; a summary item cannot localize a turn, so it is
# a session-granularity marker ``(session, None)`` that covers *every* evidence turn
# of its session at once. Callers surface that coarseness with an asterisk in output.
TurnKey = tuple[str, "int | None"]


def _evidence_turns_by_session(evidence_turns: Iterable[TurnKey]) -> dict[str, set[TurnKey]]:
    by_session: dict[str, set[TurnKey]] = {}
    for key in evidence_turns:
        by_session.setdefault(key[0], set()).add(key)
    return by_session


def turn_recall_at_k(turn_keys: Sequence[TurnKey], relevant: Iterable[TurnKey], k: int) -> float:
    """Coverage recall@k over turn keys.

    Top-k is a budget of ranked *items*. A precise ``(session, index)`` item covers
    itself; a ``(session, None)`` summary marker covers all of that session's
    evidence turns (session granularity). Returns covered-evidence / total-evidence.
    """
    relevant_set = set(relevant)
    if not relevant_set:
        return 0.0
    by_session = _evidence_turns_by_session(relevant_set)
    covered: set[TurnKey] = set()
    seen = 0
    for key in dict.fromkeys(turn_keys):
        if seen >= k:
            break
        seen += 1
        session, index = key
        if index is None:
            covered |= by_session.get(session, set())
        elif key in relevant_set:
            covered.add(key)
    return len(covered) / len(relevant_set)


def turn_ndcg_at_k(turn_keys: Sequence[TurnKey], relevant: Iterable[TurnKey], k: int) -> float:
    """Binary-relevance NDCG@k over a deduplicated ranked list of turn keys.

    An item is relevant if it is a labeled evidence turn, or a summary marker for a
    session that contains any evidence turn. IDCG assumes the relevant items in the
    list are ranked first.
    """
    relevant_set = set(relevant)
    if not relevant_set:
        return 0.0
    sessions_with_evidence = {key[0] for key in relevant_set}

    def is_relevant(key: TurnKey) -> bool:
        session, index = key
        if index is None:
            return session in sessions_with_evidence
        return key in relevant_set

    deduped = list(dict.fromkeys(turn_keys))
    top_k = deduped[:k]
    dcg = sum(1.0 / math.log2(rank + 1) for rank, key in enumerate(top_k, start=1) if is_relevant(key))
    ideal_hits = min(sum(1 for key in deduped if is_relevant(key)), k)
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    if idcg == 0.0:
        return 0.0
    return dcg / idcg


def percentiles(values: Sequence[float], points: Sequence[int] = (50, 90, 99)) -> dict[str, float]:
    """Nearest-rank percentiles for latency reporting."""
    ordered = sorted(values)
    result: dict[str, float] = {}
    for point in points:
        if not ordered:
            result[f"p{point}"] = 0.0
            continue
        rank = max(1, math.ceil(point / 100 * len(ordered)))
        result[f"p{point}"] = round(ordered[min(rank, len(ordered)) - 1], 3)
    return result


# --------------------------------------------------------------------------- #
# Retrieval arms.
# --------------------------------------------------------------------------- #


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def _unit(values: Sequence[float]) -> list[float]:
    magnitude = math.sqrt(sum(value * value for value in values))
    if magnitude == 0.0:
        return list(values)
    return [value / magnitude for value in values]


def _dedup_sessions(session_ids: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(session_ids))


# A LongMemEval question is a natural-language sentence; SQLite FTS5 MATCH ANDs
# its tokens, so a single non-matching word (e.g. "what") zeroes the arm. A
# standard lexical retrieval arm ORs the salient query terms (BM25-style), so we
# build the MATCH query from the repo's own term extractor minus light stopwords.
_FTS_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "did", "do", "does",
        "for", "from", "had", "has", "have", "how", "i", "in", "is", "it", "me",
        "my", "of", "on", "or", "that", "the", "to", "was", "were", "what",
        "when", "where", "which", "who", "why", "with", "you", "your",
    }
)


def build_fts_query(question: str) -> str:
    """Build an OR-of-terms FTS5 MATCH query from a natural-language question.

    Each term is reduced to a bareword (alphanumerics/underscore only) so no FTS5
    operator character survives to trip a syntax error and force the LIKE
    fallback; empty and stopword tokens are dropped.
    """
    _ensure_hermes_lcm_package()
    from hermes_lcm.search_query import extract_search_terms

    barewords: list[str] = []
    for term in extract_search_terms(question):
        cleaned = re.sub(r"\W+", "", term, flags=re.UNICODE)
        if cleaned and cleaned.lower() not in _FTS_STOPWORDS:
            barewords.append(cleaned)
    return " OR ".join(dict.fromkeys(barewords))


def fts_hits(store, query: str, fetch: int) -> list[tuple[str, int]]:
    """Raw FTS arm: ranked ``(session_id, store_id)`` message hits (no dedup).

    The ``store_id`` localizes each hit to a single turn for turn-level scoring;
    :func:`fts_sessions` collapses it to the session ranking.
    """
    match_query = build_fts_query(query)
    if not match_query:
        return []
    # build_fts_query composes FTS5 syntax on purpose (barewords joined by OR),
    # so it opts in to operator handling. Raw prose never does.
    rows = store.search(
        match_query, session_id=None, limit=fetch, allow_operators=True
    )
    hits: list[tuple[str, int]] = []
    for row in rows:
        store_id = row.get("store_id")
        if store_id is None:
            continue
        hits.append((str(row.get("session_id", "")), int(store_id)))
    return hits


def fts_sessions(store, query: str, fetch: int) -> list[str]:
    """Rank evidence sessions by the raw-message FTS arm (``store.search``)."""
    return _dedup_sessions(session for session, _ in fts_hits(store, query, fetch))


def vector_sessions(vector_store, dag, query_vec, model, provider, fetch: int) -> list[str]:
    """Rank evidence sessions by the summary-vector arm (``vector_store.knn``)."""
    result = vector_store.knn(query_vec, k=fetch, model=model, provider=provider)
    sessions: list[str] = []
    for embedded_id, _score, _kind in result:
        node = dag.get_node(int(embedded_id))
        if node is not None:
            sessions.append(str(node.session_id))
    return _dedup_sessions(sessions)


def _chunk_store_id(chunk_id) -> int | None:
    """Extract the source ``store_id`` from a ``store_id:chunk_index`` chunk id."""
    try:
        return int(str(chunk_id).split(":", 1)[0])
    except (TypeError, ValueError):
        return None


def chunk_hits(vector_store, query_vec, model, provider, fetch: int) -> list:
    """Raw chunk arm: the ranked ``knn_chunks`` result (``store_id:chunk_index`` ids)."""
    return list(vector_store.knn_chunks(query_vec, k=fetch, model=model, provider=provider))


def _map_chunk_sessions(result, store_id_to_session: dict[int, str]) -> list[str]:
    sessions: list[str] = []
    for chunk_id, _score, _kind in result:
        store_id = _chunk_store_id(chunk_id)
        session_id = None if store_id is None else store_id_to_session.get(store_id)
        if session_id is not None:
            sessions.append(str(session_id))
    return _dedup_sessions(sessions)


def chunk_sessions(
    vector_store, query_vec, model, provider, fetch: int,
    store_id_to_session: dict[int, str],
) -> list[str]:
    """Rank evidence sessions by the raw-chunk KNN arm (``knn_chunks``).

    Each chunk id is ``store_id:chunk_index``; its store_id maps back to the
    session that owns the source message, so a chunk hit votes for its session.
    """
    result = chunk_hits(vector_store, query_vec, model, provider, fetch)
    return _map_chunk_sessions(result, store_id_to_session)


# --------------------------------------------------------------------------- #
# Turn-key projections (parallel to the session rankings above).
# --------------------------------------------------------------------------- #


def fts_turn_keys(hits: Sequence[tuple[str, int]], store_id_to_turn: dict[int, TurnKey]) -> list[TurnKey]:
    """Project raw FTS message hits to precise ``(session, turn_index)`` keys."""
    keys: list[TurnKey] = []
    for _session, store_id in hits:
        key = store_id_to_turn.get(int(store_id))
        if key is not None:
            keys.append(key)
    return keys


def chunk_turn_keys(result, store_id_to_turn: dict[int, TurnKey]) -> list[TurnKey]:
    """Project raw chunk hits to precise ``(session, turn_index)`` keys via store_id."""
    keys: list[TurnKey] = []
    for chunk_id, _score, _kind in result:
        store_id = _chunk_store_id(chunk_id)
        key = None if store_id is None else store_id_to_turn.get(store_id)
        if key is not None:
            keys.append(key)
    return keys


def summary_turn_keys(session_ranked: Sequence[str]) -> list[TurnKey]:
    """A summary covers a whole session, so it localizes only to ``(session, None)``."""
    return [(session, None) for session in session_ranked]


# --------------------------------------------------------------------------- #
# Production ``lcm_recall`` arm — the tool users actually call.
# --------------------------------------------------------------------------- #

# A synthetic current-session id for the probe engine. lcm_recall applies a scope
# prior that BOOSTS hits belonging to the current conversation; benchmarking the
# production path honestly means the probe conversation must sit OUTSIDE the
# dataset's sessions so no evidence session is silently lifted by conversation
# membership. (The recency prior still applies to every hit — that is the honest
# production behavior, noted in benchmarks/README.md.)
_LCM_RECALL_FRESH_SESSION = "__lcm_recall_fresh_probe__"


def fresh_recall_session_id(question: "Question") -> str:
    """A current-session id guaranteed absent from ``question``'s haystack.

    Deterministic (sentinel + question id) so runs are reproducible, and disjoint
    from the haystack so the scope prior never boosts a dataset session.
    """
    existing = set(question.haystack_session_ids)
    candidate = f"{_LCM_RECALL_FRESH_SESSION}{question.question_id}"
    suffix = 0
    while candidate in existing:
        suffix += 1
        candidate = f"{_LCM_RECALL_FRESH_SESSION}{question.question_id}-{suffix}"
    return candidate


def recall_hit_sessions(hits: Sequence[dict[str, Any]]) -> list[str]:
    """Dedup production recall hits to a session ranking (hit order preserved)."""
    return _dedup_sessions(
        str(hit.get("session_id")) for hit in hits if hit.get("session_id")
    )


def recall_hit_turn_keys(
    hits: Sequence[dict[str, Any]], store_id_to_turn: dict[int, TurnKey]
) -> list[TurnKey]:
    """Project production recall hits to turn keys.

    A verbatim hit (``kind`` != ``summary``) carries a ``store_id`` that localizes
    it to one ``(session, turn_index)`` via the ingest map; a summary hit covers a
    whole session, so it becomes a ``(session, None)`` marker — the
    session-granularity credit the ``*`` asterisk warns about.
    """
    keys: list[TurnKey] = []
    for hit in hits:
        session_id = hit.get("session_id")
        if hit.get("kind") == "summary":
            if session_id:
                keys.append((str(session_id), None))
            continue
        store_id = hit.get("store_id")
        if store_id is None:
            continue
        key = store_id_to_turn.get(int(store_id))
        if key is not None:
            keys.append(key)
    return keys


def production_recall_hits(
    question: "Question",
    config,
    store,
    dag,
    provider_embedder,
    *,
    provider_name: str,
    tmp_dir: Path,
    embeddings_enabled: bool,
    limit: int,
) -> list[dict[str, Any]]:
    """Invoke the REAL ``tools.lcm_recall`` against this question's temp store.

    This is the tool users call: weighted RRF over the FTS + summary + chunk arms
    (``retrieval_core.rrf_fuse`` with ``LCM_RECALL_ARM_WEIGHTS``), the scope/recency
    prior, and chunk-vs-FTS dedup by ``store_id`` — none of which the per-arm harness
    measurements exercise. The engine is the proven smoke-test stand-in (a
    ``SimpleNamespace`` exposing the already-open store/dag/config with a fresh
    current-session id). The warmed harness embedder is injected through
    ``lcm_recall``'s provider cache so no second model load or network call occurs;
    ``lcm_recall`` clamps ``limit`` to its own production ceiling.
    """
    _ensure_hermes_lcm_package()
    import hermes_lcm.tools as lcm_tools

    engine = SimpleNamespace(
        _config=config,
        _store=store,
        _dag=dag,
        _hermes_home=str(tmp_dir),
        current_session_id=fresh_recall_session_id(question),
    )
    if embeddings_enabled:
        cache_key = (
            str(provider_name).strip().lower(),
            str(provider_embedder.model_id).strip(),
        )
        engine._lcm_embedding_provider_cache = (cache_key, provider_embedder)
    payload = json.loads(
        lcm_tools.lcm_recall({"query": question.question, "limit": limit}, engine=engine)
    )
    return list(payload.get("hits", []))


def _fuse_tiebreak(item):
    """Total-order tie-break for fused ids: plain session strings sort as-is;
    turn keys ``(session, turn|None)`` map a None turn (summary = whole
    session) to -1 so ties never compare ``None < int`` (crash observed when
    a summary turn key tied a localized turn key on score AND best rank)."""
    if isinstance(item, tuple):
        return tuple(-1 if part is None else part for part in item)
    return item


def rrf_fuse(*ranked_lists: Sequence[str]) -> list[str]:
    """Reciprocal-rank fusion over per-arm session rankings (``RRF_K`` = 60)."""
    scores: dict[str, float] = {}
    best_rank: dict[str, int] = {}
    for ranked in ranked_lists:
        for rank, session_id in enumerate(ranked, start=1):
            scores[session_id] = scores.get(session_id, 0.0) + 1.0 / (RRF_K + rank)
            best_rank[session_id] = min(best_rank.get(session_id, rank), rank)
    return sorted(scores, key=lambda sid: (-scores[sid], best_rank[sid], _fuse_tiebreak(sid)))


def rerank_by_cosine(
    sessions: Sequence[str], query_vec, session_vectors: dict[str, list[float]]
) -> list[str]:
    """Rerank fused candidates by cosine to the query embedding.

    This is a deterministic embedding-cosine reranker over the fused candidate
    pool, a placeholder for a real cross-encoder. Per the MemDelta caveat we
    only ever compare it against *this* configuration, never as a universal
    verdict.
    """
    normalized_query = _unit(list(query_vec))

    def score(session_id: str) -> float:
        vector = session_vectors.get(session_id)
        if vector is None:
            return -math.inf
        return _dot(normalized_query, vector)

    return sorted(sessions, key=lambda sid: (-score(sid), sid))


# The real rerank arm reranks a bounded candidate window (top fused sessions) in a
# single cross-encoder call under an absolute per-question budget; the rest of the
# fused ranking is appended unchanged. These bound live-provider cost/latency.
RERANK_CANDIDATE_WINDOW = 20
RERANK_TIMEOUT_S = 10.0
RERANK_MODE_PLACEHOLDER = "placeholder-cosine"
RERANK_MODE_VOYAGE = "voyage:rerank-2.5-lite"
# Run-level label when some (but not all) questions used the real reranker while
# others silently fell back -- the run is neither a clean real nor placeholder run.
RERANK_MODE_MIXED = "mixed"


def rerank_sessions_voyage(
    reranker,
    query: str,
    sessions: Sequence[str],
    session_summaries: dict[str, str],
    *,
    window: int = RERANK_CANDIDATE_WINDOW,
    timeout: float = RERANK_TIMEOUT_S,
) -> list[str] | None:
    """Rerank the top-``window`` fused sessions with a real cross-encoder.

    Uses ``VoyageProvider.rerank`` (rerank-2.5-lite) over each candidate session's
    deterministic summary in one API call. Returns the reordered candidate window
    followed by the untouched fused tail, or ``None`` to signal the caller should
    fall back to the deterministic placeholder (empty window, any provider error,
    or an empty/degenerate response that does not cover every candidate).
    """
    candidates = list(sessions[:window])
    if not candidates:
        return None
    documents = [session_summaries.get(session, "") for session in candidates]
    try:
        ranked = reranker.rerank(query, documents, top_k=len(documents), timeout=timeout)
    except Exception:
        return None
    # A non-exception but empty/degenerate response (e.g. ``data: []`` or scores
    # covering only some candidates) is NOT a trustworthy real rerank -- treat it
    # exactly like a provider error so it is labeled a placeholder fallback rather
    # than silently counted as real voyage rerank.
    covered = {index for index, _score in ranked if 0 <= index < len(candidates)}
    if len(covered) != len(candidates):
        return None
    reordered = [candidates[index] for index, _score in ranked if 0 <= index < len(candidates)]
    seen = set(reordered)
    for session in sessions:
        if session not in seen:
            reordered.append(session)
            seen.add(session)
    return reordered


# --------------------------------------------------------------------------- #
# Per-question ingest + evaluation.
# --------------------------------------------------------------------------- #


@dataclass
class ArmSamples:
    recalls: dict[int, list[float]] = field(default_factory=lambda: {1: [], 5: [], 10: []})
    ndcg10: list[float] = field(default_factory=list)
    latency_ms: list[float] = field(default_factory=list)
    turn_recalls: dict[int, list[float]] = field(default_factory=lambda: {1: [], 5: [], 10: []})
    turn_ndcg10: list[float] = field(default_factory=list)
    # True when the arm's turn ranking includes summary (session-granularity) items,
    # so its turn-level numbers carry the coarse-localization asterisk.
    session_granularity: bool = False


def _new_arm_samples() -> dict[str, ArmSamples]:
    return {arm: ArmSamples() for arm in ARMS}


def _bootstrap_db_template(template_path: Path, config) -> None:
    """Create one fully-migrated empty LCM DB to clone per question.

    Opening ``MessageStore``/``SummaryDAG``/``VectorStore`` runs the schema
    bootstrap + FTS/migration DDL once; each subsequent question copies this file
    (idempotent re-open, no migrations) instead of paying that cost 500x.
    """
    _ensure_hermes_lcm_package()
    from hermes_lcm.dag import SummaryDAG
    from hermes_lcm.store import MessageStore
    from hermes_lcm.vector_store import VectorStore

    store = MessageStore(str(template_path), ingest_protection_config=config)
    dag = SummaryDAG(str(template_path))
    vector_store = VectorStore(str(template_path), config=config)
    vector_store.close()
    dag.close()
    store.close()


def evaluate_question(
    question: Question,
    provider_embedder,
    *,
    provider_name: str,
    tmp_dir: Path,
    embeddings_enabled: bool,
    top_k: int = 10,
    use_rerank: bool = False,
    db_template: Path | None = None,
) -> dict[str, Any]:
    """Ingest one question into a fresh store and score every retrieval arm.

    Each arm reports session-level ``recall@1/5/10`` + ``ndcg@10`` + ``latency_ms``
    and a nested ``turn`` block with the same recall/NDCG at turn granularity plus a
    ``session_granularity`` flag. ``ingest_ms`` (per-question ingest wall time) and,
    for ``hybrid_rerank``, ``rerank_mode`` ride alongside for aggregation.
    """
    _ensure_hermes_lcm_package()
    from hermes_lcm.chunking import iter_message_chunks
    from hermes_lcm.config import LCMConfig
    from hermes_lcm.dag import SummaryDAG, SummaryNode
    from hermes_lcm.store import MessageStore
    from hermes_lcm.vector_store import EmbeddingIdentity, VectorStore

    db_path = tmp_dir / f"{_safe(question.question_id)}.db"
    model = provider_embedder.model_id
    dim = int(provider_embedder.dim)
    config = LCMConfig(
        database_path=str(db_path),
        embeddings_enabled=embeddings_enabled,
        embedding_provider=provider_name,
        embedding_model=model,
    )
    ingest_start = time.perf_counter()
    # F7: clone a pre-migrated template instead of re-running schema bootstrap.
    if db_template is not None and db_template.is_file():
        shutil.copyfile(db_template, db_path)
    store = MessageStore(str(db_path), ingest_protection_config=config)
    dag = SummaryDAG(str(db_path))
    vector_store = VectorStore(str(db_path), config=config)
    session_vectors: dict[str, list[float]] = {}
    session_summaries: dict[str, str] = {}
    # store_id -> owning session (chunk vote) and -> (session, turn) (turn scoring).
    store_id_to_session: dict[int, str] = {}
    store_id_to_turn: dict[int, TurnKey] = {}
    chunk_identity = None
    # F7: collect every session summary, then embed the whole corpus in one batched
    # ``embed_documents`` call instead of one call per session. (Raw chunks stay
    # per-item: for local ONNX providers, batching pads every text to the batch's
    # longest, so per-chunk embedding is actually faster; the summary-call collapse
    # is the win that matters for network/live providers.)
    summary_specs: list[tuple[str, int, str]] = []  # (session_id, node_id, summary_text)
    try:
        if embeddings_enabled:
            vector_store.register_profile(model, provider_name, dim)
            identity = vector_store.capture_identity(model, provider=provider_name)
            # The raw-chunk corpus is a distinct task='chunk' profile/identity.
            vector_store.register_profile(model, provider_name, dim, task="chunk")
            chunk_identity = EmbeddingIdentity.canonical(
                provider_name, model, "", dim, "float32", "little", "chunk"
            )

        for order, (session_id, session) in enumerate(
            zip(question.haystack_session_ids, question.haystack_sessions), start=1
        ):
            messages = [
                {
                    "role": str(turn.get("role", "user")) if isinstance(turn, dict) else "user",
                    "content": turn.get("content", "") if isinstance(turn, dict) else str(turn),
                }
                for turn in session
            ]
            if messages:
                store_ids = store.append_batch(
                    session_id, messages, source="benchmark", conversation_id=session_id
                )
                # Message i is turn index i (1:1 with the haystack turns), so its
                # store_id resolves to (session, turn) for turn-level scoring.
                for turn_index, store_id in enumerate(store_ids):
                    store_id_to_turn[int(store_id)] = (session_id, turn_index)
                if embeddings_enabled and chunk_identity is not None:
                    for store_id in store_ids:
                        store_id_to_session[int(store_id)] = session_id
                    rows = [
                        {"store_id": sid, "role": m["role"], "content": m["content"]}
                        for sid, m in zip(store_ids, messages)
                    ]
                    for chunk in iter_message_chunks(rows, policy="conversational"):
                        chunk_vector = provider_embedder.embed_documents([chunk.text])[0]
                        vector_store.record_chunk_embedding(
                            chunk.chunk_id, model, chunk_vector,
                            store_id=chunk.store_id, chunk_index=chunk.chunk_index,
                            char_start=chunk.char_start, char_end=chunk.char_end,
                            token_estimate=chunk.token_estimate, identity=chunk_identity,
                        )
            summary_text = deterministic_session_summary(session)
            session_summaries[session_id] = summary_text
            node_id = dag.add_node(
                SummaryNode(
                    session_id=session_id,
                    depth=0,
                    summary=summary_text,
                    token_count=len(summary_text.split()),
                    source_token_count=sum(len(m["content"].split()) for m in messages),
                    source_type="messages",
                    created_at=float(order),
                )
            )
            summary_specs.append((session_id, node_id, summary_text))

        if embeddings_enabled and summary_specs:
            summary_vectors = _embed_in_batches(
                provider_embedder, [text for _session, _node, text in summary_specs]
            )
            for (session_id, node_id, _text), vector in zip(summary_specs, summary_vectors):
                vector_store.record_embedding(
                    str(node_id), "summary", model, vector, identity=identity
                )
                session_vectors[session_id] = _unit(list(vector))
        ingest_ms = (time.perf_counter() - ingest_start) * 1000.0

        relevant = evidence_sessions(question)
        relevant_turns = evidence_turns(question)
        query_vec = provider_embedder.embed_query(question.question) if embeddings_enabled else None
        fetch = max(top_k * 5, 50)

        # Session rankings + parallel turn-key projections for every arm.
        fts_raw, fts_ms = _timed(lambda: fts_hits(store, question.question, fetch))
        fts_ranked = _dedup_sessions(session for session, _ in fts_raw)
        fts_turns = fts_turn_keys(fts_raw, store_id_to_turn)

        if embeddings_enabled:
            vector_ranked, vector_ms = _timed(
                lambda: vector_sessions(vector_store, dag, query_vec, model, provider_name, fetch)
            )
        else:
            vector_ranked, vector_ms = [], 0.0
        summary_turns = summary_turn_keys(vector_ranked)

        hybrid_ranked, hybrid_ms = _timed(lambda: rrf_fuse(fts_ranked, vector_ranked))
        # C6: a hybrid arm's turn keys are session-granularity markers projected from
        # its fused SESSION ranking, NOT an RRF over the raw per-arm turn-key lists.
        # Fusing precise (fts/chunk) and coarse (summary) keys in one ranked list let
        # non-evidence precise keys consume the fixed top-k coverage budget ahead of
        # the summary markers of high-ranked evidence sessions, cratering turn recall
        # below every input arm (measured 25q: rrf3 tR@5 0.40 vs chunk 0.62 / summary
        # 0.76; session-marker projection restores it to 0.88). The fused session
        # ranking is the arm's trustworthy signal, so its turn localization is honestly
        # session-granularity (carried by the ``*`` asterisk), same as summary_vectors.
        hybrid_turns = summary_turn_keys(hybrid_ranked)

        rerank_mode = RERANK_MODE_PLACEHOLDER
        rerank_start = time.perf_counter()
        rerank_ranked: list[str]
        if (
            use_rerank
            and embeddings_enabled
            and provider_name == "voyage"
            and hasattr(provider_embedder, "rerank")
        ):
            real = rerank_sessions_voyage(
                provider_embedder, question.question, hybrid_ranked, session_summaries
            )
            if real is not None:
                rerank_ranked = real
                rerank_mode = RERANK_MODE_VOYAGE
            else:
                rerank_ranked = rerank_by_cosine(hybrid_ranked, query_vec, session_vectors)
        elif embeddings_enabled:
            rerank_ranked = rerank_by_cosine(hybrid_ranked, query_vec, session_vectors)
        else:
            rerank_ranked = list(hybrid_ranked)
        rerank_ms = (time.perf_counter() - rerank_start) * 1000.0
        # C6: session-granularity markers follow the reranked session order.
        rerank_turns = summary_turn_keys(rerank_ranked)

        if embeddings_enabled:
            chunk_raw, chunk_ms = _timed(
                lambda: chunk_hits(vector_store, query_vec, model, provider_name, fetch)
            )
        else:
            chunk_raw, chunk_ms = [], 0.0
        chunk_ranked = _map_chunk_sessions(chunk_raw, store_id_to_session)
        chunk_turns = chunk_turn_keys(chunk_raw, store_id_to_turn)

        hybrid_rrf3_ranked, rrf3_ms = _timed(
            lambda: rrf_fuse(fts_ranked, vector_ranked, chunk_ranked)
        )
        # C6: session-granularity markers projected from the fused 3-arm ranking.
        rrf3_turns = summary_turn_keys(hybrid_rrf3_ranked)

        # The production tool: fetch drives lcm_recall's own limit (clamped to its
        # 25-hit ceiling). Its hits carry session_id directly (session ranking) and
        # store_id/node_id for turn projection. Its turn keys mix precise verbatim
        # keys with (session, None) summary markers, so it carries the asterisk.
        recall_raw, recall_ms = _timed(
            lambda: production_recall_hits(
                question, config, store, dag, provider_embedder,
                provider_name=provider_name, tmp_dir=tmp_dir,
                embeddings_enabled=embeddings_enabled, limit=fetch,
            )
        )
        recall_ranked = recall_hit_sessions(recall_raw)
        recall_turns = recall_hit_turn_keys(recall_raw, store_id_to_turn)

        # arm -> (session ranking, latency, turn keys, session_granularity asterisk).
        ranked_by_arm: dict[str, tuple[list[str], float, list[TurnKey], bool]] = {
            "fts": (fts_ranked, fts_ms, fts_turns, False),
            "summary_vectors": (vector_ranked, vector_ms, summary_turns, True),
            "hybrid_rrf": (hybrid_ranked, hybrid_ms, hybrid_turns, True),
            "hybrid_rerank": (rerank_ranked, rerank_ms, rerank_turns, True),
            "chunk_vectors": (chunk_ranked, chunk_ms, chunk_turns, False),
            "hybrid_rrf3": (hybrid_rrf3_ranked, rrf3_ms, rrf3_turns, True),
            "lcm_recall": (recall_ranked, recall_ms, recall_turns, True),
        }
        scored: dict[str, Any] = {"ingest_ms": ingest_ms}
        for arm, (ranked, elapsed_ms, turn_keys, session_granularity) in ranked_by_arm.items():
            scored[arm] = {
                "recall@1": recall_at_k(ranked, relevant, 1),
                "recall@5": recall_at_k(ranked, relevant, 5),
                "recall@10": recall_at_k(ranked, relevant, 10),
                "ndcg@10": ndcg_at_k(ranked, relevant, 10),
                "latency_ms": elapsed_ms,
                "turn": {
                    "recall@1": turn_recall_at_k(turn_keys, relevant_turns, 1),
                    "recall@5": turn_recall_at_k(turn_keys, relevant_turns, 5),
                    "recall@10": turn_recall_at_k(turn_keys, relevant_turns, 10),
                    "ndcg@10": turn_ndcg_at_k(turn_keys, relevant_turns, 10),
                    "session_granularity": session_granularity,
                },
            }
        scored["hybrid_rerank"]["rerank_mode"] = rerank_mode
        return scored
    finally:
        vector_store.close()
        dag.close()
        store.close()


def _embed_in_batches(embedder, texts: Sequence[str], batch_size: int = EMBED_BATCH_SIZE) -> list:
    """Embed ``texts`` in ``batch_size`` sub-batches, concatenating the results.

    One ``embed_documents`` call per sub-batch (F7 amortization) while each call
    stays inside the provider's per-call deadline. Per-text vectors are identical to
    embedding one text at a time for the deterministic/independent providers used here.
    """
    vectors: list = []
    for start in range(0, len(texts), max(1, batch_size)):
        vectors.extend(embedder.embed_documents(list(texts[start:start + batch_size])))
    return vectors


def _timed(fn):
    start = time.perf_counter()
    value = fn()
    return value, (time.perf_counter() - start) * 1000.0


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-._") or "question"


# --------------------------------------------------------------------------- #
# Aggregation + report.
# --------------------------------------------------------------------------- #


def _mean(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), 6) if values else 0.0


def _aggregate_rerank_mode(mode_counts: dict[str, int]) -> dict[str, Any]:
    """Collapse per-question rerank modes into one auditable run-level label.

    A run is labeled ``real`` (voyage) only if EVERY scored rerank-arm question
    used the real reranker; if any question silently fell back to the placeholder
    the run is ``mixed`` (never mislabeled as real); if none used voyage it is
    ``placeholder``. Per-mode counts ride alongside so the label is verifiable
    against the run rather than reflecting only the final question.
    """
    voyage = mode_counts.get(RERANK_MODE_VOYAGE, 0)
    placeholder = mode_counts.get(RERANK_MODE_PLACEHOLDER, 0)
    total = sum(mode_counts.values())
    if total == 0 or voyage == 0:
        mode = RERANK_MODE_PLACEHOLDER
    elif voyage == total:
        mode = RERANK_MODE_VOYAGE
    else:
        mode = RERANK_MODE_MIXED
    return {
        "mode": mode,
        "real_count": voyage,
        "placeholder_count": placeholder,
        "counts": dict(mode_counts),
    }


def run_harness(
    questions: Sequence[Question],
    *,
    provider_name: str,
    model: str,
    tmp_dir: Path,
    embeddings_enabled: bool | None = None,
    use_rerank: bool = False,
    reuse_db_template: bool = True,
) -> dict[str, Any]:
    """Run every arm over every question and return an aggregate-only report."""
    if embeddings_enabled is None:
        embeddings_enabled = provider_name != "none"
    embedder = resolve_harness_provider(provider_name, model)

    db_template: Path | None = None
    if reuse_db_template:
        _ensure_hermes_lcm_package()
        from hermes_lcm.config import LCMConfig

        db_template = Path(tmp_dir) / "_template.db"
        _bootstrap_db_template(
            db_template,
            LCMConfig(
                database_path=str(db_template),
                embeddings_enabled=embeddings_enabled,
                embedding_provider=provider_name,
                embedding_model=embedder.model_id,
            ),
        )

    by_category: dict[str, dict[str, ArmSamples]] = {}
    overall = _new_arm_samples()
    scored_count = 0
    abstention_count = 0
    ingest_samples: list[float] = []
    # Track per-question rerank modes so the run-level label is an aggregate, not
    # whatever the final question happened to use (FIX-2).
    rerank_mode_counts: dict[str, int] = {}

    for question in questions:
        if question.is_abstention:
            abstention_count += 1
            continue
        scored = evaluate_question(
            question,
            embedder,
            provider_name=provider_name,
            tmp_dir=tmp_dir,
            embeddings_enabled=embeddings_enabled,
            use_rerank=use_rerank,
            db_template=db_template,
        )
        scored_count += 1
        ingest_samples.append(scored.pop("ingest_ms", 0.0))
        q_mode = scored["hybrid_rerank"].pop("rerank_mode", RERANK_MODE_PLACEHOLDER)
        rerank_mode_counts[q_mode] = rerank_mode_counts.get(q_mode, 0) + 1
        category = question.category
        bucket = by_category.setdefault(category, _new_arm_samples())
        for arm, metrics in scored.items():
            turn = metrics["turn"]
            for k in (1, 5, 10):
                bucket[arm].recalls[k].append(metrics[f"recall@{k}"])
                overall[arm].recalls[k].append(metrics[f"recall@{k}"])
                bucket[arm].turn_recalls[k].append(turn[f"recall@{k}"])
                overall[arm].turn_recalls[k].append(turn[f"recall@{k}"])
            bucket[arm].ndcg10.append(metrics["ndcg@10"])
            overall[arm].ndcg10.append(metrics["ndcg@10"])
            bucket[arm].turn_ndcg10.append(turn["ndcg@10"])
            overall[arm].turn_ndcg10.append(turn["ndcg@10"])
            bucket[arm].latency_ms.append(metrics["latency_ms"])
            overall[arm].latency_ms.append(metrics["latency_ms"])
            if turn["session_granularity"]:
                bucket[arm].session_granularity = True
                overall[arm].session_granularity = True

    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark_version": BENCHMARK_VERSION,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "transcript_contents_included": False,
        "dataset": {
            "name": "LongMemEval_S",
            "repo_id": DATASET_REPO_ID,
            "revision": DATASET_REVISION,
            "file": DATASET_FILENAME,
        },
        "provider": provider_name,
        "model": model,
        "embeddings_enabled": embeddings_enabled,
        "question_count": len(questions),
        "scored_count": scored_count,
        "abstention_excluded": abstention_count,
        "rerank": {
            **_aggregate_rerank_mode(rerank_mode_counts),
            "candidate_window": RERANK_CANDIDATE_WINDOW,
            "timeout_s": RERANK_TIMEOUT_S,
        },
        "ingest": {
            "batched_embeddings": embeddings_enabled,
            "reuse_db_template": reuse_db_template,
            "per_question_ms": percentiles(ingest_samples),
        },
        "arms": {
            arm: _arm_report(overall[arm]) for arm in ARMS
        },
        "per_category": {
            category: {arm: _arm_report(samples[arm]) for arm in ARMS}
            for category, samples in sorted(by_category.items())
        },
    }


def _arm_report(samples: ArmSamples) -> dict[str, Any]:
    return {
        "recall@1": _mean(samples.recalls[1]),
        "recall@5": _mean(samples.recalls[5]),
        "recall@10": _mean(samples.recalls[10]),
        "ndcg@10": _mean(samples.ndcg10),
        "n": len(samples.ndcg10),
        "latency_ms": percentiles(samples.latency_ms),
        "turn": {
            "recall@1": _mean(samples.turn_recalls[1]),
            "recall@5": _mean(samples.turn_recalls[5]),
            "recall@10": _mean(samples.turn_recalls[10]),
            "ndcg@10": _mean(samples.turn_ndcg10),
            "session_granularity": samples.session_granularity,
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    """Aggregate-only markdown table of overall per-arm session + turn recall/NDCG.

    ``*`` on an arm name marks turn-level numbers that are session-granularity: the
    arm retrieves summaries, which localize only to a whole session, so a hit credits
    every evidence turn of that session at once.
    """
    rerank = report.get("rerank", {})
    ingest = report.get("ingest", {})
    per_q = ingest.get("per_question_ms", {})
    lines = [
        f"# LongMemEval_S retrieval — provider={report['provider']} "
        f"model={report['model'] or 'n/a'}",
        "",
        f"scored={report['scored_count']} abstention_excluded={report['abstention_excluded']} "
        f"dataset={report['dataset']['repo_id']}@{report['dataset']['revision'][:7]}",
        f"rerank={rerank.get('mode', 'n/a')} (window={rerank.get('candidate_window', 'n/a')}) "
        f"ingest_p50={per_q.get('p50', 0.0):.1f}ms "
        f"batched_embeddings={ingest.get('batched_embeddings', False)} "
        f"reuse_db_template={ingest.get('reuse_db_template', False)}",
        "",
        "| Arm | R@1 | R@5 | R@10 | NDCG@10 | tR@1 | tR@5 | tR@10 | tNDCG@10 | p50 ms | p90 ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for arm in ARMS:
        row = report["arms"][arm]
        turn = row["turn"]
        label = f"{arm}*" if turn.get("session_granularity") else arm
        lines.append(
            f"| {label} | {row['recall@1']:.3f} | {row['recall@5']:.3f} | "
            f"{row['recall@10']:.3f} | {row['ndcg@10']:.3f} | "
            f"{turn['recall@1']:.3f} | {turn['recall@5']:.3f} | "
            f"{turn['recall@10']:.3f} | {turn['ndcg@10']:.3f} | "
            f"{row['latency_ms']['p50']:.1f} | {row['latency_ms']['p90']:.1f} |"
        )
    lines.append("")
    lines.append(
        "`t*` columns are turn-level. `*` = session-granularity turn scoring "
        "(summary arms cannot localize to a turn)."
    )
    return "\n".join(lines)

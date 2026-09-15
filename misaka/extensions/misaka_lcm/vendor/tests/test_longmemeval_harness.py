"""Tests for the LongMemEval retrieval harness.

Covers the evidence-matching scorer, the metric math (recall@k / NDCG@10 /
percentiles), CLI argument validation, and an end-to-end offline stub run that
proves the ingest -> retrieve -> score plumbing over a real temp LCM store.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from benchmarking.longmemeval import (
    ARMS,
    DATASET_REVISION,
    RERANK_MODE_MIXED,
    RERANK_MODE_PLACEHOLDER,
    RERANK_MODE_VOYAGE,
    Question,
    chunk_sessions,
    deterministic_session_summary,
    evaluate_question,
    evidence_sessions,
    evidence_turns,
    fresh_recall_session_id,
    load_questions,
    ndcg_at_k,
    parse_question,
    percentiles,
    production_recall_hits,
    recall_at_k,
    recall_hit_sessions,
    recall_hit_turn_keys,
    rerank_sessions_voyage,
    rrf_fuse,
    run_harness,
    summary_turn_keys,
    turn_ndcg_at_k,
    turn_recall_at_k,
)

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "lcm_longmemeval.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("lcm_longmemeval_cli", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_raw(
    question_id: str,
    question_type: str,
    *,
    sessions: dict[str, list[dict]],
    answer_session_ids: list[str],
    question: str = "what did we decide",
) -> dict:
    session_ids = list(sessions)
    return {
        "question_id": question_id,
        "question_type": question_type,
        "question": question,
        "answer": "irrelevant",
        "question_date": "2023-01-01",
        "haystack_session_ids": session_ids,
        "haystack_dates": ["2023-01-01"] * len(session_ids),
        "haystack_sessions": [sessions[sid] for sid in session_ids],
        "answer_session_ids": answer_session_ids,
    }


# --------------------------------------------------------------------------- #
# Evidence-matching scorer.
# --------------------------------------------------------------------------- #


def test_evidence_sessions_uses_answer_session_ids():
    raw = _make_raw(
        "q1",
        "multi-session",
        sessions={
            "s1": [{"role": "user", "content": "hi"}],
            "s2": [{"role": "user", "content": "budget is 500", "has_answer": True}],
        },
        answer_session_ids=["s2"],
    )
    question = parse_question(raw)
    assert evidence_sessions(question) == {"s2"}


def test_abstention_questions_have_no_evidence_and_are_flagged():
    raw = _make_raw(
        "q9_abs",
        "single-session-user",
        sessions={"s1": [{"role": "user", "content": "hi"}]},
        answer_session_ids=[],
    )
    question = parse_question(raw)
    assert question.is_abstention is True
    assert evidence_sessions(question) == set()
    assert evidence_turns(question) == set()


def test_evidence_turns_reads_has_answer_markers():
    raw = _make_raw(
        "q2",
        "single-session-assistant",
        sessions={
            "s1": [
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": "the code is X7", "has_answer": True},
            ],
        },
        answer_session_ids=["s1"],
    )
    question = parse_question(raw)
    assert evidence_turns(question) == {("s1", 1)}


def test_category_maps_temporal_reasoning_label():
    raw = _make_raw(
        "q3",
        "temporal-reasoning",
        sessions={"s1": [{"role": "user", "content": "a"}]},
        answer_session_ids=["s1"],
    )
    assert parse_question(raw).category == "temporal"


# --------------------------------------------------------------------------- #
# Metric math.
# --------------------------------------------------------------------------- #


def test_recall_at_k_counts_relevant_within_top_k():
    retrieved = ["s3", "s1", "s2", "s9"]
    assert recall_at_k(retrieved, {"s1", "s2"}, 1) == 0.0
    assert recall_at_k(retrieved, {"s1", "s2"}, 2) == pytest.approx(0.5)
    assert recall_at_k(retrieved, {"s1", "s2"}, 3) == pytest.approx(1.0)


def test_recall_at_k_dedups_and_handles_empty_relevant():
    assert recall_at_k(["s1", "s1", "s1"], set(), 5) == 0.0
    assert recall_at_k(["s1", "s1", "s2"], {"s2"}, 2) == pytest.approx(1.0)


def test_ndcg_at_k_perfect_and_ranked():
    # Single relevant item at rank 1 -> perfect NDCG.
    assert ndcg_at_k(["s1", "s2"], {"s1"}, 10) == pytest.approx(1.0)
    # Relevant item at rank 2: DCG = 1/log2(3); IDCG (1 relevant) = 1/log2(2)=1.
    import math

    assert ndcg_at_k(["s2", "s1"], {"s1"}, 10) == pytest.approx(1.0 / math.log2(3))
    assert ndcg_at_k(["s2", "s3"], {"s1"}, 10) == 0.0


def test_percentiles_nearest_rank():
    values = [10.0, 20.0, 30.0, 40.0, 100.0]
    result = percentiles(values, points=(50, 90, 99))
    assert result["p50"] == 30.0
    assert result["p90"] == 100.0
    assert result["p99"] == 100.0
    assert percentiles([], points=(50,)) == {"p50": 0.0}


def test_rrf_fuse_rewards_agreement_across_arms():
    fts = ["s1", "s2", "s3"]
    vectors = ["s3", "s1", "s9"]
    fused = rrf_fuse(fts, vectors)
    # s1 (ranks 1 and 2) and s3 (ranks 3 and 1) outrank single-arm-only items.
    assert set(fused[:2]) == {"s1", "s3"}
    assert set(fused) == {"s1", "s2", "s3", "s9"}


def test_turn_recall_precise_keys():
    # Two labeled evidence turns; a ranked list of precise (session, turn) keys.
    evidence = {("s1", 1), ("s2", 0)}
    ranked = [("s1", 0), ("s1", 1), ("s3", 2), ("s2", 0)]
    assert turn_recall_at_k(ranked, evidence, 1) == 0.0
    assert turn_recall_at_k(ranked, evidence, 2) == pytest.approx(0.5)
    assert turn_recall_at_k(ranked, evidence, 4) == pytest.approx(1.0)


def test_turn_recall_summary_marker_covers_session_at_granularity():
    # A (session, None) summary marker covers ALL evidence turns of its session in
    # one item — the session-granularity credit an asterisk warns about.
    evidence = {("s1", 3), ("s1", 7), ("s2", 0)}
    # One summary marker for s1 at rank 1 recovers both s1 evidence turns.
    assert turn_recall_at_k([("s1", None)], evidence, 1) == pytest.approx(2 / 3)
    # A marker for a session with no evidence contributes nothing.
    assert turn_recall_at_k([("s9", None)], evidence, 1) == 0.0
    assert turn_recall_at_k([], evidence, 5) == 0.0
    assert turn_recall_at_k([("s1", 3)], set(), 5) == 0.0


def test_hybrid_turn_keys_project_from_fused_ranking_not_raw_key_fusion():
    """C6: a hybrid arm's turn keys are session-granularity markers derived from its
    fused SESSION ranking, NOT an RRF over the raw per-arm turn-key lists.

    Regression for the B5-measured turn-precision collapse: fusing precise (fts /
    chunk) and coarse (summary) turn keys in one ranked list let a flood of precise
    NON-evidence keys consume the fixed top-k coverage budget ahead of the summary
    markers of the high-ranked evidence session, dragging turn recall below every
    input arm. Projecting the (strong) fused session ranking to (session, None)
    markers restores full session-granularity coverage.
    """
    # Evidence lives entirely in session "sE" (3 labeled turns).
    evidence = {("sE", 0), ("sE", 1), ("sE", 2)}
    # The fused SESSION ranking puts the evidence session first (its strong signal).
    fused_ranking = ["sE", "sA", "sB", "sC", "sD", "sF"]
    # The precise arms AGREE on five NON-evidence turns, so each of those keys earns
    # two RRF terms and outscores the evidence session's single-arm summary marker.
    noise = [("sA", 0), ("sB", 0), ("sC", 0), ("sD", 0), ("sF", 0)]
    fts_turns = list(noise)
    chunk_turns = list(noise)
    # In the summary arm the evidence session ranks LAST, so its marker lands at
    # rank 6 — pushed out of the top-5 budget by the agreed-upon noise.
    summary_turns = summary_turn_keys(["sA", "sB", "sC", "sD", "sF", "sE"])

    # Old behavior: raw-key RRF buries ("sE", None) below five non-evidence keys.
    diluted = rrf_fuse(fts_turns, summary_turns, chunk_turns)
    # New behavior: project the fused session ranking to session-granularity markers.
    projected = summary_turn_keys(fused_ranking)

    assert all(key[1] is None for key in projected)
    # The evidence session's marker sits at rank 1 and covers all its turns.
    assert turn_recall_at_k(projected, evidence, 5) == pytest.approx(1.0)
    # The diluted fusion recovers nothing in the top-5 (the collapse being fixed).
    assert turn_recall_at_k(diluted, evidence, 5) == pytest.approx(0.0)


def test_turn_ndcg_rewards_ranking_and_credits_summary_markers():
    evidence = {("s1", 2)}
    # Precise relevant turn at rank 1 -> perfect NDCG.
    assert turn_ndcg_at_k([("s1", 2), ("s4", 0)], evidence, 10) == pytest.approx(1.0)
    # Summary marker for the evidence session counts as relevant at session grain.
    assert turn_ndcg_at_k([("s1", None)], evidence, 10) == pytest.approx(1.0)
    # Irrelevant-only ranking scores zero.
    assert turn_ndcg_at_k([("s7", 0), ("s8", 1)], evidence, 10) == 0.0


def test_deterministic_summary_is_stable_and_content_bearing():
    turns = [{"role": "user", "content": "the vault code is 4417"}]
    first = deterministic_session_summary(turns)
    second = deterministic_session_summary(turns)
    assert first == second
    assert "4417" in first


# --------------------------------------------------------------------------- #
# CLI argument validation.
# --------------------------------------------------------------------------- #


def test_cli_run_requires_model_for_non_stub_provider():
    cli = _load_cli()
    args = cli._parse_args(
        ["run", "--dataset", "x.json", "--output", "out", "--provider", "fastembed"]
    )
    with pytest.raises(SystemExit):
        cli._cmd_run(args)


def test_cli_run_rejects_nonpositive_limit(tmp_path):
    cli = _load_cli()
    dataset = tmp_path / "d.json"
    dataset.write_text("[]", encoding="utf-8")
    args = cli._parse_args(
        ["run", "--dataset", str(dataset), "--output", str(tmp_path / "o"), "--limit", "0"]
    )
    with pytest.raises(SystemExit):
        cli._cmd_run(args)


def test_cli_run_rejects_missing_dataset(tmp_path):
    cli = _load_cli()
    args = cli._parse_args(
        ["run", "--dataset", str(tmp_path / "missing.json"), "--output", str(tmp_path / "o")]
    )
    with pytest.raises(SystemExit):
        cli._cmd_run(args)


def test_cli_rejects_unknown_provider():
    cli = _load_cli()
    with pytest.raises(SystemExit):
        cli._parse_args(["run", "--dataset", "x", "--output", "o", "--provider", "nope"])


def test_load_questions_limit_validation(tmp_path):
    dataset = tmp_path / "d.json"
    dataset.write_text(json.dumps([]), encoding="utf-8")
    with pytest.raises(ValueError):
        load_questions(dataset, limit=-1)


# --------------------------------------------------------------------------- #
# End-to-end offline stub run (plumbing proof).
# --------------------------------------------------------------------------- #


def _synthetic_dataset() -> list[Question]:
    questions: list[Question] = []
    for index in range(3):
        evidence_id = f"q{index}-s-evidence"
        sessions = {
            f"q{index}-s-noise": [
                {"role": "user", "content": "unrelated small talk about the weather"},
                {"role": "assistant", "content": "yes it is sunny today"},
            ],
            evidence_id: [
                {"role": "user", "content": f"remember my locker passcode is ZEBRA{index}"},
                {
                    "role": "assistant",
                    "content": f"noted, locker passcode ZEBRA{index}",
                    "has_answer": True,
                },
            ],
        }
        questions.append(
            parse_question(
                _make_raw(
                    f"q{index}",
                    "single-session-user",
                    sessions=sessions,
                    answer_session_ids=[evidence_id],
                    question=f"what is my locker passcode ZEBRA{index}",
                )
            )
        )
    return questions


class _FakeChunkStore:
    """Minimal stand-in exposing only ``knn_chunks`` for the chunk arm."""

    def __init__(self, hits):
        self._hits = hits

    def knn_chunks(self, query_vec, k, model, provider):
        return list(self._hits)


def test_chunk_sessions_maps_hits_to_sessions_and_dedups():
    # Chunk ids are ``store_id:chunk_index``; each votes for its owning session,
    # first-seen order wins, and an unmapped store_id is dropped.
    hits = [("10:0", 0.9, "chunk"), ("11:2", 0.8, "chunk"),
            ("10:1", 0.7, "chunk"), ("99:0", 0.6, "chunk")]
    store_id_to_session = {10: "sess-a", 11: "sess-b"}
    ranked = chunk_sessions(
        _FakeChunkStore(hits), [1.0, 0.0], "model", "provider", 10, store_id_to_session
    )
    assert ranked == ["sess-a", "sess-b"]


class _KeyedEmbedder:
    """Deterministic embedder: text mentioning the passcode maps to one axis.

    This makes the chunk KNN arm resolvable offline — the evidence chunk and the
    query share the ``passcode`` axis, so the evidence session ranks first.
    """

    model_id = "keyed"
    dim = 2

    def _vec(self, text: str) -> list[float]:
        return [1.0, 0.0] if "passcode" in text.lower() else [0.0, 1.0]

    def embed_documents(self, texts):
        return [self._vec(str(text)) for text in texts]

    def embed_query(self, text):
        return self._vec(str(text))


def test_evaluate_question_chunk_arm_recovers_evidence(tmp_path):
    evidence_id = "s-evidence"
    long_evidence = (
        "please remember for later reference that my personal locker passcode "
        "phrase is the northern lighthouse keeper, and that this detail matters "
        "quite a lot to me because I keep forgetting it every single time I try "
        "to open the locker at the gym after my afternoon workout session"
    )
    sessions = {
        "s-noise": [
            {"role": "user", "content": "unrelated small talk about the sunny weather today"},
            {"role": "assistant", "content": "yes it certainly is a pleasant afternoon outside"},
        ],
        evidence_id: [
            # The passcode cue and the has_answer marker are the same (user) turn,
            # so the chunk arm's retrieved turn matches the labeled evidence turn.
            {"role": "user", "content": long_evidence, "has_answer": True},
            {"role": "assistant", "content": "noted, I will keep that safe"},
        ],
    }
    question = parse_question(
        _make_raw(
            "q-chunk", "single-session-user", sessions=sessions,
            answer_session_ids=[evidence_id],
            question="what is my locker passcode phrase",
        )
    )

    scored = evaluate_question(
        question, _KeyedEmbedder(), provider_name="stub",
        tmp_dir=tmp_path, embeddings_enabled=True,
    )

    assert "chunk_vectors" in scored and "hybrid_rrf3" in scored
    # The chunk arm recovers the evidence session via the shared passcode axis.
    assert scored["chunk_vectors"]["recall@1"] == pytest.approx(1.0)
    # The three-arm fusion keeps the evidence session in its top-k.
    assert scored["hybrid_rrf3"]["recall@10"] == pytest.approx(1.0)
    for arm in ARMS:
        assert scored[arm]["latency_ms"] >= 0.0
        # Every arm now reports a turn-level block alongside the session metrics.
        turn = scored[arm]["turn"]
        assert set(turn) >= {"recall@1", "recall@5", "recall@10", "ndcg@10", "session_granularity"}
    # The chunk arm localizes to the exact evidence turn (store_id -> turn), so its
    # turn-level recall is exact, not session-granularity.
    assert scored["chunk_vectors"]["turn"]["recall@1"] == pytest.approx(1.0)
    assert scored["chunk_vectors"]["turn"]["session_granularity"] is False
    # Summary-based arms carry the session-granularity asterisk.
    assert scored["summary_vectors"]["turn"]["session_granularity"] is True
    assert scored["hybrid_rerank"]["rerank_mode"] == RERANK_MODE_PLACEHOLDER


class _FakeReranker:
    """Records the rerank call and returns a fixed reordering, or raises."""

    def __init__(self, order=None, raise_error=False):
        self._order = order
        self._raise = raise_error
        self.calls = 0

    def rerank(self, query, documents, *, top_k=None, timeout):
        self.calls += 1
        if self._raise:
            raise RuntimeError("provider down")
        order = self._order if self._order is not None else list(range(len(documents)))
        return [(index, 1.0 - position * 0.1) for position, index in enumerate(order)]


def test_rerank_sessions_voyage_reorders_window_and_appends_tail():
    reranker = _FakeReranker(order=[2, 0, 1])
    sessions = ["a", "b", "c", "d", "e"]
    summaries = {s: f"summary {s}" for s in sessions}
    out = rerank_sessions_voyage(reranker, "q", sessions, summaries, window=3)
    assert reranker.calls == 1
    # Window [a,b,c] reordered to [c,a,b]; tail [d,e] preserved.
    assert out == ["c", "a", "b", "d", "e"]


def test_rerank_sessions_voyage_signals_fallback_on_error_and_empty():
    reranker = _FakeReranker(raise_error=True)
    assert rerank_sessions_voyage(reranker, "q", ["a", "b"], {"a": "x", "b": "y"}) is None
    assert rerank_sessions_voyage(reranker, "q", [], {}) is None


def test_rerank_sessions_voyage_empty_response_signals_fallback():
    """FIX-3: a non-exception empty response (e.g. ``data: []``) is degenerate --
    it must signal the placeholder fallback, not be accepted as a real rerank."""
    reranker = _FakeReranker(order=[])  # provider returns [] without raising
    out = rerank_sessions_voyage(reranker, "q", ["a", "b", "c"], {"a": "x", "b": "y", "c": "z"})
    assert out is None
    assert reranker.calls == 1  # the provider WAS called; its response was degenerate


def test_rerank_sessions_voyage_partial_coverage_signals_fallback():
    """FIX-3: a response scoring only some candidates does not cover the input
    set, so it is degenerate and must fall back rather than count as real."""
    reranker = _FakeReranker(order=[0])  # only 1 of 3 candidates scored
    out = rerank_sessions_voyage(reranker, "q", ["a", "b", "c"], {"a": "x", "b": "y", "c": "z"})
    assert out is None


class _RerankingEmbedder(_KeyedEmbedder):
    """A voyage-shaped embedder that also exposes a fake ``rerank``."""

    def rerank(self, query, documents, *, top_k=None, timeout):
        # Identity order is enough: we only assert the mode label, not the ordering.
        return [(index, 1.0) for index in range(len(documents))]


def test_evaluate_question_real_rerank_path_labels_voyage_mode(tmp_path):
    # use_rerank + provider voyage + a reranker-bearing embedder takes the real
    # cross-encoder path and labels it, instead of the placeholder.
    evidence_id = "s-evidence"
    sessions = {
        "s-noise": [{"role": "user", "content": "chatter about the weather"}],
        evidence_id: [
            {"role": "user", "content": "my passcode phrase", "has_answer": True},
        ],
    }
    question = parse_question(
        _make_raw(
            "q-rr", "single-session-user", sessions=sessions,
            answer_session_ids=[evidence_id], question="what is my passcode phrase",
        )
    )
    scored = evaluate_question(
        question, _RerankingEmbedder(), provider_name="voyage",
        tmp_dir=tmp_path, embeddings_enabled=True, use_rerank=True,
    )
    assert scored["hybrid_rerank"]["rerank_mode"] == RERANK_MODE_VOYAGE


def test_run_harness_mixed_rerank_reports_mixed_not_real(tmp_path, monkeypatch):
    """FIX-2: when some questions use the real reranker and others silently fall
    back, the run-level mode is ``mixed`` (with counts), never mislabeled ``real``
    from whatever the final question happened to use."""
    import benchmarking.longmemeval as lme

    monkeypatch.setattr(lme, "resolve_harness_provider", lambda *a, **k: _RerankingEmbedder())
    calls = {"n": 0}

    def _fake_rerank(reranker, query, sessions, summaries, **kwargs):
        calls["n"] += 1
        # First scored question gets a real rerank; the rest fall back silently.
        return list(sessions) if calls["n"] == 1 else None

    monkeypatch.setattr(lme, "rerank_sessions_voyage", _fake_rerank)

    report = run_harness(
        _synthetic_dataset(), provider_name="voyage", model="voyage-3",
        tmp_dir=tmp_path, use_rerank=True,
    )
    assert report["rerank"]["mode"] == RERANK_MODE_MIXED
    assert report["rerank"]["real_count"] == 1
    assert report["rerank"]["placeholder_count"] == 2
    assert report["rerank"]["counts"][RERANK_MODE_VOYAGE] == 1


def test_run_harness_all_real_rerank_reports_voyage(tmp_path, monkeypatch):
    """FIX-2: a run where every question used the real reranker is labeled real."""
    import benchmarking.longmemeval as lme

    monkeypatch.setattr(lme, "resolve_harness_provider", lambda *a, **k: _RerankingEmbedder())
    monkeypatch.setattr(
        lme, "rerank_sessions_voyage",
        lambda reranker, query, sessions, summaries, **kwargs: list(sessions),
    )
    report = run_harness(
        _synthetic_dataset(), provider_name="voyage", model="voyage-3",
        tmp_dir=tmp_path, use_rerank=True,
    )
    assert report["rerank"]["mode"] == RERANK_MODE_VOYAGE
    assert report["rerank"]["real_count"] == 3
    assert report["rerank"]["placeholder_count"] == 0


def test_stub_run_end_to_end_produces_report_and_fts_recovers_evidence(tmp_path):
    report = run_harness(
        _synthetic_dataset(),
        provider_name="stub",
        model="",
        tmp_dir=tmp_path,
    )
    assert report["scored_count"] == 3
    assert report["dataset"]["revision"] == DATASET_REVISION
    assert report["transcript_contents_included"] is False
    assert set(report["arms"]) == set(ARMS)
    # FTS is lexical and provider-independent: the passcode query must recover
    # its evidence session even under the meaningless stub embedder.
    assert report["arms"]["fts"]["recall@10"] == pytest.approx(1.0)
    for arm in ARMS:
        assert report["arms"][arm]["latency_ms"]["p50"] >= 0.0
        assert "turn" in report["arms"][arm]
    # F7 provenance is recorded and the placeholder rerank is labeled.
    assert report["rerank"]["mode"] == RERANK_MODE_PLACEHOLDER
    assert report["ingest"]["reuse_db_template"] is True
    assert "per_question_ms" in report["ingest"]


def test_db_template_reuse_matches_from_scratch_bootstrap(tmp_path):
    # Cloning a pre-migrated template must not change any scored output vs a
    # from-scratch bootstrap per question (F7 is a speed optimization only).
    dataset = _synthetic_dataset()
    (tmp_path / "templated").mkdir()
    templated = run_harness(
        dataset, provider_name="stub", model="",
        tmp_dir=tmp_path / "templated", reuse_db_template=True,
    )
    (tmp_path / "scratch").mkdir()
    from_scratch = run_harness(
        dataset, provider_name="stub", model="",
        tmp_dir=tmp_path / "scratch", reuse_db_template=False,
    )
    for arm in ARMS:
        for metric in ("recall@1", "recall@5", "recall@10", "ndcg@10"):
            assert templated["arms"][arm][metric] == from_scratch["arms"][arm][metric]
            assert templated["arms"][arm]["turn"][metric] == from_scratch["arms"][arm]["turn"][metric]
    assert templated["ingest"]["reuse_db_template"] is True
    assert from_scratch["ingest"]["reuse_db_template"] is False


# --------------------------------------------------------------------------- #
# Production lcm_recall arm (the tool users actually call).
# --------------------------------------------------------------------------- #


def test_lcm_recall_arm_is_registered_and_scored(tmp_path):
    """The production arm is a first-class arm: registered and scored end-to-end."""
    assert "lcm_recall" in ARMS
    report = run_harness(
        _synthetic_dataset(), provider_name="stub", model="", tmp_dir=tmp_path
    )
    assert "lcm_recall" in report["arms"]
    recall = report["arms"]["lcm_recall"]
    assert set(recall) >= {"recall@1", "recall@5", "recall@10", "ndcg@10", "turn"}
    # Non-degenerate: the production path recovers the evidence session (its ZEBRA
    # cue is lexically distinctive, so at minimum the FTS arm inside recall fires).
    assert recall["recall@10"] > 0.0


def test_fresh_recall_session_is_disjoint_from_haystack_and_neutralizes_scope(tmp_path):
    """The probe's current-session id sits OUTSIDE the dataset, so the scope prior
    never boosts a dataset session (recency still applies — honest production)."""
    evidence_id = "s-evidence"
    sessions = {
        "s-noise": [{"role": "user", "content": "chatter about the weather"}],
        evidence_id: [
            {"role": "user", "content": "the locker passcode is ZEBRA0", "has_answer": True},
        ],
    }
    question = parse_question(
        _make_raw(
            "q-scope", "single-session-user", sessions=sessions,
            answer_session_ids=[evidence_id], question="what is my locker passcode ZEBRA0",
        )
    )
    fresh = fresh_recall_session_id(question)
    assert fresh not in question.haystack_session_ids
    assert fresh == fresh_recall_session_id(question)  # deterministic

    # Use the real harness StubEmbedder (it carries provider_id="stub", which the
    # production KNN arms match against the recorded profile) so the vector arms
    # return hits, exercising the true scope-prior path rather than a degraded one.
    from benchmarking.longmemeval import StubEmbedder
    from hermes_lcm.config import LCMConfig
    from hermes_lcm.dag import SummaryDAG
    from hermes_lcm.store import MessageStore

    embedder = StubEmbedder()
    config = LCMConfig(
        database_path=str(tmp_path / f"{question.question_id}.db"), embeddings_enabled=True,
        embedding_provider="stub", embedding_model=embedder.model_id,
    )
    # Reuse the harness ingest to seed the store, then invoke the production tool.
    evaluate_question(
        question, embedder, provider_name="stub",
        tmp_dir=tmp_path, embeddings_enabled=True,
    )
    store = MessageStore(config.database_path, ingest_protection_config=config)
    dag = SummaryDAG(config.database_path)
    try:
        hits = production_recall_hits(
            question, config, store, dag, embedder,
            provider_name="stub", tmp_dir=tmp_path, embeddings_enabled=True, limit=25,
        )
    finally:
        dag.close()
        store.close()
    assert hits, "production recall returned no hits"
    # No hit belongs to the (fresh) current conversation, so the scope boost is inert.
    assert all(hit.get("from_current_session") is False for hit in hits)


def test_fresh_recall_session_avoids_haystack_collision():
    """If the sentinel id already exists in the haystack, a unique variant is used."""
    from benchmarking.longmemeval import _LCM_RECALL_FRESH_SESSION

    collide = f"{_LCM_RECALL_FRESH_SESSION}q-collide"
    raw = _make_raw(
        "q-collide", "single-session-user",
        sessions={collide: [{"role": "user", "content": "x"}]},
        answer_session_ids=[collide],
    )
    question = parse_question(raw)
    fresh = fresh_recall_session_id(question)
    assert fresh not in question.haystack_session_ids
    assert fresh.startswith(collide)


def test_recall_hit_projection_sessions_and_turns():
    """store_id -> (session, turn) projection: verbatim hits localize precisely,
    summary hits become (session, None) markers, unmapped/missing ids drop."""
    store_id_to_turn = {10: ("s1", 0), 11: ("s2", 3)}
    hits = [
        {"kind": "message_excerpt", "store_id": 10, "session_id": "s1"},
        {"kind": "summary", "node_id": 7, "session_id": "s2"},
        {"kind": "message_excerpt", "store_id": 11, "session_id": "s2"},
        {"kind": "message_excerpt", "store_id": 99, "session_id": "s9"},  # unmapped -> drop
        {"kind": "summary", "session_id": None},  # no session -> drop
    ]
    # Session ranking dedups in hit order (s2 first seen via the summary hit).
    assert recall_hit_sessions(hits) == ["s1", "s2", "s9"]
    # Turn projection: precise keys for verbatim, (session, None) for summary.
    assert recall_hit_turn_keys(hits, store_id_to_turn) == [
        ("s1", 0), ("s2", None), ("s2", 3),
    ]


def test_rrf_fuse_turn_keys_with_none_turn_tie_is_total_ordered():
    """A summary turn key (session, None) tying a localized (session, int) key
    on score and best rank must not crash the sort (None < int TypeError)."""
    from benchmarking.longmemeval import rrf_fuse

    fused = rrf_fuse([("s1", None), ("s2", 3)], [("s2", 3), ("s1", None)])
    assert set(fused) == {("s1", None), ("s2", 3)}
    # And a same-session tie between None-turn and int-turn keys:
    fused2 = rrf_fuse([("s1", None)], [("s1", 4)])
    assert set(fused2) == {("s1", None), ("s1", 4)}


def test_deterministic_summary_of_empty_session_is_non_empty():
    """Empty haystack sessions must yield embeddable (non-empty) summary text —
    cloud endpoints reject empty inputs with HTTP 400 (regression: voyage 500q
    run died on LongMemEval_S's empty sessions)."""
    from benchmarking.longmemeval import deterministic_session_summary

    assert deterministic_session_summary([]) == "(empty session)"
    assert deterministic_session_summary([{"role": "user", "content": "  "}]).strip()

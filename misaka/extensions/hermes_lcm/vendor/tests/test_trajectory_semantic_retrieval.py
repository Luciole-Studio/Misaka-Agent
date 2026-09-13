"""Provider-neutral semantic trajectory selection and exact-state closure."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from hermes_lcm.trajectory_store import (
    CorpusIdentity,
    TrajectorySource,
    TrajectoryState,
    TrajectoryStore,
)


class FakeEmbeddingProvider:
    provider_id = "fake"
    model_id = "fake-trajectory-v1"
    dim = 2

    def __init__(self, *, fail_query: bool = False) -> None:
        self.fail_query = fail_query
        self.document_calls = 0
        self.query_calls = 0
        self.documents: list[str] = []
        self.last_usage_tokens = 0

    def embed_documents(self, texts):
        self.document_calls += 1
        self.documents.extend(str(text) for text in texts)
        self.last_usage_tokens = sum(max(1, len(str(text)) // 4) for text in texts)
        return [
            [1.0, 0.0] if "profile toolbar" in str(text).casefold() else [0.0, 1.0]
            for text in texts
        ]

    def embed_query(self, text):
        self.query_calls += 1
        if self.fail_query:
            raise RuntimeError("synthetic provider failure")
        self.last_usage_tokens = max(1, len(str(text)) // 4)
        return [1.0, 0.0]


def _identity() -> CorpusIdentity:
    return CorpusIdentity(
        dataset_name="example/trajectory-benchmark",
        dataset_revision="dataset-rev-semantic",
        harness_commit="harness-commit-1",
        tier="small",
        domain="web",
        ingest_config_digest="trajectory-semantic-test-v1",
    )


def _source(
    asset_root: Path,
    *,
    trajectory_id: str,
    ordinal: int,
    goal: str,
    texts: tuple[str, ...],
) -> TrajectorySource:
    states = []
    for index, text in enumerate(texts):
        screenshot = asset_root / f"{trajectory_id}-{index}.png"
        screenshot.write_bytes(b"png" + hashlib.sha256(text.encode()).digest())
        states.append(TrajectoryState(
            state_index=index,
            step=index,
            url=f"https://example.test/{trajectory_id}/{index}",
            incoming_action=None if index == 0 else f"advance {index}",
            thoughts=f"inspect state {index}",
            text=text,
            screenshot_path=screenshot,
        ))
    return TrajectorySource(
        trajectory_id=trajectory_id,
        ordinal=ordinal,
        goal=goal,
        start_url=f"https://example.test/{trajectory_id}",
        outcome="completed",
        states=tuple(states),
        source_payload={
            "id": trajectory_id,
            "goal": goal,
            "states": [state.text for state in states],
        },
    )


def _build_store(tmp_path: Path, provider: FakeEmbeddingProvider | None = None):
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    store = TrajectoryStore(
        tmp_path / "lcm.db",
        _identity(),
        asset_root=asset_root,
        embedding_provider=provider,
        semantic_top_trajectories=4,
    )
    return asset_root, store


def test_semantic_index_is_same_db_idempotent_and_backup_safe(tmp_path: Path):
    provider = FakeEmbeddingProvider()
    asset_root, store = _build_store(tmp_path, provider)
    store.insert(_source(
        asset_root,
        trajectory_id="target",
        ordinal=0,
        goal="Inspect profile toolbar settings",
        texts=("Profile toolbar contains Edit and Delete.",),
    ))
    store.insert(_source(
        asset_root,
        trajectory_id="other",
        ordinal=1,
        goal="Inspect orders",
        texts=("Orders toolbar contains Export.",),
    ))
    store.finalize(["target", "other"])

    first = store.build_semantic_index()
    second = store.build_semantic_index()
    assert first["status"] == "built"
    assert second["status"] == "current"
    assert provider.document_calls == 1
    assert store.connection.execute(
        "SELECT COUNT(*) FROM lcm_trajectory_embeddings"
    ).fetchone()[0] == 2
    assert store.manifest()["semantic_index"]["document_count"] == 2

    backup = tmp_path / "backup.db"
    store.backup_to(backup)
    store.close()
    restored = TrajectoryStore(
        backup,
        _identity(),
        asset_root=asset_root,
        read_only=True,
        embedding_provider=provider,
    )
    try:
        assert restored.manifest()["semantic_index"]["document_count"] == 2
        assert restored.query("profile toolbar", limit=2)[0].trajectory_id == "target"
    finally:
        restored.close()


def test_semantic_trajectory_selection_beats_global_lexical_distractors(tmp_path: Path):
    provider = FakeEmbeddingProvider()
    asset_root, store = _build_store(tmp_path, provider)
    target = _source(
        asset_root,
        trajectory_id="target",
        ordinal=0,
        goal="Inspect profile toolbar settings",
        texts=(
            "Profile page.",
            "The profile toolbar has View, Edit, and Delete actions.",
            "Return to the customer profile.",
        ),
    )
    store.insert(target)
    ordered = ["target"]
    for index in range(12):
        trajectory_id = f"distractor-{index:02d}"
        store.insert(_source(
            asset_root,
            trajectory_id=trajectory_id,
            ordinal=index + 1,
            goal="Inspect order queue",
            texts=(
                "Toolbar toolbar toolbar on an unrelated order queue.",
                "Toolbar menu for exporting orders.",
            ),
        ))
        ordered.append(trajectory_id)
    store.finalize(ordered)
    store.build_semantic_index()

    hits = store.query("Which actions are in the profile toolbar?", limit=6, image_limit=3)
    assert hits[0].trajectory_id == "target"
    assert any(
        hit.trajectory_id == "target" and "View, Edit, and Delete" in hit.text
        for hit in hits
    )
    assert all(hit.screenshot_path for hit in hits[:3])
    assert provider.query_calls == 1
    assert store.semantic_metrics()["query_calls"] == 1


def test_adjacent_slots_are_reserved_even_when_fts_fills_the_limit(tmp_path: Path):
    provider = FakeEmbeddingProvider()
    asset_root, store = _build_store(tmp_path, provider)
    store.insert(_source(
        asset_root,
        trajectory_id="target",
        ordinal=0,
        goal="Inspect profile toolbar workflow",
        texts=(
            "Before the toolbar action, choose the customer profile.",
            "Toolbar target state with the profile toolbar.",
            "After the toolbar action, the confirmation banner appears.",
            "Toolbar unrelated repeated term.",
            "Toolbar another repeated term.",
            "Toolbar final repeated term.",
        ),
    ))
    store.finalize(["target"])
    store.build_semantic_index()

    hits = store.query("profile toolbar", limit=4, image_limit=4, include_adjacent=True)
    assert len(hits) == 4
    assert any(hit.match_kind == "adjacent" for hit in hits)
    assert any("Before the toolbar action" in hit.text for hit in hits)
    assert len({hit.exact_ref for hit in hits}) == len(hits)


def test_semantic_failure_falls_back_to_original_exact_fts(tmp_path: Path):
    provider = FakeEmbeddingProvider(fail_query=True)
    asset_root, store = _build_store(tmp_path, provider)
    store.insert(_source(
        asset_root,
        trajectory_id="target",
        ordinal=0,
        goal="Inspect profile toolbar settings",
        texts=("The profile toolbar contains Edit.",),
    ))
    store.finalize(["target"])
    store.build_semantic_index()
    hits = store.query("profile toolbar", limit=2)
    assert hits
    assert hits[0].trajectory_id == "target"
    assert hits[0].match_kind == "fts"
    assert store.semantic_metrics()["fallbacks"] == 1


def test_query_records_typed_success_attempt_and_telemetry(tmp_path: Path):
    provider = FakeEmbeddingProvider()
    asset_root, store = _build_store(tmp_path, provider)
    store.insert(_source(
        asset_root,
        trajectory_id="target",
        ordinal=0,
        goal="Inspect profile toolbar settings",
        texts=("The profile toolbar has View, Edit, and Delete actions.",),
    ))
    store.finalize(["target"])
    store.build_semantic_index()

    hits = store.query("profile toolbar actions", limit=2)

    attempt = store.last_semantic_attempt()
    assert attempt is not None
    assert attempt["outcome"] == "success"
    assert attempt["provider"] == "fake"
    assert attempt["model"] == "fake-trajectory-v1"
    assert attempt["exception_class"] is None
    assert attempt["reason"] is None
    assert attempt["latency_ms"] is not None and attempt["latency_ms"] >= 0.0

    counters = store.semantic_attempt_counters()
    assert counters == {
        "attempts": 1,
        "successes": 1,
        "fallbacks": 0,
        "fallbacks_by_reason": {},
    }

    telemetry = store.last_query_telemetry()
    assert telemetry is not None
    assert telemetry["semantic_attempt"] == attempt
    assert telemetry["source_candidate_ranks"]
    assert telemetry["source_candidate_ranks"][0]["rank"] == 1
    assert {"source_id", "rank", "score"} <= telemetry["source_candidate_ranks"][0].keys()
    assert telemetry["state_candidate_pool"]
    assert {"state_id", "rank", "score"} <= telemetry["state_candidate_pool"][0].keys()
    # Delivered refs mirror the returned hits exactly (byte-identical evidence).
    assert telemetry["delivered_evidence_refs"] == [hit.exact_ref for hit in hits]


def test_query_fallback_records_typed_reason_not_bare_counter(tmp_path: Path):
    # The historical bare ``except Exception: fallbacks += 1`` discarded the
    # failure class. The typed record must now survive alongside the counter.
    provider = FakeEmbeddingProvider(fail_query=True)  # raises RuntimeError
    asset_root, store = _build_store(tmp_path, provider)
    store.insert(_source(
        asset_root,
        trajectory_id="target",
        ordinal=0,
        goal="Inspect profile toolbar settings",
        texts=("The profile toolbar contains Edit.",),
    ))
    store.finalize(["target"])
    store.build_semantic_index()

    store.query("profile toolbar", limit=2)

    assert store.semantic_metrics()["fallbacks"] == 1  # legacy counter unchanged
    attempt = store.last_semantic_attempt()
    assert attempt is not None
    assert attempt["outcome"] == "fallback"
    assert attempt["exception_class"] == "RuntimeError"
    assert attempt["reason"] == "other"
    assert store.semantic_attempt_counters() == {
        "attempts": 1,
        "successes": 0,
        "fallbacks": 1,
        "fallbacks_by_reason": {"other": 1},
    }


def test_query_fallback_classifies_provider_and_guard_failures(tmp_path: Path):
    class _VoyageLikeRateLimit(RuntimeError):
        kind = "rate_limit"
        status_code = 429

    class ProviderRateLimited(RuntimeError):
        """Name-matched to the product's client-side spend-guard exception."""

    class _ClassifyingProvider(FakeEmbeddingProvider):
        def __init__(self, exc: Exception) -> None:
            super().__init__()
            self._exc = exc

        def embed_query(self, text):
            self.query_calls += 1
            raise self._exc

    for exc, expected_reason, expected_status in (
        (_VoyageLikeRateLimit("429"), "rate_limit", 429),
        (ProviderRateLimited("guard cooling down"), "client_rate_guard", None),
    ):
        provider = _ClassifyingProvider(exc)
        base = tmp_path / expected_reason
        base.mkdir()
        asset_root, store = _build_store(base, provider)
        store.insert(_source(
            asset_root,
            trajectory_id="target",
            ordinal=0,
            goal="Inspect profile toolbar settings",
            texts=("The profile toolbar contains Edit.",),
        ))
        store.finalize(["target"])
        store.build_semantic_index()
        store.query("profile toolbar", limit=2)
        attempt = store.last_semantic_attempt()
        assert attempt is not None
        assert attempt["outcome"] == "fallback"
        assert attempt["reason"] == expected_reason
        assert attempt["http_status"] == expected_status


def test_query_survives_exceptions_with_raising_introspection_properties(tmp_path: Path):
    # P1 regression: an exception whose kind/status_code/retry_after are
    # PROPERTIES THAT RAISE must not crash query(); it must return FTS-only
    # hits and bump the fallback counter for each.
    class _EvilKind(RuntimeError):
        @property
        def kind(self):
            raise ValueError("boom")

    class _EvilStatus(RuntimeError):
        @property
        def status_code(self):
            raise ZeroDivisionError("boom")

    class _EvilRetry(RuntimeError):
        @property
        def retry_after(self):
            raise KeyError("boom")

    class _EvilProvider(FakeEmbeddingProvider):
        def __init__(self, exc_cls) -> None:
            super().__init__()
            self._exc_cls = exc_cls

        def embed_query(self, text):
            self.query_calls += 1
            raise self._exc_cls("exotic provider failure")

    for label, exc_cls in (("kind", _EvilKind), ("status", _EvilStatus), ("retry", _EvilRetry)):
        base = tmp_path / label
        base.mkdir()
        asset_root, store = _build_store(base, _EvilProvider(exc_cls))
        store.insert(_source(
            asset_root,
            trajectory_id="target",
            ordinal=0,
            goal="Inspect profile toolbar settings",
            texts=("The profile toolbar contains Edit.",),
        ))
        store.finalize(["target"])
        store.build_semantic_index()

        hits = store.query("profile toolbar", limit=2)  # must NOT raise
        assert hits
        assert hits[0].match_kind == "fts"
        assert store.semantic_metrics()["fallbacks"] == 1
        counters = store.semantic_attempt_counters()
        assert counters["attempts"] == 1
        assert counters["fallbacks"] == 1
        assert store.last_semantic_attempt()["outcome"] == "fallback"


def test_semantic_attempt_log_is_bounded_but_counters_stay_cumulative(tmp_path: Path):
    # P2 regression: the attempt ring is capped (deque maxlen=1024) so a long
    # run cannot grow it without bound, while the funnel counters remain exact.
    provider = FakeEmbeddingProvider()
    asset_root, store = _build_store(tmp_path, provider)
    store.insert(_source(
        asset_root,
        trajectory_id="target",
        ordinal=0,
        goal="Inspect profile toolbar settings",
        texts=("The profile toolbar has View, Edit, and Delete actions.",),
    ))
    store.finalize(["target"])
    store.build_semantic_index()

    for _ in range(1100):
        store.query("profile toolbar actions", limit=2)

    assert len(store._semantic_attempts) <= 1024
    counters = store.semantic_attempt_counters()
    assert counters["attempts"] == 1100
    assert counters["successes"] == 1100
    assert counters["fallbacks"] == 0


def test_semantic_documents_use_protected_rows_not_raw_secrets(tmp_path: Path):
    provider = FakeEmbeddingProvider()
    asset_root, store = _build_store(tmp_path, provider)
    secret = "sk-proj-do-not-embed-this-secret-value"
    store.insert(_source(
        asset_root,
        trajectory_id="target",
        ordinal=0,
        goal="Inspect profile toolbar settings",
        texts=(f"profile toolbar api_key={secret}",),
    ))
    store.finalize(["target"])
    store.build_semantic_index()
    assert secret not in "\n".join(provider.documents)
    assert "[LCM sensitive redaction:" in "\n".join(provider.documents)


def test_semantic_index_rejects_changed_or_nonfinite_dimensions(tmp_path: Path):
    class BadProvider(FakeEmbeddingProvider):
        def embed_documents(self, texts):
            return [[1.0, 0.0], [float("nan")]]

    provider = BadProvider()
    asset_root, store = _build_store(tmp_path, provider)
    store.insert(_source(
        asset_root,
        trajectory_id="target",
        ordinal=0,
        goal="Inspect profile toolbar settings",
        texts=("profile toolbar",),
    ))
    store.insert(_source(
        asset_root,
        trajectory_id="other",
        ordinal=1,
        goal="Inspect orders",
        texts=("orders",),
    ))
    store.finalize(["target", "other"])
    with pytest.raises(ValueError, match="finite|dimension"):
        store.build_semantic_index()

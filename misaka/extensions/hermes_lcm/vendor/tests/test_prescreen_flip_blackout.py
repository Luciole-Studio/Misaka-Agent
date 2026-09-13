"""Prescreen-flip silent blackout (SPEC C7 FIX 1) regression tests.

Flipping ``LCM_EMBEDDING_BINARY_PRESCREEN`` on an already-populated float32
identity used to make ``_has_binary()`` true after ONE sign-bit row, routing
the whole identity onto the two-stage path whose INNER JOIN against
``lcm_embedding_binary`` silently excluded every pre-flip vector while still
reporting ``coverage='full'``. These assert the two guarantees the fix adds:

  (a) flipping the flag mints a NEW identity (a fresh, backfill-trackable
      corpus) rather than mutating the populated one in place -- the active
      identity honestly reports its own contents ('none' when fresh, never a
      dishonest 'full' hiding the pre-flip corpus); and
  (b) defense-in-depth: an identity whose binary corpus is only PARTIAL never
      takes the two-stage INNER-JOIN path (which would exclude the binary-less
      vectors); it falls back to the exact scan so every vector is scored.
"""
from __future__ import annotations


import hermes_lcm.vector_store as vector_store_module
from hermes_lcm.config import LCMConfig
from hermes_lcm.dag import SummaryDAG, SummaryNode
from hermes_lcm.vector_store import VectorStore, _pack_sign_bits


MODEL = "flip-model"
PROVIDER = "local"
DIM = 3


def _expire_after_first_prescreen_batch(deadline, scanned_rows):
    """Position-keyed deadline stub: expire once the first prescreen batch
    has completed (scanned_rows >= 1), regardless of clock call patterns."""
    return deadline is not None and scanned_rows >= 1


def _add_summary(dag: SummaryDAG, *, created_at: float) -> int:
    return dag.add_node(
        SummaryNode(
            session_id="conversation-a",
            summary=f"summary at {created_at}",
            source_token_count=100,
            created_at=created_at,
            earliest_at=created_at,
            latest_at=created_at,
        )
    )


def _record(store: VectorStore, node_id: int, vec) -> None:
    identity = store.capture_identity(MODEL)
    store.record_embedding(node_id, "summary", MODEL, vec, identity=identity)


def test_prescreen_flip_on_populated_identity_mints_new_identity(tmp_path):
    db_path = tmp_path / "vectors.db"
    dag = SummaryDAG(db_path)

    # Phase 1: prescreen OFF. Populate a float32 identity with three vectors
    # (no sign-bit rows written).
    off_config = LCMConfig(embedding_binary_prescreen=False)
    store_off = VectorStore(db_path, config=off_config)
    original_identity = store_off.register_profile(MODEL, PROVIDER, DIM)
    node_x = _add_summary(dag, created_at=1.0)
    node_diag = _add_summary(dag, created_at=2.0)
    node_y = _add_summary(dag, created_at=3.0)
    _record(store_off, node_x, [1.0, 0.0, 0.0])
    _record(store_off, node_diag, [1.0, 1.0, 0.0])
    _record(store_off, node_y, [0.0, 1.0, 0.0])

    pre_flip = store_off.knn([1.0, 0.0, 0.0], k=5, model=MODEL)
    assert pre_flip.coverage == "full"
    assert str(node_x) in {row[0] for row in pre_flip}
    store_off.close()

    # Phase 2: operator flips prescreen ON and re-registers (e.g. warmup on
    # restart). This must mint a NEW active identity -- NOT reactivate the
    # populated binary-less float32 one -- so the flag change requires a fresh,
    # backfill-trackable corpus (guarantee a).
    on_config = LCMConfig(embedding_binary_prescreen=True)
    store_on = VectorStore(db_path, config=on_config)
    flipped_identity = store_on.register_profile(MODEL, PROVIDER, DIM)
    assert flipped_identity != original_identity

    # The new active identity is empty -> honest coverage='none' (the caller
    # degrades to full_text / backfill re-embeds). The pre-flip corpus is NEVER
    # dishonestly reported as covered.
    result = store_on.knn([1.0, 0.0, 0.0], k=5, model=MODEL)
    assert result.coverage == "none"
    assert list(result) == []
    store_on.close()

    # The populated original identity is untouched: flipping the flag back off
    # and re-registering reactivates it, and every pre-flip vector is still there
    # (its vectors were never mutated -- no blackout, no re-backfill needed).
    store_off_again = VectorStore(db_path, config=off_config)
    try:
        reactivated = store_off_again.register_profile(MODEL, PROVIDER, DIM)
        assert reactivated == original_identity
        preserved = store_off_again.knn([1.0, 0.0, 0.0], k=5, model=MODEL)
        assert preserved.coverage == "full"
        assert str(node_x) in {row[0] for row in preserved}
    finally:
        store_off_again.close()
    dag.close()


def test_partial_binary_corpus_falls_back_to_exact_scan(tmp_path):
    """A float32 identity with SOME sign-bit rows scores EVERY vector.

    Directly seeds the mixed state the live-flip path can produce (writes under
    the same identity while the flag is toggled, before any re-register) and
    asserts the two-stage path is gated on a fully-synced binary corpus: partial
    binary -> exact scan, so the binary-less vectors are NOT silently excluded by
    the INNER JOIN.
    """
    db_path = tmp_path / "vectors.db"
    dag = SummaryDAG(db_path)
    config = LCMConfig(embedding_binary_prescreen=False)
    store = VectorStore(db_path, config=config)
    identity = store.register_profile(MODEL, PROVIDER, DIM)

    node_x = _add_summary(dag, created_at=1.0)
    node_y = _add_summary(dag, created_at=2.0)
    _record(store, node_x, [1.0, 0.0, 0.0])
    _record(store, node_y, [0.0, 1.0, 0.0])

    # Inject ONE sign-bit row directly, mimicking a single post-flip write into
    # the populated identity (vector-count=2, binary-count=1 -> partial).
    store.connection.execute(
        "INSERT INTO lcm_embedding_binary(embedded_id, identity_hash, bits) "
        "VALUES(?, ?, ?)",
        (str(node_y), identity, _pack_sign_bits([0.0, 1.0, 0.0])),
    )
    # The identity is NOT fully synced, so the two-stage path is declined.
    assert store._has_binary(identity, chunk=False) is True
    assert store._binary_fully_synced(identity, chunk=False) is False

    result = store.knn([1.0, 0.0, 0.0], k=5, model=MODEL)
    ids = {row[0] for row in result}
    # Both vectors are scored -- the binary-less node_x (the strongest match) is
    # NOT excluded by a partial INNER JOIN.
    assert str(node_x) in ids
    assert str(node_y) in ids
    assert result[0][0] == str(node_x)
    store.close()
    dag.close()


def test_scan_bounds_route_a_synced_binary_identity_to_the_exact_scan(tmp_path):
    """Review finding 3: a fully-synced binary identity returned ``full_approx``
    BEFORE the scan limits were consulted, so recall_scan_max_rows and
    recall_scan_budget_s were silently ignored on those profiles. A requested
    hard bound must reach every scan path and report bounded coverage."""
    db_path = tmp_path / "bounded-prescreen.db"
    dag = SummaryDAG(db_path)
    config = LCMConfig(embedding_binary_prescreen=True)
    store = VectorStore(db_path, config=config)
    store.register_profile(MODEL, PROVIDER, DIM)
    oldest = _add_summary(dag, created_at=1.0)
    _record(store, oldest, [1.0, 0.0, 0.0])
    for index in range(1, 4):
        _record(store, _add_summary(dag, created_at=1.0 + index), [0.0, 1.0, 0.0])
    assert store._binary_fully_synced(store._current_profile()["identity_hash"], chunk=False)

    # Unbounded: the two-stage path still runs and discloses its approximation.
    unbounded = store.knn([1.0, 0.0, 0.0], k=1, model=MODEL, full_scan=True)
    assert unbounded.coverage == "full_approx"

    capped = store.knn(
        [1.0, 0.0, 0.0], k=1, model=MODEL, full_scan=True, scan_max_rows=1
    )
    assert capped.coverage == "bounded"
    assert capped.scanned == 1
    assert capped.total == 4
    assert [row[0] for row in capped] != [str(oldest)]  # oldest is outside the cap

    budgeted = store.knn(
        [1.0, 0.0, 0.0], k=1, model=MODEL, full_scan=True, scan_budget_s=0.0001
    )
    assert budgeted.coverage in {"full", "bounded"}  # exact path, never full_approx
    store.close()
    dag.close()


def test_deadline_bounds_a_synced_binary_summary_prescreen(tmp_path, monkeypatch):
    """An expired deadline bounds the prescreen without changing its live path."""
    db_path = tmp_path / "deadline-prescreen.db"
    dag = SummaryDAG(db_path)
    store = VectorStore(
        db_path,
        config=LCMConfig(embedding_binary_prescreen=True),
        bounded_scan_rows=1,
    )
    store.register_profile(MODEL, PROVIDER, DIM)
    _record(store, _add_summary(dag, created_at=1.0), [1.0, 0.0, 0.0])
    _record(store, _add_summary(dag, created_at=2.0), [0.0, 1.0, 0.0])

    unbounded = store.knn([1.0, 0.0, 0.0], k=1, model=MODEL, full_scan=True)
    monkeypatch.setattr(
        vector_store_module,
        "_prescreen_deadline_expired",
        _expire_after_first_prescreen_batch,
    )
    monkeypatch.setattr(
        store,
        "_count_embedded_vectors",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("deadline expiry must not start COUNT(*)")
        ),
    )
    expired = store.knn(
        [1.0, 0.0, 0.0],
        k=1,
        model=MODEL,
        full_scan=True,
        # A genuinely-future deadline: only the patched position-keyed seam
        # trips; the real clock never expires the load path.
        deadline=vector_store_module._monotonic() + 60.0,
    )

    assert unbounded.coverage == "full_approx"
    assert expired.coverage == "bounded"
    assert expired.scanned == 1
    assert expired.total == 2
    store.close()
    dag.close()

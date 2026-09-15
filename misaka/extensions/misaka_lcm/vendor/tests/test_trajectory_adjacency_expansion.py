"""H5(b) lexical-seed adjacency pool-expansion (issue #135).

These exercise the candidate-RECALL pathology on a synthetic corpus: a target
state carries NO query term of its own (lexically invisible) while a sibling
state of the same trajectory seeds, so the target is unreachable at the pool
stage under current bytes. The ``adjacency_radius``/``adjacency_quota`` knob
must pull the invisible neighbor INTO the state pool as a quota-capped
additive arm, while the defaults must reproduce the pre-expansion pool and
delivery byte-for-byte, delivery must stay unchanged when the ranked pool
already fills the nucleus (additive-only proof), and the 5-per-trajectory
diversity cap at selection must be preserved.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from hermes_lcm.trajectory_store import (
    CorpusIdentity,
    TrajectorySource,
    TrajectoryState,
    TrajectoryStore,
)


class MagnetProvider:
    """Ranks any trajectory whose text mentions 'profile toolbar' as semantically
    top, regardless of the query -- the coarse whole-trajectory 'magnet'."""

    provider_id = "fake"
    model_id = "fake-trajectory-v1"
    dim = 2

    def __init__(self) -> None:
        self.last_usage_tokens = 0

    def embed_documents(self, texts):
        self.last_usage_tokens = sum(max(1, len(str(t)) // 4) for t in texts)
        return [
            [1.0, 0.0] if "profile toolbar" in str(t).casefold() else [0.0, 1.0]
            for t in texts
        ]

    def embed_query(self, text):  # noqa: ARG002
        self.last_usage_tokens = 1
        return [1.0, 0.0]


def _identity() -> CorpusIdentity:
    return CorpusIdentity(
        dataset_name="example/adjacency",
        dataset_revision="rev-adjacency",
        harness_commit="harness-adjacency-1",
        tier="small",
        domain="web",
        ingest_config_digest="adjacency-test-v1",
    )


def _source(asset_root, *, trajectory_id, ordinal, goal, texts) -> TrajectorySource:
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
        source_payload={"id": trajectory_id, "goal": goal},
    )


_QUERY = "widget configuration export"


def _state_id(store, trajectory_id: str, state_index: int) -> int:
    row = store._conn.execute(
        """
        SELECT s.state_id FROM lcm_trajectory_states s
        JOIN lcm_trajectory_sources src ON src.source_id = s.source_id
        WHERE src.trajectory_id = ? AND s.state_index = ?
        """,
        (trajectory_id, state_index),
    ).fetchone()
    assert row is not None, f"unknown state {trajectory_id}/{state_index}"
    return int(row[0])


def _pool_state_ids(store) -> set[int]:
    telemetry = store.last_query_telemetry()
    return {int(item["state_id"]) for item in telemetry["state_candidate_pool"]}


def _admitted(store) -> list[dict[str, int]]:
    telemetry = store.last_query_telemetry()
    expansion = telemetry.get("adjacency_expansion")
    return list(expansion["admitted"]) if expansion else []


def _build_invisible_neighbor_store(tmp_path: Path):
    """One trajectory whose answer state is lexically INVISIBLE (no query
    term) while its predecessor seeds; plus a second lexical trajectory."""
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    store = TrajectoryStore(tmp_path / "lcm.db", _identity(), asset_root=asset_root)
    store.insert(_source(
        asset_root,
        trajectory_id="answerpath",
        ordinal=0,
        goal="Update the account settings",
        texts=(
            "navigate homepage dashboard overview",          # 0: invisible, -1 of seed
            "widget configuration export panel form",        # 1: the lexical seed
            "save success banner shows view details link",   # 2: invisible ANSWER state
            "logout footer copyright notice",                # 3: invisible, +2 of seed
        ),
    ))
    store.insert(_source(
        asset_root,
        trajectory_id="othertask",
        ordinal=1,
        goal="Export the report",
        texts=("report export toolbar button",),
    ))
    store.finalize(["answerpath", "othertask"])
    return store


# --- default-off byte-identity ----------------------------------------------

def test_defaults_reproduce_current_bytes(tmp_path):
    store = _build_invisible_neighbor_store(tmp_path)
    baseline = store.query(_QUERY, image_limit=0)
    baseline_telemetry = store.last_query_telemetry()
    explicit_off = store.query(
        _QUERY, image_limit=0, adjacency_radius=0, adjacency_quota=0,
    )
    off_telemetry = store.last_query_telemetry()
    assert [h.exact_ref for h in explicit_off] == [h.exact_ref for h in baseline]
    assert off_telemetry == baseline_telemetry
    # No telemetry key leaks into the default payload (frozen-run byte parity).
    assert "adjacency_expansion" not in baseline_telemetry
    # Half-open knobs (radius without quota and vice versa) are also OFF.
    for kwargs in ({"adjacency_radius": 2}, {"adjacency_quota": 8}):
        hits = store.query(_QUERY, image_limit=0, **kwargs)
        assert [h.exact_ref for h in hits] == [h.exact_ref for h in baseline]
        assert "adjacency_expansion" not in store.last_query_telemetry()


# --- the core recall mechanism ----------------------------------------------

def test_expansion_pulls_lexically_invisible_neighbor_into_pool(tmp_path):
    store = _build_invisible_neighbor_store(tmp_path)
    answer = _state_id(store, "answerpath", 2)
    store.query(_QUERY, image_limit=0)
    assert answer not in _pool_state_ids(store), "answer state must start invisible"
    store.query(_QUERY, image_limit=0, adjacency_radius=1, adjacency_quota=8)
    assert answer in _pool_state_ids(store)
    seed = _state_id(store, "answerpath", 1)
    by_state = {entry["state_id"]: entry for entry in _admitted(store)}
    assert by_state[answer] == {"state_id": answer, "seed_state_id": seed, "distance": 1}


def test_radius_bounds_the_reach_and_orders_distance_major(tmp_path):
    store = _build_invisible_neighbor_store(tmp_path)
    far = _state_id(store, "answerpath", 3)  # +2 from the seed
    store.query(_QUERY, image_limit=0, adjacency_radius=1, adjacency_quota=8)
    assert far not in _pool_state_ids(store)
    store.query(_QUERY, image_limit=0, adjacency_radius=2, adjacency_quota=8)
    admitted = _admitted(store)
    assert far in {entry["state_id"] for entry in admitted}
    distances = [entry["distance"] for entry in admitted]
    assert distances == sorted(distances), "arm order must be distance-major"


def test_quota_caps_admissions(tmp_path):
    store = _build_invisible_neighbor_store(tmp_path)
    store.query(_QUERY, image_limit=0, adjacency_radius=2, adjacency_quota=1)
    admitted = _admitted(store)
    assert len(admitted) == 1
    # Distance-major: the single slot goes to a distance-1 neighbor.
    assert admitted[0]["distance"] == 1


def test_pool_incumbents_are_not_readmitted(tmp_path):
    """A neighbor that already entered the pool lexically is never duplicated."""
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    store = TrajectoryStore(tmp_path / "lcm.db", _identity(), asset_root=asset_root)
    store.insert(_source(
        asset_root,
        trajectory_id="allmatch",
        ordinal=0,
        goal="Finish the setup flow",
        texts=(
            "widget configuration export intro",
            "widget configuration export detail",
            "plain closing remark",  # only non-matching state
        ),
    ))
    store.finalize(["allmatch"])
    store.query(_QUERY, image_limit=0, adjacency_radius=2, adjacency_quota=8)
    admitted = _admitted(store)
    only = _state_id(store, "allmatch", 2)
    assert [entry["state_id"] for entry in admitted] == [only]
    pool = [
        int(item["state_id"])
        for item in store.last_query_telemetry()["state_candidate_pool"]
    ]
    assert len(pool) == len(set(pool)), "pool must stay duplicate-free"


# --- anti-filler / additive-only controls -------------------------------------

def test_delivery_unchanged_when_ranked_pool_fills_nucleus(tmp_path):
    """Additive-only proof on a full pool: the expanded states may enter the
    POOL but must not displace any delivered nucleus/backfill incumbent."""
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    store = TrajectoryStore(
        tmp_path / "lcm.db",
        _identity(),
        asset_root=asset_root,
        embedding_provider=MagnetProvider(),
        semantic_top_trajectories=12,
    )
    store.insert(_source(
        asset_root,
        trajectory_id="target",
        ordinal=0,
        goal="Set up the dashboard gadget",
        texts=(
            "widget configuration export panel with all three terms",
            "invisible aftermath confirmation banner",
        ),
    ))
    order = ["target"]
    for index in range(12):
        trajectory_id = f"distractor-{index:02d}"
        store.insert(_source(
            asset_root,
            trajectory_id=trajectory_id,
            ordinal=index + 1,
            goal="Inspect profile toolbar",
            texts=(
                f"profile toolbar export note {index}",
                f"quiet interstitial screen {index}",
            ),
        ))
        order.append(trajectory_id)
    store.finalize(order)
    store.build_semantic_index()

    baseline = [h.exact_ref for h in store.query(_QUERY, image_limit=0)]
    expanded = [
        h.exact_ref
        for h in store.query(
            _QUERY, image_limit=0, adjacency_radius=1, adjacency_quota=16,
        )
    ]
    assert expanded == baseline
    admitted = _admitted(store)
    assert admitted, "the pool itself must still gain adjacency entries"
    # The magnet's neighbors outrank the target's in the (seed-strength
    # ordered) arm, but a quota that clears the 12 distractors still pulls
    # the lexically invisible target state into the pool.
    invisible = _state_id(store, "target", 1)
    assert invisible in {entry["state_id"] for entry in admitted}


def test_five_per_trajectory_cap_preserved_at_selection(tmp_path):
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    store = TrajectoryStore(tmp_path / "lcm.db", _identity(), asset_root=asset_root)
    store.insert(_source(
        asset_root,
        trajectory_id="longtask",
        ordinal=0,
        goal="Complete the long procedure",
        texts=tuple(
            "widget configuration export summary step"
            if index == 3
            else f"quiet screen number {index}"
            for index in range(8)
        ),
    ))
    store.insert(_source(
        asset_root,
        trajectory_id="filler",
        ordinal=1,
        goal="Export widgets",
        texts=tuple(f"widget export list page {index}" for index in range(6)),
    ))
    store.finalize(["longtask", "filler"])
    hits = store.query(
        _QUERY,
        image_limit=0,
        include_adjacent=False,  # nucleus only: isolate the selection cap
        adjacency_radius=8,
        adjacency_quota=32,
    )
    per_trajectory: dict[str, int] = {}
    for hit in hits:
        per_trajectory[hit.trajectory_id] = per_trajectory.get(hit.trajectory_id, 0) + 1
    assert per_trajectory.get("longtask", 0) <= 5
    # ...even though MORE than 5 longtask states were admitted to the pool.
    longtask_admitted = [
        entry
        for entry in _admitted(store)
        if entry["state_id"] in {
            _state_id(store, "longtask", index) for index in range(8)
        }
    ]
    assert len(longtask_admitted) == 7


def test_expanded_states_selected_only_from_the_tail(tmp_path):
    """Expanded neighbors earn no semantic/BM25 rank: every ranked pool row
    still precedes every admitted adjacency row in the candidate pool."""
    store = _build_invisible_neighbor_store(tmp_path)
    store.query(_QUERY, image_limit=0, adjacency_radius=2, adjacency_quota=8)
    telemetry = store.last_query_telemetry()
    pool = [int(item["state_id"]) for item in telemetry["state_candidate_pool"]]
    admitted_ids = {entry["state_id"] for entry in _admitted(store)}
    ranked_positions = [
        index for index, state_id in enumerate(pool) if state_id not in admitted_ids
    ]
    admitted_positions = [
        index for index, state_id in enumerate(pool) if state_id in admitted_ids
    ]
    assert admitted_positions, "expanded states must be visible in the pool"
    assert max(ranked_positions) < min(admitted_positions)

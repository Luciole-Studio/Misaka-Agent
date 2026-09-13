"""W3b compact-delivery components: C1 diversity, C2 excerpts, C3 compilation."""

from __future__ import annotations

import hashlib
from pathlib import Path

from hermes_lcm.tokens import count_tokens
from hermes_lcm.trajectory_store import (
    CorpusIdentity,
    TrajectorySource,
    TrajectoryState,
    TrajectoryStore,
)


class StateProvider:
    provider_id = "fake"
    model_id = "fake-state-v1"
    dim = 3

    def __init__(self) -> None:
        self.last_usage_tokens = 0

    @staticmethod
    def _vector(text: str) -> list[float]:
        folded = str(text).casefold()
        if "alpha-answer" in folded:
            return [1.0, 0.0, 0.0]
        if "beta-answer" in folded:
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]

    def embed_documents(self, texts):
        self.last_usage_tokens = len(texts)
        return [self._vector(text) for text in texts]

    def embed_query(self, text):  # noqa: ARG002
        self.last_usage_tokens = 1
        return [1.0, 0.0, 0.0]


def _identity(domain: str = "web") -> CorpusIdentity:
    return CorpusIdentity(
        dataset_name="example/w3b",
        dataset_revision="rev-w3b",
        harness_commit="harness-w3b-1",
        tier="small",
        domain=domain,
        ingest_config_digest="w3b-test-v1",
    )


def _source(
    asset_root: Path,
    *,
    trajectory_id: str,
    ordinal: int,
    goal: str,
    texts: tuple[str, ...],
    urls: tuple[str, ...] | None = None,
) -> TrajectorySource:
    states = []
    for index, text in enumerate(texts):
        screenshot = asset_root / f"{trajectory_id}-{index}.png"
        screenshot.write_bytes(b"png" + hashlib.sha256(text.encode()).digest())
        states.append(TrajectoryState(
            state_index=index,
            step=index,
            url=(
                urls[index]
                if urls is not None
                else f"https://example.test/{trajectory_id}/{index}"
            ),
            incoming_action=None if index == 0 else f"click step {index}",
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


def _build_store(tmp_path: Path, *, provider=None) -> TrajectoryStore:
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    store = TrajectoryStore(
        tmp_path / "lcm.db",
        _identity(),
        asset_root=asset_root,
        embedding_provider=provider,
    )
    store.insert(_source(
        asset_root,
        trajectory_id="hub",
        ordinal=0,
        goal="Generic export dashboard",
        texts=tuple(
            f"widget configuration export repeated dashboard panel {index}"
            for index in range(7)
        ),
    ))
    store.insert(_source(
        asset_root,
        trajectory_id="target",
        ordinal=1,
        goal="Problem List Cleanup",
        texts=(
            "widget export procedure overview",
            "alpha-answer duplicate description field exact instruction",
        ),
    ))
    store.insert(_source(
        asset_root,
        trajectory_id="other",
        ordinal=2,
        goal="Open another report",
        texts=("widget export report toolbar",),
    ))
    store.finalize(["hub", "target", "other"])
    return store


def test_all_w3b_knobs_default_off_preserve_bytes_and_telemetry(tmp_path: Path):
    store = _build_store(tmp_path)
    query = "widget configuration export"
    baseline = store.query(query, image_limit=0)
    baseline_payload = [hit.to_dict() for hit in baseline]
    baseline_telemetry = store.last_query_telemetry()

    explicit_off = store.query(
        query,
        image_limit=0,
        diversity_cap=0,
        adaptive_excerpt=False,
        sharp_token_budget=0,
        antiboilerplate=False,
        title_boost=False,
    )
    assert [hit.to_dict() for hit in explicit_off] == baseline_payload
    assert store.last_query_telemetry() == baseline_telemetry
    assert "diversity_cap" not in baseline_telemetry
    assert "adaptive_excerpt" not in baseline_telemetry
    assert "sharp_compilation" not in baseline_telemetry
    assert "title_boost" not in baseline_telemetry


def test_c1_caps_after_state_arm_composition_and_reports_trajectory(tmp_path: Path):
    provider = StateProvider()
    store = _build_store(tmp_path, provider=provider)
    store.build_state_semantic_index(provider)

    hits = store.query(
        "widget configuration export",
        image_limit=0,
        include_adjacent=False,
        state_semantic_quota=8,
        diversity_cap=2,
    )
    per_trajectory: dict[str, int] = {}
    for hit in hits:
        per_trajectory[hit.trajectory_id] = (
            per_trajectory.get(hit.trajectory_id, 0) + 1
        )
    assert max(per_trajectory.values()) <= 2
    assert any("alpha-answer" in hit.text for hit in hits)

    telemetry = store.last_query_telemetry()
    assert telemetry["state_semantic_expansion"]["admitted"]
    cap = telemetry["diversity_cap"]
    assert cap["cap"] == 2
    hub = next(
        entry for entry in cap["trajectories"]
        if entry["trajectory_id"] == "hub"
    )
    assert hub["before"] == 7
    assert hub["after"] == 2
    assert hub["capped_out"] == 5


def test_c2_densest_window_anchors_rare_query_term_and_shifts_budget():
    prefix = ("Low Stock Report dashboard navigation report " * 100)
    needle = "TABLE HEADER Quantity Source Code Scope"
    suffix = (" report footer " * 100)
    text = prefix + needle + suffix
    excerpt, offset = TrajectoryStore._densest_exact_excerpt(
        text,
        "Which column is next to Quantity in the Low Stock Report?",
        500,
    )
    assert "Quantity Source Code" in excerpt
    assert offset > 0

    rows = [
        {
            "state_id": 1,
            "trajectory_id": "repeat",
            "text": "x" * 4_000,
        },
        {
            "state_id": 2,
            "trajectory_id": "repeat",
            "text": "y" * 4_000,
        },
        {
            "state_id": 3,
            "trajectory_id": "sole",
            "text": "z" * 4_000,
        },
    ]
    limits, telemetry = TrajectoryStore._adaptive_excerpt_limits(
        rows, 1_000, True
    )
    assert limits[1] == limits[2] == 750
    assert limits[3] == 1_500
    assert sum(limits.values()) == 3_000
    assert telemetry["raised_only_hits"] == [{"state_id": 3, "chars": 1500}]


def test_c3_typed_queries_exact_title_template_and_budget(tmp_path: Path):
    store = _build_store(tmp_path)
    budget = 900
    hits = store.query(
        (
            "According to company protocol `Problem List Cleanup`, what should "
            "we change in the duplicate description field before deletion?"
        ),
        image_limit=0,
        include_adjacent=False,
        sharp_token_budget=budget,
    )
    assert hits
    assert hits[0].trajectory_id == "target"
    rendered_tokens = sum(
        count_tokens(TrajectoryStore._rendered_hit_text(hit)) for hit in hits
    )
    assert rendered_tokens <= budget

    sharp = store.last_query_telemetry()["sharp_compilation"]
    assert sharp["question_template"] == "procedure"
    assert {entry["pool_type"] for entry in sharp["subqueries"]} >= {
        "raw_state",
        "entity",
        "action",
    }
    assert sharp["exact_title_boosts"]
    assert sharp["rendered_text_tokens_after"] <= budget


def test_question_template_matches_temporal_terms_as_whole_tokens():
    assert (
        TrajectoryStore._question_template("How do I update account settings?")
        == "generic"
    )
    assert (
        TrajectoryStore._question_template("What changed after yesterday?")
        == "temporal"
    )


def _cap_row(
    state_id: int,
    trajectory_id: str,
    goal: str,
    text: str,
) -> dict[str, object]:
    return {
        "state_id": state_id,
        "trajectory_id": trajectory_id,
        "goal": goal,
        "url": "",
        "incoming_action": None,
        "text": text,
    }


def _t1_selected(cap_telemetry: dict) -> list[int]:
    entry = next(
        item
        for item in cap_telemetry["trajectories"]
        if item["trajectory_id"] == "t1"
    )
    return list(entry["selected_state_ids"])


def test_g_antiboilerplate_reweights_c1_survivor_selection():
    # One trajectory ("t1") with two near-identical task-header boilerplate
    # states ranked ahead of a distinct, query-relevant needle state. Padding
    # states from a second trajectory shrink the per-position relevance gap so
    # the boilerplate/density signals -- not raw rank -- decide the survivor.
    rows = [
        _cap_row(
            1, "t1", "export dashboard",
            "export dashboard task header repeated boilerplate navigation",
        ),
        _cap_row(
            2, "t1", "export dashboard",
            "export dashboard task header repeated boilerplate navigation footer",
        ),
        _cap_row(
            3, "t1", "export dashboard",
            "quarterly revenue figure answer distinct value column",
        ),
    ]
    rows += [
        _cap_row(100 + index, "pad", "unrelated pad", f"pad body token {index}")
        for index in range(12)
    ]
    query_terms = TrajectoryStore._query_term_set(
        "quarterly revenue figure answer column"
    )

    off_rows, off_telemetry = TrajectoryStore._cap_composed_pool(rows, 1)
    assert _t1_selected(off_telemetry) == [1]
    assert "antiboilerplate" not in off_telemetry

    # Default-off keyword form is byte-identical to the positional default.
    off_rows_kw, off_telemetry_kw = TrajectoryStore._cap_composed_pool(
        rows, 1, antiboilerplate=False, query_terms=query_terms
    )
    assert [int(row["state_id"]) for row in off_rows_kw] == [
        int(row["state_id"]) for row in off_rows
    ]
    assert off_telemetry_kw == off_telemetry

    on_rows, on_telemetry = TrajectoryStore._cap_composed_pool(
        rows, 1, antiboilerplate=True, query_terms=query_terms
    )
    assert _t1_selected(on_telemetry) == [3]
    assert 3 in {int(row["state_id"]) for row in on_rows}

    scored = {
        entry["state_id"]: entry
        for entry in on_telemetry["antiboilerplate"]["scored"]
    }
    # Needle has the query-term density; boilerplate siblings resemble each other.
    assert scored[3]["density"] > scored[1]["density"]
    assert scored[1]["boilerplate"] > scored[3]["boilerplate"]
    assert scored[2]["boilerplate"] > scored[3]["boilerplate"]


def test_h_title_boost_promotes_exact_ngram_matches():
    rows = [
        {
            "state_id": 1,
            "trajectory_id": "a",
            "goal": "generic dashboard",
            "url": "https://example.test/a/0",
            "text": "some unrelated body text about panels and widgets",
        },
        {
            "state_id": 2,
            "trajectory_id": "b",
            "goal": "products grid",
            "url": "https://example.test/b/0",
            "text": "column header Last Updated At value 2024-01-02",
        },
        {
            "state_id": 3,
            "trajectory_id": "c",
            "goal": "orders",
            "url": "https://example.test/c/0",
            "text": "purchase date column with totals",
        },
    ]
    reordered, telemetry = TrajectoryStore._apply_title_boost(
        rows, "What is the Last Updated At column value?"
    )
    assert [int(row["state_id"]) for row in reordered][0] == 2
    assert telemetry["boosted_count"] == 1
    assert telemetry["boosted"][0]["state_id"] == 2
    assert "last updated at" in telemetry["boosted"][0]["phrases"]

    # No matching phrase leaves the pool order byte-identical.
    same, empty = TrajectoryStore._apply_title_boost(
        rows, "completely orthogonal unrelated inquiry"
    )
    assert [int(row["state_id"]) for row in same] == [
        int(row["state_id"]) for row in rows
    ]
    assert empty["boosted_count"] == 0


def test_h_title_boost_query_path_emits_telemetry(tmp_path: Path):
    store = _build_store(tmp_path)
    query = "widget export duplicate description field"

    off = store.query(query, image_limit=0, include_adjacent=False)
    off_payload = [hit.to_dict() for hit in off]
    assert "title_boost" not in store.last_query_telemetry()

    on = store.query(
        query, image_limit=0, include_adjacent=False, title_boost=True
    )
    telemetry = store.last_query_telemetry()["title_boost"]
    assert telemetry["boosted_count"] >= 1
    assert any(
        "duplicate description field" in " ".join(entry["phrases"])
        for entry in telemetry["boosted"]
    )
    # The exact-phrase state is delivered under the boost.
    assert any("alpha-answer" in hit.text for hit in on)
    # Default-off path is unaffected by adding the knob at call time.
    reconfirm = store.query(query, image_limit=0, include_adjacent=False)
    assert [hit.to_dict() for hit in reconfirm] == off_payload

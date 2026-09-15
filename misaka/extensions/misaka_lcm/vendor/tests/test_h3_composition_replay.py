from __future__ import annotations

from benchmarking.h3_composition_replay import measure_ceiling


class _CeilingContext:
    def global_top_state_ids(self, qid, limit=128):
        assert qid == "q1"
        assert limit == 128
        return {1}

    def fused_candidate_state_ids(self, qid, **kwargs):
        assert qid == "q1"
        assert kwargs == {"arm_quota": (6, 5)}
        return {1, 2}

    def ref_to_state_id(self, qid, ref):
        assert qid == "q1"
        return {"trajectory://test/a/state/0": 2}[ref]


def test_policy_d_ceiling_includes_scoped_fused_candidates():
    targets = {
        "q1": {
            "vanished": ["trajectory://test/a/state/0"],
            "bucket": "composition",
        }
    }

    global_ceiling = measure_ceiling(_CeilingContext(), targets)
    policy_d_ceiling = measure_ceiling(
        _CeilingContext(),
        targets,
        knob_kwargs={"arm_quota": (6, 5)},
    )

    assert global_ceiling["recoverable"] == 0
    assert policy_d_ceiling["recoverable"] == 1

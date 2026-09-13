from __future__ import annotations

import pytest

from benchmarking import state_embedding_backfill


def test_resolve_rate_rejects_an_unknown_model_without_an_assumption():
    with pytest.raises(ValueError, match="no pricing entry"):
        state_embedding_backfill._resolve_rate("voyage-future", None)


def test_resolve_rate_accepts_an_explicit_assumption_for_an_unknown_model():
    assert state_embedding_backfill._resolve_rate("voyage-future", 0.25) == 0.25


def test_resolve_rate_keeps_canonical_rate_for_a_known_model():
    assert state_embedding_backfill._resolve_rate("voyage-4-large", None) == 0.12

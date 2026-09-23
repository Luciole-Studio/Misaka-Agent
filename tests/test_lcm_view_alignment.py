"""LCM reconciles pi's live context with the transcript instead of refusing the turn.

2026-09-18 (B11): a model switch mid-turn aborted the turn; from then on every request in
that session failed with "LCM request messages do not belong to the transcript snapshot
(first_mismatch=43)" -- the aborted message -- until the card was restarted in a new
process. Aborted tails are now droppable like errored ones, and a residual disagreement is
logged and reconciled rather than raised."""
import logging

import pytest

from misaka.extensions.misaka_lcm.host import context_engine, ingest


def _user(text, ts=1):
    return {"role": "user", "content": [{"type": "text", "text": text}], "timestamp": ts}


_USAGE = {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 2,
          "cost": {"input": 0.0, "output": 0.0, "cacheRead": 0.0, "cacheWrite": 0.0, "total": 0.0}}


def _assistant(text, stop="stop", ts=2):
    return {"role": "assistant", "content": [{"type": "text", "text": text}], "stopReason": stop,
            "errorMessage": None if stop == "stop" else "Operation aborted", "timestamp": ts,
            "api": "anthropic-messages", "provider": "test", "model": "test-model", "usage": _USAGE}


def test_an_aborted_tail_missing_from_the_live_view_is_tolerated():
    saved = [_user("q"), _assistant("", stop="aborted"), _user("go on", ts=3), _assistant("done", ts=4)]
    live = [saved[0], saved[2], saved[3]]
    assert ingest.align_sources(live, saved) == [0, 2, 3]
    assert ingest.source_indices(live, saved) == [0, 2, 3]


def test_a_successful_message_can_never_be_skipped():
    saved = [_user("q"), _assistant("a1"), _user("q2", ts=3), _assistant("a2", ts=4)]
    live = [saved[0], saved[2], saved[3]]      # a1 is not droppable
    assert ingest.align_sources(live, saved) is None
    with pytest.raises(ValueError, match="first_mismatch=1"):
        ingest.source_indices(live, saved)


def test_describe_mismatch_shows_both_sides():
    saved = [_user("q"), _assistant("", stop="aborted")]
    live = [_user("q"), _assistant("partial answer", stop="aborted")]
    text = ingest.describe_mismatch(live, saved)
    assert "active=2, snapshot=2, first_mismatch=1" in text
    assert "live: role=assistant stop=aborted" in text and "partial answer" in text
    assert "saved: role=assistant stop=aborted" in text


def test_same_length_disagreement_keeps_the_live_view(caplog):
    saved = [_user("q"), _assistant("", stop="aborted"), _user("go on", ts=3)]
    live = [_user("q"), _assistant("partial", stop="aborted"), _user("go on", ts=3)]
    with caplog.at_level(logging.WARNING, logger=context_engine.logger.name):
        native, positions = context_engine._aligned(live, saved)
    assert native is live
    assert positions == [0, 1, 2]
    assert any("keeping the live messages" in r.getMessage() and "first_mismatch=1" in r.getMessage()
               for r in caplog.records)


def test_length_disagreement_falls_back_to_the_transcript(caplog):
    saved = [_user("q"), _assistant("a1"), _user("q2", ts=3), _assistant("a2", ts=4)]
    live = [saved[0], saved[2], saved[3]]
    with caplog.at_level(logging.WARNING, logger=context_engine.logger.name):
        native, positions = context_engine._aligned(live, saved)
    assert native is saved
    assert positions == [0, 1, 2, 3]
    assert any("sending the transcript view" in r.getMessage() for r in caplog.records)


def test_an_aligned_view_is_passed_through_silently(caplog):
    saved = [_user("q"), _assistant("a1")]
    live = list(saved)
    with caplog.at_level(logging.WARNING, logger=context_engine.logger.name):
        native, positions = context_engine._aligned(live, saved)
    assert native is live
    assert positions == [0, 1]
    assert not caplog.records

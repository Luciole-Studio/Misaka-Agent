"""The generic vocabulary a part puts in a custom message's details and the context engine
reads (core/moments MEMORY, TURN): neither side names the other's message types."""

from __future__ import annotations

from misaka.agent.harness.messages import create_custom_message
from misaka.ai.types import UserMessage
from misaka.core.moments import MEMORY, TURN
from misaka.extensions.misaka_lcm.host import ingest


def custom(kind, content, details=None):
    return create_custom_message(kind, content, True, details, 1_700_000_000_000)


def create_user_message(text, timestamp):
    return UserMessage(role="user", content=text, timestamp=timestamp)


def test_a_feed_entry_marked_not_memory_never_reaches_the_archive():
    messages = [
        create_user_message("question", 1_700_000_000_000),
        custom("any-feed", "Research `r_1` | Task status: running 2.", {MEMORY: False}),
        custom("any-feed", "Card done: read it.", {"stage": "done"}),
        custom("any-feed", "unmarked", None),
    ]
    archived = [ingest._text_of(message["content"]) for message in ingest.upstream_messages(messages)]
    assert archived == ["question", "Card done: read it.", "unmarked"]
    # The same rule on the alignment projection, so live view and originals agree per entry.
    assert ingest.source_messages([messages[1]]) == []
    assert len(ingest.source_messages([messages[2]])) == 1


def test_a_custom_message_opens_a_turn_only_when_its_sender_says_so():
    assert ingest.is_task_message(create_user_message("hi", 1_700_000_000_000))
    assert ingest.is_task_message(custom("work-order", "plan this", {TURN: True}))
    assert not ingest.is_task_message(custom("work-order", "plan this", {"stage": "plan"}))
    assert not ingest.is_task_message(custom("research-phase", "the type alone no longer counts", None))
    assert not ingest.is_task_message(custom("feed", "tick", {MEMORY: False}))


def test_research_ticks_are_marked_not_memory_and_phase_prompts_open_a_turn():
    """The research part speaks the vocabulary: its status ticks carry MEMORY False (the same
    stages its own context hook drops), and nothing else it sends does."""
    import inspect

    from misaka.core.research import window
    from misaka.core.research.wiring import research

    source = inspect.getsource(research.ResearchPart.__init__)
    assert 'if payload.get("stage") in TICK_STAGES:' in source and "payload[MEMORY] = False" in source
    assert "TURN: True" in inspect.getsource(window)


def test_the_replay_join_numbers_sources_the_way_the_archive_does():
    """A feed entry marked not memory sits in the transcript but not in the engine's view.
    The join that restores the engine's whole-context replacement must skip it the same way,
    or every source ordinal after it is off by one: the live view then never matches the
    replay (a cleanup compaction on every turn), and a restore drops the wrong message
    (2026-09-22: a research phase prompt vanished on a retry; a run paused with
    "did not call misaka_research_investigate")."""
    messages = [
        create_user_message("question", 1_700_000_000_000),
        custom("feed", "tick 1", {MEMORY: False}),
        custom("feed", "tick 2", {MEMORY: False}),
        custom("work-order", "# Research plan", {TURN: True}),
    ]
    replay = ingest.Replay(messages)
    view = ingest.upstream_messages(messages)
    # prepare() zips the join's rows with positions computed over the archive's view: the two
    # must be the same length and describe the same rows.
    assert len(replay.messages) == len(view) == 2
    assert [ingest._text_of(m["content"]) for m in replay.messages] == ["question", "# Research plan"]
    joined = [{**row, "content": view[i]["content"]} for i, row in enumerate(replay.messages)]
    assert replay.unchanged(joined)
    restored = replay.restore(joined)
    texts = [ingest._text_of(m["content"] if isinstance(m, dict) else m.content) for m in restored]
    assert texts == ["question", "# Research plan"]
    assert (restored[1]["customType"] if isinstance(restored[1], dict) else restored[1].customType) == "work-order"

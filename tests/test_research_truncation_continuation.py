"""A research reply cut at the output cap is continued the way Hermes continues one (issue #13, B3).

The run in the report used a model with an 8,192-token output cap; the node's conclusion needed
some 36 KB, so every synthesis stopped at the cap and the run failed. Hermes
(``agent/turn_truncation.py`` ``_continue_text``) answers a reply cut at the cap with no tool call
by asking for the rest in the same conversation -- up to the fourth cut reply -- and joins the
pieces; pi stops. The phases whose reply is their deliverable now do what Hermes does.
"""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from misaka.core.research import planner, window


def _reply(text, stop, *, usage=None, tool=False):
    content = [{"type": "text", "text": text}] if text else [{"type": "thinking", "thinking": "..."}]
    if tool:
        content.append({"type": "toolCall", "id": "c1", "name": "read", "arguments": {}})
    return {"role": "assistant", "stopReason": stop, "content": content,
            "usage": usage or {"input": 1000, "output": 8192, "cacheRead": 0, "cacheWrite": 0}}


def _window(tmp_path, replies, *, context_window=128000):
    """A window over a session that answers each message sent with the next reply."""
    from misaka.core.network.wiring.capabilities import SisterCapabilitiesPart

    observers, sent = [], []
    queue = list(replies)

    async def send(message, _options):
        sent.append(message["content"])
        for observer in list(observers):
            observer({"type": "message_end", "message": queue.pop(0)})

    session = SimpleNamespace(
        isIdle=True,
        sessionManager=SimpleNamespace(sessionFile=str(tmp_path / "session.jsonl")),
        agent=SimpleNamespace(streamFn=None),
        model={"contextWindow": context_window},
        moments=SimpleNamespace(parts=[SisterCapabilitiesPart(str(tmp_path), [])]),
        subscribe=lambda fn: observers.append(fn) or (lambda: observers.remove(fn)),
        toolScope=lambda names, callable=(): nullcontext(),
        registerCustomTools=lambda tools: None,
        unregisterCustomTools=lambda tools: None,
        getActiveToolNames=list,
        sendCustomMessage=send,
    )
    return window.WindowLO(session, lambda: None, describe=dict), sent


async def _turn(tmp_path, replies, *, continue_truncated=True, **kwargs):
    bridge, sent = _window(tmp_path, replies, **kwargs)
    try:
        _obj, text, error = await bridge._execute_turn(
            "phase", {"session_dir": str(tmp_path), "continue_truncated": continue_truncated})
    finally:
        await bridge.close()
    return text, error, sent


async def test_a_cut_reply_is_continued_and_the_pieces_joined(tmp_path):
    text, error, sent = await _turn(tmp_path, [
        _reply("## Conclusion\n\nThe first part", "length"),
        _reply(" goes on", "length"),
        _reply("and ends.", "stop"),
    ])
    assert error is None
    # Hermes' join: a newline only where neither side of the seam has whitespace.
    assert text == "## Conclusion\n\nThe first part goes on\nand ends."
    assert sent == ["phase", window.LENGTH_CONTINUATION_OUTPUT_LIMIT, window.LENGTH_CONTINUATION_OUTPUT_LIMIT]


async def test_the_fourth_cut_reply_ends_the_phase(tmp_path):
    text, error, sent = await _turn(tmp_path, [_reply(f"part {n}", "length") for n in range(4)])
    assert sent.count(window.LENGTH_CONTINUATION_OUTPUT_LIMIT) == 3
    assert text == ""
    assert error.startswith("Response remained truncated after 4 continuation attempts: ")
    assert planner.hit_output_limit(error)


async def test_a_phase_that_did_not_ask_is_not_continued(tmp_path):
    """Phases that deliver through a tool keep ``planner.commanded``'s own nudge."""
    text, error, sent = await _turn(tmp_path, [_reply("partial", "length")], continue_truncated=False)
    assert sent == ["phase"] and text == "" and planner.hit_output_limit(error)


@pytest.mark.parametrize("reply", [
    _reply("", "length"),                       # all thinking, nothing visible
    _reply("calling", "length", tool=True),     # pi fails its tool calls and goes on by itself
])
async def test_a_cut_reply_without_text_alone_is_not_continued(tmp_path, reply):
    _text, error, sent = await _turn(tmp_path, [reply])
    assert sent == ["phase"] and error


async def test_a_prompt_that_filled_the_window_is_not_continued(tmp_path):
    full = {"input": 9000, "output": 600, "cacheRead": 400, "cacheWrite": 200}
    _text, error, sent = await _turn(tmp_path, [_reply("partial", "length", usage=full)], context_window=10000)
    assert sent == ["phase"]
    assert error.startswith("Prompt used 9600 of 10000 context tokens; no room to answer: ")


def test_the_nudge_is_hermes_own_words_so_its_compressor_knows_it():
    from misaka.extensions.misaka_lcm.native import support
    assert window.LENGTH_CONTINUATION_OUTPUT_LIMIT == support._LENGTH_CONTINUATION_OUTPUT_LIMIT
    assert window.LENGTH_CONTINUATION_OUTPUT_LIMIT.startswith(support._SYNTHETIC_USER_PREFIXES)


def test_join_matches_hermes():
    assert window.join_truncated_parts(["a", "b"]) == "a\nb"
    assert window.join_truncated_parts(["a ", "b"]) == "a b"
    assert window.join_truncated_parts(["a", "\nb", "", "c"]) == "a\nb\nc"


def test_the_conclusion_and_the_dissolution_ask_for_continuation():
    run = {"id": "run", "root_session": "/fixture/session.jsonl", "workspace": "/fixture", "limits_json": "{}"}
    node = {"id": "node", "depth": 0, "question": "Question"}
    with patch.object(planner, "_lo_session", return_value="/fixture"), \
            patch.object(planner, "task_sources", return_value={}), \
            patch.object(planner, "findMostRecentSession", return_value=None), \
            patch.object(planner.runs, "plan_round", return_value=1), \
            patch.object(planner, "_call", return_value=(None, "Conclusion", None)) as call:
        planner.synthesize(object(), run, {"research_plan_approval": False}, SimpleNamespace(session=None),
                           node, [], followup=None, round=1, left=0)
    assert call.call_args.kwargs["continue_truncated"] is True
    with patch.object(planner.runs, "root", return_value=node), \
            patch.object(planner, "_lo_session", return_value="/fixture"), \
            patch.object(planner, "findMostRecentSession", return_value=None), \
            patch.object(planner, "_catalog_text", return_value=""), \
            patch.object(planner, "_call", return_value=(None, "", None)) as call:
        planner.present_dissolution(object(), run, {}, object(), material=[])
    assert call.call_args.kwargs["continue_truncated"] is True


def test_call_hands_the_choice_to_the_window():
    seen = {}
    worker = SimpleNamespace(run_llm_json=lambda *a, **kwargs: seen.update(kwargs) or (None, "", None))
    cfg = {"roles_root": "/roles", "provider": "p", "default_model": "m"}
    planner._call(worker, cfg, "prompt", cwd="/w", session_dir="/s", continue_truncated=True)
    assert seen["continue_truncated"] is True
    seen.clear()
    planner._call(worker, cfg, "prompt", cwd="/w", session_dir="/s")
    assert "continue_truncated" not in seen

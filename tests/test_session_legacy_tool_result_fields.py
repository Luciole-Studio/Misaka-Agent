"""Transcripts written before the pi 0.86-0.87 port carry ``addedToolNames`` on toolResult
messages, a field that port removed from the model. pi has no runtime validation and reads
such transcripts unchanged; the strict model here refused the whole session at load
(2026-09-23: the 20 September Last Order transcript, 345 occurrences, a pane that crashed at
every panel start). ``validate_message`` is the one choke point every path goes through (session
load, agent state, the LLM wire, the LCM's snapshot restore), so the retired key is dropped
there, from a copy; any other unknown key still fails, so real shape bugs stay visible."""
import pytest
from pydantic import ValidationError

from misaka.agent.agent import _normalizeStandardMessage
from misaka.ai.types import validate_message
from misaka.core.messages import convertToLlm
from misaka.core.session_manager import _copy_context_messages


def _tool_result(**extra):
    return {"role": "toolResult", "toolCallId": "call_1", "toolName": "read",
            "content": [{"type": "text", "text": "ok"}], "isError": False, "timestamp": 1, **extra}


def test_the_validator_drops_the_retired_key_without_touching_the_input():
    legacy = _tool_result(addedToolNames=None)
    message = validate_message(legacy)
    assert message.role == "toolResult" and not hasattr(message, "addedToolNames")
    assert "addedToolNames" in legacy                    # the stored transcript row is untouched


@pytest.mark.parametrize("entry", [
    lambda m: convertToLlm([m])[0],                   # the LLM wire
    lambda m: _normalizeStandardMessage(m),           # agent state
    lambda m: validate_message(_copy_context_messages([m])[0]),   # a compaction snapshot, then the wire
])
def test_every_entry_accepts_a_legacy_tool_result(entry):
    for value in (None, ["x"]):
        assert entry(_tool_result(addedToolNames=value)).role == "toolResult"


def test_other_unknown_keys_and_other_roles_still_fail():
    with pytest.raises(ValidationError, match="bogus"):
        validate_message(_tool_result(bogus=1))
    with pytest.raises(ValidationError, match="addedToolNames"):
        validate_message({"role": "user", "content": [{"type": "text", "text": "q"}], "timestamp": 1,
                          "addedToolNames": None})

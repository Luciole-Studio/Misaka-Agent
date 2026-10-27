"""A pane started with an initial message (``card-shell --resume --say <note>``) may already
be in a turn when that message is sent: the network wiring flushes inbox deliveries with
triggerTurn at bind, which pi's startup never does. Without a streaming behaviour prompt()
raised "Agent is already processing. Specify streamingBehavior ..." and the note was lost
(2026-09-23). The startup messages steer into that turn: queued as follow-ups they waited for the
whole run, and a card's contract reached her after she had submitted (2026-10-24, R14). An idle
session is unchanged."""
from types import SimpleNamespace

from misaka.modes.interactive.interactive_mode import InteractiveMode

_REFUSAL = ("Agent is already processing. Specify streamingBehavior ('steer' or 'followUp') "
            "to queue the message.")


def _mode(streaming):
    calls, errors = [], []

    class Session:
        isStreaming = streaming

        async def prompt(self, text, options=None):
            calls.append((text, options))
            if self.isStreaming and not (options or {}).get("streamingBehavior"):
                raise RuntimeError(_REFUSAL)

    mode = SimpleNamespace(
        session=Session(),
        options=SimpleNamespace(initialMessage="note", initialImages=[], initialMessages=["more"]),
        showError=errors.append,
    )
    return mode, calls, errors


async def test_initial_messages_steer_into_a_running_turn():
    mode, calls, errors = _mode(streaming=True)
    await InteractiveMode._promptInitialMessages(mode)
    assert calls == [("note", {"images": [], "streamingBehavior": "steer"}),
                     ("more", {"streamingBehavior": "steer"})]
    assert errors == []


async def test_idle_session_still_gets_both_messages():
    mode, calls, errors = _mode(streaming=False)
    await InteractiveMode._promptInitialMessages(mode)
    assert [text for text, _ in calls] == ["note", "more"]
    assert errors == []

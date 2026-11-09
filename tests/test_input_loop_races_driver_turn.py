"""A message typed into an idle window must survive a turn that starts a moment later.

2026-09-23: in the research window the driver starts turns of its own (phase prompts,
notifications). handleInput saw an idle agent and handed the text to the input loop; by the time
the loop called prompt() a driver turn was streaming, prompt() raised "Agent is already
processing. Specify streamingBehavior ..." and the text was gone. The loop now asks for the same
steer that Enter would have queued had it seen the turn."""
import asyncio
from types import SimpleNamespace

from misaka.modes.interactive.interactive_mode import InteractiveMode


async def test_input_loop_queues_a_steer_when_a_turn_started_in_between():
    mode = SimpleNamespace()
    loop = asyncio.get_running_loop()
    mode._shutdownFuture = loop.create_future()
    calls, errors = [], []
    texts = iter(["hello"])

    async def get_user_input():
        try:
            return next(texts)
        except StopIteration:
            mode._shutdownFuture.set_result(None)
            await asyncio.sleep(0)
            raise asyncio.CancelledError

    class Session:
        isStreaming = True

        async def prompt(self, text, options=None):
            calls.append((text, options))
            if not (options or {}).get("streamingBehavior"):
                raise RuntimeError("Agent is already processing. Specify streamingBehavior ('steer' or 'followUp') "
                                   "to queue the message.")

    mode.getUserInput = get_user_input
    mode.session = Session()
    mode.showError = errors.append
    await InteractiveMode._runInputLoop(mode)
    assert calls == [("hello", {"streamingBehavior": "steer"})]
    assert errors == []

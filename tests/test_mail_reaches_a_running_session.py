"""A message to a session that is working is read at its next tool boundary, in the same run.

2026-09-29: the inbox handed mail to the session as a follow-up, which pi reads only when the
whole run is over. A card's work is usually one run, so Last Order's notes reached five cards
183-396 s late, every one of them after the card had already submitted. The inbox also waited
for the whole run a delivery started before it polled again, so a second message to a session
woken by the first one waited for that run to end as well.

Only the model is scripted: the session, its inbox pump, the mailbox and the tool loop are real.
"""
import asyncio
import contextlib
from types import SimpleNamespace

import pytest

from misaka.ai.compat import getModels
from misaka.ai.types import AssistantMessage, DoneEvent, ToolCall, Usage, UsageCost
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.core.auth_storage import AuthStorage
from misaka.core.extensions.types import ToolDefinition
from misaka.core.model_runtime import ModelRuntime
from misaka.core.network import messages
from misaka.core.resource_loader import DefaultResourceLoader
from misaka.core.sdk import createAgentSession
from misaka.core.session_manager import SessionManager
from misaka.core.settings_manager import SettingsManager
from tests.offline import refuse_network


def _reply(model, content, stop):
    return AssistantMessage(
        content=content, api=model.api, provider=model.provider, model=model.id, stopReason=stop,
        timestamp=1, usage=Usage(input=0, output=0, cacheRead=0, cacheWrite=0, totalTokens=0,
                                 cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0)))


async def _until(check, seconds=3.0):
    """Poll ``check`` until it holds; False when it never does (the test then says what failed)."""
    deadline = asyncio.get_running_loop().time() + seconds
    while not check():
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.01)
    return True


@contextlib.asynccontextmanager
async def _world(tmp_path, monkeypatch, *, card_task=None, waiting=()):
    """A session with its inbox; ``card_task`` makes it that card's session, and ``waiting`` are
    notes queued for it before it starts."""
    for key in ("MISAKA_NET_SPACE", "MISAKA_USAGE_GENERATION", "MISAKA_SISTER_OWNER_GENERATION"):
        monkeypatch.delenv(key, raising=False)
    if card_task:
        monkeypatch.setenv("MISAKA_USAGE_GENERATION", "1")

    refuse_network(monkeypatch, "the offline mail test attempted a network connection")
    monkeypatch.setattr(messages, "POLL_SECONDS", 0.02)

    tool_bodies = {}

    async def execute(tool_call_id, params, signal, on_update, ctx):
        await tool_bodies["work"]()
        return {"content": [{"type": "text", "text": "worked"}], "details": {}}

    work = ToolDefinition(name="work", label="Work", description="Do one step of the card.",
                          parameters={"type": "object", "properties": {}}, execute=execute)
    inbox = messages.MessagesPart(sender="10032", receive=True, card_task=card_task)
    loader = DefaultResourceLoader({"cwd": str(tmp_path), "agentDir": str(tmp_path / "agent"),
                                    "noExtensions": True, "noPromptTemplates": True, "noThemes": True})
    await loader.reload()
    model = next(m for m in getModels("openai") if "image" in m.input)
    runtime = await ModelRuntime.create({"authStorage": AuthStorage.inMemory(), "modelsPath": None})
    await runtime.setRuntimeApiKey(model.provider, "fixture")
    result = await createAgentSession({
        "cwd": str(tmp_path), "agentDir": str(tmp_path / "agent"), "model": model, "modelRuntime": runtime,
        "resourceLoader": loader,
        "settingsManager": SettingsManager.inMemory({"compaction": {"enabled": False}, "retry": {"enabled": False}}),
        "sessionManager": SessionManager.create(str(tmp_path), str(tmp_path / "sessions")),
        "parts": [inbox], "customTools": [work], "tools": ["work"]})
    session = result["session"]
    calls, script = [], []

    def stream(model, context, options=None):
        calls.append(str(context.messages))
        message = script.pop(0) if script else _reply(model, [{"type": "text", "text": "idle"}], "stop")
        output = AssistantMessageEventStream()
        output.push(DoneEvent(reason=message.stopReason, message=message))
        output.end(message)
        return output

    session.agent.streamFunction = stream
    mail = messages.connect()

    def post(body):
        if card_task:
            return messages.send(mail, "10032", body, summary=body[:20], sender="last-order",
                                 to_task=card_task, generation=1)
        return messages.send(mail, "10032", body, summary=body[:20], sender="last-order",
                             to_session=session.sessionManager.getSessionId())

    def delivered(mid):
        return mail.execute("SELECT delivered_at FROM messages WHERE id=?", (mid,)).fetchone()[0] is not None

    queued = [post(body) for body in waiting]
    await session.bindExtensions({})          # starts the parts, the inbox pump among them, as a mode does
    try:
        yield SimpleNamespace(session=session, calls=calls, script=script, tool_bodies=tool_bodies,
                              post=post, delivered=delivered, inbox=inbox, waiting=queued,
                              tool_call=lambda: _reply(model, [ToolCall(id="call-1", name="work", arguments={})],
                                                       "toolUse"),
                              stop=lambda text: _reply(model, [{"type": "text", "text": text}], "stop"))
    finally:
        await inbox.session_shutdown({"type": "session_shutdown"}, None)
        await session.waitForIdle()
        session.dispose()
        mail.close()


@pytest.fixture
async def world(tmp_path, monkeypatch):
    async with _world(tmp_path, monkeypatch) as world:
        yield world


@pytest.fixture
async def card_world(tmp_path, monkeypatch):
    """A card's session started with a note from Last Order already in her inbox."""
    async with _world(tmp_path, monkeypatch, card_task="t_0000c4",
                      waiting=["check the 1982 edition before you conclude"]) as world:
        yield world


async def test_mail_to_a_working_session_is_read_at_its_next_tool_boundary(world):
    note = {}

    async def work():
        note["id"] = world.post("check the 1982 edition before you conclude")
        # The inbox hands the note over while this tool is still running.
        await _until(world.session.agent.hasQueuedMessages)

    world.tool_bodies["work"] = work
    world.script += [world.tool_call(), world.stop("concluded with the 1982 edition")]
    await world.session.prompt("start the card")
    assert "check the 1982 edition" in world.calls[1], "the note must be in the very next request"
    assert len(world.calls) == 2, "the note must not wait for the run to end and start another one"
    assert await _until(lambda: world.delivered(note["id"]))


async def test_a_run_started_by_mail_does_not_hold_back_the_next_message(world):
    seen = {}

    async def work():
        # The run this tool belongs to was started by the first note; the inbox has let go of it.
        seen["first_acked"] = await _until(lambda: world.delivered(first))
        seen["second"] = world.post("and the second note, sent while she works")
        seen["second_handed_over"] = await _until(world.session.agent.hasQueuedMessages)

    world.tool_bodies["work"] = work
    world.script += [world.tool_call(), world.stop("both notes considered")]
    first = world.post("the first note, sent while she is idle")
    assert await _until(lambda: len(world.calls) >= 1), "the first note starts a run"
    assert await _until(lambda: "second" in seen and world.delivered(seen["second"]), 5)
    await world.session.waitForIdle()
    assert seen["first_acked"], "the first note is acknowledged once it is in the transcript, not when its run ends"
    assert seen["second_handed_over"], "the inbox keeps polling while the run it started goes on"
    assert "the first note" in world.calls[0]
    assert "the second note" in world.calls[1], "the second note is read at the next tool boundary"
    assert len(world.calls) == 2


async def test_the_inbox_keeps_reading_after_a_reload(world):
    """/reload shuts the parts down and starts them again; the inbox has to come back too."""
    await world.session.reload()
    world.script.append(world.stop("read after the reload"))
    note = world.post("a note sent after /reload")
    assert await _until(lambda: world.delivered(note)), "the inbox stopped for good at the reload"
    await world.session.waitForIdle()
    assert "a note sent after /reload" in world.calls[0]


async def test_a_card_reads_her_contract_before_the_mail_that_was_waiting_for_her(card_world):
    """2026-10-24 (R14): a card whose inbox already held notes when she started got a note as her
    first message, not her contract. The waiting mail started a run of its own; the contract, sent
    a moment later, queued behind that whole run and reached fourteen cards after they had
    submitted -- each then did the card again, and a blind card read its materials before the
    rules that blinded her. The mail now goes in at the contract run's first tool boundary (B16)."""
    world = card_world

    async def work():
        await _until(world.session.agent.hasQueuedMessages)

    world.tool_bodies["work"] = work
    world.script += [world.tool_call(), world.stop("card submitted")]
    assert not await _until(lambda: world.calls, 0.2), "the waiting note must not start a run of its own"
    # As the pane sends it (`InteractiveMode._promptInitialMessages`).
    await world.session.prompt("the card contract", {"streamingBehavior": "steer"})
    assert "the card contract" in world.calls[0]
    assert "check the 1982 edition" not in world.calls[0], "the contract comes first, alone"
    assert "check the 1982 edition" in world.calls[1], "the note is read at the contract run's first tool boundary"
    assert len(world.calls) == 2
    assert await _until(lambda: world.delivered(world.waiting[0]))


async def test_a_card_that_never_ran_still_shuts_its_inbox_down(card_world):
    """Her inbox waits for her first run; a session closed before it must not hang on the wait."""
    await asyncio.wait_for(card_world.inbox.session_shutdown({"type": "session_shutdown"}, None), 2)
    assert card_world.calls == []
    assert not card_world.delivered(card_world.waiting[0]), "the note stays queued for her attempt"

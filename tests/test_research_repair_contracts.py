"""Repair acceptance: preserve intended features as well as closing the reproduced failures."""

import asyncio
import errno
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from misaka.agent.agent_loop import execute_tool_calls
from misaka.agent.types import AgentContext, AgentLoopConfig, AgentTool, AgentToolResult
from misaka.ai.types import (
    AssistantMessage,
    Context,
    Model,
    TextContent,
    ToolCall,
    Usage,
    UsageCost,
)
from misaka.ai.utils.json_parse import StreamingArgs
from misaka.core.platform import tasks
from misaka.core.research import bundle, commands, runs, workflow


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "_commit", lambda *a, **k: None)
    monkeypatch.setattr(bundle.corpus, "docs", lambda **k: [])
    con = tasks.connect(str(tmp_path / "board.db"))
    run = runs.create(con, workspace=str(tmp_path), question="fixture")
    try:
        yield con, run, runs.nodes(con, run["id"])[0]
    finally:
        con.close()


def model(api="anthropic-messages"):
    return Model(
        id="fixture",
        name="fixture",
        api=api,
        provider="fixture",
        baseUrl="http://fixture.invalid",
        reasoning=False,
        input=["text"],
        cost={"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        contextWindow=10000,
        maxTokens=1000,
    )


def message(content=()):
    return AssistantMessage(
        content=list(content),
        api="anthropic-messages",
        provider="fixture",
        model="fixture",
        stopReason="toolUse",
        timestamp=0,
        usage=Usage(
            input=0,
            output=0,
            cacheRead=0,
            cacheWrite=0,
            totalTokens=0,
            cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
        ),
    )


@pytest.mark.parametrize(
    "raw,valid",
    [
        ("{}", True),
        ("", True),
        ('{"note":"yes"}', True),
        ('{"note":"hello\nworld"}', True),
        ('{"note":"C:\\q"}', True),
        ('{"note":"hello', False),
        ("not-json", False),
        ('{"note":"an "unescaped" quote"}', False),
        ("null", False),
        ("[]", False),
        ('{"note":true}garbage', False),
    ],
)
def test_final_arguments_are_not_a_streaming_preview(raw, valid):
    args = StreamingArgs(raw)
    _preview = args.arguments
    call = ToolCall(id="fixture", name="probe", arguments={})
    args.finish_into(call)
    assert (call.argumentsError is None) == valid
    assert (
        ToolCall.model_validate_json(call.model_dump_json()).argumentsError
        == call.argumentsError
    )
    if not valid:
        with pytest.raises(ValueError):
            args.finish()
    assert StreamingArgs('{"note":"hello').arguments == {"note": "hello"}


@pytest.mark.parametrize("execution", ["sequential", "parallel"])
async def test_invalid_call_gets_receipt_without_blocking_valid_sibling(execution):
    called, prepared = [], []

    async def probe(call_id, args, *_):
        called.append((call_id, args))
        return AgentToolResult(content=[TextContent(text="ok")], details={})

    def prepare(args):
        prepared.append(args)
        return args

    tool = AgentTool(
        name="probe",
        label="probe",
        description="memory only",
        parameters={"type": "object", "properties": {"note": {"type": "string"}}},
        prepareArguments=prepare,
        execute=probe,
    )
    bad, good = [ToolCall(id=i, name="probe", arguments={}) for i in ["bad", "good"]]
    StreamingArgs("not-json").finish_into(bad)
    StreamingArgs("{}").finish_into(good)
    msg = AssistantMessage.model_validate_json(message([bad, good]).model_dump_json())
    result = await execute_tool_calls(
        AgentContext(systemPrompt="", messages=[], tools=[tool]),
        msg,
        AgentLoopConfig(model=None, convertToLlm=lambda m: m, toolExecution=execution),
        None,
        lambda e: None,
    )
    assert called == [("good", {})]
    assert prepared == [{}]
    assert [(r.toolCallId, r.isError) for r in result.messages] == [
        ("bad", True),
        ("good", False),
    ]


@pytest.mark.parametrize(
    "raw,inline,stop_block",
    [
        ("not-json", False, True),
        ('{"note":"hello', False, False),
        ("{}", False, True),
        ("", True, True),
    ],
)
async def test_anthropic_finalization_on_real_stream_path(
    monkeypatch, raw, inline, stop_block
):
    from misaka.ai.providers import anthropic

    events = [
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "call",
                "name": "probe",
                "input": {"note": "inline"} if inline else {},
            },
        }
    ]
    if raw:
        events.append(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": raw},
            }
        )
    if stop_block:
        events.append({"type": "content_block_stop", "index": 0})
    events.append({"type": "message_delta", "delta": {"stop_reason": "tool_use"}})

    async def incoming():
        for event in events:
            yield event

    monkeypatch.setattr(
        anthropic, "_create_raw_response", AsyncMock(return_value=incoming())
    )
    stream = anthropic.stream_anthropic(
        model(), Context(messages=[]), {"client": object()}
    )
    seen = [event async for event in stream]
    result = await stream.result()
    assert result.stopReason == "toolUse", result.errorMessage
    call = result.content[0]
    assert bool(call.argumentsError) == (bool(raw) and raw != "{}")
    if inline:
        assert call.arguments == {"note": "inline"}
    assert seen[-1].type == "done"


@pytest.mark.parametrize("bridge", ["proxy", "pi-messages"])
@pytest.mark.parametrize("send_end", [True, False])
def test_transport_preserves_raw_parse_failure(bridge, send_end):
    from misaka.agent.proxy import _process_proxy_event
    from misaka.ai.providers.pi_messages import _EventConverter

    converter = _EventConverter(model())
    partial, buffers = message(), {}
    convert = (
        converter.convert
        if bridge == "pi-messages"
        else lambda e: _process_proxy_event(e, partial, buffers)
    )
    convert(
        {
            "type": "toolcall_start",
            "contentIndex": 0,
            "id": "fixture",
            "toolName": "probe",
        }
    )
    convert({"type": "toolcall_delta", "contentIndex": 0, "delta": "not-json"})
    if send_end:
        convert(
            {
                "type": "toolcall_end",
                "contentIndex": 0,
                "toolCall": {
                    "type": "toolCall",
                    "id": "fixture",
                    "name": "probe",
                    "arguments": {},
                },
            }
        )
    convert(
        {"type": "done", "reason": "toolUse", "usage": message().usage.model_dump()}
    )
    result = converter.partial if bridge == "pi-messages" else partial
    assert result.content[0].argumentsError


@pytest.mark.parametrize("raw", ["not-json", "{}"])
@pytest.mark.parametrize("start", [False, True])
async def test_responses_final_item_has_same_validation(raw, start):
    from misaka.ai.providers.openai_responses_shared import process_responses_stream

    item = {
        "type": "function_call",
        "id": "item",
        "call_id": "call",
        "name": "probe",
        "arguments": raw,
    }

    async def incoming():
        if start:
            yield {
                "type": "response.output_item.added",
                "item": {**item, "arguments": ""},
            }
            yield {"type": "response.function_call_arguments.delta", "delta": raw}
            yield {"type": "response.function_call_arguments.done", "arguments": raw}
        yield {"type": "response.output_item.done", "item": item}
        yield {"type": "response.completed", "response": {"status": "completed"}}

    output, seen = message(), []
    await process_responses_stream(
        incoming(), output, SimpleNamespace(push=seen.append), model("openai-responses")
    )
    call = next(e.toolCall for e in seen if e.type == "toolcall_end")
    assert bool(call.argumentsError) == (raw != "{}")


@pytest.mark.parametrize("existing", ["file", "hardlink", "symlink"])
def test_place_never_overwrites_existing_target(tmp_path, existing):
    src, original, dst = [tmp_path / n for n in ["src", "original", "dst"]]
    src.write_bytes(b"new")
    original.write_bytes(b"original")
    if existing == "file":
        dst.write_bytes(b"original")
    elif existing == "hardlink":
        os.link(original, dst)
    else:
        dst.symlink_to(original)
    with pytest.raises(FileExistsError):
        bundle.place(src, dst)
    assert original.read_bytes() == dst.read_bytes() == b"original"


@pytest.mark.parametrize("fail_copy", [False, True])
def test_cross_device_copy_is_exclusive_and_cleans_failed_output(
    tmp_path, monkeypatch, fail_copy
):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.write_bytes(b"original")

    def no_link(*a):
        raise OSError(errno.EXDEV, "cross device")

    monkeypatch.setattr(bundle.os, "link", no_link)
    if fail_copy:
        monkeypatch.setattr(
            bundle.shutil,
            "copyfileobj",
            lambda *a: (_ for _ in ()).throw(OSError("disk full")),
        )
        with pytest.raises(OSError):
            bundle.place(src, dst)
        assert not dst.exists()
    else:
        assert bundle.place(src, dst)
        assert dst.read_bytes() == b"original"
        with pytest.raises(FileExistsError):
            bundle.place(src, dst)
    assert src.read_bytes() == b"original"


def test_explicit_url_alias_collision_is_unresolved(state):
    from misaka.core.tools._web.evidence import _frontmatter

    con, run, _ = state
    pages = Path(run["workspace"]) / "downloads/pages"
    pages.mkdir(parents=True)
    for name in ["a", "b"]:
        (pages / (name + ".md")).write_text(
            _frontmatter(
                {
                    "source_url": "https://fixture.invalid/document?id=" + name,
                    "final_url": "https://fixture.invalid/shared",
                }
            )
            + name
        )
    collector = bundle._Collector(con, run, bundle._Index(run["workspace"]))
    assert collector._resolve(
        "https://fixture.invalid/document?id=a#page", [run["workspace"]]
    )[0]
    assert (
        collector._resolve("https://fixture.invalid/shared", [run["workspace"]])[0]
        is None
    )


@pytest.mark.parametrize("changed", ["none", "driver", "node", "both"])
@pytest.mark.parametrize("action", ["assign", "start", "withdraw", "skip"])
async def test_real_approval_tools_fence_captured_owner(state, changed, action):
    con, run, root = state
    branch = runs.create_node(
        con, run["id"], trigger="fixture", parent_id=root["id"], depth=1
    )
    assert runs.acquire_driver(con, run["id"], "old-driver")
    key = runs.prepare_runner(con, "research_branches", branch["id"])
    run = runs.get(con, run["id"])
    branch = runs.node(con, branch["id"])
    runs.set_node(con, branch["id"], status="awaiting_approval")
    round_ = 2 if action == "withdraw" else 1
    session_file = str(Path(run["workspace"]) / "session.jsonl")
    runs.record_action(
        con,
        run,
        branch,
        runs.plan_key(round_),
        {"status": "ready"},
        session_file=session_file,
        tool_call_id="original-plan",
    )
    definitions = commands.review_tools(
        con, run, branch, validate=lambda x: x, session_file=session_file, round=round_
    )
    tool = next(t for t in definitions if t.name == "misaka_research_" + action)
    if changed in ["driver", "both"]:
        con.execute(
            "UPDATE research_runs SET driver_lock=? WHERE id=?",
            ("new-driver", run["id"]),
        )
    if changed in ["node", "both"]:
        con.execute(
            "UPDATE research_branches SET runner_key=? WHERE id=?",
            ("new-node", branch["id"]),
        )
    args = (
        {"status": "ready", "plan_markdown": "fixture"}
        if action == "assign"
        else {"reason": "fixture"}
        if action == "skip"
        else {}
    )
    invoke = tool.execute(
        "fixture-call",
        args,
        None,
        None,
        SimpleNamespace(sessionManager=SimpleNamespace(sessionFile=session_file)),
    )
    if changed == "none":
        await invoke
    else:
        with pytest.raises(ValueError, match="superseded"):
            await invoke
        assert (
            runs.action(con, run["id"], branch["id"], runs.plan_key(round_))[
                "tool_call_id"
            ]
            == "original-plan"
        )
        assert runs.action(con, run["id"], branch["id"], runs.start_key(round_)) is None
    if changed == "none" and action == "withdraw":
        assert runs.action(con, run["id"], branch["id"], runs.plan_key(round_)) is None
    if changed == "none":
        runs.release_runner(con, "research_branches", branch["id"], key)


async def test_spawn_cancellation_waits_for_and_reaps_late_handle(state):
    con, run, root = state
    entered, release = threading.Event(), threading.Event()
    handle, stopped = SimpleNamespace(pid=None), []

    def spawn(*a, **k):
        entered.set()
        assert release.wait(2)
        return handle

    spawner = SimpleNamespace(spawn=spawn, stop=stopped.append, alive=lambda h: True)
    task = asyncio.create_task(
        workflow._expand_level(
            con, {}, spawner, run, [root], poll_seconds=0.001, progress=None
        )
    )
    try:
        for _ in range(200):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set()
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped == [handle]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_unclaimed_runner_is_not_completed_and_late_release_is_fenced(state):
    con, run, root = state
    key = runs.prepare_runner(con, "research_branches", root["id"])
    task = asyncio.create_task(
        workflow._wait_level(
            con,
            {},
            SimpleNamespace(alive=lambda h: True),
            run,
            {root["id"]: SimpleNamespace(pid=None)},
            poll_seconds=0.001,
            progress=None,
        )
    )
    try:
        await asyncio.sleep(0.02)
        assert not task.done()
        runs.set_node(con, root["id"], status="closed")
        runs.release_runner(con, "research_branches", root["id"], key)
        assert await asyncio.wait_for(task, 0.5) == "done"
        new = runs.prepare_runner(con, "research_branches", root["id"])
        runs.release_runner(con, "research_branches", root["id"], key)
        assert runs.node(con, root["id"])["runner_key"] == new
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("cancel", [False, True])
async def test_shared_session_environment_restored_after_dispose(
    state, monkeypatch, cancel
):
    from misaka.core.platform import session as ps

    entered, release = asyncio.Event(), asyncio.Event()

    async def dispose():
        entered.set()
        await release.wait()

    monkeypatch.setenv("MISAKA_REPAIR_MARKER", "before")
    monkeypatch.setattr(
        ps,
        "open_session",
        AsyncMock(
            return_value=(
                SimpleNamespace(dispose=dispose),
                None,
                "fixture startup error",
            )
        ),
    )
    task = asyncio.create_task(
        ps.run_session(
            [], "", state[1]["workspace"], env={"MISAKA_REPAIR_MARKER": "inside"}
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if cancel:
            task.cancel()
            await asyncio.sleep(0.01)
            task.cancel()
            assert os.environ["MISAKA_REPAIR_MARKER"] == "inside"
        release.set()
        result = await asyncio.gather(task, return_exceptions=True)
        assert os.environ["MISAKA_REPAIR_MARKER"] == "before"
        assert isinstance(result[0], asyncio.CancelledError) == cancel
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "command,blocked",
    [
        ("printf '%s' '`literal`'", False),
        ("echo '${literal}'", False),
        ('printf "%s" "$(printf ok)"', True),
        ("bash -c 'echo $(cat $PRIVATE)'", True),
        ("printf '%s' '`literal`'; bash -c 'echo ${PRIVATE}'", True),
        ("cat /tmp/live-skills/SKILL.md", True),
    ],
)
def test_skill_guard_keeps_policy_but_allows_literal_display(command, blocked):
    from misaka.core.skills.wiring.skills import _command_touches

    assert _command_touches(command, "/tmp/workspace", {"/tmp/live-skills"}) == blocked
    if "`" in command:
        assert _command_touches(
            command, "/tmp/workspace", {"/tmp/live-skills"}, shell="powershell"
        )


@pytest.mark.parametrize("bridge", ["proxy", "pi-messages"])
def test_native_argument_error_is_persisted_but_not_sent_on_wire(bridge):
    from misaka.agent import proxy
    from misaka.ai.providers import pi_messages
    from misaka.ai.types import UserMessage

    call = ToolCall(id="bad", name="probe", arguments={}, argumentsError="fixture")
    context = Context(
        messages=[UserMessage(content="hello", timestamp=0), message([call])]
    )
    serialize = (proxy if bridge == "proxy" else pi_messages)._serialize_context
    wire = serialize(context)
    assert wire["messages"][0]["content"] == "hello"
    assert "argumentsError" not in wire["messages"][1]["content"][0]
    assert (
        context.model_dump()["messages"][1]["content"][0]["argumentsError"] == "fixture"
    )


def test_copy_close_failure_removes_partial_target(tmp_path, monkeypatch):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.write_bytes(b"original")
    real_open = open

    class CloseFailure:
        def __init__(self, path, mode):
            self.file = real_open(path, mode)

        def __enter__(self):
            return self.file

        def __exit__(self, *args):
            self.file.close()
            raise OSError("fixture delayed write failure")

    def no_link(*a):
        raise OSError(errno.EXDEV, "cross device")

    monkeypatch.setattr(bundle.os, "link", no_link)
    monkeypatch.setattr(
        bundle,
        "open",
        lambda path, mode: (
            CloseFailure(path, mode) if mode == "xb" else real_open(path, mode)
        ),
        raising=False,
    )
    with pytest.raises(OSError, match="delayed write"):
        bundle.place(src, dst)
    assert not dst.exists()
    assert src.read_bytes() == b"original"


async def test_approval_progress_cancellation_releases_only_own_wait(
    state, monkeypatch
):
    con, run, root = state
    runs.record_action(
        con,
        run,
        root,
        "plan",
        {"status": "ready"},
        session_file="/tmp/fixture-session",
        tool_call_id="plan",
    )
    monkeypatch.setattr(workflow.planner, "_roster", lambda cfg: [])
    entered = asyncio.Event()

    async def progress(event):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        workflow._await_approval(
            con, {}, None, None, run, root, poll_seconds=0.001, progress=progress
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert runs.node(con, root["id"])["status"] == "awaiting_approval"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runs.node(con, root["id"])["status"] == "planning"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_clarification_drains_running_sibling_without_starting_pending_node(
    state,
):
    con, run, root = state
    level = [
        runs.create_node(con, run["id"], trigger=name, parent_id=root["id"], depth=1)
        for name in ["clarifies", "running", "queued"]
    ]
    handles, started, keys = {}, [], {}

    async def start(node):
        started.append(node["id"])
        handles[node["id"]] = object()
        keys[node["id"]] = runs.prepare_runner(con, "research_branches", node["id"])
        return keys[node["id"]]

    task = asyncio.create_task(
        workflow._wait_level(
            con,
            {},
            SimpleNamespace(alive=lambda h: True),
            run,
            handles,
            poll_seconds=0.001,
            progress=None,
            pending=level,
            width=2,
            start=start,
        )
    )
    a, b, c = level
    try:
        for _ in range(100):
            if len(started) == 2:
                break
            await asyncio.sleep(0.002)
        assert started == [a["id"], b["id"]]
        runs.set_node(con, a["id"], status="waiting_input")
        runs.release_runner(con, "research_branches", a["id"], keys[a["id"]])
        await asyncio.sleep(0.02)
        assert not task.done()
        assert runs.get(con, run["id"])["status"] == "active"
        assert c["id"] not in started
        runs.record_action(
            con,
            run,
            runs.node(con, b["id"]),
            "plan",
            {"status": "ready"},
            session_file="/tmp/fixture-sibling",
            tool_call_id="sibling-plan",
        )
        runs.set_node(con, b["id"], status="closed")
        runs.release_runner(con, "research_branches", b["id"], keys[b["id"]])
        result = await asyncio.wait_for(task, 1)
        assert result["reason"] == "waiting_input"
        assert runs.get(con, run["id"])["status"] == "waiting_input"
        assert c["id"] not in started
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("failure", [OSError, InterruptedError])
def test_derived_index_ignores_io_failure_but_not_stop(state, monkeypatch, failure):
    def fail(*a):
        raise failure("fixture")

    monkeypatch.setattr(workflow, "_refresh_workspace_index", fail)
    if failure is InterruptedError:
        with pytest.raises(InterruptedError):
            workflow._try_refresh_workspace_index(*state[:2])
    else:
        workflow._try_refresh_workspace_index(*state[:2])


@pytest.mark.parametrize("stop_block", [False, True])
@pytest.mark.parametrize("raw", ["not-json", "{}"])
def test_bedrock_validates_closed_and_unclosed_blocks(stop_block, raw):
    from misaka.ai.providers import amazon_bedrock as bedrock

    output = message([ToolCall(id="call", name="probe", arguments={})])
    buffers = {0: StreamingArgs(raw)}
    if stop_block:
        bedrock.handle_content_block_stop(
            {"contentBlockIndex": 0},
            {0: 0},
            buffers,
            output,
            SimpleNamespace(push=lambda e: None),
        )
    else:
        bedrock.finish_open_tool_arguments(buffers, output)
    assert bool(output.content[0].argumentsError) == (raw != "{}")
    assert not buffers


@pytest.mark.parametrize("provider", ["mistral", "openai-completions"])
@pytest.mark.parametrize("raw", ["not-json", "{}"])
async def test_chat_completion_finalization(provider, raw, monkeypatch):
    from misaka.ai.providers import mistral, openai_completions

    async def incoming():
        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "fixture-call",
                                "type": "function",
                                "function": {"name": "probe", "arguments": raw},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }

    if provider == "mistral":
        output = message()
        assert await mistral.consume_chat_stream(
            model("mistral-conversations"),
            output,
            SimpleNamespace(push=lambda e: None),
            incoming(),
        )
    else:
        monkeypatch.setattr(
            openai_completions,
            "_create_completion_stream",
            AsyncMock(return_value=incoming()),
        )
        stream = openai_completions.stream_openai_completions(
            model(provider), Context(messages=[]), {"client": object()}
        )
        async for _event in stream:
            pass
        output = await stream.result()
    assert output.stopReason == "toolUse", output.errorMessage
    assert bool(output.content[0].argumentsError) == (raw != "{}")


@pytest.mark.parametrize("halt", ["stop", "takeover"])
async def test_stop_or_takeover_during_start_prevents_next_spawn(
    state, monkeypatch, halt
):
    con, run, root = state
    assert runs.acquire_driver(con, run["id"], "owner")
    run = runs.get(con, run["id"])
    pending = [
        runs.create_node(con, run["id"], trigger=name, parent_id=root["id"], depth=1)
        for name in ["first", "pending"]
    ]
    handles, started, stopped = {}, [], []
    monkeypatch.setattr(workflow, "_settle_stopped_tasks", AsyncMock())

    async def start(node):
        key = runs.prepare_runner(con, "research_branches", node["id"])
        started.append(node["id"])
        handles[node["id"]] = node["id"]
        if halt == "stop":
            runs.request_stop(con, run["id"])
        else:
            con.execute(
                "UPDATE research_runs SET driver_lock='successor' WHERE id=?",
                (run["id"],),
            )
        return key

    invoke = workflow._wait_level(
        con,
        {},
        SimpleNamespace(alive=lambda h: False, stop=stopped.append),
        run,
        handles,
        poll_seconds=0.001,
        progress=None,
        pending=pending,
        width=2,
        start=start,
        driver_lock="owner",
    )
    if halt == "takeover":
        with pytest.raises(RuntimeError, match="taken over"):
            await invoke
    else:
        assert await invoke == "stopped"
        assert stopped == [pending[0]["id"]]
    assert started == [pending[0]["id"]]


def test_depth_keeps_integer_and_cli_string_compatibility():
    for value in [0, 1, 12, "2"]:
        assert runs.normalize_limits({"max_depth": value})["max_depth"] == int(value)

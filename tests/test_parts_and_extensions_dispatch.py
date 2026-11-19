"""What a session's parts and its extensions see at each kernel event, and what the kernel
does with their answers.

Every session here is built through ``createAgentSession`` with a scripted provider, so each
test drives the real prompt loop: the events are raised where the kernel raises them and the
answers are folded where the kernel folds them. A part and an extension answer the same
events; a part always answers first.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

import pytest

from misaka.agent.types import AgentToolResult
from misaka.ai.auth.types import ApiKeyCredential
from misaka.ai.compat import registerFauxProvider
from misaka.ai.providers.faux import fauxAssistantMessage, fauxToolCall
from misaka.ai.types import TextContent
from misaka.ai.utils.transcript import getSystemMessageText
from misaka.core.agent_session_runtime import (
    CreateAgentSessionRuntimeResult,
    createAgentSessionRuntime,
)
from misaka.core.agent_session_services import (
    createAgentSessionFromServices,
    createAgentSessionServices,
)
from misaka.core.auth_storage import AuthStorage
from misaka.core.extensions.types import ToolDefinition
from misaka.core.model_runtime import ModelRuntime
from misaka.core.sdk import createAgentSession
from misaka.core.session_manager import SessionManager
from misaka.core.settings_manager import SettingsManager
from misaka.utils.values import read_field
from tests.model_runtime_test_utils import createInMemoryModelRegistry, getModelRuntime
from tests.suite_harness import createTestExtensionsResult, createTestResourceLoader


def _texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    return [str(read_field(item, "text")) for item in content or [] if read_field(item, "type") == "text"]


def _seen(event: str, payload: Any) -> Any:
    """What one handler was shown, reduced to the part a later handler could have changed."""
    if event == "tool_call":
        return dict(read_field(payload, "input") or {})
    if event == "tool_result":
        return _texts(read_field(payload, "content"))
    if event == "input":
        return read_field(payload, "text")
    if event == "context":
        return [text for message in read_field(payload, "messages") for text in _texts(read_field(message, "content"))]
    if event == "before_agent_start":
        return payload["systemPrompt"]
    if event in ("session_start", "session_shutdown"):
        return read_field(payload, "reason")
    return None


def _answering(who: str, trace: list[Any], event: str, answer: Any):
    def handler(payload: Any, ctx: Any) -> Any:
        trace.append((who, event, _seen(event, payload)))
        return answer(payload) if callable(answer) else answer

    return handler


def part(name: str, trace: list[Any], **answers: Any) -> Any:
    """A part that defines exactly the events named, records each call and gives the answer."""
    built = type(name, (), {"tools": [], "commands": []})()
    for event, answer in answers.items():
        setattr(built, event, _answering(name, trace, event, answer))
    return built


def extension(name: str, trace: list[Any], **answers: Any) -> dict[str, Any]:
    def factory(pi: Any) -> None:
        for event, answer in answers.items():
            pi.on(event, _answering(name, trace, event, answer))

    return {"name": name, "factory": factory}


def _echo_tool(executed: list[Any]):
    def factory(pi: Any) -> None:
        async def execute(_tool_call_id: Any, params: Any, *_rest: Any, **_more: Any) -> AgentToolResult:
            executed.append(dict(params))
            return AgentToolResult(content=[TextContent(text=f"echo {read_field(params, 'text')}")],
                                   details={"from": "tool"})

        pi.registerTool(ToolDefinition(
            name="echo", label="Echo", description="Echo text back",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}}, execute=execute))

    return {"name": "echo-tool", "factory": factory}


class Run:
    def __init__(self, session: Any, faux: Any, executed: list[Any], errors: list[Any]) -> None:
        self.session, self.faux, self.executed, self.errors = session, faux, executed, errors

    def texts(self, role: str) -> list[list[str]]:
        return [_texts(read_field(message, "content")) for message in self.session.messages
                if read_field(message, "role") == role]

    def tool_results(self) -> list[tuple[list[str], bool]]:
        return [(_texts(read_field(message, "content")), bool(read_field(message, "isError")))
                for message in self.session.messages if read_field(message, "role") == "toolResult"]


@asynccontextmanager
async def running(tmp_path, parts=(), extensions=(), bindings=None):
    faux = registerFauxProvider({})
    session = None
    try:
        model = faux.getModel()
        auth = AuthStorage.inMemory()

        async def key(_current: Any) -> ApiKeyCredential:
            return ApiKeyCredential(type="api_key", key="faux-key")

        await auth.modify(model.provider, key)
        registry = await createInMemoryModelRegistry(auth)
        registry.registerProvider(model.provider, {
            "baseUrl": model.baseUrl, "apiKey": "faux-key", "api": faux.api,
            "models": [{"id": m.id, "name": m.name, "api": m.api, "reasoning": m.reasoning, "input": m.input,
                        "inputLimits": m.inputLimits, "cost": m.cost, "contextWindow": m.contextWindow,
                        "maxTokens": m.maxTokens, "baseUrl": m.baseUrl} for m in faux.models],
        })
        executed: list[Any] = []
        errors: list[Any] = []
        loaded = await createTestExtensionsResult([_echo_tool(executed), *extensions], str(tmp_path))
        created = await createAgentSession({
            "cwd": str(tmp_path), "agentDir": str(tmp_path / "agent"), "model": model,
            "modelRuntime": getModelRuntime(registry), "settingsManager": SettingsManager.inMemory(),
            "sessionManager": SessionManager.inMemory(str(tmp_path)),
            "resourceLoader": createTestResourceLoader(loaded), "parts": list(parts), "noTools": "builtin",
        })
        session = created["session"]
        await session.bindExtensions({"onError": errors.append} if bindings is None else bindings)
        yield Run(session, faux, executed, errors)
    finally:
        if session is not None:
            session.dispose()
        faux.unregister()


def _tool_turn(text: str = "hello") -> list[Any]:
    return [fauxAssistantMessage([fauxToolCall("echo", {"text": text})], {"stopReason": "toolUse"}),
            fauxAssistantMessage("done")]


def _calls(trace: list[Any], event: str) -> list[Any]:
    return [(who, seen) for who, name, seen in trace if name == event]


def _who(trace: list[Any], event: str) -> list[str]:
    return [who for who, name, _ in trace if name == event]


EVERY_EVENT = ("session_start", "input", "before_agent_start", "agent_start", "context", "tool_call",
               "tool_result", "agent_end", "agent_settled")


async def test_parts_answer_each_event_in_their_order_and_before_the_extensions(tmp_path):
    trace: list[Any] = []
    watching = dict.fromkeys(EVERY_EVENT)
    parts = [part("first", trace, **watching), part("second", trace, **watching)]
    async with running(tmp_path, parts, [extension("ext", trace, **watching)]) as run:
        run.faux.setResponses(_tool_turn())
        await run.session.prompt("go")

        for event in EVERY_EVENT:
            requests = 2 if event == "context" else 1
            assert _who(trace, event) == ["first", "second", "ext"] * requests, event
        assert run.executed == [{"text": "hello"}]
        assert run.errors == []


async def test_a_part_that_blocks_a_tool_call_stops_the_tool_and_the_handlers_after_it(tmp_path):
    trace: list[Any] = []
    parts = [part("guard", trace, tool_call={"block": True, "reason": "not here"}), part("later", trace, tool_call=None)]
    async with running(tmp_path, parts, [extension("ext", trace, tool_call=None)]) as run:
        run.faux.setResponses(_tool_turn())
        await run.session.prompt("go")

        assert _who(trace, "tool_call") == ["guard"]
        assert run.executed == []
        assert run.tool_results() == [(["not here"], True)]


async def test_an_extension_that_blocks_a_tool_call_stops_the_tool_after_the_parts_allowed_it(tmp_path):
    trace: list[Any] = []
    async with running(tmp_path, [part("guard", trace, tool_call=None)],
                       [extension("ext", trace, tool_call={"block": True, "reason": "extension said no"})]) as run:
        run.faux.setResponses(_tool_turn())
        await run.session.prompt("go")

        assert _who(trace, "tool_call") == ["guard", "ext"]
        assert run.executed == []
        assert run.tool_results() == [(["extension said no"], True)]


async def test_the_input_a_part_rewrites_is_what_later_handlers_see_and_what_the_tool_runs_with(tmp_path):
    trace: list[Any] = []
    parts = [part("rewriter", trace, tool_call={"updatedInput": {"text": "rewritten"}}),
             part("later", trace, tool_call=None)]
    async with running(tmp_path, parts, [extension("ext", trace, tool_call=None)]) as run:
        run.faux.setResponses(_tool_turn("original"))
        await run.session.prompt("go")

        assert _calls(trace, "tool_call") == [("rewriter", {"text": "original"}), ("later", {"text": "rewritten"}),
                                              ("ext", {"text": "rewritten"})]
        assert run.executed == [{"text": "rewritten"}]
        assert run.tool_results() == [(["echo rewritten"], False)]


async def test_a_rewritten_input_survives_an_extension_that_answers_without_blocking(tmp_path):
    trace: list[Any] = []
    async with running(tmp_path, [part("rewriter", trace, tool_call={"updatedInput": {"text": "rewritten"}})],
                       [extension("ext", trace, tool_call={"reason": "noted"})]) as run:
        run.faux.setResponses(_tool_turn("original"))
        await run.session.prompt("go")

        assert run.executed == [{"text": "rewritten"}]


async def test_a_part_that_crashes_on_a_tool_call_fails_closed(tmp_path):
    trace: list[Any] = []

    def crash(_payload: Any) -> Any:
        raise RuntimeError("guard broke")

    async with running(tmp_path, [part("guard", trace, tool_call=crash)],
                       [extension("ext", trace, tool_call=None)]) as run:
        run.faux.setResponses(_tool_turn())
        await run.session.prompt("go")

        assert _who(trace, "tool_call") == ["guard"]
        assert run.executed == []
        [(texts, is_error)] = run.tool_results()
        assert is_error and "guard broke" in texts[0]


async def test_each_handler_sees_the_tool_result_as_the_one_before_left_it(tmp_path):
    trace: list[Any] = []

    def add(marker: str):
        return lambda payload: {"content": [*read_field(payload, "content"), {"type": "text", "text": marker}]}

    parts = [part("first", trace, tool_result=add("+first")), part("second", trace, tool_result=add("+second"))]
    async with running(tmp_path, parts, [extension("ext", trace, tool_result=add("+ext"))]) as run:
        run.faux.setResponses(_tool_turn())
        await run.session.prompt("go")

        assert _calls(trace, "tool_result") == [
            ("first", ["echo hello"]), ("second", ["echo hello", "+first"]),
            ("ext", ["echo hello", "+first", "+second"])]
        assert run.tool_results() == [(["echo hello", "+first", "+second", "+ext"], False)]


async def test_a_part_can_turn_a_tool_result_into_an_error_and_an_extension_can_leave_it(tmp_path):
    trace: list[Any] = []
    async with running(tmp_path, [part("judge", trace, tool_result={"isError": True})],
                       [extension("ext", trace, tool_result=None)]) as run:
        run.faux.setResponses(_tool_turn())
        await run.session.prompt("go")

        assert run.tool_results() == [(["echo hello"], True)]


async def test_a_part_that_ends_the_run_from_a_tool_result_stops_the_next_request(tmp_path):
    trace: list[Any] = []
    async with running(tmp_path, [part("stopper", trace, tool_result={"terminate": True})],
                       [extension("ext", trace, tool_result=None)]) as run:
        run.faux.setResponses(_tool_turn())
        await run.session.prompt("go")

        assert run.tool_results() == [(["echo hello"], False)]
        assert run.texts("assistant")[-1] != ["done"]
        assert len(run.texts("assistant")) == 1


async def test_a_part_s_request_to_end_the_run_survives_an_extension_that_edits_the_result(tmp_path):
    trace: list[Any] = []
    edit = {"content": [{"type": "text", "text": "edited"}]}
    async with running(tmp_path, [part("stopper", trace, tool_result={"terminate": True})],
                       [extension("ext", trace, tool_result=edit)]) as run:
        run.faux.setResponses(_tool_turn())
        await run.session.prompt("go")

        assert run.tool_results() == [(["edited"], False)]
        assert len(run.texts("assistant")) == 1


async def test_the_input_an_extension_rewrites_is_what_the_next_extension_and_the_tool_get(tmp_path):
    trace: list[Any] = []
    extensions = [extension("rewriter", trace, tool_call={"updatedInput": {"text": "rewritten"}}),
                  extension("later", trace, tool_call={"reason": "noted"})]
    async with running(tmp_path, [], extensions) as run:
        run.faux.setResponses(_tool_turn("original"))
        await run.session.prompt("go")

        assert _calls(trace, "tool_call") == [("rewriter", {"text": "original"}), ("later", {"text": "rewritten"})]
        assert run.executed == [{"text": "rewritten"}]


async def test_a_part_that_crashes_on_a_tool_result_is_logged_and_skipped(tmp_path, caplog):
    trace: list[Any] = []

    def crash(_payload: Any) -> Any:
        raise RuntimeError("part broke")

    marked = {"content": [{"type": "text", "text": "from ext"}]}
    with caplog.at_level(logging.WARNING):
        async with running(tmp_path, [part("broken", trace, tool_result=crash)],
                           [extension("ext", trace, tool_result=marked)]) as run:
            run.faux.setResponses(_tool_turn())
            await run.session.prompt("go")

            assert _who(trace, "tool_result") == ["broken", "ext"]
            assert run.tool_results() == [(["from ext"], False)]
            assert run.errors == []
    assert any("broken" in record.getMessage() and "tool_result" in record.getMessage() for record in caplog.records)


async def test_messages_and_a_forced_prompt_from_before_agent_start_reach_the_run_in_handler_order(tmp_path):
    trace: list[Any] = []

    def note(text: str) -> dict[str, Any]:
        return {"customType": "note", "content": text, "display": False}

    parts = [part("one", trace, before_agent_start={"message": note("from one"), "systemPrompt": "FORCED"}),
             part("many", trace, before_agent_start={"messages": [note("many a"), note("many b")]})]
    async with running(tmp_path, parts,
                       [extension("ext", trace, before_agent_start={"message": note("from ext")})]) as run:
        prompts: list[Any] = []

        def respond(context: Any, *_rest: Any) -> Any:
            prompts.append(getSystemMessageText(read_field(context, "messages")[0]))
            return fauxAssistantMessage("done")

        run.faux.setResponses([respond])
        await run.session.prompt("go")

        seen = _calls(trace, "before_agent_start")
        assert [who for who, _ in seen] == ["one", "many", "ext"]
        assert seen[0][1] != "FORCED" and seen[1][1] == "FORCED" and seen[2][1] == "FORCED"
        assert prompts == ["FORCED"]
        assert run.texts("custom") == [["from one"], ["many a"], ["many b"], ["from ext"]]


async def test_a_part_that_blocks_before_agent_start_fails_the_prompt_with_its_reason(tmp_path):
    trace: list[Any] = []
    async with running(tmp_path, [part("gate", trace, before_agent_start={"block": True, "reason": "over budget"})],
                       [extension("ext", trace, before_agent_start=None)]) as run:
        run.faux.setResponses([fauxAssistantMessage("never")])
        with pytest.raises(RuntimeError, match="over budget"):
            await run.session.prompt("go")

        assert _who(trace, "before_agent_start") == ["gate"]
        assert run.texts("assistant") == []


async def test_an_extension_that_blocks_before_agent_start_fails_the_prompt_with_its_reason(tmp_path):
    trace: list[Any] = []
    async with running(tmp_path, [part("gate", trace, before_agent_start=None)],
                       [extension("ext", trace, before_agent_start={"block": True, "reason": "extension budget"})]) as run:
        run.faux.setResponses([fauxAssistantMessage("never")])
        with pytest.raises(RuntimeError, match="extension budget"):
            await run.session.prompt("go")

        assert _who(trace, "before_agent_start") == ["gate", "ext"]


async def test_input_transforms_chain_from_the_parts_into_the_extensions(tmp_path):
    trace: list[Any] = []

    def suffix(marker: str):
        return lambda payload: {"action": "transform", "text": read_field(payload, "text") + marker}

    async with running(tmp_path, [part("first", trace, input=suffix(" +first"))],
                       [extension("ext", trace, input=suffix(" +ext"))]) as run:
        run.faux.setResponses([fauxAssistantMessage("done")])
        await run.session.prompt("go")

        assert _calls(trace, "input") == [("first", "go"), ("ext", "go +first")]
        assert run.texts("user") == [["go +first +ext"]]


async def test_a_part_that_handles_the_input_keeps_it_from_the_extensions_and_from_the_model(tmp_path):
    trace: list[Any] = []
    async with running(tmp_path, [part("taker", trace, input={"action": "handled"})],
                       [extension("ext", trace, input=None)]) as run:
        run.faux.setResponses([fauxAssistantMessage("never")])
        await run.session.prompt("go")

        assert _who(trace, "input") == ["taker"]
        assert run.texts("user") == [] and run.texts("assistant") == []


async def test_a_part_that_blocks_agent_end_sends_its_reason_back_as_the_next_turn(tmp_path):
    trace: list[Any] = []
    answers = iter([{"block": True, "reason": "not finished"}, None])
    async with running(tmp_path, [part("checker", trace, agent_end=lambda _payload: next(answers))],
                       [extension("ext", trace, agent_end=None)]) as run:
        run.faux.setResponses([fauxAssistantMessage("first try"), fauxAssistantMessage("second try")])
        await run.session.prompt("go")

        assert _who(trace, "agent_end") == ["checker", "checker", "ext"]
        assert run.texts("user") == [["go"], ["Stop hook feedback:\nnot finished"]]
        assert run.texts("assistant") == [["first try"], ["second try"]]


async def test_an_extension_that_blocks_agent_end_sends_its_reason_back_as_the_next_turn(tmp_path):
    trace: list[Any] = []
    answers = iter([{"block": True, "reason": "extension wants more"}, None])
    async with running(tmp_path, [part("checker", trace, agent_end=None)],
                       [extension("ext", trace, agent_end=lambda _payload: next(answers))]) as run:
        run.faux.setResponses([fauxAssistantMessage("first try"), fauxAssistantMessage("second try")])
        await run.session.prompt("go")

        assert _who(trace, "agent_end") == ["checker", "ext", "checker", "ext"]
        assert run.texts("user") == [["go"], ["Stop hook feedback:\nextension wants more"]]


async def test_the_context_a_part_rewrites_is_what_the_extension_and_the_provider_get(tmp_path):
    trace: list[Any] = []

    def tag(marker: str):
        def rewrite(payload: Any) -> dict[str, Any]:
            return {"messages": [*read_field(payload, "messages"),
                                 {"role": "user", "content": [{"type": "text", "text": marker}], "timestamp": 1}]}
        return rewrite

    async with running(tmp_path, [part("first", trace, context=tag("ctx-first"))],
                       [extension("ext", trace, context=tag("ctx-ext"))]) as run:
        sent: list[Any] = []

        def respond(context: Any, *_rest: Any) -> Any:
            sent.append([text for message in read_field(context, "messages")
                         if read_field(message, "role") != "system"
                         for text in _texts(read_field(message, "content"))])
            return fauxAssistantMessage("done")

        run.faux.setResponses([respond])
        await run.session.prompt("go")

        assert _calls(trace, "context") == [("first", ["go"]), ("ext", ["go", "ctx-first"])]
        assert sent == [["go", "ctx-first", "ctx-ext"]]
        assert run.texts("user") == [["go"]]


async def test_a_part_that_crashes_on_a_notification_is_logged_and_the_others_still_hear_it(tmp_path, caplog):
    trace: list[Any] = []

    def crash(_payload: Any) -> Any:
        raise RuntimeError("part broke")

    with caplog.at_level(logging.WARNING):
        async with running(tmp_path, [part("broken", trace, agent_start=crash), part("fine", trace, agent_start=None)],
                           [extension("ext", trace, agent_start=None)]) as run:
            run.faux.setResponses([fauxAssistantMessage("done")])
            await run.session.prompt("go")

            assert _who(trace, "agent_start") == ["broken", "fine", "ext"]
            assert run.texts("assistant") == [["done"]]
            assert run.errors == []
    assert any("broken" in record.getMessage() and "agent_start" in record.getMessage() for record in caplog.records)


async def test_reload_shuts_everything_down_and_starts_it_again_parts_first(tmp_path):
    trace: list[Any] = []
    watching = {"session_start": None, "session_shutdown": None}
    async with running(tmp_path, [part("first", trace, **watching)], [extension("ext", trace, **watching)]) as run:
        del trace[:]
        await run.session.reload()

        assert _calls(trace, "session_shutdown")[0] == ("first", "reload")
        assert [who for who, _ in _calls(trace, "session_shutdown")] == ["first", "ext"]
        assert _calls(trace, "session_start")[0] == ("first", "reload")


async def test_a_notification_raised_on_a_replaced_runner_does_not_reach_the_parts(tmp_path):
    """A run that is still unwinding when its session is replaced announces into a dead runner:
    the parts have no live session to act on there."""
    trace: list[Any] = []
    async with running(tmp_path, [part("first", trace, agent_start=None)],
                       [extension("ext", trace, agent_start=None)]) as run:
        run.session.extensionRunner.invalidate("replaced")
        await run.session._emitExtensionEvent({"type": "agent_start"})

        assert "first" not in _who(trace, "agent_start")


@asynccontextmanager
async def runtime_host(tmp_path, parts, extensions):
    """A session behind an ``AgentSessionRuntime``, which owns forking and the last shutdown."""
    home_dir = str(tmp_path)
    faux = registerFauxProvider()
    faux.setResponses([fauxAssistantMessage("one"), fauxAssistantMessage("two")])
    auth = AuthStorage.inMemory()

    async def key(_current: Any) -> ApiKeyCredential:
        return ApiKeyCredential(type="api_key", key="faux-key")

    model = faux.getModel()
    await auth.modify(model.provider, key)
    models = await ModelRuntime.create({"authStorage": auth, "modelsPath": str(tmp_path / "models.json")})
    models.registerProvider(model.provider, {
        "baseUrl": model.baseUrl, "api": model.api,
        "models": [{"id": model.id, "name": model.name, "api": model.api, "reasoning": model.reasoning,
                    "input": model.input, "cost": model.cost, "contextWindow": model.contextWindow,
                    "maxTokens": model.maxTokens, "baseUrl": model.baseUrl}],
    })

    async def create(options: Any) -> CreateAgentSessionRuntimeResult:
        services = await createAgentSessionServices({
            "agentDir": home_dir, "modelRuntime": models, "model": model, "cwd": options["cwd"],
            "resourceLoaderOptions": {"extensionFactories": list(extensions), "noSkills": True,
                                      "noPromptTemplates": True, "noThemes": True},
        })
        result = await createAgentSessionFromServices({
            "services": services, "sessionManager": options["sessionManager"],
            "sessionStartEvent": options.get("sessionStartEvent"), "model": model, "parts": list(parts),
        })
        return CreateAgentSessionRuntimeResult(
            session=result["session"], services=services, diagnostics=services.diagnostics,
            extensionsResult=result.get("extensionsResult"), modelFallbackMessage=result.get("modelFallbackMessage"))

    host = await createAgentSessionRuntime(
        create, {"cwd": home_dir, "agentDir": home_dir, "sessionManager": SessionManager.create(home_dir)})
    await host.session.bindExtensions({})
    disposed = False

    async def dispose() -> None:
        nonlocal disposed
        if not disposed:
            disposed = True
            await host.dispose()

    host.close = dispose
    try:
        yield host
    finally:
        await dispose()
        faux.unregister()


def _first_user_entry(host: Any) -> str:
    return next(read_field(entry, "id") for entry in host.session.sessionManager.getBranch()
                if read_field(read_field(entry, "message"), "role") == "user")


async def test_a_part_can_cancel_a_fork_before_the_extensions_are_asked(tmp_path):
    trace: list[Any] = []
    async with runtime_host(tmp_path, [part("gate", trace, session_before_fork={"cancel": True})],
                            [extension("ext", trace, session_before_fork=None)]) as host:
        await host.session.prompt("hello")
        before = host.session.sessionFile

        assert await host.fork(_first_user_entry(host)) == {"cancelled": True}
        assert _who(trace, "session_before_fork") == ["gate"]
        assert host.session.sessionFile == before


async def test_an_extension_can_cancel_a_fork_the_parts_allowed(tmp_path):
    trace: list[Any] = []
    async with runtime_host(tmp_path, [part("gate", trace, session_before_fork=None)],
                            [extension("ext", trace, session_before_fork={"cancel": True})]) as host:
        await host.session.prompt("hello")

        assert await host.fork(_first_user_entry(host)) == {"cancelled": True}
        assert _who(trace, "session_before_fork") == ["gate", "ext"]


async def test_the_last_shutdown_reaches_the_parts_and_then_the_extensions(tmp_path):
    trace: list[Any] = []
    watching = {"session_shutdown": None}
    async with runtime_host(tmp_path, [part("first", trace, **watching)], [extension("ext", trace, **watching)]) as host:
        await host.close()

        assert _calls(trace, "session_shutdown") == [("first", "quit"), ("ext", "quit")]

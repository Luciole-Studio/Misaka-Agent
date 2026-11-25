"""Retry and circuit limits exercised through the configured model runtime."""

import asyncio
import io
import json
import os
from contextlib import closing
from random import Random
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from misaka.ai.api.lazy import lazyApi
from misaka.ai.api.openai_completions import stream
from misaka.ai.runtime_observation import observation_sink
from misaka.ai.types import Context, Model, UserMessage
from misaka.ai.utils.provider_retry import retryProviderRequest
from misaka.ai.utils.retry import (
    RetryPolicy,
    isRetryableAssistantError,
    retryAssistantCall,
)
from misaka.cli.trace import _board_evidence, report
from misaka.core import retry_circuit
from misaka.core.auth_storage import AuthStorage
from misaka.core.network import dispatch, todo
from misaka.core.platform import budget, cards, repo, tasks
from misaka.core.research import runs
from misaka.core.runtime_trace import trace_scope
from tests.http_capture import Reply, captureServer
from tests.model_runtime_test_utils import createInMemoryModelRegistry, getModelRuntime
from tests.test_ai_openai_codex_stream import buildSSEPayload, codexModel, mockToken
from tests.test_ai_provider_retry import _Request, providerError


def _response(status, headers=None, code=None, *, message="fixture"):
    return Reply(status, {"content-type": "application/json", **(headers or {})},
                 json.dumps({"error": {"message": message, "code": code}}).encode())


def _success():
    body = {"id": "fixture", "object": "chat.completion.chunk", "model": "fixture",
            "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}
    return Reply(body=("data: " + json.dumps(body) + "\n\ndata: [DONE]\n\n").encode())


async def _configured(url):
    model = Model.model_validate({
        "id": "fixture", "name": "Fixture", "provider": "runtime-test", "api": "openai-completions",
        "baseUrl": url + "/v1", "reasoning": False, "input": ["text"], "contextWindow": 1000, "maxTokens": 100,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    })
    auth = AuthStorage.inMemory({model.provider: {"type": "api_key", "key": "fixture"}})
    registry = await createInMemoryModelRegistry(auth)

    async def load():
        return SimpleNamespace(stream=stream, streamSimple=stream)

    adapter = lazyApi(load)
    registry.registerProvider(model.provider, {"api": model.api, "streamSimple": adapter.streamSimple,
                                               "stream": adapter.stream})
    return model, getModelRuntime(registry)


async def _configured_codex(url, token=None):
    model = codexModel(baseUrl=url + "/backend-api")
    auth = AuthStorage.inMemory({model.provider: {
        "type": "oauth", "access": token or mockToken(), "refresh": "fixture", "expires": 2**53 - 1,
    }})
    registry = await createInMemoryModelRegistry(auth)
    return model, getModelRuntime(registry)


def _ask(runtime, model, *, session="a", **options):
    return runtime.streamSimple(model, Context(messages=[UserMessage(content="hi", timestamp=0)]),
                                {"sessionId": session, **options}).result()


@pytest.mark.parametrize("status", [400, 413])
async def test_deterministic_errors_ignore_retry_hint(status):
    error = providerError(status, {"x-should-retry": "true", "retry-after-ms": "0"})
    request = _Request(error, "unexpected retry")
    with pytest.raises(Exception) as raised:
        await retryProviderRequest(request, {"maxRetries": 2})
    assert raised.value is error
    assert request.calls == 1


@pytest.mark.parametrize("caps", [{"maxRetryDelayMs": 1000}, {"maxRetryDelayMs": 60_000, "runtimeMaxAgentDelayMs": 1000}])
async def test_delay_cap_reaches_outer_retry_classifier(monkeypatch, caps):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    with captureServer(lambda _: _response(429, {"retry-after": "60"})) as server:
        model, runtime = await _configured(server.baseUrl)
        result = await _ask(runtime, model, maxRetries=0, **caps)
    assert len(server.requests) == 1
    assert result.retryStop == "retry_delay_limit"
    assert not isRetryableAssistantError(result)
    assert tasks.classify_failure(result.errorMessage) == "retry_delay_limit"


@pytest.mark.parametrize("provider_cap, session_cap", [(2000, 1000.5), (1000.5, 2000), (0, 1000.5)])
async def test_codex_sse_obeys_both_delay_caps_before_sleeping(monkeypatch, provider_cap, session_cap):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    waits = []

    async def record_wait(delay, _signal):
        waits.append(delay)

    monkeypatch.setattr("misaka.ai.utils.provider_retry._abortableSleep", record_wait)
    with captureServer(lambda _: _response(503, {"retry-after-ms": "1000.75"})) as server:
        model, runtime = await _configured_codex(server.baseUrl)
        result = await _ask(runtime, model, transport="sse", maxRetries=2, maxRetryDelayMs=provider_cap,
                            runtimeMaxAgentDelayMs=session_cap)
        assert len(server.requests) == 1
    assert waits == []
    assert result.retryStop == "retry_delay_limit"
    assert not isRetryableAssistantError(result)


@pytest.mark.parametrize("failure", [
    _response(429, {"retry-after-ms": "0"}, "rate_limit_exceeded"),
    _response(503, {"retry-after-ms": "0"}),
    Reply(body=b'data: {"type":"response.failed","response":{"error":{"code":"server_error","message":"upstream failed"}}}\n\n'),
    Reply(body=b'data: {"type":"response.created","response":{"id":"incomplete"}}\n\n'),
])
async def test_codex_sse_retries_known_transient_failures_in_the_outer_loop(monkeypatch, failure):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    responses = iter([failure, Reply(body=buildSSEPayload("completed").encode())])
    with captureServer(lambda _: next(responses)) as server:
        model, runtime = await _configured_codex(server.baseUrl)
        results = []

        async def produce():
            result = await _ask(runtime, model, transport="sse")
            results.append(result)
            return result

        result = await retryAssistantCall(produce, RetryPolicy(True, 1, 0), None)
        assert [request.path for request in server.requests] == ["/backend-api/codex/responses"] * 2
        assert all(request.headers["chatgpt-account-id"] == "acc_test" for request in server.requests)
    assert result.stopReason == "stop"
    assert [message.stopReason for message in results] == ["error", "stop"]
    assert isRetryableAssistantError(results[0])


@pytest.mark.parametrize("over_cap", [False, True])
async def test_six_codex_sse_503_turns_emit_only_five_http_attempts(monkeypatch, over_cap):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    terminal = [_response(400), _response(413), _response(429, code="insufficient_quota")]
    last = _response(503, {"retry-after": "120"} if over_cap else {})
    replies = iter([_response(503)] * 4 + terminal + [last])
    observations = []
    with observation_sink(lambda kind, data: observations.append((kind, data))), captureServer(lambda _: next(replies)) as server:
        model, runtime = await _configured_codex(server.baseUrl)
        for _ in range(4):
            assert (await _ask(runtime, model, transport="sse")).retryTransient
        for _ in terminal:
            result = await _ask(runtime, model, transport="sse", maxRetries=2)
            assert result.retryStop == "provider_terminal"
            assert not isRetryableAssistantError(result)
        fifth = await _ask(runtime, model, transport="sse")
        sixth = await _ask(runtime, model, transport="sse")
        assert fifth.retryStop == sixth.retryStop == "circuit_open"
        if over_cap:
            assert fifth.errorMessage.startswith("Server requested 120s retry delay")
        assert len(server.requests) == 8
    assert sum(kind == "http_attempt" for kind, _ in observations) == 8
    assert [data["status_code"] for kind, data in observations if kind == "http_response"] == [503] * 4 + [400, 413, 429, 503]
    assert sum(kind == "circuit_rejected" for kind, _ in observations) == 1


async def test_codex_sse_respects_retry_veto_but_counts_503_toward_circuit(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    with captureServer(lambda _: _response(503, {"x-should-retry": "false"})) as server:
        model, runtime = await _configured_codex(server.baseUrl)
        messages = [await _ask(runtime, model, transport="sse", maxRetries=2) for _ in range(6)]
        assert len(server.requests) == 5
    assert [message.retryStop for message in messages] == ["provider_terminal"] * 4 + ["circuit_open"] * 2
    assert all(not isRetryableAssistantError(message) for message in messages)


async def test_codex_sse_invalid_url_protocol_is_terminal_without_opening_circuit(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "*")
    waits = []

    async def record_wait(delay, _signal):
        waits.append(delay)

    monkeypatch.setattr("misaka.ai.utils.provider_retry._abortableSleep", record_wait)
    model, runtime = await _configured_codex("file:///fixture")
    messages = [await _ask(runtime, model, transport="sse", maxRetries=2) for _ in range(6)]
    assert waits == []
    assert all(message.retryStop == "provider_terminal" and not isRetryableAssistantError(message) for message in messages)
    assert all(message.errorMessage and "protocol" in message.errorMessage for message in messages)


async def test_invalid_local_header_does_not_retry_or_open_the_circuit(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    waits = []

    async def record_wait(delay, _signal):
        waits.append(delay)

    monkeypatch.setattr("misaka.ai.utils.provider_retry._abortableSleep", record_wait)
    with captureServer(lambda _: _success()) as server:
        model, runtime = await _configured(server.baseUrl)
        for _ in range(5):
            failed = await _ask(runtime, model, maxRetries=2, headers={"x-fixture": " invalid "})
            assert failed.retryStop == "provider_terminal"
            assert not failed.retryTransient
        assert len(server.requests) == 0 and waits == []
        assert (await _ask(runtime, model)).stopReason == "stop"
        assert len(server.requests) == 1


def _card_fixture(server):
    from misaka.config import current_config, home

    home.path("models").write_text(json.dumps({"providers": {"card-test": {
        "baseUrl": server.baseUrl + "/v1", "api": "openai-completions", "apiKey": "fixture",
        "models": [{"id": "card-fixture", "contextWindow": 64000, "maxTokens": 2048}],
    }}}), encoding="utf-8")
    home.path("settings").write_text(json.dumps({
        "defaultProvider": "card-test", "defaultModel": "card-fixture", "defaultThinkingLevel": "off",
        "extensions": ["-builtin:mcp", "-builtin:misaka_lcm"],
        "retry": {"enabled": True, "maxRetries": 1, "baseDelayMs": 0, "provider": {"maxRetries": 0}},
    }), encoding="utf-8")
    profile = home.path("profiles_root") / "10032"
    profile.mkdir(parents=True)
    (profile / "SOUL.md").write_text("Fixture Sister", encoding="utf-8")
    return current_config()


@pytest.mark.parametrize("entry", ["worker", "pane", "worker-fallback"])
@pytest.mark.parametrize(("status", "error_code", "expected"), [
    (400, None, "provider_terminal"),
    (413, None, "provider_terminal"),
    (429, "usage_limit_reached", "provider_terminal"),
    (429, "rate_limit_exceeded", "rate_limit"),
])
def test_card_retry_stop_reaches_the_board(tmp_path, monkeypatch, entry, status, error_code, expected):
    from misaka.cli import card_shell, engine
    from misaka.utils import loop_watchdog

    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("MISAKA_OFFLINE", "1")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(loop_watchdog, "configure", lambda *args, **kwargs: None)
    message = budget.EXHAUSTED_MESSAGE if status == 400 else "fixture"
    with captureServer(lambda _: _response(status, {"retry-after-ms": "0"}, error_code, message=message)) as server:
        cfg = _card_fixture(server)
        with closing(tasks.connect(cfg["db"])) as con:
            tid = cards.create(con, str(tmp_path), "Retry fixture", "Return a fixture result.", "10032")
            row = tasks.get(con, tid)
            if entry == "pane":
                assert tasks.claim(con, tid, "pane-fixture", generation=row["generation"])
                monkeypatch.setenv("MISAKA_USAGE_CLAIM_LOCK", "pane-fixture")
                monkeypatch.setenv("MISAKA_USAGE_GENERATION", str(row["generation"]))
                monkeypatch.setenv("MISAKA_USAGE_TASK_ID", tid)
                monkeypatch.setattr("sys.stdin", io.StringIO())
                main = engine.main

                async def print_turn(flags, options):
                    return await main(["--print", *flags], options)

                monkeypatch.setattr(engine, "main", print_turn)
                with patch.dict(os.environ), pytest.raises(SystemExit):
                    card_shell.launch(tid)
            else:
                if entry == "worker-fallback":
                    monkeypatch.setattr(todo.TodoPart, "_settle_failure", lambda *args: None)
                assert dispatch.run_task(con, row, cfg)
            is_transient = expected == "rate_limit"
            current = tasks.get(con, tid)
            assert current["status"] == ("ready" if is_transient else "failed")
            assert bool(current["next_attempt_at"]) is is_transient
            event = json.loads(tasks.latest_payload(con, tid, "cooldown" if is_transient else "gave_up"))
            assert event["failure_kind"] == expected
            assert not dispatch.run_task(con, current, cfg)
            assert len(server.requests) == (2 if is_transient else 1)


async def test_sister_supervisor_keeps_child_retry_stop_when_it_settles_the_card(tmp_path, monkeypatch):
    from misaka.core.network.sister_runtime import SisterRuntime
    from misaka.core.platform import session as engine_session
    from tests.test_runtime_trace_interruption import _interpreter

    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("MISAKA_OFFLINE", "1")
    python, environment = _interpreter()
    spawn = asyncio.create_subprocess_exec

    async def launch_child(executable, *args, **options):
        if args[:2] == ("-m", "misaka.cli.subagent_child"):
            # Force the parent supervisor to settle the attempt through the actual child protocol.
            script = ("from misaka.core.network.todo import TodoPart;"
                      "TodoPart._settle_failure = lambda *args: None;"
                      "from misaka.cli.subagent_child import main; raise SystemExit(main())")
            env = options["env"]
            env.update(PYTHONPATH=environment["PYTHONPATH"], PYTHONNOUSERSITE="1")
            return await spawn(python, "-c", script, *args[2:], **options)
        return await spawn(executable, *args, **options)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch_child)
    with captureServer(lambda _: _response(413, message=budget.EXHAUSTED_MESSAGE)) as server:
        cfg = _card_fixture(server)
        engine, session, error = await engine_session.open_session(["--no-session", "-nt"], str(tmp_path))
        assert error is None
        try:
            with closing(tasks.connect(cfg["db"])) as con:
                tid = cards.create(con, str(tmp_path), "Retry fixture", "Return a fixture result.", "10032")
                runtime = SisterRuntime(session, lambda: con, lambda: cfg)
                try:
                    context = SimpleNamespace(cwd=str(tmp_path), sessionManager=session.sessionManager,
                                              model=session.model, modelRegistry=session.modelRegistry)
                    await runtime.launch(tid, context=context, tool_call_id="retry-fixture")
                    handle = runtime._handles[tid]
                    await asyncio.wait_for(handle.done.wait(), timeout=45)
                    assert handle.agent.retry_stop == "provider_terminal", handle.agent.error
                    row = tasks.get(con, tid)
                    assert row["status"] == "failed" and row["next_attempt_at"] is None
                    assert json.loads(tasks.latest_payload(con, tid, "gave_up"))["failure_kind"] == "provider_terminal"
                    assert len(server.requests) == 1
                finally:
                    await runtime.close()
        finally:
            await engine_session.dispose(engine)


@pytest.mark.parametrize("invalid_token", [False, True])
async def test_codex_sse_trace_counts_attempts_before_response_or_setup_failure(monkeypatch, tmp_path, invalid_token):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("MISAKA_HOME", str(tmp_path / "home"))
    trace_id = "codex-sse"
    expected = int(not invalid_token)
    with trace_scope(trace_id, True), captureServer(lambda _: Reply(body=buildSSEPayload("completed").encode())) as server:
        model, runtime = await _configured_codex(server.baseUrl, token="invalid-fixture-token" if invalid_token else None)
        result = await _ask(runtime, model, transport="sse")
        assert len(server.requests) == expected
    assert result.stopReason == ("error" if invalid_token else "stop")
    assert report(trace_id)["usage"]["http_attempts"] == expected


@pytest.mark.parametrize("status", [408, 503])
async def test_circuit_counts_logical_failures_and_isolates_sessions(monkeypatch, tmp_path, status):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("MISAKA_HOME", str(tmp_path / "home"))
    trace_id = f"circuit-{status}"
    with trace_scope(trace_id, True), captureServer(lambda _: _response(status, {"retry-after-ms": "0"})) as server:
        model, runtime = await _configured(server.baseUrl)
        for attempt in range(5):
            assert (await _ask(runtime, model, maxRetries=1)).stopReason == "error"
            assert len(server.requests) == (attempt + 1) * 2
        blocked = await _ask(runtime, model, maxRetries=1)
        assert len(server.requests) == 10
        assert not isRetryableAssistantError(blocked)
        await _ask(runtime, model, session="b", maxRetries=0)
        assert len(server.requests) == 11
    trace = report(trace_id)
    assert trace["usage"]["circuit_refused"] == 1
    assert trace["usage"]["http_attempts"] == 11


async def test_only_one_half_open_trial_can_use_the_route(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    now = 1000
    monkeypatch.setattr(retry_circuit, "monotonic", lambda: now)
    reached, released = Event(), Event()
    healthy = False

    def respond(_request):
        if healthy:
            reached.set()
            if released.wait(5):
                return _success()
        return _response(503)

    with captureServer(respond) as server:
        model, runtime = await _configured(server.baseUrl)
        events = []
        with observation_sink(lambda event, data: events.append((event, data))):
            for _ in range(5):
                await _ask(runtime, model, maxRetries=0)
            now += 29
            assert (await _ask(runtime, model)).retryStop == "circuit_open"
            assert len(server.requests) == 5
            now += 1
            # A failed probe opens the circuit again, starting a new cooldown.
            assert (await _ask(runtime, model)).retryStop == "circuit_open"
            assert len(server.requests) == 6
            now += 30
            healthy = True
            probe = _ask(runtime, model)
            try:
                assert await asyncio.to_thread(reached.wait, 5)
                assert (await _ask(runtime, model)).retryStop == "circuit_open"
                assert len(server.requests) == 7
            finally:
                released.set()
            assert (await probe).stopReason == "stop"
            assert (await _ask(runtime, model)).stopReason == "stop"
        assert [data["status"] for kind, data in events if kind == "circuit_probe_finished"] == ["transient", "success"]
        assert len(server.requests) == 8


async def test_success_or_an_elapsed_window_clears_failure_streak(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    now = 1000
    monkeypatch.setattr(retry_circuit, "monotonic", lambda: now)
    status = 503
    with captureServer(lambda _: _success() if status == 200 else _response(status)) as server:
        model, runtime = await _configured(server.baseUrl)
        for _ in range(4):
            await _ask(runtime, model)
        now += 121
        for _ in range(4):
            assert (await _ask(runtime, model)).retryStop is None
        status = 200
        assert (await _ask(runtime, model)).stopReason == "stop"
        status = 503
        for _ in range(4):
            assert (await _ask(runtime, model)).retryStop is None
        assert (await _ask(runtime, model)).retryStop == "circuit_open"
        assert len(server.requests) == 14


async def test_local_retry_delays_spread_a_simultaneous_batch(monkeypatch):
    from misaka.ai.utils import provider_retry

    delays = []
    random = Random(23)
    monkeypatch.setattr("misaka.ai.utils.retry.random.randint", random.randint)

    async def wait(ms, _signal):
        delays.append(ms)
        await asyncio.sleep(0)

    monkeypatch.setattr(provider_retry, "_abortableSleep", wait)
    requests = [_Request(providerError(503), "recovered") for _ in range(20)]
    assert await asyncio.gather(*(retryProviderRequest(req, {"maxRetries": 1}) for req in requests)) == ["recovered"] * 20
    assert {req.calls for req in requests} == {2}
    assert len(delays) == 20 and all(0 <= ms <= 500 for ms in delays)
    peak = max(sum(candidate // 100 == bucket for candidate in delays) for bucket in range(6))
    assert peak < 10  # A fixed 500 ms delay schedules all 20 requests together.


def test_research_card_stops_on_fifth_consecutive_deferral(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "task_state_dir", lambda tid: str(tmp_path / tid))
    monkeypatch.setattr(repo, "enabled", lambda *a: False)
    with closing(tasks.connect(str(tmp_path / "board.sqlite"))) as con:
        runs.init(con)
        run = runs.create(con, question="Retry fixture", workspace=str(tmp_path))
        card = cards.create(con, str(tmp_path), "Card", "Retry fixture", "10032")
        runs.link_task(con, run["id"], card, kind="research", node=runs.root(con, run["id"]))
        for attempt in range(1, 6):
            con.execute("UPDATE tasks SET next_attempt_at=NULL WHERE id=?", (card,))
            assert tasks.claim(con, card, f"lock-{attempt}", ttl_seconds=60, generation=1)
            assert tasks.mark_failed(con, card, 1, f"lock-{attempt}", failure_kind="rate_limit", reason="429")
            assert tasks.get(con, card)["status"] == ("failed" if attempt == 5 else "ready")
        assert len(tasks.latest_payload(con, card, "gave_up", generation=1)) > 0
        [stop] = _board_evidence(con, run["id"])
        assert (stop["event"], stop["reason"], stop["failures"], stop["generation"]) == (
            "gave_up", "consecutive_rate_limits", 5, 1,
        )
        runs.reopen_cards(con, run["id"])
        assert tasks.get(con, card)["status"] == "failed"
        assert tasks.get(con, card)["generation"] == 1
        runs.resume(con, run["id"])
        assert tasks.claim(con, card, "new-generation", ttl_seconds=60, generation=2)
        assert tasks.mark_failed(con, card, 2, "new-generation", failure_kind="rate_limit", reason="429")
        assert tasks.get(con, card)["status"] == "ready"


def test_rate_limit_deferrals_remain_unbounded_for_an_ordinary_card(tmp_path):
    with closing(tasks.connect(str(tmp_path / "board.sqlite"))) as con:
        card = cards.create(con, str(tmp_path), "Card", "Retry fixture", "10032")
        for attempt in range(1, 7):
            con.execute("UPDATE tasks SET next_attempt_at=NULL WHERE id=?", (card,))
            assert tasks.claim(con, card, f"lock-{attempt}", ttl_seconds=60, generation=1)
            assert tasks.mark_failed(con, card, 1, f"lock-{attempt}", failure_kind="rate_limit", reason="429")
            assert tasks.get(con, card)["status"] == "ready"
        assert tasks.latest_payload(con, card, "gave_up", generation=1) is None

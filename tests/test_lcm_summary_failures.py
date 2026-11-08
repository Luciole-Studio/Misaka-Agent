"""What happens to a summary when its model fails, through the real call path.

None of this had a test, and the real sessions on this machine failed here 29 times, 26 of them on a
503. A local provider answers by model, so a test can fail the summarizer alone and see where the
summary goes: back to the same model, to the session's main model, or to plain truncation.
"""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from misaka.core.model_registry import ModelRegistry
from misaka.core.model_runtime import ModelRuntime
from misaka.core.session_manager import SessionManager
from misaka.extensions.misaka_lcm.host import context_engine as ce
from misaka.extensions.misaka_lcm.host import llm, storage
from misaka.extensions.misaka_lcm.native import auxiliary_recovery
from misaka.extensions.misaka_lcm.vendor import escalation

SUMMARIZER, MAIN, SPARE = "claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5"
SOURCE = "source text about a river and a harbor " * 80
EVENTS = [
    ("message_start", {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "stub", "content": [], "stop_reason": None,
        "usage": {"input_tokens": 5, "output_tokens": 1}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "The summary."}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}}),
    ("message_stop", {"type": "message_stop"}),
]


class Relay:
    """A provider that fails the models it is told to, and writes down what it was asked."""

    def __init__(self):
        self.failing, self.requests = {}, []

    def fail(self, model, status, message="it failed"):
        self.failing[model] = (status, message)

    def asked(self):
        return [body["model"] for body in self.requests]

    def handler(self):
        relay = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
                relay.requests.append(body)
                failure = relay.failing.get(body["model"])
                if callable(failure):
                    failure = failure(body)
                if failure == "drop":
                    self.close_connection = True
                    return
                if failure is None:
                    data = "".join(f"event: {name}\ndata: {json.dumps(event)}\n\n" for name, event in EVENTS).encode()
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                else:
                    status, message = failure
                    data = json.dumps({"type": "error", "error": {"type": "stub", "message": message}}).encode()
                    self.send_response(status)
                    self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.send_header("connection", "close")
                self.end_headers()
                self.wfile.write(data)
                self.close_connection = True

        return Handler


def model(name):
    return {"id": name, "name": name, "reasoning": True, "input": ["text"], "contextWindow": 200_000, "maxTokens": 8_000,
            "cost": {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0}}


@pytest.fixture
def relay(tmp_path, monkeypatch):
    stub = Relay()
    server = ThreadingHTTPServer(("127.0.0.1", 0), stub.handler())
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    home = tmp_path / "home"
    home.mkdir()
    (home / "models.json").write_text(json.dumps({"providers": {"relay": {
        "name": "Relay", "baseUrl": f"http://127.0.0.1:{server.server_address[1]}", "api": "anthropic-messages",
        "apiKey": "stub-key", "models": [model(SUMMARIZER), model(MAIN), model(SPARE)]}}}))
    stub.settings = home / "settings.json"
    stub.settings.write_text(json.dumps({"auxiliary": {"compression": {"provider": "relay", "model": SUMMARIZER}}}))
    monkeypatch.setenv("MISAKA_HOME", str(home))
    monkeypatch.setattr(auxiliary_recovery, "_TRANSIENT_RETRY_BACKOFF_BASE", 0)
    llm.install()
    registry = ModelRegistry(asyncio.run(ModelRuntime.create()))
    tokens = llm._REGISTRY.set(registry), llm._MODEL.set(registry.find("relay", MAIN))
    yield stub
    llm._MODEL.reset(tokens[1])
    llm._REGISTRY.reset(tokens[0])
    server.shutdown()
    server.server_close()


def summarize(**options):
    return escalation.summarize_with_escalation(SOURCE, 800, 2000, timeout=8, **options)


def test_a_healthy_summarizer_writes_the_summary(relay):
    assert summarize() == ("The summary.", 1)
    assert relay.asked() == [SUMMARIZER]


def test_a_summary_asks_for_the_whole_output_and_no_temperature(relay):
    summarize()
    body = relay.requests[0]
    assert body["max_tokens"] == 8_000 and "temperature" not in body


def test_a_429_hands_the_summary_to_the_main_model(relay):
    relay.fail(SUMMARIZER, 429, "slow down")
    assert summarize() == ("The summary.", 1)
    assert relay.asked() == [SUMMARIZER, MAIN]


def test_a_dropped_connection_retries_then_hands_the_summary_to_the_main_model(relay):
    relay.failing[SUMMARIZER] = "drop"
    assert summarize() == ("The summary.", 1)
    assert relay.asked() == [SUMMARIZER, SUMMARIZER, SUMMARIZER, MAIN]


def test_a_503_retries_the_summarizer_and_never_reaches_the_main_model(relay):
    relay.fail(SUMMARIZER, 503, "No available accounts")
    summary, level = summarize()
    assert level == 3 and summary.startswith("source text")
    assert relay.asked() == [SUMMARIZER] * 6       # three attempts at each of the two model levels


@pytest.mark.parametrize("status, message", [(401, "invalid key"), (402, "insufficient credits"),
                                             (400, "bad request shape"), (404, "model not found")])
def test_these_failures_end_in_truncation_on_the_summarizer(relay, status, message):
    relay.fail(SUMMARIZER, status, message)
    summary, level = summarize()
    assert level == 3 and summary.startswith("source text")
    assert set(relay.asked()) == {SUMMARIZER}


def test_a_429_with_no_main_model_ends_in_truncation(relay):
    llm._MODEL.set(None)
    relay.fail(SUMMARIZER, 429, "slow down")
    assert summarize()[1] == 3
    assert set(relay.asked()) == {SUMMARIZER}


def test_a_429_on_both_models_ends_in_truncation(relay):
    relay.fail(SUMMARIZER, 429, "slow down")
    relay.fail(MAIN, 429, "slow down")
    assert summarize()[1] == 3
    assert relay.asked() == [SUMMARIZER, MAIN] * 2


def test_a_configured_chain_comes_before_the_main_model(relay):
    relay.settings.write_text(json.dumps({"auxiliary": {"compression": {
        "provider": "relay", "model": SUMMARIZER, "fallback_chain": [{"provider": "relay", "model": SPARE}]}}}))
    relay.fail(SUMMARIZER, 429, "slow down")
    assert summarize() == ("The summary.", 1)
    assert relay.asked() == [SUMMARIZER, SPARE]


def test_a_credit_limited_402_retries_with_the_output_it_can_afford(relay):
    relay.failing[SUMMARIZER] = lambda body: None if body["max_tokens"] <= 7_053 else (402, "can only afford 7117 tokens")
    assert summarize() == ("The summary.", 1)
    assert [body["max_tokens"] for body in relay.requests] == [8_000, 7_053]


def test_a_402_marks_the_route_unhealthy(relay, caplog):
    relay.fail(SUMMARIZER, 402, "insufficient credits")
    summarize()
    assert "unhealthy" in caplog.text


def test_two_failed_summaries_open_the_route_s_circuit(relay):
    relay.fail(SUMMARIZER, 503, "No available accounts")
    breaker = escalation.SummaryCircuitBreaker()
    summarize(circuit_breaker=breaker)
    assert not breaker.allows("")
    before = len(relay.requests)
    assert summarize(circuit_breaker=breaker)[1] == 3
    assert len(relay.requests) == before


def test_the_spend_guard_stops_calling_the_model(relay):
    guard = escalation.SummarySpendGuard(max_calls=1, window_seconds=600, backoff_seconds=600)
    assert summarize(spend_guard=guard)[1] == 1
    before = len(relay.requests)
    assert summarize(spend_guard=guard)[1] == 3
    assert len(relay.requests) == before


def ignored_session(tmp_path, monkeypatch):
    monkeypatch.setenv("MISAKA_HOME", str(tmp_path / "ignored-home"))
    monkeypatch.setenv("LCM_IGNORE_SESSION_PATTERNS", "*")
    ce.close_all()
    project = tmp_path / "project"
    project.mkdir()
    manager = SessionManager.create(str(project), str(tmp_path / "sessions"))
    manager.appendCustomEntry("lcm-project", {"workspace": str(project.resolve())})
    return storage.context(SimpleNamespace(cwd=str(project), sessionManager=manager, model=None), str(project), "test")


def test_a_session_lcm_ignores_gets_a_summary_from_the_native_compressor(tmp_path, monkeypatch):
    ctx = ignored_session(tmp_path, monkeypatch)
    asked = []

    def call_llm(**kwargs):
        asked.append(kwargs.get("task"))
        message = SimpleNamespace(content="## Goal\nKeep the ledger.", reasoning=None, reasoning_content=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=None)

    monkeypatch.setattr(llm, "call_llm", call_llm)
    for index in range(24):
        ctx.sessionManager.appendMessage({"role": "user", "content": f"fact {index} " + "harbor ledger entry " * 60,
                                          "timestamp": 1789460410000 + index})
    ce.sync(ctx)
    assert ce.bound_engine(ctx)._bypasses_lcm_context_management()
    native = ctx.sessionManager.buildSessionContext().messages
    result = ce.compact(ce.prepare({"reason": "manual", "messages": native}, ctx))
    ce.close_all()
    assert result.summary == "LCM host_fallback" and asked == ["compression"]
    assert len(result.contextMessages) < len(native)
    assert "[CONTEXT COMPACTION" in str(result.contextMessages) and "Keep the ledger." in str(result.contextMessages)

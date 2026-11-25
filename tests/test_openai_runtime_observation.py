import json

import pytest

from misaka.ai.api.openai_completions import stream
from misaka.ai.runtime_observation import observation_context, observation_sink
from misaka.ai.types import Context, Model, UserMessage
from misaka.cli.trace import _usage
from misaka.utils.js import AbortController
from tests.http_capture import Reply, captureServer


def _model(url):
    return Model.model_validate({
        "id": "fixture", "name": "Fixture", "provider": "test", "api": "openai-completions", "baseUrl": url + "/v1",
        "reasoning": False, "input": ["text"], "contextWindow": 1000, "maxTokens": 100,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    })


def _reply(usage, *, finished=True):
    chunk = {"id": "fixture-response", "object": "chat.completion.chunk", "model": "fixture",
             "choices": [{"index": 0, "delta": {"content": "text"}, "finish_reason": "stop" if finished else None}]}
    if usage is not None:
        chunk["usage"] = usage
    return Reply(body=("data: " + json.dumps(chunk) + "\n\n" + ("data: [DONE]\n\n" if finished else "")).encode())


async def _observe(server, **options):
    observations = []
    with observation_sink(lambda kind, data: observations.append((kind, data))), observation_context(request_id="local-request"):
        context = Context(messages=[UserMessage(content="hi", timestamp=0)])
        message = await stream(_model(server.baseUrl), context, {"apiKey": "fixture", **options}).result()
    assert all(data["request_id"] == "local-request" for _, data in observations)
    return message, observations


@pytest.mark.parametrize("reported_total", [12, 15])
async def test_provider_usage_compares_independent_reported_total(monkeypatch, reported_total):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    raw = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": reported_total,
           "prompt_cache_hit_tokens": 3, "prompt_cache_miss_tokens": 7,
           "completion_tokens_details": {"reasoning_tokens": 1}, "unexpected": "not numeric metadata"}
    with captureServer(lambda _request: _reply(raw)) as server:
        result, observations = await _observe(server)

    assert result.stopReason == "stop"
    record = dict(observations)["provider_usage"]
    assert record["status"] == ("reported" if reported_total == 12 else "conflicting")
    assert record["raw"]["total_tokens"] == reported_total
    assert "unexpected" not in record["raw"]
    assert record["normalized"] == {"input": 7, "output": 2, "cache_read": 3, "cache_write": 0, "total": 12, "reasoning": 1}
    assert record["issues"] == ([] if reported_total == 12 else ["total_tokens_mismatch"])


@pytest.mark.parametrize("cache", [10, -3])
async def test_conflicting_provider_counts_are_kept_for_inspection_but_not_summed(monkeypatch, cache):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    valid = {"prompt_tokens": 7, "completion_tokens": 1, "total_tokens": 8}
    inconsistent = {"prompt_tokens": 2, "completion_tokens": 0, "total_tokens": 2, "cached_tokens": cache}
    with captureServer(lambda _request: _reply(inconsistent)) as server:
        _, bad = await _observe(server)
    with captureServer(lambda _request: _reply(valid)) as server:
        _, other = await _observe(server)
    record = dict(bad)["provider_usage"]
    assert record["raw"]["cached_tokens"] == cache
    assert record["issues"]
    events = [{"event": "provider_usage", **record},
              {"event": "provider_usage", **dict(other)["provider_usage"], "request_id": "good"}]
    result = _usage(events)
    assert result["conflicting"] == 1 and result["reported"] == 1
    assert result["reported_tokens"]["total"] == 8
    # Older v1 traces used the reported status even when they carried a warning.
    events[0]["status"] = "reported"
    assert _usage(events) == result


@pytest.mark.parametrize("reported", [True, False])
async def test_explicit_zero_is_distinct_from_missing_usage_when_stream_ends(monkeypatch, reported):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    raw = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0} if reported else None
    with captureServer(lambda _request: _reply(raw, finished=reported)) as server:
        result, observations = await _observe(server)

    assert result.stopReason == ("stop" if reported else "error")
    record = dict(observations)["provider_usage"]
    assert record["status"] == ("reported" if reported else "missing")
    if reported:
        assert record["normalized"]["total"] == 0
    else:
        assert "normalized" not in record
    summary = _usage([{"event": "provider_usage", **record}])
    assert summary["missing"] == int(not reported)
    assert (None if not reported else 0) == (summary["reported_tokens"] or {}).get("total")


async def test_partial_provider_usage_keeps_field_presence_outside_reported_totals(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    with captureServer(lambda _request: _reply({"prompt_tokens": 9, "completion_tokens": None})) as server:
        _, observations = await _observe(server)
    record = dict(observations)["provider_usage"]
    assert record["status"] == "partial"
    assert record["raw"] == {"prompt_tokens": 9}
    assert record["present"] == ["prompt_tokens", "completion_tokens"]
    summary = _usage([{"event": "provider_usage", **record}])
    assert summary["partial"] == 1 and summary["reported_tokens"] is None


@pytest.mark.parametrize("code,attempts,stop", [("rate_limited", 2, "stop"), ("insufficient_quota", 1, "error")])
async def test_http_attempt_events_match_transport_retry_and_quota_rejection(monkeypatch, code, attempts, stop):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")

    def reply(_request):
        if len(server.requests) == 1:
            body = json.dumps({"error": {"message": "fixture", "code": code}}).encode()
            return Reply(429, {"content-type": "application/json", "retry-after-ms": "1"}, body)
        return _reply({"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12})

    with captureServer(reply) as server:
        result, observations = await _observe(server, maxRetries=1)

    assert len(server.requests) == attempts
    assert result.stopReason == stop
    assert [data["attempt"] for kind, data in observations if kind == "http_attempt"] == list(range(1, attempts + 1))
    statuses = [data["status_code"] for kind, data in observations if kind == "http_response"]
    assert statuses == ([429, 200] if attempts == 2 else [429])
    assert dict(observations)["provider_usage"]["status"] == ("reported" if attempts == 2 else "missing")


async def test_abort_before_dispatch_has_no_http_attempt(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    controller = AbortController()
    controller.abort()
    with captureServer() as server:
        result, observations = await _observe(server, signal=controller.signal)

    assert result.stopReason == "aborted"
    assert not server.requests
    assert [kind for kind, _ in observations] == ["http_observable", "provider_usage"]


def test_circuit_and_budget_refusals_are_distinct_from_missing_usage():
    requests = [
        {"event": "model_started", "request_id": "stopped"},
        {"event": "circuit_rejected", "request_id": "stopped"},
        {"event": "model_finished", "request_id": "stopped", "http_attempts": 0},
        {"event": "budget_refused", "request_id": "over-budget"},
        {"event": "model_finished", "request_id": "stream-interrupted"},
    ]
    result = _usage(requests)
    assert result["requests"] == 3 and result["http_attempts"] == 0
    assert result["circuit_refused"] == result["budget_refused"] == result["missing"] == 1
    assert result["reported_tokens"] is None
